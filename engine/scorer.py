"""
Scorer — the PLUMBING for the Rater job. It does not judge anything and it makes
no network calls.

DECISION (2026-08-16, PLAN.md): scoring is done by Rater SUBAGENTS in-session, not
by this script calling an LLM API. So scorer.py is reduced to two verbs:

  --pending          print the unscored work as JSON batches, series-first
  --import <file>    write a Rater's returned scores into event_scores

`--import` is the Rater's ONLY write path into the DB, and it is idempotent:
re-importing the same file writes nothing.

  .venv/bin/python scorer.py --pending                    # all unscored future work
  .venv/bin/python scorer.py --pending --batch-size 30    # ~30 judgments per batch
  .venv/bin/python scorer.py --pending --out /tmp/p.json  # to a file
  .venv/bin/python scorer.py --import /tmp/scores.json    # write judgments
  .venv/bin/python scorer.py --status                     # coverage report

The rubric (the full six-dimension prompt, the calibration anchors, and both JSON
shapes) lives in `docs/rubric.md` — hand that file to every Rater subagent
verbatim so Philly scores stay comparable with Chicago's.

SERIES-FIRST (the cost lever, preserved from the API version): instances of one
series are the same event repeating, so the Rater judges the series ONCE and every
instance inherits that judgment. A 14-week knitting circle is 1 judgment, not 14.
Standalone events are judged individually.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from models import (
    CITY, Event, EventScore, Group, Series, Session, Venue, quality_score,
)

RUBRIC_PATH = Path(__file__).parent / "docs" / "rubric.md"

# Recorded in event_scores.model. Rater subagents judge in-session, so there is no
# API model id to record; override with --model if you want the exact agent model.
DEFAULT_MODEL = "rater-subagent"

UNIVERSAL = ["social_quality", "serendipity", "uniqueness",
             "community_building", "solo_friendliness"]
# Canonical event categories. The scraper's raw `category` is messy source data;
# this is the clean Rater-assigned one.
CATEGORIES = [
    "music", "performing_arts", "visual_arts", "food_drink", "community", "talk",
    "class", "sports", "nightlife", "family", "wellness", "professional",
    "science_tech", "comedy", "film", "spiritual", "market", "hobby", "activism", "other",
]
BATCH_SIZE = 30
MAX_DESC = 600           # what the Rater sees; export.py truncates harder for the site


def _today0():
    return dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)


# ─────────────────────────────── --pending ─────────────────────────────────

def pending(sess, *, include_past=False, rescore=False, batch_size=BATCH_SIZE,
            limit=None) -> dict:
    """The unscored work, grouped series-first, cut into batches.

    A UNIT is one judgment: either a series (every instance inherits the result)
    or a standalone event. Returns the JSON-serializable payload."""
    venues = sess.query(Venue).all()
    vname = {v.id: v.name for v in venues}
    vhood = {v.id: v.neighborhood for v in venues}
    gname = {g.id: g.name for g in sess.query(Group).all()}
    scored = {k for (k,) in sess.query(EventScore.dedupe_key).distinct()}

    q = sess.query(Event)
    if not include_past:
        q = q.filter(Event.start >= _today0())
    events = [e for e in q.all() if rescore or e.dedupe_key not in scored]

    smap = {s.id: s for s in sess.query(Series).all()}
    by_series: dict[str, list[Event]] = {}
    singles: list[Event] = []
    for e in events:
        if e.series_id and e.series_id in smap:
            by_series.setdefault(e.series_id, []).append(e)
        else:
            singles.append(e)

    def unit(e: Event, unit_id: str, kind: str, members: list[Event],
             recurrence: str = "") -> dict:
        return {
            "unit_id": unit_id,
            "kind": kind,
            "name": e.name,
            "description": (e.description or "")[:MAX_DESC],
            "venue": vname.get(e.venue_id, ""),
            "neighborhood": vhood.get(e.venue_id),
            "category": e.category or "",
            "cost": e.cost or "",
            "date": e.start.date().isoformat() if e.start else "",
            "time": e.start.strftime("%H:%M") if (e.start and not e.all_day) else "",
            "recurrence": recurrence or "one-time / unknown",
            "organizer": gname.get(e.group_id, ""),
            "url": e.url or "",
            "n_events": len(members),
        }

    units = []
    for sid, members in by_series.items():
        rep = min(members, key=lambda e: e.start or dt.datetime.max)
        sr = smap[sid]
        units.append(unit(rep, f"series:{sid}", "series", members,
                          recurrence=f"{sr.cadence}, {sr.n_dates} dates on calendar"))
    for e in singles:
        units.append(unit(e, f"event:{e.dedupe_key}", "event", [e]))

    # earliest work first — the near-term week is what the Bulletin shows
    units.sort(key=lambda u: (u["date"] or "9999", u["name"]))
    if limit:
        units = units[:limit]

    batches = [
        {"batch": i + 1, "n_units": len(units[i:i + batch_size]),
         "units": units[i:i + batch_size]}
        for i in range(0, len(units), batch_size)
    ]
    n_events = sum(u["n_events"] for u in units)
    return {
        "generated": dt.datetime.now().isoformat(),
        "city": CITY,
        "rubric": str(RUBRIC_PATH.relative_to(Path(__file__).parent.parent)),
        "batch_size": batch_size,
        "n_units": len(units),
        "n_events": n_events,
        "n_inherit": n_events - len(units),
        "batches": batches,
    }


# ─────────────────────────────── --import ──────────────────────────────────

def _validate(entry: dict, i: int) -> tuple[dict | None, str | None]:
    """Check one Rater result. Returns (clean, error). Fail-fast validation runs
    over the WHOLE file before anything is written, so a malformed batch can
    never half-land."""
    uid = entry.get("unit_id")
    if not uid or not isinstance(uid, str):
        return None, f"[{i}] missing unit_id"
    if not uid.startswith(("series:", "event:")):
        return None, f"[{i}] unit_id must start with 'series:' or 'event:' ({uid})"
    dims = {}
    reasons = {}
    for d in UNIVERSAL:
        v = entry.get(d)
        if not isinstance(v, dict) or "score" not in v:
            return None, f"[{i}] {uid}: missing dimension '{d}' (need {{score, reason}})"
        try:
            s = int(v["score"])
        except (TypeError, ValueError):
            return None, f"[{i}] {uid}: {d}.score is not an int"
        if not 1 <= s <= 10:
            return None, f"[{i}] {uid}: {d}.score {s} out of range 1-10"
        dims[d] = s
        reasons[d] = (v.get("reason") or "").strip()
    vfp = entry.get("value_for_price")
    vfp_score = None
    if isinstance(vfp, dict) and "score" in vfp:
        try:
            vfp_score = int(vfp["score"])
        except (TypeError, ValueError):
            return None, f"[{i}] {uid}: value_for_price.score is not an int"
        if not 1 <= vfp_score <= 10:
            return None, f"[{i}] {uid}: value_for_price.score out of range 1-10"
        reasons["value_for_price"] = (vfp.get("reason") or "").strip()
    cat = entry.get("category")
    if cat is not None and cat not in CATEGORIES:
        cat = "other"
    dq = entry.get("description_quality")
    if dq not in (None, "rich", "thin", "missing"):
        return None, f"[{i}] {uid}: description_quality must be rich|thin|missing"
    return {"unit_id": uid, "dims": dims, "vfp": vfp_score, "reasons": reasons,
            "category": cat, "description_quality": dq}, None


def _members(sess, unit_id: str) -> list[Event]:
    """Every event a judgment applies to. A series judgment lands on all of that
    series' instances (that IS the inheritance); an event judgment on one row."""
    kind, _, ident = unit_id.partition(":")
    if kind == "series":
        return sess.query(Event).filter(Event.series_id == ident).all()
    return sess.query(Event).filter(Event.dedupe_key == ident).all()


def _unchanged(row: EventScore, clean: dict, model: str) -> bool:
    """Would writing this judgment change anything? (The idempotency test.)"""
    return (
        row.model == model
        and all(getattr(row, d) == clean["dims"][d] for d in UNIVERSAL)
        and row.value_for_price == clean["vfp"]
        and row.category == clean["category"]
        and row.description_quality == clean["description_quality"]
        and (row.reasons or {}) == clean["reasons"]
    )


def import_scores(sess, path: Path, *, model: str = DEFAULT_MODEL) -> int:
    """Read a Rater's JSON and write event_scores rows. Idempotent: a re-import of
    the same file writes nothing. Returns a process exit code."""
    raw = json.loads(Path(path).read_text())
    if isinstance(raw, list):
        entries = raw
    elif isinstance(raw, dict):
        entries = raw.get("scores") or raw.get("results") or []
        model = raw.get("model") or model
    else:
        print("import: file must be a list of results or {'scores': [...]}")
        return 1
    if not entries:
        print("import: no score entries in file — nothing to do.")
        return 1

    cleans, errors = [], []
    for i, entry in enumerate(entries):
        clean, err = _validate(entry, i)
        (errors if err else cleans).append(err or clean)
    if errors:
        print(f"import: {len(errors)} invalid entr{'y' if len(errors) == 1 else 'ies'} "
              f"— NOTHING was written:")
        for e in errors[:20]:
            print(f"  {e}")
        return 1

    # newest existing judgment per dedupe_key (history is kept; newest wins)
    latest: dict[str, EventScore] = {}
    for sc in sess.query(EventScore).order_by(EventScore.scored_at).all():
        latest[sc.dedupe_key] = sc

    written = unchanged = 0
    unmatched = []
    for clean in cleans:
        members = _members(sess, clean["unit_id"])
        if not members:
            unmatched.append(clean["unit_id"])
            continue
        composite = {d: {"score": v} for d, v in clean["dims"].items()}
        if clean["vfp"] is not None:
            composite["value_for_price"] = {"score": clean["vfp"]}
        for m in members:
            prev = latest.get(m.dedupe_key)
            if prev is not None and _unchanged(prev, clean, model):
                unchanged += 1
                continue
            sess.add(EventScore(
                event_id=m.id, dedupe_key=m.dedupe_key, model=model,
                value_for_price=clean["vfp"],
                quality=quality_score(composite),
                category=clean["category"],
                description_quality=clean["description_quality"],
                reasons=clean["reasons"], scored_at=dt.datetime.now(),
                **clean["dims"],
            ))
            written += 1
    sess.commit()

    print(f"import: {len(cleans)} judgments -> {written} score rows written, "
          f"{unchanged} already current (no-op).")
    if unmatched:
        print(f"  {len(unmatched)} unit_id(s) matched no events (stale batch?): "
              f"{', '.join(unmatched[:5])}{' …' if len(unmatched) > 5 else ''}")
    return 0


# ─────────────────────────────── --status ──────────────────────────────────

def status(sess) -> None:
    scored = {k for (k,) in sess.query(EventScore.dedupe_key).distinct()}
    future = sess.query(Event).filter(Event.start >= _today0()).all()
    have = sum(1 for e in future if e.dedupe_key in scored)
    pct = (100.0 * have / len(future)) if future else 0.0
    p = pending(sess)
    print(f"{CITY}: {have}/{len(future)} future events scored ({pct:.0f}%) · "
          f"{sess.query(EventScore).count()} score rows total")
    print(f"pending: {p['n_units']} judgments cover {p['n_events']} events "
          f"({p['n_inherit']} inherit from a series) in {len(p['batches'])} batches")


# ──────────────────────────────── cli ──────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--pending", action="store_true",
                    help="print unscored work as JSON batches (series-first)")
    ap.add_argument("--import", dest="import_path", type=Path,
                    help="write a Rater's JSON results into event_scores (idempotent)")
    ap.add_argument("--status", action="store_true", help="scored-coverage report")
    ap.add_argument("--batch-size", type=int, default=BATCH_SIZE,
                    help=f"judgments per batch (default {BATCH_SIZE})")
    ap.add_argument("--limit", type=int, help="cap the number of judgments emitted")
    ap.add_argument("--all", action="store_true",
                    help="include past events (default: future only)")
    ap.add_argument("--rescore", action="store_true",
                    help="include events that already have a score")
    ap.add_argument("--out", type=Path, help="write --pending JSON to a file")
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help=f"value recorded in event_scores.model (default {DEFAULT_MODEL})")
    args = ap.parse_args()

    if not (args.pending or args.import_path or args.status):
        ap.print_help()
        return 1

    sess = Session()
    try:
        code = 0
        if args.pending:
            payload = pending(sess, include_past=args.all, rescore=args.rescore,
                              batch_size=args.batch_size, limit=args.limit)
            text = json.dumps(payload, indent=2, ensure_ascii=False)
            if args.out:
                args.out.parent.mkdir(parents=True, exist_ok=True)
                args.out.write_text(text)
                print(f"Wrote {args.out} — {payload['n_units']} judgments covering "
                      f"{payload['n_events']} events in {len(payload['batches'])} batches")
            else:
                print(text)
        if args.import_path:
            code = import_scores(sess, args.import_path, model=args.model) or code
        if args.status:
            status(sess)
        return code
    finally:
        sess.close()


if __name__ == "__main__":
    sys.exit(main())
