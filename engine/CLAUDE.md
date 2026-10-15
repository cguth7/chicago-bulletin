# engine/ — the city event engine

The Chicago Bulletin's backend. Agent territory: every tool is a CLI with honest
output. The site (`../docs/`) is the human half, and `export.py → ../docs/data.json`
is the ONE seam between them (`../docs/CONTRACT.md` is that seam's contract).

The engine was first built for Philadelphia (phillybulletin.com) and also runs
Cambridge, MA; adapter docstrings often name the Philly source that first needed
them. That's provenance, not a bug.

## Run it with `.venv/bin/python` — NOT `python3`
SQLAlchemy 2.0's `Mapped[str | None]` annotations need Python 3.10+; the venv is
3.13 with the engine's only third-party dependency (SQLAlchemy).

```
cd engine && .venv/bin/python tests.py      # the no-network suite (must stay green)
```

## City parameters — `../city.json`
The repo-root `city.json` is the whole parameterization: city/state/timezone,
`bbox` (the downtown focus band — a sanity signal, never a filter), site origin +
name, hoods-geojson path, data dir. `models.py` loads it once and exposes
constants (CITY, TIMEZONE, BBOX, DB_PATH, SITE, SITE_NAME, HOODS_PATH…).

The DB lives at `~/chicago-bulletin-data/chicago.db` — OUTSIDE the repo, so every
checkout, worktree, and agent session shares one copy (per-checkout copies fork
silently). `BULLETIN_DB=<path>` overrides it for scratch runs.

Hoods = 98 Chicago neighborhoods in `../docs/hoods.geojson` (the site's map reads
the same file). First match in `order` wins; the display key is `group || name`.

## The model (three independent axes)
1. **Discovery** — `discovered_via` / `discovered_by` columns on Venue/Group/Source,
   written once on first touch.
2. **Extraction** — `pipeline_sources` (1 row per scrapeable endpoint) →
   `source_coverage` (M:N bridge to venues/groups). An aggregator is ONE source
   row that fans out via many coverage rows. The scrape endpoint lives ONLY on
   `pipeline_sources.url`; coverage carries `target_page_url` + `routing_key`.
   Venue coverage is the *where* (routing); group coverage is the *who*.
3. **Monitoring** — a `monitored` bool on Venue/Group. Event visibility is
   DERIVED (`models.is_visible`), never stored.

Plus the Scout's verdict on each source: `scout_verdict` = NULL (not yet
scouted) | `go` | `no_go`. `funnel.py` reads it to tell the Scout's queue from the
Builder's.

## The DB is the source of truth — NO JSON, NO drop-rebuild
Mutate the durable layer ONLY via idempotent operators (`operators.py`). Do NOT
edit JSON; do NOT `init_db(drop=True)` to "reseed". A "lead" is an un-built row in
its real table. See `docs/leads-pipeline.md` for the contract + a worked example.

```
  lead born      → add_venue_lead / add_group_lead / add_source_lead
                   (add_source_lead derives an adapter from a platform guess
                   via platform() / PLATFORM_MAP)
  fill/correct   → update_venue_fields / update_group_fields / update_source_fields
                   (update_source_fields refuses adapter/url on a BUILT source)
  scout verdict  → set_source_status(id, 'planned', verdict='go')
                   set_source_status(id, 'none', verdict='no_go', notes=why)
  pipeline built → mark_source_built      (status → 'built', GATED by a live probe)
                   build_source           (the atomic path Max uses:
                   mark_source_built + connect_coverage + verify_target + decide_monitored)
  turns on venue → connect_coverage       (writes the source_coverage row)
  is it real     → verify_target          (venue/group status → 'verified')
  do we want it  → decide_monitored       (owns venue.monitored; requires a BUILT
                   dedicated source with health='ok', or a verified target)
  re-probe       → probe_source_live      (the gate alone, writes nothing)
  routing surgery → set_source_aggregator / retarget_coverage / remove_coverage
  keep it true   → steward.py             (re-probe + re-derive drift; --fix applies)
  same real room → merge_venues           (explicit survivor; old slug becomes alias)
  bad auto-alias → remove_alias           (undo a wrong alias; frees the slug)
  same listing?  → record_dedupe_verdict  (persist a judge's same/distinct call)
```

The Python idiom (imports only resolve from `engine/`, and Bash cwd is not
reliable between calls, so ALWAYS one line):
```
cd <repo>/engine && .venv/bin/python - <<'PY'
from models import Session, Event, Venue, Group, PipelineSource, SourceCoverage
s = Session()
from operators import build_source   # operators take sess=s; commit once at the end
PY
```

## Roles are scoped allowlists (`../.claude/agents/`)
Tej creates + vets leads · Scout triages + documents sources · Max builds
pipelines · Rater judges events · Steward notices rot. **`built` is earned by a
live probe**, so a Scout cannot fabricate a pipeline and a Rater cannot touch
anything but `event_scores`. That scoping is the safety property; keep it.

Model policy: the Rater runs Sonnet (its charter's `model:` field); the other
roles default to Opus. Generic spawns inherit the session model.

## Files
- `models.py` — schema + the city block; `leads_inbox` view; `is_visible`.
- `operators.py` — the sanctioned durable-layer mutations (idempotent, no drop).
- `discover.py` — firehose operators + `decide_monitored` (re-exported by operators).
- `funnel.py` — where the pipeline is stuck: counts every stage, names who each
  queue waits on, picks the next move. `--json` for agents; `--write` for the site.
- `scrapers/` — the transport adapters (run `.venv/bin/python -m scrapers` for the
  LIVE list). `core.py` is the shared half (fetch/UA/norm/validate — the seam tests
  patch); one file per platform family (`squarespace.py`, `wordpress.py`,
  `feeds.py`, `jsonld.py`, `eventbrite.py`, `ticketing.py`, `algolia.py`);
  `templates.py` holds the Chicago adapters (`chicago_parks`, `do312`).
  `scrapers/__init__.py` holds `ADAPTERS` (dispatch on `PipelineSource.adapter`).
  Smoke-test one: `python -m scrapers <adapter> <url>`.
- `probe_sources.py` — sniffs a built source's real platform, suggests adapter+url.
- `sniff.py` — the same fingerprints pointed at LEADS with websites: 1 page fetch
  + ≤2 endpoint confirms → adapter guess. `--known` does the DB's uncovered rows.
- `ingest.py` — runs built sources → Event rows. Idempotent; re-runs update in
  place. Each pull also retires ghosts (future events that vanished from their
  feed → `lifecycle='unlisted'`). `operators.suppress_series` /
  `suppress_event` hide upstream-wrong listings for good.
- `dedupe.py` — explainable venue/event identity rules. `python dedupe.py` only
  reports; venue merges always go through `merge_venues`. `--suspects` /
  `--import-verdicts` run the same-listing judgment loop.
- `series.py` — derives recurring clusters (pure date math), re-run by ingest.
- `costs.py` — cost text → cents. Deterministic.
- `scorer.py` — **plumbing only, no API**: `--pending` emits judgment batches,
  `--import` writes them, `--status` reports coverage. See `docs/rubric.md`.
- `steward.py` — the maintenance pass (report-first, `--fix` applies).
- `export.py` — THE SEAM → `../docs/data.json` plus the agent-facing text files
  (`today.txt`, `week.txt`, `d/`), `sitemap.xml`, and `funnel.json`.
  `--out` writes elsewhere. Every run appends per-source counts to
  `~/chicago-bulletin-data/export_history.jsonl`.
- `hoods.py` — reads `../docs/hoods.geojson`; `--assign` fills `venue.neighborhood`.
- `geocode.py` — Nominatim (1 req/s), cached in `data/geocode_cache.json`.
- `view_tables.py` — dump every table to one HTML file for eyeballing.
- `tests.py` — the no-network suite. It gates every change to this directory.
- `data/weights.json` — the composite's weight sets (the Rater tunes this).
- `../sources/<source-id>.md` — one doc per source (Scout's verdict, Max's build
  log). `_TEMPLATE.md` is the format.

## Scoring: subagents, not an API
Rater **subagents** judge in-session — no API keys, no external spend:
```
.venv/bin/python scorer.py --pending --batch-size 30 --out /tmp/pending.json
#   → fan batches out to parallel Rater subagents, each given docs/rubric.md
.venv/bin/python scorer.py --import /tmp/batch1_scores.json     # idempotent
.venv/bin/python scorer.py --status
```
Series-first: one judgment per series, instances inherit. The rubric is shared
with every city on this engine — never edit it per-batch.

## How to run
- `.venv/bin/python funnel.py` — start here: what's stuck, who's next
- `.venv/bin/python ingest.py --days 7` — scrape built sources → this week's events
- `.venv/bin/python ingest.py --source <id> --days 7` — verify ONE source fast
- `.venv/bin/python ingest.py --stale 12` — skip sources pulled <12h ago
- `.venv/bin/python ingest.py --source <agg> --discover` — firehose mode: unrouted
  aggregator events mint candidate venues + 'discovered' coverage. Their events
  stay INVISIBLE until Tej vets the venue (verify → decide_monitored).
- `.venv/bin/python geocode.py && .venv/bin/python hoods.py --assign` — coords → hoods
- `.venv/bin/python steward.py [--fix|--skip-probe]` — maintenance pass
- `.venv/bin/python export.py` — refresh the site (`--out /tmp/x.json` to dry-run)

`data.json` goes stale at midnight (the export windows from "today"), so a live
site needs `../refresh.sh` once a day — see `../SETUP.md`.

## KISS rule
If a feature isn't needed to answer "what's worth going to this week," don't
build it.
