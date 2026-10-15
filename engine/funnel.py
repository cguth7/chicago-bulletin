"""
The funnel — where is the pipeline stuck? One read-only pass over the DB that
counts every stage a lead moves through and names who each pile is waiting on:

  leads (venues + groups)  → Tej finds each one's calendar (a source lead)
  sources                  → Scout says GO / NO-GO
  GO sources               → Max builds them (`built` = a live probe passed)
  built-but-failing        → Steward re-probes, Max repairs
  discovered venues        → Tej vets what aggregators minted (`--discover`)
  events this week         → Rater scores them
  the site                 → export.py writes docs/data.json

The biggest pile is the BOTTLENECK. `next` is the stage /next should work
on: the furthest-downstream non-empty queue (finish what's started before you
start more). An empty DB says "seed".

  .venv/bin/python funnel.py            # the terminal view
  .venv/bin/python funnel.py --json     # the same dict, for agents
  .venv/bin/python funnel.py --write    # also write ../docs/funnel.json (the site's
                                        #   funnel.html reads it; export.py does this too)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

from models import (
    CITY, DB_PATH, Event, EventScore, Group, PipelineSource, Session, SourceCoverage,
    Venue, is_visible,
)

OUT = Path(__file__).parent.parent / "docs" / "funnel.json"
WINDOW_DAYS = 7


def compute(sess) -> dict:
    today = dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    horizon = today + dt.timedelta(days=WINDOW_DAYS)

    venues = sess.query(Venue).all()
    groups = sess.query(Group).all()
    sources = {s.id: s for s in sess.query(PipelineSource).all()}
    cov = sess.query(SourceCoverage).all()

    # ── leads: does each venue/group have a source pointed at it yet? ──────────
    covered = {(c.target_kind, c.target_id) for c in cov}
    disc_only = {}                        # target -> True if ALL its coverage is 'discovered'
    for c in cov:
        k = (c.target_kind, c.target_id)
        disc_only[k] = disc_only.get(k, True) and c.adoption == "discovered"

    def declined(t):
        return (t.notes or "").lstrip().upper().startswith("DECLINED")

    leads = [("venue", v) for v in venues] + [("group", g) for g in groups]
    no_source = [t for k, t in leads
                 if (k, t.id) not in covered and not t.monitored and not declined(t)]
    to_vet = [t for k, t in leads
              if disc_only.get((k, t.id)) and t.status != "verified"
              and not t.monitored and not declined(t)]

    # ── sources: Scout → Max → healthy ────────────────────────────────────────
    built = [s for s in sources.values() if s.status == "built"]
    healthy = [s for s in built if s.health == "ok"]
    failing = [s for s in built if s.health != "ok"]
    unbuilt = [s for s in sources.values() if s.status != "built"]
    awaiting_scout = [s for s in unbuilt if not s.scout_verdict]
    no_go = [s for s in unbuilt if s.scout_verdict == "no_go"]
    awaiting_max = [s for s in unbuilt if s.scout_verdict == "go"]

    # ── events in the window: visible? scored? ────────────────────────────────
    vmap = {v.id: v for v in venues}
    gmap = {g.id: g for g in groups}
    scored_keys = {k for (k,) in sess.query(EventScore.dedupe_key).distinct()}
    evs = (sess.query(Event)
           .filter(Event.start >= today, Event.start < horizon,
                   Event.lifecycle == "active").all())
    visible = [e for e in evs
               if is_visible(e, vmap.get(e.venue_id), gmap.get(e.group_id),
                             sources.get(e.source_id))]
    hidden = len(evs) - len(visible)
    unscored = [e for e in visible if e.dedupe_key not in scored_keys]

    # ── the site: when was data.json last written? ────────────────────────────
    data_json = Path(__file__).parent.parent / "docs" / "data.json"
    generated = None
    if data_json.exists():
        try:
            generated = json.loads(data_json.read_text()).get("generated")
        except (ValueError, OSError):
            pass
    last_pull = max((s.last_scraped for s in built if s.last_scraped), default=None)
    stale = bool(visible) and (
        generated is None
        or dt.datetime.fromisoformat(generated) < today
        or (last_pull is not None and dt.datetime.fromisoformat(generated) < last_pull))

    # ── the queues, upstream → downstream ─────────────────────────────────────
    def names(rows, n=8):
        return [r.name for r in rows[:n]]

    queues = [
        {"stage": "find a calendar", "who": "tej", "n": len(no_source),
         "what": "venues/groups with no source lead yet", "examples": names(no_source)},
        {"stage": "scout", "who": "scout", "n": len(awaiting_scout),
         "what": "source leads with no GO/NO-GO verdict", "examples": names(awaiting_scout)},
        {"stage": "build", "who": "max", "n": len(awaiting_max),
         "what": "GO sources not yet built", "examples": names(awaiting_max)},
        {"stage": "repair", "who": "steward", "n": len(failing),
         "what": "built sources whose last probe wasn't healthy",
         "examples": [f"{s.name} ({s.health})" for s in failing[:8]]},
        {"stage": "vet", "who": "tej", "n": len(to_vet),
         "what": "aggregator-discovered venues/groups waiting for a yes/no",
         "examples": names(to_vet)},
        {"stage": "score", "who": "rater", "n": len(unscored),
         "what": f"visible events in the next {WINDOW_DAYS} days with no score",
         "examples": names(unscored)},
        {"stage": "export", "who": "export.py", "n": 1 if stale else 0,
         "what": "docs/data.json is older than today or than the last pull",
         "examples": []},
    ]

    bottleneck = max(queues, key=lambda q: q["n"])
    if bottleneck["n"] == 0:
        bottleneck = None
    # next move: drain from the bottom up — finished work beats started work.
    # Export is the round's closing step, not work in itself: it's next only
    # when nothing else is queued.
    live = [q for q in queues if q["n"] > 0 and q["stage"] != "export"]
    nxt = live[-1] if live else (queues[-1] if queues[-1]["n"] else None)
    if not sources and not leads:
        nxt = {"stage": "seed", "who": "tej", "n": 0,
               "what": "empty DB — seed the first leads", "examples": []}

    return {
        "city": CITY,
        "generated": dt.datetime.now().isoformat(timespec="seconds"),
        "db": str(DB_PATH),
        "totals": {
            "venues": len(venues), "groups": len(groups),
            "monitored": sum(1 for t in venues + groups if t.monitored),
            "sources": len(sources), "built": len(built), "healthy": len(healthy),
            "no_go": len(no_go),
            "events_window": len(evs), "visible": len(visible), "hidden": hidden,
            "scored": len(visible) - len(unscored),
            "site_generated": generated,
        },
        "queues": queues,
        "bottleneck": bottleneck and bottleneck["stage"],
        "next": nxt,
    }


def render(f: dict) -> str:
    t = f["totals"]
    q = {x["stage"]: x for x in f["queues"]}

    def wait(stage):
        x = q[stage]
        if not x["n"]:
            return ""
        flag = "   ◀ BOTTLENECK" if f["bottleneck"] == stage else ""
        return f"   ← {x['n']} waiting on {x['who']}{flag}"

    leads = t["venues"] + t["groups"]
    lines = [
        f"{f['city']} — the funnel  ({f['generated'][:16].replace('T', ' ')})",
        "",
        f"leads (venues + groups)      {leads:>5}{wait('find a calendar')}",
        f"sources                      {t['sources']:>5}{wait('scout')}",
        f"  NO-GO                      {t['no_go']:>5}",
        f"  built                      {t['built']:>5}{wait('build')}",
        f"    healthy                  {t['healthy']:>5}{wait('repair')}",
        f"monitored venues + groups    {t['monitored']:>5}{wait('vet')}",
        f"events, next {WINDOW_DAYS} days          {t['events_window']:>5}"
        + (f"   ({t['hidden']} hidden behind unmonitored venues/groups)" if t["hidden"] else ""),
        f"  visible                    {t['visible']:>5}",
        f"  scored                     {t['scored']:>5}{wait('score')}",
        f"site data.json               {(t['site_generated'] or 'never')[:16]:>16}"
        + ("   ← stale: run export.py" if q["export"]["n"] else ""),
        "",
    ]
    n = f["next"]
    if n:
        lines.append(f"NEXT → {n['who']}: {n['what']}"
                     + (f" ({n['n']})" if n.get("n") else ""))
        for ex in (n.get("examples") or [])[:5]:
            lines.append(f"         · {ex}")
    else:
        lines.append("NEXT → nothing queued. Seed more leads to grow, or run the "
                     "daily refresh (ingest → score → export).")
    return "\n".join(lines)


def write(sess=None, out: Path = OUT) -> dict:
    own = sess is None
    sess = sess or Session()
    try:
        f = compute(sess)
    finally:
        if own:
            sess.close()
    out.write_text(json.dumps(f, indent=1))
    return f


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--json", action="store_true", help="print the dict instead")
    ap.add_argument("--write", action="store_true", help=f"also write {OUT.name} for the site")
    args = ap.parse_args()
    s = Session()
    f = compute(s)
    s.close()
    if args.write:
        OUT.write_text(json.dumps(f, indent=1))
    print(json.dumps(f, indent=1) if args.json else render(f))
