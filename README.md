# ruutu-dl

Download videos and record live streams from [ruutu.fi](https://www.ruutu.fi) (Sanoma / Nelonen Media) to MP4.

Pure Python standard library plus [yt-dlp](https://github.com/yt-dlp/yt-dlp). No `requests`, no `PySocks`.
Optional SOCKS5 tunnel support (for example `ssh -D`) implemented from scratch, including a local HLS relay so
players like VLC can watch through the tunnel without any proxy configuration.

## Features

- Logs in through Sanoma SSO with your Ruutu account, caches the tokens and refreshes them without asking for the password again.
- Resolves `/video/<id>`, `/stream/<id>` (live events) or a bare media ID.
- Picks the best HLS rendition and remuxes it to MP4 without re-encoding (needs `ffmpeg`), or falls back to the progressive MP4.
- Live streams: records until `Ctrl+C` or for `--duration N` seconds.
- `--proxy socks5://host:port` routes everything through a SOCKS5 tunnel. Stream tokens are bound to the IP that
  requested them, so API calls and downloads must leave from the same address; the script takes care of that.
- `--url-only` prints the manifest URL and, with a proxy, starts a local HLS relay (`http://127.0.0.1:<port>/playlist.m3u8`)
  that you can open in VLC, mpv, QuickTime or a browser.
- Refuses DRM-protected media instead of producing a broken file.

## Requirements

- Python 3.11+
- [yt-dlp](https://github.com/yt-dlp/yt-dlp)
- [ffmpeg](https://ffmpeg.org/) (recommended; without it only the 3000 kbps progressive MP4 is available)
- For `rec.sh` only: [streamlink](https://streamlink.github.io/)

```bash
uv tool install yt-dlp streamlink   # or pipx / brew
brew install ffmpeg
```

## Usage

```bash
# first run: log in (credentials can also come from RUUTU_USER / RUUTU_PASS)
python3 ruutu_dl.py -u you@example.com -p 'secret' https://www.ruutu.fi/video/100128341

# later runs reuse the cached tokens
python3 ruutu_dl.py https://www.ruutu.fi/video/100128341 -o episode.mp4

# live event through an ssh -D tunnel, stop after two hours
python3 ruutu_dl.py --proxy socks5://127.0.0.1:7654 --duration 7200 https://www.ruutu.fi/stream/100329586

# just watch in VLC through the tunnel (keeps a local relay running until Ctrl+C)
python3 ruutu_dl.py --proxy socks5://127.0.0.1:7654 --url-only https://www.ruutu.fi/stream/100329586
```

All options:

| Option | Description |
| --- | --- |
| `-u`, `-p` | Ruutu / Sanoma account credentials (or `RUUTU_USER`, `RUUTU_PASS`). Needed only until tokens are cached. |
| `-o FILE` | Output file (default `ruutu-<id>.mp4` in the current directory). |
| `--proxy` | SOCKS5 proxy: `socks5://host:port` or `host:port` (or `RUUTU_PROXY`). Default: direct connection. |
| `--duration N` | Stop after N seconds (mainly for live streams). |
| `--progressive` | Force the token-less progressive MP4 (3000 kbps) instead of HLS. |
| `--url-only` | Print the manifest URL and serve the local relay; do not download. |
| `--relogin` | Ignore cached tokens and log in again. |
| `--debug` | Print status and key headers of every HTTP response. |

Tokens are stored in `~/.ruutu-dl/auth.json` (mode 600). The access token lives 24 hours and is renewed with the
refresh token (valid for a year) through the same endpoint the web player uses.

### rec.sh

A small alternative for live channels using streamlink and ffmpeg directly:

```bash
./rec.sh "<hls-index.m3u8 url with token>" my-recording
```

## How it works

- Sanoma SSO: `auth-api.nm-ovp.nelonenmedia.fi` → `tili.sanoma.fi/kirjaudu` (CSRF cookie) → `trpc/login.login` →
  `www.ruutu.fi/sacu/authorized`, which sets the `authAccessToken` / `authRefreshToken` cookies used as a Bearer token.
- Media metadata comes from `mcc.nm-ovp.nelonenmedia.fi/v2/media/<id>`. Live events (`/stream/<id>`) return a
  `video-stream` object whose `videoId` points to the actual media.
- HLS/DASH URLs carry a signed token bound to the client IP and valid for 24 hours. The progressive MP4 has no token.
- yt-dlp handles VOD. For live HLS yt-dlp delegates to ffmpeg, which cannot speak SOCKS, so the script spins up an
  in-process HTTP CONNECT → SOCKS5 bridge and hands it to ffmpeg via `-http_proxy`.
- VLC on macOS cannot be pointed at a SOCKS proxy from the command line, hence the HLS relay that rewrites playlists
  to point at `127.0.0.1`.

## Notes

Intended for personal use with your own subscription. Respect Ruutu's terms of service and the rights holders.
