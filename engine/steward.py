"""
Steward — the maintenance pass (the org function that KEEPS things true).

Charters exist to create (Tej), triage (Scout), build (Max), judge (Rater) —
nothing existed to notice rot. Chicago's 2026-06-09 audit found 34/45 monitored
venues with no built source ("DRIFTED": a flag with a writer that never re-runs)
and an adapter ecosystem that had gone months without a re-probe. This job is the
fix — and Philly inherits ~40 v1 sources that are three months stale, so it earns
its keep here on day one.

  .venv/bin/python steward.py               # full report: probe drift + monitored drift + routing gaps (writes NOTHING)
  .venv/bin/python steward.py --skip-probe  # flags-only audit, no network
  .venv/bin/python steward.py --fix         # apply: re-stamp drifted health (re-gates), re-derive monitored

Report-only by default — the Steward proposes, --fix applies, and every apply
goes through operators (mark_source_built re-gate / decide_monitored), never raw
writes. The Steward NEVER builds a new source (status none/planned stays Max's).
"""
from __future__ import annotations

import argparse
import datetime as dt

from models import Event, EventScore, Group, PipelineSource, Session, SourceCoverage, Venue
from operators import decide_monitored, mark_source_built, probe_source_live


def probe_drift(sess, fix: bool) -> int:
    """Re-run the build gate on every BUILT source; report health drift."""
    built = (sess.query(PipelineSource).filter_by(status="built")
             .order_by(PipelineSource.id).all())
    print(f"── probe pass: {len(built)} built sources ──")
    drifted = 0
    for s in built:
        health, detail = probe_source_live(s.adapter, s.url, s.name)
        mark = "  " if health == s.health else "▲ "
        if health != s.health:
            drifted += 1
            print(f"{mark}{s.id:42s} {s.health or '?':12s} -> {health:12s} {detail[:60]}")
            if fix:
                mark_source_built(s.id, sess=sess)  # re-gate + re-stamp honestly
        else:
            print(f"{mark}{s.id:42s} {health:12s} (stable)")
    print(f"{drifted} drifted{' — re-stamped' if fix and drifted else ''}\n")
    return drifted


def monitored_drift(sess, fix: bool) -> int:
    """Compare every venue's AND group's monitored flag to what decide_monitored
    would say (groups gained the flag's writer 2026-08-17 — the ppl-walk gap)."""
    # Must mirror decide_monitored EXACTLY (discover.py): built AND health='ok'.
    # Omitting the health check made this report claim drift that --fix could never
    # clear — a built-but-no_events source (nextfab, 2026-08-16) looked like a
    # missing flag forever while the operator correctly refused to set it. A drift
    # line the fix can't close is noise, and noise is how a maintenance pass dies.
    print("── monitored pass ──")
    drifted = 0
    for model, kind in ((Venue, "venue"), (Group, "group")):
        built_dedicated = {
            c.target_id
            for c in sess.query(SourceCoverage).filter_by(target_kind=kind)
            if (src := sess.get(PipelineSource, c.source_id))
            and not src.is_aggregator and src.status == "built" and src.health == "ok"
        }
        for t in sess.query(model).order_by(model.id).all():
            expected = bool(t.status == "verified" or t.id in built_dedicated)
            if t.monitored != expected:
                drifted += 1
                print(f"▲ {kind[0]}:{t.id:40s} monitored {t.monitored} -> {expected}")
                if fix:
                    decide_monitored(t, sess)
    inbox = (sess.query(Venue).filter_by(monitored=False).count()
             + sess.query(Group).filter_by(monitored=False).count())
    print(f"{drifted} drifted{' — re-derived' if fix and drifted else ''}; "
          f"{inbox} venues+groups now in leads_inbox\n")
    return drifted


def routing_gap(sess) -> int:
    """Flag built aggregators the ingest loop silently skips (later.md P1).

    is_aggregator picks which ingest pass runs and the passes are exclusive:
    the aggregator pass routes ONLY via venue coverage rows that carry a
    routing_key (ingest.py). A built aggregator with none — group-only
    coverage, or venue rows with no key — matches neither pass: ingest prints
    0 and continues BEFORE stamping last_scraped, while probe_source_live
    calls the adapter directly with no routing, so the row reads built/ok
    forever while yielding nothing (campus-philly, penn-today, bowerbird all
    froze at 2026-08-16 exactly this way). No --fix here on purpose: flipping
    the flag vs minting routing_keys vs scheduling --discover is a per-source
    judgment (Max), not a derivation the Steward can apply.
    """
    print("── routing pass ──")
    gapped = 0
    aggs = (sess.query(PipelineSource)
            .filter_by(status="built", is_aggregator=True)
            .order_by(PipelineSource.id).all())
    for s in aggs:
        covs = sess.query(SourceCoverage).filter_by(source_id=s.id).all()
        if any(c.target_kind == "venue" and c.routing_key for c in covs):
            continue
        gapped += 1
        kinds = "/".join(sorted({c.target_kind for c in covs})) or "none"
        last = s.last_scraped.date() if s.last_scraped else "never"
        print(f"▲ {s.id:42s} no routable venue coverage ({len(covs)} rows: {kinds}) "
              f"— last pull {last}; flip flag, set routing_keys, or --discover (Max)")
    print(f"{gapped} built aggregators silently skipped by ingest\n")
    return gapped


def summary(sess):
    today0 = dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    scored = {k for (k,) in sess.query(EventScore.dedupe_key).distinct()}
    unscored = sum(1 for e in sess.query(Event).filter(Event.start >= today0)
                   if e.dedupe_key not in scored)
    by_health: dict = {}
    for s in sess.query(PipelineSource).filter_by(status="built"):
        by_health[s.health] = by_health.get(s.health, 0) + 1
    print(f"── summary ── built sources by health: {by_health} · "
          f"{unscored} future events unscored (Rater handoff)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix", action="store_true", help="apply via operators (default: report only)")
    ap.add_argument("--skip-probe", action="store_true", help="no network — flags audit only")
    args = ap.parse_args()

    sess = Session()
    if not args.skip_probe:
        probe_drift(sess, args.fix)
    monitored_drift(sess, args.fix)
    routing_gap(sess)
    if args.fix:
        sess.commit()
    summary(sess)
    sess.close()
