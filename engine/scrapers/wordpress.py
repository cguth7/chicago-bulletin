"""WordPress families: The Events Calendar (tribe_wp) + the two-step WP REST list -> per-event-page JSON-LD shape."""
from __future__ import annotations

import datetime as dt
import html
import json
import re
import sys
import time
from urllib.error import HTTPError, URLError

from . import core
from .core import _in_range, _with_route, host_root, norm, strip_html
from .jsonld import _is_event_type, _jsonld_blocks

# ---------------- tribe_wp  (The Events Calendar / WordPress) ----------------

def scrape_tec(base_url, source_name, start, end, max_pages=10, city_only=None):
    """The Events Calendar REST. base_url = site root (https://wickerpark.org).

    city_only (default OFF — generic output unchanged): drop events whose
    `venue.city` names a different city. Only for REGIONAL tribe feeds where a
    nonprofit lists gigs across a whole metro (The Heart of Jazz: Cherry Hill,
    Princeton, Wilmington, NYC). Blank/absent city passes — we never drop a row
    for missing metadata.
    SAFETY CAP: max_pages * 50 rows is the hard ceiling on one call."""
    root = host_root(base_url) if "/wp-json" not in base_url else base_url.split("/wp-json")[0]
    out, page = [], 1
    while page <= max_pages:
        url = (f"{root}/wp-json/tribe/events/v1/events"
               f"?start_date={start}&end_date={end}&per_page=50&page={page}")
        try:
            data = core.fetch_json(url)
        except HTTPError as e:
            if e.code != 404:
                print(f"  [{source_name}] tec page {page}: {e}", file=sys.stderr)
            break
        except (URLError, json.JSONDecodeError) as e:
            print(f"  [{source_name}] tec page {page}: {e}", file=sys.stderr)
            break
        events = data.get("events", [])
        if not events:
            break
        for ev in events:
            venue = ev.get("venue") or {}
            if isinstance(venue, list):
                venue = venue[0] if venue else {}
            organizer = ev.get("organizer") or {}
            if isinstance(organizer, list):
                organizer = organizer[0] if organizer else {}
            if city_only:
                vcity = strip_html(venue.get("city", "") or "").strip()
                if vcity and vcity.lower() != city_only.lower():
                    continue
            s, e = ev.get("start_date", ""), ev.get("end_date", "")
            img = ev.get("image")
            vname = strip_html(venue.get("venue", ""))
            out.append(norm(
                name=strip_html(ev.get("title", "")),
                date=s[:10], time_=s[11:16] if len(s) >= 16 else "",
                end_date=e[:10], end_time=e[11:16] if len(e) >= 16 else "",
                venue=vname,
                address=strip_html(venue.get("address", "")),
                lat=venue.get("geo_lat"), lng=venue.get("geo_lng"),
                description=strip_html(ev.get("description", "")),
                image=(img or {}).get("url", "") if isinstance(img, dict) else "",
                category=", ".join(c.get("name", "") for c in ev.get("categories", []) if isinstance(c, dict)),
                tags=_with_route(", ".join(t.get("name", "") for t in ev.get("tags", []) if isinstance(t, dict)), vname),
                cost=strip_html(ev.get("cost", "")),
                organizer=strip_html(organizer.get("organizer", "")),
                url=ev.get("url", ""), source=source_name, all_day=ev.get("all_day", False),
            ))
        if data.get("next_rest_url"):
            page += 1
        else:
            break
    return out


# ---------------- wp_jsonld_pages  (WP REST CPT list -> per-event-page JSON-LD) --

# EventON's data-time="<epoch>-<epoch>" pair on the single-event page. VERIFIED
# (queerbooks.com, 2026-08-17, 3 pages against the server-rendered card): the
# epochs are the venue WALL CLOCK encoded as if it were UTC — read the UTC
# fields directly and NEVER localize (1786644000 renders "6:00 pm" on the card;
# localized to ET it would read 14:00). The same pages' JSON-LD startDate reads
# a full HOUR later than the card (19:00 vs 18:00), so the epoch pair is the
# primary date source and JSON-LD dates are only the fallback.
_EVO_DATA_TIME = re.compile(r'data-time="(\d{9,12})-(\d{9,12})"')

# EventON JSON-LD dates are NON-zero-padded ("2026-8-13T19:00-4:00"), which
# date.fromisoformat rejects, and the trailing "-4:00" pseudo-offset is
# malformed ISO anyway. Take the wall-clock fields leniently, ignore the rest.
_LOOSE_ISO = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[T ](\d{1,2}):(\d{2}))?")


def _loose_when(s):
    """Lenient '(Y)-(M)-(D)[T(H):(MM)]…' -> 'YYYY-MM-DD[THH:MM]' or ''."""
    m = _LOOSE_ISO.match(s.strip()) if isinstance(s, str) else None
    if not m:
        return ""
    try:
        day = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return ""
    if m.group(4) is None:
        return day.isoformat()
    return f"{day.isoformat()}T{int(m.group(4)):02d}:{m.group(5)}"


def _epoch_wall(ts):
    """EventON epoch -> 'YYYY-MM-DDTHH:MM' (the UTC fields ARE the wall clock)."""
    try:
        return (dt.datetime.fromtimestamp(int(ts), dt.timezone.utc)
                .strftime("%Y-%m-%dT%H:%M"))
    except (ValueError, OSError, OverflowError):
        return ""


def scrape_wp_jsonld_pages(list_url, source_name, start, end, max_events=20):
    """Two-step WP scraper for sites whose REST list carries NO event dates:
    step 1 GET a WP REST post-collection (`list_url`, the full endpoint on the
    source row, e.g. …/wp-json/wp/v2/ajde_events?per_page=20) for `link` +
    `title.rendered`; step 2 fetch each single-event page and read its
    @type:Event JSON-LD + EventON's data-time epoch pair. Generalizes to any
    WP CPT whose single pages carry Event JSON-LD (EventON lite is the first
    user: Giovanni's Room, Philly source "giovannis-room"). The /events/
    archive page has NO JSON-LD — only single pages do — hence the REST list.

    Dates: prefer data-time (wall-clock-as-UTC, see _EVO_DATA_TIME above) over
    the JSON-LD's non-padded, hour-skewed startDate (_loose_when fallback).
    Name/description/image/url come from the JSON-LD, with the REST title as
    the name fallback when a page's JSON-LD is missing or malformed.

    SAFETY CAP: every REST item costs one page fetch, so max_events is a HARD
    ceiling on pages pulled per call (default 20 — the list is publish-date
    ordered, newest first, which is also roughly event-date ordered here)."""
    try:
        posts = core.fetch_json(list_url)
    except (HTTPError, URLError, json.JSONDecodeError) as e:
        print(f"  [{source_name}] wp_jsonld_pages list: {e}", file=sys.stderr)
        return []
    if not isinstance(posts, list):
        print(f"  [{source_name}] wp_jsonld_pages: list is not an array",
              file=sys.stderr)
        return []
    out = []
    for post in posts[:max_events]:
        if not isinstance(post, dict):
            continue
        link = post.get("link") or ""
        if not link:
            continue
        try:
            body = core.fetch(link).decode("utf-8", "replace")
        except (HTTPError, URLError):
            continue
        time.sleep(0.2)
        ld = next((ev for ev in _jsonld_blocks(body)
                   if _is_event_type(ev.get("@type"))), None)
        m = _EVO_DATA_TIME.search(body)
        if m:
            s_iso, e_iso = _epoch_wall(m.group(1)), _epoch_wall(m.group(2))
        elif ld:
            s_iso, e_iso = _loose_when(ld.get("startDate")), _loose_when(ld.get("endDate"))
        else:
            continue
        if not _in_range(s_iso[:10], start, end):
            continue
        rest_title = strip_html((post.get("title") or {}).get("rendered", ""))
        ld = ld or {}
        name = strip_html(ld.get("name", "")) or rest_title
        if not name:
            continue
        img = ld.get("image")
        if isinstance(img, list):
            img = img[0] if img else ""
        if isinstance(img, dict):
            img = img.get("url", "")
        out.append(norm(
            name=name,
            date=s_iso[:10], time_=s_iso[11:16] if len(s_iso) >= 16 else "",
            end_date=e_iso[:10], end_time=e_iso[11:16] if len(e_iso) >= 16 else "",
            description=strip_html(ld.get("description", "")),
            image=img if isinstance(img, str) else "",
            url=ld.get("url") or link,
            source=source_name,
        ))
    return out


# ---------------- craftpeak  (Craftpeak brewery WP theme) ----------------
#
# 2026-09-06 (Max): Craftpeak is a WordPress platform for craft breweries
# (images on craftpeak-cooler-images.imgix.net give it away). It ships an
# `event` custom post type rendered with Beaver Builder, and it is DELIBERATELY
# opaque to the usual tricks:
#   - The Events Calendar REST -> rest_no_route (plugin absent)
#   - wp-json/wp/v2 exposes post/page/attachment only (event CPT not REST-public)
#   - `?ical=1` on the listing returns the HTML page, not an .ics
#   - the event detail page's JSON-LD @graph has WebSite + WebPage nodes ONLY,
#     no Event node
# So this is a two-step HTML scrape: the taxonomy listing gives us event URLs,
# and each URL's SLUG ENDS IN THE FULL DATE (/event/philly-oktoberfest-2026-09-19/).
# That lets us window-filter from the listing alone and fan out to detail pages
# ONLY for events actually in range — the times and the body live there.
#
# Point it at any Craftpeak taxonomy or event archive; Other Half Brewing's
# Philadelphia filter (/event-type/phl/) was the first source. Multi-location
# breweries use an `event_type` term per taproom, so the LISTING URL is what
# scopes a source to one city — do not "helpfully" widen it to /event/.
#
# Listing pages are sorted NEWEST-FIRST: page 1 carries every upcoming event and
# page 2+ are archives. Hence max_pages=1 by default, and we stop early on the
# first page that yields no in-window dates.

_CP_EVENT_HREF = re.compile(
    r'href="(https?://[^"]+/event/([a-z0-9-]*?)-(\d{4}-\d{2}-\d{2}))/?"', re.I)
# The Beaver Builder icon-text module renders the whole when-string into a
# title= attribute: title='September 19 2026 12:00 pm - 9:00 pm'
_CP_WHEN = re.compile(
    r"""title=['"]([A-Z][a-z]+ \d{1,2},? \d{4}[^'"]*)['"]""")
_CP_HEADING = re.compile(r'<span class="fl-heading-text">(.*?)</span>', re.S)
_CP_OG = re.compile(r'<meta property="og:(image|title)" content="([^"]*)"', re.I)
_CP_CLOCK = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?", re.I)


def _cp_clock(s):
    """'12:00 pm' / '9 PM' -> 'HH:MM' (24h). '' when unparseable."""
    m = _CP_CLOCK.search(s or "")
    if not m:
        return ""
    h = int(m.group(1)) % 12
    if m.group(3).lower() == "p":
        h += 12
    return f"{h:02d}:{int(m.group(2) or 0):02d}"


def _cp_when(body):
    """Pull (start_time, end_time) out of the detail page's when-string.
    'September 19 2026 12:00 pm - 9:00 pm' -> ('12:00', '21:00'). The DATE is
    taken from the url slug (authoritative), not from here."""
    for m in _CP_WHEN.finditer(body):
        txt = html.unescape(m.group(1))
        if not _CP_CLOCK.search(txt):
            continue
        halves = re.split(r"\s[-–—]\s", txt, maxsplit=1)
        t0 = _cp_clock(halves[0])
        t1 = _cp_clock(halves[1]) if len(halves) > 1 else ""
        if t0:
            return t0, t1
    return "", ""


def _cp_body(body):
    """The event's prose. Craftpeak renders it into a `content-builder` module
    whose inner divs nest arbitrarily, so a non-greedy `<div>(.*?)</div>` match
    silently returns "" (it did — that's why this is a slice, not a regex). We
    take everything from the module's opening class attribute up to the NEXT
    Beaver Builder module and let strip_html do the rest."""
    i = body.find('class="content-builder')
    if i < 0:
        return ""
    i = body.find(">", i)          # step past the tag we landed INSIDE, or the
    if i < 0:                      # attribute text leaks into the description
        return ""
    j = body.find('class="fl-module ', i)
    if j > i:
        j = body.rfind("<", i, j)      # and cut back to that tag's OPENING '<',
    if j <= i:                         # else a dangling '<div' survives stripping
        j = len(body)
    return strip_html(body[i + 1:j]).strip()


def _cp_detail(url, source_name):
    """Fetch one event page -> (title, start, end, description, image)."""
    try:
        body = core.fetch(url).decode("utf-8", "replace")
    except (HTTPError, URLError) as e:
        print(f"  [{source_name}] craftpeak detail {url}: {e}", file=sys.stderr)
        return None
    og = {k.lower(): v for k, v in _CP_OG.findall(body)}
    title = ""
    h = _CP_HEADING.search(body)
    if h:
        title = strip_html(h.group(1)).strip()
    if not title:
        title = html.unescape(og.get("title", "")).split("|")[0].strip()
    t0, t1 = _cp_when(body)
    return (title, t0, t1, _cp_body(body),
            html.unescape(og.get("image", "")))


def scrape_craftpeak(listing_url, source_name, start, end,
                     max_pages=1, max_events=60):
    """Craftpeak brewery events: taxonomy listing -> per-event detail pages.

    listing_url is the archive/taxonomy page, e.g.
    https://otherhalfbrewing.com/event-type/phl/ .

    SAFETY CAP: this is a fan-out adapter (1 listing fetch + 1 fetch PER
    in-window event), so both ends are bounded — max_pages listing fetches and
    max_events detail fetches. Because only in-window slugs are fanned out, a
    normal run is ~10 requests, not hundreds."""
    seen, want = set(), []
    for page in range(1, max_pages + 1):
        url = listing_url if page == 1 else (
            listing_url.rstrip("/") + f"/page/{page}/")
        try:
            body = core.fetch(url).decode("utf-8", "replace")
        except (HTTPError, URLError) as e:
            print(f"  [{source_name}] craftpeak p{page}: {e}", file=sys.stderr)
            break
        found = 0
        for href, _slug_txt, date in _CP_EVENT_HREF.findall(body):
            if href in seen:
                continue
            seen.add(href)
            if not _in_range(date, start, end):
                continue
            found += 1
            want.append((href, date))
        if not found:
            break                       # archive page: nothing in window left
    want.sort(key=lambda t: t[1])

    out = []
    for href, date in want[:max_events]:
        got = _cp_detail(href, source_name)
        if not got:
            continue
        title, t0, t1, desc, img = got
        if not title:
            continue
        out.append(norm(
            name=title, date=date, time_=t0, end_time=t1,
            description=desc, image=img, url=href, source=source_name,
        ))
    return out
