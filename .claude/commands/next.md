---
description: Run one round of the Chicago Bulletin build loop — read the funnel, work the stuck stage with the role agents, report before/after
---

Run the Chicago Bulletin build loop. The repo and the DB carry all state — do not
ask the user for context. Arguments (optional): $ARGUMENTS
(e.g. "full", "3 rounds", "focus Pilsen and Logan Square", "just score", a pasted list of
leads, or a path to a file of them).

## 0. A list of leads overrides the funnel
If the arguments contain a LIST of leads — Instagram handles copied off an events
aggregator, venue names, URLs, one per line or comma-separated — or name a file
holding one (e.g. `leads.txt`), that list IS this round's work, whatever the
funnel says. Split it into batches of ~20 and spawn one **Tej** per batch in
parallel (charter template: "Seed from a list of leads"). Then continue the round
from step 3 (re-run the funnel, report, write the ledger entry). The next round's funnel will route
the new source leads to Scout.

## 0b. `full` — one source, end to end
If the arguments say `full` (or "full loop"), the goal is depth, not breadth:
get at least ONE source from lead to scored events on the site, then stop.
Work thin slices instead of draining each queue:
- **seed** (empty DB only): ONE Tej spawn, ~15 civic leads in one neighborhood.
- **scout**: ONE sweep of ~10 sources, the likeliest GOs first (a known
  platform — WordPress/Tribe, Eventbrite, iCal, Squarespace — over bespoke sites).
- **build**: ONE Max spawn on the single most promising GO (or 2–3 on the same
  platform), then `ingest.py --days 10`.
- **vet / score / export**: as usual, but only for what this run produced.

Loop rounds (steps 1–3, one LEDGER entry for the whole run) until the funnel
shows `scored` > 0 for events from a source built in this run and `export.py`
has run. Cap: 8 rounds. If a stage dead-ends (every scouted source NO-GO, a
build that won't pass its probe), take the next candidate in that stage rather
than moving on. If the cap hits first, stop and report exactly where it stuck.

## 1. Orient
```
cd <repo>/engine && .venv/bin/python funnel.py
```
If that fails because `.venv` or the DB is missing, run `../setup.sh` first.
Read `engine/CLAUDE.md` once per session — it carries the rules — and the LAST
entry of `LEDGER.md`: what the previous round did, decided, and left half-done.
Honor its decisions; finish what it left off before starting something new.

## 2. Work the NEXT move the funnel names
The funnel picks the furthest-downstream non-empty queue (finish started work
before starting more). Spawn the role agents in `.claude/agents/` for it.
Batch the spawns and launch independent ones in parallel, in one message.

| next | do this |
|---|---|
| **seed** (empty DB / dry funnel) | 3 parallel **Tej** spawns, one neighborhood cluster each (e.g. Loop + West Loop + River North · Logan Square + Wicker Park + Humboldt Park · Pilsen + Bridgeport + Hyde Park), 25–40 leads each. Civic long tail first: CPL branches, Park District parks, community centers, farmers markets, chambers/SSAs, plus the citywide aggregators (Do312, Chicago Park District, DCASE). Each finds the calendar (`add_source_lead` + `connect_coverage`) for what it adds. |
| **find a calendar** (tej) | `.venv/bin/python sniff.py --known` first (free), then one **Tej** spawn per ~20 remaining leads. |
| **scout** | **Scout** in SWEEP mode, 10–20 sources per spawn, parallel. Hand each spawn its source ids from `funnel.py --json`. |
| **build** (max) | Group the GO sources by adapter family; one **Max** spawn per 2–4 same-platform sources. After builds: `.venv/bin/python ingest.py --days 10` (aggregators that fan out to unknown venues: `ingest.py --source <id> --discover`). |
| **repair** (steward) | One **Steward** spawn; then Max for anything it says needs a rebuild. |
| **vet** (tej) | One **Tej** spawn on the discovered queue: verify + `decide_monitored` what's real and in Chicago; `DECLINED:` notes for the rest. |
| **score** (rater) | `.venv/bin/python scorer.py --pending --batch-size 30 --out /tmp/pending.json`, then one **Rater** spawn per batch in parallel (each imports its own file). |
| **export** | `.venv/bin/python export.py` |
| *nothing queued* | The pipeline is drained. Grow it: seed neighborhoods that are thin on the site (Tej, as in **seed**) — or stop and tell the user it's caught up. |

Deterministic scripts before agents, and after any round that added venues:
`.venv/bin/python geocode.py && .venv/bin/python hoods.py --assign`.

Every spawn prompt says: the repo path, the exact ids to work, and "write only
through operators.py". Model policy: the Rater runs Sonnet (its charter says so);
everything else defaults to Opus.

## 3. Close the round
- If visible events changed: score anything unscored, then `export.py`.
- Run `funnel.py` again and report the before/after in a few lines: what moved,
  what's now the bottleneck, and the honest numbers (built, visible, scored).
- Append an entry to `LEDGER.md` in the shape its header describes: Ran /
  Funnel before → after / Decided or learned / Left off. A few lines, real
  numbers, no padding.
- Keep `.venv/bin/python tests.py` green if anyone touched `engine/` code (Max
  writes adapters).

## 4. Stop or continue
Each round spends real tokens. Stop after ONE round and report, unless the
arguments asked for more rounds — then loop back to step 1 until those rounds are
done or the funnel says nothing is queued. `full` loops by its own rule (step 0b).
