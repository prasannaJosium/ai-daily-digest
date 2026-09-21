#!/bin/sh
# Installs the Linux schedule in the current user's crontab (no sudo needed):
#   - collect + render every day at 02:00 UTC (07:30 IST)
#   - keep the Tailscale-only web server up (at boot and every 5 minutes)
#   ./install-cron.sh             # install or update
#   ./install-cron.sh --uninstall
DIR="$(cd "$(dirname "$0")" && pwd)"
TAG="# ai-daily"
current="$(crontab -l 2>/dev/null | grep -v "$TAG")"
if [ "$1" = "--uninstall" ]; then
    printf '%s\n' "$current" | crontab -
    echo "removed ai-daily cron entries"
    exit 0
fi
chmod +x "$DIR/run.sh" "$DIR/serve.sh"
{
    [ -n "$current" ] && printf '%s\n' "$current"
    echo "${AIDAILY_CRON:-0 2 * * *} $DIR/run.sh $TAG"
    echo "@reboot sleep 30 && $DIR/serve.sh $TAG"
    echo "*/5 * * * * $DIR/serve.sh >/dev/null 2>&1 $TAG"
} | crontab -
crontab -l | grep "$TAG"
