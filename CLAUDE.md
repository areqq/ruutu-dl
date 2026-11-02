# ruutu-dl – ruutu.fi downloader

Tools for downloading ruutu.fi (Sanoma / Nelonen Media) videos and live streams to MP4, optionally through a SOCKS5 tunnel.

## Files

- `ruutu_dl.py` – main script. Pure stdlib + external `ffmpeg`. Sanoma SSO login, token cache and renewal, media API,
  HLS master filtering, local HLS relay, ffmpeg remux. Own SOCKS5 client on top of `http.client` (urllib has no SOCKS).
- `rec.sh` – alternative live recorder: streamlink + ffmpeg to fragmented MP4. Args: `M3U8_URL [NAME]`.
- `*.mp4`, `*.ts` – recordings, never commit.

## Usage

```bash
python3 ruutu_dl.py [--proxy socks5://127.0.0.1:7654] [-u LOGIN -p PASSWORD] https://www.ruutu.fi/video/<ID>
```

- Default: direct connection. Proxy is SOCKS5 only: `socks5://host:port`, `host:port`, or `RUUTU_PROXY`.
- Credentials: `-u/-p` or `RUUTU_USER`/`RUUTU_PASS`, needed only for the first run.
- Token cache: `~/.ruutu-dl/auth.json` (chmod 600). Access token 24 h, refresh token 1 year. Expired access token is
  renewed via `POST https://www.ruutu.fi/api/renew?token=auth` with the `authAccessToken`/`authRefreshToken` cookies.
  `--relogin` ignores the cache.
- URL may be `/video/<id>`, `/stream/<id>` (live events) or a bare `<id>`.
- `--url-only` prints the manifest URL and keeps the local HLS relay running for a player. The relay also runs during
  downloads (watch while recording).
- `--duration N` stops after N seconds (ffmpeg `-t`). Live: `mediaType` ends with `_live`.
- All user-facing messages are in English.

Tooling: `uv tool install streamlink mitmproxy pproxy` (binaries in `~/.local/bin`), ffmpeg from Homebrew.

## How ruutu.fi works (reverse engineered from captured traffic, November 2026)

### Login (Sanoma SSO)
1. `GET https://auth-api.nm-ovp.nelonenmedia.fi/v2/sso/init/login?client=ruutu-web&widget=true&redirect_uri=https://www.ruutu.fi/sacu/authorized&ref_url=https://www.ruutu.fi/&region=fi` → 302 to `https://tili.sanoma.fi/kirjaudu?...client_id=...&state=...`.
2. `GET` of the `kirjaudu` page sets cookie `_sso_csrf=<token>|<sig>`. The part before `|` goes into the `X-CSRF-Token` header.
3. `POST https://tili.sanoma.fi/trpc/login.login` (JSON: `client_id, redirect_uri, state, scope:null, code_challenge:null, code_challenge_method:null, queryString, service:"nelonen", username, password, failedAttempts:0`) → `result.data.redirectUri` with `code` and `state`.
4. `GET <redirectUri>&style=ruutu3&remember_me=true` on www.ruutu.fi → 307 with `Set-Cookie: authAccessToken` (JWT ES384, 24 h) and `authRefreshToken` (1 year). The access token is the Bearer for the APIs.

Pitfalls: cookies must be scoped per host (sending tili.sanoma.fi cookies to ruutu.fi yields 403). Step 4 is a page
navigation: `Accept: text/html` plus navigate-type `Sec-Fetch-*` headers. tili.sanoma.fi runs DataDome; login without
its cookie worked, but may start returning 403.

### Media API
- `GET https://mcc.nm-ovp.nelonenmedia.fi/v2/media/<ID>` with `Authorization: Bearer <authAccessToken>` and ruutu.fi `Origin`/`Referer`.
- Response: `clip.playback.{mediaType, runtime, drm{enabled,type,assetId}, geoblock{enabled,url}, media.streamUrls{webHls, apple, android, cast, dash, http, samsung, audioHls}}`. Some entries can be `null`.
- Free content (live channel `ruutu-free-01`, free episodes) and a paid live football match (`jalkapallo-01`) had `drm.enabled=false`. Premium content may have DRM; the script aborts then.
- `/stream/<id>` returns `type: "video-stream"` (event: `name`, `start/end`, `status`, `statusMessage`, `onlineRights`) without `clip`. The real media is `videoId`; the script follows it with a second call.
- HLS/DASH URLs embed an HS256 JWT in the path with `path`, `ip` (client IP that called the media API), `nbf`/`exp` (24 h). Downloads must come from the same IP as the media API call, hence one proxy setting for the whole script.
- `streamUrls.http` (progressive MP4, 3000 kbps) returns 403 both directly and through the tunnel, so it is not used.
- VOD master playlists (Unified Streaming) keep audio in a separate `#EXT-X-MEDIA:TYPE=AUDIO` rendition referenced by `AUDIO="..."` on the variant; live masters (AWS Elemental) have muxed audio. The script builds a filtered master with the best variant plus its audio group and drops subtitles.

### Geoblock
- `GET https://geoblock.nm-ovp.nelonenmedia.fi/geoblock` returns XML `<geoblock status="true">...</geoblock>` for hosting IP ranges (e.g. Hetzner) and an empty result for residential IPs. Enforced only in the browser player; the media API and CDN do not check it.
- The endpoint's CORS allows an `x-gatling-token` header (probably a bypass for Sanoma's own load tests).

## HLS relay (`HlsRelay`)
- Local `http.server.ThreadingHTTPServer` on 127.0.0.1 with a random port. `/playlist.m3u8` serves the filtered master; `/p/<base64url(upstream)><ext>` proxies any upstream URL, rewriting `.m3u8` bodies (segment lines and `URI="..."` attributes) to local URLs. `/` redirects to the playlist.
- Why: VLC 3 ignores `--socks` for HTTPS, `--http-proxy` no longer exists, and on macOS VLC takes proxies only from system settings. ffmpeg cannot speak SOCKS either. The relay solves both with one mechanism.
- Must forward `Content-Range`/`Accept-Ranges` (VLC's first request carries `Range`), keep file extensions in local URLs (demuxer detection), and ignore `ConnectionResetError`/`BrokenPipeError` in `handle_error` (ffmpeg closes connections early when `-t` is reached).

## Live recording notes
- yt-dlp was dropped: for live HLS it delegates to ffmpeg anyway, and its native downloader cannot do live. ffmpeg reading from the relay covers VOD and live alike.
- `rec.sh` alternative: streamlink supports `--http-proxy socks5h://...` natively; piping to `ffmpeg -c copy -movflags +frag_keyframe+empty_moov` gives an MP4 that survives Ctrl+C.

## Traffic analysis
- Chrome with a separate profile: `open -na "Google Chrome" --args --proxy-server="http://127.0.0.1:8080" --ignore-certificate-errors-spki-list=<mitmproxy CA SPKI> --user-data-dir=~/.chrome-socks-7654`.
- Chain: Chrome → mitmdump :8080 (`--mode upstream:http://127.0.0.1:8081 -w flows.mitm`) → pproxy :8081 → ssh -D :7654. mitmproxy has no upstream SOCKS mode, hence pproxy.
- SPKI for Chrome: `openssl x509 -in ~/.mitmproxy/mitmproxy-ca-cert.pem -pubkey -noout | openssl pkey -pubin -outform der | openssl dgst -sha256 -binary | base64`.
- Read flows with the mitmproxy venv Python: `~/.local/share/uv/tools/mitmproxy/bin/python` + `mitmproxy.io.FlowReader`.

## Rules
- Python: stdlib only in `ruutu_dl.py`, type hints, dataclasses, no imports inside functions, messages in English.
- Never commit passwords, tokens, tokenized stream URLs (they embed the tunnel IP) or account e-mail addresses.
