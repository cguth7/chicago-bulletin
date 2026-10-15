"""
operators.py — the ONLY sanctioned way to mutate the durable layer
(venues / groups / pipeline_sources / source_coverage).

Every operator is IDEMPOTENT (safe to re-run; touches only its delta) and NEVER
drops tables. The DB is the source of truth — there is no JSON seed anymore.
See docs/leads-pipeline.md for the full contract + the worked Cole's example.

The lifecycle (Option B — a "lead" is an un-built row in its real table):

    add_venue_lead / add_group_lead   ->  candidate row is born          (a lead)
    update_venue_fields               ->  fill/correct a venue's attrs    (the patch)
    add_source_lead                   ->  PipelineSource(status none|planned|built)
    mark_source_built                 ->  flip a source to built          (the build)
    set_source_aggregator             ->  dedicated <-> aggregator routing (the fan-out)
    connect_coverage                  ->  link source -> venue/group      (turns it on)
    retarget_coverage / remove_coverage -> repoint / undo a bad link      (the correction)
    decide_monitored                  ->  owns venue.monitored            (do we want it)
    remove_alias                      ->  undo a wrong auto-alias         (the un-merge)
    record_dedupe_verdict             ->  persist a same/distinct call    (the judgment)

`decide_monitored` already lives in discover.py; we re-export it so callers have
one import surface.
"""
from __future__ import annotations

import datetime as dt
from urllib.error import HTTPError, URLError

from models import (
    DedupeVerdict, Event, EventSuppression, Group, PipelineSource, Session, SourceCoverage, Venue,
    VenueAlias, now, slug,
)
from discover import decide_monitored  # re-export: the monitoring operator
from scrapers import ADAPTERS, UA, validate_events

# platform_guess -> (adapter, build status). Moved here from seed.py so the build
# ladder lives with the operators. "built" = adapter confirmed working city-wide;
# "planned" = adapter exists for another city, needs a port; "none" = needs work.
PLATFORM_MAP = {
    "tribe_wp":         ("tribe_wp", "built"),
    "squarespace":      ("squarespace", "built"),
    "localist":         ("localist", "built"),
    "bibliocommons":    ("bibliocommons", "built"),
    "wix":              ("wix", "built"),
    "ical":             ("ical", "built"),
    "html_jsonld":      ("html_jsonld", "built"),
    "eventbrite":       ("eventbrite", "planned"),
    "humanitix":        ("humanitix", "planned"),
    "ticketmaster":     ("ticketmaster_api", "planned"),
    "seetickets":       ("seetickets", "none"),
    "dice":             ("dice", "none"),
    "axs":              ("axs", "none"),
    "songkick":         ("songkick", "none"),
    "bandsintown":      ("bandsintown", "none"),
    "wordpress_custom": ("html", "none"),
    "custom_html":      ("html", "none"),
    "custom_json":      ("custom_json", "none"),
    "rss":              ("rss", "none"),
    "api":              ("custom_json", "none"),
    "none":             ("manual", "none"),
    "unknown":          ("html", "none"),
}


def platform(platform_guess: str | None) -> tuple[str, str]:
    """Map a human platform_guess to (adapter, build_status). The bootstrap helper
    used when you only know the platform, not the adapter — e.g. from a leads agent
    or a probe. If you already know the adapter, pass it to add_source_lead directly."""
    return PLATFORM_MAP.get((platform_guess or "unknown").lower(), ("html", "none"))


def _finish(sess, obj, own):
    """When the operator owns its session: commit, then refresh+expunge so the
    returned object stays usable (attributes loaded) after the session closes.
    When the caller passed a session, leave commit/lifecycle to them."""
    if own:
        sess.commit()
        sess.refresh(obj)
        sess.expunge(obj)
    return obj


# ─────────────────────────────── entity leads ──────────────────────────────

def add_venue_lead(name, *, kind="other", address=None, neighborhood=None,
                   website=None, hosts_events="unknown", discovered_via="llm_recall",
                   discovered_by="agent", notes=None, sess=None) -> Venue:
    """Mint a candidate Venue if new; idempotent on slug (re-touch bumps last_seen).
    This is 'a lead is born'. Status stays candidate — judgment is decide_monitored's."""
    own = sess is None
    sess = sess or Session()
    # create(checkfirst) is a tiny forward migration for durable DBs made before
    # aliases existed. init_db/Base.metadata still own clean-database creation.
    # Use this session's connection so the schema check participates in its
    # transaction (important for callers that intentionally roll back a batch).
    VenueAlias.__table__.create(bind=sess.connection(), checkfirst=True)
    vid = slug(name)
    alias = sess.get(VenueAlias, vid)
    if alias is not None:
        v = sess.get(Venue, alias.venue_id)
        if v is not None:
            v.last_seen = now()
            return _finish(sess, v, own)
    v = sess.get(Venue, vid)
    if v is None:
        from dedupe import find_matching_venue
        v, reason = find_matching_venue(sess, name=name, address=address, website=website)
        if v is not None:
            sess.add(VenueAlias(alias_id=vid, venue_id=v.id, name=name, reason=reason))
            v.last_seen = now()
        else:
            v = Venue(
                id=vid, name=name, kind=kind, address=address, neighborhood=neighborhood,
                website=website, hosts_events=hosts_events,
                status="candidate", monitored=False,
                discovered_via=discovered_via, discovered_by=discovered_by, notes=notes,
            )
            sess.add(v)
    else:
        v.last_seen = now()
    return _finish(sess, v, own)


def merge_venues(survivor_id, duplicate_id, *, reason="manual duplicate review",
                 sess=None) -> tuple[Venue, dict[str, int]]:
    """Explicitly merge one venue into another and retain the old slug as an alias.

    Events and coverage are repointed, same-source collisions are coalesced,
    cross-source observations are retained, descriptive gaps are filled, and
    recurring series are re-derived. The caller must name the survivor: fuzzy
    venue candidates are only reported, never auto-applied.
    """
    if survivor_id == duplicate_id:
        raise ValueError("survivor and duplicate must be different venues")
    own = sess is None
    sess = sess or Session()
    VenueAlias.__table__.create(bind=sess.connection(), checkfirst=True)
    survivor = sess.get(Venue, survivor_id)
    duplicate = sess.get(Venue, duplicate_id)
    if survivor is None:
        raise KeyError(f"no Venue '{survivor_id}'")
    if duplicate is None:
        alias = sess.get(VenueAlias, duplicate_id)
        if alias and alias.venue_id == survivor_id:
            return _finish(sess, survivor, own), {"events": 0, "event_duplicates": 0,
                                                   "coverage": 0, "aliases": 0}
        raise KeyError(f"no Venue '{duplicate_id}'")

    counts = {"events": 0, "event_duplicates": 0, "coverage": 0, "aliases": 0}
    from dedupe import events_match, merge_event_rows
    for event in list(sess.query(Event).filter_by(venue_id=duplicate_id).all()):
        event.venue_id = survivor_id
        counts["events"] += 1
        match = next((row for row in sess.query(Event).filter_by(venue_id=survivor_id).all()
                      if row is not event and row.source_id == event.source_id
                      and events_match(row, event)), None)
        if match is not None:
            merge_event_rows(sess, match, event)
            counts["event_duplicates"] += 1

    for cov in list(sess.query(SourceCoverage)
                    .filter_by(target_kind="venue", target_id=duplicate_id).all()):
        existing = (sess.query(SourceCoverage)
                    .filter_by(source_id=cov.source_id, target_kind="venue",
                               target_id=survivor_id).first())
        if existing:
            existing.is_primary = existing.is_primary or cov.is_primary
            existing.target_page_url = existing.target_page_url or cov.target_page_url
            existing.routing_key = existing.routing_key or cov.routing_key
            sess.delete(cov)
        else:
            cov.target_id = survivor_id
        counts["coverage"] += 1

    for field in ("address", "neighborhood", "lat", "lng", "website", "notes"):
        if getattr(survivor, field) in (None, "") and getattr(duplicate, field) not in (None, ""):
            setattr(survivor, field, getattr(duplicate, field))
    survivor.monitored = survivor.monitored or duplicate.monitored
    if duplicate.status == "verified":
        survivor.status = "verified"
    survivor.first_seen = min(survivor.first_seen, duplicate.first_seen)
    survivor.last_seen = max(survivor.last_seen, duplicate.last_seen)

    for alias in sess.query(VenueAlias).filter_by(venue_id=duplicate_id).all():
        alias.venue_id = survivor_id
        counts["aliases"] += 1
    prior = sess.get(VenueAlias, duplicate_id)
    if prior is None:
        sess.add(VenueAlias(alias_id=duplicate_id, venue_id=survivor_id,
                            name=duplicate.name, reason=reason))
        counts["aliases"] += 1
    sess.delete(duplicate)
    sess.flush()
    from series import derive_series
    derive_series(sess)
    return _finish(sess, survivor, own), counts


def remove_alias(alias_id, *, sess=None) -> bool:
    """Delete a VenueAlias row that never should have existed — the sanctioned
    undo for a wrong auto-alias (add_venue_lead's fuzzy match once resolved the
    Boathouse at FDR Park onto fdr-park itself, 2026-08-19, and the fix was a
    hand-deleted row). Returns True if a row was removed, False if it was
    already gone (idempotent). The venue the alias pointed at is untouched —
    this only re-opens the slug so a future mint creates a fresh row."""
    own = sess is None
    sess = sess or Session()
    VenueAlias.__table__.create(bind=sess.connection(), checkfirst=True)
    alias = sess.get(VenueAlias, alias_id)
    removed = alias is not None
    if removed:
        sess.delete(alias)
        sess.flush()
    if own:
        sess.commit()
    return removed


def add_group_lead(name, *, kind="presenter", website=None, discovered_via="llm",
                   discovered_by="agent", notes=None, sess=None) -> Group:
    """Mint a candidate Group (the 'who') if new; idempotent on slug."""
    own = sess is None
    sess = sess or Session()
    gid = slug(name)
    g = sess.get(Group, gid)
    if g is None:
        g = Group(id=gid, name=name, kind=kind, website=website, status="candidate",
                  monitored=False, discovered_via=discovered_via,
                  discovered_by=discovered_by, notes=notes)
        sess.add(g)
    else:
        g.last_seen = now()
    return _finish(sess, g, own)


def update_venue_fields(venue_id, *, name=None, address=None, neighborhood=None,
                        website=None, kind=None, notes=None, sess=None) -> Venue:
    """Correct/fill a venue's descriptive ATTRIBUTES (not lifecycle). Use this
    instead of a raw Session write when a lead's address/hood/website/kind shows up
    after the row was minted — e.g. Scout/Max found the street address that lets
    geocode.py resolve coords. Only non-None args are applied, so it's a safe patch;
    a passed value DOES overwrite (this is a deliberate correction, unlike
    add_venue_lead which only fills on create). Idempotent.

    `name` is the DISPLAY name only — the id/slug never changes (ingest and
    discover resolve by id, so a rename can't orphan coverage; added 2026-08-29
    when MIT's Localist minted building-code names like "Building W18" that
    needed human names). Same-room-different-slug is still merge_venues.

    Deliberately CANNOT touch status / monitored / discovered_via/by — those are
    write-once provenance or owned by verify_target / decide_monitored. Lat/lng are
    owned by geocode.py (set address here, then run geocode)."""
    own = sess is None
    sess = sess or Session()
    v = sess.get(Venue, venue_id)
    if v is None:
        raise KeyError(f"no Venue '{venue_id}'")
    if name is not None:
        v.name = name
    if address is not None:
        v.address = address
    if neighborhood is not None:
        v.neighborhood = neighborhood
    if website is not None:
        v.website = website
    if kind is not None:
        v.kind = kind
    if notes is not None:
        v.notes = notes
    return _finish(sess, v, own)


def update_group_fields(group_id, *, name=None, kind=None, website=None,
                        description=None, join_url=None, notes=None, sess=None) -> Group:
    """Correct/fill a group's descriptive ATTRIBUTES — the group twin of
    update_venue_fields. `description` is the one public line (what it is, who
    it's for); `join_url` is how to join (the site falls back to `website`).
    Only non-None args are applied; a passed value overwrites. Idempotent.
    Deliberately CANNOT touch status / monitored / discovered_via/by — those are
    owned by verify_target / decide_monitored or are write-once provenance."""
    own = sess is None
    sess = sess or Session()
    g = sess.get(Group, group_id)
    if g is None:
        raise KeyError(f"no Group '{group_id}'")
    for attr, val in (("name", name), ("kind", kind), ("website", website),
                      ("description", description), ("join_url", join_url),
                      ("notes", notes)):
        if val is not None:
            setattr(g, attr, val)
    return _finish(sess, g, own)


# ─────────────────────────────── source leads ──────────────────────────────

def add_source_lead(name, *, adapter=None, platform_guess=None, url=None,
                    api_url=None, is_aggregator=False, has_api=False,
                    status=None, discovered_via=None, discovered_by=None,
                    notes=None, verdict=None, sess=None) -> PipelineSource:
    """Register a scrapeable endpoint (a LEAD). Pass `adapter` if known, or
    `platform_guess` to derive it via PLATFORM_MAP. Idempotent on slug.

    A lead is NEVER born 'built' — 'built' is earned via mark_source_built's live
    gate, not a guess. So a PLATFORM_MAP 'built' is capped to 'planned' here
    ('we have an adapter, Max should confirm it works'). Explicit status= is honored
    only up to 'planned'; pass mark_source_built to actually build.
    `verdict` ('go'|'no_go') is the Scout's call — leave it None from Tej."""
    if verdict not in (None, "go", "no_go"):
        raise ValueError(f"verdict must be 'go' or 'no_go', not {verdict!r}")
    own = sess is None
    sess = sess or Session()
    if adapter is None:
        adapter, st = platform(platform_guess)
        status = status if status is not None else st
    elif status is None:
        # an explicit adapter is a stronger signal than a platform guess: known
        # adapter -> 'planned' (Max confirms), unknown -> 'none'. (This used to
        # fall through to platform(None) == ('html','none') and silently strand
        # known-adapter leads at 'none' — found by Tej in the 2026-06-09 e2e test.)
        status = "planned" if adapter in ADAPTERS else "none"
    # the cap: a freshly-added lead tops out at 'planned'. Only the gated
    # mark_source_built/build_source may set 'built'.
    if status == "built":
        status = "planned"
    sid = slug(name)
    s = sess.get(PipelineSource, sid)
    if s is None:
        s = PipelineSource(
            id=sid, name=name, adapter=adapter, url=url, api_url=api_url,
            has_api=has_api, is_aggregator=is_aggregator, status=status,
            discovered_via=discovered_via, discovered_by=discovered_by, notes=notes,
            scout_verdict=verdict,
        )
        sess.add(s)
    else:
        # already exists: only fill gaps, don't clobber a built source's corrections
        if url and not s.url:
            s.url = url
        if notes:
            s.notes = notes
        if verdict:
            s.scout_verdict = verdict
    return _finish(sess, s, own)


def update_source_fields(source_id, *, api_url=None, has_api=None, script_ref=None,
                         notes=None, adapter=None, url=None, sess=None) -> PipelineSource:
    """Correct/fill a source's descriptive ATTRIBUTES post-mint — the missing
    parity twin of update_venue_fields (2026-06-09 audit: Tej had no way to fix a
    source row, and Scout had to abuse set_source_status to correct attrs).
    Only non-None args apply; a passed value DOES overwrite (deliberate correction).

    adapter/url are FUNCTIONAL, not descriptive: on a status='built' source they
    define what the gate validated, so changing them here would falsify 'built'.
    For built sources route those through mark_source_built (re-gates) or
    set_source_status (honest downgrade). Status itself is never touchable here."""
    own = sess is None
    sess = sess or Session()
    s = sess.get(PipelineSource, source_id)
    if s is None:
        raise KeyError(f"no PipelineSource '{source_id}'")
    if (adapter or url) and s.status == "built":
        raise ValueError(
            f"'{source_id}' is built — changing adapter/url would falsify the build. "
            "Use mark_source_built(adapter=, url=) to re-gate, or set_source_status to downgrade.")
    if adapter:
        s.adapter = adapter
    if url:
        s.url = url
    if api_url is not None:
        s.api_url = api_url
    if has_api is not None:
        s.has_api = has_api
    if script_ref is not None:
        s.script_ref = script_ref
    if notes is not None:
        s.notes = notes
    return _finish(sess, s, own)


def probe_source_live(adapter, url, name, *, days=120):
    """THE BUILD GATE. Run the source's actual adapter over [today, +days] and
    classify the result into (health, detail). Pure check — writes nothing.

      ok          adapter returned >=1 VALID event (name + in-window date)
      no_events   adapter ran clean but returned 0 valid (dormant / past-only)
      http_error  the fetch hit a 4xx/5xx (404, 403/Cloudflare, ...)
      parse_error got a response but couldn't decode/extract (wrong adapter/shape)

    detail carries the actual message thrown so you can SEE the error
    ('HTTP 403', 'all 30 events past', 'JSONDecodeError: ...')."""
    fn = ADAPTERS.get(adapter)
    if fn is None:
        return "parse_error", f"no adapter '{adapter}'"
    start = dt.date.today()
    end = start + dt.timedelta(days=days)
    try:
        events = fn(url, name, start, end)
    except HTTPError as e:
        return "http_error", f"HTTP {e.code}"
    except (URLError, TimeoutError) as e:
        return "http_error", f"{type(e).__name__}: {e}"
    except Exception as e:                      # JSON decode, key errors, bad shape
        return "parse_error", f"{type(e).__name__}: {e}"[:140]
    v = validate_events(events, start, end)
    if v["n_valid"]:
        return "ok", f"{v['n_valid']} valid (e.g. {v['sample']})"
    if v["reason"] == "all_invalid":
        return "no_events", f"{v['n_total']} returned, none with name+in-range date (past-only?)"
    # 0 events: our adapters swallow HTTP/parse errors and return [] (printing to
    # stderr), so an empty list is ambiguous — genuinely dormant, or the URL is
    # dead/blocked. Do a lightweight reachability check to recover the real reason.
    reach = _url_reachable(url)
    if reach is not None and reach >= 400:
        return "http_error", f"HTTP {reach}"
    return "no_events", "adapter returned 0 events"


def _url_reachable(url):
    """Return the HTTP status of a GET on url (the gate uses this to tell a dead/
    blocked URL apart from a genuinely-empty calendar, since adapters swallow
    HTTP errors). None if the check itself couldn't run."""
    from urllib.request import Request, urlopen
    if not url:
        return None
    # scrapers.UA, not a local copy: a duplicated string silently diverged once
    # (campus-philly's Cloudflare 403'd the old Mac UA while the adapters had
    # moved on) — the gate must see exactly what the adapters send.
    try:
        with urlopen(Request(url, headers={"User-Agent": UA}), timeout=15) as r:
            return r.status
    except HTTPError as e:
        return e.code
    except (URLError, TimeoutError, Exception):
        return None


def mark_source_built(source_id, *, adapter=None, url=None, force=False,
                      sess=None) -> PipelineSource:
    """The build flip: status='built', GATED on a live probe. We run the adapter;
    a source only goes 'built' with health='ok' if it returns >=1 valid event.

    If the gate fails we STILL set status='built' (the pipeline is wired — it's in
    ingest's worklist) but stamp health=no_events|http_error|parse_error + the
    detail, so it's a visible built-but-failing source, not a silent false-built.
    Stamps last_scraped. Idempotent. (force is accepted for symmetry; the gate
    always runs and always records health regardless.)"""
    own = sess is None
    sess = sess or Session()
    s = sess.get(PipelineSource, source_id)
    if s is None:
        raise KeyError(f"no PipelineSource '{source_id}'")
    if adapter:
        s.adapter = adapter
    if url:
        s.url = url
    health, detail = probe_source_live(s.adapter, s.url, s.name)
    s.status = "built"
    s.health = health
    s.health_detail = detail
    s.last_scraped = now()
    return _finish(sess, s, own)


def set_source_status(source_id, status, *, adapter=None, url=None, api_url=None,
                      notes=None, verdict=None, sess=None) -> PipelineSource:
    """Honest downgrade/correction of an existing source's status. Two callers:
    Scout (triage — GO -> 'planned' / NO-GO -> 'none', reason in notes) and Max
    (the inverse of mark_source_built — a falsely 'built' source that yields no
    events -> 'none'). Optionally corrects adapter/url/api_url and appends notes
    so the real endpoint (or the dead end) is recorded. Idempotent.
    Scout always passes `verdict` ('go' with 'planned', 'no_go' with 'none') —
    that's what moves a source out of the Scout queue in funnel.py."""
    if verdict not in (None, "go", "no_go"):
        raise ValueError(f"verdict must be 'go' or 'no_go', not {verdict!r}")
    own = sess is None
    sess = sess or Session()
    s = sess.get(PipelineSource, source_id)
    if s is None:
        raise KeyError(f"no PipelineSource '{source_id}'")
    if adapter:
        s.adapter = adapter
    if url:
        s.url = url
    if api_url:
        s.api_url = api_url
    if notes:
        s.notes = notes
    if verdict:
        s.scout_verdict = verdict
    s.status = status
    return _finish(sess, s, own)


def set_source_aggregator(source_id, is_aggregator=True, *, note=None,
                          sess=None) -> PipelineSource:
    """Flip a source between dedicated and aggregator ROUTING semantics.

    The flag decides which ingest pass runs the source (dump-on-one-venue vs
    route-per-event) and whether decide_monitored counts its coverage as a
    dedicated feed — so it's functional, not descriptive, and gets its own
    operator instead of riding update_source_fields. Flipping does NOT touch
    status/health: the gate validated the endpoint+adapter, and those didn't
    change. After the flip every covered venue is re-judged (a venue whose only
    'dedicated' feed just became an aggregator must not stay monitored on that
    basis, and vice versa). Idempotent."""
    own = sess is None
    sess = sess or Session()
    s = sess.get(PipelineSource, source_id)
    if s is None:
        raise KeyError(f"no PipelineSource '{source_id}'")
    s.is_aggregator = bool(is_aggregator)
    if note:
        s.notes = f"{s.notes}\n{note}" if s.notes else note
    for cov in sess.query(SourceCoverage).filter_by(source_id=source_id).all():
        t = sess.get(Venue if cov.target_kind == "venue" else Group, cov.target_id)
        if t is not None:
            decide_monitored(t, sess)
    return _finish(sess, s, own)


# ─────────────────────────────── connections ───────────────────────────────

def _bare_routing_key(key):
    """coverage.routing_key is the BARE park slug: ingest._park_slug strips the
    `park:` prefix from the event TAG before lookup, so a key stored WITH the
    prefix never matches. 21 Cambridge rows sat inert for a week that way
    (09-06). Normalize at the write boundary instead of trusting callers."""
    if key is None:
        return None
    key = key.strip()
    if key.lower().startswith("park:"):
        key = key[len("park:"):].strip()
    return key or None


def connect_coverage(source_id, target_kind, target_id, *, is_primary=False,
                     adoption="adopted", usefulness="unknown", target_page_url=None,
                     routing_key=None, note=None, discovered_by="agent",
                     sess=None) -> SourceCoverage:
    """Link a source to the venue/group it lists events for — 'if it turns on a
    venue or group, make a row connecting that in coverage'. Idempotent on the
    (source_id, target_kind, target_id) triple.

    target_page_url = the target's page ON that source (an Eventbrite /o/ page).
    routing_key     = aggregator->target join key ingest routes by (park slug).
    Neither is ever a scrape endpoint — that's PipelineSource.url, full stop."""
    own = sess is None
    sess = sess or Session()
    routing_key = _bare_routing_key(routing_key)
    if target_kind == "group":
        src = sess.get(PipelineSource, source_id)
        if src is not None and src.is_aggregator:
            # The aggregator ingest pass ROUTES via venue coverage only; group
            # coverage on an aggregator is an organizer stamp on the routed
            # events (ingest._organizer_group), never a route. Group-only
            # coverage therefore yields nothing — it froze campus-philly /
            # penn-today / bowerbird at built/ok/0 (later.md P1) — so require
            # at least one routable venue row first.
            routed = (sess.query(SourceCoverage)
                      .filter_by(source_id=source_id, target_kind="venue")
                      .filter(SourceCoverage.routing_key.isnot(None)).first())
            if routed is None:
                raise ValueError(
                    f"'{source_id}' is an aggregator with no routable venue "
                    "coverage; group coverage alone is never routed. Cover its "
                    "venues with routing_keys first (then the group row stamps "
                    "the organizer), or set_source_aggregator(False).")
    cov = (sess.query(SourceCoverage)
           .filter_by(source_id=source_id, target_kind=target_kind, target_id=target_id)
           .first())
    if cov is None:
        cov = SourceCoverage(
            source_id=source_id, target_kind=target_kind, target_id=target_id,
            is_primary=is_primary, adoption=adoption, usefulness=usefulness,
            target_page_url=target_page_url, routing_key=routing_key,
            note=note, discovered_by=discovered_by,
        )
        sess.add(cov)
    else:
        # re-touch: keep judged fields, only upgrade is_primary. Passed values DO
        # overwrite (deliberate correction, same semantics as update_venue_fields).
        if is_primary:
            cov.is_primary = True
        if target_page_url:
            cov.target_page_url = target_page_url
        if routing_key:
            cov.routing_key = routing_key
        if cov.adoption == "discovered" and adoption != "discovered":
            # a vet touching a firehose-minted row IS the adoption verdict
            # (Tej, vet round 5: adoptions had to go remove+connect otherwise)
            cov.adoption = adoption
    return _finish(sess, cov, own)


def retarget_coverage(source_id, target_kind, target_id, new_target_id, *,
                      new_target_kind=None, routing_key=None, sess=None) -> SourceCoverage:
    """Repoint an existing coverage row at the target it should have named —
    e.g. a source connected to its Group when its events really belong to venues
    (penn-today), or a coverage row on the wrong room. The new target must
    already exist in its table; if the (source, new target) row already exists
    the old row folds into it (is_primary upgrades, page url / routing key fill
    gaps) instead of duplicating. Both venues get re-judged. Idempotent."""
    own = sess is None
    sess = sess or Session()
    routing_key = _bare_routing_key(routing_key)
    new_kind = new_target_kind or target_kind
    cov = (sess.query(SourceCoverage)
           .filter_by(source_id=source_id, target_kind=target_kind, target_id=target_id)
           .first())
    if cov is None:
        # already retargeted? idempotent success only if the destination row exists
        cov = (sess.query(SourceCoverage)
               .filter_by(source_id=source_id, target_kind=new_kind, target_id=new_target_id)
               .first())
        if cov is not None:
            return _finish(sess, cov, own)
        raise KeyError(f"no coverage {source_id} -> {target_kind}:{target_id}")
    model = Venue if new_kind == "venue" else Group
    if sess.get(model, new_target_id) is None:
        raise KeyError(f"no {new_kind} '{new_target_id}' to retarget onto")
    existing = (sess.query(SourceCoverage)
                .filter_by(source_id=source_id, target_kind=new_kind, target_id=new_target_id)
                .first())
    if existing is not None and existing is not cov:
        existing.is_primary = existing.is_primary or cov.is_primary
        existing.target_page_url = existing.target_page_url or cov.target_page_url
        existing.routing_key = existing.routing_key or cov.routing_key
        sess.delete(cov)
        cov = existing
    else:
        cov.target_kind = new_kind
        cov.target_id = new_target_id
    if routing_key:
        cov.routing_key = routing_key
    for kind, tid in ((target_kind, target_id), (new_kind, new_target_id)):
        t = sess.get(Venue if kind == "venue" else Group, tid)
        if t is not None:
            decide_monitored(t, sess)
    return _finish(sess, cov, own)


def remove_coverage(source_id, target_kind, target_id, *, sess=None) -> bool:
    """Delete a coverage row that never should have existed (a mis-connect, a
    room the source turns out not to list). Returns True if a row was removed,
    False if it was already gone (idempotent). A venue target is re-judged so
    monitored can't outlive its feed."""
    own = sess is None
    sess = sess or Session()
    cov = (sess.query(SourceCoverage)
           .filter_by(source_id=source_id, target_kind=target_kind, target_id=target_id)
           .first())
    removed = cov is not None
    if removed:
        sess.delete(cov)
        sess.flush()
    t = sess.get(Venue if target_kind == "venue" else Group, target_id)
    if t is not None:
        decide_monitored(t, sess)
    if own:
        sess.commit()
    return removed


def record_dedupe_verdict(dedupe_key_a, dedupe_key_b, verdict, *, reason=None,
                          judged_by="rater", sess=None) -> DedupeVerdict:
    """Persist a judge's same/distinct call on one suspect event pair — the
    write half of the dupe-suspect loop (detector: dedupe.dupe_suspects; the
    verdict is what makes a judgment durable, so the judge only ever sees NEW
    pairs). 'same' makes export collapse the pair's display; 'distinct' keeps
    both rows and retires the pair from the queue forever. Idempotent on the
    sorted key pair; a re-record with a different verdict OVERWRITES (a
    deliberate correction, same semantics as update_venue_fields)."""
    if verdict not in ("same", "distinct"):
        raise ValueError(f"verdict must be 'same' or 'distinct', not {verdict!r}")
    if dedupe_key_a == dedupe_key_b:
        raise ValueError("a pair needs two different events")
    own = sess is None
    sess = sess or Session()
    # tiny forward migration for durable DBs made before verdicts existed
    # (same pattern as VenueAlias)
    DedupeVerdict.__table__.create(bind=sess.connection(), checkfirst=True)
    key_a, key_b = sorted((dedupe_key_a, dedupe_key_b))
    row = sess.get(DedupeVerdict, (key_a, key_b))
    if row is None:
        row = DedupeVerdict(dedupe_key_a=key_a, dedupe_key_b=key_b,
                            verdict=verdict, reason=reason, judged_by=judged_by)
        sess.add(row)
    elif (row.verdict, row.reason) != (verdict, reason):
        row.verdict = verdict
        row.reason = reason
        row.judged_by = judged_by
        row.judged_at = now()
    return _finish(sess, row, own)


def suppress_event(event_id, *, reason, by="agent", sess=None) -> Event:
    """Durably hide one event the upstream feed has WRONG (the JazzAttack case,
    2026-08-30: the venue's own calendar kept publishing a series after the
    organizer moved out). lifecycle='suppressed' — merge_event_values preserves
    it against re-ingest (unlike 'unlisted', which a re-seen row flips back to
    'active'), and export hides it alongside 'unlisted'. The reason is required
    because a suppression contradicts a live feed — say why. Idempotent."""
    own = sess is None
    sess = sess or Session()
    e = sess.get(Event, event_id)
    if e is None:
        raise KeyError(f"no event {event_id}")
    if e.lifecycle != "suppressed":
        e.lifecycle = "suppressed"
        e.verified_by = f"{by}: {reason}"
    return _finish(sess, e, own)


def suppress_series(name, venue_id, *, reason, by="agent", sess=None):
    """The durable form of suppress_event (2026-09-11): a standing rule on
    (normalized name, venue) that ingest applies to every row it lands, from ANY
    source. suppress_event only marked rows that existed at the time; do215
    re-syndicated the Ethical Society's stale JazzAttack Tuesday a week later as
    a fresh row and it walked straight onto the site. This closes that: the rule
    outlives the rows. Also suppresses every matching row already in the DB.
    Returns (rule, rows_suppressed_now). Idempotent."""
    from dedupe import normalize_event_name
    own = sess is None
    sess = sess or Session()
    key = normalize_event_name(name)
    if not key:
        raise ValueError("suppress_series needs a real event name")
    venue_filter = (EventSuppression.venue_id.is_(None) if venue_id is None
                    else EventSuppression.venue_id == venue_id)
    rule = (sess.query(EventSuppression)
            .filter(EventSuppression.name_norm == key, venue_filter).first())
    if rule is None:
        rule = EventSuppression(name_norm=key, venue_id=venue_id, reason=reason, created_by=by)
        sess.add(rule)
        sess.flush()
    n = 0
    q = sess.query(Event).filter(Event.lifecycle != "suppressed")
    q = q.filter(Event.venue_id.is_(None) if venue_id is None else Event.venue_id == venue_id)
    for e in q.all():
        if normalize_event_name(e.name) == key:
            e.lifecycle = "suppressed"
            e.verified_by = f"{by}: {reason}"
            n += 1
    return _finish(sess, rule, own), n


# ─────────────────────────────── verification ──────────────────────────────

def verify_target(target_kind, target_id, *, by="agent", sess=None):
    """Promote a venue/group candidate -> verified. THIS is the writer that makes
    venue.status a live flag (it was DEAD — only ever 'candidate'). A target is
    verified when we have real evidence it exists & hosts events: the canonical
    trigger is a working dedicated source attached to it (see build_source), but a
    human/agent can also verify directly. Idempotent."""
    own = sess is None
    sess = sess or Session()
    model = Venue if target_kind == "venue" else Group
    t = sess.get(model, target_id)
    if t is None:
        raise KeyError(f"no {target_kind} '{target_id}'")
    if t.status != "verified":
        t.status = "verified"
        t.verified_by = by          # was DEAD; now records who verified
    return _finish(sess, t, own)


def build_source(source_id, target_kind, target_id, *, adapter=None, url=None,
                 is_primary=True, by="max", sess=None):
    """The Max happy-path, atomic, GATED: (1) mark_source_built runs the live gate
    and stamps health, (2) connect it to its venue/group, (3) verify that target —
    BUT ONLY IF the gate passed (health='ok'): a working source proves the venue is
    real. If health != 'ok' (built-but-failing), we connect coverage but do NOT
    verify the target (no proof it hosts events yet). (4) re-decide monitored.
    Returns (source, coverage, target). Idempotent end-to-end."""
    own = sess is None
    sess = sess or Session()
    s = mark_source_built(source_id, adapter=adapter, url=url, sess=sess)
    cov = connect_coverage(source_id, target_kind, target_id,
                           is_primary=is_primary, adoption="adopted",
                           discovered_by=by, sess=sess)
    t = sess.get(Venue if target_kind == "venue" else Group, target_id)
    if s.health == "ok":
        t = verify_target(target_kind, target_id, by=by, sess=sess)
    if t is not None:
        decide_monitored(t, sess)
    if own:
        sess.commit()
        for o in (s, cov, t):
            if o is not None:
                sess.refresh(o); sess.expunge(o)
    return s, cov, t


__all__ = [
    "platform", "PLATFORM_MAP",
    "add_venue_lead", "add_group_lead", "add_source_lead", "update_venue_fields",
    "merge_venues", "remove_alias", "record_dedupe_verdict",
    "update_source_fields",
    "probe_source_live", "mark_source_built", "set_source_status",
    "set_source_aggregator",
    "connect_coverage", "retarget_coverage", "remove_coverage",
    "decide_monitored", "verify_target", "build_source",
]
