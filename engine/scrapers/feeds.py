"""Feed/calendar platforms: localist, bibliocommons, wix, ical (+ ppr_ical routing wrapper), luma, nps_api."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse, parse_qs
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import core
from .core import LOCAL_TZ, UA, _in_range, _with_route, host_root, norm, route_tag, strip_html

# ---------------- localist  (/api/2/events JSON; libraries & universities) ----

def scrape_localist(base_url, source_name, start, end, max_pages=10):
    """Localist platform API. base_url = host root (https://events.depaul.edu).
    Used by DePaul, UChicago; BiblioCommons/CPL is a separate adapter."""
    root = host_root(base_url)
    days = (end - start).days + 1
    out, page = [], 1
    while page <= max_pages:
        url = (f"{root}/api/2/events?start={start.isoformat()}&days={days}"
               f"&pp=100&page={page}")
        try:
            data = core.fetch_json(url)
        except (HTTPError, URLError, json.JSONDecodeError) as e:
            print(f"  [{source_name}] localist: {e}", file=sys.stderr)
            break
        evs = data.get("events", [])
        if not evs:
            break
        for wrap in evs:
            e = wrap.get("event", {})
            inst = (e.get("event_instances") or [{}])[0].get("event_instance", {})
            s = inst.get("start", "") or ""
            en = inst.get("end", "") or ""
            if not _in_range(s, start, end):
                continue
            geo = e.get("geo") or {}
            out.append(norm(
                name=strip_html(e.get("title", "")),
                date=s[:10], time_=s[11:16] if len(s) >= 16 else "",
                end_date=en[:10], end_time=en[11:16] if len(en) >= 16 else "",
                venue=e.get("location_name", "") or e.get("location", ""),
                # 2026-09-06: routing tag so an aggregator (MIT's calendar)
                # can route on coverage.routing_key like tribe_wp does.
                tags=route_tag(e.get("location_name", "") or e.get("location", "")),
                address=geo.get("street", "") or "",
                lat=float(geo["latitude"]) if geo.get("latitude") else None,
                lng=float(geo["longitude"]) if geo.get("longitude") else None,
                description=strip_html(e.get("description_text") or e.get("description", "")),
                image=e.get("photo_url", ""),
                category=", ".join(f.get("name", "") for f in (e.get("filters", {}) or {}).get("event_types", []) if isinstance(f, dict)),
                cost=e.get("ticket_cost", "") or "",
                url=e.get("localist_url", "") or e.get("url", ""),
                source=source_name,
            ))
        pg = data.get("page", {})
        if pg.get("current", 1) >= pg.get("total", 1):
            break
        page += 1
        time.sleep(0.3)
    return out


# ---------------- bibliocommons  (public gateway JSON; libraries) ----------------

def scrape_bibliocommons(search_url, source_name, start, end, max_pages=10):
    """BiblioCommons (Chicago Public Library et al). The HTML search page is a
    client-side Ember app — we hit the public gateway JSON API instead.

    search_url is the human URL, e.g.
      https://chipublib.bibliocommons.com/events/search/index?locations=46
    We pull the library slug from the host and the locations filter from the query,
    then call gateway.bibliocommons.com/v2/libraries/<slug>/events/search.
    """
    p = urlparse(search_url)
    lib = p.netloc.split(".")[0]  # chipublib.bibliocommons.com -> chipublib
    qs = parse_qs(p.query)
    locations = (qs.get("locations") or [""])[0]
    api = f"https://gateway.bibliocommons.com/v2/libraries/{lib}/events/search"
    out, page = [], 1
    while page <= max_pages:
        url = (f"{api}?startDate={start.isoformat()}&endDate={end.isoformat()}"
               f"&limit=50&page={page}")
        if locations:
            url += f"&locations={locations}"
        try:
            data = core.fetch_json(url, headers={"Accept": "application/json"})
        except (HTTPError, URLError, json.JSONDecodeError) as e:
            print(f"  [{source_name}] bibliocommons: {e}", file=sys.stderr)
            break
        ents = data.get("entities", {}) or {}
        ev_ents = ents.get("events", {}) or {}
        locs = ents.get("locations", {}) or {}
        imgs = ents.get("images", {}) or {}
        results = (data.get("events", {}) or {}).get("results", []) or []
        for eid in results:
            ev = ev_ents.get(eid) or ev_ents.get(str(eid)) or {}
            d = ev.get("definition", {}) or {}
            s = d.get("start", "") or ""          # local "YYYY-MM-DDTHH:MM"
            en = d.get("end", "") or ""
            if not _in_range(s, start, end):
                continue
            loc = locs.get(str(d.get("branchLocationId")), {}) or {}
            img = imgs.get(str(d.get("featuredImageId")), {}) or {}
            out.append(norm(
                name=strip_html(d.get("title", "")),
                date=s[:10], time_=s[11:16] if len(s) >= 16 else "",
                end_date=en[:10], end_time=en[11:16] if len(en) >= 16 else "",
                venue=loc.get("name", ""),
                description=strip_html(d.get("description", "")),
                image=img.get("url", ""),
                url=f"https://{p.netloc}/events/{ev.get('id', eid)}",
                source=source_name,
            ))
        pg = (data.get("events", {}) or {}).get("pagination", {}) or {}
        if pg.get("page", page) >= pg.get("pages", page):
            break
        page += 1
        time.sleep(0.3)
    return out


# ---------------- wix  (Wix Events; instance token + query API) ----------------

WIX_EVENTS_APP_ID = "140603ad-af8d-84a5-2c80-a0f60cb47351"


def _wix_instance(root):
    """Fetch the short-lived signed Wix Events instance token for a site root."""
    data = core.fetch_json(f"{root}/_api/v1/access-tokens")
    return ((data.get("apps") or {}).get(WIX_EVENTS_APP_ID) or {}).get("instance")


def _wix_local_datetime(value, timezone_id):
    """Wix emits UTC instants plus the event's IANA venue timezone.

    Return a naive venue wall-clock datetime, which is the engine's storage
    contract. Invalid/missing values stay non-events instead of being guessed.
    """
    if not value:
        return None
    try:
        instant = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if instant.tzinfo is not None:
            if not timezone_id:
                return None
            instant = instant.astimezone(ZoneInfo(timezone_id))
        return instant.replace(tzinfo=None)
    except (ValueError, TypeError, ZoneInfoNotFoundError):
        return None


def scrape_wix(page_url, source_name, start, end, max_pages=10):
    """Wix Events. Two steps: get a signed instance token from /_api/v1/access-tokens,
    then POST to /_api/wix-events-web/v1/events/query. Returns SCHEDULED (upcoming)
    events. page_url is any page on the site (we use its host root)."""
    root = host_root(page_url)
    try:
        instance = _wix_instance(root)
    except (HTTPError, URLError, json.JSONDecodeError) as e:
        print(f"  [{source_name}] wix token: {e}", file=sys.stderr)
        return []
    if not instance:
        print(f"  [{source_name}] wix: no Events app on this site", file=sys.stderr)
        return []

    api = f"{root}/_api/wix-events-web/v1/events/query"
    headers = {"Authorization": instance, "Content-Type": "application/json"}
    out, offset, page = [], 0, 0
    while page < max_pages:
        body = json.dumps({
            "limit": 100, "offset": offset, "fieldset": ["FULL"],
            "filter": {"status": "SCHEDULED"},
            "sort": "scheduling.config.startDate:asc",
        }).encode()
        try:
            req = Request(api, data=body, headers={"User-Agent": UA, **headers})
            with urlopen(req, timeout=20) as r:
                data = json.loads(r.read())
        except (HTTPError, URLError, json.JSONDecodeError) as e:
            print(f"  [{source_name}] wix query: {e}", file=sys.stderr)
            break
        events = data.get("events", []) or []
        if not events:
            break
        for ev in events:
            sched = (ev.get("scheduling") or {}).get("config", {}) or {}
            s = sched.get("startDate", "") or ""   # UTC ISO + IANA timeZoneId
            en = sched.get("endDate", "") or ""
            tzid = sched.get("timeZoneId", "") or ""
            sdt = _wix_local_datetime(s, tzid)
            edt = _wix_local_datetime(en, tzid)
            if not sdt or not (start <= sdt.date() <= end):
                continue
            slug_ = ev.get("slug", "")
            img = ev.get("mainImage") or {}
            out.append(norm(
                name=strip_html(ev.get("title", "")),
                date=sdt.date().isoformat(), time_=sdt.strftime("%H:%M"),
                end_date=edt.date().isoformat() if edt else "",
                end_time=edt.strftime("%H:%M") if edt else "",
                description=strip_html(ev.get("description") or ev.get("about", "")),
                image=img.get("url", "") if isinstance(img, dict) else "",
                url=f"{root}/events/{slug_}" if slug_ else "",
                source=source_name,
            ))
        total = data.get("total", 0)
        offset += len(events)
        page += 1
        if offset >= total:
            break
        time.sleep(0.3)
    return out


# ---------------- ical  (.ics feed) ----------------

def _ics_unescape(v: str) -> str:
    """RFC-5545 TEXT unescaping: \\n / \\N -> newline, \\, \\; \\\\ -> literal.
    Without this, LibCal/DDay feeds leak raw backslashes into titles and
    descriptions ("Songs\\, Stories and Play") — 2026-08-29, Max (cambridge)."""
    if not v:
        return v
    out, i = [], 0
    while i < len(v):
        c = v[i]
        if c == "\\" and i + 1 < len(v):
            nxt = v[i + 1]
            out.append("\n" if nxt in "nN" else nxt)
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def scrape_ical(ics_url, source_name, start, end):
    """Minimal iCalendar VEVENT parser (no external deps). Handles DTSTART/DTEND
    in date and datetime forms, SUMMARY, DESCRIPTION, URL, LOCATION. Unfolds
    RFC-5545 line continuations. Does NOT expand RRULE recurrences."""
    try:
        raw = core.fetch(ics_url).decode("utf-8", "replace")
    except (HTTPError, URLError) as e:
        print(f"  [{source_name}] ical: {e}", file=sys.stderr)
        return []
    # unfold continuation lines (a line starting with space/tab continues the prior)
    raw = re.sub(r"\r?\n[ \t]", "", raw)
    out = []
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", raw, re.S):
        fields = {}
        for line in block.splitlines():
            if ":" not in line:
                continue
            key, val = line.split(":", 1)
            fields[key.split(";")[0]] = (val.strip(), key)

        def _dt(name):
            if name not in fields:
                return None
            val, key = fields[name]
            is_utc = val.endswith("Z")          # RFC 5545: trailing Z = UTC
            v = val.replace("Z", "")
            try:
                if "T" in v:                       # 20260601T190000
                    d = dt.datetime.strptime(v[:15], "%Y%m%dT%H%M%S")
                    if is_utc:
                        # Google Calendar ICS emits UTC; a 7pm ET event used to
                        # store as 23:00 (and cross midnight → wrong DATE).
                        # Convert to local wall-clock (2026-08-16, Max).
                        d = (d.replace(tzinfo=dt.timezone.utc)
                             .astimezone(LOCAL_TZ).replace(tzinfo=None))
                    return d
                return dt.datetime.strptime(v[:8], "%Y%m%d")   # all-day
            except Exception:
                return None

        sdt = _dt("DTSTART")
        if not sdt or not (start <= sdt.date() <= end):
            continue
        edt = _dt("DTEND")
        name = fields.get("SUMMARY", ("", ""))[0]
        if not name:
            continue
        out.append(norm(
            name=strip_html(_ics_unescape(name)),
            date=sdt.strftime("%Y-%m-%d"),
            time_=sdt.strftime("%H:%M") if "T" in fields.get("DTSTART", ("",))[0] else "",
            end_date=edt.strftime("%Y-%m-%d") if edt else "",
            end_time=edt.strftime("%H:%M") if edt and "T" in fields.get("DTEND", ("",))[0] else "",
            description=strip_html(_ics_unescape(fields.get("DESCRIPTION", ("", ""))[0])),
            # LOCATION stays RAW: scrape_ppr_ical does its own unescape + comma
            # split for venue routing, and pre-unescaping here would change its
            # \n handling (space, not newline). Keep that wrapper byte-identical.
            address=fields.get("LOCATION", ("", ""))[0],
            url=fields.get("URL", ("", ""))[0],
            source=source_name, all_day="T" not in fields.get("DTSTART", ("",))[0],
        ))
    return out


# ---------------- ppr_ical  (PPR Google Calendar ICS + LOCATION routing) ------


# ---------------- cambridge_city_ical  (City of Cambridge .ics) --------------


# ---------------- luma  (lu.ma calendars — PORTABLE) -----------------------

_LUMA_CAL_ID = re.compile(r"cal-[A-Za-z0-9]+")


def _luma_local(value, timezone_id):
    """Luma emits UTC instants ('...Z') plus the event's own IANA timezone.
    Return a naive venue wall-clock datetime (the engine's storage contract),
    or None if the value is missing/unparseable."""
    if not value:
        return None
    try:
        instant = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if instant.tzinfo is not None:
            instant = instant.astimezone(ZoneInfo(timezone_id or "America/New_York"))
        return instant.replace(tzinfo=None)
    except (ValueError, TypeError, ZoneInfoNotFoundError):
        return None


def _luma_cost(ticket_info):
    """Luma `ticket_info` -> a cost string. '' when the shape is unknown (better
    an empty cost than an invented free/paid claim)."""
    if not isinstance(ticket_info, dict):
        return ""
    if ticket_info.get("is_free"):
        return "Free"
    price = ticket_info.get("price")
    if isinstance(price, dict) and isinstance(price.get("cents"), int):
        cents = price["cents"]
        if cents == 0:
            return "Free"
        dollars = cents / 100
        amount = f"${dollars:,.0f}" if cents % 100 == 0 else f"${dollars:,.2f}"
        return amount + ("+" if price.get("is_flexible") else "")
    return ""


def scrape_luma(url, source_name, start, end, max_pages=5, max_events=200):
    """Luma (lu.ma / luma.com) public calendars — PORTABLE, one adapter per city.

    Luma hosts a calendar per org; the public listing API needs no auth:
        GET https://api.lu.ma/calendar/get-items
            ?calendar_api_id=cal-XXXX&period=future&pagination_limit=50
        -> {"entries": [{"event": {...}, "ticket_info": {...}, "tags": [...]},
            ...], "has_more": bool}

    `url` may be either that API url or the human calendar page
    (https://luma.com/<handle>) — the calendar api id is pulled out of whichever
    is given (the page carries it in __NEXT_DATA__), so a source row can hold the
    friendly url. The query is rebuilt here, so period/limit never have to be
    right on the source row.

    Per entry, `event` carries name, UTC `start_at`/`end_at` (plus the event's own
    IANA `timezone` — converted to venue wall clock, the engine's storage
    contract), a short `url` SLUG (-> https://luma.com/<slug>), `cover_url`,
    `geo_address_info` (full_address / venue name) and `coordinate`. Cost comes
    from the entry's `ticket_info` (is_free / price.cents), category from `tags`.
    Descriptions are NOT in the listing (they'd cost one fetch per event) — the
    engine keeps the title + url instead.

    SAFETY CAP: one HTTP call per page, `max_pages` pages, `max_events` rows.
    Pagination follows `next_cursor`/`has_more`; it stops the moment either the
    cursor is missing or the window is exceeded.
    """
    m = _LUMA_CAL_ID.search(url or "")
    if not m:                                # human page -> dig the id out of it
        try:
            m = _LUMA_CAL_ID.search(core.fetch(url).decode("utf-8", "replace"))
        except (HTTPError, URLError) as e:
            print(f"  [{source_name}] luma: {e}", file=sys.stderr)
            return []
    if not m:
        print(f"  [{source_name}] luma: no calendar_api_id in {url}", file=sys.stderr)
        return []
    cal_id = m.group(0)

    api = ("https://api.lu.ma/calendar/get-items"
           f"?calendar_api_id={cal_id}&period=future&pagination_limit=50")
    out, cursor, seen = [], "", set()
    for _ in range(max_pages):
        page_url = api + (f"&pagination_cursor={cursor}" if cursor else "")
        try:
            data = core.fetch_json(page_url)
        except (HTTPError, URLError, json.JSONDecodeError) as e:
            print(f"  [{source_name}] luma: {e}", file=sys.stderr)
            break
        entries = (data or {}).get("entries") or []
        if not entries:
            break
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            ev = entry.get("event") or {}
            name = (ev.get("name") or "").strip()
            when = _luma_local(ev.get("start_at"), ev.get("timezone") or "America/New_York")
            if not name or when is None:
                continue
            if when.date() > end:
                continue                     # feed is chronological; keep scanning
            if when.date() < start:
                continue
            key = ev.get("api_id") or (name, when)
            if key in seen:
                continue
            seen.add(key)
            end_dt = _luma_local(ev.get("end_at"), ev.get("timezone") or "America/New_York")
            geo = ev.get("geo_address_info") or {}
            coord = ev.get("coordinate") or {}
            tags = [t.get("name", "") for t in (entry.get("tags") or [])
                    if isinstance(t, dict)]
            slug = (ev.get("url") or "").strip()
            place = (geo.get("address") or "").strip()
            # full_address is 'Venue Name, 308 E Girard Ave, ..., USA' — drop the
            # duplicated venue name and the country so the geocoder gets a street.
            addr = (geo.get("full_address") or "").strip()
            if place and addr.startswith(place + ","):
                addr = addr[len(place) + 1:].strip()
            addr = re.sub(r",\s*USA$", "", addr)
            out.append(norm(
                name=name,
                date=when.date().isoformat(),
                time_=when.strftime("%H:%M"),
                end_date=(end_dt.date().isoformat()
                          if end_dt and end_dt.date() != when.date() else ""),
                end_time=end_dt.strftime("%H:%M") if end_dt else "",
                venue=place,
                address=addr,
                lat=coord.get("latitude"), lng=coord.get("longitude"),
                image=ev.get("cover_url") or ev.get("social_image_url") or "",
                category=(tags[0] if tags else ""),
                tags=",".join(t for t in tags if t),
                cost=_luma_cost(entry.get("ticket_info")),
                url=(f"https://luma.com/{slug}" if slug else ""),
                source=source_name,
                all_day=False,
            ))
            if len(out) >= max_events:
                return out
        if not (data or {}).get("has_more"):
            break
        cursor = (data.get("next_cursor") or data.get("pagination_cursor")
                  or (entries[-1].get("api_id") if isinstance(entries[-1], dict) else ""))
        if not cursor:
            break
        time.sleep(0.2)
    return out


# ---------------- google_calendar_ics  (public GCal ICS + RRULE expansion) ----

_GCAL_MAX_SCAN = 800          # occurrences examined per series before giving up


def _gcal_parse_dt(val):
    """RFC-5545 value -> (naive local datetime, is_all_day). Trailing Z = UTC."""
    is_utc = val.endswith("Z")
    v = val.replace("Z", "")
    try:
        if "T" in v:
            d = dt.datetime.strptime(v[:15], "%Y%m%dT%H%M%S")
            if is_utc:
                d = (d.replace(tzinfo=dt.timezone.utc)
                     .astimezone(LOCAL_TZ).replace(tzinfo=None))
            return d, False
        return dt.datetime.strptime(v[:8], "%Y%m%d"), True
    except Exception:
        return None, False


_GCAL_DAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


def _gcal_occurrences(sdt, rrule, win_start, win_end):
    """Expand a DAILY/WEEKLY RRULE into the naive-local start datetimes that fall
    inside [win_start, win_end] (dates). Returns [] for anything else — an
    unsupported FREQ degrades to "just the DTSTART", never to a wrong date.

    Only DAILY and WEEKLY are handled because that is what venue calendars
    actually use (quizzo, open mic, yoga, food trucks). MONTHLY/YEARLY series are
    rare enough that guessing BYSETPOS wrong would cost more than it earns."""
    parts = {}
    for kv in rrule.split(";"):
        if "=" in kv:
            k, v = kv.split("=", 1)
            parts[k.upper()] = v
    freq = parts.get("FREQ", "").upper()
    if freq not in ("DAILY", "WEEKLY"):
        return []
    interval = max(1, int(parts.get("INTERVAL") or 1))
    count = int(parts["COUNT"]) if parts.get("COUNT", "").isdigit() else None
    until = None
    if parts.get("UNTIL"):
        until, _ = _gcal_parse_dt(parts["UNTIL"])
    bydays = [_GCAL_DAYS[d[-2:]] for d in parts.get("BYDAY", "").split(",")
              if d[-2:] in _GCAL_DAYS] if freq == "WEEKLY" else []

    out, emitted, cur = [], 0, sdt
    if freq == "WEEKLY":
        # walk week-by-week from DTSTART's week, emitting each BYDAY in the week
        if not bydays:
            bydays = [sdt.weekday()]
        week0 = sdt - dt.timedelta(days=sdt.weekday())
        for w in range(_GCAL_MAX_SCAN):
            base = week0 + dt.timedelta(weeks=w * interval)
            if base.date() > win_end + dt.timedelta(days=7):
                break
            for wd in sorted(bydays):
                cur = base + dt.timedelta(days=wd)
                if cur < sdt:
                    continue
                if until and cur > until:
                    return out
                emitted += 1
                if count and emitted > count:
                    return out
                if win_start <= cur.date() <= win_end:
                    out.append(cur)
    else:                                              # DAILY
        for i in range(_GCAL_MAX_SCAN):
            cur = sdt + dt.timedelta(days=i * interval)
            if cur.date() > win_end:
                break
            if until and cur > until:
                break
            if count and i + 1 > count:
                break
            if win_start <= cur.date() <= win_end:
                out.append(cur)
    return out


def scrape_google_calendar(ics_url, source_name, start, end, max_events=200):
    """A public Google Calendar ICS feed WITH recurrence expansion (2026-09-06,
    Max — Wissahickon Brewing East Falls).

    `scrape_ical` deliberately does not expand RRULE, which is correct for feeds
    that publish one VEVENT per occurrence. Google Calendar does the opposite: a
    venue's real weekly programming (quizzo, open mic, yoga, the food-truck
    residency) is ONE VEVENT with an open-ended RRULE and a DTSTART years in the
    past, so the plain ical reading of such a feed returns almost nothing —
    Wissahickon's 3,879-VEVENT calendar yields 2 events in 30d without expansion
    and ~20 with it.

    Handles: DAILY/WEEKLY RRULE (INTERVAL, BYDAY, COUNT, UNTIL), EXDATE
    cancellations, RECURRENCE-ID overrides (the moved instance wins and the
    original occurrence is suppressed), STATUS:CANCELLED.

    SAFETY CAP: max_events hard ceiling, plus _GCAL_MAX_SCAN occurrences per
    series. One HTTP fetch total — there is no per-event fan-out."""
    try:
        raw = core.fetch(ics_url).decode("utf-8", "replace")
    except (HTTPError, URLError) as e:
        print(f"  [{source_name}] google_calendar: {e}", file=sys.stderr)
        return []
    raw = re.sub(r"\r?\n[ \t]", "", raw)

    blocks = []
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", raw, re.S):
        f, multi = {}, {}
        for line in block.splitlines():
            if ":" not in line:
                continue
            key, val = line.split(":", 1)
            name = key.split(";")[0].upper()
            f.setdefault(name, (val.strip(), key))
            multi.setdefault(name, []).append(val.strip())
        blocks.append((f, multi))

    # instances that were individually edited: (UID, occurrence-date) -> override
    overrides = set()
    for f, _ in blocks:
        if "RECURRENCE-ID" in f and "UID" in f:
            d, _ad = _gcal_parse_dt(f["RECURRENCE-ID"][0])
            if d:
                overrides.add((f["UID"][0], d.date()))

    out, seen = [], set()
    for f, multi in blocks:
        if f.get("STATUS", ("", ""))[0].upper() == "CANCELLED":
            continue
        name = f.get("SUMMARY", ("", ""))[0]
        if not name or "DTSTART" not in f:
            continue
        sdt, all_day = _gcal_parse_dt(f["DTSTART"][0])
        if not sdt:
            continue
        edt, _ = _gcal_parse_dt(f["DTEND"][0]) if "DTEND" in f else (None, False)
        dur = (edt - sdt) if edt else None
        uid = f.get("UID", ("", ""))[0]

        exdates = set()
        for raw_ex in multi.get("EXDATE", []):
            for piece in raw_ex.split(","):
                d, _ = _gcal_parse_dt(piece.strip())
                if d:
                    exdates.add(d.date())

        if "RRULE" in f and "RECURRENCE-ID" not in f:
            starts = _gcal_occurrences(sdt, f["RRULE"][0], start, end)
            if not starts and start <= sdt.date() <= end:
                starts = [sdt]                 # unsupported FREQ -> DTSTART only
        else:
            starts = [sdt] if start <= sdt.date() <= end else []

        for s in starts:
            if s.date() in exdates:
                continue
            if "RECURRENCE-ID" not in f and (uid, s.date()) in overrides:
                continue                        # the edited instance emits it
            key = (name, s.date(), s.strftime("%H:%M"))
            if key in seen:
                continue
            seen.add(key)
            e = s + dur if dur else None
            out.append(norm(
                name=strip_html(_ics_unescape(name)),
                date=s.strftime("%Y-%m-%d"),
                time_="" if all_day else s.strftime("%H:%M"),
                end_date=e.strftime("%Y-%m-%d") if e else "",
                end_time="" if (all_day or not e) else e.strftime("%H:%M"),
                description=strip_html(_ics_unescape(
                    f.get("DESCRIPTION", ("", ""))[0])),
                address=_ics_unescape(f.get("LOCATION", ("", ""))[0]),
                url=f.get("URL", ("", ""))[0],
                source=source_name, all_day=all_day,
            ))
            if len(out) >= max_events:
                out.sort(key=lambda r: (r["date"], r["time"]))
                return out
    out.sort(key=lambda r: (r["date"], r["time"]))
    return out


# ---------------- NPS events API ----------------

_NPS_TIME = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*([AaPp])\.?[Mm]\.?\s*$")


def _nps_hhmm(s):
    """'02:15 PM' -> '14:15'; '' when the string isn't a 12-hour clock."""
    m = _NPS_TIME.match(s or "")
    if not m:
        return ""
    h, mi, ap = int(m.group(1)) % 12, m.group(2), m.group(3).lower()
    return f"{h + (12 if ap == 'p' else 0):02d}:{mi}"


def _nps_true(v):
    return str(v).strip().lower() == "true"


def scrape_nps_api(url, source_name, start, end, max_pages=4, max_events=200):
    """National Park Service events API (developer.nps.gov/api/v1/events) —
    PORTABLE: any `?parkCode=<code>` url works, so every NPS unit in every city
    is one source row away (Independence NHP first, 2026-10-12).

    Key: an `api_key=` in the url wins; else env NPS_API_KEY; else DEMO_KEY
    (api.data.gov demo limit ~30 req/hr — fine for a once-daily ingest).
    The window rides server-side (dateStart/dateEnd); pageSize=50 walked by
    pageNumber, max_pages × max_events hard-capped.

    One record = one program; `dates[]` lists every occurrence (minus
    `cancelleddatelist`) and `times[]` the daily slots (12-hour strings).
    Expansion is one row per DATE, not date × slot: ingest's dedupe key is
    (source, venue, date, name), so two same-day slots of one talk would
    collapse into one row anyway. The earliest slot is the start, the latest
    slot's end the end, and every slot is spelled out in the description.
    The free-text `location` (which building / meeting point) is prepended to
    the description — dedicated sources don't store venue/address per event."""
    from urllib.parse import urlencode
    base, _, qs = url.partition("?")
    params = {k: v[-1] for k, v in parse_qs(qs).items()}
    if not params.get("api_key"):
        params["api_key"] = os.environ.get("NPS_API_KEY") or "DEMO_KEY"
    params.update({"dateStart": start.isoformat(), "dateEnd": end.isoformat(),
                   "pageSize": "50"})
    out, seen = [], set()
    for page in range(1, max_pages + 1):
        params["pageNumber"] = str(page)
        try:
            data = core.fetch_json(f"{base}?{urlencode(params)}")
        except (HTTPError, URLError, json.JSONDecodeError) as e:
            print(f"  [{source_name}] nps_api page {page}: {e}", file=sys.stderr)
            if page == 1:
                raise
            break
        items = data.get("data") or []
        for ev in items:
            title = strip_html(ev.get("title") or "")
            if not title:
                continue
            slots = []
            for t in ev.get("times") or []:
                a, b = _nps_hhmm(t.get("timestart")), _nps_hhmm(t.get("timeend"))
                if a:
                    slots.append((a, b, (t.get("timestart") or "").strip().lstrip("0")))
            slots.sort()
            all_day = _nps_true(ev.get("isallday")) or not slots
            cancelled = set(ev.get("cancelleddatelist") or [])
            dates = ev.get("dates") or ([ev["date"]] if ev.get("date") else [])
            loc = strip_html(ev.get("location") or "")
            desc = strip_html(ev.get("description") or "")
            extra = []
            if loc:
                extra.append(f"Location: {loc}.".replace("..", "."))
            if len(slots) > 1:
                extra.append("Times: " + ", ".join(s[2] for s in slots) + ".")
            if ev.get("timeinfo"):
                extra.append(strip_html(ev["timeinfo"]))
            description = " ".join(extra + [desc])
            imgs = ev.get("images") or []
            image = (imgs[0].get("url") or "") if imgs else ""
            if image.startswith("/"):
                image = "https://www.nps.gov" + image
            link = (ev.get("infourl") or "").strip() or (
                f"https://www.nps.gov/planyourvisit/event-details.htm?id={ev['id']}"
                if ev.get("id") else "")
            tags = ", ".join(t for t in (ev.get("types") or []) if t)
            lat = ev.get("latitude") or ""
            lng = ev.get("longitude") or ""
            for d in sorted(set(dates)):
                if d in cancelled or not _in_range(d, start, end):
                    continue
                key = (title, d)
                if key in seen:
                    continue
                seen.add(key)
                out.append(norm(
                    name=title, date=d,
                    time_="" if all_day else slots[0][0],
                    end_date="" if all_day or not slots[-1][1] else d,
                    end_time="" if all_day else slots[-1][1],
                    venue=ev.get("parkfullname") or "", address=loc,
                    lat=float(lat) if lat else None, lng=float(lng) if lng else None,
                    description=description, image=image,
                    category=ev.get("category") or "", tags=tags,
                    cost="Free" if _nps_true(ev.get("isfree")) else "",
                    url=link, source=source_name, all_day=all_day))
                if len(out) >= max_events:
                    return out
        try:
            total = int(data.get("total") or 0)
        except (TypeError, ValueError):
            total = 0
        if not items or page * 50 >= total:
            break
    return out
