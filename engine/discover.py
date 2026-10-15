"""
Discovery + monitoring decision — two of the going-forward modular operators
(see docs/leads-pipeline.md — to be generalized into operators.py). Each is
idempotent and touches only its deltas.

  discover_from_events(raw, source_id, sess)  ingest mints/confirms a candidate
                                               venue from a scraped event; an
                                               aggregator also drops a 'discovered'
                                               coverage row (the vetting queue).
  sync_firehose_coverage(sess)                reconcile the 'discovered' coverage
                                               rows against events (add new, drop
                                               vanished); never touches judged rows.
  decide_monitored(venue_or_group, sess)      the function that OWNS the monitored
                                               flag. A venue/group is wanted if a
                                               dedicated (non-aggregator) source
                                               feeds it, OR it's verified. Firehose-
                                               only venues stay off until vetted.

source_coverage is the COMPLETE connection map (model B): one row per source→
target link. SYNC RULE — events is the source of truth; 'discovered' (firehose)
rows are a refreshable view of events; 'adopted'/judged rows are sticky.
"""
from __future__ import annotations

from models import (
    CITY, CITY_STATE, Event, Group, PipelineSource, Session, SourceCoverage, Venue,
    init_db, now, slug,
)


def discover_from_events(raw: dict, source_id: str, sess) -> Venue:
    """Resolve the venue named in a scraped event; mint it as a candidate if new.
    Idempotent: seeing a known venue again just bumps last_seen."""
    name = (raw.get("venue") or raw.get("address") or "").strip()
    # a bare city name is a listing's placeholder, not a room (playdate emits
    # venue="Philadelphia" when the organizer didn't set one; multi-city
    # 2026-08-29 — the state suffix comes from CITY_STATE, was hardcoded ", pa")
    if not name or name.lower() in (CITY.lower(), CITY_STATE.lower()):
        return None
    vid = slug(name)
    src = sess.get(PipelineSource, source_id)
    from_firehose = bool(src and src.is_aggregator)

    v = sess.get(Venue, vid)
    if v is None:
        # fuzzy identity BEFORE minting: "Bok Building" must resolve to the
        # existing `bok`, not become a duplicate candidate (the Ethical Society
        # lesson). Import here to keep dedupe->models the only import direction.
        from dedupe import find_matching_venue
        v, _reason = find_matching_venue(sess, name=name, address=raw.get("address"),
                                         lat=raw.get("lat"), lng=raw.get("lng"))
        if v is not None:
            vid = v.id
    if v is None:
        v = Venue(
            id=vid, name=name, address=raw.get("address"),
            lat=raw.get("lat"), lng=raw.get("lng"),
            hosts_events="unknown",
            status="candidate", monitored=False,          # judgment is decide_monitored's job
            discovered_via="eventbrite_firehose" if from_firehose else "scrape",
            discovered_by="ingest",
        )
        sess.add(v)
    else:
        v.last_seen = now()

    # An aggregator hit is a lead, not an adoption: queue a 'discovered' coverage row.
    if from_firehose:
        exists = (sess.query(SourceCoverage)
                  .filter_by(source_id=source_id, target_kind="venue", target_id=vid)
                  .first())
        if not exists:
            sess.add(SourceCoverage(
                source_id=source_id, target_kind="venue", target_id=vid,
                is_primary=False, adoption="discovered",
                usefulness="unknown", discovered_by="firehose"))
    sess.flush()
    return v


def sync_firehose_coverage(sess) -> tuple[int, int]:
    """Reconcile the firehose (un-adopted) coverage rows against events — events
    is the source of truth. Adds a 'discovered' row for every (aggregator source,
    target) pair observed in events; drops 'discovered' rows whose events have
    vanished. NEVER touches 'adopted'/judged rows (is_primary, notes stay sticky).
    Run after ingest. Returns (added, removed)."""
    agg_ids = {s.id for s in sess.query(PipelineSource).filter_by(is_aggregator=True)}
    observed: set[tuple] = set()
    for kind, col in (("venue", Event.venue_id), ("group", Event.group_id)):
        for sid, tid in (sess.query(Event.source_id, col)
                         .filter(Event.source_id.isnot(None), col.isnot(None)).distinct()):
            if sid in agg_ids:
                observed.add((sid, kind, tid))
    all_rows = sess.query(SourceCoverage).all()
    covered_any = {(c.source_id, c.target_kind, c.target_id) for c in all_rows}
    discovered = {(c.source_id, c.target_kind, c.target_id): c
                  for c in all_rows if c.adoption == "discovered"}
    added = removed = 0
    # add a 'discovered' row only where NO coverage row of any kind exists yet
    # (a pair already 'adopted' stays adopted — never duplicate it)
    for sid, kind, tid in observed - covered_any:
        sess.add(SourceCoverage(source_id=sid, target_kind=kind, target_id=tid,
                                is_primary=False, adoption="discovered",
                                usefulness="unknown", discovered_by="firehose"))
        added += 1
    # drop 'discovered' rows whose events vanished (judged rows untouched)
    for key, c in discovered.items():
        if key not in observed:
            sess.delete(c)
            removed += 1
    sess.flush()
    return added, removed


def decide_monitored(t: Venue | Group, sess) -> bool:
    """Set & return target.monitored (Venue OR Group). Wanted iff a dedicated
    (non-aggregator) source ACTUALLY FEEDS it (status='built' AND health='ok'),
    or it's verified.

    'Feeds' used to count ANY dedicated coverage row, even to a status='none'
    source — which left 34/45 venues monitored with nothing feeding them and,
    worse, HIDDEN from leads_inbox (it lists monitored=0). The funnel thought
    they were handled. (2026-06-09 audit, finding 6 — the DRIFTED flag lesson.)
    health='ok' is required too: a built-but-failing source is wired but feeds
    nothing — that venue still needs work, so it belongs in the inbox (caught by
    tests.py::test_gate_and_build_source on the suite's first run).

    The group arm (2026-08-17, same rule) closes the ppl-walk gap: group events
    are invisible until group.monitored, and nothing wrote that flag."""
    kind = "venue" if isinstance(t, Venue) else "group"
    has_dedicated = False
    for r in (sess.query(SourceCoverage)
              .filter_by(target_kind=kind, target_id=t.id).all()):
        s = sess.get(PipelineSource, r.source_id)
        if s and not s.is_aggregator and s.status == "built" and s.health == "ok":
            has_dedicated = True
            break
    t.monitored = bool(t.status == "verified" or has_dedicated)
    sess.flush()
    return t.monitored


if __name__ == "__main__":
    print(__doc__)
