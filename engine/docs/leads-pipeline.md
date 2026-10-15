# Leads pipeline — the agent-native workflow

Status: **LIVE.** Everything below exists and is the contract agents follow instead
of editing JSON. The operator list below is the founding core; the full current
surface (routing surgery, `remove_alias`, `record_dedupe_verdict`, ...) is the
block in `engine/CLAUDE.md`, which is canonical.

## The decision (why this exists)

The durable layer (venues / groups / sources / coverage) was being **seeded from
`data/leads_venues.json` with `init_db(drop=True)`** — a destructive batch rebuild.
That's the wrong primitive for agent work: state lives in a flat file, the DB is
nuked every change, and there's no visible "this lead progressed from candidate →
built." We are cutting JSON out entirely. **The DB becomes the source of truth,
mutated by idempotent operators.**

We chose **Option B**: a "lead" is not a separate table — it's an un-built row in
its destination table. The funnel is a status ladder, not a promotion step.

```
  venues / groups : status candidate → verified        (a lead = candidate)
  sources         : status none → planned → built       (a lead = none/planned)
  the connection  : a source_coverage row links source → venue/group
  the "want it?"  : venue.monitored, owned by decide_monitored()
```

A single read-only `leads_inbox` SQL VIEW unions the un-built rows across all three
tables, so "show me everything pending" is one query (and one tab in tables.html) —
without a second physical table to keep in sync.

## Flag audit (2026-05-31) — what's real vs. fluff

Annotated inline in `models.py`. Summary, so agents stop trusting dead flags:

| Flag | Verdict | Note |
|---|---|---|
| `source.status` (none/planned/built) | 🟢 LIVE | the build ladder; `is_visible` keys off `built` |
| `venue.monitored` | 🟢 LIVE | owned by `decide_monitored()` |
| `coverage.adoption` (adopted/discovered) | 🟢 LIVE | `discovered` = the vetting queue |
| `coverage.is_primary`, `is_aggregator`, `event.lifecycle`, `discovered_via/by` | 🟢 LIVE | real write paths |
| `venue.status` (candidate/verified/rejected) | 🔴 DEAD | only ever `candidate`; no verification process exists |
| `venue.maybe_fake` | 🔴 DEAD | zero write sites |
| `venue.verified_by` | 🔴 DEAD | always the default `scraper` |
| `venue.hosts_events`, `coverage.usefulness` | 🟠 FROZEN | LLM guess at seed, never derived from real event yield |

Decision: **document now, don't drop the columns yet.** Future cleanup options —
(a) drop the DEAD ones; (b) make the FROZEN ones DERIVED by an operator from actual
event counts. Not done yet on purpose.

**Update 2026-06-09:** `venue.status` and `verified_by` are now 🟢 LIVE —
`verify_target` (fired by `build_source` on a passing gate) writes both. A fourth
category exists: **DRIFTED** (has a writer, never re-run) — `monitored` was the
case study (34/45 venues stale); `steward.py` now re-derives it, and
`decide_monitored` requires status='built' AND health='ok'. `maybe_fake` remains
the only 🔴 DEAD venue flag. Sources gained `discovered_via/by`;
`coverage.url` split into `target_page_url` + `routing_key`.

## The operator interface (`operators.py`)

The ONLY sanctioned way to mutate the durable layer. Every operator is idempotent
(safe to re-run; touches only its delta) and NEVER drops tables.

```
add_venue_lead(name, *, kind, address, neighborhood, website, hood,
               discovered_via, discovered_by) -> Venue
    # mint a candidate venue if new; idempotent on slug. This is "a lead is born."
    # NB: fills its fields ONLY on create. Re-touching an existing lead just bumps
    # last_seen — it will NOT clobber a row, so it can't fill a gap discovered later.
    # To add/correct an attribute on an existing venue, use update_venue_fields.

update_venue_fields(venue_id, *, address, neighborhood, website, kind, notes) -> Venue
    # patch a venue's descriptive ATTRIBUTES after the row exists (only non-None
    # args apply; a passed value DOES overwrite — a deliberate correction). The
    # sanctioned way to fill e.g. a street address Scout/Max found post-mint, so
    # geocode.py can then resolve coords. Idempotent. CANNOT touch status /
    # monitored / discovered_via/by (provenance is write-once; status & monitored
    # are owned by verify_target / decide_monitored) or lat/lng (owned by geocode.py).

merge_venues(survivor_id, duplicate_id, *, reason) -> (Venue, counts)
    # explicit, reviewed identity merge: repoints events and coverage, coalesces
    # same-source repeats, RETAINS different-source event observations, re-derives
    # series, and retains duplicate_id in venue_aliases so a later rediscovery cannot
    # recreate the duplicate. Idempotent.

add_group_lead(name, *, kind, website, discovered_via, discovered_by) -> Group
    # same, for an organizer/producer (the 'who').

add_source_lead(name, *, adapter, url, is_aggregator, has_api,
                discovered_via) -> PipelineSource
    # register a scrapeable endpoint at status='none'/'planned'. Not built yet.

mark_source_built(source_id, *, adapter=None, url=None) -> PipelineSource
    # the build flip, GATED. Runs the adapter live (probe_source_live) and stamps
    # health: ok (>=1 valid event) | no_events | http_error (404/403/..) | parse_error,
    # with health_detail = the actual message ('HTTP 403', 'all past', exception).
    # Always sets status='built' (it's in ingest's worklist) but a failing gate is a
    # VISIBLE built-but-failing source, never a silent false-built. "built" = wired;
    # "health=ok" = actually producing. add_source_lead can NEVER mint 'built' (caps
    # at 'planned') — building is earned by this live test, not a JSON platform guess.

probe_source_live(adapter, url, name, *, days=120) -> (health, detail)
    # THE BUILD GATE, pure (writes nothing): run the adapter, classify the result.
    # Minimum event schema (scrapers.valid_event): name + a date that parses & is
    # in-window. >=1 such event = ok. Recovers HTTP status on empty (adapters swallow
    # errors) so 404/403 are distinguished from genuinely-dormant.

connect_coverage(source_id, target_kind, target_id, *, is_primary,
                 adoption='adopted', discovered_by) -> SourceCoverage
    # "if it turns on a venue or group, make a row connecting that in coverage."
    # idempotent on (source_id, target_kind, target_id).

decide_monitored(venue_or_group) -> bool   # EXISTS in discover.py — keep/reuse
    # owns the monitored flag: wanted iff a dedicated source feeds it (or verified).
```

Note: `discover.py` already implements the *going-forward* half of this for
firehose discovery (`discover_from_events`, `sync_firehose_coverage`,
`decide_monitored`). `operators.py` generalizes those into the named operators
above so an agent (or `ingest.py`) calls them everywhere, not just at firehose time.

## The clean cut — done

All of it shipped: `operators.py`, the `leads_inbox` VIEW (`models.py`), the
`decide_monitored` owner.

## The worked example (Cole's Bar, the way it SHOULD happen)

```
add_venue_lead("Cole's Bar", kind="bar", hood="Logan Square", ...,
               discovered_via="llm")              # -> Venue(status=candidate)
add_source_lead("Cole's Bar Events", adapter="html_jsonld",
                url="https://colesbarchicago.com/")  # -> PipelineSource(status=none)
# agent researches, confirms the adapter works against live site:
mark_source_built("coles-bar-events")             # status -> built
connect_coverage("coles-bar-events", "venue", "coles-bar", is_primary=True)
decide_monitored(<coles venue>)                   # monitored -> True (dedicated source)
# ingest.py then scrapes built sources -> events. No JSON, no drop, fully queryable.
```
