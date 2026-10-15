"""
Series derivation — promote recurring-event clusters to first-class rows.

Pure date math, no LLM, no network. Clusters events by (venue_id, normalized
name), infers cadence from the median gap between distinct dates, upserts the
`series` table, and stamps events.series_id. Re-derived after every ingest
(ingest calls derive_series) and checkable by the Steward.

The payoff: the Rater judges a SERIES once and instances inherit (scorer.py's
--pending groups series-first), and the Bulletin can say "join this weekly thing"
instead of listing 14 identical Tuesdays. Run vs series is the key cut: Antigone
(17 dates, median gap 1d) is a theater RUN you see once; Bridge Club (gap 7d)
you join.

  .venv/bin/python series.py     # derive + print the table
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

from models import Event, Series, Session, now

MIN_INSTANCES = 3

# strip episode/volume/date suffixes so "Footholds Vol. 7" == "Footholds Vol. 8"
EPISODE = re.compile(
    r"\b(vol\.?|volume|ep\.?|episode|week|no\.?|pt\.?|part|session)\s*\d+\b"
    r"|#\s*\d+\b"  # '#' is non-word, so it can't sit after \b inside the group above
    r"|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b"
    r"|\b\d{1,2}/\d{1,2}\b", re.I)
NONWORD = re.compile(r"[^a-z0-9]+")


def norm_name(name: str) -> str:
    s = EPISODE.sub(" ", (name or "").lower())
    return NONWORD.sub(" ", s).strip()


def cadence_of(gaps: list[int]) -> str:
    median = sorted(gaps)[len(gaps) // 2]
    if median <= 3:
        return "run"
    if 5 <= median <= 9:
        return "weekly"
    if 10 <= median <= 18:
        return "biweekly"
    if 19 <= median <= 40:
        return "monthly"
    return "irregular"


def derive_series(sess) -> tuple[int, int]:
    """(Re)compute the series table + events.series_id from ALL events (past
    included — cadence needs the full horizon). Idempotent; deletes series rows
    whose cluster dissolved. Returns (n_series, n_events_stamped)."""
    buckets: dict[tuple, list[Event]] = defaultdict(list)
    for e in sess.query(Event).all():
        if e.start:
            key = norm_name(e.name)
            if key:
                buckets[(e.venue_id, key)].append(e)

    live_ids: set[str] = set()
    stamped = 0
    for (venue_id, key), members in buckets.items():
        dates = sorted({e.start.date() for e in members})
        gaps = [(b - a).days for a, b in zip(dates, dates[1:]) if (b - a).days > 0]
        sid = f"{venue_id}|{key}"
        if len(dates) < MIN_INSTANCES or not gaps:
            for e in members:           # cluster too small (or dissolved) — unstamp
                if e.series_id == sid:
                    e.series_id = None
            continue
        live_ids.add(sid)
        row = sess.get(Series, sid)
        if row is None:
            row = Series(id=sid, venue_id=venue_id, name="", cadence="",
                         median_gap_days=0, n_dates=0)
            sess.add(row)
        row.name = Counter(e.name for e in members).most_common(1)[0][0]
        row.cadence = cadence_of(gaps)
        row.median_gap_days = sorted(gaps)[len(gaps) // 2]
        row.n_dates = len(dates)
        row.first_date = min(e.start for e in members)
        row.last_date = max(e.start for e in members)
        row.last_seen = now()
        for e in members:
            if e.series_id != sid:
                e.series_id = sid
                stamped += 1

    for row in sess.query(Series).all():   # drop orphans (cluster dissolved)
        if row.id not in live_ids:
            sess.delete(row)
    sess.flush()
    return len(live_ids), stamped


if __name__ == "__main__":
    sess = Session()
    n, stamped = derive_series(sess)
    sess.commit()
    for s in sess.query(Series).order_by(Series.n_dates.desc()).all():
        print(f"{s.cadence:>9}  n={s.n_dates:<3} gap~{s.median_gap_days:<3}d "
              f"{s.name[:55]}  @ {s.venue_id}")
    print(f"\n{n} series, {stamped} events (re)stamped")
    sess.close()
