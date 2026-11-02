#!/usr/bin/env python3
"""Download ruutu.fi videos and live streams to MP4, optionally through a SOCKS5 tunnel.

Pure standard library. The only external dependency is ffmpeg (HLS download + remux to MP4).
By default the script connects directly. With --proxy all traffic (login, API, download) goes
through SOCKS5, because stream tokens are bound to the IP that requested them from the media API.
ffmpeg and media players read playlists from a local HLS relay that fetches segments through SOCKS.

Usage:
  ruutu_dl.py https://www.ruutu.fi/video/100128341
  ruutu_dl.py --proxy socks5://127.0.0.1:7654 -u user@example.com -p 'secret' https://www.ruutu.fi/video/100128341
Credentials may also come from RUUTU_USER / RUUTU_PASS. Tokens are cached in ~/.ruutu-dl/auth.json
and reused until they expire (~24 h); afterwards they are renewed with the refresh token.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import http.client
import http.server
import json
import os
import re
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Mapping
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
)
RUUTU_ORIGIN = "https://www.ruutu.fi"
SSO_INIT_URL = (
    "https://auth-api.nm-ovp.nelonenmedia.fi/v2/sso/init/login"
    "?client=ruutu-web&widget=true"
    "&redirect_uri=https%3A%2F%2Fwww.ruutu.fi%2Fsacu%2Fauthorized"
    "&ref_url=https%3A%2F%2Fwww.ruutu.fi%2F&region=fi"
)
LOGIN_URL = "https://tili.sanoma.fi/trpc/login.login"
MEDIA_URL = "https://mcc.nm-ovp.nelonenmedia.fi/v2/media/{media_id}"
RENEW_URL = "https://www.ruutu.fi/api/renew?token=auth"
VLC_BIN = "/Applications/VLC.app/Contents/MacOS/VLC"
AUTH_CACHE = Path.home() / ".ruutu-dl" / "auth.json"
TOKEN_SAFETY_MARGIN_S = 300
STREAM_HEADERS = {"Origin": RUUTU_ORIGIN, "Referer": RUUTU_ORIGIN + "/"}


@dataclass(frozen=True)
class SocksProxy:
    host: str
    port: int

    @property
    def url(self) -> str:
        return f"socks5://{self.host}:{self.port}"


def parse_proxy(value: str) -> SocksProxy | None:
    """Accepts 'socks5://host:port', 'socks5h://host:port', 'host:port' or 'none'."""
    text = value.strip()
    if text.lower() in ("", "none", "off", "direct"):
        return None
    if "://" in text:
        parts = urlsplit(text)
        if parts.scheme not in ("socks5", "socks5h", "socks"):
            raise argparse.ArgumentTypeError(f"Only SOCKS5 proxies are supported, not: {parts.scheme}")
        if not parts.hostname or not parts.port:
            raise argparse.ArgumentTypeError(f"Proxy needs a host and a port: {value}")
        return SocksProxy(parts.hostname, parts.port)
    host, sep, port = text.rpartition(":")
    if not sep or not port.isdigit():
        raise argparse.ArgumentTypeError(f"Proxy must be host:port or socks5://host:port: {value}")
    return SocksProxy(host or "127.0.0.1", int(port))


@dataclass
class HttpResponse:
    status: int
    headers: list[tuple[str, str]]
    body: bytes

    def header(self, name: str) -> str | None:
        wanted = name.lower()
        return next((v for k, v in self.headers if k.lower() == wanted), None)

    def set_cookies(self) -> dict[str, str]:
        jar: dict[str, str] = {}
        for k, v in self.headers:
            if k.lower() != "set-cookie":
                continue
            cookie = SimpleCookie()
            cookie.load(v)
            for morsel in cookie.values():
                jar[morsel.key] = morsel.value
        return jar

    def json(self) -> dict:
        return json.loads(self.body.decode("utf-8"))

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


@dataclass
class AuthTokens:
    access_token: str
    refresh_token: str

    def is_valid(self) -> bool:
        return jwt_expiry(self.access_token) - TOKEN_SAFETY_MARGIN_S > time.time()


@dataclass
class Session:
    """Minimal HTTPS client over an optional SOCKS5 proxy, with per-host cookies."""

    proxy: SocksProxy | None
    timeout: float = 30.0
    debug: bool = False
    cookies: dict[str, dict[str, str]] = field(default_factory=dict)

    def cookies_for(self, host: str) -> dict[str, str]:
        return self.cookies.setdefault(host, {})

    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
    ) -> HttpResponse:
        parts = urlsplit(url)
        if parts.scheme != "https":
            raise ValueError(f"Only https URLs are supported: {url}")
        host = parts.hostname or ""
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"
        all_headers = {
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
            "Accept-Language": "fi-FI,fi;q=0.9,en;q=0.8",
            "Connection": "close",
        }
        host_cookies = self.cookies_for(host)
        if host_cookies:
            all_headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in host_cookies.items())
        if headers:
            all_headers.update(headers)
        conn = SocksHttpsConnection(self.proxy, host, parts.port or 443, self.timeout)
        try:
            conn.request(method, path, body=body, headers=all_headers)
            resp = conn.getresponse()
            result = HttpResponse(resp.status, resp.getheaders(), resp.read())
        finally:
            conn.close()
        host_cookies.update(result.set_cookies())
        if self.debug:
            print(f"[debug] {method} {url[:120]} -> {result.status}", file=sys.stderr)
            for k, v in result.headers:
                if k.lower() in ("location", "server", "x-cache", "content-type", "x-amzn-errortype", "x-amzn-requestid"):
                    print(f"[debug]   {k}: {v[:160]}", file=sys.stderr)
            if result.status >= 400:
                print(f"[debug]   body: {result.body[:500]!r}", file=sys.stderr)
        return result


class SocksHttpsConnection(http.client.HTTPSConnection):
    def __init__(self, proxy: SocksProxy | None, host: str, port: int, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._proxy = proxy

    def connect(self) -> None:
        if self._proxy is None:
            super().connect()
            return
        raw = socks5_connect(self._proxy, self.host, self.port, self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


def socks5_connect(proxy: SocksProxy, host: str, port: int, timeout: float) -> socket.socket:
    sock = socket.create_connection((proxy.host, proxy.port), timeout=timeout)
    sock.sendall(b"\x05\x01\x00")
    if recv_exact(sock, 2) != b"\x05\x00":
        sock.close()
        raise ConnectionError("SOCKS5: proxy rejected the no-auth handshake")
    host_bytes = host.encode("idna")
    sock.sendall(b"\x05\x01\x00\x03" + bytes([len(host_bytes)]) + host_bytes + struct.pack(">H", port))
    version, reply, _, address_type = recv_exact(sock, 4)
    if version != 5 or reply != 0:
        sock.close()
        raise ConnectionError(f"SOCKS5: CONNECT to {host}:{port} rejected (code {reply})")
    if address_type == 1:
        recv_exact(sock, 4)
    elif address_type == 3:
        recv_exact(sock, recv_exact(sock, 1)[0])
    elif address_type == 4:
        recv_exact(sock, 16)
    recv_exact(sock, 2)
    return sock


def recv_exact(sock: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("SOCKS5: connection closed during handshake")
        data += chunk
    return data


def jwt_expiry(token: str) -> float:
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return float(payload.get("exp", 0))
    except (IndexError, ValueError, json.JSONDecodeError):
        return 0.0


def load_cached_tokens() -> AuthTokens | None:
    if not AUTH_CACHE.exists():
        return None
    data = json.loads(AUTH_CACHE.read_text())
    return AuthTokens(data["access_token"], data["refresh_token"])


def save_tokens(tokens: AuthTokens) -> None:
    AUTH_CACHE.parent.mkdir(parents=True, exist_ok=True)
    AUTH_CACHE.write_text(json.dumps(tokens.__dict__, indent=2))
    AUTH_CACHE.chmod(0o600)


def renew_tokens(session: Session, tokens: AuthTokens) -> AuthTokens | None:
    """Renews an expired access token the way the web player does: POST /api/renew with the auth cookies."""
    session.cookies_for("www.ruutu.fi").update(
        {"authAccessToken": tokens.access_token, "authRefreshToken": tokens.refresh_token}
    )
    resp = session.request("POST", RENEW_URL, headers={**STREAM_HEADERS, "Content-Length": "0"}, body=b"")
    if resp.status != 200:
        return None
    try:
        data = resp.json()
    except (ValueError, UnicodeDecodeError):
        return None
    access, refresh = data.get("authAccessToken"), data.get("authRefreshToken")
    if not access or not refresh:
        return None
    return AuthTokens(access, refresh)


def obtain_tokens(session: Session, user: str | None, password: str | None, relogin: bool) -> AuthTokens | None:
    cached = None if relogin else load_cached_tokens()
    if cached and cached.is_valid():
        return cached
    if cached and cached.refresh_token:
        print("Access token expired, renewing via /api/renew…", file=sys.stderr)
        renewed = renew_tokens(session, cached)
        if renewed:
            save_tokens(renewed)
            print("Token renewed.", file=sys.stderr)
            return renewed
        print("Renewal failed.", file=sys.stderr)
    if user and password:
        print("Logging in to Sanoma SSO…", file=sys.stderr)
        tokens = login(session, user, password)
        save_tokens(tokens)
        print(f"Logged in, tokens cached in {AUTH_CACHE}", file=sys.stderr)
        return tokens
    print("No credentials and no valid token, trying without login.", file=sys.stderr)
    return None


def login(session: Session, username: str, password: str) -> AuthTokens:
    init = session.request("GET", SSO_INIT_URL)
    login_page_url = init.header("Location")
    if init.status != 302 or not login_page_url:
        raise RuntimeError(f"SSO init: expected 302, got {init.status}")
    query = urlsplit(login_page_url).query
    params = {k: v[0] for k, v in parse_qs(query).items()}

    page = session.request("GET", login_page_url, headers=navigation_headers(RUUTU_ORIGIN + "/"))
    if page.status != 200:
        raise RuntimeError(f"Login page returned HTTP {page.status}: {page.body[:300]!r}")
    csrf_cookie = session.cookies_for("tili.sanoma.fi").get("_sso_csrf", "")
    csrf_token = csrf_cookie.replace("%7C", "|").split("|")[0]
    if not csrf_token:
        raise RuntimeError("No _sso_csrf cookie after loading the login page")

    params.setdefault("region", "fi-FI")
    payload = {
        "client_id": params["client_id"],
        "redirect_uri": params["redirect_uri"],
        "state": params["state"],
        "scope": None,
        "code_challenge": None,
        "code_challenge_method": None,
        "queryString": "?" + urlencode({**params, "email": username}),
        "service": params.get("service", "nelonen"),
        "username": username,
        "password": password,
        "failedAttempts": 0,
    }
    resp = session.request(
        "POST",
        LOGIN_URL,
        headers={
            "Content-Type": "application/json",
            "Origin": "https://tili.sanoma.fi",
            "Referer": login_page_url,
            "X-CSRF-Token": csrf_token,
        },
        body=json.dumps(payload).encode("utf-8"),
    )
    if resp.status != 200:
        raise RuntimeError(f"Login rejected (HTTP {resp.status}): {resp.body[:300]!r}")
    data = resp.json().get("result", {}).get("data", {})
    redirect_uri = data.get("redirectUri")
    if not redirect_uri:
        raise RuntimeError(f"Login failed: {data.get('message') or resp.body[:300]!r}")

    authorized = session.request(
        "GET",
        redirect_uri + "&style=ruutu3&remember_me=true",
        headers=navigation_headers("https://tili.sanoma.fi/"),
    )
    cookies = authorized.set_cookies()
    if "authAccessToken" not in cookies:
        raise RuntimeError(
            f"No authAccessToken after authorization (HTTP {authorized.status}): {authorized.body[:300]!r}"
        )
    return AuthTokens(cookies["authAccessToken"], cookies.get("authRefreshToken", ""))


def navigation_headers(referer: str) -> dict[str, str]:
    return {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": referer,
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Site": "cross-site",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-User": "?1",
        "Sec-Fetch-Dest": "document",
    }


def fetch_media(session: Session, media_id: str, tokens: AuthTokens | None) -> dict:
    headers = dict(STREAM_HEADERS)
    if tokens:
        headers["Authorization"] = f"Bearer {tokens.access_token}"
    resp = session.request("GET", MEDIA_URL.format(media_id=media_id), headers=headers)
    if resp.status != 200:
        raise RuntimeError(f"Media API returned HTTP {resp.status}: {resp.body[:300]!r}")
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"Media API: success=false: {json.dumps(data)[:300]}")
    if data.get("type") == "video-stream":
        video_id = data.get("videoId")
        if not video_id:
            raise RuntimeError(f"Event '{data.get('name')}' has no media yet (status: {data.get('statusMessage')})")
        print(f"Live event '{data.get('name')}' ({data.get('statusMessage')}), media {video_id}", file=sys.stderr)
        return fetch_media(session, str(video_id), tokens)
    if "clip" not in data:
        raise RuntimeError(f"Unknown media API response: type={data.get('type')}, keys={sorted(data)}")
    return data


def pick_hls_url(media: dict) -> str:
    playback = media["clip"]["playback"]
    if playback.get("drm", {}).get("enabled"):
        raise RuntimeError("This media is DRM protected and cannot be saved.")
    urls = playback["media"]["streamUrls"]
    for key in ("webHls", "apple", "android"):
        url = (urls.get(key) or {}).get("url")
        if url:
            return url
    available = sorted(k for k, v in urls.items() if v)
    raise RuntimeError(f"No HLS stream available, found: {available}")


def parse_media_id(url: str) -> str:
    """Accepts https://www.ruutu.fi/video/<id>, /stream/<id>, any /<section>/<id>, or a bare <id>."""
    text = url.strip()
    if text.isdigit():
        return text
    match = re.search(r"ruutu\.fi/(?:[a-z-]+/)*?(\d+)(?:[/?#]|$)", text)
    if not match:
        raise argparse.ArgumentTypeError(f"Cannot extract a media ID from: {url}")
    return match.group(1)


@dataclass(frozen=True)
class HlsVariant:
    url: str
    bandwidth: int
    resolution: str
    audio_group: str | None
    stream_inf: str


def parse_attributes(attribute_list: str) -> dict[str, str]:
    return {k: v.strip('"') for k, v in re.findall(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', attribute_list)}


def parse_master_playlist(master_url: str, text: str) -> list[HlsVariant]:
    """Returns the #EXT-X-STREAM-INF variants. An empty list means this is already a media playlist."""
    variants: list[HlsVariant] = []
    pending: dict[str, str] | None = None
    pending_line = ""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-STREAM-INF:"):
            pending = parse_attributes(line[len("#EXT-X-STREAM-INF:"):])
            pending_line = line
        elif pending is not None and line and not line.startswith("#"):
            variants.append(
                HlsVariant(
                    url=urljoin(master_url, line),
                    bandwidth=int(pending.get("AVERAGE-BANDWIDTH") or pending.get("BANDWIDTH") or 0),
                    resolution=pending.get("RESOLUTION", "?"),
                    audio_group=pending.get("AUDIO"),
                    stream_inf=drop_attributes(pending_line, ("SUBTITLES", "CLOSED-CAPTIONS")),
                )
            )
            pending = None
    return variants


def drop_attributes(tag_line: str, names: tuple[str, ...]) -> str:
    """Removes the given attributes from an HLS tag line, keeping the rest verbatim."""
    for name in names:
        tag_line = re.sub(rf',?{name}=("[^"]*"|[^,]*)', "", tag_line)
    return tag_line.replace(":,", ":")


@dataclass(frozen=True)
class SelectedStreams:
    """Best variant of a master playlist, its separate audio rendition (if any) and a filtered master
    that contains only those, with absolute URIs so the relay can rewrite them."""

    video_url: str
    audio_url: str | None
    filtered_master: str


def select_streams(master_url: str, text: str) -> SelectedStreams:
    variants = parse_master_playlist(master_url, text)
    if not variants:
        return SelectedStreams(master_url, None, text)
    best = max(variants, key=lambda v: v.bandwidth)
    listing = ", ".join(f"{v.resolution}@{v.bandwidth // 1000}k" for v in sorted(variants, key=lambda v: v.bandwidth))
    print(f"HLS variants: {listing}. Using {best.resolution}@{best.bandwidth // 1000}k.", file=sys.stderr)

    audio_url: str | None = None
    lines = ["#EXTM3U"]
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(("#EXT-X-VERSION", "#EXT-X-INDEPENDENT-SEGMENTS")):
            lines.append(line)
        elif line.startswith("#EXT-X-MEDIA:") and best.audio_group:
            attrs = parse_attributes(line[len("#EXT-X-MEDIA:"):])
            if attrs.get("TYPE") == "AUDIO" and attrs.get("GROUP-ID") == best.audio_group and attrs.get("URI"):
                absolute = urljoin(master_url, attrs["URI"])
                lines.append(re.sub(r'URI="[^"]*"', f'URI="{absolute}"', line))
                if audio_url is None or attrs.get("DEFAULT") == "YES":
                    audio_url = absolute
    lines += [best.stream_inf, best.url]
    return SelectedStreams(best.url, audio_url, "\n".join(lines) + "\n")


@dataclass(frozen=True)
class Segment:
    url: str
    duration: float
    sequence: int


@dataclass(frozen=True)
class MediaPlaylist:
    init_url: str | None
    segments: list[Segment]
    target_duration: float
    ended: bool

    @property
    def fragmented_mp4(self) -> bool:
        return self.init_url is not None


def parse_media_playlist(playlist_url: str, text: str) -> MediaPlaylist:
    init_url: str | None = None
    segments: list[Segment] = []
    target = 6.0
    sequence = 0
    duration: float | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXT-X-MAP:"):
            init_url = urljoin(playlist_url, parse_attributes(line[len("#EXT-X-MAP:"):]).get("URI", ""))
        elif line.startswith("#EXT-X-TARGETDURATION:"):
            target = float(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            sequence = int(line.split(":", 1)[1])
        elif line.startswith("#EXTINF:"):
            duration = float(line.split(":", 1)[1].split(",", 1)[0])
        elif line and not line.startswith("#"):
            segments.append(Segment(urljoin(playlist_url, line), duration or target, sequence))
            sequence += 1
            duration = None
    return MediaPlaylist(init_url, segments, target, "#EXT-X-ENDLIST" in text)


def fetch_media_playlist(session: Session, url: str) -> MediaPlaylist:
    resp = session.request("GET", url, headers=STREAM_HEADERS)
    if resp.status != 200:
        raise RuntimeError(f"Media playlist returned HTTP {resp.status}: {resp.body[:200]!r}")
    return parse_media_playlist(url, resp.text())


def fetch_bytes(session: Session, url: str) -> bytes:
    for attempt in range(3):
        try:
            resp = session.request("GET", url, headers=STREAM_HEADERS)
        except (OSError, ConnectionError) as exc:
            if attempt == 2:
                raise
            print(f"\nRetrying segment after error: {exc}", file=sys.stderr)
            continue
        if resp.status == 200:
            return resp.body
        if attempt == 2 or resp.status < 500:
            raise RuntimeError(f"Segment returned HTTP {resp.status}: {url[-80:]}")
    raise RuntimeError("unreachable")


class Progress:
    def __init__(self, total_segments: int | None) -> None:
        self.total = total_segments
        self.done = 0
        self.bytes = 0
        self.seconds = 0.0
        self.started = time.monotonic()

    def step(self, size: int, seconds: float) -> None:
        self.done += 1
        self.bytes += size
        self.seconds += seconds
        elapsed = max(time.monotonic() - self.started, 1e-6)
        total = f"/{self.total}" if self.total else ""
        print(
            f"\rsegments {self.done}{total}  {self.bytes / (1 << 20):8.1f} MiB  "
            f"{self.seconds:7.0f} s of media  {self.bytes / elapsed / (1 << 20):5.1f} MiB/s",
            end="",
            file=sys.stderr,
            flush=True,
        )

    def finish(self) -> None:
        print(file=sys.stderr)


# --- MP4 (ISO BMFF) helpers: enough to merge two single-track fragmented MP4 streams into one file -------------


@dataclass(frozen=True)
class Box:
    type: bytes
    start: int
    end: int
    header: int

    @property
    def payload(self) -> slice:
        return slice(self.start + self.header, self.end)


def iter_boxes(data: bytes, start: int = 0, end: int | None = None) -> list[Box]:
    end = len(data) if end is None else end
    boxes: list[Box] = []
    pos = start
    while pos + 8 <= end:
        size = int.from_bytes(data[pos : pos + 4], "big")
        box_type = data[pos + 4 : pos + 8]
        header = 8
        if size == 1:
            size = int.from_bytes(data[pos + 8 : pos + 16], "big")
            header = 16
        elif size == 0:
            size = end - pos
        if size < header:
            raise RuntimeError(f"Corrupt MP4 box {box_type!r} at offset {pos}")
        boxes.append(Box(box_type, pos, pos + size, header))
        pos += size
    return boxes


def child_box(data: bytes, parent: Box, box_type: bytes) -> Box | None:
    return next((b for b in iter_boxes(data, parent.start + parent.header, parent.end) if b.type == box_type), None)


def make_box(box_type: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), box_type) + payload


def fullbox_track_id_offset(data: bytes, box: Box) -> int:
    """Offset of track_ID inside tkhd: after version/flags, creation and modification times (32 or 64 bit)."""
    version = data[box.start + box.header]
    return box.start + box.header + (4 + 16 if version == 1 else 4 + 8)


def set_u32(data: bytearray, offset: int, value: int) -> None:
    data[offset : offset + 4] = value.to_bytes(4, "big")


@dataclass
class InitSegment:
    ftyp: bytes
    mvhd: bytes
    trak: bytes
    trex: bytes

    @staticmethod
    def parse(data: bytes, new_track_id: int) -> InitSegment:
        top = {b.type: b for b in iter_boxes(data)}
        moov = top.get(b"moov")
        if moov is None:
            raise RuntimeError("Init segment has no moov box")
        mvhd, trak, mvex = (child_box(data, moov, t) for t in (b"mvhd", b"trak", b"mvex"))
        if mvhd is None or trak is None or mvex is None:
            raise RuntimeError("Init segment moov lacks mvhd/trak/mvex")
        trex = child_box(data, mvex, b"trex")
        tkhd = child_box(data, trak, b"tkhd")
        if trex is None or tkhd is None:
            raise RuntimeError("Init segment lacks trex/tkhd")
        buf = bytearray(data)
        set_u32(buf, fullbox_track_id_offset(data, tkhd), new_track_id)
        set_u32(buf, trex.start + trex.header + 4, new_track_id)
        ftyp = top.get(b"ftyp")
        return InitSegment(
            ftyp=bytes(buf[ftyp.start : ftyp.end]) if ftyp else make_box(b"ftyp", b"iso6\x00\x00\x00\x01iso6cmfc"),
            mvhd=bytes(buf[mvhd.start : mvhd.end]),
            trak=bytes(buf[trak.start : trak.end]),
            trex=bytes(buf[trex.start : trex.end]),
        )


def build_moov(video: InitSegment, audio: InitSegment | None) -> bytes:
    mvhd = bytearray(video.mvhd)
    set_u32(mvhd, len(mvhd) - 4, 3 if audio else 2)
    traks = video.trak + (audio.trak if audio else b"")
    mvex = make_box(b"mvex", video.trex + (audio.trex if audio else b""))
    return make_box(b"moov", bytes(mvhd) + traks + mvex)


def rewrite_fragment(data: bytes, track_id: int, sequence: int) -> bytes:
    """Keeps only moof/mdat boxes, sets tfhd track_ID and mfhd sequence_number. Sizes never change,
    so trun data offsets stay valid."""
    buf = bytearray(data)
    out = bytearray()
    for box in iter_boxes(data):
        if box.type == b"moof":
            for child in iter_boxes(data, box.start + box.header, box.end):
                if child.type == b"mfhd":
                    set_u32(buf, child.start + child.header + 4, sequence)
                elif child.type == b"traf":
                    tfhd = child_box(data, child, b"tfhd")
                    if tfhd is not None:
                        set_u32(buf, tfhd.start + tfhd.header + 4, track_id)
            out += buf[box.start : box.end]
        elif box.type == b"mdat":
            out += buf[box.start : box.end]
    return bytes(out)


# --- stdlib downloaders -------------------------------------------------------------------------------------------


def download_vod(session: Session, video: MediaPlaylist, audio: MediaPlaylist | None, output: Path) -> Path:
    """Pure-Python VOD download. Fragmented MP4 tracks are merged into one MP4; MPEG-TS segments are concatenated."""
    if not video.fragmented_mp4:
        if audio is not None:
            raise RuntimeError("MPEG-TS video with separate audio needs ffmpeg to be muxed into one file.")
        output = output.with_suffix(".ts")
        print(f"MPEG-TS segments: concatenating into {output}", file=sys.stderr)
        concatenate_segments(session, video, output)
        return output
    if audio is not None and not audio.fragmented_mp4:
        raise RuntimeError("Mixed fMP4 video and MPEG-TS audio needs ffmpeg.")

    output = output.with_suffix(".mp4")
    video_init = InitSegment.parse(fetch_bytes(session, video.init_url or ""), 1)
    audio_init = InitSegment.parse(fetch_bytes(session, audio.init_url or ""), 2) if audio else None
    total = len(video.segments) + (len(audio.segments) if audio else 0)
    progress = Progress(total)
    sequence = 1
    with output.open("wb") as fh, concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        fh.write(video_init.ftyp + build_moov(video_init, audio_init))
        video_futures = [pool.submit(fetch_bytes, session, s.url) for s in video.segments]
        audio_futures = [pool.submit(fetch_bytes, session, s.url) for s in audio.segments] if audio else []
        for index in range(max(len(video_futures), len(audio_futures))):
            for futures, playlist, track_id in ((video_futures, video, 1), (audio_futures, audio, 2)):
                if playlist is None or index >= len(futures):
                    continue
                data = futures[index].result()
                fh.write(rewrite_fragment(data, track_id, sequence))
                sequence += 1
                progress.step(len(data), playlist.segments[index].duration if track_id == 1 else 0.0)
    progress.finish()
    return output


def concatenate_segments(session: Session, playlist: MediaPlaylist, output: Path) -> None:
    progress = Progress(len(playlist.segments))
    with output.open("wb") as fh, concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        for segment, future in zip(playlist.segments, [pool.submit(fetch_bytes, session, s.url) for s in playlist.segments]):
            data = future.result()
            fh.write(data)
            progress.step(len(data), segment.duration)
    progress.finish()


def record_live(
    session: Session,
    playlist_url: str,
    output: Path,
    duration: int | None,
    from_start: bool,
) -> Path:
    """Pure-Python live recording: follows the playlist and appends new segments until Ctrl+C,
    --duration, or #EXT-X-ENDLIST. MPEG-TS goes to .ts; single-track fMP4 goes to .mp4."""
    playlist = fetch_media_playlist(session, playlist_url)
    output = output.with_suffix(".mp4" if playlist.fragmented_mp4 else ".ts")
    print(f"Live: recording to {output} until Ctrl+C" + (f" or for {duration}s" if duration else ""), file=sys.stderr)
    progress = Progress(None)
    last_sequence: int | None = None
    with output.open("wb") as fh:
        if playlist.init_url:
            fh.write(fetch_bytes(session, playlist.init_url))
        try:
            while True:
                if last_sequence is None:
                    pending = playlist.segments if from_start else playlist.segments[-3:]
                else:
                    pending = [s for s in playlist.segments if s.sequence > last_sequence]
                for segment in pending:
                    data = fetch_bytes(session, segment.url)
                    fh.write(data)
                    last_sequence = segment.sequence
                    progress.step(len(data), segment.duration)
                    if duration and progress.seconds >= duration:
                        progress.finish()
                        return output
                if playlist.ended:
                    break
                time.sleep(max(1.0, playlist.target_duration / 2))
                playlist = fetch_media_playlist(session, playlist_url)
        except KeyboardInterrupt:
            print("\nInterrupted, file is complete up to the last written segment.", file=sys.stderr)
    progress.finish()
    return output


class HlsRelay(http.server.ThreadingHTTPServer):
    """Local HTTP server that fetches HLS playlists and segments (through SOCKS5 if configured)
    and rewrites every URI to point back at itself, so any player or ffmpeg can consume the stream."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, proxy: SocksProxy | None, root_url: str, best_master: str) -> None:
        super().__init__(("127.0.0.1", 0), HlsRelayHandler)
        self.proxy = proxy
        self.root_url = root_url
        self.best_master = best_master

    @property
    def base_url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def playlist_url(self) -> str:
        return f"{self.base_url}/playlist.m3u8"

    def local_url(self, upstream_url: str) -> str:
        """/p/<base64url(upstream)><original extension>, so players recognise the type from the path."""
        token = base64.urlsafe_b64encode(upstream_url.encode("utf-8")).decode("ascii").rstrip("=")
        suffix = Path(urlsplit(upstream_url).path).suffix
        return f"{self.base_url}/p/{token}{suffix}"

    def upstream_url(self, path: str) -> str | None:
        if path.startswith("/p/"):
            token = path[3:].split(".", 1)[0]
            return base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode("utf-8")
        return None

    def rewrite_playlist(self, playlist_url: str, text: str) -> str:
        def rewrite_uri_attr(match: re.Match[str]) -> str:
            return f'URI="{self.local_url(urljoin(playlist_url, match.group(1)))}"'

        lines = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                lines.append(line)
            elif stripped.startswith("#"):
                lines.append(re.sub(r'URI="([^"]+)"', rewrite_uri_attr, line))
            else:
                lines.append(self.local_url(urljoin(playlist_url, stripped)))
        return "\n".join(lines) + "\n"

    def start(self) -> None:
        threading.Thread(target=self.serve_forever, name="hls-relay", daemon=True).start()

    def handle_error(self, request: object, client_address: object) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError, TimeoutError)):
            return
        super().handle_error(request, client_address)


class HlsRelayHandler(http.server.BaseHTTPRequestHandler):
    server: HlsRelay
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path in ("", "/"):
            self.send_response(302)
            self.send_header("Location", "/playlist.m3u8")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/playlist.m3u8":
            body = self.server.rewrite_playlist(self.server.root_url, self.server.best_master).encode("utf-8")
            self.respond(200, "application/vnd.apple.mpegurl", body, {})
            return
        upstream = self.server.upstream_url(path)
        if upstream is None:
            self.send_error(404)
            return
        is_playlist = urlsplit(upstream).path.endswith(".m3u8")
        headers = dict(STREAM_HEADERS)
        if "Range" in self.headers and not is_playlist:
            headers["Range"] = self.headers["Range"]
        try:
            resp = Session(self.server.proxy, timeout=60.0).request("GET", upstream, headers=headers)
        except (OSError, ConnectionError) as exc:
            self.send_error(502, f"Upstream error: {exc}")
            return
        content_type = resp.header("Content-Type") or "application/octet-stream"
        body = resp.body
        if resp.status == 200 and (is_playlist or "mpegurl" in content_type.lower()):
            body = self.server.rewrite_playlist(upstream, resp.text()).encode("utf-8")
            content_type = "application/vnd.apple.mpegurl"
        passthrough = {k: v for k in ("Content-Range", "Accept-Ranges") if (v := resp.header(k))}
        self.respond(resp.status, content_type, body, passthrough)

    def respond(self, status: int, content_type: str, body: bytes, extra_headers: Mapping[str, str]) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in extra_headers.items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return


def run_ffmpeg(ffmpeg: str, input_url: str, output: Path, live: bool, duration: int | None) -> int:
    """Downloads HLS and remuxes to MP4 without re-encoding. On Ctrl+C ffmpeg finalizes the file; we wait for it."""
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "warning", "-stats", "-y", "-i", input_url]
    if duration:
        cmd += ["-t", str(duration)]
    cmd += ["-c", "copy", "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", str(output)]
    if live:
        print("Live: recording until Ctrl+C" + (f" or for {duration}s" if duration else ""), file=sys.stderr)
    print("Running:", " ".join(cmd), file=sys.stderr)
    proc = subprocess.Popen(cmd)
    try:
        return proc.wait()
    except KeyboardInterrupt:
        print("\nInterrupted, waiting for ffmpeg to finalize the file…", file=sys.stderr)
        try:
            return proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            return proc.wait()


def print_player_banner(relay: HlsRelay, proxy: SocksProxy | None) -> None:
    banner = "=" * 72
    why = "The original URL will not play in VLC (token bound to the tunnel IP). " if proxy else ""
    print(
        f"\n{banner}\nHLS RELAY IS RUNNING while this script runs. {why}Open EXACTLY this in VLC:\n\n"
        f"    {relay.playlist_url}\n\nor from a terminal:\n\n    {VLC_BIN} '{relay.playlist_url}'\n{banner}\n",
        flush=True,
    )


def wait_forever() -> int:
    print("Not downloading (--url-only). Ctrl+C to stop.", file=sys.stderr)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print("Bye.", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Download ruutu.fi videos and live streams to MP4, optionally via SOCKS5.")
    parser.add_argument("url", type=parse_media_id, help="e.g. https://www.ruutu.fi/video/100128341 or /stream/<id>")
    parser.add_argument("-u", "--user", default=os.environ.get("RUUTU_USER"), help="account login (or RUUTU_USER)")
    parser.add_argument("-p", "--password", default=os.environ.get("RUUTU_PASS"), help="account password (or RUUTU_PASS)")
    parser.add_argument("-o", "--output", type=Path, help="output .mp4 file (default: ruutu-<id>.mp4)")
    parser.add_argument(
        "--proxy",
        type=parse_proxy,
        default=os.environ.get("RUUTU_PROXY", ""),
        help="SOCKS5 proxy: socks5://host:port or host:port (or RUUTU_PROXY; default: direct connection)",
    )
    parser.add_argument("--duration", type=int, help="stop after N seconds (mainly for live streams)")
    parser.add_argument(
        "--url-only",
        action="store_true",
        help="do not download: print the manifest URL and keep the local HLS relay running for a player (VLC etc.)",
    )
    parser.add_argument("--relogin", action="store_true", help="ignore cached tokens and log in again")
    parser.add_argument("--ffmpeg", default=shutil.which("ffmpeg"), help="path to ffmpeg (default: from PATH)")
    parser.add_argument(
        "--no-ffmpeg",
        action="store_true",
        help="pure-Python download even if ffmpeg exists: VOD -> MP4 (fMP4 merge), live -> .ts",
    )
    parser.add_argument("--from-start", action="store_true", help="live: start from the beginning of the playlist instead of the live edge")
    parser.add_argument("--debug", action="store_true", help="print status and key headers of every HTTP response")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    proxy: SocksProxy | None = parse_proxy(args.proxy) if isinstance(args.proxy, str) else args.proxy
    print(f"Proxy: {proxy.url if proxy else 'none (direct connection)'}", file=sys.stderr)
    media_id: str = args.url
    output: Path = args.output or Path.cwd() / f"ruutu-{media_id}.mp4"
    ffmpeg: str | None = None if args.no_ffmpeg else args.ffmpeg
    if ffmpeg is None and not args.url_only:
        print("ffmpeg not used: pure-Python download (VOD -> MP4, MPEG-TS live -> .ts).", file=sys.stderr)

    tokens = obtain_tokens(Session(proxy, debug=args.debug), args.user, args.password, args.relogin)
    media = fetch_media(Session(proxy, debug=args.debug), media_id, tokens)
    playback = media["clip"]["playback"]
    media_type = str(playback.get("mediaType") or "")
    live = media_type.endswith("_live")
    print(f"Media {media_id}: {media_type}, {playback.get('runtime')}s, DRM: no, live: {'yes' if live else 'no'}", file=sys.stderr)

    master_url = pick_hls_url(media)
    print(f"Original HLS manifest (token bound to {'the tunnel' if proxy else 'your'} IP):", file=sys.stderr)
    print(master_url, flush=True)
    master = Session(proxy).request("GET", master_url, headers=STREAM_HEADERS)
    if master.status != 200:
        raise RuntimeError(f"HLS manifest returned HTTP {master.status}: {master.body[:200]!r}")
    streams = select_streams(master_url, master.text())
    relay = HlsRelay(proxy, master_url, streams.filtered_master)
    relay.start()
    print_player_banner(relay, proxy)
    try:
        if args.url_only:
            return wait_forever()
        if ffmpeg:
            return run_ffmpeg(ffmpeg, relay.playlist_url, output, live, args.duration)
        session = Session(proxy, timeout=60.0)
        if live:
            if streams.audio_url:
                raise RuntimeError("Live stream with separate audio rendition needs ffmpeg.")
            written = record_live(session, streams.video_url, output, args.duration, args.from_start)
        else:
            video = fetch_media_playlist(session, streams.video_url)
            audio = fetch_media_playlist(session, streams.audio_url) if streams.audio_url else None
            if args.duration:
                video, audio = truncate_playlists(video, audio, args.duration)
            written = download_vod(session, video, audio, output)
        print(f"Saved {written} ({written.stat().st_size / (1 << 20):.1f} MiB)", file=sys.stderr)
        return 0
    finally:
        relay.shutdown()


def truncate_playlists(video: MediaPlaylist, audio: MediaPlaylist | None, seconds: int) -> tuple[MediaPlaylist, MediaPlaylist | None]:
    def cut(playlist: MediaPlaylist) -> MediaPlaylist:
        kept: list[Segment] = []
        total = 0.0
        for segment in playlist.segments:
            if total >= seconds:
                break
            kept.append(segment)
            total += segment.duration
        return MediaPlaylist(playlist.init_url, kept, playlist.target_duration, playlist.ended)

    return cut(video), (cut(audio) if audio else None)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, ConnectionError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
