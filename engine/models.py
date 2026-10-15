"""
City Bulletin engine — the event data model (SQLite + SQLAlchemy 2.0).

Three independent axes, separated cleanly:

  - Discovery   : columns on Venue/Group — how/when we first learned it exists.
  - Extraction  : PipelineSource (1 row per endpoint) -> SourceCoverage bridge (M:N).
  - Monitoring  : a `monitored` flag on Venue/Group; Event visibility is DERIVED.

City parameters come from the repo-root city.json (see the CITY block below).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
from pathlib import Path

from sqlalchemy import (
    Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, Text, create_engine,
    event as sa_event,
)
from sqlalchemy.orm import (
    DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker,
)

# ─────────────────────────────── CITY PARAMS ────────────────────────────────
# The repo-root city.json is the whole parameterization — constants, not a
# config framework. Edit that file, not these lines.
_ROOT = Path(__file__).parent.parent
_city = json.loads((_ROOT / "city.json").read_text())

CITY = _city["city"]
CITY_SLUG = _city["db_file"].rsplit(".", 1)[0]   # "chicago"
CITY_STATE = _city["city_state"]       # geocoder anchor
TIMEZONE = _city["timezone"]           # all stored datetimes are venue wall-clock
BBOX = _city["bbox"]                   # the focus band (sanity signal, never a filter)
SITE = _city["site"]                   # canonical origin, no trailing slash
SITE_NAME = _city["site_name"]
HOODS_PATH = _ROOT / _city["hoods_geojson"]

# The DB lives OUTSIDE the repo so every checkout/worktree (and every agent
# session) shares ONE copy — per-checkout copies fork silently. BULLETIN_DB
# overrides it for scratch runs. export_history.jsonl lives next to the DB.
DATA_DIR = Path(_city["data_dir"]).expanduser()
DB_PATH = Path(os.environ.get("BULLETIN_DB") or DATA_DIR / _city["db_file"])


class Base(DeclarativeBase):
    pass


def now() -> dt.datetime:
    return dt.datetime.now()


def slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").strip().lower()).strip("-")
    return s or "unknown"


def in_bbox(lat: float | None, lng: float | None) -> bool:
    """Is this point inside the city focus band? (Used for sanity checks — never
    a filter on stored rows; we keep what we scrape and judge later.)"""
    if lat is None or lng is None:
        return False
    return (BBOX["lat_min"] <= lat <= BBOX["lat_max"]
            and BBOX["lng_min"] <= lng <= BBOX["lng_max"])


# Built-in fallback weights over the 5 universal dimensions (sum to 1.0).
# Live source of truth is data/weights.json (editable, named sets).
QUALITY_WEIGHTS = {
    "community_building": 0.263,
    "social_quality": 0.237,
    "serendipity": 0.211,
    "solo_friendliness": 0.184,
    "uniqueness": 0.105,
}
WEIGHTS_PATH = Path(__file__).parent / "data" / "weights.json"


def load_weights() -> dict[str, float]:
    """Active weight set from data/weights.json; falls back to QUALITY_WEIGHTS."""
    try:
        cfg = json.loads(WEIGHTS_PATH.read_text())
        w = cfg["sets"][cfg.get("active", "default")]
        return {k: float(v) for k, v in w.items()}
    except Exception:
        return dict(QUALITY_WEIGHTS)


def quality_score(scores: dict | None, weights: dict | None = None):
    """Weighted composite of the universal dims. Renormalizes over whichever
    dims are present, so older partial scores still yield a value."""
    if not scores:
        return None
    weights = weights or QUALITY_WEIGHTS
    total = wsum = 0.0
    for dim, w in weights.items():
        d = scores.get(dim)
        if isinstance(d, dict) and "score" in d:
            total += d["score"] * w
            wsum += w
    return round(total / wsum, 1) if wsum else None


# ─────────────────────────────── EXTRACTION ────────────────────────────────

class PipelineSource(Base):
    """A scrapeable endpoint + the script that pulls events from it. The build
    axis. ONE row per endpoint — an aggregator (Eventbrite) is a single row that
    fans out to many entities via SourceCoverage, never duplicated per venue."""
    __tablename__ = "pipeline_sources"

    id: Mapped[str] = mapped_column(String, primary_key=True)        # slug
    name: Mapped[str] = mapped_column(String)
    # transport ONLY: eventbrite | ticketmaster_api | tribe_wp | humanitix
    # | rss | html | custom_json | manual
    adapter: Mapped[str] = mapped_column(String)
    url: Mapped[str | None] = mapped_column(String, nullable=True)       # calendar/feed endpoint
    api_url: Mapped[str | None] = mapped_column(String, nullable=True)
    has_api: Mapped[bool] = mapped_column(Boolean, default=False)
    # one endpoint, many entities (resolve venue/org per event). false = single-owner calendar.
    is_aggregator: Mapped[bool] = mapped_column(Boolean, default=False)
    script_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    # LIVE: none | planned | built. THE build ladder = "is this in ingest's
    # worklist?" (ingest runs every status=='built' source). Flipped to 'built' ONLY
    # by mark_source_built/build_source AFTER the live build-gate confirms the adapter
    # returns valid events. add_source_lead caps a guess at 'planned' — never 'built'.
    status: Mapped[str] = mapped_column(String, default="none")
    # LIVE: health of a BUILT source, stamped at build time by the gate (NOT
    # re-derived each ingest). ok = produced >=1 valid event; no_events = adapter ran
    # clean but returned nothing (dormant / past-only); http_error = 4xx/5xx (404,
    # 403/Cloudflare); parse_error = got a response, couldn't extract events.
    # A source can be status='built' AND health!='ok' (built-but-failing) — we keep
    # the pipeline, health records why it's dry. health_detail holds the actual
    # message thrown ('HTTP 403', 'all 30 events past', 'JSONDecodeError: ...').
    # LIVE: the Scout's verdict — NULL = never scouted | go | no_go. Written by
    # set_source_status(verdict=) / add_source_lead(verdict=). status can't carry
    # this: Tej's adapter guess already makes a lead 'planned', and a NO-GO and an
    # untouched lead are both 'none'. funnel.py reads it to find the Scout queue.
    scout_verdict: Mapped[str | None] = mapped_column(String, nullable=True)
    health: Mapped[str | None] = mapped_column(String, nullable=True)
    health_detail: Mapped[str | None] = mapped_column(String, nullable=True)
    # last successful pull from the live endpoint: stamped by the build gate AND by
    # every ingest run. `ingest.py --stale N` uses it to skip fresh sources.
    last_scraped: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    # LIVE (write-once): provenance parity with Venue/Group — sources were the only
    # entity without it (2026-06-09 audit; Tej's charter says to set it).
    discovered_via: Mapped[str | None] = mapped_column(String, nullable=True)
    discovered_by: Mapped[str | None] = mapped_column(String, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    events: Mapped[list["Event"]] = relationship(back_populates="source")


class SourceCoverage(Base):
    """Which PipelineSource lists events for which target (venue OR group). The
    M:N bridge. `is_primary` is scoped per-TARGET (a venue's own calendar is its
    primary; an aggregator is nobody's primary) — that's the dedup signal."""
    __tablename__ = "source_coverage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("pipeline_sources.id"), index=True)
    target_kind: Mapped[str] = mapped_column(String, default="venue")  # venue | group
    target_id: Mapped[str] = mapped_column(String, index=True)         # venue/group slug
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)   # LIVE: canonical feed for that target (dedup signal)
    # LIVE: adopted | discovered. discovered = new from an aggregator, unvetted — THE
    # vetting queue. Written by discover.py (firehose=discovered, curated=adopted).
    adoption: Mapped[str] = mapped_column(String, default="adopted")
    # The old single `url` column carried THREE meanings (target page, aggregator
    # routing slug, and — wrongly — scrape endpoint) and caused a silent zero-yield
    # bug (Empty Bottle, 2026-06-09 audit). Split: the scrape endpoint lives ONLY
    # on PipelineSource.url (what the build gate probes is what ingest scrapes).
    target_page_url: Mapped[str | None] = mapped_column(String, nullable=True)  # target's page on that source (/o/<id>)
    routing_key: Mapped[str | None] = mapped_column(String, nullable=True)      # aggregator->target join key (e.g. park slug)
    # FROZEN: high | medium | low | inactive | unknown. An LLM guess at seed, never
    # recomputed from actual scrape yield. TODO: derive from events per source.
    usefulness: Mapped[str] = mapped_column(String, default="unknown")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    discovered_by: Mapped[str] = mapped_column(String, default="llm")  # llm | seed | firehose
    first_seen: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


# ──────────────────────────── BROWSE / ENTITIES ────────────────────────────

class Venue(Base):
    """A durable place — the 'where'. Browse spine."""
    __tablename__ = "venues"

    id: Mapped[str] = mapped_column(String, primary_key=True)        # slug
    name: Mapped[str] = mapped_column(String)
    # theater|bar|church|gallery|cafe|warehouse|makerspace|museum|library|park|market|garden|other
    kind: Mapped[str] = mapped_column(String, default="other")
    address: Mapped[str | None] = mapped_column(String, nullable=True)
    neighborhood: Mapped[str | None] = mapped_column(String, nullable=True)
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    website: Mapped[str | None] = mapped_column(String, nullable=True)
    # FLAG AUDIT 2026-05-31 (see docs/leads-pipeline.md). LIVE = has an operator that
    # writes it post-seed; FROZEN = set once at seed from an LLM guess, never derived;
    # DEAD = no writer anywhere, only the default — do NOT trust these.
    #
    # FROZEN: unknown | no | occasional | regular. An LLM guess at seed; NOT derived
    # from actual events yet. TODO: an operator should compute this from event yield.
    hosts_events: Mapped[str] = mapped_column(String, default="unknown")
    # LIVE since 2026-06-08 (was DEAD): candidate | verified. verify_target is the
    # only writer — fired by build_source when the gate passes health='ok', i.e.
    # "verified" = a working dedicated source PROVED this venue is real & hosting.
    status: Mapped[str] = mapped_column(String, default="candidate")
    # LIVE: owned + flipped by decide_monitored(). "DO WE WANT IT?" — orthogonal to
    # status; keys off whether a dedicated source feeds the venue.
    monitored: Mapped[bool] = mapped_column(Boolean, default=False)
    # DEAD: zero write sites anywhere. Schema cruft; no fake-detector exists.
    maybe_fake: Mapped[bool] = mapped_column(Boolean, default=False)
    # LIVE (write-once): discovery provenance, set on first touch by the operator
    # that mints the row. llm|eventbrite_firehose|scrape|manual|osm|foia|flyer
    # (`flyer` comes over from the Philly v1 import — the flyer pipeline is identity.)
    discovered_via: Mapped[str | None] = mapped_column(String, nullable=True)
    discovered_by: Mapped[str | None] = mapped_column(String, nullable=True)   # agent/run id
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_seen: Mapped[dt.datetime] = mapped_column(DateTime, default=now)   # doubles as discovered_at
    last_seen: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    # LIVE since 2026-06-08 (was DEAD): verify_target records WHO verified (the
    # `by=` arg, e.g. "max"). Rows still reading the default "scraper" predate it.
    verified_by: Mapped[str] = mapped_column(String, default="scraper")

    events: Mapped[list["Event"]] = relationship(back_populates="venue")


class VenueAlias(Base):
    """A retired or alternate venue slug resolved to its canonical Venue.

    Venue merges are explicit, but aliases make them durable: a later firehose
    rediscovery of the old spelling resolves to the survivor instead of recreating
    the duplicate row.
    """
    __tablename__ = "venue_aliases"

    alias_id: Mapped[str] = mapped_column(String, primary_key=True)
    venue_id: Mapped[str] = mapped_column(ForeignKey("venues.id"), index=True)
    name: Mapped[str | None] = mapped_column(String, nullable=True)
    reason: Mapped[str | None] = mapped_column(String, nullable=True)
    first_seen: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class Group(Base):
    """A durable producer/organizer — the 'who', venue-independent. NOT an
    aggregator: meta-aggregators (Campus Philly, Penn Today) are PipelineSource
    rows with is_aggregator=True, never Group rows."""
    __tablename__ = "groups"

    id: Mapped[str] = mapped_column(String, primary_key=True)        # slug
    name: Mapped[str] = mapped_column(String)
    # presenter|company|society|promoter|civic|school|religious|media|other
    kind: Mapped[str] = mapped_column(String, default="presenter")
    website: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="candidate")
    monitored: Mapped[bool] = mapped_column(Boolean, default=False)
    discovered_via: Mapped[str | None] = mapped_column(String, nullable=True)
    discovered_by: Mapped[str | None] = mapped_column(String, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_seen: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    last_seen: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    verified_by: Mapped[str] = mapped_column(String, default="scraper")
    # The membership-directory fields (2026-10-13): one public line on what the
    # group is / who it's for, and how to join (Meetup, Discord, signup form).
    # A group needs no feed to be listed — name + website + description is enough.
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    join_url: Mapped[str | None] = mapped_column(String, nullable=True)

    events: Mapped[list["Event"]] = relationship(back_populates="group")


class Series(Base):
    """A recurring-event cluster at a venue — DERIVED from events by
    series.py (pure date math: median gap between distinct dates), re-derived
    after every ingest. Distinguishes a weekly club (join it) from a theater
    run (see it once). The Rater scores a series ONCE and instances inherit —
    the biggest judgment-cost lever (a 14-week knitting circle is 1 judgment,
    not 14)."""
    __tablename__ = "series"

    id: Mapped[str] = mapped_column(String, primary_key=True)   # f"{venue_id}|{norm_name}"
    name: Mapped[str] = mapped_column(String)                   # most common raw instance name
    venue_id: Mapped[str | None] = mapped_column(ForeignKey("venues.id"), nullable=True)
    cadence: Mapped[str] = mapped_column(String)                # run|weekly|biweekly|monthly|irregular
    median_gap_days: Mapped[int] = mapped_column(Integer)
    n_dates: Mapped[int] = mapped_column(Integer)
    first_date: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    last_date: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    first_seen: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    last_seen: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class Event(Base):
    """One source's event observation. Public identity is resolved at export.

    Visibility is DERIVED, never stored (see is_visible).
    """
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dedupe_key: Mapped[str] = mapped_column(String, unique=True, index=True)
    name: Mapped[str] = mapped_column(String)
    start: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    end: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    all_day: Mapped[bool] = mapped_column(Boolean, default=False)

    venue_id: Mapped[str | None] = mapped_column(ForeignKey("venues.id"), nullable=True)            # where
    group_id: Mapped[str | None] = mapped_column(ForeignKey("groups.id"), nullable=True)            # who
    source_id: Mapped[str | None] = mapped_column(ForeignKey("pipeline_sources.id"), nullable=True)  # listed-where
    # raw organizer text as the platform published it, kept verbatim (like `cost`).
    # NOT a group link — turning these strings into Group rows is Tej's lead work.
    organizer: Mapped[str | None] = mapped_column(String, nullable=True)

    category: Mapped[str | None] = mapped_column(String, nullable=True)
    tags: Mapped[str | None] = mapped_column(String, nullable=True)
    cost: Mapped[str | None] = mapped_column(String, nullable=True)   # raw source text, kept verbatim
    # structured cost, parsed from `cost` at ingest (costs.parse_cost — deterministic,
    # no LLM, no venue priors; priors are persona-layer inference, not stored fact).
    cost_min_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_max_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_free: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # derived by series.derive_series after each ingest; NULL = not part of a cluster
    series_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    image: Mapped[str | None] = mapped_column(String, nullable=True)
    url: Mapped[str | None] = mapped_column(String, nullable=True)
    discovered_via: Mapped[str] = mapped_column(String, default="api")  # api | website | flyer | manual
    # active = upcoming/current | past = already happened at ingest time |
    # unlisted = vanished from its own feed on a re-pull (renamed/cancelled/
    # deleted upstream — the Rabbit ghost, 2026-08-19). ingest's window-aware
    # retirement is the only writer of 'unlisted'; a re-seen row flips back to
    # active. Export hides 'unlisted'; we never delete.
    lifecycle: Mapped[str] = mapped_column(String, default="active")

    first_seen: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    last_seen: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    verified_by: Mapped[str] = mapped_column(String, default="scraper")

    venue: Mapped["Venue | None"] = relationship(back_populates="events")
    group: Mapped["Group | None"] = relationship(back_populates="events")
    source: Mapped["PipelineSource | None"] = relationship(back_populates="events")


class DedupeVerdict(Base):
    """A judged same/distinct verdict on one suspect event pair (2026-08-19,
    the Conejo double-listing). The deterministic detector (dedupe.dupe_suspects)
    surfaces same-venue/same-minute pairs the display matcher does NOT collapse;
    an agentic judge decides, and the verdict persists here so 'same' collapses
    display forever and 'distinct' never re-enters the queue (Bartram's runs
    three genuinely distinct events at Sat 10:00). Keyed on the pair's
    dedupe_keys, sorted — stable identity across rebuilds, like EventScore.
    Written ONLY by operators.record_dedupe_verdict."""
    __tablename__ = "dedupe_verdicts"

    dedupe_key_a: Mapped[str] = mapped_column(String, primary_key=True)  # lexicographically first
    dedupe_key_b: Mapped[str] = mapped_column(String, primary_key=True)
    verdict: Mapped[str] = mapped_column(String)          # same | distinct
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    judged_by: Mapped[str] = mapped_column(String, default="rater")
    judged_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class EventSuppression(Base):
    """A standing rule: any event whose normalized name + venue match is
    suppressed on sight (2026-09-11, the JazzAttack leak). suppress_event is
    per ROW, so a syndicator that mints a fresh row every week (do215 copying
    the Ethical Society's stale calendar) walked straight past it. This is the
    series-level version: ingest consults it on every insert and re-seen row,
    source-agnostic, so the correction holds without anyone re-suppressing.
    Written ONLY by operators.suppress_series."""
    __tablename__ = "event_suppressions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name_norm: Mapped[str] = mapped_column(String, index=True)   # dedupe.normalize_event_name
    venue_id: Mapped[str | None] = mapped_column(ForeignKey("venues.id"), nullable=True)
    reason: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String, default="agent")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class EventScore(Base):
    """Output of the Rater job: one row per (event, scoring run). History kept;
    export uses the newest row per dedupe_key. Written ONLY by
    `scorer.py --import` (Rater subagents judge in-session; there is no API call).
    These rows are the expensive asset — preserved verbatim across migrations."""
    __tablename__ = "event_scores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id"), index=True, nullable=True)
    dedupe_key: Mapped[str] = mapped_column(String, index=True)   # stable identity across rebuilds
    model: Mapped[str] = mapped_column(String)

    # 5 universal dimensions (1-10) — describe the event, same for every viewer
    social_quality: Mapped[int] = mapped_column(Integer)
    serendipity: Mapped[int] = mapped_column(Integer)
    uniqueness: Mapped[int] = mapped_column(Integer)
    community_building: Mapped[int] = mapped_column(Integer)
    solo_friendliness: Mapped[int] = mapped_column(Integer)
    # folded into the page composite (weight in data/weights.json)
    value_for_price: Mapped[int | None] = mapped_column(Integer, nullable=True)

    quality: Mapped[float | None] = mapped_column(Float, nullable=True)            # weighted composite
    category: Mapped[str | None] = mapped_column(String, nullable=True)            # canonical category (Rater)
    description_quality: Mapped[str | None] = mapped_column(String, nullable=True)  # rich|thin|missing
    reasons: Mapped[dict | None] = mapped_column(JSON, nullable=True)              # per-dim one-liner
    scored_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now, index=True)


# ──────────────────────────────── DERIVED ──────────────────────────────────

def is_visible(event: "Event", venue: "Venue | None", group: "Group | None",
               source: "PipelineSource | None") -> bool:
    """The monitoring rule, computed — never a column.
    visible ⇔ source built AND (no venue OR venue.monitored)
                              AND (no group OR group.monitored)."""
    if not source or source.status != "built":
        return False
    if venue is not None and not venue.monitored:
        return False
    if group is not None and not group.monitored:
        return False
    return True


# One DB file, many concurrent writers (agent sessions in parallel Conductor
# workspaces + the 00:30 export cron). Default SQLite gives them a 5s lock wait
# and a whole-file write lock: "database is locked" mid-ingest. WAL lets readers
# run during a write, and the timeouts make a contending writer wait instead of
# failing. Set per-connection because PRAGMAs are connection-scoped (journal_mode
# is the exception — it persists in the file — but re-issuing it is harmless).
engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"timeout": 30})


@sa_event.listens_for(engine, "connect")
def _sqlite_concurrency_pragmas(dbapi_connection, connection_record):
    cur = dbapi_connection.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=30000")
    cur.close()


Session = sessionmaker(bind=engine)


# The funnel as a single read-only view (Option B's "leads inbox"): every un-built
# row across venues/groups/sources, with a uniform shape. "Show me all pending work"
# = SELECT * FROM leads_inbox. A venue/group is pending until a built source feeds it;
# a source is pending until status='built'.
LEADS_INBOX_SQL = """
CREATE VIEW IF NOT EXISTS leads_inbox AS
  SELECT 'source' AS kind, s.id AS id, s.name AS name, s.status AS stage,
         s.adapter AS detail,
         CASE WHEN s.is_aggregator THEN 'aggregator' ELSE 'dedicated' END AS note,
         NULL AS discovered_via
    FROM pipeline_sources s
   WHERE s.status != 'built'
  UNION ALL
  SELECT 'venue', v.id, v.name, v.status, v.kind, v.neighborhood, v.discovered_via
    FROM venues v
   WHERE v.monitored = 0
  UNION ALL
  SELECT 'group', g.id, g.name, g.status, g.kind, NULL, g.discovered_via
    FROM groups g
   WHERE g.monitored = 0
"""


def init_db(drop: bool = False) -> None:
    if drop:
        Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    from sqlalchemy import text
    # create_all never alters an existing table, so a nullable column added to a
    # model is ALTERed in here — idempotent, and every city DB gets it on its
    # next init_db() instead of whoever remembers (the 09-20 lesson, for columns).
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            have = {r[1] for r in conn.execute(text(f"PRAGMA table_info({table.name})"))}
            for col in table.columns:
                if col.name not in have and col.nullable:
                    conn.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {col.name} "
                                      f"{col.type.compile(engine.dialect)}"))
    # (re)create the leads_inbox view
    with engine.begin() as conn:
        conn.execute(text("DROP VIEW IF EXISTS leads_inbox"))
        conn.execute(text(LEADS_INBOX_SQL))
