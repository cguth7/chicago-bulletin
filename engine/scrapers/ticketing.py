"""Ticketing platforms: crowdwork, agile_feed, eventive, ticketleap, ticketmaster_api, beventi_vendor, xtrulink."""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import core
from .core import LOCAL_TZ, _in_range, _slug, _with_route, host_root, norm, route_tag, strip_html

# ---------------- crowdwork  (Crowdwork / Fourth Wall Tickets platform) ---------

def scrape_crowdwork(shows_url, source_name, start, end, max_events=200):
    """Crowdwork (formerly Fourth Wall Tickets) — the comedy-theater ticketing
    platform. Generic per-venue adapter: any Crowdwork house is
    `crowdwork.com/api/v2/<theatre-slug>/shows` (SideQuest today; Helium/
    ComedySportz would reuse this). Plain JSON, no auth.

    `data[]` is one object per SHOW, each with a `dates[]` array of every
    upcoming showtime (ISO with explicit offsets) — fan each show out over its
    dates or a 18-night run collapses to one event. Cost: `cost_tiers[].cost` is
    cents PRE-fees (the honest ticket price); `cost.formatted` includes fees.
    SAFETY CAP: one fetch; max_events caps the expanded rows."""
    try:
        data = core.fetch_json(shows_url)
    except (HTTPError, URLError, json.JSONDecodeError) as e:
        print(f"  [{source_name}] crowdwork: {e}", file=sys.stderr)
        return []
    out = []
    for show in (data.get("data") or []):
        if (show.get("status") or "active") != "active":
            continue
        name = (show.get("name") or "").strip()
        if not name:
            continue
        tiers = [t.get("cost") for t in (show.get("cost_tiers") or [])
                 if isinstance(t.get("cost"), int)]
        if tiers:
            cents = min(tiers)
            cost = "Free" if cents == 0 else f"${cents / 100:.2f}"
        else:
            cost = (show.get("cost") or {}).get("formatted") or ""
        desc = ((show.get("description") or {}).get("body")
                or show.get("description_short") or "")
        tags = ", ".join((show.get("tags") or {}).get("public") or [])
        for iso in (show.get("dates") or []):
            if not _in_range(iso[:10], start, end):
                continue
            out.append(norm(
                name=name, date=iso[:10],
                time_=iso[11:16] if len(iso) >= 16 else "",
                venue=(show.get("theatre") or {}).get("name") or "",
                description=strip_html(desc),
                image=(show.get("img") or {}).get("url") or "",
                category=((show.get("tags") or {}).get("public") or [""])[0],
                tags=tags, cost=cost,
                url=show.get("url") or "", source=source_name,
            ))
            if len(out) >= max_events:
                return out
    return out



# ---------------- agile_feed  (Agile WebSales / agiletix.com event feed) --------

AGILE_BUILDINGS = {
    # slug(Venue.Address1) -> building-level venue name. Agile "venues" are
    # individual SCREENS (PFS runs 9 across 3 buildings); collapse to the
    # building BEFORE routing/minting or an aggregator fragments into
    # per-screen venue rows. Extend this map when porting to another Agile org;
    # unmapped addresses fall back to the feed's own Venue.Name.
    # e.g. "1412-chestnut-street": "Philadelphia Film Center",  (Philly's)
}


def scrape_agile_feed(feed_url, source_name, start, end, max_events=200):
    """Agile WebSales (agiletix.com) "WebSales Event Feed" — the ticketing
    backend behind many arthouse cinemas; first user is the Philadelphia Film
    Society, whose WP site (filmadelphia.org) is 403-walled by Imperva. ONLY
    feed.ashx is exempt from the WAF: one GET, no auth, no pagination —
    `feed.ashx?guid=<list-guid>&showslist=true&format=json`.

    JSON path: `ArrayOfShows[]` (one per film: Name, ShortDescription,
    EventImage, CustomProperties incl. Genre) -> `CurrentShowings[]` (one per
    SHOWTIME — the event instance; StartDate/EndDate are naive local ET, and
    each carries a per-screen Venue object + LegacyPurchaseLink).

    AGGREGATOR adapter: collapse screens -> buildings via AGILE_BUILDINGS
    (keyed on slugged Venue.Address1) and emit the routing tag from the
    building name, so ingest routes / --discover mints at building level.

    GUID-rotation failure mode is a valid-but-EMPTY shell (SourceLink +
    LastUpdated only, no ArrayOfShows) — RAISE on it so the build gate stamps
    parse_error, never a silent no_events (Scout doc, 2026-08-16).
    SAFETY CAP: one fetch; max_events caps the expanded showings."""
    try:
        data = core.fetch_json(feed_url)
    except (HTTPError, URLError) as e:
        print(f"  [{source_name}] agile_feed: {e}", file=sys.stderr)
        return []
    # NOTE: JSONDecodeError deliberately NOT caught — a bad GUID can also return
    # non-JSON, and that too must surface as parse_error at the gate, not [].
    if "ArrayOfShows" not in data:
        # empty shell = wrong/rotated GUID, not a dormant venue: fail LOUDLY
        raise ValueError(
            f"agile_feed: no ArrayOfShows in feed (rotated/empty GUID?) — "
            f"keys: {sorted(data)[:6]}")
    out = []
    for show in (data.get("ArrayOfShows") or []):
        name = re.sub(r"\s+", " ", (show.get("Name") or "")).strip()
        if not name:
            continue
        genres = [p.get("Value") for p in (show.get("CustomProperties") or [])
                  if p.get("Name") == "Genre" and p.get("Value")]
        desc = strip_html(show.get("ShortDescription") or "")
        img = show.get("EventImage") or show.get("ThumbImage") or ""
        for sh in (show.get("CurrentShowings") or []):
            if sh.get("DateTBD"):
                continue
            iso = sh.get("StartDate") or ""
            if not _in_range(iso[:10], start, end):
                continue
            v = sh.get("Venue") or {}
            addr1 = (v.get("Address1") or "").strip()
            building = AGILE_BUILDINGS.get(_slug(addr1)) or (v.get("Name") or "").strip()
            addr = ", ".join(p for p in (addr1, v.get("City"), v.get("Zip")) if p)
            endiso = sh.get("EndDate") or ""
            out.append(norm(
                name=name, date=iso[:10],
                time_=iso[11:16] if len(iso) >= 16 else "",
                end_time=endiso[11:16] if endiso[:10] == iso[:10] else "",
                venue=building, address=addr,
                description=desc, image=img,
                category="Film",
                tags=_with_route(", ".join(genres), building),
                url=sh.get("LegacyPurchaseLink") or show.get("InfoLink") or "",
                source=source_name,
            ))
            if len(out) >= max_events:
                return out
    return out



# ---------------- eventive  (Eventive event-bucket JSON; cinéSPEAK) -----------

def _eventive_local(iso, tzid):
    """Eventive UTC instant ('2026-08-20T23:00:00.000Z') → naive venue wall-clock
    datetime in the event's own IANA `timezone` (falls back to the engine's
    LOCAL_TZ), because the engine stores wall clock everywhere."""
    if not iso:
        return None
    try:
        d = dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    try:
        tz = ZoneInfo(tzid) if tzid else LOCAL_TZ
    except ZoneInfoNotFoundError:
        tz = LOCAL_TZ
    return d.astimezone(tz).replace(tzinfo=None) if d.tzinfo else d


def scrape_eventive(bucket_url, source_name, start, end, max_events=200,
                    schedule_root="https://cinespeak.eventive.org/schedule"):
    """Eventive (api.eventive.org) — the film-festival/microcinema ticketing
    platform. `bucket_url` is the FULL bucket-events endpoint incl. the tenant's
    public api_key (shipped in every browser bundle, not a secret):
      https://api.eventive.org/event_buckets/<bucket>/events?api_key=<key>
    One GET, no pagination: `{"events": [...]}` — the org's whole history, past
    shows included, so the window filter here is doing real work.

    Per event: `name`; `start_time`/`end_time` are UTC ISO instants, converted
    via _eventive_local to the event's own `timezone`; `venue.name`/`.address`
    (address is newline-separated in the feed); `id` → the public event page
    <schedule_root>/<id>. Virtual and non-'visible' events are dropped.

    AGGREGATOR adapter: screenings hop across many Philly rooms, so each row
    carries route_tag(venue.name) for ingest's coverage fan-out. First user is
    cinéSPEAK (Philly source "cinespeak"); a second Eventive org would get its
    own source row and pass its own schedule_root. SAFETY CAP: one fetch;
    max_events is a HARD ceiling on rows returned."""
    try:
        data = core.fetch_json(bucket_url)
    except (HTTPError, URLError, json.JSONDecodeError) as e:
        print(f"  [{source_name}] eventive: {e}", file=sys.stderr)
        return []
    out = []
    for ev in (data.get("events") or []):
        name = re.sub(r"\s+", " ", (ev.get("name") or "")).strip()
        if not name:
            continue
        if ev.get("is_virtual") or ev.get("visibility") not in (None, "visible"):
            continue
        tzid = ev.get("timezone") or ""
        sdt = _eventive_local(ev.get("start_time"), tzid)
        if not sdt or not (start <= sdt.date() <= end):
            continue
        edt = _eventive_local(ev.get("end_time"), tzid)
        v = ev.get("venue") or {}
        # names carry stray whitespace ("Kingsessing Library ") — strip, or the
        # routing slug won't match the coverage row minted from the clean name
        vname = strip_html(v.get("name") or "")
        addr = re.sub(r"\s*\n\s*", ", ", (v.get("address") or "").strip())
        eid = ev.get("id") or ""
        out.append(norm(
            name=name,
            date=sdt.strftime("%Y-%m-%d"), time_=sdt.strftime("%H:%M"),
            end_date=edt.strftime("%Y-%m-%d") if edt else "",
            end_time=edt.strftime("%H:%M") if edt else "",
            venue=vname, address=addr,
            description=strip_html(ev.get("short_description")
                                   or ev.get("description") or ""),
            category="Film",
            tags=route_tag(vname),
            url=f"{schedule_root}/{eid}" if eid else "",
            source=source_name,
        ))
        if len(out) >= max_events:
            break
    return out



# ---------------- ticketleap  (TicketLeap organizer page embedded JSON) --------

def _tl_listings(body):
    """Extract the JSON array of {"type":"listing",...} objects a TicketLeap
    organizer page server-renders into its HTML. It's a JS data blob (NOT
    JSON-LD), so we bracket-match: find the first listing marker, walk back to
    the array's opening '[', then scan forward tracking depth with full JSON
    string/escape awareness. Returns [] when the page has no listings block
    (dormant organizer); raises JSONDecodeError on a malformed blob so the
    build gate stamps parse_error instead of a silent no_events."""
    i = body.find('"type":"listing"')
    if i == -1:
        return []
    j = body.rfind("[", 0, i)
    if j == -1:
        return []
    depth, in_str, esc = 0, False, False
    for k in range(j, len(body)):
        c = body[k]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return json.loads(body[j:k + 1])
    return []


def _tl_dt(ts, wall):
    """server_event_start_ts is a TRUE UTC EPOCH: localized to America/New_York
    it equals the naive `event_start` wall string (verified 2026-08-17 against
    both live Mouth Of Rain listings). Prefer the epoch → naive ET wall time;
    fall back to the naive string if the epoch is missing/garbled."""
    if ts:
        try:
            return (dt.datetime.fromtimestamp(int(ts), tz=dt.timezone.utc)
                    .astimezone(LOCAL_TZ).replace(tzinfo=None))
        except (ValueError, OSError, OverflowError):
            pass
    try:
        return dt.datetime.fromisoformat(wall) if wall else None
    except ValueError:
        return None


def scrape_ticketleap(page_url, source_name, start, end, max_events=200):
    """TicketLeap whitelabel organizer listing page
    (events.ticketleap.com/tickets/<org>). No anonymous REST — the /api/ routes
    401/404 — but the page embeds its full listings as JSON (see _tl_listings).
    Generic per-organizer adapter: Mouth Of Rain first
    (Philly source "mouthofrain-ticketleap"); any TicketLeap organizer reuses it.

    Per-event venue_name/venue_city vary (group-shaped source, venues roam), so
    we filter venue_city == the working city — organizers can list events anywhere —
    and emit the routing tag from venue_name for any future aggregator use
    (harmless on dedicated/group sources). min_price is the pre-fee price
    (min_price_fmt includes fees — don't use it). Event URL =
    https://events.ticketleap.com/tickets/<listing_slug>.
    SAFETY CAP: one fetch, no pagination; max_events caps rows."""
    try:
        body = core.fetch(page_url).decode("utf-8", "replace")
    except (HTTPError, URLError) as e:
        print(f"  [{source_name}] ticketleap: {e}", file=sys.stderr)
        return []
    out = []
    for it in _tl_listings(body):
        if not isinstance(it, dict) or it.get("type") != "listing":
            continue
        name = strip_html(it.get("listing_title") or "").strip()
        if not name:
            continue
        city = (it.get("venue_city") or "").strip()
        if city and city.lower() != core.HOME_CITY.lower():
            continue
        sdt = _tl_dt(it.get("server_event_start_ts"), it.get("event_start"))
        edt = _tl_dt(it.get("server_event_end_ts"), it.get("event_end"))
        if not sdt or not (start <= sdt.date() <= end):
            continue
        cost = ""
        try:
            cents = round(float(it.get("min_price")) * 100)
            cost = "Free" if cents == 0 else f"${cents / 100:.2f}"
        except (TypeError, ValueError):
            pass
        if it.get("has_only_free_tickets"):
            cost = "Free"
        img = it.get("image") or ""
        if img.startswith("//"):
            img = "https:" + img
        lslug = (it.get("listing_slug") or "").strip("/")
        vname = (it.get("venue_name") or "").strip()
        out.append(norm(
            name=name,
            date=sdt.strftime("%Y-%m-%d"), time_=sdt.strftime("%H:%M"),
            end_date=edt.strftime("%Y-%m-%d") if edt else "",
            end_time=edt.strftime("%H:%M") if edt else "",
            venue=vname, image=img, cost=cost,
            tags=route_tag(vname),
            url=(f"https://events.ticketleap.com/tickets/{lslug}"
                 if lslug else page_url),
            source=source_name,
        ))
        if len(out) >= max_events:
            break
    return out



# ---------------- ticketmaster_api  (TM Discovery v2; venueId-scoped) ----------------

def scrape_ticketmaster(api_url, source_name, start, end, max_pages=5, max_events=200):
    """Ticketmaster Discovery v2 (ported from Philly scrape_week.py, re-shaped to
    the (url, source_name, start, end) -> norm() interface).

    api_url is the FULL keyed endpoint from the source row — apikey + the scoping
    params (venueId=... repeated, source=ticketmaster,ticketweb) live in the URL,
    not in env. That keeps the adapter venue-agnostic: point it at any Discovery
    query (venueId, attractionId, dmaId...) and it just walks the result.

    We append the [start,end] window server-side (startDateTime/endDateTime, UTC
    midnights — close enough; the exact keep/drop uses localDate via _in_range)
    plus sort=date,asc and page=N, walking page.totalPages. Filters out
    dates.status.code == 'cancelled' (the venue widgets do the same). Uses
    localDate/localTime (venue wall-clock), never the UTC dateTime.

    SAFETY CAP: max_events is a HARD ceiling on rows returned in one call and
    max_pages bounds the walk (TM deep-paging caps at size*page=1000 anyway) —
    this can never run unbounded.
    """
    sep = "&" if "?" in api_url else "?"
    window = (f"startDateTime={start.isoformat()}T00:00:00Z"
              f"&endDateTime={end.isoformat()}T23:59:59Z&sort=date,asc")
    out, page = [], 0
    while page < max_pages and len(out) < max_events:
        url = f"{api_url}{sep}{window}&page={page}"
        try:
            data = core.fetch_json(url)
        except (HTTPError, URLError, json.JSONDecodeError) as e:
            print(f"  [{source_name}] ticketmaster page {page}: {e}", file=sys.stderr)
            break
        events = (data.get("_embedded") or {}).get("events", []) or []
        if not events:
            break
        for ev in events:
            dates = (ev.get("dates") or {})
            if ((dates.get("status") or {}).get("code") or "").lower() == "cancelled":
                continue
            sd = (dates.get("start") or {})
            local_date = sd.get("localDate", "") or ""
            if not _in_range(local_date, start, end):
                continue
            venues = ((ev.get("_embedded") or {}).get("venues") or [{}])
            v = venues[0] if venues else {}
            loc = v.get("location") or {}
            cls = (ev.get("classifications") or [{}])[0]
            images = ev.get("images") or []
            best_img = next(
                (im.get("url", "") for im in images
                 if im.get("ratio") == "16_9" and im.get("width", 0) >= 640),
                images[0].get("url", "") if images else "")
            price = (ev.get("priceRanges") or [{}])[0]
            cost = ""
            if price.get("min") is not None:
                mn, mx = price.get("min"), price.get("max")
                cost = f"${mn:g}–${mx:g}" if mx and mx != mn else f"${mn:g}"
            out.append(norm(
                name=strip_html(ev.get("name", "")),
                date=local_date,
                time_=(sd.get("localTime") or "")[:5],
                venue=v.get("name", ""),
                address=(v.get("address") or {}).get("line1", ""),
                lat=float(loc["latitude"]) if loc.get("latitude") else None,
                lng=float(loc["longitude"]) if loc.get("longitude") else None,
                description=strip_html(ev.get("info", "") or ev.get("pleaseNote", "")),
                image=best_img,
                category=(cls.get("segment") or {}).get("name", "") if isinstance(cls, dict) else "",
                tags=", ".join(filter(None, [
                    (cls.get("genre") or {}).get("name", ""),
                    (cls.get("subGenre") or {}).get("name", ""),
                ])) if isinstance(cls, dict) else "",
                cost=cost,
                url=ev.get("url", ""),
                source=source_name,
            ))
            if len(out) >= max_events:
                break
        pg = data.get("page") or {}
        if page + 1 >= pg.get("totalPages", 1):
            break
        page += 1
        time.sleep(0.3)
    return out



# ---------------- beventi_vendor  (Beventi vendor profile, Nuxt 3 payload) ------

def _devalue(flat, i=0, depth=0, max_depth=12):
    """Resolve a Nuxt 3 (devalue) `_payload.json` flat array into plain data.

    The payload is a list where any INTEGER is an index back into the same list,
    and the reference graph has CYCLES (an event's `top_profiles` points back at
    the vendor), so a naive recursive resolver never terminates. The depth cap is
    the whole trick: past `max_depth` we return None instead of recursing. 12 is
    deep enough for the event objects (~4 levels) with slack to spare.

    Reactivity wrappers (`["ShallowReactive", 7]`) are unwrapped to their target.
    Generic to any Nuxt 3 site — reusable if another one shows up."""
    if depth > max_depth:
        return None
    # bools are NOT indices (isinstance(True, int) is True in Python, and devalue
    # stores real booleans in their own slot) — exclude them explicitly
    is_ref = isinstance(i, int) and not isinstance(i, bool) and 0 <= i < len(flat)
    v = flat[i] if is_ref else i
    if isinstance(v, dict):
        return {k: _devalue(flat, x, depth + 1, max_depth) for k, x in v.items()}
    if isinstance(v, list):
        if v and v[0] in ("ShallowReactive", "Reactive", "ShallowRef", "Ref"):
            return _devalue(flat, v[1], depth + 1, max_depth)
        return [_devalue(flat, x, depth + 1, max_depth) for x in v]
    return v


def scrape_beventi_vendor(payload_url, source_name, start, end, max_events=100):
    """Beventi (beventi.co) — a small national ticketing platform for book/author
    signing events. The vendor profile is a Nuxt 3 SSR page; appending
    `/_payload.json` to the vendor URL yields the page's whole dataset in one ~8 KB
    GET (no auth, no Cloudflare). The Laravel API at api2.beventi.co is auth-gated
    and NOT usable — see Philly source "the-broken-spine-beventi".

    Shape: `_devalue(flat)["data"]["orderForms-profile-<profile_id>"]` — one entry
    per open ticket order form, each wrapping a full `event` object (title,
    description, venue_name, street address, lat/lng, date_start/end,
    start_time/end_time, IANA timezone, image). The profile id is the vendor's, so
    the key is found BY PREFIX, never hardcoded. Times are already venue wall-clock
    strings — no timezone math needed; `date_end` is authoritative and can roll past
    midnight (a 22:00-00:00 release party ends the NEXT day).

    Ticket url is `https://beventi.co/tickets/<form.uid>`; cost comes from
    `ticket_badges.pricing.label` ("Tickets from $25.00") and falls back to the
    availability label ("Sold out").

    AGGREGATOR adapter: one organizer, several host venues (their shop + partner
    breweries), so every row carries route_tag(venue_name) for ingest's coverage
    fan-out. SAFETY CAP: exactly one fetch, no pagination; max_events is a HARD
    ceiling on rows returned."""
    try:
        flat = core.fetch_json(payload_url)
    except (HTTPError, URLError, json.JSONDecodeError) as e:
        print(f"  [{source_name}] beventi: {e}", file=sys.stderr)
        return []
    if not isinstance(flat, list):
        print(f"  [{source_name}] beventi: unexpected payload shape", file=sys.stderr)
        return []

    data = (_devalue(flat) or {}).get("data") or {}
    forms = []
    for k, v in data.items():
        if k.startswith("orderForms-profile-") and isinstance(v, list):
            forms.extend(v)

    root = host_root(payload_url)
    out, seen = [], set()
    for f in forms:
        if not isinstance(f, dict):
            continue
        ev = f.get("event")
        if not isinstance(ev, dict) or ev.get("private") or ev.get("is_virtual"):
            continue
        if ev.get("status") not in (None, "published"):
            continue
        name = strip_html(ev.get("title") or "")
        d1 = (ev.get("date_start") or "")[:10]
        if not name or not d1 or not _in_range(d1, start, end):
            continue
        uid = f.get("uid") or ""
        key = (uid, d1, name)
        if key in seen:
            continue
        seen.add(key)
        vname = strip_html(ev.get("venue_name") or "")
        addr = ", ".join(p for p in [
            strip_html(ev.get("address_line1") or ""),
            strip_html(ev.get("address_line2") or ""),
            strip_html(ev.get("city") or ""),
            " ".join(p for p in [strip_html(ev.get("state") or ""),
                                 strip_html(ev.get("postal_code") or "")] if p),
        ] if p)
        badges = f.get("ticket_badges") or {}
        pricing = badges.get("pricing") or {}
        avail = badges.get("availability") or {}
        cost = strip_html(pricing.get("label") or avail.get("label") or "")
        try:
            lat = float(ev["latitude"]) if ev.get("latitude") else None
            lng = float(ev["longitude"]) if ev.get("longitude") else None
        except (TypeError, ValueError):
            lat = lng = None
        d2 = (ev.get("date_end") or "")[:10]
        out.append(norm(
            name=name,
            date=d1, time_=(ev.get("start_time") or "")[:5],
            end_date=d2 or d1,   # authoritative; rolls to the NEXT day on a
                                 # 22:00-00:00 party (ingest needs it to build `end`)
            end_time=(ev.get("end_time") or "")[:5],
            venue=vname, address=addr, lat=lat, lng=lng,
            description=strip_html(ev.get("description") or ""),
            image=ev.get("image") or "",
            category="Books", tags=route_tag(vname), cost=cost,
            url=f"{root}/tickets/{uid}" if uid else payload_url,
            source=source_name,
        ))
        if len(out) >= max_events:
            break
    return out



# ---------------- xtrulink  (ticketapp.org month-grid ticketing calendars) ------

_XTL_DAY = re.compile(
    r'<div class="XTL-events" data-date="(\d{4}-\d{2}-\d{2})"')
_XTL_NAME_LINK = re.compile(
    r'class="XTL-event-name">\s*<a href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_XTL_TIME = re.compile(r'class="XTL-event-time">\s*([^<]*)<')
_XTL_IMG = re.compile(r'class="[^"]*XTL-event-image"\s+src="([^"]+)"')
_XTL_DESC = re.compile(r'class="[^"]*XTL-event-description">(.*)', re.S)


def _xtl_hhmm(t):
    """'10:00am' / '5pm' -> 'HH:MM' (24h), '' on anything else."""
    m = re.match(r"^\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*$", (t or "").lower())
    if not m:
        return ""
    h = int(m.group(1)) % 12 + (12 if m.group(3) == "pm" else 0)
    return f"{h:02d}:{int(m.group(2) or 0):02d}"


def scrape_xtrulink(url_template, source_name, start, end, max_months=6,
                    max_events=200):
    """XTruLink (ticketapp.org) — a generic museum-ticketing platform whose
    embedded calendar serves an HTML month grid (NOT JSON) at
    /api/calendar/data/<calendar-uuid>/?v=month&m=<M>&d=1&y=<Y>. The source row's
    url carries {m}/{y} placeholders + its own calendar uuid, so this adapter is
    venue-agnostic (Woodmere Art Museum first: Philly source "woodmere-art-museum").

    Parse per day cell (`XTL-events` + data-date): each `XTL-eventbox` sibling
    carries name + ticket url (XTL-event-name > a), '10:00am - 5:00pm' time
    (XTL-event-time), image, and a rich-HTML description (stripped by norm).

    TRAPS (Scout, 2026-08-29): fake 200s — wrong paths AND a bare 'Mozilla/5.0'
    UA both return the XTruLink dashboard LOGIN page with HTTP 200 and zero
    XTL-event markers, so success is gated on markers, never status. The grid
    includes muted adjacent-month days, so month fetches overlap — dedupe on
    (date, name, time).

    SAFETY CAP: max_months month fetches per call, max_events hard ceiling."""
    out, seen = [], set()
    ym = (start.year, start.month)
    end_ym = (end.year, end.month)
    months = 0
    while ym <= end_ym and months < max_months and len(out) < max_events:
        y, m = ym
        url = url_template.replace("{m}", str(m)).replace("{y}", str(y))
        try:
            body = core.fetch(url).decode("utf-8", "replace")
        except (HTTPError, URLError) as e:
            print(f"  [{source_name}] xtrulink {y}-{m:02d}: {e}", file=sys.stderr)
            break
        if "XTL-event" not in body:
            # fake-200 login page (bad UA / bad path / dead calendar uuid)
            print(f"  [{source_name}] xtrulink {y}-{m:02d}: no XTL-event markers "
                  f"(login page?)", file=sys.stderr)
            break
        # split into day cells; parts[0] is the preamble before the first cell
        parts = _XTL_DAY.split(body)
        for i in range(1, len(parts), 2):
            date, chunk = parts[i], parts[i + 1]
            if not (start <= dt.date.fromisoformat(date) <= end):
                continue
            for box in re.split(r'<div class="XTL-eventbox">', chunk)[1:]:
                # bound at the NEXT event's summary div so the greedy
                # description regex can't absorb the following event
                box = re.split(r'<div class="XTL-event[ "]', box)[0]
                nl = _XTL_NAME_LINK.search(box)
                if not nl:
                    continue
                link, name = nl.group(1), strip_html(nl.group(2))
                tm = _XTL_TIME.search(box)
                t_start = t_end = ""
                if tm:
                    bits = [b.strip() for b in tm.group(1).split("-", 1)]
                    t_start = _xtl_hhmm(bits[0])
                    t_end = _xtl_hhmm(bits[1]) if len(bits) > 1 else ""
                key = (date, name, t_start)
                if not name or key in seen:
                    continue
                seen.add(key)
                img = _XTL_IMG.search(box)
                desc = _XTL_DESC.search(box)
                out.append(norm(
                    name=name, date=date, time_=t_start, end_time=t_end,
                    description=strip_html(desc.group(1)) if desc else "",
                    image=(img.group(1).replace(" ", "%20") if img else ""),
                    url=link, source=source_name,
                ))
                if len(out) >= max_events:
                    break
        months += 1
        ym = (y + 1, 1) if m == 12 else (y, m + 1)
        time.sleep(0.2)
    return out


# ---------------- signup.software  (Signup Software storefront) ----------------

_SS_STORE = re.compile(r"/store/(\d+)")
# Small orgs date-stamp their titles ("9/3 Lindy 101 Lesson & Social Dance"). The
# date is already a column here, so drop a leading M/D (or M/D/YY) prefix.
_SS_DATE_PREFIX = re.compile(r"^\d{1,2}/\d{1,2}(?:/\d{2,4})?\s*[-–—:]?\s+")


def _ss_local(iso, tzname):
    """UTC ISO ('2026-09-04T00:00:00.000Z') -> local datetime in the venue's tz."""
    if not iso:
        return None
    try:
        d = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    try:
        tz = ZoneInfo(tzname) if tzname else LOCAL_TZ
    except (ZoneInfoNotFoundError, ValueError):
        tz = LOCAL_TZ
    return d.astimezone(tz)


def scrape_signup_software(store_url, source_name, start, end, max_events=200):
    """Signup Software (signup.software) — small-org class/event ticketing.
    Storefront is a React SPA; the data comes from ONE unauthenticated
    Prisma-shaped POST: `https://api.signup.software/query/event` with
    {where, orderBy, include, take}. Org id is the `/store/<oid>/` path segment.

    `store_url` carries the config in its query string:
      venue=<id>    only this venue's events (a storefront can span rooms)
      exclude=<re>  drop names matching this regex, case-insensitive. Jazz Attack
                    sells each Thursday TWICE — "Lindy 101 Lesson & Social Dance"
                    (20:00-23:00) and "Just the Dance!" (21:00-23:00, the same
                    party minus the lesson) — so the second is excluded, not
                    listed as a separate event.
      series=1      also emit `isSeries` rows (multi-week courses). Default OFF:
                    a series is one DB row with an rrule in repeatOptions, and
                    its sessions duplicate the weekly drop-in events.

    Times are UTC in the API and localized with the joined venue's `timezone`.
    Price is integer CENTS. SAFETY CAP: exactly one POST, `take` = max_events."""
    parsed = urlparse(store_url)
    m = _SS_STORE.search(parsed.path)
    if not m:
        print(f"  [{source_name}] signup_software: no /store/<id>/ in {store_url}", file=sys.stderr)
        return []
    oid = int(m.group(1))
    q = parse_qs(parsed.query)
    venue_id = int(q["venue"][0]) if q.get("venue") else None
    exclude = re.compile(q["exclude"][0], re.I) if q.get("exclude") else None
    want_series = q.get("series", ["0"])[0] not in ("0", "", "false")

    where = {"organizationId": oid, "published": True, "hidden": False,
             "cancelled": False,
             "lastOccuringDate": {"gte": f"{start.isoformat()}T00:00:00.000Z"},
             "startTime": {"lte": f"{(end + dt.timedelta(days=1)).isoformat()}T23:59:59.999Z"}}
    if venue_id is not None:
        where["venueId"] = venue_id
    payload = {"where": where, "orderBy": [{"startTime": "asc"}],
               "take": max(1, min(int(max_events), 200)),
               "include": {"venue": {"select": {"id": True, "name": True,
                                                "address": True, "timezone": True}}}}
    try:
        data = core.post_json("https://api.signup.software/query/event", payload)
    except (HTTPError, URLError, json.JSONDecodeError) as e:
        print(f"  [{source_name}] signup_software: {e}", file=sys.stderr)
        return []

    out = []
    for ev in (data.get("data") or []):
        name = (ev.get("name") or "").strip()
        if not name or (exclude and exclude.search(name)):
            continue
        if ev.get("isSeries") and not want_series:
            continue
        name = _SS_DATE_PREFIX.sub("", name)
        venue = ev.get("venue") or {}
        sdt = _ss_local(ev.get("startTime"), venue.get("timezone"))
        edt = _ss_local(ev.get("endTime"), venue.get("timezone"))
        if not sdt or not (start <= sdt.date() <= end):
            continue
        cents = ev.get("price")
        cost = ""
        if isinstance(cents, int):
            cost = "Free" if cents == 0 else f"${cents / 100:.2f}"
        desc = " ".join(x for x in (ev.get("headline"), ev.get("description")) if x)
        out.append(norm(
            name=name, date=sdt.date().isoformat(), time_=sdt.strftime("%H:%M"),
            end_date=edt.date().isoformat() if edt else "",
            end_time=edt.strftime("%H:%M") if edt else "",
            venue=venue.get("name") or "", address=venue.get("address") or "",
            description=strip_html(desc), image=ev.get("mediaUrl") or "",
            category=ev.get("eventType") or "", cost=cost,
            url=f"https://signup.software/store/{oid}/events/{ev.get('id')}",
            source=source_name,
        ))
        if len(out) >= max_events:
            break
    return out


# ---------------- venuepilot  (VenuePilot box-office GraphQL) ----------------
#
# 2026-09-06 (Max): VenuePilot is a box-office/ticketing platform used by small
# music + comedy rooms. Venues embed a widget script
# (<script src="https://www.venuepilot.co/widgets/<key>.js">) whose inline
# `window.venuepilotSettings.general.accountIds` names the account; the widget
# bundle then talks to a PUBLIC, unauthenticated Apollo endpoint at
# https://www.venuepilot.co/graphql. The `paginatedEvents(arguments: {...})`
# query below is copied verbatim from the shipped bundle (vp-widget.umd.js), so
# it stays in sync with what the site itself asks for. Schema introspection is
# DISABLED on this endpoint — don't try to rediscover it that way; read the
# bundle. Claude's Comedy Club (account 3340) was the first source.
#
# Generic per-account adapter: any VenuePilot room in any city reuses it. The
# account id travels in the source url as a query param, never in code:
#   https://www.venuepilot.co/graphql?accountIds=3340

_VP_QUERY = """
query($accountIds: [Int!]!, $startDate: String!, $endDate: String,
      $limit: Int, $page: Int) {
  paginatedEvents(arguments: {
    accountIds: $accountIds, startDate: $startDate, endDate: $endDate,
    limit: $limit, page: $page
  }) {
    collection {
      id name date doorTime startTime endTime minimumAge description
      websiteUrl status ticketsUrl venue { name }
    }
    metadata { currentPage totalPages totalCount }
  }
}
"""

# VenuePilot rooms list the SAME show several times when it has multiple ticket
# products (a 21+ door ticket, an all-ages ticket, a performer sign-up slot).
# The sign-up rows are box-office plumbing, not events a reader can attend, so
# they're dropped. Opt-out with drop_signups=False.
_VP_SIGNUP = re.compile(r"sign[\s-]*up", re.I)


def _vp_time(val):
    """VenuePilot returns wall-clock 'YYYY-MM-DD HH:MM:SS' (or ISO) in the
    venue's own timezone — no offset, so no conversion. Return 'HH:MM'."""
    s = (val or "").strip().replace("T", " ")
    m = re.search(r"(\d{2}):(\d{2})", s)
    return f"{m.group(1)}:{m.group(2)}" if m else ""


def scrape_venuepilot(api_url, source_name, start, end,
                      max_pages=6, max_events=200, drop_signups=True):
    """VenuePilot public GraphQL (paginatedEvents). api_url carries the account:
    https://www.venuepilot.co/graphql?accountIds=3340 (repeatable/comma-listed).

    `date` is the show date; `startTime`/`endTime` are venue wall-clock. A show
    that ends after midnight reports an endTime EARLIER than startTime — we roll
    end_date to the next day rather than emit a backwards event.
    SAFETY CAP: max_pages requests of 50 rows, and max_events rows overall."""
    q = parse_qs(urlparse(api_url).query)
    ids = []
    for raw in q.get("accountIds", []) + q.get("accountId", []):
        for part in raw.split(","):
            part = part.strip()
            if part.isdigit():
                ids.append(int(part))
    if not ids:
        print(f"  [{source_name}] venuepilot: no accountIds in url", file=sys.stderr)
        return []
    endpoint = api_url.split("?")[0]

    out, page, seen = [], 1, set()
    while page <= max_pages and len(out) < max_events:
        payload = {"query": _VP_QUERY, "variables": {
            "accountIds": ids, "startDate": start.isoformat(),
            "endDate": end.isoformat(), "limit": 50, "page": page}}
        try:
            data = core.post_json(endpoint, payload,
                                  headers={"Referer": "https://www.venuepilot.co/"})
        except (HTTPError, URLError, ValueError) as e:
            print(f"  [{source_name}] venuepilot p{page}: {e}", file=sys.stderr)
            break
        if data.get("errors"):
            print(f"  [{source_name}] venuepilot: {data['errors'][0].get('message')}",
                  file=sys.stderr)
            break
        block = ((data.get("data") or {}).get("paginatedEvents")) or {}
        rows = block.get("collection") or []
        if not rows:
            break
        for ev in rows:
            if not isinstance(ev, dict):
                continue
            eid = str(ev.get("id") or "")
            if eid and eid in seen:
                continue
            seen.add(eid)
            name = strip_html(ev.get("name") or "").strip()
            if not name:
                continue
            if drop_signups and _VP_SIGNUP.search(name):
                continue
            date = (ev.get("date") or "")[:10]
            if not _in_range(date, start, end):
                continue
            t0 = _vp_time(ev.get("startTime")) or _vp_time(ev.get("doorTime"))
            t1 = _vp_time(ev.get("endTime"))
            end_date = ""
            if t1 and t0 and t1 < t0:          # ends after midnight
                try:
                    end_date = (dt.date.fromisoformat(date)
                                + dt.timedelta(days=1)).isoformat()
                except ValueError:
                    end_date = ""
            age = ev.get("minimumAge")
            vname = ((ev.get("venue") or {}).get("name") or "").strip()
            out.append(norm(
                name=name, date=date, time_=t0,
                end_date=end_date, end_time=t1,
                venue=vname,
                description=ev.get("description") or "",
                tags=_with_route(f"{age}+" if age else "", vname),
                url=(ev.get("ticketsUrl") or ev.get("websiteUrl") or api_url),
                source=source_name,
            ))
            if len(out) >= max_events:
                break
        meta = block.get("metadata") or {}
        if page >= (meta.get("totalPages") or page):
            break
        page += 1
    return out
