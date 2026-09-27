#!/data/data/com.termux/files/usr/bin/bash
cd "$(dirname "$0")"
set -e
command -v cloudflared >/dev/null 2>&1 || pkg install -y cloudflared
PORT="${PORT:-5050}"; export PORT
python app.py & FLASK=$!
trap "kill $FLASK 2>/dev/null" INT TERM EXIT
sleep 2
echo ""
echo "================================================"
echo "  Copy the https://*.trycloudflare.com URL below"
echo "  Camera   : <URL>/camera"
echo "  Director : <URL>/control"
echo "  Audience : <URL>/watch"
echo "  Debug    : F12 console + status line at page bottom"
echo "================================================"
echo ""
cloudflared tunnel --url "https://localhost:$PORT" --no-tls-verify --no-autoupdate

