"""Conservative identity resolution for venues and event instances.

The ingest key handles exact repeats.  This module handles the narrower, messier
case where two sources describe the same thing differently.  Matching is kept
deliberately explainable: venues need corroborating identity signals, and events
must share a venue and calendar day before their titles are compared.

The public entry point is also a safe audit/apply CLI for rows already in the DB::

    .venv/bin/python dedupe.py                 # report only
    .venv/bin/python dedupe.py --apply-events  # coalesce same-source repeats only

The dupe-suspect loop (2026-08-19) covers what string rules can't: a
deterministic detector surfaces same-venue/same-minute pairs the matcher does
NOT collapse, an agentic judge rules same/distinct, and the verdict persists
(dedupe_verdicts) so 'same' collapses display and 'distinct' never re-enters
the queue::

    .venv/bin/python dedupe.py --suspects [--out /tmp/suspects.json]
    .venv/bin/python dedupe.py --import-verdicts /tmp/verdicts.json

Venue merges are never guessed by the CLI.  Use operators.merge_venues with an
explicit survivor after reviewing the audit output.
"""
from __future__ import annotations

import argparse
import datetime as dt
import math
import re
from collections import defaultdict
from difflib import SequenceMatcher
from urllib.parse import urlparse

from models import (
    CITY, CITY_SLUG, DedupeVerdict, Event, EventScore, Group, PipelineSource,
    Session, SourceCoverage, Venue, is_visible, slug,
)


NONWORD = re.compile(r"[^a-z0-9]+")
UNIT = re.compile(r"\b(?:suite|ste|unit|apt|apartment|floor|fl)\s*[a-z0-9-]+\b", re.I)
STATUS = re.compile(
    r"(?:\b(?:sold\s*out|rescheduled|postponed|new\s*date|just\s*added)\b[\s:!\-–—]*)",
    re.I,
)
TICKET = re.compile(
    r"(?:\b(?:general\s*admission|ga|vip|early\s*show|late\s*show|tickets?)\b)", re.I
)
DATE_BITS = re.compile(
    r"\b(?:mon|tue|wed|thu|fri|sat|sun)(?:day)?\b|"
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\b|"
    r"\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b",
    re.I,
)
GENERIC_EVENT_WORDS = {
    "a", "an", "and", "at", "by", "for", "from", "in", "of", "on", "the", "to", "with",
    "event", "events", "live", "presents", "presented",
}
# City-name words are generic in venue names ("Philadelphia Ethical Society" ==
# "Ethical Society of Philadelphia") — derived from the city params (multi-city,
# 2026-08-29; was a hardcoded philadelphia/philly pair).
GENERIC_VENUE_WORDS = {
    "a", "an", "and", "at", "of", "the",
    *f"{CITY} {CITY_SLUG}".lower().replace("-", " ").replace("/", " ").split(),
    "center", "centre", "building", "venue",
}
SHARED_HOSTS = {
    "facebook.com", "instagram.com", "eventbrite.com", "ticketmaster.com",
    "meetup.com", "linktr.ee", "google.com",
}
GENERIC_URL_PATHS = {
    "", "events", "calendar", "event-calendar", "events-calendar", "shows",
    "tickets", "whats-on", "upcoming-events", "schedule",
}


def _words(value: str | None, *, drop: set[str] | None = None) -> set[str]:
    words = set(NONWORD.sub(" ", (value or "").lower()).split())
    return words - (drop or set())


def _jaccard(left: set[str], right: set[str]) -> float:
    return len(left & right) / len(left | right) if left and right else 0.0


def normalize_address(value: str | None) -> str:
    """Normalize enough for corroboration, not enough to invent an address."""
    value = UNIT.sub(" ", (value or "").lower())
    replacements = {
        " street ": " st ", " avenue ": " ave ", " road ": " rd ",
        " boulevard ": " blvd ", " place ": " pl ", " lane ": " ln ",
        " drive ": " dr ", " square ": " sq ", " north ": " n ", " south ": " s ",
        " east ": " e ", " west ": " w ",
    }
    value = f" {NONWORD.sub(' ', value).strip()} "
    for old, new in replacements.items():
        value = value.replace(old, new)
    return " ".join(value.split())


def website_host(value: str | None) -> str:
    if not value:
        return ""
    parsed = urlparse(value if "://" in value else f"https://{value}")
    return (parsed.hostname or "").lower().removeprefix("www.")


def _distance_m(a_lat, a_lng, b_lat, b_lng) -> float | None:
    if None in (a_lat, a_lng, b_lat, b_lng):
        return None
    # Equirectangular distance is accurate enough across one city block.
    lat = math.radians((a_lat + b_lat) / 2)
    x = math.radians(b_lng - a_lng) * math.cos(lat)
    y = math.radians(b_lat - a_lat)
    return 6_371_000 * math.sqrt(x * x + y * y)


def venue_match_reason(existing: Venue, *, name: str, address: str | None = None,
                       website: str | None = None, lat: float | None = None,
                       lng: float | None = None) -> str | None:
    """Return an explainable match reason, or None when identity is uncertain."""
    if existing.id == slug(name):
        return "same slug"
    old_words = _words(existing.name, drop=GENERIC_VENUE_WORDS)
    new_words = _words(name, drop=GENERIC_VENUE_WORDS)
    name_overlap = _jaccard(old_words, new_words)
    name_containment = (len(old_words & new_words) / min(len(old_words), len(new_words))
                        if old_words and new_words else 0.0)
    old_addr, new_addr = normalize_address(existing.address), normalize_address(address)
    same_address = bool(
        old_addr and new_addr and any(c.isdigit() for c in old_addr)
        and (old_addr == new_addr or old_addr.startswith(new_addr + " ")
             or new_addr.startswith(old_addr + " "))
    )
    old_host, new_host = website_host(existing.website), website_host(website)
    same_site = bool(old_host and old_host == new_host and old_host not in SHARED_HOSTS)
    distance = _distance_m(existing.lat, existing.lng, lat, lng)

    # A shared address/site alone is not enough: complexes often contain multiple rooms.
    if same_address and (name_overlap >= 0.60 or
                         (min(len(old_words), len(new_words)) >= 2 and
                          name_containment >= 0.80)):
        return "similar name + same address"
    # Same-brand siblings (library branches, rec centers) share one website and
    # most name words but each carries its own distinguishing word — when BOTH
    # names have words the other lacks, the site alone must not merge them
    # (Cambridge seeding, 2026-08-29: Collins→Boudreau et al.). Word-order
    # variants and pure additions ("X" vs "The X Annex") remain subsets → merge.
    names_diverge = bool(old_words - new_words) and bool(new_words - old_words)
    if name_overlap >= 0.60 and same_site and not names_diverge:
        return "similar name + same website"
    if name_overlap >= 0.75 and distance is not None and distance <= 60:
        return "similar name + coordinates within 60m"
    return None


def find_matching_venue(sess, *, name: str, address: str | None = None,
                        website: str | None = None, lat: float | None = None,
                        lng: float | None = None) -> tuple[Venue | None, str | None]:
    """Find one unambiguous venue match. Multiple candidates means no auto-merge."""
    matches = []
    for venue in sess.query(Venue).all():
        reason = venue_match_reason(venue, name=name, address=address, website=website,
                                    lat=lat, lng=lng)
        if reason:
            matches.append((venue, reason))
    return matches[0] if len(matches) == 1 else (None, None)


def normalize_event_name(value: str | None) -> str:
    """Remove listing/status decoration while retaining the event's semantic title."""
    value = STATUS.sub(" ", value or "")
    value = TICKET.sub(" ", value)
    value = DATE_BITS.sub(" ", value)
    return " ".join(NONWORD.sub(" ", value.lower()).split())


def matching_suppression(sess, name: str | None, venue_id: str | None):
    """The standing EventSuppression rule this (name, venue) trips, or None.
    Exact on normalized name; venue must match (NULL matches roaming group rows)."""
    from models import EventSuppression
    key = normalize_event_name(name)
    if not key:
        return None
    return (sess.query(EventSuppression)
            .filter(EventSuppression.name_norm == key,
                    EventSuppression.venue_id.is_(None) if venue_id is None
                    else EventSuppression.venue_id == venue_id)
            .first())


def apply_suppression(sess, event: Event) -> bool:
    """Ingest hook: if a rule matches, mark the row suppressed (idempotent).
    Returns True when the row is (now) suppressed by a rule."""
    rule = matching_suppression(sess, event.name, event.venue_id)
    if rule is None:
        return False
    if event.lifecycle != "suppressed":
        event.lifecycle = "suppressed"
        event.verified_by = f"{rule.created_by}: {rule.reason}"
    return True


def event_url_key(value: str | None) -> str:
    """Identity-bearing form of a listing URL: host + path, tracking stripped.

    Empty when the URL cannot identify ONE event — no host, or a generic
    listing path (a calendar page many events share must never be identity).
    """
    if not value:
        return ""
    parsed = urlparse(value if "://" in value else f"https://{value}")
    host = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path.rstrip("/")
    if not host or "." not in host or path.strip("/").lower() in GENERIC_URL_PATHS:
        return ""
    return f"{host}{path.lower()}"


def event_name_similarity(left: str | None, right: str | None) -> float:
    a, b = normalize_event_name(left), normalize_event_name(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    aw = _words(a, drop=GENERIC_EVENT_WORDS)
    bw = _words(b, drop=GENERIC_EVENT_WORDS)
    if min(len(aw), len(bw)) < 2:
        return 0.0
    return max(_jaccard(aw, bw), SequenceMatcher(None, a, b).ratio())


def events_match(left: Event, right: Event) -> bool:
    """Same instance test. Location and local calendar day are hard gates —
    except that a venue-LESS observation (a roaming group's feed: venue_id NULL)
    may match a venued one; two different concrete venues never match. Without
    the exception, a group's own listing and an aggregator's venued listing of
    the same party display twice (TEASE, 2026-08-17)."""
    if not left.start or not right.start:
        return False
    if (left.venue_id != right.venue_id
            and left.venue_id is not None and right.venue_id is not None):
        return False
    if left.start.date() != right.start.date():
        return False
    if left.start.time() != dt.time() and right.start.time() != dt.time():
        delta = abs((left.start - right.start).total_seconds())
        if delta > 90 * 60:
            return False
    # Two SOURCES pointing at one event-specific URL is definitive identity even
    # when the titles diverge ("Anime Drag Tribute Show" cross-posting "Otaku
    # After Dark", 2026-08-18). Same-source rows keep the name test: a dedicated
    # feed may legally stamp one ticket URL on two showtimes of one run.
    if left.source_id and right.source_id and left.source_id != right.source_id:
        url_key = event_url_key(left.url)
        if url_key and url_key == event_url_key(right.url):
            return True
    return event_name_similarity(left.name, right.name) >= 0.82


def load_verdicts(sess) -> dict[tuple[str, str], str]:
    """All persisted judge calls, {(key_a, key_b) sorted: 'same'|'distinct'}.
    Tolerates a durable DB minted before the table existed (same tiny forward
    migration as VenueAlias)."""
    DedupeVerdict.__table__.create(bind=sess.connection(), checkfirst=True)
    return {(v.dedupe_key_a, v.dedupe_key_b): v.verdict
            for v in sess.query(DedupeVerdict).all()}


def verdict_for(verdicts: dict, left: Event, right: Event) -> str | None:
    return verdicts.get(tuple(sorted((left.dedupe_key, right.dedupe_key))))


def dupe_suspects(sess, events: list[Event],
                  verdicts: dict | None = None) -> list[tuple[Event, Event]]:
    """The deterministic half of the dupe-suspect loop (2026-08-19): pairs at
    the SAME venue and the SAME start minute that the display matcher does NOT
    already collapse and no judge has ruled on. These go to an agentic judge
    (same/distinct — catches translations, renames, everything string rules
    miss); a hardcoded time-collapse is wrong on live counter-examples
    (Bartram's runs three distinct events at Sat 10:00). Caller supplies the
    candidate events (usually the visible window)."""
    verdicts = load_verdicts(sess) if verdicts is None else verdicts
    by_slot = defaultdict(list)
    for e in events:
        # midnight = date-only/all-day (events_match reads it the same way);
        # two all-day listings at one venue share no meaningful "minute"
        if e.venue_id and e.start and e.start.time() != dt.time():
            by_slot[(e.venue_id, e.start)].append(e)
    pairs = []
    for members in by_slot.values():
        members.sort(key=lambda row: row.id or 0)
        for i, left in enumerate(members):
            for right in members[i + 1:]:
                if events_match(left, right):
                    continue    # the matcher already collapses this pair
                if verdict_for(verdicts, left, right):
                    continue    # already judged — never re-surfaces
                pairs.append((left, right))
    return pairs


def find_matching_event(sess, candidate: Event, *, source_id: str | None = None) -> Event | None:
    """Find the strongest existing match, refusing ambiguous ties."""
    matches = []
    if not candidate.start:
        return None
    day0 = dt.datetime.combine(candidate.start.date(), dt.time())
    day1 = day0 + dt.timedelta(days=1)
    rows = (sess.query(Event)
            .filter(Event.venue_id == candidate.venue_id,
                    Event.start >= day0, Event.start < day1).all())
    if source_id is not None:
        rows = [row for row in rows if row.source_id == source_id]
    for row in rows:
        if row is candidate:
            continue
        if events_match(row, candidate):
            matches.append((event_name_similarity(row.name, candidate.name), row))
    if not matches:
        return None
    matches.sort(key=lambda item: (-item[0], item[1].id or 0))
    if len(matches) > 1 and matches[0][0] == matches[1][0]:
        return None
    return matches[0][1]


def source_rank(sess, source_id: str | None, venue_id: str | None) -> tuple[int, int]:
    """Primary dedicated venue calendars outrank dedicated feeds, then aggregators."""
    source = sess.get(PipelineSource, source_id) if source_id else None
    if source is None:
        return (0, 0)
    primary = False
    if venue_id:
        primary = (sess.query(SourceCoverage)
                   .filter_by(source_id=source.id, target_kind="venue",
                              target_id=venue_id, is_primary=True).first()) is not None
    return (2 if primary and not source.is_aggregator else
            1 if not source.is_aggregator else 0, 1 if source.health == "ok" else 0)


EVENT_COPY_FIELDS = (
    "name", "start", "end", "all_day", "source_id", "group_id", "category", "tags",
    "organizer", "cost",
    "cost_min_cents", "cost_max_cents", "is_free", "description", "image", "url",
    "discovered_via", "lifecycle", "verified_by",
)


def merge_event_values(sess, survivor: Event, duplicate: Event) -> Event:
    """Copy the better listing onto survivor without changing its stable identity."""
    promote = source_rank(sess, duplicate.source_id, duplicate.venue_id) > source_rank(
        sess, survivor.source_id, survivor.venue_id)
    same_source = duplicate.source_id == survivor.source_id
    for field in EVENT_COPY_FIELDS:
        # A suppression (operators.suppress_event) outranks the feed: the whole
        # point is that upstream keeps listing something we know is wrong, so a
        # re-seen observation must not resurrect it.
        if field == "lifecycle" and survivor.lifecycle == "suppressed":
            continue
        incoming = getattr(duplicate, field)
        current = getattr(survivor, field)
        if incoming is not None and (promote or same_source or current is None or current == ""):
            setattr(survivor, field, incoming)
    survivor.first_seen = min(survivor.first_seen, duplicate.first_seen)
    survivor.last_seen = max(survivor.last_seen, duplicate.last_seen)
    return survivor


def merge_event_rows(sess, survivor: Event, duplicate: Event) -> Event:
    """Coalesce one source's repeated row, preserving score history.

    Different sources are independent observations. They are canonicalized only
    for public export and must never be destroyed by this storage-layer helper.
    """
    if survivor.source_id != duplicate.source_id:
        raise ValueError("cross-source event observations must be retained")
    merge_event_values(sess, survivor, duplicate)
    # Scores are historical assets. Keep every row, but attach them to the survivor.
    for score in sess.query(EventScore).filter_by(event_id=duplicate.id).all():
        score.event_id = survivor.id
        score.dedupe_key = survivor.dedupe_key
    sess.delete(duplicate)
    sess.flush()
    return survivor


def deduplicate_events(sess, *, apply: bool = False) -> list[tuple[Event, Event]]:
    """Find legacy pairs; optionally coalesce same-source repeats only."""
    buckets = defaultdict(list)
    for event in sess.query(Event).order_by(Event.id).all():
        if event.start:
            buckets[(event.venue_id, event.start.date())].append(event)
    pairs = []
    for members in buckets.values():
        survivors = []
        for event in members:
            matching = [row for row in survivors if events_match(row, event)]
            match = next((row for row in matching if row.source_id == event.source_id),
                         matching[0] if matching else None)
            if match is None:
                survivors.append(event)
                continue
            # Pick the source-preferred row as survivor, then the older stable id.
            if (source_rank(sess, event.source_id, event.venue_id), -(event.id or 0)) > (
                    source_rank(sess, match.source_id, match.venue_id), -(match.id or 0)):
                match, event = event, match
                survivors.remove(event)
                survivors.append(match)
            pairs.append((match, event))
            if apply and match.source_id == event.source_id:
                merge_event_rows(sess, match, event)
            else:
                # Keep cross-source observations available for later same-source
                # comparisons even though export will display one representative.
                survivors.append(event)
    return pairs


def canonical_event_rows(sess, events: list[Event]) -> list[Event]:
    """Choose display survivors without changing the durable event observations.

    This is export's safety net for legacy rows and sources ingested before the
    matching layer existed. A primary dedicated source wins; equal-ranked rows
    retain the older stable id. Ambiguous matches remain visible.
    """
    # Judge verdicts outrank the string rules: 'same' collapses a pair the
    # matcher can't see (a translated title, a rename), 'distinct' protects a
    # pair the rules might someday wrongly collapse. Detector + judge + this
    # consumer are the whole dupe-suspect loop (2026-08-19).
    verdicts = load_verdicts(sess)

    def display_match(left, right):
        ruled = verdict_for(verdicts, left, right)
        if ruled is not None:
            return ruled == "same"
        return events_match(left, right)

    # Buckets key on the local day only: venue equality is events_match's gate,
    # and a venue-less observation must be able to meet its venued twin.
    buckets = defaultdict(list)
    undated = []
    for event in events:
        if event.start:
            buckets[event.start.date()].append(event)
        else:
            undated.append(event)

    def display_rank(event):
        # A pinned observation beats a venue-less one (the reader gets a map pin
        # and a hood); only then does source preference break the tie.
        return (1 if event.venue_id else 0,
                source_rank(sess, event.source_id, event.venue_id))

    def coalesce_url_twins(members):
        # URL twins are settled BEFORE the name pass: a row matching one
        # survivor by URL and another by name would otherwise trip the
        # ambiguity guard and display MORE copies than having no rule at all.
        by_key = defaultdict(list)
        for event in members:
            key = event_url_key(event.url)
            if key:
                by_key[key].append(event)
        drop = set()
        for twins in by_key.values():
            clusters = []
            for event in sorted(twins, key=lambda row: row.id or 0):
                home = next((c for c in clusters if events_match(c[0], event)), None)
                clusters.append([event]) if home is None else home.append(event)
            for cluster in clusters:
                # A timed listing beats a date-only cross-post: it carries the
                # canonical hour and can still meet its name-twins downstream.
                best = max(cluster, key=lambda row: (
                    display_rank(row),
                    1 if row.start and row.start.time() != dt.time() else 0,
                    -(row.id or 0)))
                drop.update(id(row) for row in cluster if row is not best)
        return [row for row in members if id(row) not in drop]

    kept = list(undated)
    for members in buckets.values():
        members = coalesce_url_twins(members)
        survivors = []
        for event in sorted(members, key=lambda row: row.id or 0):
            matches = [row for row in survivors if display_match(row, event)]
            if len(matches) != 1:
                survivors.append(event)
                continue
            current = matches[0]
            if display_rank(event) > display_rank(current):
                survivors[survivors.index(current)] = event
        kept.extend(survivors)
    chosen = {id(row) for row in kept}
    return [row for row in events if id(row) in chosen]


def venue_candidates(sess) -> list[tuple[Venue, Venue, str]]:
    """Report venue pairs worth human review; never merge them automatically."""
    rows = sess.query(Venue).order_by(Venue.id).all()
    out = []
    for i, left in enumerate(rows):
        for right in rows[i + 1:]:
            reason = venue_match_reason(left, name=right.name, address=right.address,
                                        website=right.website, lat=right.lat, lng=right.lng)
            if reason:
                out.append((left, right, reason))
    return out


def visible_window_events(sess, days: int = 10) -> list[Event]:
    """The events a reader could actually see — same window/visibility/lifecycle
    filters as export. The judge should only ever spend judgment on these."""
    today0 = dt.datetime.combine(dt.date.today(), dt.time())
    horizon = today0 + dt.timedelta(days=days)
    vmap = {v.id: v for v in sess.query(Venue).all()}
    gmap = {g.id: g for g in sess.query(Group).all()}
    smap = {s.id: s for s in sess.query(PipelineSource).all()}
    return [e for e in sess.query(Event).all()
            if e.start and today0 <= e.start < horizon
            and e.lifecycle != "unlisted"
            and is_visible(e, vmap.get(e.venue_id), gmap.get(e.group_id),
                           smap.get(e.source_id))]


def suspects_report(sess, days: int = 10) -> list[dict]:
    """--suspects: unjudged same-venue/same-minute pairs as judge-ready JSON.
    Each entry carries what a judge needs to rule same/distinct; verdicts come
    back through --import-verdicts (operators.record_dedupe_verdict is the only
    write path, mirroring scorer.py's --pending/--import split)."""
    def side(e):
        return {"dedupe_key": e.dedupe_key, "name": e.name,
                "start": e.start.isoformat() if e.start else None,
                "venue_id": e.venue_id, "source_id": e.source_id,
                "url": e.url, "cost": e.cost,
                "description": (e.description or "")[:300] or None}
    return [{"a": side(a), "b": side(b)}
            for a, b in dupe_suspects(sess, visible_window_events(sess, days))]


def import_verdicts(sess, path) -> int:
    """Write a judge's returned verdicts. File shape: a list (or {'verdicts':
    [...]}) of {'a': dedupe_key, 'b': dedupe_key, 'verdict': 'same'|'distinct',
    'reason': str}. All-or-nothing: one invalid entry rejects the file."""
    import json
    from operators import record_dedupe_verdict
    data = json.load(open(path))
    entries = data.get("verdicts", []) if isinstance(data, dict) else data
    errors = [e for e in entries
              if not (e.get("a") and e.get("b") and e.get("verdict") in ("same", "distinct"))]
    if errors:
        print(f"import: {len(errors)} invalid entr{'y' if len(errors) == 1 else 'ies'} "
              f"(need a, b, verdict∈{{same,distinct}}) — nothing written")
        return 1
    for e in entries:
        record_dedupe_verdict(e["a"], e["b"], e["verdict"],
                              reason=e.get("reason"),
                              judged_by=e.get("judged_by", "rater"), sess=sess)
    sess.commit()
    print(f"import: {len(entries)} verdict(s) persisted")
    return 0


def main(apply_events: bool = False):
    sess = Session()
    pairs = deduplicate_events(sess, apply=apply_events)
    venues = venue_candidates(sess)
    action = "same-source repeats coalesced; cross-source observations retained" if apply_events else "candidate"
    print(f"events: {len(pairs)} duplicate pair(s) {action}")
    for keep, drop in pairs[:50]:
        day = keep.start.date().isoformat() if keep.start else "?"
        print(f"  {day}  {keep.venue_id}: {keep.name!r}  ~=  {drop.name!r}")
    print(f"venues: {len(venues)} candidate pair(s); review then use operators.merge_venues")
    for left, right, reason in venues[:50]:
        print(f"  {left.id}  ~=  {right.id}  ({reason})")
    if apply_events:
        sess.commit()
    else:
        sess.rollback()
    sess.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply-events", action="store_true",
                        help="coalesce matched event rows (default is report-only)")
    parser.add_argument("--suspects", action="store_true",
                        help="print unjudged dupe-suspect pairs as judge-ready JSON")
    parser.add_argument("--days", type=int, default=10,
                        help="suspect window, today .. +days (default 10, matches export)")
    parser.add_argument("--out", type=str, default=None,
                        help="write --suspects JSON to a file instead of stdout")
    parser.add_argument("--import-verdicts", dest="verdicts_path", type=str,
                        help="persist a judge's verdicts JSON (the only write path)")
    args = parser.parse_args()
    if args.suspects or args.verdicts_path:
        import json as _json
        import sys
        sess = Session()
        if args.verdicts_path:
            sys.exit(import_verdicts(sess, args.verdicts_path))
        pairs = suspects_report(sess, days=args.days)
        payload = _json.dumps(pairs, indent=2)
        if args.out:
            open(args.out, "w").write(payload)
            print(f"{len(pairs)} suspect pair(s) -> {args.out}")
        else:
            print(payload)
        sess.close()
    else:
        main(apply_events=args.apply_events)
