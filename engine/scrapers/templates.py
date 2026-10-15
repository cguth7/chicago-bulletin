"""Chicago-specific adapters: chicago_parks (Park District aggregator) and do312 (DoStuff)."""
from __future__ import annotations

import datetime as dt
import html
import json
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse, parse_qs

from . import core
from .core import _in_range, host_root, norm, strip_html

# ═══════════════════════════════ CHICAGO ═══════════════════════════════════
# Two Chicago adapters from the original (June 2026) Chicago engine, both
# smoke-tested live again on 2026-10-15. A Scout/Max pass still confirms each
# source that points at them, like any other.
#
#   scrape_chicago_parks  — the AGGREGATOR shape: one listing endpoint fans out to
#       many parks, each event carrying a `park:<slug>` routing tag that
#       ingest.py matches against coverage.routing_key. Two-step listing→node JSON.
#   scrape_do312          — the DoStuff Media platform shape (do312/doNYC/doLA):
#       a human page + `.json` suffix returns clean venue JSON.
# ───────────────────────────────────────────────────────────────────────────
def scrape_chicago_parks(url, source_name, start, end, max_pages=40, max_events=200):
    """Chicago Park District Event Finder — a Drupal site (themes/custom/chiparks).

    AGGREGATOR adapter: one source (chicagoparkdistrict.com/events) fans out to
    every park. The site already explodes recurring programs into one event NODE
    per occurrence (slug like `...-oriole-38`).

    Transport, two steps:
      1. Enumerate the MAP-view listing `/events/map?page=N` (0-indexed). Each
         card carries the event slug AND a canonical park link
         `/parks-facilities/<park-slug>` + the park's display name — that link is
         the per-event -> park mapping that lets coverage fan out.
      2. For each unique event slug, fetch the node as JSON via Drupal's
         `?_format=json`. `field_date` = {value, end_value} ISO-8601 (precise,
         tz-aware); `field_location.url` = the park slug; `title` = event name.

    We pass `venue=<Park Display Name>` and stash the park slug in `tags` (as
    `park:<slug>`) so the aggregator coverage step can match events to the 123
    park venue leads by slug/name.

    `url` is the listing root (https://www.chicagoparkdistrict.com/events). We
    page the map view until a page yields no new event links, capping at
    max_pages. SAFETY CAP: step 2 fetches one node per event, so a runaway listing
    means hundreds of sequential HTTP calls. `max_events` is a HARD ceiling on how
    many events this adapter will ever pull in one call — enumeration stops once it
    is reached and the per-node loop never exceeds it. Bump it deliberately if you
    truly need more, but it exists so a scrape can never run unbounded.
    """
    root = host_root(url)
    map_url = root + "/events/map"

    # ---- step 1: enumerate event slugs + their park from the map-view listing.
    # Each card is a <div class="node--type-event ...">...</div>. Within a card:
    #   <h3 class="event--title"><a href="/events/<slug>" ...>
    #   <a href="/parks-facilities/<park-slug>"><span ...>Park Name</span></a>
    # We split on the card boundary so the park link can only match *its own* card.
    card_split = re.compile(r'node--type-event node--view-mode-card')
    slug_re = re.compile(r'<h3 class="event--title">\s*<a href="(/events/[^"#?]+)"')
    park_re = re.compile(r'<a href="(/parks-facilities/[^"#?]+)"[^>]*>\s*<span[^>]*>([^<]+)</span>')
    events = {}  # slug -> (park_slug, park_name)
    for page in range(max_pages):
        try:
            body = core.fetch(f"{map_url}?page={page}").decode("utf-8", "replace")
        except (HTTPError, URLError) as e:
            print(f"  [{source_name}] map page {page}: {e}", file=sys.stderr)
            break
        new = 0
        for card in card_split.split(body)[1:]:
            sm = slug_re.search(card)
            if not sm:
                continue
            slug = sm.group(1)
            if slug in events:
                continue
            pm = park_re.search(card)
            park_slug = pm.group(1).rsplit("/", 1)[-1] if pm else ""
            park_name = html.unescape(pm.group(2).strip()) if pm else ""
            events[slug] = (park_slug, park_name)
            new += 1
            if len(events) >= max_events:
                break  # hard safety cap — never pull more than max_events
        if len(events) >= max_events:
            print(f"  [{source_name}] hit max_events={max_events} cap at page {page}", file=sys.stderr)
            break
        if new == 0:
            break  # ran past the last populated page
        time.sleep(0.15)

    # ---- step 2: resolve each event node to precise dates + park
    out = []
    for slug, (park_slug, park_name) in list(events.items())[:max_events]:
        node_url = root + slug + "?_format=json"
        try:
            d = core.fetch_json(node_url)
        except (HTTPError, URLError, ValueError) as e:
            print(f"  [{source_name}] node {slug}: {e}", file=sys.stderr)
            continue
        title = (d.get("title") or [{}])[0].get("value", "")
        fdate = (d.get("field_date") or [{}])[0]
        start_iso = fdate.get("value", "")
        end_iso = fdate.get("end_value", "") or start_iso
        if not title or not start_iso:
            continue
        # window filter: keep if the event's date range overlaps [start, end]
        try:
            ev_start = dt.date.fromisoformat(start_iso[:10])
            ev_end = dt.date.fromisoformat(end_iso[:10])
        except ValueError:
            continue
        if ev_end < start or ev_start > end:
            continue
        # clamp displayed start into the window for multi-month ongoing programs
        # so the event surfaces with a date inside the requested window
        disp_start = ev_start if ev_start >= start else start

        # park identity: prefer the node's field_location, fall back to map card
        loc = (d.get("field_location") or [{}])[0]
        loc_url = loc.get("url", "") or ""
        node_park_slug = loc_url.rsplit("/", 1)[-1] if loc_url else park_slug
        ev_park_slug = node_park_slug or park_slug

        addr = ""  # address lives in the rendered card, not needed for mapping
        out.append(norm(
            name=title,
            date=disp_start.isoformat(),
            time_=start_iso[11:16] if len(start_iso) >= 16 else "",
            end_date=ev_end.isoformat() if ev_end != ev_start else "",
            end_time=end_iso[11:16] if len(end_iso) >= 16 else "",
            venue=park_name or ev_park_slug.replace("-", " ").title(),
            address=addr,
            description=strip_html((d.get("field_description") or d.get("body") or [{}])[0].get("value", "")),
            category=", ".join(
                (c.get("name") or "") for c in (d.get("field_event_category") or [])
            ) if d.get("field_event_category") else "",
            cost=str((d.get("field_event_fee") or [{}])[0].get("value", "")) if d.get("field_event_fee") else "",
            organizer="Chicago Park District",
            url=root + slug,
            tags=f"park:{ev_park_slug}" if ev_park_slug else "",
            source=source_name,
        ))
        time.sleep(0.05)
    return out



# ---------------- do312  (DoStuff Media; Do312 aggregator JSON) ----------------

def scrape_do312(venue_url, source_name, start, end, max_pages=10, max_events=200):
    """Do312 (DoStuff Media platform — also DoNYC/DoLA/etc). The human venue page
    `https://do312.com/venues/<slug>` is server-rendered Ember; the same path with a
    `.json` suffix returns the venue's events as clean JSON (no auth).

    venue_url is either the human venue URL or the .json one — we normalize to .json.
    Shape: top-level `event_groups[]`, each `{date, events[]}`. Each event has
    `title`, `begin_date` (YYYY-MM-DD), `begin_time` (ISO w/ tz), `end_date`/`end_time`,
    `permalink`, `category`, `is_free`, `imagery`. Paginated via `?page=N`
    (`paging.total_pages`); page_size 25.

    Aggregator transport, but pointed at one venue's endpoint = effectively a
    single-venue feed. SAFETY CAP: one JSON fetch per page; `max_pages` bounds the
    page walk and `max_events` is a HARD ceiling on rows returned in one call — the
    walk stops once either is hit, so this can never run unbounded.
    """
    base = venue_url.split("?")[0]
    if not base.endswith(".json"):
        base = base.rstrip("/") + ".json"
    out, page = [], 1
    while page <= max_pages and len(out) < max_events:
        url = f"{base}?page={page}"
        try:
            data = core.fetch_json(url, headers={"Accept": "application/json"})
        except (HTTPError, URLError, json.JSONDecodeError) as e:
            print(f"  [{source_name}] do312 page {page}: {e}", file=sys.stderr)
            break
        groups = data.get("event_groups") or []
        any_events = False
        for g in groups:
            for ev in (g.get("events") or []):
                any_events = True
                bd = (ev.get("begin_date") or "")[:10]
                if not _in_range(bd, start, end):
                    continue
                bt = ev.get("begin_time") or ""        # ISO "2026-06-11T21:00:00-05:00"
                et = ev.get("end_time") or ""
                img = (ev.get("imagery") or {}).get("aws") or {}
                image = img.get("cover_image_w_1200_h_450") or img.get("cover_image_h_300_w_864") or ""
                ven = ev.get("venue") or {}
                out.append(norm(
                    name=strip_html(ev.get("title", "")),
                    date=bd,
                    time_=bt[11:16] if len(bt) >= 16 else "",
                    end_date=(ev.get("end_date") or "")[:10] or "",
                    end_time=et[11:16] if len(et) >= 16 else "",
                    venue=ven.get("title", ""),
                    address=ven.get("full_address") or ven.get("address", ""),
                    lat=ven.get("latitude"), lng=ven.get("longitude"),
                    description=strip_html(ev.get("excerpt") or ev.get("description", "")),
                    image=image,
                    category=ev.get("category", "") or "",
                    cost="Free" if ev.get("is_free") else (ev.get("ticket_info") or ""),
                    url=urljoin("https://do312.com", ev.get("permalink", "")),
                    source=source_name,
                ))
                if len(out) >= max_events:
                    break
            if len(out) >= max_events:
                break
        pg = data.get("paging") or {}
        if not any_events or page >= pg.get("total_pages", page):
            break
        page += 1
        time.sleep(0.3)
    return out
