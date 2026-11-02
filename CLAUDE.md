# ruutu.fi – pobieranie wideo

Narzędzia do pobierania materiałów z ruutu.fi (Sanoma / Nelonen Media) do MP4, z opcjonalnym tunelem SOCKS5.

## Pliki

- `ruutu_dl.py` – główny skrypt. Czysty stdlib + zewnętrzny `yt-dlp` (opcjonalnie `ffmpeg`). Logowanie do Sanoma SSO, cache tokenów, media API, pobieranie przez yt-dlp. Własny klient SOCKS5 nad `http.client` (urllib nie obsługuje SOCKS).
- `rec.sh` – nagrywanie kanału live (HLS) przez streamlink + ffmpeg do fragmentowanego MP4. Argumenty: `[URL_M3U8] [NAZWA]`.
- `*.mp4`, `*.ts` – nagrania, nie commitować.

## Użycie

```bash
python3 ruutu_dl.py [--proxy socks5://127.0.0.1:7654] [-u LOGIN -p HASLO] https://www.ruutu.fi/video/<ID>
```

- Domyślnie bez proxy. Proxy tylko SOCKS5: `socks5://host:port`, `host:port`, albo `RUUTU_PROXY`.
- Dane logowania: `-u/-p` lub `RUUTU_USER`/`RUUTU_PASS`. Potrzebne tylko przy pierwszym uruchomieniu.
- Cache tokenów: `~/.ruutu-dl/auth.json` (chmod 600). Access token ważny 24 h, refresh token rok. Wygasły token odświeżany przez `POST https://www.ruutu.fi/api/renew?token=auth`. `--relogin` ignoruje cache.
- `--debug` wypisuje status i nagłówki każdej odpowiedzi HTTP.
- Z ffmpeg: HLS w najwyższej jakości, remux do MP4. Bez ffmpeg lub z `--progressive`: progresywny MP4 3000 kbps (bez tokenu).
- URL może być `/video/<id>`, `/stream/<id>` (kanały live) albo samo `<id>`.
- Live (`mediaType` kończy się na `_live`): yt-dlp z downloaderem ffmpeg, nagrywa do Ctrl+C lub `--duration N` sekund. Przy `--proxy` skrypt uruchamia w wątku własny mostek HTTP CONNECT → SOCKS5 (`ConnectBridge`) i podaje go ffmpeg przez `-http_proxy`, bo ffmpeg nie obsługuje SOCKS. Mostek musi używać blokujących gniazd (nieblokujący `sendall` rzucał `BlockingIOError`, gdy ffmpeg czytał wolniej niż tunel).
- `--url-only`: wypisuje URL manifestu na stdout i nie pobiera. Z `--proxy` dodatkowo uruchamia lokalny relay HLS (`HlsRelay`, `http://127.0.0.1:<port>/playlist.m3u8`), który pobiera playlisty i segmenty przez SOCKS i przepisuje adresy na siebie. Powód: VLC 3 ignoruje `--socks` dla HTTPS, `--http-proxy` już nie istnieje, a na macOS VLC bierze proxy tylko z ustawień systemowych. Relay przekazuje `Content-Range`/`Accept-Ranges` (VLC pyta pierwszym żądaniem z `Range`), a lokalne adresy zachowują rozszerzenie (`.m3u8`, `.ts`) dla demuxerów.

Narzędzia instalowane przez `uv tool install yt-dlp streamlink mitmproxy pproxy` (binarki w `~/.local/bin`).

## Jak działa ruutu.fi (odtworzone z przechwyconego ruchu, listopad 2026)

### Logowanie (Sanoma SSO)
1. `GET https://auth-api.nm-ovp.nelonenmedia.fi/v2/sso/init/login?client=ruutu-web&widget=true&redirect_uri=https://www.ruutu.fi/sacu/authorized&ref_url=https://www.ruutu.fi/&region=fi` → 302 na `https://tili.sanoma.fi/kirjaudu?...client_id=...&state=...`.
2. `GET` strony `kirjaudu` ustawia cookie `_sso_csrf=<token>|<sig>`. Część przed `|` idzie w nagłówku `X-CSRF-Token`.
3. `POST https://tili.sanoma.fi/trpc/login.login` (JSON: `client_id, redirect_uri, state, scope:null, code_challenge:null, code_challenge_method:null, queryString, service:"nelonen", username, password, failedAttempts:0`) → `result.data.redirectUri` z `code` i `state`.
4. `GET <redirectUri>&style=ruutu3&remember_me=true` na www.ruutu.fi → 307 z `Set-Cookie: authAccessToken` (JWT ES384, 24 h) i `authRefreshToken` (1 rok). To jest Bearer do API.

Pułapki: cookies muszą być scoped per host (wysyłanie cookies z tili.sanoma.fi do ruutu.fi daje 403). Krok 4 to nawigacja strony: `Accept: text/html` + nagłówki `Sec-Fetch-*` typu navigate. tili.sanoma.fi ma DataDome; logowanie bez jego cookie działało, ale może zacząć zwracać 403.

### Media API
- `GET https://mcc.nm-ovp.nelonenmedia.fi/v2/media/<ID>` z `Authorization: Bearer <authAccessToken>`, `Origin`/`Referer` ruutu.fi.
- Odpowiedź: `clip.playback.{mediaType, runtime, drm{enabled,type,assetId}, geoblock{enabled,url}, media.streamUrls{webHls, apple, android, cast, dash, http, samsung, audioHls}}`.
- Wolne treści (kanał live `ruutu-free-01`, odcinki free) miały `drm.enabled=false`. Treści premium mogą mieć DRM, wtedy skrypt przerywa. Płatny mecz live (`jalkapallo-01`) też był bez DRM.
- Adresy `/stream/<id>` zwracają `type: "video-stream"` (wydarzenie: `name`, `start/end`, `status`, `statusMessage`, `onlineRights`) bez `clip`. Właściwe medium jest w `videoId`; skrypt pobiera je drugim wywołaniem. Niektóre wpisy `streamUrls` mogą być `null`.
- `streamUrls.http` to progresywny MP4 bez tokenu (3000 kbps). HLS/DASH mają w ścieżce JWT HS256 z polami `path`, `ip` (IP klienta wywołującego media API), `nbf`/`exp` (24 h). Pobieranie musi iść z tego samego IP co wywołanie media API, stąd proxy spójne dla całego skryptu.

### Geoblock
- `GET https://geoblock.nm-ovp.nelonenmedia.fi/geoblock` zwraca XML `<geoblock status="true">...</geoblock>` dla IP z zakresów hostingowych (np. Hetzner), pusty wynik dla IP residential. Egzekwowane wyłącznie w odtwarzaczu przeglądarkowym; media API i CDN tego nie sprawdzają.
- CORS tego endpointu dopuszcza nagłówek `x-gatling-token` (prawdopodobnie bypass do testów obciążeniowych Sanoma).

## Nagrywanie live (rec.sh)
- yt-dlp nie nadaje się do live HLS przez SOCKS: natywny downloader nie wspiera live i oddaje do ffmpeg, a ffmpeg nie umie SOCKS (401, bo token jest przypięty do IP tunelu).
- streamlink obsługuje `--http-proxy socks5h://...` i live HLS natywnie. Wyjście pipe'owane do `ffmpeg -c copy -movflags +frag_keyframe+empty_moov` daje MP4 poprawny także po Ctrl+C.
- Alternatywa: ffmpeg przez proxy HTTP (`-http_proxy http://127.0.0.1:8081`) z `pproxy -l http://127.0.0.1:8081 -r socks5://127.0.0.1:7654` jako konwerterem HTTP→SOCKS.

## Analiza ruchu
- Chrome z osobnym profilem: `open -na "Google Chrome" --args --proxy-server="http://127.0.0.1:8080" --ignore-certificate-errors-spki-list=<SPKI CA mitmproxy> --user-data-dir=~/.chrome-socks-7654`.
- Łańcuch: Chrome → mitmdump :8080 (`--mode upstream:http://127.0.0.1:8081 -w flows.mitm`) → pproxy :8081 → ssh -D :7654. mitmproxy nie ma upstream SOCKS, stąd pproxy.
- SPKI dla Chrome: `openssl x509 -in ~/.mitmproxy/mitmproxy-ca-cert.pem -pubkey -noout | openssl pkey -pubin -outform der | openssl dgst -sha256 -binary | base64`.
- Flows czyta się Pythonem z venv mitmproxy: `~/.local/share/uv/tools/mitmproxy/bin/python` + `mitmproxy.io.FlowReader`.

## Zasady
- Python: stdlib only w `ruutu_dl.py`, type hints, dataclasses, brak importów w funkcjach.
- Nigdy nie zapisywać w repo haseł, tokenów ani adresów e-mail kont.
