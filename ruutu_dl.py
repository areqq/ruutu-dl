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


def build_best_master(master_url: str, text: str) -> str:
    """Builds a master playlist that contains only the highest-bandwidth variant and its audio renditions.

    Players and ffmpeg then get exactly one program: best video plus matching audio (VOD keeps audio in a
    separate #EXT-X-MEDIA rendition). Subtitles are dropped. URIs are made absolute so the relay can rewrite them.
    """
    variants = parse_master_playlist(master_url, text)
    if not variants:
        return text
    best = max(variants, key=lambda v: v.bandwidth)
    listing = ", ".join(f"{v.resolution}@{v.bandwidth // 1000}k" for v in sorted(variants, key=lambda v: v.bandwidth))
    print(f"HLS variants: {listing}. Using {best.resolution}@{best.bandwidth // 1000}k.", file=sys.stderr)

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
    lines += [best.stream_inf, best.url]
    return "\n".join(lines) + "\n"


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
    parser.add_argument("--debug", action="store_true", help="print status and key headers of every HTTP response")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    proxy: SocksProxy | None = parse_proxy(args.proxy) if isinstance(args.proxy, str) else args.proxy
    print(f"Proxy: {proxy.url if proxy else 'none (direct connection)'}", file=sys.stderr)
    media_id: str = args.url
    output: Path = args.output or Path.cwd() / f"ruutu-{media_id}.mp4"
    if not args.url_only and args.ffmpeg is None:
        raise RuntimeError("ffmpeg not found. Install it (brew install ffmpeg) or pass --ffmpeg /path/to/ffmpeg.")

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
    relay = HlsRelay(proxy, master_url, build_best_master(master_url, master.text()))
    relay.start()
    print_player_banner(relay, proxy)
    try:
        if args.url_only:
            return wait_forever()
        return run_ffmpeg(args.ffmpeg, relay.playlist_url, output, live, args.duration)
    finally:
        relay.shutdown()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, ConnectionError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
