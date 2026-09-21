#!/bin/sh
# Serves site/ (plus the saved-stories API, see server.py) on the Tailscale address only (private to the tailnet). Safe to run repeatedly:
# cron calls it at boot and every 5 minutes, and it starts the server only if it isn't running.
cd "$(dirname "$0")" || exit 1
PORT="${AIDAILY_PORT:-8420}"
BIND="${AIDAILY_BIND:-$(tailscale ip -4 2>/dev/null | head -1)}"
[ -n "$BIND" ] || { echo "no tailscale address yet" >&2; exit 1; }
if ss -ltn "sport = :$PORT" | grep -q LISTEN; then
    exit 0
fi
mkdir -p logs site
nohup python3 server.py --bind "$BIND" --port "$PORT" >> logs/serve.log 2>&1 &
echo "serving http://$BIND:$PORT/"
