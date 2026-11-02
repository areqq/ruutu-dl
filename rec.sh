#!/usr/bin/env bash
# Records a live HLS stream to a fragmented MP4 in the current directory through a SOCKS5 tunnel
# on 127.0.0.1:7654, using streamlink + ffmpeg (no re-encoding). Alternative to ruutu_dl.py.
# Usage: ./rec.sh M3U8_URL [NAME]
#   ./rec.sh "https://.../hls-index.m3u8?..." match
# Get the tokenized URL from `ruutu_dl.py --url-only ...` (streamUrls.webHls.url in the media API).
# Tokens expire after ~24 h and are bound to the IP that called the media API.
# Stop with Ctrl+C. The MP4 is fragmented, so the file stays valid even when interrupted.
set -euo pipefail

if [[ $# -lt 1 || -z "$1" ]]; then
  echo "Usage: $0 M3U8_URL [NAME]" >&2
  exit 2
fi
URL="$1"
NAME="${2:-ruutu-live}"
PROXY="socks5h://127.0.0.1:7654"
OUT="$PWD/${NAME}-$(date +%Y%m%d-%H%M%S).mp4"
STREAMLINK="$HOME/.local/bin/streamlink"

for bin in "$STREAMLINK" ffmpeg; do
  if ! command -v "$bin" >/dev/null 2>&1; then
    echo "Missing: $bin" >&2
    exit 1
  fi
done

if ! nc -z 127.0.0.1 7654 2>/dev/null; then
  echo "SOCKS tunnel on 127.0.0.1:7654 is not responding" >&2
  exit 1
fi

echo "Recording to: $OUT"
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
