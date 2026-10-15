"""JSON-LD listing families: html_jsonld, html_jsonld_paged, playdate."""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse, parse_qs

from . import core
from .core import LOCAL_TZ, host_root, norm, route_tag, strip_html

# ---------------- html_jsonld  (listing page -> per-event JSON-LD) ----------------

def _jsonld_blocks(body):
    """Yield parsed JSON-LD objects from a page body, flattening @graph and lists."""
    for m in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        body, re.I | re.S,
    ):
        try:
            obj = json.loads(m.group(1).strip())
        except Exception:
            continue
        items = obj.get("@graph", [obj]) if isinstance(obj, dict) else obj
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list):
            continue
        for i in items:
            if not isinstance(i, dict):
                continue
            # An ItemList's own @type is 'ItemList', which _is_event_type rejects —
            # so a season published as one ItemList of ListItems (PCMS) used to read
            # as zero events. Descend one level to the real Event objects.
            if i.get("@type") == "ItemList":
                for li in (i.get("itemListElement") or []):
                    inner = li.get("item") if isinstance(li, dict) else None
                    if isinstance(inner, dict):
                        yield inner
                continue
            yield i


_HUMAN_DATE = re.compile(
    r"([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})"                     # October 16, 2026
    r"(?:\s+at\s+(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?)?", re.I)   # At 7:30 pm


def _jsonld_localize(c):
    """An ISO string carrying an explicit UTC/offset marker → naive local
    wall-clock 'YYYY-MM-DDTHH:MM'. Naive strings (already venue wall-clock, the
    overwhelming majority) pass through untouched, as do offset strings that are
    already Eastern (astimezone is then a no-op). Added 2026-08-16 for playdate,
    whose JSON-LD emits +00:00 UTC — an 8pm ET show used to land on the WRONG
    next-day date once sliced."""
    try:
        d = dt.datetime.fromisoformat(c.replace("Z", "+00:00"))
    except ValueError:
        return c
    if d.tzinfo is None:
        return c
    return d.astimezone(LOCAL_TZ).strftime("%Y-%m-%dT%H:%M")


def _jsonld_when(c):
    """Normalize a JSON-LD startDate to 'YYYY-MM-DD[THH:MM]' in LOCAL wall-clock;
    '' if unparseable.

    Almost every site emits ISO-8601 and passes through untouched (offset-bearing
    strings are localized via _jsonld_localize). PCMS emits a human string
    ("October 16, 2026 At 7:30 pm", inconsistent am/pm casing), which the strict
    `date.fromisoformat` used to drop silently. Tolerant only on the fallback
    path, so no ISO source changes behavior."""
    if not isinstance(c, str):
        return ""
    try:
        dt.date.fromisoformat(c[:10])
        return _jsonld_localize(c)
    except ValueError:
        pass
    m = _HUMAN_DATE.search(c)
    if not m:
        return ""
    try:
        d = dt.datetime.strptime(f"{m.group(1)[:3]} {m.group(2)} {m.group(3)}",
                                 "%b %d %Y").date()
    except ValueError:
        return ""
    if not m.group(4):
        return d.isoformat()
    hh = int(m.group(4)) % 12 + (12 if m.group(6).lower() == "p" else 0)
    return f"{d.isoformat()}T{hh:02d}:{m.group(5) or '00'}"


def _is_event_type(t):
    if isinstance(t, list):
        return any(_is_event_type(x) for x in t)
    return isinstance(t, str) and "event" in t.lower()


def scrape_html_jsonld(listing_url, source_name, start, end, max_events=200,
                       link_re=r'/(?:events?|shows?|event)/[a-z0-9][a-z0-9\-/]*'):
    """Generic two-step JSON-LD scraper for venues that publish each event on its
    own detail page with <script type="application/ld+json"> @type:Event (incl.
    MusicEvent/TheaterEvent). Step 1: fetch the listing page, collect detail links
    matching link_re. Step 2: fetch each, parse its Event JSON-LD. Used by custom
    static sites (Cole's Bar / Astro) and Mato venue pages.

    Falls back to events found inline on the listing page itself (ItemList/graph).
    """
    root = host_root(listing_url)
    try:
        body = core.fetch(listing_url).decode("utf-8", "replace")
    except (HTTPError, URLError) as e:
        print(f"  [{source_name}] jsonld listing: {e}", file=sys.stderr)
        return []

    # collect candidate detail-page links
    links = []
    for href in re.findall(r'href=["\']([^"\']+)["\']', body):
        if re.search(link_re, href, re.I):
            full = urljoin(root, href.split("#")[0].split("?")[0])
            if full not in links and full.rstrip("/") != listing_url.rstrip("/"):
                links.append(full)
    links = links[:max_events]

    def _emit(ev, page_url):
        if not _is_event_type(ev.get("@type")):
            return None
        sd = ev.get("startDate")
        # startDate may be a list (recurring); take the soonest in-window
        cands = sd if isinstance(sd, list) else [sd]
        best = None
        for c in cands:
            c = _jsonld_when(c)
            if not c:
                continue
            try:
                d = dt.date.fromisoformat(c[:10])
            except Exception:
                continue
            if start <= d <= end and (best is None or c < best):
                best = c
        if not best:
            return None
        ed = _jsonld_when(ev.get("endDate")) if isinstance(ev.get("endDate"), str) else ""
        loc = ev.get("location") or {}
        if isinstance(loc, list):
            loc = loc[0] if loc else {}
        addr = loc.get("address") if isinstance(loc, dict) else ""
        if isinstance(addr, dict):
            addr = ", ".join(filter(None, [addr.get("streetAddress"), addr.get("addressLocality")]))
        geo = loc.get("geo") if isinstance(loc, dict) else None
        lat = lng = None
        if isinstance(geo, dict):
            try:
                lat, lng = float(geo.get("latitude")), float(geo.get("longitude"))
            except (TypeError, ValueError):
                lat = lng = None
        img = ev.get("image")
        if isinstance(img, list):
            img = img[0] if img else ""
        if isinstance(img, dict):
            img = img.get("url", "")
        # strip_html here matters beyond cosmetics: route_tag slugs this name, so
        # an undecoded entity ("Johnny Brenda&#8217;s") would mint the routing key
        # park:johnny-brenda-8217-s and never match a coverage row (r5, 2026-08-16).
        vname = strip_html(loc.get("name", "")) if isinstance(loc, dict) else ""
        return norm(
            name=strip_html(ev.get("name", "")),
            date=best[:10], time_=best[11:16] if len(best) >= 16 else "",
            all_day=len(best) < 16,
            end_date=ed[:10], end_time=ed[11:16] if len(ed) >= 16 else "",
            venue=vname,
            tags=route_tag(vname),
            address=addr or "",
            lat=lat, lng=lng,
            description=strip_html(ev.get("description", "")),
            image=img or "",
            url=ev.get("url") or page_url,
            source=source_name,
        )

    out, seen = [], set()
    # inline events on the listing page (some sites embed full Event objects)
    for ev in _jsonld_blocks(body):
        row = _emit(ev, listing_url)
        if row and row["name"] not in seen:
            seen.add(row["name"]); out.append(row)

    # per-detail-page events
    for link in links:
        try:
            pb = core.fetch(link).decode("utf-8", "replace")
        except (HTTPError, URLError):
            continue
        for ev in _jsonld_blocks(pb):
            row = _emit(ev, link)
            if row and row["name"] not in seen:
                seen.add(row["name"]); out.append(row)
        time.sleep(0.2)
    return out


# ---------------- playdate  (playdate.events aggregator, paginated JSON-LD) -----


# ---------------- html_jsonld_paged  (inline-JSON-LD listing + &page=N) ---------

def scrape_html_jsonld_paged(listing_url, source_name, start, end, max_pages=10,
                             max_events=200):
    """Paged variant of scrape_html_jsonld for listings that embed full
    @type:Event JSON-LD blocks INLINE and paginate with a `page=N` query param
    that composes with the listing's existing query string (DesignPhiladelphia
    festival first: Philly source "designphiladelphia", ~20 events/page).

    Delegates each page to scrape_html_jsonld with a never-matching link_re so
    ONLY the inline blocks are read — one fetch per page, no per-event detail
    fan-out. Stops at the first page that adds nothing new in-window.

    SAFETY CAP: max_pages fetches per call, max_events hard ceiling."""
    sep = "&" if "?" in listing_url else "?"
    out, seen = [], set()
    for n in range(1, max_pages + 1):
        url = listing_url if n == 1 else f"{listing_url}{sep}page={n}"
        rows = scrape_html_jsonld(url, source_name, start, end,
                                  max_events=max_events, link_re=r"(?!x)x")
        added = 0
        for r in rows:
            key = (r["name"], r["date"])
            if key in seen:
                continue
            seen.add(key)
            out.append(r)
            added += 1
        if added == 0 or len(out) >= max_events:
            break
        time.sleep(0.2)
    return out[:max_events]


# ---------------- bentobox  (BentoBox restaurant CMS event pages) -------------

_BB_EVENT_HREF = re.compile(r'href=["\']([^"\']*/(?:store/)?event/[^"\']*)["\']', re.I)


def scrape_bentobox(listing_url, source_name, start, end, max_events=60):
    """BentoBox CMS (getbento.com) — the restaurant-site platform behind Bistro
    Romano and a long tail of independent restaurants (2026-09-06, Max).

    Shape: an events index at `/store/events/` (or the sitemap) linking to
    `/store/event/<slug>/` or `/event/<slug>/` detail pages, each carrying a
    proper `@type: Event` JSON-LD block.

    WHY NOT `html_jsonld`: BentoBox stamps a **fake Z** on startDate —
    `"startDate": "2026-09-30T17:30:00Z"` is the 5:30 PM local seating, not
    17:30 UTC. The generic adapter's `_jsonld_localize` faithfully converts that
    to 13:30 and every event lands four hours early. Here the timezone marker is
    DISCARDED and the wall-clock is taken literally, which is what the CMS means.

    `location.name` is the useless boilerplate "Event Location", so venue is left
    empty for ingest's dedicated pass to fill from coverage — never routed.

    SAFETY CAP: one fetch for the listing + at most max_events detail fetches."""
    root = host_root(listing_url)
    try:
        body = core.fetch(listing_url).decode("utf-8", "replace")
    except (HTTPError, URLError) as e:
        print(f"  [{source_name}] bentobox listing: {e}", file=sys.stderr)
        return []

    links = []
    for href in _BB_EVENT_HREF.findall(body):
        full = urljoin(root, href.split("#")[0].split("?")[0])
        full = re.sub(r"/{2,}$", "/", full)          # sitemap emits trailing '//'
        if full not in links and full.rstrip("/") != listing_url.rstrip("/"):
            links.append(full)
    links = links[:max_events]

    out, seen = [], set()
    for page in links:
        try:
            det = core.fetch(page).decode("utf-8", "replace")
        except (HTTPError, URLError) as e:
            print(f"  [{source_name}] bentobox {page}: {e}", file=sys.stderr)
            continue
        for ev in _jsonld_blocks(det):
            if not _is_event_type(ev.get("@type")):
                continue
            sd = ev.get("startDate") or ""
            if not isinstance(sd, str) or len(sd) < 10:
                continue
            sd = sd.replace("Z", "")                # the fake-Z: keep wall-clock
            sd = re.sub(r"[+-]\d{2}:\d{2}$", "", sd)
            try:
                d = dt.date.fromisoformat(sd[:10])
            except ValueError:
                continue
            if not (start <= d <= end):
                continue
            ed = ev.get("endDate") or ""
            if isinstance(ed, str):
                ed = re.sub(r"[+-]\d{2}:\d{2}$", "", ed.replace("Z", ""))
            else:
                ed = ""
            key = (ev.get("name", ""), sd[:16])
            if key in seen:
                continue
            seen.add(key)
            loc = ev.get("location") or {}
            if isinstance(loc, list):
                loc = loc[0] if loc else {}
            addr = loc.get("address") if isinstance(loc, dict) else ""
            if isinstance(addr, dict):
                addr = ", ".join(filter(None, [addr.get("streetAddress"),
                                               addr.get("addressLocality")]))
            elif not isinstance(addr, str):
                addr = ""
            if not addr and isinstance(loc, dict) and loc.get("streetAddress"):
                addr = ", ".join(filter(None, [loc.get("streetAddress"),
                                               loc.get("addressLocality")]))
            img = ev.get("image")
            if isinstance(img, list):
                img = img[0] if img else ""
            if isinstance(img, dict):
                img = img.get("url", "")
            out.append(norm(
                name=strip_html(ev.get("name", "")),
                date=sd[:10],
                time_=sd[11:16] if len(sd) >= 16 else "",
                all_day=len(sd) < 16,
                end_date=ed[:10], end_time=ed[11:16] if len(ed) >= 16 else "",
                venue="",                       # "Event Location" is boilerplate
                address=addr or "",
                description=strip_html(ev.get("description", "")),
                image=img or "",
                url=ev.get("url") or page,
                source=source_name,
            ))
            if len(out) >= max_events:
                return sorted(out, key=lambda r: (r["date"], r["time"]))
        time.sleep(0.15)
    return sorted(out, key=lambda r: (r["date"], r["time"]))
