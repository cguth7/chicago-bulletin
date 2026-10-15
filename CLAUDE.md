# The Chicago Bulletin

A city event engine + a static site: every event we can find in Chicago for the
next seven days, scored for community potential. Agents build the database; the
site reads one exported file.

- **README.md** — the human instructions (install, build, view, publish).
- **LEDGER.md** — the running log and the memory between sessions: `/next` reads
  the last entry first and appends one when it finishes.
- **city.json** — the whole city parameterization (name, timezone, focus bbox,
  data dir, site origin). The engine reads nothing city-specific from anywhere else.
- **engine/** — agent territory. Read `engine/CLAUDE.md` before touching anything
  there. The DB (`~/chicago-bulletin-data/chicago.db`, outside the repo) is the
  only source of truth, mutated only through idempotent operators. Roles are
  scoped allowlists (`.claude/agents/`); `built` is earned by a live probe.
- **docs/** — the site. Static HTML/JS, no build step: `python3 -m http.server
  8000 -d docs`. `docs/city.js` is the site's city config; `docs/funnel.html`
  shows where the pipeline is stuck.
- **sources/** — one doc per scraped source (Scout writes, Max appends).
- **The seam:** `engine/export.py → docs/data.json`. The contract is
  `docs/CONTRACT.md`. Either side can be rebuilt without touching the other.
- **The loop:** `/next` reads `engine/funnel.py` and works whichever stage
  is stuck — Tej (leads) → Scout (go/no-go) → Max (build) → Tej (vet) → Rater
  (score) → export.

KISS rule: if a feature isn't needed to answer "what's worth going to this week,"
don't build it.
