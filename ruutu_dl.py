#!/usr/bin/env python3
"""Pobiera wideo z ruutu.fi do MP4 przez tunel SOCKS5.

Jedyna zależność zewnętrzna: yt-dlp (plus ffmpeg, jeśli chcesz HLS w najwyższej jakości).
Domyślnie łączy się bezpośrednio. Z opcją --proxy cały ruch (logowanie, API, pobieranie)
idzie przez SOCKS5, bo token strumienia jest przypięty do IP, z którego wywołano media API.

Użycie:
  ruutu_dl.py https://www.ruutu.fi/video/100128341
  ruutu_dl.py --proxy socks5://127.0.0.1:7654 -u user@example.com -p 'haslo' https://www.ruutu.fi/video/100128341
Dane logowania można też podać przez RUUTU_USER / RUUTU_PASS. Tokeny są cache'owane
w ~/.ruutu-dl/auth.json i używane ponownie do wygaśnięcia (~24 h).
"""

from __future__ import annotations

import argparse
import base64
import http.client
import http.server
import json
import os
import re
import selectors
import shutil
import socket
import socketserver
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


@dataclass(frozen=True)
class SocksProxy:
    host: str
    port: int

    def ytdlp_url(self) -> str:
        return f"socks5://{self.host}:{self.port}"


def parse_proxy(value: str) -> SocksProxy | None:
    """Przyjmuje 'socks5://host:port', 'socks5h://host:port', 'host:port' albo 'none'."""
    text = value.strip()
    if text.lower() in ("", "none", "off", "direct"):
        return None
    if "://" in text:
        parts = urlsplit(text)
        if parts.scheme not in ("socks5", "socks5h", "socks"):
            raise argparse.ArgumentTypeError(f"Obsługiwany jest tylko SOCKS5, nie: {parts.scheme}")
        if not parts.hostname or not parts.port:
            raise argparse.ArgumentTypeError(f"Proxy wymaga hosta i portu: {value}")
        return SocksProxy(parts.hostname, parts.port)
    host, sep, port = text.rpartition(":")
    if not sep or not port.isdigit():
        raise argparse.ArgumentTypeError(f"Proxy w formacie host:port lub socks5://host:port: {value}")
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


@dataclass
class AuthTokens:
    access_token: str
    refresh_token: str

    def is_valid(self) -> bool:
        return jwt_expiry(self.access_token) - TOKEN_SAFETY_MARGIN_S > time.time()


@dataclass
class Session:
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
            raise ValueError(f"Obsługiwane jest tylko https: {url}")
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
        raise ConnectionError("SOCKS5: proxy odrzucił handshake bez uwierzytelnienia")
    host_bytes = host.encode("idna")
    sock.sendall(b"\x05\x01\x00\x03" + bytes([len(host_bytes)]) + host_bytes + struct.pack(">H", port))
    version, reply, _, address_type = recv_exact(sock, 4)
    if version != 5 or reply != 0:
        sock.close()
        raise ConnectionError(f"SOCKS5: CONNECT do {host}:{port} odrzucony (kod {reply})")
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
            raise ConnectionError("SOCKS5: połączenie zamknięte w trakcie handshake")
        data += chunk
    return data


class ConnectBridge(socketserver.ThreadingTCPServer):
    """Lokalny proxy HTTP CONNECT, który tuneluje połączenia przez SOCKS5.

    ffmpeg (używany przez yt-dlp do live HLS) obsługuje tylko proxy HTTP, a tunel jest SOCKS5.
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, proxy: SocksProxy) -> None:
        super().__init__(("127.0.0.1", 0), ConnectBridgeHandler)
        self.proxy = proxy

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> None:
        threading.Thread(target=self.serve_forever, name="connect-bridge", daemon=True).start()


class ConnectBridgeHandler(socketserver.BaseRequestHandler):
    server: ConnectBridge

    def handle(self) -> None:
        header = b""
        while b"\r\n\r\n" not in header and len(header) < 65536:
            chunk = self.request.recv(4096)
            if not chunk:
                return
            header += chunk
        request_line = header.split(b"\r\n", 1)[0].decode("latin-1")
        parts = request_line.split()
        if len(parts) < 2 or parts[0] != "CONNECT":
            self.request.sendall(b"HTTP/1.1 405 Method Not Allowed\r\nConnection: close\r\n\r\n")
            return
        host, _, port = parts[1].rpartition(":")
        try:
            upstream = socks5_connect(self.server.proxy, host, int(port or 443), 30.0)
        except (OSError, ValueError, ConnectionError):
            self.request.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
            return
        self.request.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        try:
            pump_bidirectional(self.request, upstream)
        finally:
            upstream.close()


def pump_bidirectional(a: socket.socket, b: socket.socket, idle_timeout: float = 120.0) -> None:
    """Przepycha dane w obie strony. Gniazda zostają blokujące: select mówi tylko, skąd czytać,
    a sendall czeka, aż odbiorca odbierze (ffmpeg czyta wolniej, niż tunel dostarcza)."""
    a.settimeout(idle_timeout)
    b.settimeout(idle_timeout)
    peer = {a: b, b: a}
    with selectors.DefaultSelector() as selector:
        selector.register(a, selectors.EVENT_READ)
        selector.register(b, selectors.EVENT_READ)
        while True:
            ready = selector.select(timeout=idle_timeout)
            if not ready:
                return
            for key, _ in ready:
                sock: socket.socket = key.fileobj  # type: ignore[assignment]
                try:
                    data = sock.recv(65536)
                    if not data:
                        return
                    peer[sock].sendall(data)
                except OSError:
                    return


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


def renew_tokens(session: Session, tokens: AuthTokens) -> AuthTokens | None:
    """Odświeża wygasły token tak jak frontend: POST /api/renew z ciasteczkami auth*Token."""
    session.cookies_for("www.ruutu.fi").update(
        {"authAccessToken": tokens.access_token, "authRefreshToken": tokens.refresh_token}
    )
    resp = session.request(
        "POST",
        RENEW_URL,
        headers={"Origin": RUUTU_ORIGIN, "Referer": RUUTU_ORIGIN + "/", "Content-Length": "0"},
        body=b"",
    )
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
        print("Token wygasł, odświeżam przez /api/renew…", file=sys.stderr)
        renewed = renew_tokens(session, cached)
        if renewed:
            save_tokens(renewed)
            print("Token odświeżony.", file=sys.stderr)
            return renewed
        print("Odświeżenie nieudane.", file=sys.stderr)
    if user and password:
        print("Loguję do Sanoma SSO…", file=sys.stderr)
        tokens = login(session, user, password)
        save_tokens(tokens)
        print("Zalogowano, tokeny zapisane w", AUTH_CACHE, file=sys.stderr)
        return tokens
    print("Brak danych logowania i ważnego tokenu, próbuję bez logowania.", file=sys.stderr)
    return None


def save_tokens(tokens: AuthTokens) -> None:
    AUTH_CACHE.parent.mkdir(parents=True, exist_ok=True)
    AUTH_CACHE.write_text(json.dumps(tokens.__dict__, indent=2))
    AUTH_CACHE.chmod(0o600)


def login(session: Session, username: str, password: str) -> AuthTokens:
    init = session.request("GET", SSO_INIT_URL)
    login_page_url = init.header("Location")
    if init.status != 302 or not login_page_url:
        raise RuntimeError(f"SSO init: oczekiwano 302, dostałem {init.status}")
    query = urlsplit(login_page_url).query
    params = {k: v[0] for k, v in parse_qs(query).items()}

    page = session.request("GET", login_page_url, headers=navigation_headers(RUUTU_ORIGIN + "/"))
    if page.status != 200:
        raise RuntimeError(f"Strona logowania zwróciła {page.status}: {page.body[:300]!r}")
    csrf_cookie = session.cookies_for("tili.sanoma.fi").get("_sso_csrf", "")
    csrf_token = csrf_cookie.replace("%7C", "|").split("|")[0]
    if not csrf_token:
        raise RuntimeError("Brak ciasteczka _sso_csrf po wejściu na stronę logowania")

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
        raise RuntimeError(f"Logowanie odrzucone (HTTP {resp.status}): {resp.body[:300]!r}")
    data = resp.json().get("result", {}).get("data", {})
    redirect_uri = data.get("redirectUri")
    if not redirect_uri:
        raise RuntimeError(f"Logowanie nieudane: {data.get('message') or resp.body[:300]!r}")

    authorized = session.request(
        "GET",
        redirect_uri + "&style=ruutu3&remember_me=true",
        headers=navigation_headers("https://tili.sanoma.fi/"),
    )
    cookies = authorized.set_cookies()
    if "authAccessToken" not in cookies:
        raise RuntimeError(
            f"Brak authAccessToken po autoryzacji (HTTP {authorized.status}): {authorized.body[:300]!r}"
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
    headers = {"Origin": RUUTU_ORIGIN, "Referer": RUUTU_ORIGIN + "/"}
    if tokens:
        headers["Authorization"] = f"Bearer {tokens.access_token}"
    resp = session.request("GET", MEDIA_URL.format(media_id=media_id), headers=headers)
    if resp.status != 200:
        raise RuntimeError(f"Media API zwróciło HTTP {resp.status}: {resp.body[:300]!r}")
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"Media API: success=false: {json.dumps(data)[:300]}")
    if data.get("type") == "video-stream":
        video_id = data.get("videoId")
        if not video_id:
            raise RuntimeError(f"Wydarzenie '{data.get('name')}' nie ma jeszcze medium (status: {data.get('statusMessage')})")
        print(f"Wydarzenie live '{data.get('name')}' ({data.get('statusMessage')}), medium {video_id}", file=sys.stderr)
        return fetch_media(session, str(video_id), tokens)
    if "clip" not in data:
        raise RuntimeError(f"Nieznany typ odpowiedzi media API: type={data.get('type')}, klucze={sorted(data)}")
    return data


def pick_stream_url(media: dict, prefer_hls: bool) -> str:
    playback = media["clip"]["playback"]
    if playback.get("drm", {}).get("enabled"):
        raise RuntimeError("Materiał ma DRM, nie da się go zapisać.")
    urls = playback["media"]["streamUrls"]
    order = ["webHls", "apple", "http"] if prefer_hls else ["http", "webHls", "apple"]
    for key in order:
        url = (urls.get(key) or {}).get("url")
        if url:
            return url
    available = sorted(k for k, v in urls.items() if v)
    raise RuntimeError(f"Brak znanego strumienia, dostępne: {available}")


def parse_media_id(url: str) -> str:
    """Akceptuje https://www.ruutu.fi/video/<id>, /stream/<id>, inne /<sekcja>/<id> albo samo <id>."""
    text = url.strip()
    if text.isdigit():
        return text
    match = re.search(r"ruutu\.fi/(?:[a-z-]+/)*?(\d+)(?:[/?#]|$)", text)
    if not match:
        raise argparse.ArgumentTypeError(f"Nie umiem wyciągnąć ID wideo z: {url}")
    return match.group(1)


def run_ytdlp(
    stream_url: str,
    output: Path,
    proxy: SocksProxy | None,
    ytdlp: str,
    live: bool,
    duration: int | None,
) -> int:
    cmd = [
        ytdlp,
        *(["--proxy", proxy.ytdlp_url()] if proxy else []),
        "--referer", RUUTU_ORIGIN + "/",
        "--add-header", f"Origin:{RUUTU_ORIGIN}",
        "--user-agent", USER_AGENT,
        "-f", "bv*+ba/b",
        "--merge-output-format", "mp4",
        "--remux-video", "mp4",
        "--no-part",
        "-o", str(output),
    ]
    if live or duration:
        cmd += ["--downloader", "ffmpeg"]
    if live and proxy:
        bridge = ConnectBridge(proxy)
        bridge.start()
        print(f"Live przez SOCKS: mostek HTTP CONNECT dla ffmpeg na {bridge.url}", file=sys.stderr)
        cmd += ["--downloader-args", f"ffmpeg_i:-http_proxy {bridge.url}"]
    if duration:
        cmd += ["--downloader-args", f"ffmpeg_o:-t {duration}"]
    if live:
        print("Live: nagrywam do przerwania Ctrl+C" + (f" lub przez {duration}s" if duration else ""), file=sys.stderr)
    cmd.append(stream_url)
    print("Uruchamiam:", " ".join(cmd[:-1]), "<URL>", file=sys.stderr)
    return subprocess.call(cmd)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Pobiera wideo z ruutu.fi do MP4 przez tunel SOCKS5.")
    parser.add_argument("url", type=parse_media_id, help="np. https://www.ruutu.fi/video/100128341")
    parser.add_argument("-u", "--user", default=os.environ.get("RUUTU_USER"), help="login (lub RUUTU_USER)")
    parser.add_argument("-p", "--password", default=os.environ.get("RUUTU_PASS"), help="hasło (lub RUUTU_PASS)")
    parser.add_argument("-o", "--output", type=Path, help="plik wyjściowy .mp4 (domyślnie ruutu-<id>.mp4)")
    parser.add_argument(
        "--proxy",
        type=parse_proxy,
        default=os.environ.get("RUUTU_PROXY", ""),
        help="SOCKS5: socks5://host:port lub host:port (lub RUUTU_PROXY; domyślnie bez proxy)",
    )
    parser.add_argument("--progressive", action="store_true", help="wymuś zwykły MP4 (3000 kbps) zamiast HLS")
    parser.add_argument("--duration", type=int, help="zatrzymaj po N sekundach (głównie dla live)")
    parser.add_argument(
        "--url-only",
        action="store_true",
        help="nie pobieraj: wypisz URL manifestu; z --proxy trzymaj też lokalny proxy HTTP dla odtwarzacza (VLC --http-proxy)",
    )
    parser.add_argument("--relogin", action="store_true", help="zignoruj cache tokenów i zaloguj ponownie")
    parser.add_argument("--ytdlp", default=shutil.which("yt-dlp") or "yt-dlp", help="ścieżka do yt-dlp")
    parser.add_argument("--debug", action="store_true", help="wypisz status i nagłówki każdej odpowiedzi HTTP")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    proxy: SocksProxy | None = parse_proxy(args.proxy) if isinstance(args.proxy, str) else args.proxy
    print(f"Proxy: {proxy.ytdlp_url() if proxy else 'brak (połączenie bezpośrednie)'}", file=sys.stderr)
    media_id: str = args.url
    output: Path = args.output or Path.cwd() / f"ruutu-{media_id}.mp4"

    tokens = obtain_tokens(Session(proxy, debug=args.debug), args.user, args.password, args.relogin)
    media = fetch_media(Session(proxy, debug=args.debug), media_id, tokens)
    prefer_hls = not args.progressive and shutil.which("ffmpeg") is not None
    if not prefer_hls and not args.progressive:
        print("Brak ffmpeg: biorę progresywny MP4 (3000 kbps) zamiast HLS.", file=sys.stderr)
    playback = media["clip"]["playback"]
    media_type = str(playback.get("mediaType") or "")
    live = media_type.endswith("_live")
    stream_url = pick_stream_url(media, prefer_hls or live)
    print(f"Media {media_id}: {media_type}, {playback.get('runtime')}s, DRM: nie, live: {'tak' if live else 'nie'}", file=sys.stderr)
    print(f"Oryginalny manifest HLS (token przypięty do IP{' tunelu' if proxy else ''}):", file=sys.stderr)
    print(stream_url, flush=True)
    relay = start_player_relay(stream_url, proxy)
    try:
        if args.url_only:
            return wait_forever()
        return run_ytdlp(stream_url, output, proxy, args.ytdlp, live, args.duration)
    finally:
        if relay:
            relay.shutdown()


def start_player_relay(stream_url: str, proxy: SocksProxy | None) -> HlsRelay | None:
    """Wypisuje adres do odtwarzacza. Z proxy uruchamia lokalny relay HLS, bo VLC nie umie SOCKS ani proxy z CLI."""
    banner = "=" * 72
    if proxy is None:
        print(f"\n{banner}\nADRES DO VLC (połączenie bezpośrednie):\n\n    {stream_url}\n{banner}\n", flush=True)
        return None
    relay = HlsRelay(proxy, stream_url)
    relay.start()
    print(
        f"\n{banner}\nRELAY HLS PRZEZ SOCKS DZIAŁA, dopóki działa ten skrypt.\n"
        f"Oryginalny URL nie zadziała w VLC (token na IP tunelu). W VLC otwórz DOKŁADNIE:\n\n"
        f"    {relay.playlist_url}\n\nalbo z terminala:\n\n    {VLC_BIN} '{relay.playlist_url}'\n{banner}\n",
        flush=True,
    )
    return relay


def wait_forever() -> int:
    print("Nie pobieram (--url-only). Ctrl+C kończy.", file=sys.stderr)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print("Koniec.", file=sys.stderr)
    return 0


class HlsRelay(http.server.ThreadingHTTPServer):
    """Lokalny serwer HTTP: pobiera playlisty i segmenty HLS przez SOCKS5 i przepisuje adresy na siebie."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, proxy: SocksProxy, root_url: str) -> None:
        super().__init__(("127.0.0.1", 0), HlsRelayHandler)
        self.proxy = proxy
        self.root_url = root_url

    @property
    def base_url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def playlist_url(self) -> str:
        return f"{self.base_url}/playlist.m3u8"

    def local_url(self, upstream_url: str) -> str:
        """/p/<base64url(upstream)><oryginalne rozszerzenie>, żeby odtwarzacze poznawały typ po ścieżce."""
        token = base64.urlsafe_b64encode(upstream_url.encode("utf-8")).decode("ascii").rstrip("=")
        suffix = Path(urlsplit(upstream_url).path).suffix
        return f"{self.base_url}/p/{token}{suffix}"

    def upstream_url(self, path: str) -> str | None:
        if path == "/playlist.m3u8":
            return self.root_url
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
        upstream = self.server.upstream_url(path)
        if upstream is None:
            self.send_error(404)
            return
        is_playlist = urlsplit(upstream).path.endswith(".m3u8")
        headers = {"Origin": RUUTU_ORIGIN, "Referer": RUUTU_ORIGIN + "/"}
        if "Range" in self.headers and not is_playlist:
            headers["Range"] = self.headers["Range"]
        try:
            resp = Session(self.server.proxy, timeout=60.0).request("GET", upstream, headers=headers)
        except (OSError, ConnectionError) as exc:
            self.send_error(502, f"Upstream: {exc}")
            return
        content_type = resp.header("Content-Type") or "application/octet-stream"
        body = resp.body
        if resp.status == 200 and (is_playlist or "mpegurl" in content_type.lower()):
            body = self.server.rewrite_playlist(upstream, body.decode("utf-8", errors="replace")).encode("utf-8")
            content_type = "application/vnd.apple.mpegurl"
        self.send_response(resp.status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for passthrough in ("Content-Range", "Accept-Ranges"):
            value = resp.header(passthrough)
            if value:
                self.send_header(passthrough, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, ConnectionError, OSError) as exc:
        print(f"Błąd: {exc}", file=sys.stderr)
        sys.exit(1)
