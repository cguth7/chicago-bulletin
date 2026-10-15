#!/bin/sh
# One-time setup: the engine's venv, an empty DB, a green test run, and an
# empty site export so docs/ opens without errors. Safe to re-run.
set -e
cd "$(dirname "$0")/engine"

if [ ! -x .venv/bin/python ]; then
  PY=""
  for c in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
      PY="$c"; break
    fi
  done
  [ -n "$PY" ] || { echo "Need Python 3.10+ (3.13 recommended). Install it, then re-run."; exit 1; }
  echo "→ creating engine/.venv with $PY"
  "$PY" -m venv .venv
  .venv/bin/python -m pip install --quiet --upgrade pip
  .venv/bin/python -m pip install --quiet sqlalchemy
fi

echo "→ initializing the DB (idempotent — never drops anything)"
.venv/bin/python -c "from models import init_db, DB_PATH; DB_PATH.parent.mkdir(parents=True, exist_ok=True); init_db(); print('  ', DB_PATH)"

echo "→ running the test suite"
.venv/bin/python tests.py | tail -1

echo "→ writing the (empty) site export"
.venv/bin/python export.py >/dev/null

echo
.venv/bin/python funnel.py
echo
echo "Done. Next: open Claude Code in this folder and run /bootstrap."
echo "View the site: python3 -m http.server 8000 -d docs  →  http://localhost:8000"
