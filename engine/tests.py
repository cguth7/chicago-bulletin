"""
Engine test suite — zero deps, no network, never touches the real DB.

  .venv/bin/python tests.py

Mechanics: rebinds models.Session to an in-memory SQLite BEFORE exercising
operators (they share the sessionmaker object), and registers stub adapters in
scrapers.ADAPTERS so the build gate runs for real without HTTP. Every contract
that bit us in the 2026-06-09 Chicago e2e audit has a test here so it can't
regress silently, plus the two bugs fixed during the 2026-08-16 Philly port
(CSS in descriptions, un-truncated descriptions) and the scorer's new
--pending/--import plumbing.
"""
from __future__ import annotations

import datetime as dt
import json as _json_mod

from sqlalchemy import create_engine

import models
from models import (Base, Event, Group, PipelineSource, Session, SourceCoverage,
                    Venue, VenueAlias, is_visible, slug)

# ── harness: isolated DB + stub adapters ────────────────────────────────────

test_engine = create_engine("sqlite://")  # in-memory
Base.metadata.create_all(test_engine)
Session.configure(bind=test_engine)       # operators' Session() now hits memory

import scrapers  # noqa: E402

# The adapter fixtures below are real Philadelphia feeds (where this engine was
# first built), so their wall-clock and city-filter assertions are Eastern /
# Philadelphia. Pin the scrapers to the fixtures' zone + city so the suite tests
# ADAPTER LOGIC regardless of what city.json says.
from zoneinfo import ZoneInfo  # noqa: E402
for _m in (scrapers, scrapers.core, scrapers.jsonld, scrapers.feeds, scrapers.ticketing):
    _m.LOCAL_TZ = ZoneInfo("America/New_York")
scrapers.core.HOME_CITY = "Philadelphia"

TOMORROW = (dt.date.today() + dt.timedelta(days=1)).isoformat()


def stub_ok(url, source_name, start, end, **kw):
    return [scrapers.norm(name="Stub Event", date=TOMORROW, time_="19:00",
                          venue="Stub Venue", source=source_name, url=url)]


def stub_empty(url, source_name, start, end, **kw):
    return []


scrapers.ADAPTERS["stub_ok"] = stub_ok
scrapers.ADAPTERS["stub_empty"] = stub_empty

from operators import (  # noqa: E402  (import AFTER rebind is not required, but explicit)
    add_group_lead, add_source_lead, add_venue_lead, build_source, connect_coverage,
    decide_monitored, mark_source_built, merge_venues, probe_source_live, set_source_status,
    update_group_fields, update_source_fields, update_venue_fields,
)

PASS = 0


def ok(cond, label):
    global PASS
    assert cond, label
    PASS += 1


# ── operators: leads ────────────────────────────────────────────────────────

def test_venue_lead_lifecycle():
    s = Session()
    v = add_venue_lead("Test Tavern", kind="bar", neighborhood="Logan Square",
                       discovered_via="manual", discovered_by="tests", sess=s)
    ok(v.id == slug("Test Tavern") and v.status == "candidate" and not v.monitored,
       "venue lead born candidate+unmonitored")
    v2 = add_venue_lead("Test Tavern", kind="theater", address="123 Fake St", sess=s)
    ok(v2.kind == "bar" and v2.address is None,
       "re-touch fills NOTHING (create-only semantics)")
    update_venue_fields(v.id, address="123 Real St", kind="theater", sess=s)
    ok(v.address == "123 Real St" and v.kind == "theater",
       "update_venue_fields overwrites deliberately")
    old_id = v.id
    update_venue_fields(v.id, name="Test Public House", sess=s)
    ok(v.name == "Test Public House" and v.id == old_id,
       "display-name rename keeps the slug (id never changes)")
    s.rollback()


def test_update_group_fields():
    s = Session()
    g = add_group_lead("Test Run Club", kind="club", website="https://run.example",
                       discovered_via="manual", discovered_by="tests", sess=s)
    update_group_fields(g.id, description="Saturday 5Ks, all paces",
                        join_url="https://meetup.example/run", sess=s)
    ok(g.description == "Saturday 5Ks, all paces" and g.join_url == "https://meetup.example/run"
       and g.website == "https://run.example" and g.status == "candidate" and not g.monitored,
       "update_group_fields patches description/join_url, leaves lifecycle alone")
    try:
        update_group_fields("no-such-group", description="x", sess=s)
        ok(False, "unknown group raises")
    except KeyError:
        ok(True, "unknown group raises")
    s.rollback()


def test_venue_identity_and_aliases():
    s = Session()
    canonical = add_venue_lead("Ethical Society of Philadelphia",
                               address="1906 Rittenhouse Square", sess=s)
    alternate = add_venue_lead("Philadelphia Ethical Society",
                               address="1906 Rittenhouse Sq.", sess=s)
    ok(alternate.id == canonical.id, "reordered name + normalized address resolves venue")
    alias = s.get(VenueAlias, "philadelphia-ethical-society")
    ok(alias is not None and alias.venue_id == canonical.id, "alternate slug retained as alias")
    again = add_venue_lead("Philadelphia Ethical Society", sess=s)
    ok(again.id == canonical.id, "later sparse rediscovery follows durable alias")

    studio = add_venue_lead("Studio 34 Yoga Healing Arts",
                            address="4522 Baltimore Ave, Philadelphia, PA 19143", sess=s)
    short = add_venue_lead("Studio 34", address="4522 Baltimore Ave", sess=s)
    ok(short.id == studio.id, "contained venue name + same address resolves venue")

    room = add_venue_lead("Rittenhouse Concert Room",
                          address="1906 Rittenhouse Square", sess=s)
    ok(room.id != canonical.id, "shared building without similar name stays distinct")

    # Same-brand siblings on one website (library branches) must not auto-merge:
    # both names carry a distinguishing word, so the shared site is not identity.
    b1 = add_venue_lead("O'Connell Branch Library",
                        website="https://library.example.org", sess=s)
    b2 = add_venue_lead("O'Neill Branch Library",
                        website="https://library.example.org", sess=s)
    ok(b2.id != b1.id, "sibling branches sharing a website stay distinct")
    annex = add_venue_lead("The O'Connell Branch Library Annex Room",
                           website="https://library.example.org", sess=s)
    ok(annex.id == b1.id, "subset-name + same website still resolves venue")
    s.rollback()


def test_explicit_venue_merge_repoints_dependents():
    s = Session()
    keep = add_venue_lead("Merge Hall", address="10 Broad St", sess=s)
    drop = add_venue_lead("Old Hall Name", address="10 Broad St", sess=s)
    src = add_source_lead("Merge Feed", adapter="stub_ok", url="https://merge", sess=s)
    other = add_source_lead("Other Merge Feed", adapter="stub_ok", url="https://other", sess=s)
    connect_coverage(src.id, "venue", drop.id, is_primary=True, sess=s)
    when = dt.datetime.now().replace(hour=19, minute=0, second=0, microsecond=0)
    s.add(Event(dedupe_key="merge-old", name="Merge Night", start=when,
                venue_id=drop.id, source_id=src.id))
    s.add(Event(dedupe_key="merge-keep", name="SOLD OUT: Merge Night", start=when,
                venue_id=keep.id, source_id=src.id))
    s.add(Event(dedupe_key="merge-other", name="Merge Night", start=when,
                venue_id=drop.id, source_id=other.id))
    s.flush()

    merged, counts = merge_venues(keep.id, drop.id, reason="test-reviewed", sess=s)
    ok(merged.id == keep.id and s.get(Venue, drop.id) is None, "explicit merge keeps survivor")
    ok(s.query(Event).filter_by(venue_id=keep.id).count() == 2
       and counts["events"] == 2 and counts["event_duplicates"] == 1,
       "venue merge coalesces same-source collision but retains other source")
    ok({e.source_id for e in s.query(Event).filter_by(venue_id=keep.id)} == {src.id, other.id},
       "venue merge preserves distinct source observations")
    ok(s.query(SourceCoverage).filter_by(target_kind="venue", target_id=keep.id).count() == 1,
       "venue merge repoints coverage")
    ok(s.get(VenueAlias, drop.id).venue_id == keep.id, "venue merge tombstones old slug as alias")
    again, repeat = merge_venues(keep.id, drop.id, sess=s)
    ok(again.id == keep.id and sum(repeat.values()) == 0, "venue merge is idempotent")
    s.rollback()


def test_source_lead_status_derivation():
    s = Session()
    ok(add_source_lead("S known", adapter="stub_ok", sess=s).status == "planned",
       "explicit KNOWN adapter -> planned (audit bug 3)")
    ok(add_source_lead("S unknown", adapter="not_an_adapter", sess=s).status == "none",
       "explicit unknown adapter -> none")
    ok(add_source_lead("S guess", platform_guess="squarespace", sess=s).status == "planned",
       "PLATFORM_MAP 'built' capped to planned")
    ok(add_source_lead("S forced", adapter="stub_ok", status="built", sess=s).status == "planned",
       "status='built' can never be minted")
    src = add_source_lead("S prov", adapter="stub_ok", discovered_via="aggregator",
                          discovered_by="tej", sess=s)
    ok((src.discovered_via, src.discovered_by) == ("aggregator", "tej"),
       "source provenance lands on create")
    s.rollback()


def test_update_source_fields_guard():
    s = Session()
    src = add_source_lead("S patch", adapter="stub_ok", url="https://a", sess=s)
    update_source_fields(src.id, api_url="https://api", notes="n", sess=s)
    ok(src.api_url == "https://api", "update_source_fields patches attrs")
    mark_source_built(src.id, sess=s)
    try:
        update_source_fields(src.id, url="https://b", sess=s)
        ok(False, "built-guard must raise")
    except ValueError:
        ok(True, "built source refuses adapter/url patch (would falsify build)")
    s.rollback()


# ── the build gate ──────────────────────────────────────────────────────────

def test_gate_and_build_source():
    s = Session()
    v = add_venue_lead("Gate Venue", discovered_via="manual", sess=s)
    src = add_source_lead("Gate Source", adapter="stub_ok", url="https://gate", sess=s)
    ok(probe_source_live("stub_ok", "https://gate", "x")[0] == "ok", "gate: ok on valid event")
    ok(probe_source_live("stub_empty", "https://gate", "x")[0] == "no_events", "gate: no_events on empty")

    bs, cov, bv = build_source(src.id, "venue", v.id, sess=s)
    ok(bs.status == "built" and bs.health == "ok", "build_source flips built+ok")
    ok(bv.status == "verified" and bv.monitored, "working source => venue verified+monitored")
    ok(cov.is_primary, "build_source coverage is primary")

    # failing gate: built-but-failing stays VISIBLE as such, target NOT verified
    v2 = add_venue_lead("Dormant Venue", discovered_via="manual", sess=s)
    src2 = add_source_lead("Dormant Source", adapter="stub_empty", url="https://d", sess=s)
    bs2, _, bv2 = build_source(src2.id, "venue", v2.id, sess=s)
    ok(bs2.status == "built" and bs2.health == "no_events", "failing gate stamped honestly")
    ok(bv2.status == "candidate" and not bv2.monitored, "no proof => not verified, not monitored")
    s.rollback()


def test_decide_monitored_requires_built():
    s = Session()
    v = add_venue_lead("Mon Venue", discovered_via="manual", sess=s)
    src = add_source_lead("Mon Source", adapter="stub_ok", url="https://m", sess=s)
    connect_coverage(src.id, "venue", v.id, sess=s)
    ok(decide_monitored(v, s) is False,
       "coverage to a PLANNED source does not monitor (audit finding 6)")
    mark_source_built(src.id, sess=s)
    ok(decide_monitored(v, s) is True, "built dedicated source monitors")
    set_source_status(src.id, "none", notes="rotted", sess=s)
    ok(decide_monitored(v, s) is False, "honest downgrade un-monitors on re-derive")
    s.rollback()


def test_decide_monitored_group_arm():
    """The ppl-walk gap (2026-08-16): nothing wrote group.monitored, so group
    events could never become visible. Groups now follow the venue rule."""
    s = Session()
    g = add_group_lead("Walk Group", discovered_via="manual", sess=s)
    src = add_source_lead("Walk Source", adapter="stub_ok", url="https://w", sess=s)
    connect_coverage(src.id, "group", g.id, sess=s)
    ok(decide_monitored(g, s) is False,
       "coverage to a PLANNED source does not monitor the group")
    mark_source_built(src.id, sess=s)
    ok(decide_monitored(g, s) is True, "built+ok dedicated source monitors the group")
    set_source_status(src.id, "none", notes="rotted", sess=s)
    ok(decide_monitored(g, s) is False, "honest downgrade un-monitors the group")
    s.rollback()


def test_group_only_ingest_and_visibility():
    """A group-fed source (ppl-walk: events roam, no fixed room) used to be
    silently skipped by ingest's venue-only dedicated pass."""
    import ingest
    s = Session()
    g = add_group_lead("Photo Walk", discovered_via="manual", sess=s)
    src = add_source_lead("Photo Walk Cal", adapter="stub_ok", url="https://pw", sess=s)
    bs, _, bg = build_source(src.id, "group", g.id, sess=s)
    ok(bs.health == "ok" and bg.status == "verified" and bg.monitored,
       "working group source => group verified+monitored (build_source was venue-only)")
    venue, group = ingest._dedicated_target(s, bs)
    ok(venue is None and group is not None and group.id == g.id,
       "group-only source resolves to its group instead of a silent skip")
    raw = stub_ok("https://pw", "Photo Walk Cal", dt.date.today(), dt.date.today())[0]
    ok(ingest._upsert_event(s, raw, None, bs, group=group) == "new", "group event inserts")
    ok(ingest._upsert_event(s, raw, None, bs, group=group) == "seen", "group upsert idempotent")
    e = s.query(Event).filter_by(group_id=g.id).one()
    ok(e.venue_id is None and f"|group:{g.id}|" in e.dedupe_key,
       "venue-less observation with group-scoped identity")
    ok(is_visible(e, None, group, bs), "monitored group => event visible")
    group.monitored = False
    ok(not is_visible(e, None, group, bs), "unmonitored group hides its events")
    s.rollback()


def test_discover_from_events_fuzzy_and_placeholder():
    from discover import discover_from_events
    s = Session()
    agg = add_source_lead("Fire Agg", adapter="stub_ok", is_aggregator=True, sess=s)
    known = add_venue_lead("Bok", discovered_via="manual", sess=s)
    known.lat, known.lng = 39.9291, -75.1665
    s.flush()
    # a variant name at the same coordinates resolves to the EXISTING venue
    v = discover_from_events({"venue": "Bok Building", "lat": 39.9291, "lng": -75.1665},
                             agg.id, s)
    ok(v is not None and v.id == known.id,
       "fuzzy identity resolves variant to existing venue, no duplicate mint")
    cov = s.query(SourceCoverage).filter_by(source_id=agg.id, target_id=known.id).first()
    ok(cov is not None and cov.adoption == "discovered",
       "aggregator hit queues a 'discovered' coverage row")
    # a bare city name is a listing placeholder, never a venue (CITY, not a
    # literal, so this holds under any BULLETIN_CITY)
    from models import CITY
    ok(discover_from_events({"venue": CITY}, agg.id, s) is None,
       "bare city-name venue is dropped, not minted")
    # a genuinely new name mints an UNMONITORED candidate
    v2 = discover_from_events({"venue": "Totally New Room"}, agg.id, s)
    ok(v2 is not None and v2.status == "candidate" and v2.monitored is False,
       "unknown venue mints as unmonitored candidate")
    s.rollback()


def test_routing_map_honors_aliases():
    from ingest import _routing_map
    from operators import merge_venues
    s = Session()
    agg = add_source_lead("Alias Agg", adapter="stub_ok", is_aggregator=True, sess=s)
    keep = add_venue_lead("Lardner's Point Park", discovered_via="manual", sess=s)
    dup = add_venue_lead("Lardners Point Park", discovered_via="manual", sess=s)
    s.flush()
    merge_venues(keep.id, dup.id, reason="spelling variant", sess=s)
    connect_coverage(agg.id, "venue", keep.id, routing_key=keep.id, sess=s)
    m = _routing_map(s, agg)
    ok(m.get(keep.id) is keep and m.get(dup.id) is keep,
       "aggregator routing map resolves a merged alias slug to the survivor")
    s.rollback()


def test_coverage_semantics():
    s = Session()
    v = add_venue_lead("Cov Venue", discovered_via="manual", sess=s)
    src = add_source_lead("Cov Source", adapter="stub_ok", sess=s)
    cov = connect_coverage(src.id, "venue", v.id, target_page_url="https://p1", sess=s)
    cov = connect_coverage(src.id, "venue", v.id, target_page_url="https://p2",
                           is_primary=True, sess=s)
    ok(cov.target_page_url == "https://p2" and cov.is_primary,
       "retouch overwrites page url, upgrades primary")
    n = s.query(SourceCoverage).filter_by(source_id=src.id).count()
    ok(n == 1, "connect_coverage idempotent on triple")
    # routing_key is the BARE slug — a `park:` prefix (the event TAG's shape)
    # never matches ingest._park_slug's lookup; 21 Cambridge rows sat inert
    # for a week that way (09-06). Normalize at the write boundary.
    cov = connect_coverage(src.id, "venue", v.id, routing_key=" park:Some-Room ", sess=s)
    ok(cov.routing_key == "Some-Room", "connect_coverage strips a park: prefix + whitespace")
    cov = connect_coverage(src.id, "venue", v.id, routing_key="park:", sess=s)
    ok(cov.routing_key == "Some-Room", "an empty key after stripping is a no-op, not an overwrite")
    s.rollback()


def test_set_source_aggregator_rejudges_monitored():
    from operators import set_source_aggregator
    s = Session()
    v = add_venue_lead("Agg Flip Venue", discovered_via="manual", sess=s)
    src = add_source_lead("Agg Flip Source", adapter="stub_ok", url="https://af", sess=s)
    build_source(src.id, "venue", v.id, sess=s)
    v = s.get(Venue, v.id)
    ok(v.monitored, "precondition: dedicated built source monitors its venue")
    v.status = "candidate"  # so monitored has no second leg to stand on
    src = set_source_aggregator(src.id, True, note="really a citywide board", sess=s)
    ok(src.is_aggregator and src.status == "built" and src.health == "ok",
       "flip sets the flag without touching the gate's verdict")
    ok(s.get(Venue, v.id).monitored is False,
       "venue fed only by the now-aggregator is re-judged un-monitored")
    set_source_aggregator(src.id, False, sess=s)
    ok(s.get(Venue, v.id).monitored is True, "flip back re-monitors")
    s.rollback()


def test_routing_gap_and_aggregator_group_guard():
    # the later.md P1 shape: a built aggregator with no routable venue coverage
    # matches NEITHER ingest pass and freezes at built/ok/0 forever
    import steward
    from operators import set_source_aggregator
    s = Session()
    v = add_venue_lead("Routed Park", discovered_via="manual", sess=s)
    good = add_source_lead("Routed Agg", adapter="stub_ok", url="https://ragg", sess=s)
    mark_source_built(good.id, sess=s)
    set_source_aggregator(good.id, True, sess=s)
    connect_coverage(good.id, "venue", v.id, routing_key="routed-park", sess=s)
    # frozen shape: group-only coverage, flag flipped AFTER the coverage existed
    # (so the connect-time guard never saw the combination)
    g = add_group_lead("Roaming Org", sess=s)
    bad = add_source_lead("Frozen Agg", adapter="stub_ok", url="https://fagg", sess=s)
    mark_source_built(bad.id, sess=s)
    connect_coverage(bad.id, "group", g.id, sess=s)
    set_source_aggregator(bad.id, True, sess=s)
    ok(steward.routing_gap(s) == 1,
       "routing pass flags exactly the unroutable aggregator")
    unrouted = add_source_lead("Unrouted Agg", adapter="stub_ok", url="https://uagg", sess=s)
    mark_source_built(unrouted.id, sess=s)
    set_source_aggregator(unrouted.id, True, sess=s)
    try:
        connect_coverage(unrouted.id, "group", g.id, sess=s)
        ok(False, "group coverage on an unrouted aggregator must be rejected")
    except ValueError:
        ok(True, "connect_coverage refuses the never-routed aggregator+group combo")
    # a ROUTED aggregator may carry a group: it is the organizer stamp, not a route
    cov = connect_coverage(good.id, "group", g.id, sess=s)
    ok(cov.target_kind == "group", "group coverage accepted on a routed aggregator")
    s.rollback()


def test_group_stamped_alongside_venue():
    """Venue coverage is the where, group coverage the who. Until 2026-09-06 a
    source with both landed events venue-only (NKCDC: 60 events, 0 attributed),
    and an organizer-run aggregator (cinéSPEAK) could not carry a group at all."""
    import ingest
    from operators import set_source_aggregator
    s = Session()
    v = add_venue_lead("Engagement Center", discovered_via="manual", sess=s)
    g = add_group_lead("Neighborhood CDC", discovered_via="manual", sess=s)
    src = add_source_lead("CDC Calendar", adapter="stub_ok", url="https://cdc", sess=s)
    build_source(src.id, "venue", v.id, sess=s)
    connect_coverage(src.id, "group", g.id, sess=s)
    bs = s.get(PipelineSource, src.id)
    venue, group = ingest._dedicated_target(s, bs)
    ok(venue is not None and group is not None and group.id == g.id,
       "dedicated source with both coverages resolves venue AND group")
    raw = stub_ok("https://cdc", "CDC Calendar", dt.date.today(), dt.date.today())[0]
    ok(ingest._upsert_event(s, raw, venue, bs, group=group) == "new", "inserts")
    e = s.query(Event).filter_by(source_id=bs.id).one()
    ok(e.venue_id == v.id and e.group_id == g.id and f"|{v.id}|" in e.dedupe_key,
       "event carries both, identity stays venue-scoped (no dupes on upgrade)")
    # a row landed before the stamp existed gains it on the next pull
    e.group_id = None
    ok(ingest._upsert_event(s, raw, venue, bs, group=group) == "seen", "re-seen")
    ok(s.query(Event).filter_by(source_id=bs.id).one().group_id == g.id,
       "re-seen row gains the group stamp")
    # organizer-run aggregator: routed by venue, stamped with its group
    agg = add_source_lead("Org Agg", adapter="stub_ok", url="https://oagg", sess=s)
    mark_source_built(agg.id, sess=s)
    set_source_aggregator(agg.id, True, sess=s)
    connect_coverage(agg.id, "venue", v.id, routing_key="ec", sess=s)
    connect_coverage(agg.id, "group", g.id, is_primary=True, sess=s)
    ok(ingest._organizer_group(s, s.get(PipelineSource, agg.id)).id == g.id,
       "aggregator resolves its organizer group")
    s.rollback()


def test_retarget_and_remove_coverage():
    from operators import remove_coverage, retarget_coverage
    s = Session()
    g = add_group_lead("Retarget Org", sess=s)
    right = add_venue_lead("Right Room", discovered_via="manual", sess=s)
    src = add_source_lead("Retarget Source", adapter="stub_ok", url="https://rt", sess=s)
    mark_source_built(src.id, sess=s)
    connect_coverage(src.id, "group", g.id, is_primary=True, sess=s)

    # group -> venue: the penn-today shape (events belong to rooms, not the org)
    cov = retarget_coverage(src.id, "group", g.id, right.id,
                            new_target_kind="venue", routing_key="right-room", sess=s)
    ok(cov.target_kind == "venue" and cov.target_id == right.id and cov.is_primary,
       "retarget repoints the row, keeps is_primary")
    ok(cov.routing_key == "right-room", "retarget can stamp the routing key")
    ok(s.get(Venue, right.id).monitored, "new venue target re-judged monitored")
    cov2 = retarget_coverage(src.id, "group", g.id, right.id,
                             new_target_kind="venue", sess=s)
    ok(cov2.target_id == right.id, "re-run is idempotent (row already moved)")

    # retargeting onto an already-covered target folds instead of duplicating
    wrong = add_venue_lead("Wrong Room", discovered_via="manual", sess=s)
    connect_coverage(src.id, "venue", wrong.id, target_page_url="https://w", sess=s)
    retarget_coverage(src.id, "venue", wrong.id, right.id, sess=s)
    n = s.query(SourceCoverage).filter_by(source_id=src.id, target_kind="venue").count()
    ok(n == 1, "fold: one row per (source, target) survives a collision retarget")

    try:
        retarget_coverage(src.id, "venue", right.id, "no-such-venue", sess=s)
        ok(False, "retarget onto a missing target must raise")
    except KeyError:
        ok(True, "retarget onto a missing target raises KeyError")

    ok(remove_coverage(src.id, "venue", right.id, sess=s) is True, "remove deletes the row")
    ok(remove_coverage(src.id, "venue", right.id, sess=s) is False, "re-remove is a no-op")
    ok(s.get(Venue, right.id).monitored is False,
       "monitored does not outlive the removed feed")
    s.rollback()


# ── costs + series (engine-side) ────────────────────────────────────────────

def test_parse_cost():
    from costs import parse_cost
    ok(parse_cost("Free") == (0, 0, True), "Free")
    ok(parse_cost("$10-$15") == (1000, 1500, False), "range")
    ok(parse_cost("suggested donation") == (0, 0, True), "donation = free")
    ok(parse_cost("") == (None, None, None), "empty = no signal, never a guess")
    ok(parse_cost("doors at 8") == (None, None, None), "no digits = no signal")


def test_derive_series():
    from series import derive_series
    s = Session()
    v = add_venue_lead("Series Venue", discovered_via="manual", sess=s)
    for i, day in enumerate((2, 9, 16, 23)):                       # weekly
        s.add(Event(dedupe_key=f"sv|w{i}", name="Chess Club #"+str(i),
                    start=dt.datetime(2026, 6, day, 19), venue_id=v.id))
    for i in range(3):                                             # nightly run
        s.add(Event(dedupe_key=f"sv|r{i}", name="A Play",
                    start=dt.datetime(2026, 6, 1 + i, 20), venue_id=v.id))
    s.add(Event(dedupe_key="sv|solo", name="One Night Only",
                start=dt.datetime(2026, 6, 12, 20), venue_id=v.id))
    s.flush()
    n, stamped = derive_series(s)
    ok(n == 2 and stamped == 7, f"2 clusters, 7 stamped (got {n}, {stamped})")
    chess = s.query(Event).filter(Event.dedupe_key == "sv|w0").one()
    sr = s.get(models.Series, chess.series_id)
    ok(sr.cadence == "weekly" and sr.n_dates == 4, "episode numbers cluster as weekly")
    solo = s.query(Event).filter(Event.dedupe_key == "sv|solo").one()
    ok(solo.series_id is None, "singleton stays unstamped")
    n2, stamped2 = derive_series(s)
    ok(n2 == 2 and stamped2 == 0, "derive is idempotent")
    s.rollback()


# ── ingest mechanics ────────────────────────────────────────────────────────

def test_upsert_and_visibility():
    import ingest
    s = Session()
    v = add_venue_lead("Up Venue", discovered_via="manual", sess=s)
    src = add_source_lead("Up Source", adapter="stub_ok", url="https://u", sess=s)
    build_source(src.id, "venue", v.id, sess=s)

    raw = stub_ok("https://u", "Up Source", dt.date.today(), dt.date.today())[0]
    ok(ingest._upsert_event(s, raw, v, src) == "new", "first upsert -> new")
    ok(ingest._upsert_event(s, raw, v, src) == "seen", "second upsert -> seen (idempotent)")
    revised = dict(raw, description="Fresh source copy", url="https://u/revised")
    ok(ingest._upsert_event(s, revised, v, src) == "seen", "same-source refresh stays one row")
    ok(s.query(Event).filter_by(venue_id=v.id).count() == 1, "no duplicate rows")

    e = s.query(Event).filter_by(venue_id=v.id).one()
    ok(e.description == "Fresh source copy" and e.url == "https://u/revised",
       "same source refreshes canonical fields")
    ok(e.lifecycle == "active" and e.start.date().isoformat() == TOMORROW, "lifecycle/start parsed")
    ok(is_visible(e, v, None, src), "built + monitored => visible")
    set_source_status(src.id, "none", sess=s)
    ok(not is_visible(e, v, None, s.get(PipelineSource, src.id)), "downgrade hides events")

    # upstream junk: a "name" that is itself a URL never mints a row
    # (theheartofjazz.org once titled a post with its own image URL)
    junk = dict(raw, name="https://cdn.example.org/uploads/RIT-Mo-Collective.jpg")
    ok(ingest._upsert_event(s, junk, v, src) is None, "URL-as-name row is dropped")
    ok(ingest._upsert_event(s, dict(raw, name="  HTTP://x.com/a "), v, src) is None,
       "URL-as-name guard is case/whitespace-insensitive")
    s.rollback()


def test_cross_source_event_observations_and_display_deduplication():
    import ingest
    s = Session()
    v = add_venue_lead("Dedupe Venue", discovered_via="manual", sess=s)
    agg = add_source_lead("Dedupe Aggregator", adapter="stub_ok", url="https://agg",
                          is_aggregator=True, sess=s)
    dedicated = add_source_lead("Dedupe Primary", adapter="stub_ok", url="https://primary",
                                sess=s)
    build_source(agg.id, "venue", v.id, is_primary=False, sess=s)
    build_source(dedicated.id, "venue", v.id, is_primary=True, sess=s)
    day = TOMORROW
    listed = {"name": "SOLD OUT: Solar Myth Jazz Night — VIP tickets", "date": day,
              "time": "19:00", "description": "thin", "url": "https://agg/e"}
    primary = {"name": "Solar Myth Jazz Night", "date": day, "time": "19:30",
               "description": "The venue's full description", "url": "https://primary/e"}
    ok(ingest._upsert_event(s, listed, v, agg) == "new", "aggregator listing inserts")
    ok(ingest._upsert_event(s, primary, v, dedicated) == "new",
       "different source remains a separate observation")
    rows = s.query(Event).filter_by(venue_id=v.id).all()
    ok(len(rows) == 2 and {row.source_id for row in rows} == {agg.id, dedicated.id},
       "database retains both source observations")
    ok(len({row.dedupe_key for row in rows}) == 2
       and all(row.dedupe_key.startswith(row.source_id + "|") for row in rows),
       "stored observation identity is source-scoped")
    from dedupe import canonical_event_rows
    displayed = canonical_event_rows(s, rows)
    ok(len(displayed) == 1 and displayed[0].source_id == dedicated.id,
       "public export chooses the primary dedicated observation")

    later = dict(primary, time="22:30", url="https://primary/late")
    later["name"] = "Solar Myth Jazz Night — Late Show"
    ok(ingest._upsert_event(s, later, v, dedicated) == "new",
       "same-day title at a materially different time stays separate")
    ok(s.query(Event).filter_by(venue_id=v.id).count() == 3, "false positive guard retains late show")
    s.rollback()


def test_export_deduplication_keeps_db_history():
    from dedupe import canonical_event_rows
    s = Session()
    v = add_venue_lead("Export Dedupe Venue", sess=s)
    agg = add_source_lead("Export Dedupe Agg", adapter="stub_ok", url="https://eda",
                          is_aggregator=True, sess=s)
    primary = add_source_lead("Export Dedupe Primary", adapter="stub_ok", url="https://edp",
                              sess=s)
    build_source(agg.id, "venue", v.id, is_primary=False, sess=s)
    build_source(primary.id, "venue", v.id, is_primary=True, sess=s)
    when = dt.datetime.now().replace(hour=19, minute=0, second=0, microsecond=0)
    a = Event(dedupe_key="export-agg", name="SOLD OUT: Community Print Night",
              start=when, venue_id=v.id, source_id=agg.id)
    p = Event(dedupe_key="export-primary", name="Community Print Night",
              start=when, venue_id=v.id, source_id=primary.id)
    s.add_all([a, p]); s.flush()
    displayed = canonical_event_rows(s, [a, p])
    ok(displayed == [p], "export chooses the primary dedicated listing")
    ok(s.query(Event).filter(Event.dedupe_key.like("export-%")).count() == 2,
       "export dedupe retains both durable source observations")
    s.rollback()


def test_groupless_venue_observation_collapse():
    """A roaming group's venue-less listing and an aggregator's venued listing of
    the SAME party must display once — and the venued one wins (it has a pin)."""
    from dedupe import canonical_event_rows, events_match
    s = Session()
    v = add_venue_lead("Club Sixhundred", discovered_via="manual", sess=s)
    g = add_group_lead("Roam Producer", discovered_via="manual", sess=s)
    agg = add_source_lead("Roam Agg", adapter="stub_ok", url="https://ra",
                          is_aggregator=True, sess=s)
    ded = add_source_lead("Roam Own Cal", adapter="stub_ok", url="https://rd", sess=s)
    build_source(agg.id, "venue", v.id, is_primary=False, sess=s)
    build_source(ded.id, "group", g.id, sess=s)
    when = dt.datetime.now().replace(hour=20, minute=0, second=0, microsecond=0)
    venued = Event(dedupe_key="roam|venued", name="Tease: A Pop Up Revue",
                   start=when, venue_id=v.id, source_id=agg.id)
    roaming = Event(dedupe_key="roam|groupless", name="TEASE: A Pop Up Revue",
                    start=when, venue_id=None, group_id=g.id, source_id=ded.id)
    s.add_all([venued, roaming]); s.flush()
    ok(events_match(venued, roaming), "venue-less observation matches its venued twin")
    displayed = canonical_event_rows(s, [venued, roaming])
    ok(displayed == [venued], "one representative displays, and the pinned one wins")
    other = Event(dedupe_key="roam|other", name="Completely Different Reading",
                  start=when, venue_id=None, group_id=g.id, source_id=ded.id)
    s.add(other); s.flush()
    ok(len(canonical_event_rows(s, [venued, roaming, other])) == 2,
       "a different venue-less event the same night stays separate")
    s.rollback()


def test_same_url_cross_source_identity():
    """A renamed cross-post sharing one event-specific URL is the SAME event
    (Otaku After Dark / Anime Drag Tribute Show, 2026-08-18) — but only across
    sources, and never via a generic calendar-page URL."""
    from dedupe import canonical_event_rows, event_url_key, events_match
    ok(event_url_key("https://www.eventbrite.com/e/otaku-tickets-199?aff=x&utm_y=z")
       == "eventbrite.com/e/otaku-tickets-199",
       "url key strips www, query tracking, and keeps host+path")
    ok(event_url_key("https://venue.com/events/") == "" and event_url_key(None) == "",
       "a generic calendar path is never identity")

    s = Session()
    v = add_venue_lead("Url Twin Venue", discovered_via="manual", sess=s)
    agg1 = add_source_lead("Url Twin Agg A", adapter="stub_ok", url="https://uta",
                           is_aggregator=True, sess=s)
    agg2 = add_source_lead("Url Twin Agg B", adapter="stub_ok", url="https://utb",
                           is_aggregator=True, sess=s)
    build_source(agg1.id, "venue", v.id, is_primary=False, sess=s)
    build_source(agg2.id, "venue", v.id, is_primary=False, sess=s)
    when = dt.datetime.now().replace(hour=20, minute=30, second=0, microsecond=0)
    midnight = dt.datetime.combine(when.date(), dt.time())
    ticket = "https://www.eventbrite.com/e/otaku-after-dark-tickets-1997"
    timed = Event(dedupe_key="ut|timed", name="Otaku After Dark", start=when,
                  venue_id=v.id, source_id=agg1.id, url=ticket)
    renamed = Event(dedupe_key="ut|renamed", name="Anime Drag Tribute Show",
                    start=midnight, venue_id=v.id, source_id=agg2.id,
                    url=ticket + "?aff=oddtdtcreator&utm_source=ig")
    s.add_all([timed, renamed]); s.flush()
    ok(events_match(timed, renamed), "same event url across sources is identity")
    same_source = Event(dedupe_key="ut|samesource", name="Totally Different Night",
                        start=midnight, venue_id=v.id, source_id=agg1.id, url=ticket)
    ok(not events_match(timed, same_source),
       "one source reusing a url does not collapse its own distinct events")
    displayed = canonical_event_rows(s, [timed, renamed])
    ok(displayed == [timed], "the timed listing represents its date-only url twin")

    # The transitive trio: a name-twin AND a url-twin must still display ONCE
    # (the url pre-pass keeps the ambiguity guard from tripping on it).
    named = Event(dedupe_key="ut|named", name="Otaku After Dark", start=when,
                  venue_id=v.id, source_id=agg2.id, url="https://utb/events/2026/otaku")
    s.add(named); s.flush()
    ok(len(canonical_event_rows(s, [named, renamed, timed])) == 1,
       "name-twin + renamed url-twin collapse to one displayed row")
    s.rollback()


def test_export_filters_invisible():
    """Export is the ONLY public surface, so is_visible must bite there. Until
    2026-08-17 it never did — un-vetted discovered-venue events leaked onto the
    site (53 live at the time of the fix)."""
    import tempfile

    from export import main as export_main
    s = Session()
    v = add_venue_lead("Leak Venue", discovered_via="manual", sess=s)
    src = add_source_lead("Leak Source", adapter="stub_ok", url="https://leak", sess=s)
    build_source(src.id, "venue", v.id, sess=s)
    hidden = add_venue_lead("Leak Hidden Candidate", discovered_via="manual", sess=s)
    when = dt.datetime.now().replace(hour=19, minute=0, second=0,
                                     microsecond=0) + dt.timedelta(days=1)
    s.add_all([
        Event(dedupe_key="leak|vis", name="Visible Night", start=when,
              venue_id=v.id, source_id=src.id),
        Event(dedupe_key="leak|invis", name="Invisible Night", start=when,
              venue_id=hidden.id, source_id=src.id),
    ])
    s.commit()          # export opens its own session; cleanup below undoes this
    try:
        with tempfile.NamedTemporaryFile("r", suffix=".json", delete=False) as fh:
            export_main(days=7, out=fh.name)
            bundle = _json_mod.load(open(fh.name))
        names = {e["name"] for e in bundle["events"]}
        ok("Visible Night" in names, "visible event exports")
        ok("Invisible Night" not in names, "un-vetted candidate venue's event held back")
        ok(bundle["meta"]["invisible_suppressed"] >= 1,
           "suppression is counted, not silent")
    finally:
        s.query(Event).filter(Event.dedupe_key.like("leak|%")).delete(synchronize_session=False)
        s.query(SourceCoverage).filter_by(source_id=src.id).delete()
        s.query(PipelineSource).filter_by(id=src.id).delete()
        s.query(Venue).filter(Venue.id.in_([v.id, hidden.id])).delete(synchronize_session=False)
        s.commit()


def test_ghost_retirement_window_aware():
    """The Rabbit case (2026-08-19): a future event that vanishes from its own
    feed INSIDE the pulled window retires to lifecycle='unlisted' — reversible
    if re-seen — while rows beyond the pull's horizon are never touched, and
    export hides unlisted rows with the count reported."""
    import tempfile

    import ingest
    from export import main as export_main
    s = Session()
    v = add_venue_lead("Ghost Venue", discovered_via="manual", sess=s)
    src = add_source_lead("Ghost Source", adapter="stub_ok", url="https://ghost", sess=s)
    build_source(src.id, "venue", v.id, sess=s)
    kept_raw = {"name": "Cineclub en Español", "date": TOMORROW, "time": "18:30"}
    ghost_raw = {"name": "English Language Film Club", "date": TOMORROW, "time": "18:30"}
    far_raw = {"name": "Far Future Gala",
               "date": (dt.date.today() + dt.timedelta(days=40)).isoformat(),
               "time": "19:00"}
    end = dt.date.today() + dt.timedelta(days=10)
    touched: set = set()
    for raw in (kept_raw, ghost_raw, far_raw):
        ingest._upsert_event(s, raw, v, src, touched=touched)
    ok(ingest._retire_vanished(s, src, touched, end) == 0,
       "a pull that confirms every row retires nothing")

    # second pull: the English row has vanished from the feed
    touched = set()
    ingest._upsert_event(s, kept_raw, v, src, touched=touched)
    ok(ingest._retire_vanished(s, src, touched, end) == 1, "the vanished row retires")
    rows = {e.name: e for e in s.query(Event).filter_by(venue_id=v.id).all()}
    ok(rows["English Language Film Club"].lifecycle == "unlisted", "ghost -> unlisted")
    ok(rows["Cineclub en Español"].lifecycle == "active", "confirmed row stays active")
    ok(rows["Far Future Gala"].lifecycle == "active",
       "a --days N pull says nothing about rows beyond its horizon")

    # reversible: the row reappears in the feed on a later pull
    ingest._upsert_event(s, ghost_raw, v, src)
    ok(rows["English Language Film Club"].lifecycle == "active",
       "a re-seen row flips back to active")

    # ghost it again and prove export hides it, with the count reported
    ingest._retire_vanished(
        s, src, {rows["Cineclub en Español"], rows["Far Future Gala"]}, end)
    s.commit()          # export opens its own session; cleanup below undoes this
    try:
        with tempfile.NamedTemporaryFile("r", suffix=".json", delete=False) as fh:
            export_main(days=7, out=fh.name)
            bundle = _json_mod.load(open(fh.name))
        names = {e["name"] for e in bundle["events"]}
        ok("Cineclub en Español" in names, "the live row exports")
        ok("English Language Film Club" not in names, "the unlisted ghost is held back")
        ok(bundle["meta"]["unlisted_suppressed"] >= 1, "suppression is counted, not silent")
    finally:
        s.query(Event).filter_by(venue_id=v.id).delete(synchronize_session=False)
        s.query(SourceCoverage).filter_by(source_id=src.id).delete()
        s.query(PipelineSource).filter_by(id=src.id).delete()
        s.query(Venue).filter_by(id=v.id).delete()
        s.commit()


def test_suppress_event_survives_reingest():
    """The JazzAttack case (2026-08-30): upstream keeps publishing an event we
    KNOW is wrong (the organizer moved venues), so 'unlisted' won't stick — the
    next pull re-sees the row and flips it back. suppress_event is durable:
    merge_event_values preserves lifecycle='suppressed' and export hides it."""
    import tempfile

    import ingest
    from export import main as export_main
    from operators import suppress_event
    s = Session()
    v = add_venue_lead("Stale Calendar Hall", discovered_via="manual", sess=s)
    src = add_source_lead("Stale Source", adapter="stub_ok", url="https://stale", sess=s)
    build_source(src.id, "venue", v.id, sess=s)
    raw = {"name": "Moved-Out Dance Night", "date": TOMORROW, "time": "19:00"}
    ingest._upsert_event(s, raw, v, src)
    row = s.query(Event).filter_by(venue_id=v.id).one()
    suppress_event(row.id, reason="organizer moved venues; feed is stale", sess=s)
    ok(row.lifecycle == "suppressed", "suppress_event marks the row")
    ok("organizer moved venues" in row.verified_by, "the reason is recorded")

    # the feed still lists it — a re-ingest must NOT resurrect it
    ingest._upsert_event(s, raw, v, src)
    ok(row.lifecycle == "suppressed", "re-seen row stays suppressed")
    suppress_event(row.id, reason="organizer moved venues; feed is stale", sess=s)
    ok(row.lifecycle == "suppressed", "idempotent re-suppress")

    s.commit()          # export opens its own session; cleanup below undoes this
    try:
        with tempfile.NamedTemporaryFile("r", suffix=".json", delete=False) as fh:
            export_main(days=7, out=fh.name)
            bundle = _json_mod.load(open(fh.name))
        names = {e["name"] for e in bundle["events"]}
        ok("Moved-Out Dance Night" not in names, "export hides the suppressed row")
    finally:
        s.query(Event).filter_by(venue_id=v.id).delete(synchronize_session=False)
        s.query(SourceCoverage).filter_by(source_id=src.id).delete()
        s.query(PipelineSource).filter_by(id=src.id).delete()
        s.query(Venue).filter_by(id=v.id).delete()
        s.commit()


def test_suppress_series_catches_fresh_syndicated_rows():
    """The JazzAttack LEAK (2026-09-11): suppress_event is per row, so when
    do215 re-syndicated the Ethical Society's stale Tuesday a week later as a
    brand-new row, it walked onto the site. suppress_series is a standing rule
    on (name, venue): it suppresses what's there now AND anything any source
    lands later — new rows and re-seen rows alike — with zero follow-up."""
    import ingest
    from models import EventSuppression
    from operators import suppress_series
    s = Session()
    v = add_venue_lead("Stale Calendar Hall 2", discovered_via="manual", sess=s)
    src = add_source_lead("Stale Venue Cal", adapter="stub_ok", url="https://stale2", sess=s)
    syn = add_source_lead("Stale Syndicator", adapter="stub_ok", url="https://stale2-syn", sess=s)
    build_source(src.id, "venue", v.id, sess=s)
    build_source(syn.id, "venue", v.id, sess=s)
    day2 = (dt.date.today() + dt.timedelta(days=8)).isoformat()
    raw1 = {"name": "Moved-Out Swing Night", "date": TOMORROW, "time": "19:00"}
    ingest._upsert_event(s, raw1, v, src)
    existing = s.query(Event).filter_by(venue_id=v.id).one()

    rule, n = suppress_series("SOLD OUT: Moved-Out Swing Night", v.id,
                              reason="organizer moved venues; calendar is stale", sess=s)
    ok(n == 1 and existing.lifecycle == "suppressed", "rule suppresses the rows already present")
    ok(rule.name_norm == "moved out swing night", "rule keys on the normalized name")
    rule2, n2 = suppress_series("Moved-Out Swing Night", v.id, reason="again", sess=s)
    ok(rule2.id == rule.id and n2 == 0, "idempotent: same rule, nothing new to suppress")

    # a DIFFERENT source lands a NEW row for a later week — the leak case
    raw2 = {"name": "Moved-Out Swing Night", "date": day2, "time": "19:00"}
    ok(ingest._upsert_event(s, raw2, v, syn) == "new", "syndicator mints a fresh row")
    fresh = s.query(Event).filter_by(venue_id=v.id, source_id=syn.id).one()
    ok(fresh.lifecycle == "suppressed", "fresh syndicated row is suppressed on insert")
    ok("organizer moved venues" in fresh.verified_by, "the rule's reason travels with it")

    # the venue's own feed re-sees the original row — still suppressed
    ingest._upsert_event(s, raw1, v, src)
    ok(existing.lifecycle == "suppressed", "re-seen row stays suppressed")

    # a different name at the same venue is untouched
    ingest._upsert_event(s, {"name": "Unrelated Talk", "date": TOMORROW, "time": "19:00"}, v, src)
    other = s.query(Event).filter_by(venue_id=v.id, name="Unrelated Talk").one()
    ok(other.lifecycle == "active", "rule is exact: other events at the venue are untouched")

    s.query(Event).filter_by(venue_id=v.id).delete(synchronize_session=False)
    s.query(EventSuppression).filter_by(venue_id=v.id).delete()
    for sid in (src.id, syn.id):
        s.query(SourceCoverage).filter_by(source_id=sid).delete()
        s.query(PipelineSource).filter_by(id=sid).delete()
    s.query(Venue).filter_by(id=v.id).delete()
    s.commit()


def test_dupe_suspect_queue_and_verdicts():
    """The dupe-suspect loop (2026-08-19): the detector surfaces same-venue/
    same-minute pairs the matcher does NOT collapse; a persisted verdict drives
    display ('same' collapses, 'distinct' protects) and retires the pair from
    the queue forever."""
    from dedupe import canonical_event_rows, dupe_suspects
    from models import DedupeVerdict
    from operators import record_dedupe_verdict
    s = Session()
    v = add_venue_lead("Suspect Garden", discovered_via="manual", sess=s)
    src = add_source_lead("Suspect Source", adapter="stub_ok", url="https://sus", sess=s)
    build_source(src.id, "venue", v.id, sess=s)
    when = dt.datetime.now().replace(hour=10, minute=0, second=0,
                                     microsecond=0) + dt.timedelta(days=1)
    ride = Event(dedupe_key="sus|ride", name="Learn to Ride a Bike",
                 start=when, venue_id=v.id, source_id=src.id)
    boat = Event(dedupe_key="sus|boat", name="Free Community Boating",
                 start=when, venue_id=v.id, source_id=src.id)
    s.add_all([ride, boat]); s.flush()

    ok(dupe_suspects(s, [ride, boat]) == [(ride, boat)],
       "same venue + same minute + unmatched names -> a suspect pair")
    ok(len(canonical_event_rows(s, [ride, boat])) == 2,
       "no hardcoded time-collapse: both display until a judge says otherwise")

    record_dedupe_verdict(ride.dedupe_key, boat.dedupe_key, "same",
                          reason="test: one event double-listed", sess=s)
    ok(dupe_suspects(s, [ride, boat]) == [], "a judged pair never re-enters the queue")
    ok(len(canonical_event_rows(s, [ride, boat])) == 1, "'same' collapses display")

    record_dedupe_verdict(boat.dedupe_key, ride.dedupe_key, "distinct",
                          reason="test: correction — genuinely distinct", sess=s)
    ok(s.query(DedupeVerdict).count() == 1,
       "one row per sorted pair; a re-record overwrites (correction)")
    ok(len(canonical_event_rows(s, [ride, boat])) == 2, "'distinct' restores both")
    ok(dupe_suspects(s, [ride, boat]) == [], "still judged, still out of the queue")

    # a pair the matcher ALREADY collapses is never a suspect
    twin = Event(dedupe_key="sus|twin", name="Learn to Ride a Bike!",
                 start=when, venue_id=v.id, source_id=src.id)
    s.add(twin); s.flush()
    ok((ride, twin) not in dupe_suspects(s, [ride, boat, twin]),
       "matcher-collapsed pairs are excluded from the queue")

    try:
        record_dedupe_verdict(ride.dedupe_key, boat.dedupe_key, "maybe", sess=s)
        ok(False, "invalid verdict must raise")
    except ValueError:
        ok(True, "invalid verdict rejected")
    s.rollback()


def test_remove_alias_reopens_slug():
    """The Boathouse incident (2026-08-19): a wrong auto-alias had to be
    hand-deleted because no operator existed. remove_alias is that operator —
    idempotent, and the freed slug mints fresh instead of resolving away."""
    from operators import remove_alias
    s = Session()
    v = add_venue_lead("Alias Test Park", discovered_via="manual", sess=s)
    s.add(VenueAlias(alias_id="alias-test-boathouse", venue_id=v.id,
                     name="Alias Test Boathouse", reason="wrong auto-alias"))
    s.flush()
    fetched = add_venue_lead("Alias Test Boathouse", sess=s)
    ok(fetched.id == v.id, "before removal the alias swallows the mint")
    ok(remove_alias("alias-test-boathouse", sess=s), "alias removed")
    ok(not remove_alias("alias-test-boathouse", sess=s), "second removal is a no-op")
    fresh = add_venue_lead("Alias Test Boathouse", discovered_via="manual", sess=s)
    ok(fresh.id == "alias-test-boathouse" and fresh.id != v.id,
       "freed slug mints a fresh venue")
    s.rollback()


def test_wix_utc_is_converted_to_event_timezone():
    local = scrapers._wix_local_datetime("2026-08-18T23:00:00Z", "America/New_York")
    ok(local == dt.datetime(2026, 8, 18, 19), "Wix UTC instant becomes Eastern wall clock")
    winter = scrapers._wix_local_datetime("2026-12-18T23:00:00Z", "America/New_York")
    ok(winter == dt.datetime(2026, 12, 18, 18), "Wix conversion respects DST")
    ok(scrapers._wix_local_datetime("2026-08-18T23:00:00Z", "") is None,
       "aware Wix timestamp without venue timezone is rejected")
    ok(scrapers._wix_local_datetime("bad", "America/New_York") is None,
       "invalid Wix timestamp is rejected, never guessed")


# ── the two bugs fixed in the port (2026-08-16) ─────────────────────────────

def test_strip_html_kills_style_blocks():
    """Bug 1: Squarespace event bodies embed <style>; the old stripper removed the
    TAGS but kept the CSS, so raw CSS landed in `description` (chi event id 5)."""
    from scrapers import strip_html
    ok(strip_html("<style>.sqs-block{color:#fff;font-size:12px}</style>"
                  "<p>Knitting circle &amp; tea</p>") == "Knitting circle & tea",
       "style block dropped content and all")
    ok("color" not in strip_html("<style>a{color:red}</style>Bingo"), "no CSS leak")
    ok(strip_html("<script>var x=1;</script><div>Show</div>") == "Show", "script dropped")
    ok(strip_html("<style>.a{color:red}<p>Body text") == "Body text",
       "unclosed <style>: residual CSS declarations swept too")
    ok(strip_html("@media (max-width:640px){.a{display:none}} Doors 8pm") == "Doors 8pm",
       "nested at-rules unwound")
    ok(strip_html("<p>Bring $5 &amp; a friend</p>") == "Bring $5 & a friend",
       "ordinary text survives untouched")
    # and the guarantee is structural: norm() sanitizes too, so an adapter that
    # forgets to call strip_html still cannot put CSS in the DB.
    row = scrapers.norm(name="x", description="<style>.a{color:red}</style>Real text")
    ok(row["description"] == "Real text", "norm() sanitizes descriptions itself")


def test_export_clips_description():
    """Bug 2: descriptions are truncated SERVER-side so data.json stays small."""
    import inspect
    from export import MAX_DESCRIPTION, clip, main
    long = "word " * 300
    out = clip(long)
    ok(len(out) <= MAX_DESCRIPTION + 1, f"clipped to ~{MAX_DESCRIPTION} (got {len(out)})")
    ok(out.endswith("…"), "clipped text is marked with an ellipsis")
    ok(clip("short one") == "short one", "short text passes through verbatim")
    ok(clip(None) is None, "None stays None")
    ok(inspect.signature(main).parameters["days"].default == 10,
       "public export defaults to the week plus staleness buffer")


def test_export_week_md_and_sitemap():
    """The agent-facing renditions (data.json is too big for assistant fetch
    tools): week.md carries the score reasons — the product's voice — and
    sitemap.xml carries every event permalink plus the static pages."""
    import tempfile
    from pathlib import Path

    from export import (day_markdown, main as export_main, sitemap_xml,
                        today_markdown, week_markdown)

    bundle = {
        "generated": "2026-08-22T00:30:00", "city": "Philadelphia",
        "venues": [{"id": "test-tavern", "name": "Test Tavern",
                    "neighborhood": "Fishtown–Lower Kensington"}],
        "series": [{"id": "sr-weekly", "cadence": "weekly"},
                   {"id": "sr-run", "cadence": "run"}],
        "events": [
            {"id": 1, "name": "Print Night", "date": "2026-08-23", "time": "19:00",
             "venue_id": "test-tavern", "is_free": True, "quality": 8.2,
             "series_id": "sr-weekly",
             "scores": {"community_building":
                        {"score": 9, "reason": "Same crowd returns weekly."},
                        "serendipity": {"score": 3, "reason": "Low churn."}}},
            {"id": 2, "name": "Unscored Run", "date": "2026-08-23",
             "series_id": "sr-run", "cost": "$10"},
            {"id": 3, "name": "Far Future Gala", "date": "2026-09-15"},
        ],
    }
    md = week_markdown(bundle)
    ok("## Sunday, August 23" in md, "events grouped under day headings")
    ok("- Print Night — Sun Aug 23 · 7:00 PM · Test Tavern · "
       "Fishtown–Lower Kensington · Free · 8.2/10 ↻ — Same crowd returns weekly."
       in md,
       "week lines repeat their date (bench: an agent day-shifted an event "
       "when only section headers carried dates) + time, venue·hood, cost, "
       "score, ↻, and the TOP reason")
    ok("- Unscored Run — Sun Aug 23 · $10" in md,
       "unscored + run-cadence event gets no score, no reason, no ↻")
    ok(md.index("Print Night") < md.index("Unscored Run"),
       "scored events rank above unscored within a day")
    ok("Far Future Gala" not in md,
       "week.md is 7 days even when the bundle carries buffer days")
    ok(models.SITE.split("//")[1] in md.splitlines()[3], "header links the site")

    today = today_markdown(bundle)
    ok("today is Saturday, August 22" in today,
       "today.txt names the current day — assistants never do date math")
    ok(f"{models.SITE}/d/2026-08-23.txt" in today,
       "today.txt links each other in-window day's file")
    ok("2026-09-15" not in today, "days beyond the window are not linked")
    ok("## Today — Saturday, August 22 (0 events)" in today,
       "an empty today is stated plainly, not padded")
    ok(f"{models.SITE}/week.txt" in today,
       "today.txt links the whole-week file for multi-day questions")

    day = day_markdown(bundle, "2026-08-23")
    ok("Sunday, August 23" in day and "(2 events)" in day,
       "a day file is titled with its date and count")
    ok("- Print Night — 7:00 PM · Test Tavern · Fishtown–Lower Kensington · "
       "Free · 8.2/10 ↻ — Same crowd returns weekly." in day,
       "day files carry the same line format as week.txt")

    xml = sitemap_xml(bundle)
    ok(xml.startswith('<?xml version="1.0"'), "sitemap is XML")
    ok("<lastmod>2026-08-22</lastmod>" in xml, "urls carry the export date as lastmod")
    ok(f"<loc>{models.SITE}/event.html?id=1</loc>" in xml
       and f"<loc>{models.SITE}/event.html?id=3</loc>" in xml,
       "every event permalink is listed")
    ok(f"<loc>{models.SITE}/calendar.html</loc>" in xml,
       "static pages are listed")

    # and the export pass actually writes them beside the bundle
    with tempfile.TemporaryDirectory() as td:
        export_main(days=7, out=Path(td) / "x.json")
        ok((Path(td) / "week.txt").exists() and (Path(td) / "sitemap.xml").exists(),
           "export emits week.txt + sitemap.xml beside data.json")
        ok((Path(td) / "week.txt").read_text() == (Path(td) / "week.md").read_text(),
           "week.txt is byte-identical to week.md (the text/plain twin some "
           "assistant fetchers need)")
        ok((Path(td) / "today.txt").exists() and (Path(td) / "d").is_dir(),
           "export emits today.txt and the d/ day-file directory")
        (Path(td) / "d" / "1999-01-01.txt").write_text("stale")
        export_main(days=7, out=Path(td) / "x.json")
        ok(not (Path(td) / "d" / "1999-01-01.txt").exists(),
           "d/ is rewritten wholesale — departed dates prune themselves")


# ── adapters (parse-only, no network; fixtures are real Philly feeds) ──────

def test_route_tag_matches_venue_ids():
    """Aggregator routing is an EXACT slug match against venue ids, so scrapers'
    local _slug must never drift from models.slug (they are separate copies so
    scrapers.py can stay stdlib-only)."""
    for name in ("Union Transfer", "Bob & Barbara's", "Père Lachaise café", "", "  "):
        ok(scrapers._slug(name) == slug(name), f"_slug parity: {name!r}")
    ok(scrapers.route_tag("Union Transfer") == "park:union-transfer", "route tag shape")
    ok(scrapers.route_tag("") == "", "no venue name -> no routing tag")
    import ingest
    tagged = scrapers.norm(name="x", venue="Union Transfer",
                           tags=scrapers.route_tag("Union Transfer"))
    ok(ingest._park_slug(tagged) == "union-transfer", "ingest reads the tag back")


def _offline(monkey: dict):
    """Swap scrapers.core.fetch/fetch_json for canned responses; returns a restore
    fn. Family modules call `core.fetch(...)` (attribute lookup at call time), so
    patching the core module is the one seam that reaches every adapter."""
    real = (scrapers.core.fetch, scrapers.core.fetch_json, scrapers.time.sleep)

    def fake_fetch(url, headers=None, timeout=20):
        for frag, body in monkey.items():
            if frag in url:
                return body if isinstance(body, bytes) else body.encode()
        raise AssertionError(f"unexpected fetch: {url}")

    scrapers.core.fetch = fake_fetch
    scrapers.core.fetch_json = lambda url, **kw: _json_mod.loads(fake_fetch(url))
    scrapers.time.sleep = lambda *_: None
    return lambda: (setattr(scrapers.core, "fetch", real[0]),
                    setattr(scrapers.core, "fetch_json", real[1]),
                    setattr(scrapers.time, "sleep", real[2]))


def test_eventbrite_adapters_parse():
    start = dt.date.today()
    end = start + dt.timedelta(days=90)
    d1 = (start + dt.timedelta(days=5))
    iso = d1.isoformat()

    # eventbrite: __SERVER_DATA__ blob, online events dropped
    blob = _json_mod.dumps({"search_data": {"events": {
        "pagination": {"page_count": 1},
        "results": [
            {"name": "Real Show", "start_date": iso, "start_time": "20:00",
             "is_online_event": False, "url": "https://eventbrite.com/e/x?aff=1",
             "primary_venue": {"name": "Union Transfer", "address": {
                 "latitude": "39.96", "longitude": "-75.15",
                 "localized_address_display": "1026 Spring Garden St"}}},
            {"name": "Webinar", "start_date": iso, "is_online_event": True,
             "primary_venue": {"name": "Online"}},
        ]}}})
    restore = _offline({"eventbrite.com": f"<html>window.__SERVER_DATA__ = {blob};</html>"})
    try:
        rows = scrapers.scrape_eventbrite("https://www.eventbrite.com/d/pa--philadelphia/all-events/",
                                          "eb", start, end)
    finally:
        restore()
    ok(len(rows) == 1 and rows[0]["name"] == "Real Show", "online-only events dropped")
    ok(rows[0]["tags"] == "park:union-transfer" and rows[0]["lat"] == 39.96,
       "venue routing tag + coords lifted")
    ok(rows[0]["url"] == "https://eventbrite.com/e/x", "tracking query stripped from url")

    # eventbrite_organizer: organizer profile page's inline "upcomingEvents":[...]
    # array (bracket-balanced out of the Next.js props, NOT __SERVER_DATA__).
    org = _json_mod.dumps([
        {"name": "Drag Brunch [Sold Out]", "start_date": iso, "start_time": "12:00:00",
         "end_date": iso, "end_time": "14:00:00", "is_online_event": False,
         "is_cancelled": False, "url": "https://www.eventbrite.com/e/y-tickets-1?aff=z",
         "ticket_availability": {"is_free": False,
                                 "minimum_ticket_price": {"display": "12.51 USD"}},
         "primary_venue": {"name": "Franky Bradley's", "address": {
             "city": "Philadelphia", "latitude": "39.95", "longitude": "-75.16",
             "localized_address_display": "1320 Chancellor St"}}},
        {"name": "Suburban Brunch", "start_date": iso, "start_time": "12:00:00",
         "is_online_event": False, "primary_venue": {"name": "The Rabbit Hole",
             "address": {"city": "Conshohocken"}}},
        {"name": "Cancelled Show", "start_date": iso, "is_cancelled": True,
         "primary_venue": {"name": "X"}},
        {"name": "Webinar", "start_date": iso, "is_online_event": True,
         "primary_venue": {"name": "Online"}},
    ])
    page = ('<html><script>{"organizer":{"name":"Henn"},"upcomingEvents":'
            + org + ',"upcomingEventsTotal":4}</script></html>')
    restore = _offline({"eventbrite.com": page})
    try:
        rows = scrapers.scrape_eventbrite_organizer(
            "https://www.eventbrite.com/o/henn-house-events-33705988809", "hh", start, end)
    finally:
        restore()
    ok(len(rows) == 2, f"organizer: online + cancelled dropped (got {len(rows)})")
    ok(rows[0]["name"] == "Drag Brunch [Sold Out]",
       "bracket-balancer survives a ']' inside an event title")
    ok(rows[0]["tags"] == "park:franky-bradley-s" and rows[0]["lat"] == 39.95,
       "organizer: venue routing tag + coords lifted")
    ok(rows[0]["cost"] == "12.51 USD" and rows[0]["end_time"] == "14:00",
       "organizer: ticket price + end time carried")
    ok(rows[0]["url"] == "https://www.eventbrite.com/e/y-tickets-1",
       "organizer: tracking query stripped")


def test_html_jsonld_itemlist_and_human_dates():
    start = dt.date.today()
    end = start + dt.timedelta(days=90)
    d1 = start + dt.timedelta(days=7)

    # html_jsonld now descends ItemList -> itemListElement[].item and tolerates a
    # human startDate. Both were silent zero-yield bugs on PCMS.
    ok(scrapers._jsonld_when("2026-10-16T19:30:00") == "2026-10-16T19:30:00",
       "ISO startDate passes through untouched")
    ok(scrapers._jsonld_when("October 16, 2026 At 7:30 pm") == "2026-10-16T19:30",
       f"human startDate parsed (got {scrapers._jsonld_when('October 16, 2026 At 7:30 pm')})")
    ok(scrapers._jsonld_when("March 08, 2027 At 2 PM") == "2027-03-08T14:00", "bare hour + caps PM")
    ok(scrapers._jsonld_when("next tuesday") == "", "unparseable date yields nothing, never a guess")

    listing = ('<script type="application/ld+json">' + _json_mod.dumps({
        "@type": "ItemList", "itemListElement": [
            {"@type": "ListItem", "item": {
                "@type": "MusicEvent", "name": "Takács Quartet",
                "startDate": f"{d1:%B %d, %Y} At 7:30 pm",
                "location": {"@type": "MusicVenue", "name": "Perelman Theater"},
                "url": "https://www.pcmsconcerts.org/concerts/#1"}},
            {"@type": "ListItem", "item": {
                "@type": "MusicEvent", "name": "Last Season",
                "startDate": "October 16, 2019 At 7:30 pm"}}]}) + "</script>")
    restore = _offline({"pcmsconcerts.org": listing})
    try:
        rows = scrapers.scrape_html_jsonld("https://www.pcmsconcerts.org/concerts/",
                                           "pcms", start, end)
    finally:
        restore()
    ok(len(rows) == 1 and rows[0]["name"] == "Takács Quartet",
       f"ItemList descended, out-of-window item dropped (got {[r['name'] for r in rows]})")
    ok(rows[0]["date"] == d1.isoformat() and rows[0]["time"] == "19:30", "human date + time landed")
    ok(rows[0]["venue"] == "Perelman Theater", "MusicVenue name lifted")

    # a BreadcrumbList must NOT be mistaken for an event list
    crumbs = ('<script type="application/ld+json">' + _json_mod.dumps(
        {"@type": "BreadcrumbList", "itemListElement": [
            {"@type": "ListItem", "item": {"@type": "WebPage", "name": "Home"}}]}) + "</script>")
    restore = _offline({"example.org": crumbs})
    try:
        ok(scrapers.scrape_html_jsonld("https://example.org/e", "x", start, end) == [],
           "BreadcrumbList is not descended into")
    finally:
        restore()


def test_html_jsonld_entity_routing():
    start = dt.date.today()
    end = start + dt.timedelta(days=90)
    d1 = start + dt.timedelta(days=5)

    # html_jsonld: an undecoded entity in location.name used to poison the routing
    # slug (park:johnny-brenda-8217-s never matches a coverage row — r5, 2026-08-16)
    page = ('<script type="application/ld+json">' + _json_mod.dumps({
        "@type": "MusicEvent", "name": "Times New Viking",
        "startDate": f"{d1.isoformat()}T20:00:00",
        "location": {"@type": "MusicVenue", "name": "Johnny Brenda&#8217;s",
                     "address": "1201 N Frankford Ave"}}) + "</script>")
    restore = _offline({"example.org": page})
    try:
        rows = scrapers.scrape_html_jsonld("https://example.org/events/", "r5", start, end)
    finally:
        restore()
    ok(len(rows) == 1 and rows[0]["venue"] == "Johnny Brenda’s",
       f"JSON-LD venue name entity-decoded (got {rows[0]['venue']!r})")
    ok(rows[0]["tags"] == "park:johnny-brenda-s",
       f"routing slug is clean after decoding (got {rows[0]['tags']!r})")


def test_squarespace_roving_group_and_page_collection():
    """Sante Philly (2026-08-23) is a GROUP-shaped Squarespace feed: one organizer,
    a different venue every time, carried in location.addressTitle/addressLine*.
    The generic adapter must fold the street address in and leave `venue` blank
    (the group path in ingest keeps venue_id NULL and lets the event roam).

    And the Mission Story Slam trap: `/events?format=json` on a Squarespace *page*
    (collection typeName='page') returns neither items[] nor upcoming[]/past[] —
    it must yield ZERO rows, not an exception, so the live gate honestly fails.
    """
    start = dt.date.today()
    end = start + dt.timedelta(days=90)
    d1 = start + dt.timedelta(days=5)
    ms = int(dt.datetime.combine(d1, dt.time(17, 0)).timestamp() * 1000)

    feed = _json_mod.dumps({"upcoming": [
        {"id": "e1", "title": "Sant&#233; at Rally Cafe", "startDate": ms,
         "endDate": ms + 2 * 3600 * 1000,
         "location": {"addressTitle": "Rally Coffee",
                      "addressLine1": "701 South 7th Street",
                      "addressLine2": "Philadelphia, Pennsylvania, 19147"},
         "fullUrl": "/public-events/sant-at-rally-cafe"}], "past": []})
    restore = _offline({"santephilly.com": feed})
    try:
        rows = scrapers.scrape_squarespace(
            "https://www.santephilly.com/public-events", "sante", start, end)
    finally:
        restore()
    ok(len(rows) == 1, f"roving squarespace group yields its event (got {len(rows)})")
    ok(rows[0]["venue"] == "",
       f"generic squarespace leaves venue blank for a group source (got {rows[0]['venue']!r})")
    ok(rows[0]["address"] == "701 South 7th Street, Philadelphia, Pennsylvania, 19147",
       f"host address folded from addressLine1+2 (got {rows[0]['address']!r})")
    ok(rows[0]["name"] == "Santé at Rally Cafe",
       f"title entity-decoded (got {rows[0]['name']!r})")

    page = _json_mod.dumps({"collection": {"typeName": "page", "type": 10,
                                           "itemCount": 0}, "mainContent": ""})
    restore = _offline({"missionstoryslam.org": page})
    try:
        rows = scrapers.scrape_squarespace(
            "https://missionstoryslam.org/events", "mss", start, end)
    finally:
        restore()
    ok(rows == [], "a Squarespace PAGE (no items[]/upcoming[]) yields zero rows, not a crash")


def test_ical_unescapes_rfc5545_text():
    """LibCal/DDay feeds escape commas and newlines in TEXT values. Those escapes
    used to leak verbatim into titles and descriptions ("Songs\\, Stories and
    Play") — 2026-08-29, Max (cambridge CPL). LOCATION stays raw on purpose:
    scrape_ppr_ical owns that unescape for its comma-split routing."""
    start = dt.date.today()
    end = start + dt.timedelta(days=90)
    d1 = start + dt.timedelta(days=3)
    ics = ("BEGIN:VCALENDAR\r\n"
           "BEGIN:VEVENT\r\n"
           f"DTSTART:{d1:%Y%m%d}T103000\r\n"
           "SUMMARY:Songs\\, Stories and Play (O'Neill)\r\n"
           "DESCRIPTION:Bring a friend\\, a book\\;\\nthen stay for snacks.\r\n"
           "LOCATION:Main Library\r\n"
           "END:VEVENT\r\n"
           "END:VCALENDAR\r\n")
    restore = _offline({"cambridgepl.libcal.com": ics})
    try:
        rows = scrapers.scrape_ical(
            "https://cambridgepl.libcal.com/ical_subscribe.php?src=c&cid=7052",
            "cpl", start, end)
    finally:
        restore()
    ok(len(rows) == 1, f"ical parses the VEVENT (got {len(rows)})")
    r = rows[0]
    ok(r["name"] == "Songs, Stories and Play (O'Neill)",
       f"SUMMARY backslash-escapes unescaped (got {r['name']!r})")
    ok("\\," not in r["description"] and "\\n" not in r["description"]
       and "a book;" in r["description"],
       f"DESCRIPTION backslash-escapes unescaped (got {r['description']!r})")
    ok(r["time"] == "10:30", f"naive DTSTART kept as wall-clock (got {r['time']!r})")


def test_crowdwork_dates_and_costs():
    start = dt.date.today()
    end = start + dt.timedelta(days=90)
    d1 = start + dt.timedelta(days=7)
    d2 = start + dt.timedelta(days=8)
    iso1, iso2 = d1.isoformat(), d2.isoformat()

    # crowdwork: dates[] fan-out, cents cost tiers, HTML description sanitized
    cw = _json_mod.dumps({"data": [
        {"name": "Astro Quest", "status": "active",
         "theatre": {"name": "SideQuest Theater"},
         "dates": [f"{iso1}T20:00:00.000-04:00", f"{iso2}T14:00:00.000-04:00",
                   "2019-01-01T20:00:00.000-05:00"],
         "cost": {"formatted": "$27.09 (includes fees)"},
         "cost_tiers": [{"name": "General", "cost": 2500, "cost_after_fees": 2709}],
         "description": {"body": "<div><strong>Sci-fi</strong> improv</div>"},
         "img": {"url": "https://cdn/x.png"}, "tags": {"public": ["Improv"]},
         "url": "https://www.crowdwork.com/e/astro-quest"},
        {"name": "Cancelled Show", "status": "inactive",
         "dates": [f"{iso1}T20:00:00.000-04:00"]},
    ]})
    restore = _offline({"crowdwork.com": cw})
    try:
        rows = scrapers.scrape_crowdwork(
            "https://crowdwork.com/api/v2/sidequesttheater/shows", "sq", start, end)
    finally:
        restore()
    ok(len(rows) == 2 and all(r["name"] == "Astro Quest" for r in rows),
       f"dates[] fanned out, past date + inactive show dropped (got {len(rows)})")
    ok(rows[0]["cost"] == "$25.00", f"pre-fee tier cents, not fee-inflated text (got {rows[0]['cost']})")
    ok(rows[0]["description"] == "Sci-fi improv", "HTML body sanitized")
    ok(rows[0]["time"] == "20:00" and rows[1]["time"] == "14:00", "showtimes preserved")
    ok(rows[0]["venue"] == "SideQuest Theater" and rows[0]["tags"] == "Improv",
       "theatre name + public tags lifted")


def test_p3_agile_feed():
    # the fixture is the Philadelphia Film Society's feed; the shipped map is empty
    scrapers.ticketing.AGILE_BUILDINGS.update({
        "1412-chestnut-street": "Philadelphia Film Center",
        "400-ranstead-st": "PFS Bourse Theater", "125-s-2nd-st": "PFS East Theater"})
    start = dt.date.today()
    end = start + dt.timedelta(days=90)
    d1 = start + dt.timedelta(days=7)
    iso1 = d1.isoformat()

    # agile_feed: CurrentShowings[] = event instances; per-SCREEN feed venues
    # collapse to buildings via AGILE_BUILDINGS (slugged Address1) for both the
    # routing tag AND the venue field (so --discover mints buildings, not screens)
    feed = _json_mod.dumps({"SourceLink": "x", "LastUpdated": "y", "ArrayOfShows": [
        {"Name": "A  BAY OF BLOOD", "Duration": "85",
         "ShortDescription": "<p>Slasher &amp; gore</p>",
         "EventImage": "https://img/bay.jpg",
         "InfoLink": "https://prod5.agileticketing.net/websales/pages/info.aspx?e=1",
         "CustomProperties": [
             {"Name": "Genre", "Value": "Horror"}, {"Name": "Rating", "Value": "R"}],
         "CurrentShowings": [
             {"StartDate": f"{iso1}T21:15:00", "EndDate": f"{iso1}T22:40:00",
              "SalesState": "DuringSales",
              "LegacyPurchaseLink": "https://prod5.agileticketing.net/websales/t?e=1",
              "Venue": {"VenueID": 1545, "Name": "PFC - Greenfield",
                        "Address1": "1412 Chestnut Street", "City": "Philadelphia",
                        "State": "PA", "Zip": "19102"}},
             {"StartDate": f"{iso1}T19:00:00",
              "Venue": {"VenueID": 1601, "Name": "PFS - Bourse Theater 3",
                        "Address1": "400 Ranstead St.", "City": "Philadelphia",
                        "Zip": "19106"}},
             {"StartDate": "2019-01-01T19:00:00",
              "Venue": {"Name": "PFC - Mainstage", "Address1": "1412 Chestnut Street"}},
             {"StartDate": f"{iso1}T12:00:00", "DateTBD": True,
              "Venue": {"Name": "PFC - Mainstage", "Address1": "1412 Chestnut Street"}}]},
        {"Name": "", "CurrentShowings": [{"StartDate": f"{iso1}T12:00:00"}]},
    ]})
    restore = _offline({"agileticketing.net": feed})
    try:
        rows = scrapers.scrape_agile_feed(
            "https://prod5.agileticketing.net/websales/feed.ashx?guid=g&showslist=true&format=json",
            "pfs", start, end)
    finally:
        restore()
    ok(len(rows) == 2, f"showings expanded; past, TBD + nameless dropped (got {len(rows)})")
    ok(rows[0]["name"] == "A BAY OF BLOOD", "show name whitespace collapsed")
    ok(rows[0]["venue"] == "Philadelphia Film Center"
       and rows[0]["tags"] == "Horror, park:philadelphia-film-center",
       f"PFC screen collapsed to the building row (got {rows[0]['tags']})")
    ok(rows[1]["venue"] == "PFS Bourse Theater"
       and rows[1]["tags"].endswith("park:pfs-bourse-theater"),
       f"Bourse screen 3 collapsed to one building key (got {rows[1]['tags']})")
    ok(rows[0]["time"] == "21:15" and rows[0]["end_time"] == "22:40",
       "naive-ET showtime + same-day end time kept")
    ok(rows[0]["url"].endswith("t?e=1"), "per-showing LegacyPurchaseLink preferred")
    ok(rows[0]["description"] == "Slasher & gore", "ShortDescription sanitized")
    ok(rows[0]["address"] == "1412 Chestnut Street, Philadelphia, 19102",
       "address assembled from feed parts")

    # rotated/empty GUID = valid JSON shell with NO ArrayOfShows -> raises, so
    # the build gate stamps parse_error instead of a silent no_events
    restore = _offline({"agileticketing.net":
                        '{"SourceLink": "x", "LastUpdated": "y"}'})
    try:
        try:
            scrapers.scrape_agile_feed(
                "https://prod5.agileticketing.net/websales/feed.ashx?guid=rotated",
                "pfs", start, end)
            raised = False
        except ValueError:
            raised = True
    finally:
        restore()
    ok(raised, "empty feed shell (rotated GUID) raises -> gate sees parse_error")


def test_eventive_bucket_feed():
    """eventive (cinéSPEAK first): one bucket-events GET, UTC instants -> venue
    wall clock, aggregator routing on venue.name (Philly source "cinespeak")."""
    # the conversion helper alone, on fixed dates (the wix-test pattern)
    ok(scrapers._eventive_local("2026-08-20T23:00:00.000Z", "America/New_York")
       == dt.datetime(2026, 8, 20, 19), "UTC instant becomes Eastern wall clock")
    ok(scrapers._eventive_local("2026-12-20T23:00:00.000Z", "America/New_York")
       == dt.datetime(2026, 12, 20, 18), "conversion respects DST")
    ok(scrapers._eventive_local("2026-08-21T01:00:00.000Z", "America/New_York")
       == dt.datetime(2026, 8, 20, 21), "next-day UTC lands back on the show's date")
    ok(scrapers._eventive_local("2026-08-20T23:00:00.000Z", "Bad/Zone")
       == dt.datetime(2026, 8, 20, 19), "unknown tz falls back to the engine zone")
    ok(scrapers._eventive_local("nope", "America/New_York") is None,
       "invalid timestamp is rejected, never guessed")

    start = dt.date.today()
    end = start + dt.timedelta(days=90)
    d1 = start + dt.timedelta(days=7)
    tz = scrapers.ZoneInfo("America/New_York")

    def utc(day, hh):        # desired ET wall clock -> the feed's UTC instant
        return (dt.datetime(day.year, day.month, day.day, hh, tzinfo=tz)
                .astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"))

    feed = _json_mod.dumps({"events": [
        {"id": "abc123", "name": "  cinéSPEAK presents She's\n the He!  ",
         "start_time": utc(d1, 21), "end_time": utc(d1, 23),
         "timezone": "America/New_York", "visibility": "visible",
         "short_description": "",
         "description": "<p>Queer shorts &amp; a talkback</p>",
         # trailing space is real (feed has "Kingsessing Library "); the routing
         # slug must still match the clean venue id
         "venue": {"name": "Kingsessing Library ",
                   "address": "1201 South 51st Street\nPhiladelphia, PA 19143"}},
        {"id": "old1", "name": "Ancient Screening", "start_time": "2019-01-01T23:00:00.000Z",
         "timezone": "America/New_York", "venue": {"name": "Vox Populi"}},
        {"id": "virt", "name": "Watch-at-Home Night", "start_time": utc(d1, 19),
         "timezone": "America/New_York", "is_virtual": True,
         "venue": {"name": "Vox Populi"}},
        {"id": "hid", "name": "Members Preview", "start_time": utc(d1, 19),
         "timezone": "America/New_York", "visibility": "hidden",
         "venue": {"name": "Vox Populi"}},
        {"id": "anon", "name": "", "start_time": utc(d1, 19)},
    ]})
    restore = _offline({"api.eventive.org": feed})
    try:
        rows = scrapers.scrape_eventive(
            "https://api.eventive.org/event_buckets/b1/events?api_key=k", "cs",
            start, end)
    finally:
        restore()
    ok(len(rows) == 1, f"past, virtual, hidden + nameless dropped (got {len(rows)})")
    r = rows[0]
    ok(r["name"] == "cinéSPEAK presents She's the He!", "name whitespace collapsed")
    ok(r["date"] == d1.isoformat() and r["time"] == "21:00" and r["end_time"] == "23:00",
       f"UTC start/end become local wall clock ({r['date']} {r['time']}-{r['end_time']})")
    ok(r["venue"] == "Kingsessing Library"
       and r["tags"] == "park:kingsessing-library",
       f"venue stripped so the routing slug matches the venue id (got {r['tags']})")
    ok(r["address"] == "1201 South 51st Street, Philadelphia, PA 19143",
       "feed's newline address flattened")
    ok(r["url"] == "https://cinespeak.eventive.org/schedule/abc123",
       "public event page built from the schedule root + id")
    ok(r["description"] == "Queer shorts & a talkback" and r["category"] == "Film",
       "empty short_description falls through to sanitized description")


def test_ticketleap_organizer_page():
    """ticketleap (Mouth Of Rain first): the listings JSON is bracket-matched out
    of the organizer page HTML; server_event_start_ts is a TRUE UTC epoch and is
    preferred over the naive wall string; venue_city filters to Philadelphia
    (Philly source "mouthofrain-ticketleap")."""
    start = dt.date.today()
    end = start + dt.timedelta(days=90)
    d1 = start + dt.timedelta(days=7)
    tz = scrapers.ZoneInfo("America/New_York")

    def epoch(day, hh):      # desired ET wall clock -> the blob's UTC epoch
        return int(dt.datetime(day.year, day.month, day.day, hh,
                               tzinfo=tz).timestamp())

    listings = [
        # naive strings DELIBERATELY wrong (09:00) — the epoch must win
        {"type": "listing", "event_start": f"{d1} 09:00:00",
         "event_end": f"{d1} 11:30:00",
         "server_event_start_ts": epoch(d1, 21),
         "server_event_end_ts": epoch(d1, 23),
         "venue_name": "Val's Lesbian Bar", "venue_city": "Philadelphia",
         "listing_title": "BLACK OUT ] A PARTY [ FOR BLACK QUEER PEOPLE",
         "listing_slug": "mouthofrain/black-out-452433510",
         "min_price": "10.00", "min_price_fmt": "$11.55",
         "image": "//tlt-events.s3.amazonaws.com/139882/x.jpg",
         "sold_out": False},
        # no epoch -> falls back to the naive wall string; 0.00 -> Free
        {"type": "listing", "event_start": f"{d1} 18:00:00",
         "server_event_start_ts": None, "venue_name": "CLUB 624",
         "venue_city": "Philadelphia", "listing_title": "FIELD DAY",
         "listing_slug": "mouthofrain/field-day-1", "min_price": "0.00"},
        # not Philadelphia -> dropped
        {"type": "listing", "event_start": f"{d1} 20:00:00",
         "server_event_start_ts": epoch(d1, 20),
         "venue_name": "C'mon Everybody", "venue_city": "Brooklyn",
         "listing_title": "NYC TAKEOVER", "listing_slug": "mouthofrain/nyc",
         "min_price": "15.00"},
        # past -> dropped
        {"type": "listing", "event_start": "2019-06-01 21:00:00",
         "server_event_start_ts": 1559437200, "venue_name": "Val's Lesbian Bar",
         "venue_city": "Philadelphia", "listing_title": "ARCHIVE PARTY",
         "listing_slug": "mouthofrain/old", "min_price": "5.00"},
    ]
    # mimic the real page: minified JSON, an organizationInfo blob (with its own
    # arrays) BEFORE the listings array, TicketLeap's \/ slash-escaping throughout
    blob = _json_mod.dumps(listings, separators=(",", ":")).replace("/", "\\/")
    page = ('<html><script>window.__data = [{"organizationInfo":'
            '{"name":"Mouth Of Rain","links":["https:\\/\\/x"]},'
            '"copyrightName":"TicketLeap"},\n        ' + blob + ',\n'
            '{"footer":true}];</script></html>')
    restore = _offline({"events.ticketleap.com": page})
    try:
        rows = scrapers.scrape_ticketleap(
            "https://events.ticketleap.com/tickets/mouthofrain", "mor", start, end)
    finally:
        restore()
    ok(len(rows) == 2, f"non-Philly + past listings dropped (got {len(rows)})")
    r = rows[0]
    ok(r["date"] == d1.isoformat() and r["time"] == "21:00" and r["end_time"] == "23:00",
       f"UTC epoch wins over the (wrong) naive wall string ({r['date']} {r['time']}-{r['end_time']})")
    ok(r["name"] == "BLACK OUT ] A PARTY [ FOR BLACK QUEER PEOPLE",
       "brackets inside JSON strings don't break the bracket-matcher")
    ok(r["cost"] == "$10.00", f"min_price (pre-fee, not min_price_fmt) -> cost text (got {r['cost']!r})")
    ok(r["url"] == "https://events.ticketleap.com/tickets/mouthofrain/black-out-452433510",
       "event url built from listing_slug")
    ok(r["image"] == "https://tlt-events.s3.amazonaws.com/139882/x.jpg",
       "protocol-relative image absolutized")
    ok(r["venue"] == "Val's Lesbian Bar" and r["tags"] == "park:val-s-lesbian-bar",
       f"venue kept + routing tag emitted (got {r['tags']})")
    r2 = rows[1]
    ok(r2["time"] == "18:00", "missing epoch falls back to the naive wall string")
    ok(r2["cost"] == "Free", "min_price 0.00 -> Free")

    # no listings block at all -> [] (dormant organizer), never an exception
    restore = _offline({"events.ticketleap.com": "<html>nothing here</html>"})
    try:
        ok(scrapers.scrape_ticketleap("https://events.ticketleap.com/tickets/x",
                                      "mor", start, end) == [],
           "page without a listings blob yields no events")
    finally:
        restore()


def test_wp_jsonld_pages_two_step():
    """wp_jsonld_pages (Giovanni's Room first): WP REST list supplies links only;
    dates come from each single page — EventON's data-time epochs are WALL CLOCK
    encoded as-if-UTC and must beat the hour-skewed, non-zero-padded JSON-LD
    startDate; JSON-LD is the fallback when data-time is absent
    (Philly source "giovannis-room")."""
    start = dt.date.today()
    end = start + dt.timedelta(days=90)
    d1 = start + dt.timedelta(days=7)

    def wall_epoch(day, hh, mm=0):   # ET wall clock encoded as if it were UTC
        return int(dt.datetime(day.year, day.month, day.day, hh, mm,
                               tzinfo=dt.timezone.utc).timestamp())

    posts = _json_mod.dumps([
        {"link": "https://queerbooks.com/events/epoch-wins/",
         "title": {"rendered": "REST Title A"}},
        {"link": "https://queerbooks.com/events/loose-only/",
         "title": {"rendered": "Craft &#038; Consciousness"}},
        {"link": "https://queerbooks.com/events/old-one/",
         "title": {"rendered": "Archive Event"}},
    ])
    # page 1: data-time (18:00-19:30 as-if-UTC) AND a deliberately hour-skewed,
    # non-padded JSON-LD (19:00) — the epoch pair must win, like on the live site
    p1 = (f'<html><div data-time="{wall_epoch(d1, 18)}-{wall_epoch(d1, 19, 30)}">'
          f'</div><script type="application/ld+json">'
          + _json_mod.dumps({
              "@type": "Event", "name": "Kim Narby Presents SATURN RETURNING",
              "startDate": f"{d1.year}-{d1.month}-{d1.day}T19:00-4:00",
              "endDate": f"{d1.year}-{d1.month}-{d1.day}T20:30-4:00",
              "image": "https://queerbooks.com/x.png",
              "description": "<strong>Join us</strong> for a reading",
              "url": "https://queerbooks.com/events/epoch-wins/"})
          + "</script></html>")
    # page 2: NO data-time; non-padded JSON-LD only, empty name -> REST title
    p2 = ('<html><script type="application/ld+json">'
          + _json_mod.dumps({
              "@type": "Event", "name": "",
              "startDate": f"{d1.year}-{d1.month}-{d1.day}T18:30-4:00"})
          + "</script></html>")
    # page 3: past event -> dropped by the window filter
    p3 = '<html><div data-time="1559437200-1559442600"></div></html>'
    restore = _offline({"ajde_events": posts, "epoch-wins": p1,
                        "loose-only": p2, "old-one": p3})
    try:
        rows = scrapers.scrape_wp_jsonld_pages(
            "https://queerbooks.com/wp-json/wp/v2/ajde_events?per_page=20",
            "gr", start, end)
        capped = scrapers.scrape_wp_jsonld_pages(
            "https://queerbooks.com/wp-json/wp/v2/ajde_events?per_page=20",
            "gr", start, end, max_events=1)
    finally:
        restore()
    ok(len(rows) == 2, f"past event dropped (got {len(rows)})")
    r = rows[0]
    ok(r["date"] == d1.isoformat() and r["time"] == "18:00" and r["end_time"] == "19:30",
       f"data-time epochs read as wall-clock-as-UTC beat the skewed JSON-LD "
       f"({r['date']} {r['time']}-{r['end_time']})")
    ok(r["name"] == "Kim Narby Presents SATURN RETURNING", "name from the JSON-LD")
    ok(r["description"] == "Join us for a reading", "description sanitized")
    ok(r["image"] == "https://queerbooks.com/x.png"
       and r["url"] == "https://queerbooks.com/events/epoch-wins/",
       "image + url lifted from the JSON-LD")
    r2 = rows[1]
    ok(r2["date"] == d1.isoformat() and r2["time"] == "18:30",
       f"non-padded JSON-LD date parsed leniently ({r2['date']} {r2['time']})")
    ok(r2["name"] == "Craft & Consciousness",
       "empty JSON-LD name falls back to the REST title, entities decoded")
    ok(len(capped) == 1, "max_events hard-caps the per-run page fetches")
    ok(scrapers._loose_when("2026-8-3") == "2026-08-03"
       and scrapers._loose_when("not a date") == ""
       and scrapers._loose_when(None) == "",
       "_loose_when: date-only padded, junk and None -> ''")


def test_html_jsonld_mec_inline_dates():
    """south-street-district's planned shape: Modern Events Calendar injects full
    Event JSON-LD blocks inline on the LISTING page with DATE-ONLY startDates.
    html_jsonld must keep them (time ''), emit the location.name routing tag for
    aggregator fan-out, and not double-count when a detail link repeats the same
    event (Philly source "south-street-district")."""
    start = dt.date.today()
    end = start + dt.timedelta(days=30)
    d1 = start + dt.timedelta(days=2)

    block = _json_mod.dumps({
        "@context": "https://schema.org", "@type": "Event",
        "name": "Quizzo at Marsha's", "startDate": d1.isoformat(),
        "endDate": d1.isoformat(),
        "location": {"@type": "Place", "name": "Marsha&#8217;s",
                     "address": "430 South St, Philadelphia, PA 19147"},
        "offers": {"@type": "Offer", "price": "0"},
        "url": "https://southstreet.com/events/quizzo-at-marshas/"})
    listing = (f'<html><a href="/events/quizzo-at-marshas/">Quizzo</a>'
               f'<script type="application/ld+json">{block}</script></html>')
    detail = f'<html><script type="application/ld+json">{block}</script></html>'

    restore = _offline({"events-calendar": listing, "quizzo-at-marshas": detail})
    try:
        rows = scrapers.scrape_html_jsonld("https://southstreet.com/events-calendar/",
                                           "south-street-district", start, end)
    finally:
        restore()
    ok(len(rows) == 1, "inline listing event not double-counted via its detail page")
    r = rows[0]
    ok(r["date"] == d1.isoformat() and r["time"] == "",
       "MEC date-only startDate keeps the event with no time")
    ok(r["tags"] == "park:marsha-s",
       "location.name routes (entity decoded before slugging)")
    ok(r["address"].startswith("430 South St"), "string address carried")


# ── scorer plumbing: --pending / --import (no API anywhere) ─────────────────

def test_scorer_pending_and_import():
    import json as _json
    import tempfile

    import scorer
    from models import EventScore, Series
    from series import derive_series

    s = Session()
    v = add_venue_lead("Rater Venue", neighborhood="Fishtown",
                       discovered_via="manual", sess=s)
    base = dt.datetime.now().replace(hour=19, minute=0, second=0, microsecond=0)
    for i in range(3):                                     # a weekly series
        s.add(Event(dedupe_key=f"rv|w{i}", name="Bridge Club",
                    start=base + dt.timedelta(days=7 * (i + 1)), venue_id=v.id))
    s.add(Event(dedupe_key="rv|solo", name="One Night Only",
                start=base + dt.timedelta(days=3), venue_id=v.id))
    s.flush()
    derive_series(s)
    s.flush()

    p = scorer.pending(s)
    ok(p["n_units"] == 2 and p["n_events"] == 4,
       f"series-first: 4 events -> 2 judgments (got {p['n_units']}/{p['n_events']})")
    ok(p["n_inherit"] == 2, "the 2 extra series instances inherit")
    kinds = {u["kind"] for u in p["batches"][0]["units"]}
    ok(kinds == {"series", "event"}, "both unit kinds emitted")
    sid = next(u["unit_id"] for u in p["batches"][0]["units"] if u["kind"] == "series")
    eid = next(u["unit_id"] for u in p["batches"][0]["units"] if u["kind"] == "event")
    ok(sid.startswith("series:") and eid == "event:rv|solo", "unit ids are addressable")

    def judgment(unit_id, n):
        d = {"unit_id": unit_id, "category": "community", "description_quality": "thin"}
        for dim in scorer.UNIVERSAL:
            d[dim] = {"score": n, "reason": f"because {dim}"}
        d["value_for_price"] = {"score": n, "reason": "free"}
        return d

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        _json.dump({"scores": [judgment(sid, 8), judgment(eid, 4)]}, fh)
        path = fh.name

    ok(scorer.import_scores(s, path) == 0, "import returns success")
    rows = s.query(EventScore).all()
    ok(len(rows) == 4, f"one score row per member event (got {len(rows)})")
    ok({r.dedupe_key for r in rows} == {"rv|w0", "rv|w1", "rv|w2", "rv|solo"},
       "series judgment inherited by every instance")
    club = s.query(EventScore).filter_by(dedupe_key="rv|w1").one()
    ok(club.social_quality == 8 and club.quality is not None, "dims + composite written")
    ok(club.reasons["serendipity"] == "because serendipity", "reasons round-trip")

    scorer.import_scores(s, path)
    ok(s.query(EventScore).count() == 4, "re-import of the same file is a NO-OP")

    bad = judgment(eid, 4)
    bad["social_quality"] = {"score": 99, "reason": "out of range"}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        _json.dump({"scores": [bad]}, fh)
        badpath = fh.name
    ok(scorer.import_scores(s, badpath) == 1, "out-of-range score is rejected")
    ok(s.query(EventScore).count() == 4, "a rejected file writes NOTHING")

    ok(scorer.pending(s)["n_units"] == 0, "everything scored -> pending is empty")

    # cleanup: import_scores commits, so undo it explicitly (in-memory DB is shared)
    s.query(EventScore).delete()
    s.query(Event).filter(Event.dedupe_key.like("rv|%")).delete(synchronize_session=False)
    for row in s.query(Series).all():
        s.delete(row)
    s.query(Venue).filter_by(id=v.id).delete()
    s.commit()


def test_sniff_platform_fingerprints():
    """sniff.py: pure classify/verdict logic + offline endpoint-confirm flow."""
    import probe_sources
    import sniff

    tec_body = ('<html><head><meta name="generator" content="WordPress 6.4"></head>'
                '<link rel="stylesheet" href="/wp-content/plugins/the-events-calendar/x.css">'
                '<a href="/events/?ical=1">export</a>'
                '<script type="application/ld+json">{"@type":"Event","name":"A"}</script>'
                '</html>')
    c = probe_sources.classify_html(tec_body)
    ok("wordpress" in c["platforms"] and "the_events_calendar" in c["platforms"],
       "classify_html: WP + TEC markers")
    ok(c["generator"] == "WordPress 6.4", "classify_html: generator meta")
    ok(c["jsonld_event_count"] == 1 and c["ical_links"] == ["/events/?ical=1"],
       "classify_html: jsonld + ical link")

    a, e, _ = sniff.html_verdict({"platforms": ["wordpress", "wix"]}, "https://x.com/cal")
    ok(a == "wix" and e == "https://x.com", "html_verdict: wix beats bare wordpress")
    a, e, _ = sniff.html_verdict({"platforms": ["eventbrite"]}, "https://y.com/")
    ok(a == "eventbrite*", "html_verdict: embed verdict is starred (needs per-site look)")
    a, e, _ = sniff.html_verdict(
        {"platforms": [], "jsonld_event_count": 0, "ical_links": ["cal.ics"]},
        "https://z.com/events/")
    ok(a == "ical" and e == "https://z.com/events/cal.ics",
       "html_verdict: relative ical link resolved against page url")
    a, _, _ = sniff.html_verdict({"platforms": []}, "https://q.com/")
    ok(a == "?", "html_verdict: honest unknown")

    responses = {
        "https://wp.example.org/events/": (200, "text/html", tec_body),
        "https://wp.example.org/wp-json/tribe/events/v1/events?per_page=1":
            (200, "application/json", '{"events":[{"title":"X"}],"total":42}'),
        "https://sq.example.com/calendar": (200, "text/html",
            '<html><script src="https://static1.squarespace.com/x.js"></script></html>'),
        "https://sq.example.com/calendar?format=json": (200, "application/json",
            '{"items":[{"startDate":1735689600000,"title":"Y"}]}'),
    }

    def fake_get(url, timeout=15):
        return responses.get(url, (404, "", None))

    real = probe_sources.get, sniff.get
    probe_sources.get = sniff.get = fake_get
    try:
        v = sniff.sniff_site("https://wp.example.org/events/")
        ok(v["adapter"] == "tribe_wp" and v["endpoint"] == "https://wp.example.org"
           and "total=42" in v["evidence"], "sniff_site: TEC page -> confirmed tribe_wp root")
        v = sniff.sniff_site("https://sq.example.com/calendar")
        ok(v["adapter"] == "squarespace"
           and v["endpoint"] == "https://sq.example.com/calendar",
           "sniff_site: squarespace ?format=json confirmed on the page itself")
        v = sniff.sniff_site("https://gone.example.com/")
        ok(v["adapter"] == "unreachable", "sniff_site: dead site is an honest verdict")
    finally:
        probe_sources.get, sniff.get = real


def test_beventi_vendor_devalue_payload():
    """beventi_vendor (The Broken Spine first): the Nuxt 3 `_payload.json` is a
    devalue FLAT ARRAY — ints are back-references into the same array and the graph
    has CYCLES (an event's top_profiles points back at the vendor), so the resolver
    must be depth-capped or it never returns. The event list key carries the
    vendor's profile id, so it's found by PREFIX. Times are already venue wall
    clock; date_end is authoritative and rolls past midnight
    (Philly source "the-broken-spine-beventi")."""
    start = dt.date.today()
    end = start + dt.timedelta(days=90)
    d1 = start + dt.timedelta(days=10)
    d2 = d1 + dt.timedelta(days=1)

    # every non-literal value in a devalue payload is an INDEX (8 holds `false`)
    def event(idx_desc, title, venue, ds, de, t1, t2):
        return {"title": title, "description": idx_desc, "venue_name": venue,
                "address_line1": "915 North 28th Street", "address_line2": "Suite 1C",
                "city": "Philadelphia", "state": "PA", "postal_code": "19130",
                "latitude": "39.97350640", "longitude": "-75.18164580",
                "date_start": ds, "date_end": de, "start_time": t1, "end_time": t2,
                "timezone": "America/New_York", "status": "published",
                "private": 8, "is_virtual": 8,
                "image": "https://assets.beventi.co/x.png",
                "top_profiles": 3}          # index 3 -> the vendor -> back here: a CYCLE
    flat = [
        {"data": 1},                                             # 0
        ["ShallowReactive", 2],                                  # 1 (reactivity wrapper)
        {"author-profilebrokenspine": 3, "orderForms-profile-42506": 4},  # 2
        {"username": "brokenspine", "events": 4},                # 3 vendor -> forms (cycle)
        [5, 6, 7],                                               # 4 the order forms
        {"uid": "aaa111", "event": event("<b>Rollover</b> party", "ACOTAR 6 Book Release Party",
                                         "The Broken Spine", d1.isoformat(), d2.isoformat(),
                                         "22:00", "00:00"),
         "ticket_badges": {"availability": {"code": "sold_out", "label": "Sold out"},
                           "pricing": None}},                    # 5
        {"uid": "bbb222", "event": event("Book fair", "Live Sports Romance Podcast",
                                         "Love City Brewing", d1.isoformat(), d1.isoformat(),
                                         "14:00", "18:00"),
         "ticket_badges": {"availability": None,
                           "pricing": {"label": "Tickets from $25.00", "amount": 25}}},  # 6
        {"uid": "ccc333", "event": event("old", "Past Signing", "The Broken Spine",
                                         "2024-01-05", "2024-01-05", "19:00", "21:00"),
         "ticket_badges": {}},                                   # 7 out of window -> dropped
        False,                                                   # 8 a literal
    ]
    url = "https://beventi.co/vendors/brokenspine/_payload.json"
    restore = _offline({"_payload.json": _json_mod.dumps(flat)})
    try:
        rows = scrapers.scrape_beventi_vendor(url, "bs", start, end)
        capped = scrapers.scrape_beventi_vendor(url, "bs", start, end, max_events=1)
    finally:
        restore()

    ok(len(rows) == 2, f"cyclic payload resolves and past event dropped (got {len(rows)})")
    a = rows[0]
    ok(a["date"] == d1.isoformat() and a["time"] == "22:00"
       and a["end_date"] == d2.isoformat() and a["end_time"] == "00:00",
       f"date_end is authoritative across midnight ({a['end_date']} {a['end_time']})")
    ok(a["venue"] == "The Broken Spine" and a["tags"] == "park:the-broken-spine",
       "routing tag minted from the event's own venue_name")
    ok(a["address"] == "915 North 28th Street, Suite 1C, Philadelphia, PA 19130",
       f"address assembled from the payload parts ({a['address']})")
    ok(a["lat"] == 39.9735064 and a["lng"] == -75.1816458, "lat/lng floats")
    ok(a["cost"] == "Sold out", "cost falls back to the availability badge")
    ok(a["url"] == "https://beventi.co/tickets/aaa111", "ticket url from the form uid")
    ok(a["description"] == "Rollover party", "description sanitized")
    b = rows[1]
    ok(b["tags"] == "park:love-city-brewing" and b["cost"] == "Tickets from $25.00",
       "second host venue routes separately; pricing label preferred")
    ok(len(capped) == 1, "max_events hard-caps the rows returned")
    # a pure 2-cycle: without the depth cap this recurses forever
    ok(scrapers._devalue([{"a": 1}, {"b": 0}], max_depth=4)
       == {"a": {"b": {"a": {"b": {"a": None}}}}},
       "the depth cap terminates a cyclic reference graph")


def test_signup_software_storefront():
    """signup.software: one POST /query/event, Prisma shape. UTC times localize
    with the VENUE's tz (Jazz Attack's rows carry America/Detroit == 20:00 ET),
    the redundant "M/D " title prefix is dropped, cents become a $ price, series
    rows (multi-week courses that duplicate the weekly drop-in) are off by
    default, and the exclude= regex kills the second ticket product sold for the
    same night. Config rides the store url's query string."""
    start = dt.date.today()
    end = start + dt.timedelta(days=30)
    d1 = start + dt.timedelta(days=4)
    utc = d1 + dt.timedelta(days=1)   # 00:00Z on the NEXT day == 20:00 ET on d1

    def row(i, name, hh, series=False, price=1200):
        return {"id": i, "name": name, "isSeries": series, "price": price,
                "eventType": "groupClass", "cancelled": False,
                "startTime": f"{utc.isoformat()}T{hh:02d}:00:00.000Z",
                "endTime": f"{utc.isoformat()}T{hh + 3:02d}:00:00.000Z",
                "description": "<p>Swing</p>", "headline": "Drop in",
                "mediaUrl": "https://x/i.jpg",
                "venue": {"id": 12, "name": "Boys Choir", "address": "1336 Spring Garden St",
                          "timezone": "America/Detroit"}}

    seen = {}

    def fake_post(url, payload, headers=None, timeout=20):
        seen["url"], seen["payload"] = url, payload
        return {"data": [row(1, f"{d1:%-m/%-d} Lindy 101 Lesson & Social Dance", 0),
                         row(2, f"{d1:%-m/%-d} Just the Dance!", 1),
                         row(3, "Thursday Level 1 Series", 0, series=True, price=7500),
                         row(4, "Free Jam", 0, price=0)]}

    real = scrapers.core.post_json
    scrapers.core.post_json = fake_post
    try:
        rows = scrapers.scrape_signup_software(
            "https://signup.software/store/2/events?venue=12&exclude=just+the+dance",
            "ja", start, end)
        with_series = scrapers.scrape_signup_software(
            "https://signup.software/store/2/events?venue=12&series=1", "ja", start, end)
        nostore = scrapers.scrape_signup_software("https://signup.software/x", "ja", start, end)
    finally:
        scrapers.core.post_json = real

    ok(seen["url"] == "https://api.signup.software/query/event", "single Prisma query endpoint")
    ok(seen["payload"]["where"]["organizationId"] == 2, "org id read off /store/<id>/")
    ok(seen["payload"]["where"]["venueId"] == 12, "venue= narrows the storefront to one room")
    ok(seen["payload"]["take"] == 200, "take is the safety cap, not unbounded")
    ok([r["name"] for r in rows] == ["Lindy 101 Lesson & Social Dance", "Free Jam"],
       f"exclude= and series default drop the duplicates ({[r['name'] for r in rows]})")
    r = rows[0]
    ok(r["date"] == d1.isoformat() and r["time"] == "20:00",
       f"UTC midnight -> 20:00 in the venue tz ({r['date']} {r['time']})")
    ok(r["end_time"] == "23:00", "end time localizes too")
    ok(r["cost"] == "$12.00" and rows[1]["cost"] == "Free", "cents -> price, 0 -> Free")
    ok(r["address"] == "1336 Spring Garden St" and "<p>" not in r["description"],
       "venue address joined in, description plain")
    ok(r["url"] == "https://signup.software/store/2/events/1", "per-event storefront url")
    ok(len(with_series) == 4 and any(x["name"] == "Thursday Level 1 Series" for x in with_series),
       "series=1 opts the multi-week courses back in")
    ok(nostore == [], "a url with no /store/<id>/ yields nothing, not a crash")


def test_xtrulink_month_grid():
    """XTruLink (ticketapp.org) HTML month grid: day cells route on data-date,
    muted adjacent-month days overlap across month fetches (dedupe), 12-hour
    times parse to 24h, rich-HTML descriptions come out plain, and the fake-200
    login page (no XTL-event markers) is treated as no data — never parsed."""
    def cell(date, *events):
        return f'<td><div class="XTL-events" data-date="{date}" style="x">' + \
               "".join(events) + "</div></td>"

    def event(name, link, tm, desc="", img=""):
        e = (f'<div class="XTL-event mobilehide"><p class="XTL-event-month-time">{tm}</p>'
             f'<p class="XTL-event-month-name">{name}</p></div>'
             f'<div class="XTL-eventbox"><div class="XTL-event-name">'
             f'<a href="{link}" class="XTL-link" target="_top">{name}</a></div>'
             f'<div class="XTL-event-time">{tm}</div>')
        if img:
            e += f'<img class="mobilehide XTL-event-image" src="{img}">'
        if desc:
            e += f'<div class="mobilehide XTL-event-description">{desc}</div>'
        return e + "</div>"

    tix = "https://x.ticketapp.org/portal/product/60/event/abc"
    grid_oct = "<table>" + cell(
        "2026-09-27", event("Out Of Window", tix, "10:00am - 5:00pm")) + cell(
        "2026-10-05",
        event("Weekend Artmaking", tix, "10:00am - 5:00pm",
              desc='<p><strong>Location:</strong> Maguire Hall</p><p>Drop in!</p>',
              img="https://x.ticketapp.org/custom_upload/images/Children's Studio.jpg"),
        event("Oil Painting (1/4)", tix + "2", "1:30pm - 4:30pm")) + cell(
        "2026-10-31", event("Halloween Jazz", tix + "3", "7pm - 9pm")) + "</table>"
    grid_nov = "<table>" + cell(                      # Oct 31 repeats, muted
        "2026-10-31", event("Halloween Jazz", tix + "3", "7pm - 9pm")) + cell(
        "2026-11-02", event("Film Night", tix + "4", "6:00pm - 9:00pm")) + "</table>"
    tmpl = "https://x.ticketapp.org/api/calendar/data/uuid/?v=month&m={m}&d=1&y={y}"

    restore = _offline({"m=10&": grid_oct, "m=11&": grid_nov})
    try:
        rows = scrapers.scrape_xtrulink(tmpl, "wm", dt.date(2026, 10, 1),
                                        dt.date(2026, 11, 5))
    finally:
        restore()
    ok([r["name"] for r in rows] == ["Weekend Artmaking", "Oil Painting (1/4)",
                                     "Halloween Jazz", "Film Night"],
       f"in-window events kept, muted out-of-window + cross-month repeats dropped "
       f"({[r['name'] for r in rows]})")
    a = rows[0]
    ok(a["date"] == "2026-10-05" and a["time"] == "10:00" and a["end_time"] == "17:00",
       f"12-hour range -> 24h start/end ({a['time']}-{a['end_time']})")
    ok(a["url"] == tix, "per-event ticket url from the XTL-link")
    ok(a["description"] == "Location: Maguire Hall Drop in!",
       f"rich-HTML description stripped to plain text ({a['description']!r})")
    ok(" " not in a["image"] and "Children's%20Studio" in a["image"],
       "image url spaces percent-encoded")
    ok(rows[2]["time"] == "19:00", "minute-less '7pm' parses")

    # fake-200 login page: HTTP 200, zero XTL-event markers -> no rows, no parse
    restore = _offline({"m=10&": "<html><h2>XTruLink Dashboard Login</h2></html>"})
    try:
        rows = scrapers.scrape_xtrulink(tmpl, "wm", dt.date(2026, 10, 1),
                                        dt.date(2026, 10, 31))
    finally:
        restore()
    ok(rows == [], "login page (no XTL-event markers) yields no events")


def test_html_jsonld_paged_inline():
    """html_jsonld_paged walks page=1..N of an inline-JSON-LD listing (one fetch
    per page, no detail fan-out), dedupes on (name, date), and stops at the
    first page that adds nothing — page 4 is never fetched (_offline raises on
    unexpected urls)."""
    def ld(name, date, venue):
        return ('<script type="application/ld+json">' + _json_mod.dumps(
            {"@context": "https://schema.org", "@type": "Event", "name": name,
             "startDate": f"{date}T00:00:00-04:00",
             "location": {"@type": "Place", "name": venue},
             "url": f"https://festival.example.org/events/{scrapers._slug(name)}"})
            + "</script>")

    base = "https://festival.example.org/events?daterange=09%2F30%2F2026+-+10%2F11%2F2026"
    p1 = "<html>" + ld("Kickoff", "2026-09-30", "Forman Arts Initiative") + \
         ld("En Su SITIO", "2026-10-01", "Taller Puertorriqueno") + "</html>"
    p2 = "<html>" + ld("Kickoff", "2026-09-30", "Forman Arts Initiative") + \
         ld("Homeplace", "2026-10-02", "Cherry Street Pier") + "</html>"
    p3 = "<html>no events here</html>"
    # order matters: the bare-base fragment would match every page url
    restore = _offline({"page=2": p2, "page=3": p3, "daterange": p1})
    try:
        rows = scrapers.scrape_html_jsonld_paged(
            base, "dp", dt.date(2026, 9, 1), dt.date(2026, 10, 31))
    finally:
        restore()
    ok([r["name"] for r in rows] == ["Kickoff", "En Su SITIO", "Homeplace"],
       f"pages walked in order, (name,date) dedupe across pages "
       f"({[r['name'] for r in rows]})")
    ok(rows[0]["venue"] == "Forman Arts Initiative"
       and rows[0]["tags"] == "park:forman-arts-initiative",
       "aggregator routing tag minted from the JSON-LD Place name")
    ok(rows[0]["date"] == "2026-09-30" and rows[0]["time"] == "00:00",
       "date-only JSON-LD keeps the T00:00:00 artifact (documented, not inferred)")


# ── the docs tell the truth: mirrors in sync, pointers resolve ──────────────

def test_docs_truth():
    """Prose rots the way adapters do. Two guards: every backticked repo
    path in the charters must actually exist (this is how `engine/AGENTS.md`
    lived in the docs for weeks pointing at nothing); and every sanctioned
    operator must be documented somewhere an agent will read.
    """
    import pathlib
    import re

    ENG = pathlib.Path(__file__).resolve().parent
    ROOT = ENG.parent
    AGENTS = ["max", "rater", "scout", "steward", "tej"]

    def charter(name):
        """(frontmatter description, post-frontmatter body) of a .claude charter."""
        txt = (ROOT / ".claude/agents" / f"{name}.md").read_text()
        fm, body = re.match(r"^---\n(.*?)\n---\n(.*)$", txt, re.S).groups()
        return re.search(r"^description: (.*)$", fm, re.M).group(1), body.strip("\n")

    # (a) PATH POINTERS RESOLVE — conservative: skip anything that isn't
    # unambiguously a repo path (assignments, URLs, globs, prose with spaces).
    doc_files = [ROOT / "CLAUDE.md", ENG / "CLAUDE.md", ROOT / "README.md",
                 ROOT / ".claude/commands/next.md"]
    doc_files += [ROOT / ".claude/agents" / f"{n}.md" for n in AGENTS]
    NOT_A_PATH = re.compile(r"""[\s<>*()~?#='"|]""")
    LOOKS_LIKE_FILE = re.compile(r"\.(py|md|json|csv|geojson|db)$")
    roots = (ROOT, ENG, ROOT / "docs")
    # a bare filename (`later.md`, `_TEMPLATE.md`) is checked by basename anywhere
    # in the repo; only slashed tokens are held to an exact location.
    basenames = set()
    for dirpath, dirnames, filenames in __import__("os").walk(ROOT):
        dirnames[:] = [d for d in dirnames
                       if d not in {".git", ".venv", "__pycache__", "node_modules"}]
        basenames.update(filenames)

    checked, dangling = 0, []
    for f in doc_files:
        for raw in re.findall(r"`([^`\n]+)`", f.read_text()):
            tok = re.sub(r":\d+$", "", raw.strip().rstrip(".,;:"))
            if not tok or NOT_A_PATH.search(tok):
                continue
            if not ("/" in tok or LOOKS_LIKE_FILE.search(tok)):
                continue
            if tok.startswith(("http", "--", "/")):   # urls, flags, url paths
                continue
            # outputs a fresh clone hasn't generated yet (export / geocode), and
            # the city one-off module Max creates on first need
            if tok.rstrip("/").rsplit("/", 1)[-1] in {
                    "data.json", "funnel.json", "d", "geocode_cache.json", "chicago.py"}:
                continue
            checked += 1
            if "/" in tok:
                found = any((r / tok).exists() for r in roots)
            else:
                found = tok in basenames or any((r / tok).exists() for r in roots)
            if not found:
                dangling.append(f"{tok}  (in {f.relative_to(ROOT)})")
    ok(not dangling, "every backticked repo path in the docs resolves: " + "; ".join(dangling))
    ok(checked >= 60, f"the path scan still sees the docs' pointers (checked {checked})")

    # (b) OPERATOR COVERAGE — an undocumented operator is an operator nobody uses.
    import operators
    prose = (ENG / "CLAUDE.md").read_text() + "".join(charter(n)[1] for n in AGENTS)
    # ALL-CAPS exports are lookup tables (PLATFORM_MAP), not operations to teach.
    undocumented = [n for n in operators.__all__ if not n.isupper() and n not in prose]
    ok(not undocumented,
       f"every operator in operators.__all__ is documented: {undocumented}")


def test_algolia_events_index():
    """algolia_events: the whole config (credentials, facet filters, link root)
    rides the source url; hits are POSTed page by page and stop at nbPages.
    Wall-clock strings are NOT shifted, a multi-day run is ONE row with
    end_date, and the safety cap bounds the POSTs
    (Philly source "amrevmuseum")."""
    start = dt.date.today()
    end = start + dt.timedelta(days=60)
    d1 = start + dt.timedelta(days=3)
    d2 = start + dt.timedelta(days=5)
    past = start - dt.timedelta(days=30)

    posted = []

    def hit(title, s_iso, e_iso, loc="Museum of the American Revolution"):
        return {"title": title, "date_start": s_iso, "date_end": e_iso,
                "description": "<p>Body <style>a{color:red}</style></p>",
                "route": f"/events/{slug(title)}", "location_label": loc,
                "event_type": "Talk", "image": {"img": "https://img/x.jpg"}}

    page0 = {"nbPages": 2, "hits": [
        hit("Lecture", f"{d1} 18:30:00", f"{d1} 20:00:00"),
        hit("Long Weekend", f"{d1} 09:30:00", f"{d2} 18:00:00"),
        hit("Old News", f"{past} 10:00:00", f"{past} 11:00:00"),
    ]}
    page1 = {"nbPages": 2, "hits": [hit("All Day", f"{d2} 00:00:00", "")]}

    real = scrapers.core.post_json

    def fake_post(url, payload, headers=None, timeout=20):
        posted.append((url, payload, headers))
        return [page0, page1][payload["page"]]

    scrapers.core.post_json = fake_post
    try:
        rows = scrapers.scrape_algolia_events(
            "https://APP-dsn.algolia.net/1/indexes/production_events/query"
            "?x-algolia-application-id=APP&x-algolia-api-key=KEY"
            "&site=https%3A%2F%2Fmuseum.example"
            "&facetFilters=location_filter%3AMuseum+of+the+American+Revolution",
            "amrev", start, end)
    finally:
        scrapers.core.post_json = real

    ok(len(posted) == 2, f"walked both pages, then stopped on nbPages (got {len(posted)})")
    url0, body0, hdrs0 = posted[0]
    ok(url0.endswith("/1/indexes/production_events/query") and "?" not in url0,
       "credentials move OUT of the url into headers")
    ok(hdrs0["x-algolia-api-key"] == "KEY"
       and hdrs0["x-algolia-application-id"] == "APP", "both keys sent as headers")
    ok(body0["facetFilters"] == [["location_filter:Museum of the American Revolution"]],
       "facetFilters is ANDed nested-list form")

    ok(len(rows) == 3, f"past-only hit dropped, rest kept (got {len(rows)})")
    r = next(r for r in rows if r["name"] == "Lecture")
    ok(r["date"] == d1.isoformat() and r["time"] == "18:30" and r["end_time"] == "20:00",
       "wall-clock kept verbatim (no tz shift)")
    ok(r["url"] == "https://museum.example/events/lecture", "relative route joined to site")
    ok(r["image"] == "https://img/x.jpg" and r["category"] == "Talk",
       "image dict unwrapped, event_type -> category")
    ok("color:red" not in r["description"] and "Body" in r["description"],
       "norm() strips the CSS out of the indexed body")
    run = next(r for r in rows if r["name"] == "Long Weekend")
    ok(run["date"] == d1.isoformat() and run["end_date"] == d2.isoformat()
       and run["end_time"] == "", "multi-day run is ONE row with end_date, no end time")
    ad = next(r for r in rows if r["name"] == "All Day")
    ok(ad["all_day"] and ad["time"] == "", "midnight start -> all_day")

    posted.clear()
    scrapers.core.post_json = fake_post
    try:
        capped = scrapers.scrape_algolia_events(
            "https://APP-dsn.algolia.net/1/indexes/i/query"
            "?x-algolia-application-id=APP&x-algolia-api-key=KEY",
            "amrev", start, end, max_pages=1)
    finally:
        scrapers.core.post_json = real
    ok(len(posted) == 1, "max_pages hard-caps the POSTs")
    ok(len(capped) == 2, "page-1 rows still returned under the cap")

    nocred = scrapers.scrape_algolia_events(
        "https://APP-dsn.algolia.net/1/indexes/i/query", "amrev", start, end)
    ok(nocred == [], "a url with no credentials returns [] instead of raising")


def test_venuepilot_graphql():
    """venuepilot (Claude's Comedy Club first): the public paginatedEvents query.
    Times are venue WALL-CLOCK with no offset (no tz conversion); a show whose
    endTime is EARLIER than its startTime crosses midnight and must roll
    end_date forward, not emit a backwards event. The same show is listed once
    per ticket product, so performer 'Sign Up' rows are dropped as box-office
    plumbing. accountIds ride the url — no ids means no request at all.
    (Philly source "claudescomedy")"""
    start = dt.date.today()
    end = start + dt.timedelta(days=60)
    d1 = start + dt.timedelta(days=3)
    d2 = start + dt.timedelta(days=4)

    pages = {
        1: {"collection": [
            {"id": 1, "name": "Erik Terrell", "date": f"{d1}T00:00:00.000",
             "doorTime": f"{d1} 19:00:00", "startTime": f"{d1} 20:00:00",
             "endTime": f"{d1} 21:30:00", "minimumAge": 21,
             "description": "<p>A <b>debut</b> special</p>",
             "ticketsUrl": "https://t.venuepilot.com/e/erik",
             "websiteUrl": "https://claudescomedy.com/erik",
             "venue": {"name": "Claude's Comedy Club & Bar"}},
            # ends after midnight -> end_date rolls to the next day
            {"id": 2, "name": "Late Night Showcase", "date": f"{d2}T00:00:00.000",
             "startTime": f"{d2} 22:30:00", "endTime": f"{d2} 01:00:00",
             "minimumAge": None, "description": "",
             "ticketsUrl": "", "websiteUrl": "https://claudescomedy.com/late",
             "venue": {"name": "Claude's Comedy Club & Bar"}},
            # box-office plumbing, not an attendable event
            {"id": 3, "name": "7 PM - Open Mic Sign Up", "date": f"{d2}T00:00:00.000",
             "startTime": f"{d2} 19:00:00", "endTime": f"{d2} 00:00:00",
             "venue": {"name": "Claude's Comedy Club & Bar"}},
            # out of window -> dropped
            {"id": 4, "name": "Next Year", "date": "2019-01-01T00:00:00.000",
             "startTime": "2019-01-01 20:00:00",
             "venue": {"name": "Claude's Comedy Club & Bar"}},
        ], "metadata": {"currentPage": 1, "totalPages": 2, "totalCount": 5}},
        2: {"collection": [
            # page 2 repeats id 1 (server-side shuffle) -> must not double-count
            {"id": 1, "name": "Erik Terrell", "date": f"{d1}T00:00:00.000",
             "startTime": f"{d1} 20:00:00",
             "venue": {"name": "Claude's Comedy Club & Bar"}},
            {"id": 5, "name": "Say Gay", "date": f"{d2}T00:00:00.000",
             "startTime": f"{d2} 20:00:00", "endTime": f"{d2} 21:30:00",
             "venue": {"name": "Claude's Comedy Club & Bar"}},
        ], "metadata": {"currentPage": 2, "totalPages": 2, "totalCount": 5}},
    }
    calls = []

    def fake_post(url, payload, headers=None, timeout=20):
        calls.append((url, payload["variables"]))
        return {"data": {"paginatedEvents": pages[payload["variables"]["page"]]}}

    real = scrapers.core.post_json
    scrapers.core.post_json = fake_post
    try:
        rows = scrapers.scrape_venuepilot(
            "https://www.venuepilot.co/graphql?accountIds=3340",
            "claudescomedy", start, end)
        n_calls = len(calls)            # 2 pages walked (calls accumulates)
        keep = scrapers.scrape_venuepilot(
            "https://www.venuepilot.co/graphql?accountIds=3340",
            "claudescomedy", start, end, drop_signups=False)
        before = len(calls)
        noids = scrapers.scrape_venuepilot(
            "https://www.venuepilot.co/graphql", "claudescomedy", start, end)
        n_after_noids = len(calls) - before
    finally:
        scrapers.core.post_json = real

    ok(len(rows) == 3, f"2 pages, deduped, sign-up + past dropped (got {len(rows)})")
    ok(len(keep) == 4, "drop_signups=False keeps the sign-up row")
    ok(n_calls == 2, "totalPages=2 is walked, then the loop stops")
    ok(noids == [] and n_after_noids == 0,
       "no accountIds in the url -> zero requests, empty result")
    ok(calls[0][1]["accountIds"] == [3340]
       and calls[0][1]["startDate"] == start.isoformat()
       and calls[0][1]["endDate"] == end.isoformat(),
       "accountIds parsed off the url; window passed server-side")
    ok(calls[0][0] == "https://www.venuepilot.co/graphql",
       "the query string is stripped before POSTing")
    et = next(r for r in rows if r["name"] == "Erik Terrell")
    ok(et["date"] == d1.isoformat() and et["time"] == "20:00"
       and et["end_time"] == "21:30" and et["end_date"] == "",
       "wall-clock times pass through unconverted; same-day end carries no date")
    ok(et["description"] == "A debut special", "description is plain text")
    ok("21+" in et["tags"] and et["url"] == "https://t.venuepilot.com/e/erik",
       "minimumAge becomes a tag; ticketsUrl preferred over websiteUrl")
    late = next(r for r in rows if r["name"] == "Late Night Showcase")
    ok(late["time"] == "22:30" and late["end_time"] == "01:00"
       and late["end_date"] == (d2 + dt.timedelta(days=1)).isoformat(),
       "an after-midnight end rolls end_date forward")
    ok(late["url"] == "https://claudescomedy.com/late",
       "an empty ticketsUrl falls back to websiteUrl")


def _cp_page(slug, when, body="", title=None):
    """Minimal Craftpeak event-detail page: Beaver Builder heading + the
    icon-text module that carries the whole when-string in a title= attribute."""
    return (
        '<html><head>'
        f'<meta property="og:title" content="{title or slug} | Other Half Brewing">'
        '<meta property="og:image" content="https://img.example/x.png?a=1&amp;b=2">'
        '</head><body>'
        f'<div class="fl-module fl-module-heading"><span class="fl-heading-text">'
        f'{title or slug}</span></div>'
        '<div class="fl-module fl-module-icon"><div class="fl-icon-text">'
        f"<p><span class='ui-label size--large' title='{when}'>{when}</span></p>"
        '</div></div>'
        + (f'<div class="fl-module fl-module-cp-content-builder">'
           f'<div class="content-builder module--content-builder">'
           f'<div class="layout-content"><h3>Heading</h3><p>{body}</p></div>'
           f'</div></div>' if body else "")
        + '<div class="fl-module fl-module-footer">SUBSCRIBE</div>'
          '</body></html>')


def test_craftpeak_listing_and_detail():
    """craftpeak (Other Half Brewing Philadelphia first): a two-step HTML scrape.
    Craftpeak exposes no REST, no ical and no Event JSON-LD, but every event
    URL's slug ENDS IN THE DATE — so the window filter runs off the listing and
    only in-range events cost a detail fetch. Descriptions come out of a
    `content-builder` module by SLICE (its divs nest, so a non-greedy regex
    returns nothing). (Philly source "otherhalfphilly")"""
    start = dt.date.today()
    end = start + dt.timedelta(days=60)
    d1 = start + dt.timedelta(days=2)
    d2 = start + dt.timedelta(days=9)
    host = "https://otherhalfbrewing.com"

    listing = "".join(
        f'<a href="{host}/event/{s}-{d}/" class="excerpt-box">card</a>'
        for s, d in [("philly-oktoberfest", d2), ("clone-philly-trivia", d1),
                     ("old-taproom-closure", "2019-07-18")])
    pages = {
        "/event-type/phl/": listing,
        # page 2 is the ARCHIVE (nothing in window) -> the walk must stop
        "/event-type/phl/page/2/":
            f'<a href="{host}/event/ancient-thing-2018-03-01/">x</a>',
        f"/event/philly-oktoberfest-{d2}": _cp_page(
            "philly-oktoberfest", f"{d2:%B} {d2.day} {d2.year} 12:00 pm - 9:00 pm",
            "Raise a <strong>stein</strong>", title="Philly Oktoberfest"),
        f"/event/clone-philly-trivia-{d1}": _cp_page(
            "clone-philly-trivia", f"{d1:%B} {d1.day} {d1.year} 6:00 pm",
            title="Philly Trivia"),
    }
    fetched = []

    def fake_fetch(url, headers=None, timeout=20):
        fetched.append(url)
        for frag, body in pages.items():
            if frag in url:
                return body.encode()
        raise AssertionError(f"unexpected fetch: {url}")

    real = scrapers.core.fetch
    scrapers.core.fetch = fake_fetch
    try:
        rows = scrapers.scrape_craftpeak(f"{host}/event-type/phl/", "ohp", start, end)
        n_fetch = len(fetched)          # count THIS run only (fetched accumulates)
        two = scrapers.scrape_craftpeak(f"{host}/event-type/phl/", "ohp", start, end,
                                        max_pages=2)
        capped = scrapers.scrape_craftpeak(f"{host}/event-type/phl/", "ohp", start, end,
                                           max_events=1)
    finally:
        scrapers.core.fetch = real

    ok(len(rows) == 2, f"the 2019 slug is out of window and never fetched (got {len(rows)})")
    ok(n_fetch == 3, "1 listing fetch + 1 detail fetch PER IN-WINDOW event only")
    ok(len(two) == 2 and len(capped) == 1,
       "an archive page with nothing in window adds nothing; max_events caps")
    ok([r["date"] for r in rows] == [d1.isoformat(), d2.isoformat()],
       "results are sorted by the date read off the url slug")
    okt = next(r for r in rows if r["name"] == "Philly Oktoberfest")
    ok(okt["time"] == "12:00" and okt["end_time"] == "21:00",
       "'12:00 pm - 9:00 pm' -> 24h start and end")
    ok(okt["description"] == "Heading Raise a stein",
       f"content-builder slice yields plain prose (got {okt['description']!r})")
    ok(okt["image"] == "https://img.example/x.png?a=1&b=2",
       "og:image entities are unescaped (&amp; would break the imgix signature)")
    ok(okt["url"] == f"{host}/event/philly-oktoberfest-{d2}",
       "the event url is the slug url, trailing slash trimmed")
    triv = next(r for r in rows if r["name"] == "Philly Trivia")
    ok(triv["time"] == "18:00" and triv["end_time"] == "",
       "a single-clock when-string gives a start and no end")
    ok(triv["description"] == "", "no content-builder module -> empty description")
    ok(scrapers._cp_clock("12:00 am") == "00:00"
       and scrapers._cp_clock("12:00 pm") == "12:00"
       and scrapers._cp_clock("9 PM") == "21:00",
       "noon/midnight and bare-hour clocks convert correctly")

def test_squarespace_stacked_month_format_and_dormant_feed():
    """People's Budget Office (2026-09-06, Max, GO-HARD r3): an events-stacked
    Squarespace collection that is REAL but seasonally dormant — past[] holds 30
    items, upcoming[] is empty. Two things must hold.

    1. The month walk uses `&month=MM-YYYY`. Squarespace answers `&month=YYYY-MM`
       with an EMPTY BODY (not JSON), so getting this backwards turns a live feed
       into a parse_error. Pin the query string.
    2. past[] items outside [start, end] must NOT leak into a future window: a
       dormant feed has to yield ZERO rows so the live gate stamps an honest
       `no_events` instead of a false build off stale events.
    """
    import re
    start = dt.date.today()
    end = start + dt.timedelta(days=60)
    old = int(dt.datetime.combine(start - dt.timedelta(days=93), dt.time(18, 0)).timestamp() * 1000)

    body = _json_mod.dumps({
        "collection": {"typeName": "events-stacked"},
        "upcoming": [],
        "past": [{"id": "p1", "title": "Closing Party at Love Park", "startDate": old,
                  "endDate": old + 2 * 3600 * 1000,
                  "fullUrl": "/events/closing-party-at-love-park-1"}],
    })

    seen = []
    real = (scrapers.core.fetch_json, scrapers.time.sleep)
    scrapers.core.fetch_json = lambda url, **kw: (seen.append(url), _json_mod.loads(body))[1]
    scrapers.time.sleep = lambda *_: None
    try:
        rows = scrapers.scrape_squarespace(
            "https://www.peoplesbudgetoffice.org/events", "pbo", start, end)
    finally:
        scrapers.core.fetch_json, scrapers.time.sleep = real

    ok(rows == [], f"dormant events-stacked feed yields zero rows, past[] never leaks (got {len(rows)})")
    months = [u for u in seen if "month=" in u]
    ok(months, "the events-stacked path actually walks months")
    ok(all(re.search(r"[&?]month=\d{2}-\d{4}(&|$)", u) for u in months),
       f"month param is MM-YYYY, not YYYY-MM (got {months[:3]})")
    ok(f"month={start.month:02d}-{start.year}" in months[0],
       f"the walk starts at the window's first month (got {months[0]})")


def test_google_calendar_rrule_expansion():
    """google_calendar (Wissahickon Brewing East Falls first, 2026-09-06, Max,
    GO-HARD r3): a public Google Calendar ICS where the venue's REAL weekly
    programming is one open-ended recurring VEVENT with a DTSTART years in the
    past. `scrape_ical` deliberately does not expand RRULE, so it reads that
    3,879-VEVENT calendar as 2 events; this adapter expands DAILY/WEEKLY.

    Pins: (1) an open-ended weekly recurs into the window off an ancient
    DTSTART; (2) UNTIL/COUNT/INTERVAL are respected; (3) EXDATE cancels one
    instance; (4) a RECURRENCE-ID override replaces (not duplicates) its
    occurrence; (5) STATUS:CANCELLED is dropped; (6) an unsupported FREQ
    degrades to the DTSTART alone rather than to wrong dates; (7) max_events
    caps."""
    start = dt.date(2026, 9, 6)          # a Sunday
    end = dt.date(2026, 9, 26)

    ics = """BEGIN:VCALENDAR
BEGIN:VEVENT
DTSTART;TZID=America/New_York:20240118T193000
DTEND;TZID=America/New_York:20240118T213000
SUMMARY:Wiss Quizzo!
DESCRIPTION:Free\\, win PRIZES
LOCATION:Wissahickon Brewing Company - East Falls
UID:quizzo@x
RRULE:FREQ=WEEKLY;BYDAY=TH
EXDATE;TZID=America/New_York:20260917T193000
END:VEVENT
BEGIN:VEVENT
DTSTART;TZID=America/New_York:20260910T200000
DTEND;TZID=America/New_York:20260910T220000
SUMMARY:Wiss Quizzo! (late start)
UID:quizzo@x
RECURRENCE-ID;TZID=America/New_York:20260910T193000
END:VEVENT
BEGIN:VEVENT
DTSTART;TZID=America/New_York:20260907T190000
SUMMARY:Open Mic
UID:mic@x
RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=MO;COUNT=2
END:VEVENT
BEGIN:VEVENT
DTSTART;TZID=America/New_York:20260908T120000
SUMMARY:Food Truck Week
UID:truck@x
RRULE:FREQ=DAILY;UNTIL=20260910T035959Z
END:VEVENT
BEGIN:VEVENT
DTSTART;TZID=America/New_York:20260912T110000
SUMMARY:Cancelled Thing
UID:dead@x
STATUS:CANCELLED
END:VEVENT
BEGIN:VEVENT
DTSTART;TZID=America/New_York:20260913T110000
SUMMARY:Monthly Mystery
UID:month@x
RRULE:FREQ=MONTHLY;BYMONTHDAY=13
END:VEVENT
BEGIN:VEVENT
DTSTART:20260916T230000Z
SUMMARY:UTC Night Show
UID:utc@x
END:VEVENT
END:VCALENDAR
"""

    real = scrapers.core.fetch
    scrapers.core.fetch = lambda url, headers=None, timeout=20: ics.encode()
    try:
        rows = scrapers.scrape_google_calendar("https://cal/basic.ics", "wbef", start, end)
        capped = scrapers.scrape_google_calendar("https://cal/basic.ics", "wbef",
                                                 start, end, max_events=2)
    finally:
        scrapers.core.fetch = real

    by = {(r["name"], r["date"]) for r in rows}
    quizzo = sorted(r["date"] for r in rows if r["name"] == "Wiss Quizzo!")
    ok(quizzo == ["2026-09-24"],
       f"open-ended weekly expands from a 2024 DTSTART; EXDATE 09-17 and the "
       f"RECURRENCE-ID'd 09-10 are both suppressed (got {quizzo})")
    ok(("Wiss Quizzo! (late start)", "2026-09-10") in by,
       "the edited instance emits itself, at its OWN 20:00 start")
    ok(next(r for r in rows if r["name"].endswith("(late start)"))["time"] == "20:00",
       "override keeps its moved time")
    mic = sorted(r["date"] for r in rows if r["name"] == "Open Mic")
    ok(mic == ["2026-09-07", "2026-09-21"], f"INTERVAL=2 + COUNT=2 (got {mic})")
    truck = sorted(r["date"] for r in rows if r["name"] == "Food Truck Week")
    ok(truck == ["2026-09-08", "2026-09-09"],
       f"FREQ=DAILY stops at UNTIL (got {truck})")
    ok(not any(r["name"] == "Cancelled Thing" for r in rows),
       "STATUS:CANCELLED never emits")
    mystery = [r["date"] for r in rows if r["name"] == "Monthly Mystery"]
    ok(mystery == ["2026-09-13"],
       f"an unsupported FREQ degrades to the DTSTART, not to guessed dates (got {mystery})")
    utc = next(r for r in rows if r["name"] == "UTC Night Show")
    ok((utc["date"], utc["time"]) == ("2026-09-16", "19:00"),
       f"a trailing-Z DTSTART converts to local wall-clock (got {utc['date']} {utc['time']})")
    q = next(r for r in rows if r["name"] == "Wiss Quizzo!")
    ok(q["end_time"] == "21:30" and q["end_date"] == "2026-09-24",
       "DTEND rides along as a duration on every expanded occurrence")
    ok(q["description"] == "Free, win PRIZES",
       f"RFC-5545 escapes are unescaped (got {q['description']!r})")
    ok([r["date"] for r in rows] == sorted(r["date"] for r in rows),
       "output is date-sorted")
    ok(len(capped) == 2, f"max_events is a hard ceiling (got {len(capped)})")


def test_bentobox_fake_z_and_index():
    """bentobox (Bistro Romano first, 2026-09-06, Max, GO-HARD r3): the BentoBox
    restaurant CMS stamps a FAKE Z on startDate — "2026-09-30T17:30:00Z" is the
    5:30 PM local seating, not 17:30 UTC. The generic `html_jsonld` adapter
    faithfully localizes that and lands every event four hours early, which is
    why this is a separate adapter. Also pins: the sitemap's trailing-`//`
    hrefs collapse to one URL (no double fetch), location.name "Event Location"
    is boilerplate and must NOT become a venue/routing key, and max_events caps
    the detail fan-out."""
    start = dt.date(2026, 9, 1)
    end = dt.date(2026, 10, 31)
    host = "https://www.bistroromano.com"

    def det(slug_, name, sd, ed):
        return ('<script type="application/ld+json">' + _json_mod.dumps({
            "@context": "https://schema.org", "@type": "Organization",
            "name": "Bistro Romano"}) + "</script>"
            '<script type="application/ld+json">' + _json_mod.dumps({
                "@context": "https://schema.org", "@type": "Event",
                "name": name, "startDate": sd, "endDate": ed,
                "description": "Taste 30 <b>Wines</b>",
                "image": ["https://img/x.jpg"],
                "url": f"{host}/store/event/{slug_}/",
                "location": {"@type": "PostalAddress", "name": "Event Location",
                             "streetAddress": "120 Lombard Street",
                             "addressLocality": "Philadelphia"},
            }) + "</script>")

    pages = {
        "/store/events/": (
            f'<a href="{host}/store/event/wine-tasting-sept-30/">a</a>'
            f'<a href="{host}/store/event/wine-tasting-sept-30-740p//">b</a>'
            f'<a href="{host}/store/event/wine-tasting-sept-30/">dup</a>'
            f'<a href="{host}/store/event/wine-tasting-2021/">old</a>'),
        "/store/event/wine-tasting-sept-30/": det(
            "wine-tasting-sept-30", "Wine Tasting Wed Sept. 30 @ 5:30",
            "2026-09-30T17:30:00Z", "2026-09-30T19:00:00Z"),
        "/store/event/wine-tasting-sept-30-740p/": det(
            "wine-tasting-sept-30-740p", "Wine Tasting Wed Sept. 30 @ 7:30p",
            "2026-09-30T19:30:00Z", "2026-09-30T21:00:00Z"),
        "/store/event/wine-tasting-2021/": det(
            "wine-tasting-2021", "Wine Tasting 2021",
            "2021-07-21T19:30:00Z", "2021-07-21T21:00:00Z"),
    }
    fetched = []

    def fake_fetch(url, headers=None, timeout=20):
        fetched.append(url)
        for frag, body in pages.items():
            if url.endswith(frag):
                return body.encode()
        raise AssertionError(f"unexpected fetch: {url}")

    real = (scrapers.core.fetch, scrapers.jsonld.time.sleep)
    scrapers.core.fetch = fake_fetch
    scrapers.jsonld.time.sleep = lambda *_: None
    try:
        rows = scrapers.scrape_bentobox(f"{host}/store/events/", "br", start, end)
        n = len(fetched)
        capped = scrapers.scrape_bentobox(f"{host}/store/events/", "br", start, end,
                                          max_events=1)
    finally:
        scrapers.core.fetch, scrapers.jsonld.time.sleep = real

    ok(len(rows) == 2, f"the 2021 event is out of window (got {len(rows)})")
    ok([r["time"] for r in rows] == ["17:30", "19:30"],
       f"the fake Z is DISCARDED — 17:30Z is the 5:30 PM seating (got "
       f"{[r['time'] for r in rows]})")
    ok(rows[0]["end_time"] == "19:00", "endDate keeps its wall-clock too")
    ok(n == 4, f"the '//' href and the duplicate collapse to one fetch each (got {n})")
    ok(all(r["venue"] == "" and r["tags"] == "" for r in rows),
       "'Event Location' is boilerplate: venue stays empty for the dedicated pass")
    ok(rows[0]["address"] == "120 Lombard Street, Philadelphia",
       f"address joins street + locality (got {rows[0]['address']!r})")
    ok(rows[0]["description"] == "Taste 30 Wines", "description is plain text")
    ok(rows[0]["image"] == "https://img/x.jpg", "image list takes the first entry")
    ok(len(capped) == 1, "max_events is a hard ceiling")


def test_connect_coverage_bare_key_and_adoption_upgrade():
    """routing keys are BARE slugs (ingest._park_slug strips 'park:' from the
    TAG) — a prefixed key never matched, 21 Cambridge rows sat inert (09-06).
    And a vet re-touching a firehose 'discovered' row IS the adoption verdict
    (Tej vet round 5 had to remove+connect to get rows adopted)."""
    s = Session()
    v = add_venue_lead("Bare Key Venue", discovered_via="manual", sess=s)
    src = add_source_lead("Bare Key Source", adapter="stub_ok", sess=s)
    cov = connect_coverage(src.id, "venue", v.id, adoption="discovered",
                           routing_key="park:bare-key-venue", sess=s)
    ok(cov.routing_key == "bare-key-venue" and cov.adoption == "discovered",
       "connect_coverage strips a park: prefix; discovered stays discovered")
    cov = connect_coverage(src.id, "venue", v.id, routing_key="park:bare-key-venue",
                           sess=s)
    ok(cov.adoption == "adopted", "re-touch with adoption='adopted' upgrades a discovered row")
    cov = connect_coverage(src.id, "venue", v.id, adoption="discovered", sess=s)
    ok(cov.adoption == "adopted", "an adopted row never demotes back to discovered")
    s.rollback()


def test_nps_api_dates_times_expansion():
    """nps_api (Independence NHP first, 2026-10-12, Max): one record per program
    with dates[] x times[]. Pins: one row per in-window DATE (cancelled dates
    dropped; same-day slots fold into earliest start / latest end, listed in the
    description — ingest's dedupe key is per date+name), 12h -> 24h clock,
    string booleans ("true" free -> "Free"), location text prepended to a
    plain-text description, relative image -> nps.gov, infourl else the NPS
    detail page, DEMO_KEY when unkeyed + window server-side, max_events cap."""
    start, end = dt.date(2026, 10, 12), dt.date(2026, 10, 22)
    payload = {"total": "3", "data": [
        {"id": "AAA", "title": "Benjamin Franklin", "dates": [
            "2026-10-10", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-30"],
         "cancelleddatelist": ["2026-10-14"],
         "times": [{"timestart": "03:15 PM", "timeend": "04:15 PM"},
                   {"timestart": "02:15 PM", "timeend": "03:15 PM"}],
         "isallday": "false", "isfree": "true", "infourl": "",
         "location": "Franklin Court, 318 Market St",
         "description": "<p>Join a <b>ranger</b>.</p>", "types": ["Talk"],
         "category": "Regular Event", "latitude": "", "longitude": "",
         "parkfullname": "Independence National Historical Park",
         "images": [{"url": "/common/uploads/x.jpg"}]},
        {"id": "BBB", "title": "Free Museum Day", "dates": ["2026-10-20"],
         "times": [], "isallday": "true", "isfree": "false",
         "infourl": "https://inht.org/x", "location": "", "description": "Hi",
         "latitude": "39.9", "longitude": "-75.1", "images": []},
        {"id": "CCC", "title": "", "dates": ["2026-10-20"], "times": []},
    ]}
    urls = []

    def fake_fetch_json(url, headers=None, timeout=20):
        urls.append(url)
        return payload

    real = scrapers.core.fetch_json
    scrapers.core.fetch_json = fake_fetch_json
    try:
        rows = scrapers.scrape_nps_api(
            "https://developer.nps.gov/api/v1/events?parkCode=inde", "nps", start, end)
        capped = scrapers.scrape_nps_api(
            "https://developer.nps.gov/api/v1/events?parkCode=inde&api_key=K1",
            "nps", start, end, max_events=1)
    finally:
        scrapers.core.fetch_json = real

    ok(len(urls) == 2, f"total 3 fits one page (got {len(urls)} fetches)")
    ok("api_key=DEMO_KEY" in urls[0] and "dateStart=2026-10-12" in urls[0]
       and "dateEnd=2026-10-22" in urls[0] and "parkCode=inde" in urls[0],
       f"unkeyed url gets DEMO_KEY + the window (got {urls[0]})")
    ok("api_key=K1" in urls[1] and "DEMO_KEY" not in urls[1], "a url key wins")
    bf = [r for r in rows if r["name"] == "Benjamin Franklin"]
    ok([r["date"] for r in bf] == ["2026-10-13", "2026-10-15"],
       f"out-of-window + cancelled dates dropped (got {[r['date'] for r in bf]})")
    ok(bf[0]["time"] == "14:15" and bf[0]["end_time"] == "16:15",
       f"slots fold to earliest start / latest end (got {bf[0]['time']}-{bf[0]['end_time']})")
    ok(bf[0]["description"] == ("Location: Franklin Court, 318 Market St. "
                                "Times: 2:15 PM, 3:15 PM. Join a ranger ."),
       f"location + slots + plain text (got {bf[0]['description']!r})")
    ok(bf[0]["cost"] == "Free" and bf[0]["tags"] == "Talk", "isfree string -> Free")
    ok(bf[0]["image"] == "https://www.nps.gov/common/uploads/x.jpg", "image absolutized")
    ok(bf[0]["url"] == "https://www.nps.gov/planyourvisit/event-details.htm?id=AAA",
       "no infourl -> NPS detail page")
    md = [r for r in rows if r["name"] == "Free Museum Day"]
    ok(len(md) == 1 and md[0]["all_day"] and md[0]["time"] == "" and md[0]["cost"] == "",
       "isallday string -> all-day, isfree false -> no cost claim")
    ok(md[0]["url"] == "https://inht.org/x" and md[0]["lat"] == 39.9, "infourl + coords kept")
    ok(len(rows) == 3, f"untitled record skipped (got {len(rows)})")
    ok(len(capped) == 1, "max_events is a hard ceiling")



# ── the funnel: every stage counted, the right queue named ──────────────────

def test_funnel_stages_and_next_move():
    """funnel.py is what /next steers by, so its queues must match the
    operators that move a lead between stages: no verdict → Scout; go → Max;
    built + unhealthy → Steward; visible + unscored → Rater."""
    import funnel
    from operators import set_source_status
    s = Session()
    add_source_lead("Funnel Unscouted", adapter="html", url="https://x/a", sess=s)
    add_source_lead("Funnel Go", adapter="html", url="https://x/b", sess=s)
    add_source_lead("Funnel Nogo", adapter="html", url="https://x/c", sess=s)
    s.commit()
    set_source_status("funnel-go", "planned", verdict="go", sess=s)
    set_source_status("funnel-nogo", "none", verdict="no_go", notes="social-only", sess=s)
    s.commit()
    f = funnel.compute(s)
    q = {x["stage"]: x for x in f["queues"]}
    ok("Funnel Unscouted" in q["scout"]["examples"]
       and "Funnel Go" not in q["scout"]["examples"]
       and "Funnel Nogo" not in q["scout"]["examples"],
       "no verdict = Scout's queue; a verdict of either kind leaves it")
    ok("Funnel Go" in q["build"]["examples"], "verdict=go and unbuilt = Max's queue")
    ok(f["totals"]["no_go"] >= 1, "NO-GO counted")
    try:
        set_source_status("funnel-go", "planned", verdict="maybe", sess=s)
        ok(False, "a bogus verdict is refused")
    except ValueError:
        ok(True, "a bogus verdict is refused")
    ok(f["next"] is not None and f["next"]["n"] > 0,
       "with work queued, the next move names a non-empty queue")
    ok(f["next"]["stage"] == [x for x in f["queues"]
                               if x["n"] and x["stage"] != "export"][-1]["stage"],
       "next = the furthest-downstream non-empty work queue")
    ok("NEXT" in funnel.render(f), "the terminal view renders")
    s.close()

if __name__ == "__main__":
    for fn in [v for k, v in sorted(globals().items())
               if k.startswith("test_") and callable(v) and not k == "test_engine"]:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n{PASS} assertions passed (engine).")
