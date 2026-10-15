#!/bin/sh
# The daily refresh: pull fresh events from every built source, then rebuild the
# site files. data.json windows from "today", so a live site needs this once a
# day (cron/launchd — see README.md). New events land unscored; the next
# /next round scores them.
cd "$(dirname "$0")/engine" || exit 1
.venv/bin/python ingest.py --stale 12 --days 10 || echo "ingest had failures (non-fatal; the Steward catches rot)"
.venv/bin/python export.py --days 10 || exit 1

# Publishing to GitHub Pages? Uncomment to push the refreshed site files:
# cd .. && git add docs && git commit -qm "refresh $(date +%F)" && git push -q
