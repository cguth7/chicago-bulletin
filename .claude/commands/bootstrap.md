---
description: Run one round of the Chicago Bulletin build loop — read the funnel, work the stuck stage with the role agents, report before/after
---

Run the Chicago Bulletin build loop. The repo and the DB carry all state — do not
ask the user for context. Arguments (optional): $ARGUMENTS
(e.g. "3 rounds", "focus Pilsen and Logan Square", "just score").

## 1. Orient
```
cd <repo>/engine && .venv/bin/python funnel.py
```
If that fails because `.venv` or the DB is missing, run `../setup.sh` first.
Read `engine/CLAUDE.md` once per session — it carries the rules.

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
- Keep `.venv/bin/python tests.py` green if anyone touched `engine/` code (Max
  writes adapters).

## 4. Stop or continue
Each round spends real tokens. Stop after ONE round and report, unless the
arguments asked for more rounds — then loop back to step 1 until those rounds are
done or the funnel says nothing is queued.
