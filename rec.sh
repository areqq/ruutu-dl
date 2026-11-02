#!/usr/bin/env bash
# Nagrywa strumień HLS ruutu-free-01 w najwyższej jakości do MP4 w bieżącym katalogu,
# przez tunel SOCKS5 na 127.0.0.1:7654. Bez rekodowania (remux TS -> MP4).
# Użycie: ./rec.sh URL_M3U8 [NAZWA]
#   ./rec.sh "https://.../hls-index.m3u8?..." mecz
# URL z tokenem weź z `ruutu_dl.py --url-only ...` (pole streamUrls.webHls.url w media API).
# Tokeny wygasają po ~24 h i są przypięte do IP, które wywołało media API.
# Przerwanie: Ctrl+C. MP4 jest fragmentowany, więc plik jest poprawny także po przerwaniu.
set -euo pipefail

if [[ $# -lt 1 || -z "$1" ]]; then
  echo "Użycie: $0 URL_M3U8 [NAZWA]" >&2
  exit 2
fi
URL="$1"
NAME="${2:-ruutu-live}"
PROXY="socks5h://127.0.0.1:7654"
OUT="$PWD/${NAME}-$(date +%Y%m%d-%H%M%S).mp4"
STREAMLINK="$HOME/.local/bin/streamlink"

for bin in "$STREAMLINK" ffmpeg; do
  if ! command -v "$bin" >/dev/null 2>&1; then
    echo "Brak: $bin" >&2
    exit 1
  fi
done

if ! nc -z 127.0.0.1 7654 2>/dev/null; then
  echo "Tunel SOCKS na 127.0.0.1:7654 nie odpowiada" >&2
  exit 1
fi

echo "Nagrywam do: $OUT"
"$STREAMLINK" \
  --http-proxy "$PROXY" \
  --http-header "Referer=https://www.ruutu.fi/" \
  --http-header "Origin=https://www.ruutu.fi" \
  --hls-live-restart \
  --stdout \
  "hls://$URL" best \
| ffmpeg -hide_banner -loglevel warning -stats \
  -i pipe:0 \
  -c copy -bsf:a aac_adtstoasc \
  -movflags +frag_keyframe+empty_moov+default_base_moof \
  -f mp4 -y "$OUT"
