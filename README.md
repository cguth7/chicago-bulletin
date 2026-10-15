# The Chicago Bulletin

Every event we can find in Chicago for the next seven days, gathered automatically
and scored for community potential rather than hype. Real life, easier than the
feed.

It's a fork-and-run kit. Claude Code agents do the work:

1. **Tej** seeds leads: venues, groups, aggregators.
2. **Scout** decides which calendars are worth scraping.
3. **Max** builds the scrapers, which count only after a live probe passes.
4. **Rater** scores each event.

A funnel shows where the pipeline is stuck, and `/next` works whichever stage
that is. The output is a SQLite DB on your machine and a static site you can open
on localhost or publish anywhere.

```sh
./setup.sh                              # venv, empty DB, tests
claude                                  # then: /next   (repeat)
python3 -m http.server 8000 -d docs     # http://localhost:8000
```

Full instructions: **[SETUP.md](SETUP.md)**.

Built on the engine behind the [Philly Bulletin](https://phillybulletin.com).
