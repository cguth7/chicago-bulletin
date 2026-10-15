"""Live smoke test CLI: `.venv/bin/python -m scrapers <adapter> <url> [days]`."""
from __future__ import annotations

import datetime as dt

from . import ADAPTERS, validate_events

# Live smoke test for ONE adapter+url — the fastest way to check a Scout's
# endpoint before Max builds it. No hardcoded Philly endpoints here on
# purpose: an unverified URL in the code reads as a verified one later.
#
#   .venv/bin/python -m scrapers html_jsonld https://example.org/events [days]
import sys

if len(sys.argv) < 3:
    print(f"usage: python -m scrapers <adapter> <url> [days]\n"
          f"adapters: {', '.join(ADAPTERS)}")
    raise SystemExit(1)
adapter, url = sys.argv[1], sys.argv[2]
days = int(sys.argv[3]) if len(sys.argv) > 3 else 60
start = dt.date.today()
end = start + dt.timedelta(days=days)
rows = ADAPTERS[adapter](url, "smoke-test", start, end)
v = validate_events(rows, start, end)
print(f"{adapter}: {v['n_valid']}/{v['n_total']} valid in {days}d "
      f"(e.g. {v['sample']})")
for r in rows[:5]:
    print(f"  {r['date']} {r['time']:5s} {r['name'][:60]}")
