# The Chicago Bulletin

Every event we can find in Chicago for the next seven days, gathered automatically
and scored for community potential rather than hype. Real life, easier than the
feed.

It's a fork-and-run kit. You bring Claude Code and some tokens; Claude Code agents
do the work:

1. **Tej** seeds leads: venues, groups, aggregators.
2. **Scout** decides which calendars are worth scraping.
3. **Max** builds the scrapers, which count only after a live probe passes.
4. **Rater** scores each event.

A funnel shows where the pipeline is stuck, and `/next` works whichever stage
that is. The output is a SQLite DB on your machine and a static site you can open
on localhost or publish anywhere.

```sh
./setup.sh                              # venv, empty DB, tests
claude                                  # then: /next full, then /next (repeat)
python3 -m http.server 8000 -d docs     # http://localhost:8000
```

Built on the engine behind the [Philly Bulletin](https://phillybulletin.com).

## 0. What you need

- macOS or Linux, `git`, and **Python 3.10+** (3.13 recommended).
- **[Claude Code](https://claude.com/claude-code)** logged in. All the "AI" here is
  Claude Code subagents working in your session: no API keys, no other services.
  Every agent run comes out of your Claude usage.

## 1. Install (2 minutes)

```sh
git clone https://github.com/cguth7/chicago-bulletin && cd chicago-bulletin
./setup.sh
```

`setup.sh` creates `engine/.venv` (the only dependency is SQLAlchemy), creates an
empty DB at `~/chicago-bulletin-data/chicago.db`, runs the test suite, writes an
empty site export, and prints the funnel. The funnel should say
`NEXT → tej: empty DB — seed the first leads`.

The DB lives **outside** the repo on purpose: every checkout and agent session
shares one copy. To put it somewhere else, edit `data_dir` in `city.json`.

Also put your email in `contact` in `city.json`. The geocoder (OpenStreetMap's
Nominatim) sends it so OSM can reach you instead of blocking you. It's optional
but polite.

## 2. Build the database (this is where your tokens go)

```sh
claude          # in the repo folder
> /next
```

Each `/next` round reads the funnel, finds the stage where work is piling
up, and spawns the right agents for it:

| role | job | stage it drains |
|---|---|---|
| **Tej** | seeds venues, groups and aggregators; finds each one's calendar; vets venues that aggregators turn up | seed · find a calendar · vet |
| **Scout** | looks at each calendar and decides GO or NO-GO, with the reason | scout |
| **Max** | writes or reuses a scraper adapter; a source counts as `built` only after a live probe returns real events | build |
| **Rater** | scores each event (or each recurring series) on a six-part rubric | score |
| **Steward** | re-probes built sources and reports what broke | repair |

A round ends with the funnel before and after, written as a short entry in
`LEDGER.md`. That file is the memory between sessions: the next `/next` reads it
first, so decisions ("skip the suburbs") and half-finished work carry over. Run `/next` again for the next
round, or `/next 3 rounds` to let it keep going. `/next full` pushes one
source all the way to scored events on the site, then stops (a good first run).
You can steer it: `/next focus Pilsen and Hyde Park`, `/next just score`.

**Starting from your own list (recommended).** The best seed is a list you
already trust. For example, copy the Instagram accounts an events aggregator
follows or tags into `leads.txt`, one per line, then run `/next leads.txt`
(pasting the list after `/next` works too). A list always takes priority
over the funnel. Tej checks each lead against the DB, finds the venue or group
behind each handle, and attaches its calendar. Rounds after that go through Scout,
then Max, as usual.

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

The site only reads the files `engine/export.py` writes into `docs/`. `/next`
runs the export at the end of a round. To refresh by hand:
`cd engine && .venv/bin/python export.py`.

## 4. Keep it fresh (optional)

The export covers "today onward", so it goes stale at midnight. `./refresh.sh`
pulls every built source again and rewrites the site files. You can run it by hand,
or once a day from cron:

```
30 0 * * *  /path/to/chicago-bulletin/refresh.sh >> ~/chicago-bulletin-data/refresh.log 2>&1
```

Optionally, add a weekly health check of every built source. It only writes a
report; the Steward agent reads it first on its next run:

```
30 7 * * 1  cd /path/to/chicago-bulletin/engine && .venv/bin/python steward.py > ~/chicago-bulletin-data/steward_weekly.txt 2>&1
```

New events from the refresh show up without a score until your next
`/next` round scores them.

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
city.json              the city: name, timezone, focus bbox, data dir, site URL, contact
setup.sh / refresh.sh  install once / refresh daily
LEDGER.md              what each round did and decided (/next reads + writes it)
engine/                the engine (agents work here — see engine/CLAUDE.md)
  funnel.py            where is it stuck?
  ingest.py            built sources → events
  export.py            DB → docs/ (the only bridge to the site)
  scrapers/            28 platform adapters (Squarespace, WordPress, iCal,
                       Eventbrite, Localist, BiblioCommons, Wix, Luma, …)
  docs/rubric.md       how events are scored
.claude/agents/        the five roles
.claude/commands/      /next
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
