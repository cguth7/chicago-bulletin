"""Eventbrite: city discovery blob + organizer-profile variants."""
from __future__ import annotations

import json
import re
import sys
import time
from urllib.error import HTTPError, URLError

from . import core
from .core import _in_range, norm, route_tag, strip_html

# ---------------- eventbrite  (city discovery page __SERVER_DATA__ blob) --------

EB_BLOB = re.compile(r"window\.__SERVER_DATA__\s*=\s*(\{.*?\});", re.S)


def scrape_eventbrite(discovery_url, source_name, start, end, max_pages=25,
                      max_events=600):
    """Eventbrite city discovery page. Not an API: the page server-renders a
    `window.__SERVER_DATA__` JS blob whose `search_data.events.results[]` is the
    search result set. Ported from Philly v1 (library/scrapers.py:scrape_eventbrite),
    confirmed unchanged 2026-08-16.

    discovery_url is the city page (https://www.eventbrite.com/d/pa--philadelphia/
    all-events/); we append the server-side window (start_date/end_date — verified
    respected) plus page=N. Online-only events are dropped: this engine is about
    leaving the house.

    SAFETY CAP: the unfiltered feed reports 49 pages / 10k objects. max_pages bounds
    the walk and max_events is a HARD ceiling on rows returned, so this can never run
    unbounded (v1 used max_pages=50; 25 x 20/page is plenty for a 90-day window).
    """
    base = discovery_url.split("?")[0]
    out, page = [], 1
    while page <= max_pages and len(out) < max_events:
        url = (f"{base}?page={page}&start_date={start.isoformat()}"
               f"&end_date={end.isoformat()}")
        try:
            body = core.fetch(url).decode("utf-8", "replace")
        except (HTTPError, URLError) as e:
            print(f"  [{source_name}] eventbrite page {page}: {e}", file=sys.stderr)
            break
        m = EB_BLOB.search(body)
        if not m:
            print(f"  [{source_name}] eventbrite page {page}: no __SERVER_DATA__ blob",
                  file=sys.stderr)
            break
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError as e:
            print(f"  [{source_name}] eventbrite page {page}: {e}", file=sys.stderr)
            break
        ev = ((data.get("search_data") or {}).get("events") or {})
        results = ev.get("results") or []
        if not results:
            break
        for r in results:
            if r.get("is_online_event") or r.get("is_cancelled"):
                continue
            sd = (r.get("start_date") or "")[:10]
            if not _in_range(sd, start, end):
                continue
            v = r.get("primary_venue") or {}
            addr = v.get("address") or {}
            lat, lng = addr.get("latitude"), addr.get("longitude")
            vname = v.get("name", "") or ""
            cats = [t.get("display_name", "") for t in (r.get("tags") or [])
                    if isinstance(t, dict) and t.get("prefix") == "EventbriteCategory"]
            out.append(norm(
                name=strip_html(r.get("name", "")),
                date=sd, time_=(r.get("start_time") or "")[:5],
                end_date=(r.get("end_date") or "")[:10],
                end_time=(r.get("end_time") or "")[:5],
                venue=vname,
                address=addr.get("localized_address_display", "") or "",
                lat=float(lat) if lat else None, lng=float(lng) if lng else None,
                description=strip_html(r.get("summary") or ""),
                image=((r.get("image") or {}).get("url", "")
                       if isinstance(r.get("image"), dict) else ""),
                category=", ".join(c for c in cats if c),
                tags=route_tag(vname),
                # search results carry only primary_organizer_id, never the name;
                # tickets_by is the platform ("eventbrite"), not the organizer.
                organizer="",
                url=(r.get("url") or "").split("?")[0],
                source=source_name,
            ))
            if len(out) >= max_events:
                break
        if page >= (ev.get("pagination") or {}).get("page_count", page):
            break
        page += 1
        time.sleep(0.4)
    return out


# ---------------- eventbrite_organizer  (organizer profile `upcomingEvents`) ----

_EB_ORG_KEY = '"upcomingEvents":'


def _eb_json_array(body, key=_EB_ORG_KEY):
    """Pull the bracket-balanced JSON array that follows `key` in a page's HTML.
    Organizer pages inline the Next.js props as one enormous JSON string, so a
    non-greedy regex can't find the array's end — we balance brackets instead
    (string-aware, so a `]` inside an event title can't close it early)."""
    i = body.find(key)
    if i < 0:
        return None
    s = body.find("[", i)
    if s < 0:
        return None
    depth, in_str, esc = 0, False, False
    for j in range(s, len(body)):
        ch = body[j]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
            if depth == 0:
                return body[s:j + 1]
    return None


def scrape_eventbrite_organizer(organizer_url, source_name, start, end,
                                max_events=150, city_only=None):
    """Eventbrite ORGANIZER profile page (https://www.eventbrite.com/o/<slug>-<id>).

    Not the city-discovery page the `eventbrite` adapter walks: an organizer page
    server-renders its Next.js props with an `"upcomingEvents":[...]` array whose
    objects are the SAME event shape as the discovery blob's
    `search_data.events.results[]` (name/url/start_date/start_time/primary_venue
    /image/ticket_availability). Confirmed live 2026-08-23 on henn-house-events
    and honeytree-evileye.

    ONE fetch, no pagination: the page carries the organizer's whole upcoming list
    (`upcomingEventsTotal`). max_events is still a HARD ceiling so a prolific
    organizer can never fan out unbounded. Online-only and cancelled events are
    dropped (this engine is about leaving the house).

    city_only (default OFF — output unchanged unless asked): drop events whose
    primary_venue city != the given city, for producers who also book the suburbs.
    """
    try:
        body = core.fetch(organizer_url).decode("utf-8", "replace")
    except (HTTPError, URLError) as e:
        print(f"  [{source_name}] eventbrite organizer: {e}", file=sys.stderr)
        return []
    blob = _eb_json_array(body)
    if blob is None:
        print(f"  [{source_name}] eventbrite organizer: no upcomingEvents array",
              file=sys.stderr)
        return []
    try:
        results = json.loads(blob)
    except json.JSONDecodeError as e:
        print(f"  [{source_name}] eventbrite organizer: {e}", file=sys.stderr)
        return []
    out = []
    for r in results:
        if not isinstance(r, dict):
            continue
        if r.get("is_online_event") or r.get("is_cancelled"):
            continue
        sd = (r.get("start_date") or "")[:10]
        if not _in_range(sd, start, end):
            continue
        v = r.get("primary_venue") or {}
        addr = v.get("address") or {}
        if city_only and (addr.get("city") or "").lower() != city_only.lower():
            continue
        lat, lng = addr.get("latitude"), addr.get("longitude")
        vname = v.get("name", "") or ""
        ta = r.get("ticket_availability") or {}
        if ta.get("is_free"):
            cost = "Free"
        else:
            cost = ((ta.get("minimum_ticket_price") or {}).get("display", "")
                    if isinstance(ta.get("minimum_ticket_price"), dict) else "")
        out.append(norm(
            name=strip_html(r.get("name", "")),
            date=sd, time_=(r.get("start_time") or "")[:5],
            end_date=(r.get("end_date") or "")[:10],
            end_time=(r.get("end_time") or "")[:5],
            venue=vname,
            address=addr.get("localized_address_display", "") or "",
            lat=float(lat) if lat else None, lng=float(lng) if lng else None,
            description=strip_html(r.get("summary") or ""),
            image=((r.get("image") or {}).get("url", "")
                   if isinstance(r.get("image"), dict) else ""),
            cost=cost,
            tags=route_tag(vname),
            # tickets_by is the platform ("eventbrite"), not the organizer; the
            # profile's group is already linked via source_coverage.
            organizer="",
            url=(r.get("url") or "").split("?")[0],
            source=source_name,
        ))
        if len(out) >= max_events:
            break
    return out


