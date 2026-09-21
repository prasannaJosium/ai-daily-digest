#!/bin/sh
# Daily entry point on Linux (cron): collect, render, and keep a dated log.
cd "$(dirname "$0")" || exit 1
mkdir -p logs
PYTHONIOENCODING=utf-8 python3 collector.py >> "logs/$(date -u +%F).log" 2>&1
rc=$?
# keep two weeks of logs
find logs -name '*.log' -mtime +14 -delete
exit $rc
