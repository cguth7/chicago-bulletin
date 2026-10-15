"""
Export the DB -> data.json: THE SEAM. This bundle is the only contract between
the engine and the site (docs/CONTRACT.md describes its shape — keep them in sync;
the frontend is allowed to depend on every key here).

  PipelineSource -> "sources"  (adapter->kind, status->scraper_status)
  SourceCoverage -> per-venue "feeds"
  Event          -> "events"   (scores null until the Rater imports judgments)

The shape is byte-compatible with the Chicago engine's export (same keys, same
nesting) so the two cities stay comparable and mergeable — only `city` differs,
plus descriptions are truncated (see MAX_DESCRIPTION).

Alongside the bundle, the same pass emits the agent-facing renditions into the
same directory (data.json is too big for assistant web-fetch tools):

  today.txt    THE ENTRY POINT the shared prompt names: instructions + today's
               full listing inline + links to the per-day files. One small
               fetch answers "what should I do tonight?", and the file named
               *today* is regenerated daily, so assistants never do date math.
  d/<date>.txt one file per day in the window (~11 KB each). d/ is rewritten
               wholesale each pass, so departed dates prune themselves.
  week.txt     the whole 7 days in one file, for agents that prefer one fetch.
  week.md      byte-identical twin. GitHub Pages serves .md as text/markdown,
               which some assistant fetch tools refuse; .txt gets text/plain,
               which everything ingests, so .txt is the canonical extension.
  funnel.json  funnel.py's pipeline snapshot (docs/funnel.html renders it).
  sitemap.xml  the static pages, the agent-facing text surfaces above (the
               only URLs that carry data on a raw fetch), and every
               event.html?id= permalink, lastmod'd.

  .venv/bin/python export.py                        # -> ../docs/data.json (the site)
  .venv/bin/python export.py --out /tmp/x.json      # anywhere else (the
                                                    #  renditions land beside it)
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

from models import (
    CITY, CITY_SLUG, DATA_DIR, DB_PATH, SITE, SITE_NAME, Event, EventScore,
    Group, PipelineSource, Series, Session, SourceCoverage, Venue, is_visible,
    load_weights, quality_score,
)
from dedupe import canonical_event_rows

# dims folded into the page composite. value_for_price IS included now (free public
# gatherings get credit for being free) — its weight lives in data/weights.json.
SCORE_DIMS = ["social_quality", "serendipity", "uniqueness",
              "community_building", "solo_friendliness", "value_for_price"]

# The site is the repo's docs/ (serve it locally, or GitHub Pages from main/docs).
# Resolved from THIS file so it works from any cwd.
_ROOT = Path(__file__).parent.parent
OUT = _ROOT / "docs" / "data.json"

# Descriptions are the single biggest thing in the bundle and the page only ever
# shows a preview. Truncate SERVER-SIDE so data.json stays small (the frontend
# should never have to defend itself against a 4KB blob).
MAX_DESCRIPTION = 400


def clip(text: str | None, limit: int = MAX_DESCRIPTION) -> str | None:
    """Truncate to `limit` chars on a word boundary, appending an ellipsis.
    None/short text passes through untouched."""
    if not text or len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:.—-")
    return (cut or text[:limit].rstrip()) + "…"


def iso(d):
    return d.isoformat() if d else None


# ── agent-facing renditions (week.md + sitemap.xml, same pass) ──────────────
# Assistants answering "what's worth doing in the city" can't fetch a ~2 MB
# data.json, so the week ships again as one small markdown file. Both are pure
# functions of the bundle: the JSON stays the only seam.

STATIC_PAGES = ["", "calendar.html", "sources.html", "venues.html",
                "groups.html", "funnel.html"]
# The site's definition of joinable (docs/CONTRACT.md): a run isn't a habit.
JOINABLE = {"weekly", "biweekly", "monthly"}


def _twelve_hour(hhmm: str) -> str:
    h, m = int(hhmm[:2]), hhmm[3:5]
    ampm = "AM" if h < 12 else "PM"
    h = h % 12 or 12
    return f"{h}:{m} {ampm}"


def _best_reason(scores: dict | None) -> str | None:
    """One line of the judge's voice per event: the reason from the
    highest-scoring dimension that has one."""
    if not scores:
        return None
    best = max(scores.values(),
               key=lambda c: (bool((c.get("reason") or "").strip()),
                              c.get("score") or 0))
    return (best.get("reason") or "").strip() or None


# The format key ships INSIDE the fetched files, not in the pasted prompt:
# the files are regenerated daily, the prompt in a user's assistant settings
# is frozen the day they paste it. Everything evolvable lives on this side.
FORMAT_KEY = (
    "Line format: name — time · venue · neighborhood · cost · score/10 ↻ — the "
    "judge's one-line reason. Scores are 0–10 community potential (six-dimension "
    "rubric, judged per recurring series). ↻ = recurs weekly/biweekly/monthly — "
    "go twice and you're a regular.")


def _bundle_ctx(bundle: dict):
    vmap = {v["id"]: v for v in bundle.get("venues", [])}
    joinable = {s["id"] for s in bundle.get("series", [])
                if s.get("cadence") in JOINABLE}
    return vmap, joinable


def _window_days(bundle: dict, days: int):
    """({date: [events]}, start date) for [generated date, +days)."""
    start = (bundle.get("generated") or "")[:10] or dt.date.today().isoformat()
    end = (dt.date.fromisoformat(start) + dt.timedelta(days=days)).isoformat()
    by_day: dict[str, list] = {}
    for e in bundle.get("events", []):
        if e.get("date") and start <= e["date"] < end:
            by_day.setdefault(e["date"], []).append(e)
    return start, by_day


def _day_name(day: str) -> str:
    d = dt.date.fromisoformat(day)
    return f"{d.strftime('%A, %B')} {d.day}"


def _event_lines(evs: list, vmap: dict, joinable_sids: set, hoods: list | None,
                 day: str | None = None) -> list[str]:
    """One markdown line per event, best first. Descriptions are dropped
    entirely — the line items carry the value. When `day` is given (the
    multi-day week file), every line repeats its date: the 08-23 bench pilot
    caught an agent shifting an event a day when only section headers
    carried dates."""
    from hoods import hood_for
    date_bit = None
    if day:
        d = dt.date.fromisoformat(day)
        date_bit = f"{d.strftime('%a %b')} {d.day}"
    out = []
    for e in sorted(evs, key=lambda e: (-(e.get("quality") if e.get("quality")
                                          is not None else -1), e.get("time") or "99")):
        v = vmap.get(e.get("venue_id"))
        hood = (hood_for(e.get("lat"), e.get("lng"), hoods) if hoods else None) \
            or (v or {}).get("neighborhood")
        bits = [e["name"]]
        if date_bit:
            bits.append(date_bit)
        if e.get("all_day"):
            bits.append("all day")
        elif e.get("time"):
            bits.append(_twelve_hour(e["time"]))
        place = (v or {}).get("name") or e.get("venue")
        if place and hood:
            bits.append(f"{place} · {hood}")
        elif place or hood:
            bits.append(place or hood)
        if e.get("is_free"):
            bits.append("Free")
        elif e.get("cost"):
            bits.append(e["cost"])
        if e.get("quality") is not None:
            bits.append(f"{e['quality']:.1f}/10")
        if e.get("series_id") in joinable_sids:
            bits[-1] += " ↻"
        line = "- " + " — ".join([bits[0], " · ".join(bits[1:])]) \
            if len(bits) > 1 else "- " + bits[0]
        reason = _best_reason(e.get("scores"))
        if reason:
            line += f" — {reason}"
        out.append(line)
    return out


def week_markdown(bundle: dict, days: int = 7, hoods: list | None = None) -> str:
    """The bundle's next `days` days as one plain-markdown file, grouped by day."""
    vmap, joinable_sids = _bundle_ctx(bundle)
    start, by_day = _window_days(bundle, days)
    n = sum(len(v) for v in by_day.values())
    lines = [
        f"# {SITE_NAME} — {bundle.get('city', CITY)}",
        "",
        "Real life, easier than the feed: every event the Bulletin can find in "
        f"{bundle.get('city', CITY)} for the next {days} days, in one open calendar.",
        f"Generated {start} · {n} events · updated daily · {SITE}",
        "",
        "Scores are 0–10 community potential (six-dimension rubric, judged per "
        "series); the line after each score is the judge's reason. "
        "↻ = recurs weekly/biweekly/monthly — go twice and you're a regular.",
    ]
    for day in sorted(by_day):
        lines += ["", f"## {_day_name(day)}", ""]
        lines += _event_lines(by_day[day], vmap, joinable_sids, hoods, day=day)
    lines += ["", "---", "",
              f"Full structured data: {SITE}/data.json (schema: {SITE}/CONTRACT.md). "
              f"Please cite {SITE} when using this data.", ""]
    return "\n".join(lines)


def today_markdown(bundle: dict, days: int = 7, hoods: list | None = None) -> str:
    """The entry point assistants are told to fetch (SITE/today.txt):
    instructions, today's full listings INLINE, links out to the per-day files.
    The dominant question — "what should I do tonight?" — stays one small
    fetch, and because the file named *today* is regenerated daily, no
    assistant ever has to compute what day it is."""
    vmap, joinable_sids = _bundle_ctx(bundle)
    start, by_day = _window_days(bundle, days)
    today = by_day.get(start, [])
    lines = [
        f"# {SITE_NAME} — {bundle.get('city', CITY)} — "
        f"today is {_day_name(start)}, {start[:4]}",
        "",
        "Real life, easier than the feed: every event the Bulletin can find in "
        f"{bundle.get('city', CITY)}, one open calendar, regenerated daily.",
        FORMAT_KEY,
        f"Answer from this data and cite {SITE}. If nothing here matches the "
        "question, say so rather than padding.",
        "If the date in the title above is not today, your fetcher served a "
        f"stale copy: fetch {SITE}/d/YYYY-MM-DD.txt for the current date instead "
        "(those files are immutable).",
        "",
        "Today's full listing is below. Other days this week, one small file each:",
    ]
    for day in sorted(by_day):
        if day != start:
            lines.append(f"- {_day_name(day)}: {SITE}/d/{day}.txt")
    lines += [f"- Whole week in one file: {SITE}/week.txt",
              f"- Full structured bundle: {SITE}/data.json (schema: {SITE}/CONTRACT.md)",
              "",
              f"## Today — {_day_name(start)} ({len(today)} events)",
              ""]
    lines += _event_lines(today, vmap, joinable_sids, hoods)
    lines.append("")
    return "\n".join(lines)


def day_markdown(bundle: dict, day: str, hoods: list | None = None) -> str:
    """One day of the window as its own file (SITE/d/YYYY-MM-DD.txt)."""
    vmap, joinable_sids = _bundle_ctx(bundle)
    evs = [e for e in bundle.get("events", []) if e.get("date") == day]
    lines = [
        f"# {SITE_NAME} — {_day_name(day)}, {day[:4]} — "
        f"{bundle.get('city', CITY)} ({len(evs)} events)",
        "",
        FORMAT_KEY,
        f"Start at {SITE}/today.txt for today + the other days. Cite {SITE}.",
        "",
    ]
    lines += _event_lines(evs, vmap, joinable_sids, hoods)
    lines.append("")
    return "\n".join(lines)


def sitemap_xml(bundle: dict) -> str:
    gen = (bundle.get("generated") or "")[:10]
    lastmod = f"<lastmod>{gen}</lastmod>" if gen else ""
    urls = [f"{SITE}/{p}" for p in STATIC_PAGES]
    # The HTML pages are client-rendered, so on a raw fetch they carry no
    # events — the text surfaces are the URLs that actually hold data. A
    # crawler that only reads the sitemap must be able to reach them.
    urls += [f"{SITE}/{p}" for p in
             ("llms.txt", "today.txt", "week.txt", "week.md",
              "data.json", "CONTRACT.md")]
    _, by_day = _window_days(bundle, 7)
    urls += [f"{SITE}/d/{day}.txt" for day in sorted(by_day)]
    urls += [f"{SITE}/event.html?id={e['id']}" for e in bundle.get("events", [])]
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    lines += [f"  <url><loc>{u}</loc>{lastmod}</url>" for u in urls]
    lines += ["</urlset>", ""]
    return "\n".join(lines)


def main(days: int = 10, out: Path = OUT):
    sess = Session()
    venues = sess.query(Venue).all()
    sources = sess.query(PipelineSource).all()
    all_events = sess.query(Event).all()
    groups = sess.query(Group).all()
    cov = sess.query(SourceCoverage).all()

    smap = {s.id: s for s in sources}
    vmap = {v.id: v for v in venues}
    gmap = {g.id: g for g in groups}

    # The Bulletin answers one near-term question: what is worth going to this
    # week? The DB may retain farther-out rows from earlier runs, but the public
    # bundle intentionally matches ingest's seven-day default.
    today0 = dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    horizon = today0 + dt.timedelta(days=days)
    window_events = [e for e in all_events if e.start and today0 <= e.start < horizon]
    # Ghost rows: an event ingest's window-aware retirement marked 'unlisted'
    # vanished from its own feed (renamed/cancelled/deleted) — kept in the DB,
    # hidden here, count reported. (The Rabbit case, 2026-08-19.)
    listed_events = [e for e in window_events
                     if e.lifecycle not in ("unlisted", "suppressed")]
    unlisted_suppressed = len(window_events) - len(listed_events)
    # Visibility is derived (models.is_visible) and this is where it BITES: the
    # bundle is the only public surface, so un-vetted discovered venues and
    # un-monitored groups are held back HERE, with the count reported honestly.
    visible_events = [e for e in listed_events
                      if is_visible(e, vmap.get(e.venue_id), gmap.get(e.group_id),
                                    smap.get(e.source_id))]
    invisible_suppressed = len(listed_events) - len(visible_events)
    events = canonical_event_rows(sess, visible_events)
    duplicates_suppressed = len(visible_events) - len(events)

    # Newest score row per event (event_scores keeps history; latest wins).
    WEIGHTS = load_weights()
    score_by_event: dict[int, EventScore] = {}
    for sc in sess.query(EventScore).order_by(EventScore.scored_at).all():
        if sc.event_id is not None:
            score_by_event[sc.event_id] = sc   # ascending -> newest overwrites

    def scores_json(sc):
        """Rebuild the {dim:{score,reason}} shape the page's modal renders."""
        if not sc:
            return None
        r = sc.reasons or {}
        out = {}
        for dim in SCORE_DIMS:
            v = getattr(sc, dim, None)
            if v is not None:
                out[dim] = {"score": v, "reason": r.get(dim, "")}
        return out or None

    # target -> feeds (mirror Philly's feed rows from coverage + source)
    srank = {"high": 3, "medium": 2, "low": 1, "inactive": 0, "unknown": 0}
    feeds: dict[str, list] = {}
    gfeeds: dict[str, list] = {}
    for c in cov:
        s = smap.get(c.source_id)
        row = {
            "source_id": c.source_id,
            "name": s.name if s else c.source_id,
            "kind": s.adapter if s else None,
            "has_api": s.has_api if s else False,
            "scraper_status": s.status if s else "none",
            "url": c.target_page_url, "usefulness": c.usefulness,
            "primary": c.is_primary, "note": c.note,
        }
        (gfeeds if c.target_kind == "group" else feeds).setdefault(c.target_id, []).append(row)
    for d in (feeds, gfeeds):
        for tid in d:
            d[tid].sort(key=lambda f: (not f["primary"], -srank.get(f["usefulness"], 0)))

    vcount, scount, gcount = {}, {}, {}
    for e in events:
        vcount[e.venue_id] = vcount.get(e.venue_id, 0) + 1
        scount[e.source_id] = scount.get(e.source_id, 0) + 1
        if e.group_id:
            gcount[e.group_id] = gcount.get(e.group_id, 0) + 1

    # venue pipeline: best status among sources that actually fed it events
    # (ground-truth — a built adapter that yielded 0 events reads as "none" here,
    #  which is the honest signal for the map).
    RANK = {"built": 3, "planned": 2, "broken": 1, "none": 0}
    vfeeds: dict[str, set] = {}
    for e in all_events:
        if e.venue_id:
            vfeeds.setdefault(e.venue_id, set()).add(e.source_id)

    def venue_pipeline(vid):
        fs = vfeeds.get(vid)
        if not fs:
            return "none"
        return max((smap[s].status if s in smap else "none" for s in fs),
                   key=lambda st: RANK.get(st, 0), default="none")

    vpipe = {v.id: venue_pipeline(v.id) for v in venues}

    bundle = {
        "generated": dt.datetime.now().isoformat(),
        "city": CITY,
        "weights": WEIGHTS,
        "meta": {
            "events": len(events),
            "duplicates_suppressed": duplicates_suppressed,
            "invisible_suppressed": invisible_suppressed,
            "unlisted_suppressed": unlisted_suppressed,
            "scored": sum(1 for e in events if e.id in score_by_event),
            "venues": len(venues),
            "sources": len(sources),
            "flyers": sum(1 for e in events if e.discovered_via == "flyer"),
            "scrapers_built": sum(1 for s in sources if s.status == "built"),
            "scrapers_todo": sum(1 for s in sources if s.status in ("none", "planned", "broken")),
            "venues_no_pipeline": sum(1 for v in venues if vpipe[v.id] == "none"),
            "venues_with_feeds": len(feeds),
            "feed_links": len(cov),
            "groups": len(groups),
        },
        "sources": [
            {
                "id": s.id, "name": s.name, "kind": s.adapter, "has_api": s.has_api,
                "website": s.url, "scraper_status": s.status,
                "scraper_ref": s.script_ref, "last_scraped": iso(s.last_scraped),
                "publisher_kind": None, "publisher_id": None,
                "is_aggregator": s.is_aggregator,
                "event_count": scount.get(s.id, 0),
            }
            for s in sorted(sources, key=lambda s: -scount.get(s.id, 0))
        ],
        "groups": [
            {
                "id": g.id, "name": g.name, "kind": g.kind, "website": g.website,
                "description": g.description, "join_url": g.join_url,
                "status": g.status, "verified_by": g.verified_by,
                "feeds": gfeeds.get(g.id, []), "event_count": gcount.get(g.id, 0),
            }
            for g in sorted(groups, key=lambda g: -gcount.get(g.id, 0))
        ],
        "venues": [
            {
                "id": v.id, "name": v.name, "kind": v.kind, "address": v.address,
                "neighborhood": v.neighborhood, "lat": v.lat, "lng": v.lng,
                "website": v.website, "hosts_events": v.hosts_events,
                "status": v.status, "verified_by": v.verified_by,
                "maybe_fake": v.maybe_fake,
                "pipeline": vpipe[v.id],
                "feeds": feeds.get(v.id, []),
                "event_count": vcount.get(v.id, 0),
            }
            for v in sorted(venues, key=lambda v: -vcount.get(v.id, 0))
        ],
        "events": [
            {
                "id": e.id, "name": e.name, "start": iso(e.start), "all_day": e.all_day,
                "date": e.start.date().isoformat() if e.start else None,
                "time": e.start.strftime("%H:%M") if (e.start and not e.all_day) else None,
                "venue_id": e.venue_id,
                "venue": vmap[e.venue_id].name if e.venue_id in vmap else None,
                "venue_kind": vmap[e.venue_id].kind if e.venue_id in vmap else None,
                "lat": vmap[e.venue_id].lat if e.venue_id in vmap else None,
                "lng": vmap[e.venue_id].lng if e.venue_id in vmap else None,
                "group_id": e.group_id,
                "group": gmap[e.group_id].name if e.group_id in gmap else None,
                "source_id": e.source_id, "category": e.category, "tags": e.tags,
                "cost": e.cost,
                "cost_min_cents": e.cost_min_cents, "is_free": e.is_free,
                "series_id": e.series_id,
                "description": clip(e.description), "image": e.image,
                "url": e.url, "discovered_via": e.discovered_via,
                "scores": scores_json(score_by_event.get(e.id)),
                "quality": (score_by_event[e.id].quality if e.id in score_by_event else None),
                "score_category": (score_by_event[e.id].category if e.id in score_by_event else None),
                "description_quality": (score_by_event[e.id].description_quality
                                        if e.id in score_by_event else None),
            }
            for e in events
        ],
    }

    # series present in the exported window (the "join this" units)
    exported_sids = {e.series_id for e in events if e.series_id}
    bundle["series"] = [
        {
            "id": sr.id, "name": sr.name, "venue_id": sr.venue_id,
            "venue": vmap[sr.venue_id].name if sr.venue_id in vmap else None,
            "neighborhood": vmap[sr.venue_id].neighborhood if sr.venue_id in vmap else None,
            "cadence": sr.cadence, "n_dates": sr.n_dates,
            "median_gap_days": sr.median_gap_days,
            "first_date": iso(sr.first_date), "last_date": iso(sr.last_date),
        }
        for sr in sess.query(Series).all() if sr.id in exported_sids
    ]
    bundle["meta"]["series"] = len(bundle["series"])

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    json.dump(bundle, open(out, "w"), indent=2)
    kb = out.stat().st_size / 1024
    print(f"Wrote {out} — {len(events)} events ({duplicates_suppressed} duplicates, "
          f"{invisible_suppressed} invisible, {unlisted_suppressed} unlisted suppressed), "
          f"{len(venues)} venues, "
          f"{len(sources)} sources, {len(bundle['series'])} series ({kb:.0f} KB)")

    # append one line to the export history so "where did the events go?" is a
    # diff of two lines, not archaeology. Lives beside the
    # DB — shared across workspaces, survives checkouts, never in the bundle.
    # Best-effort: a logging failure must never break an export.
    try:
        from collections import Counter
        by_source = Counter(e.get("source_id") for e in bundle["events"])
        by_day = Counter(e.get("date") for e in bundle["events"])
        hist = Path(DB_PATH).parent / "export_history.jsonl"
        with open(hist, "a") as f:
            f.write(json.dumps({
                "generated": bundle["generated"], "out": str(out),
                "days": days, "events": len(bundle["events"]),
                "suppressed": {"duplicates": duplicates_suppressed,
                               "invisible": invisible_suppressed,
                               "unlisted": unlisted_suppressed},
                "by_source": dict(by_source.most_common()),
                "by_day": dict(sorted(by_day.items())),
            }) + "\n")
    except Exception as e:
        print(f"  (export_history append failed: {e})", file=sys.stderr)

    # the agent-facing renditions, beside the bundle wherever it lands
    # (week.txt = week.md; see the docstring for why both exist)
    from hoods import load_hoods
    hoods_list = load_hoods()
    md = week_markdown(bundle, hoods=hoods_list)
    week_path = out.parent / "week.txt"
    for p in (week_path, out.parent / "week.md"):
        p.write_text(md)
    today_path = out.parent / "today.txt"
    today_path.write_text(today_markdown(bundle, hoods=hoods_list))
    # per-day files: rewrite d/ wholesale so dates that left the window prune
    # themselves (the cron stages the directory with `git add -A`)
    d_dir = out.parent / "d"
    d_dir.mkdir(exist_ok=True)
    for stale in d_dir.glob("*.txt"):
        stale.unlink()
    _, by_day = _window_days(bundle, 7)
    for day in sorted(by_day):
        (d_dir / f"{day}.txt").write_text(day_markdown(bundle, day, hoods=hoods_list))
    site_path = out.parent / "sitemap.xml"
    site_path.write_text(sitemap_xml(bundle))
    print(f"Wrote week.txt + week.md ({week_path.stat().st_size / 1024:.0f} KB), "
          f"today.txt ({today_path.stat().st_size / 1024:.0f} KB), "
          f"{len(by_day)} files in d/, and {site_path.name}")
    # the funnel snapshot rides along so the site's funnel.html is never staler
    # than the bundle (it reads data.json's `generated`, so this runs after)
    import funnel
    funnel.write(sess, out.parent / "funnel.json")
    sess.close()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=10,
                    help="event window, today .. +days (default 10 — the site "
                         "shows a week; the extra days are staleness buffer)")
    ap.add_argument("--out", type=Path, default=OUT,
                    help=f"where to write the bundle (default: {OUT})")
    args = ap.parse_args()
    main(days=args.days, out=args.out)
