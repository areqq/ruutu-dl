# ruutu-dl

Download videos and record live streams from [ruutu.fi](https://www.ruutu.fi) (Sanoma / Nelonen Media) to MP4.

Pure Python standard library. The only external dependency is [ffmpeg](https://ffmpeg.org/). No `yt-dlp`, no
`requests`, no `PySocks`. Optional SOCKS5 tunnel support (for example `ssh -D`) is implemented from scratch,
including a local HLS relay so ffmpeg and players like VLC can read the stream through the tunnel without any
proxy configuration.

## Features

- Logs in through Sanoma SSO with your Ruutu account, caches the tokens and renews them without asking for the password again.
- Resolves `/video/<id>`, `/stream/<id>` (live events) or a bare media ID.
- Picks the highest-bandwidth HLS variant together with its audio rendition and remuxes to MP4 without re-encoding.
- Live streams: records until `Ctrl+C` or for `--duration N` seconds. ffmpeg finalizes the file cleanly on interrupt.
- `--proxy socks5://host:port` routes everything through a SOCKS5 tunnel. Stream tokens are bound to the IP that
  requested them, so API calls and downloads must leave from the same address; the script takes care of that.
- `--url-only` prints the manifest URL and keeps the local HLS relay running (`http://127.0.0.1:<port>/playlist.m3u8`)
  so you can watch in VLC, mpv, QuickTime or a browser. The relay also runs while recording, so you can watch and
  record at the same time.
- Refuses DRM-protected media instead of producing a broken file.

## Requirements

- Python 3.11+
- ffmpeg (`brew install ffmpeg`, `apt install ffmpeg`, …)
- For `rec.sh` only: [streamlink](https://streamlink.github.io/)

## Usage

```bash
# first run: log in (credentials can also come from RUUTU_USER / RUUTU_PASS)
python3 ruutu_dl.py -u you@example.com -p 'secret' https://www.ruutu.fi/video/100128341

# later runs reuse the cached tokens
python3 ruutu_dl.py https://www.ruutu.fi/video/100128341 -o episode.mp4

# live event through an ssh -D tunnel, stop after two hours
python3 ruutu_dl.py --proxy socks5://127.0.0.1:7654 --duration 7200 https://www.ruutu.fi/stream/100329586

# just watch in VLC through the tunnel (keeps the local relay running until Ctrl+C)
python3 ruutu_dl.py --proxy socks5://127.0.0.1:7654 --url-only https://www.ruutu.fi/stream/100329586
```

All options:

| Option | Description |
| --- | --- |
| `-u`, `-p` | Ruutu / Sanoma account credentials (or `RUUTU_USER`, `RUUTU_PASS`). Needed only until tokens are cached. |
| `-o FILE` | Output file (default `ruutu-<id>.mp4` in the current directory). |
| `--proxy` | SOCKS5 proxy: `socks5://host:port` or `host:port` (or `RUUTU_PROXY`). Default: direct connection. |
| `--duration N` | Stop after N seconds (mainly for live streams). |
| `--url-only` | Print the manifest URL and serve the local relay; do not download. |
| `--relogin` | Ignore cached tokens and log in again. |
| `--ffmpeg PATH` | ffmpeg binary (default: found on `PATH`). |
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
- HLS URLs carry a signed token bound to the client IP and valid for 24 hours.
- The script fetches the master playlist, keeps only the best variant plus its `#EXT-X-MEDIA` audio rendition
  (VOD ships audio separately) and serves that through a local HTTP relay that rewrites every URI to itself and
  fetches segments through the tunnel. ffmpeg reads from the relay and writes MP4 with `-c copy`.
- VLC on macOS cannot be pointed at a SOCKS proxy from the command line, which is why the relay exists at all.

## Notes

Intended for personal use with your own subscription. Respect Ruutu's terms of service and the rights holders.
