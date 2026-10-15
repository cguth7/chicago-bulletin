"""
Ingest — run every BUILT, dedicated (non-aggregator) PipelineSource against the
venue it covers, normalize, and upsert Event rows into the DB. Idempotent on
dedupe_key (re-runs bump last_seen, never duplicate).

Event visibility is DERIVED (models.is_visible): an event shows iff its source is
built AND its venue is monitored. Both hold for everything we ingest here.

  .venv/bin/python ingest.py            # today .. +7 days (the near-term week)
  .venv/bin/python ingest.py --days 30
  .venv/bin/python ingest.py --source <id> --days 7    # verify ONE source, seconds
  .venv/bin/python ingest.py --stale 12                # only sources not pulled in 12h
"""
from __future__ import annotations

import argparse
import datetime as dt

from costs import parse_cost
from models import (
    DB_PATH, Event, EventScore, Group, PipelineSource, Session, SourceCoverage,
    Venue, VenueAlias, is_visible, now, slug,
)
from scrapers import ADAPTERS
from series import derive_series

# Aggregators fan out over a whole city; ingest must pull deep enough to reach all
# our covered targets (the focus park universe is ~400 events). Bounded, not infinite.
AGG_INGEST_CAP = 1500


def dedupe_key(source_id: str, target_id: str, date: str, name: str) -> str:
    """Stable identity for one source's observation of an event instance.
    target_id is the venue id, or 'group:<id>' for a group-fed observation."""
    return f"{source_id}|{target_id}|{date}|{slug(name)}"


def legacy_dedupe_key(venue_id: str, date: str, name: str) -> str:
    """Pre-observation key, retained so existing rows update in place."""
    return f"{venue_id}|{date}|{slug(name)}"


def _parse_dt(date: str, time_: str):
    if not date:
        return None
    try:
        if time_:
            return dt.datetime.fromisoformat(f"{date}T{time_}")
        return dt.datetime.fromisoformat(date)
    except Exception:
        return None


def _upsert_event(sess, raw, venue, source, group=None, touched=None):
    """Insert/coalesce raw event for `venue` — or for `group` when the source
    feeds a group with no fixed room (venue_id stays NULL, the event roams).
    Returns 'new'|'seen'|None. `touched` (a set) collects the DB row each raw
    event landed on, so the caller can retire rows the pull did NOT confirm."""
    if not raw.get("name") or not raw.get("date"):
        return None
    # A name that IS a URL is upstream junk (the-heart-of-jazz once published a
    # post titled with its own image URL). No legitimate event name starts with
    # a scheme, so drop the row rather than minting a verdict-needing dupe.
    if raw["name"].lstrip().lower().startswith(("http://", "https://")):
        return None
    target = venue.id if venue is not None else f"group:{group.id}"
    dk = dedupe_key(source.id, target, raw["date"], raw["name"])
    old_dk = legacy_dedupe_key(target, raw["date"], raw["name"])
    sdt = _parse_dt(raw.get("date"), raw.get("time"))
    edt = _parse_dt(raw.get("end_date"), raw.get("end_time"))
    lifecycle = "active" if (sdt and sdt.date() >= dt.date.today()) else "past"
    lo, hi, free = parse_cost(raw.get("cost"))
    candidate = Event(
        dedupe_key=dk, name=raw["name"], start=sdt, end=edt,
        all_day=raw.get("all_day", False),
        venue_id=venue.id if venue is not None else None,
        group_id=group.id if group is not None else None,
        source_id=source.id,
        category=raw.get("category") or None, tags=raw.get("tags") or None,
        organizer=(raw.get("organizer") or "").strip() or None,
        cost=raw.get("cost") or None,
        cost_min_cents=lo, cost_max_cents=hi, is_free=free,
        description=raw.get("description") or None,
        image=raw.get("image") or None, url=raw.get("url") or None,
        discovered_via="api", lifecycle=lifecycle,
    )
    row = (sess.query(Event)
           .filter(Event.source_id == source.id,
                   Event.dedupe_key.in_([dk, old_dk])).first())
    if row:
        from dedupe import apply_suppression, merge_event_values
        candidate.first_seen = row.first_seen
        candidate.last_seen = now()
        merge_event_values(sess, row, candidate)
        apply_suppression(sess, row)
        if touched is not None:
            touched.add(row)
        return "seen"
    # Fuzzy identity is deliberately downstream of the exact key and scoped to
    # this source. A different source is evidence, not a row to overwrite; export
    # later chooses one public representative without mutating either observation.
    from dedupe import apply_suppression, find_matching_event, merge_event_values
    row = find_matching_event(sess, candidate, source_id=source.id)
    if row is not None:
        candidate.first_seen = row.first_seen
        candidate.last_seen = now()
        merge_event_values(sess, row, candidate)
        apply_suppression(sess, row)
        if touched is not None:
            touched.add(row)
        return "seen"
    # A standing suppress_series rule (the JazzAttack leak, 2026-09-11) catches
    # a brand-new row the moment it lands — per-row suppress_event can't, because
    # the row didn't exist yet when the human said "this listing is wrong".
    apply_suppression(sess, candidate)
    sess.add(candidate)
    if touched is not None:
        touched.add(candidate)
    return "new"


def _retire_vanished(sess, source, touched, window_end, venue_scope=None):
    """Window-aware ghost retirement (2026-08-19, the Rabbit case): a FUTURE
    event of this source whose start lies inside the window the pull just
    covered, and which the pull did NOT confirm, has vanished from its own feed
    (renamed / cancelled / deleted upstream) -> lifecycle='unlisted'. Kept and
    reversible — a re-seen row's upsert copies lifecycle back to 'active'.

    The window check is the whole trick: a --days N pull says nothing about
    rows beyond its horizon, so those are never touched. Only future starts are
    considered (feeds legally drop events that already happened today).
    `venue_scope` limits an aggregator to the venues it could actually route
    this run — a previously-discovered venue's rows aren't ghosts just because
    this pull ran without --discover. Callers only invoke this off a pull that
    confirmed >=1 event."""
    q = (sess.query(Event)
         .filter(Event.source_id == source.id,
                 Event.lifecycle == "active",
                 Event.start.isnot(None),
                 Event.start >= now(),
                 Event.start < dt.datetime.combine(window_end, dt.time())))
    if venue_scope is not None:
        q = q.filter(Event.venue_id.in_(venue_scope))
    retired = 0
    for row in q.all():
        if row not in touched:
            row.lifecycle = "unlisted"
            retired += 1
    return retired


def _organizer_group(sess, source):
    """The group a source's events belong to, if it has group coverage
    (primary first). Read for BOTH ingest passes: a dedicated source can feed a
    venue and be run by a group (NKCDC at the Engagement Center), and an
    aggregator can be one organizer fanning out over many rooms (cinéSPEAK,
    Penn Today). Until 2026-09-06 venue coverage silently won and the group
    was never stamped — 27 group-attributed events out of ~800."""
    gcov = (sess.query(SourceCoverage)
            .filter_by(source_id=source.id, target_kind="group")
            .order_by(SourceCoverage.is_primary.desc()).first())
    return sess.get(Group, gcov.target_id) if gcov else None


def _dedicated_target(sess, source):
    """The venue and/or group a dedicated source feeds (primary coverage
    first). Returns (venue, group): both set when the source is a group's
    calendar at a fixed room, venue-only for a plain venue calendar, group-only
    for a roaming group (ppl-walk — used to be silently skipped, 2026-08-16),
    or (None, None) when nothing is connected."""
    cov = (sess.query(SourceCoverage)
           .filter_by(source_id=source.id, target_kind="venue")
           .order_by(SourceCoverage.is_primary.desc()).first())
    venue = sess.get(Venue, cov.target_id) if cov else None
    return venue, _organizer_group(sess, source)


def _routing_map(sess, source) -> dict:
    """routing_key -> Venue for an aggregator's venue coverage. A merged spelling
    of the same room (venue_aliases) routes to the survivor too — Eventbrite keeps
    one venue record per spelling ("Lardner's Point Park" / "Lardners Point Park",
    2026-09-09) and a coverage row carries only one key. An explicit key wins
    over an alias."""
    out = {}
    covs = (sess.query(SourceCoverage)
            .filter_by(source_id=source.id, target_kind="venue").all())
    for c in covs:
        v = sess.get(Venue, c.target_id)
        if v and c.routing_key:
            out[c.routing_key] = v
    for c in covs:
        v = sess.get(Venue, c.target_id)
        if v and c.routing_key:
            for a in sess.query(VenueAlias).filter_by(venue_id=v.id).all():
                out.setdefault(a.alias_id, v)
    return out


def _park_slug(raw):
    """An aggregator event tags its target as 'park:<slug>'. Pull the slug, else ''."""
    for tag in (raw.get("tags") or "").split(","):
        tag = tag.strip()
        if tag.startswith("park:"):
            return tag[len("park:"):]
    return ""


def ingest(days: int = 7, only: str | None = None, stale_hours: float | None = None,
           discover: bool = False):
    sess = Session()
    start = dt.date.today()
    end = start + dt.timedelta(days=days)
    # --stale N: only scrape sources whose last successful pull is >N hours old
    # (or never pulled). Makes a recurring loop cheap — fresh sources are skipped,
    # and a run killed mid-way resumes where it left off (per-source commits).
    stale_cutoff = (now() - dt.timedelta(hours=stale_hours)) if stale_hours is not None else None

    def is_fresh(s):
        return stale_cutoff is not None and s.last_scraped is not None and s.last_scraped > stale_cutoff

    q = (sess.query(PipelineSource)
         .filter(PipelineSource.status == "built",
                 PipelineSource.is_aggregator == False))  # noqa: E712
    if only:
        # verify ONE source without paying for an all-source run (the parks
        # aggregator alone is ~10 min) — 2026-06-09 audit finding
        q = q.filter(PipelineSource.id == only)
    built = q.order_by(PipelineSource.id).all()
    skipped_fresh = [s for s in built if is_fresh(s)]
    built = [s for s in built if not is_fresh(s)]
    if only and not built:
        print(f"note: '{only}' is not a built dedicated source (aggregator? not built?)")

    total_new = total_seen = total_retired = 0
    print(f"Ingesting {len(built)} built dedicated sources, {start} .. {end}"
          + (f"  ({len(skipped_fresh)} skipped, scraped <{stale_hours}h ago)"
             if stale_cutoff is not None else "") + "\n")

    for s in built:
        fn = ADAPTERS.get(s.adapter)
        if not fn:
            continue
        # the venue OR group this source feeds (primary coverage if present, else any)
        venue, group = _dedicated_target(sess, s)
        if venue is None and group is None:
            continue
        # scrape the SAME url the build gate validated (s.url). coverage.url is the
        # target's page on that source (models.py), not the scrape endpoint — they
        # diverge whenever Scout corrects the endpoint post-connect (Empty Bottle:
        # gate probed the TM API while cov.url still held the homepage shell).
        url = s.url
        try:
            events = fn(url, s.name, start, end)
        except Exception as e:
            print(f"  [{s.name[:34]:34s}] ERROR {type(e).__name__}: {e}")
            continue

        new = seen = 0
        touched: set = set()
        for raw in events:
            r = _upsert_event(sess, raw, venue, s, group=group, touched=touched)
            if r == "new":
                new += 1
            elif r == "seen":
                seen += 1
        # retire only off a pull that confirmed >=1 event: adapters swallow HTTP
        # errors and return [] (and shape rot yields all-invalid raws), and a
        # broken pull must not unlist a whole calendar
        retired = _retire_vanished(sess, s, touched, end) if (new or seen) else 0
        total_new += new
        total_seen += seen
        total_retired += retired
        s.last_scraped = now()
        # commit per source: a 10-min run killed mid-way keeps finished sources
        # (one end-of-run commit used to roll back EVERYTHING on interrupt)
        sess.commit()
        flag = "" if (new or seen) else "  (0 — adapter/url mismatch?)"
        gone = f", {retired} unlisted" if retired else ""
        print(f"  [{s.adapter:11s}] {s.name[:34]:34s} -> {new:3d} new, {seen:3d} seen{gone}{flag}")

    # --- aggregator pass: one source fans out to its many covered venues ---
    # A dedicated source dumps all events on one venue; an aggregator instead
    # ROUTES each event to the covered venue named in that event (park:<slug> tag,
    # matched to a coverage row's venue). Events for parks we don't cover are dropped.
    aq = (sess.query(PipelineSource)
          .filter(PipelineSource.status == "built",
                  PipelineSource.is_aggregator == True))  # noqa: E712
    if only:
        aq = aq.filter(PipelineSource.id == only)
    aggs = [s for s in aq.order_by(PipelineSource.id).all() if not is_fresh(s)]
    if aggs:
        print()
    for s in aggs:
        fn = ADAPTERS.get(s.adapter)
        if not fn:
            continue
        # map every routing key this source covers -> its venue. The aggregator's
        # source-specific join key (the park slug) lives in coverage.routing_key,
        # set when the coverage was connected — routing never name-matches.
        slug_to_venue = _routing_map(sess, s)
        if not slug_to_venue and not discover:
            print(f"  [{s.adapter:11s}] {s.name[:34]:34s} -> 0 (no covered venues w/ park_slug)")
            continue
        # an organizer-run aggregator (one group, many rooms) stamps its group
        # on every routed event; routing itself is still venue-only
        agg_group = _organizer_group(sess, s)
        # An aggregator must pull deep enough to reach ALL its covered targets,
        # not just the first page. We still cap (bounded, never unbounded) but at a
        # ceiling comfortably above the focus universe. Adapters that don't accept
        # max_events fall back to their own default.
        try:
            try:
                events = fn(s.url, s.name, start, end, max_events=AGG_INGEST_CAP)
            except TypeError:
                events = fn(s.url, s.name, start, end)
        except Exception as e:
            print(f"  [{s.name[:34]:34s}] ERROR {type(e).__name__}: {e}")
            continue
        new = seen = routed = minted = 0
        touched = set()
        for raw in events:
            venue = slug_to_venue.get(_park_slug(raw))
            if not venue:
                if not discover:
                    continue  # event for a venue we don't cover — drop
                # --discover: the firehose path (discover.py, the going-forward
                # design). Mint/resolve a candidate venue + a 'discovered'
                # coverage row; the event stores against it but stays INVISIBLE
                # (is_visible needs monitored=True) until Tej vets the venue.
                from discover import discover_from_events
                venue = discover_from_events(raw, s.id, sess)
                if venue is None:
                    continue  # no venue name at all, or a bare-city placeholder
                minted += 1
            else:
                routed += 1
            r = _upsert_event(sess, raw, venue, s, group=agg_group, touched=touched)
            if r == "new":
                new += 1
            elif r == "seen":
                seen += 1
        # without --discover only routable venues were upserted, so only their
        # rows can honestly be called vanished; with --discover every named
        # venue's events were confirmed or not, so the whole source is in scope
        scope = None if discover else set(v.id for v in slug_to_venue.values())
        retired = (_retire_vanished(sess, s, touched, end, venue_scope=scope)
                   if (new or seen) else 0)
        total_new += new
        total_seen += seen
        total_retired += retired
        s.last_scraped = now()
        sess.commit()
        mint_note = f", {minted} to discovered venues" if minted else ""
        gone = f", {retired} unlisted" if retired else ""
        print(f"  [{s.adapter:11s}] {s.name[:34]:34s} -> {new:3d} new, {seen:3d} seen{gone} "
              f"({routed} routed to {len(slug_to_venue)} covered parks{mint_note})")

    sess.commit()

    if discover:
        # reconcile the 'discovered' coverage queue against events (adds new pairs,
        # drops vanished ones; never touches adopted/judged rows)
        from discover import sync_firehose_coverage
        d_add, d_rm = sync_firehose_coverage(sess)
        sess.commit()
        print(f"\ndiscover: coverage queue {d_add:+d}/-{d_rm} — vet candidates via "
              f"Tej (verify) + decide_monitored")

    # re-derive series clusters now that the events table moved (pure date math)
    n_series, stamped = derive_series(sess)
    sess.commit()
    print(f"\nseries: {n_series} clusters, {stamped} events (re)stamped")

    # visibility summary
    vis = 0
    for e in sess.query(Event).all():
        v = sess.get(Venue, e.venue_id) if e.venue_id else None
        g = sess.get(Group, e.group_id) if e.group_id else None
        src = sess.get(PipelineSource, e.source_id) if e.source_id else None
        if is_visible(e, v, g, src):
            vis += 1
    n_events = sess.query(Event).count()

    # the dupe-judge handoff: the deterministic detector runs every ingest; an
    # agentic judge only ever sees NEW pairs (persisted verdicts never re-surface)
    from dedupe import dupe_suspects, visible_window_events
    suspects = dupe_suspects(sess, visible_window_events(sess, days))
    # the Rater handoff: scoring the delta is a standing decision, not a memory
    # burden — every ingest ends by saying what scoring now costs (paid LLM call,
    # so it's announced, never auto-run).
    scored_keys = {k for (k,) in sess.query(EventScore.dedupe_key).distinct()}
    today0 = dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    unscored = sum(1 for e in sess.query(Event).filter(Event.start >= today0)
                   if e.dedupe_key not in scored_keys)
    print(f"\nDone. {total_new} new, {total_seen} seen, {total_retired} retired to "
          f"unlisted. {DB_PATH.name} now has {n_events} events ({vis} visible).")
    if unscored:
        print(f"RATER HANDOFF: {unscored} future events unscored — "
              f"run `scorer.py --pending` to get the judgment batches.")
    if suspects:
        print(f"DUPE JUDGE HANDOFF: {len(suspects)} unjudged suspect pair(s) — "
              f"run `dedupe.py --suspects` and import verdicts.")
    sess.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7,
                    help="event window, today .. +days (default 7 — the near-term week)")
    ap.add_argument("--source", type=str, default=None,
                    help="ingest ONE source by id (verify a new build cheaply)")
    ap.add_argument("--stale", type=float, default=None, metavar="HOURS",
                    help="only scrape sources last pulled >HOURS ago (loop mode)")
    ap.add_argument("--discover", action="store_true",
                    help="mint candidate venues (+ 'discovered' coverage) from "
                         "unrouted aggregator events instead of dropping them; "
                         "their events stay invisible until the venue is vetted")
    args = ap.parse_args()
    ingest(days=args.days, only=args.source, stale_hours=args.stale,
           discover=args.discover)
