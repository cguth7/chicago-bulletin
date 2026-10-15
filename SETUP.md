# Setup — build your own Chicago Bulletin

You bring Claude Code and some tokens. The repo brings the engine, five agent
roles, and a site. You end up with a local database of Chicago events, scored for
community potential, and a site that shows them.

## 0. What you need

- macOS or Linux, `git`, and **Python 3.10+** (3.13 recommended).
- **[Claude Code](https://claude.com/claude-code)** logged in. All the "AI" here is
  Claude Code subagents working in your session: no API keys, no other services.
  Every agent run comes out of your Claude usage.

## 1. Install (2 minutes)

```sh
git clone <this repo> chicago-bulletin && cd chicago-bulletin
./setup.sh
```

`setup.sh` creates `engine/.venv` (the only dependency is SQLAlchemy), creates an
empty DB at `~/chicago-bulletin-data/chicago.db`, runs the test suite, writes an
empty site export, and prints the funnel. The funnel should say
`NEXT → tej: empty DB — seed the first leads`.

The DB lives **outside** the repo on purpose: every checkout and agent session
shares one copy. To put it somewhere else, edit `data_dir` in `city.json`.

## 2. Build the database (this is where your tokens go)

```sh
claude          # in the repo folder
> /bootstrap
```

Each `/bootstrap` round reads the funnel, finds the stage where work is piling
up, and spawns the right agents for it:

| role | job | stage it drains |
|---|---|---|
| **Tej** | seeds venues, groups and aggregators; finds each one's calendar; vets venues that aggregators turn up | seed · find a calendar · vet |
| **Scout** | looks at each calendar and decides GO or NO-GO, with the reason | scout |
| **Max** | writes or reuses a scraper adapter; a source counts as `built` only after a live probe returns real events | build |
| **Rater** | scores each event (or each recurring series) on a six-part rubric | score |
| **Steward** | re-probes built sources and reports what broke | repair |

A round ends with the funnel before and after. Run `/bootstrap` again for the next
round, or `/bootstrap 3 rounds` to let it keep going. You can steer it:
`/bootstrap focus Pilsen and Hyde Park`, `/bootstrap just score`.

**Rough scale:** each round runs a handful of subagents, mostly Opus (the Rater
runs Sonnet). The same engine's Cambridge, MA port went from an empty DB to
7 built sources and ~100 visible scored events in about a dozen spawns. Chicago is
much bigger, so it grows for as long as you keep running rounds.

**Watching it:** `engine/.venv/bin/python engine/funnel.py` in a terminal, or
the funnel page on the site (below). It shows every stage, who each pile is
waiting on, and the bottleneck:

```
leads (venues + groups)        212   ← 30 waiting on tej
sources                        140   ← 44 waiting on scout
  NO-GO                         45
  built                         18   ← 33 waiting on max   ◀ BOTTLENECK
    healthy                     16   ← 2 waiting on steward
...
NEXT → max: GO sources not yet built (33)
```

## 3. Look at it locally

```sh
python3 -m http.server 8000 -d docs
```

Then open:
- <http://localhost:8000>: the bulletin (list and map)
- <http://localhost:8000/calendar.html>: the printable week
- <http://localhost:8000/funnel.html>: where the pipeline is stuck
- <http://localhost:8000/today.txt>: the plain-text version for assistants

The site only reads the files `engine/export.py` writes into `docs/`. `/bootstrap`
runs the export at the end of a round. To refresh by hand:
`cd engine && .venv/bin/python export.py`.

## 4. Keep it fresh (optional)

The export covers "today onward", so it goes stale at midnight. `./refresh.sh`
pulls every built source again and rewrites the site files. You can run it by hand,
or once a day from cron:

```
30 0 * * *  /path/to/chicago-bulletin/refresh.sh >> ~/chicago-bulletin-data/refresh.log 2>&1
```

New events from the refresh show up without a score until your next
`/bootstrap` round scores them.

## 5. Publish it (optional)

The site is static, so anything that serves a folder will work. With GitHub Pages:

1. Push the repo to GitHub, including the generated files in `docs/`
   (`data.json`, `today.txt`, `d/`, …). The DB stays on your machine.
2. Settings → Pages → Deploy from a branch → `main`, folder `/docs`.
3. Set `"site"` in `city.json` to the published URL (e.g.
   `https://you.github.io/chicago-bulletin`) and re-export. The text exports
   and sitemap use it for their links.
4. Uncomment the `git push` lines at the bottom of `refresh.sh` if the cron job
   should publish too.

## Where things are

```
city.json              the city: name, timezone, focus bbox, data dir, site URL
setup.sh / refresh.sh  install once / refresh daily
engine/                the engine (agents work here — see engine/CLAUDE.md)
  funnel.py            where is it stuck?
  ingest.py            built sources → events
  export.py            DB → docs/ (the only bridge to the site)
  scrapers/            28 platform adapters (Squarespace, WordPress, iCal,
                       Eventbrite, Localist, BiblioCommons, Wix, Luma, …)
  docs/rubric.md       how events are scored
.claude/agents/        the five roles
.claude/commands/      /bootstrap
docs/                  the site (static; city.js = the site's city config)
sources/               one doc per scraped source (Scout + Max write these)
```

## Notes

- The engine was built for the [Philly Bulletin](https://phillybulletin.com) and
  generalized from there. Many adapter docstrings name the Philadelphia source
  that first needed them. Two Chicago adapters (`chicago_parks` for the Park
  District, `do312` for Do312) come from an earlier Chicago prototype. Both, plus
  `bibliocommons` for the Chicago Public Library, returned live events on
  2026-10-15.
- The DB only changes through the operators in `engine/operators.py`. Agents are
  told not to edit it directly, and you shouldn't either. That rule is what keeps
  the funnel's numbers honest.
- Tests: `cd engine && .venv/bin/python tests.py` (no network). The adapter tests
  use real Philadelphia feed fixtures, which is expected.
