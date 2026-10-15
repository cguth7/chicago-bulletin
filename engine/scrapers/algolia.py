"""
Algolia-backed event indexes.

Family shape (2026-09-06, Max — Museum of the American Revolution first): a
site renders its events list client-side out of an Algolia index. The public
search credentials are compiled into the site's JS bundle, so the index is a
plain, unauthenticated JSON API once you have them — no headless browser, no
prose-date parsing, no per-event detail fetch.

The whole configuration rides the SOURCE URL (keys live in the url, never in
code — the Ticketmaster rule):

  https://<APPID>-dsn.algolia.net/1/indexes/<index>/query
      ?x-algolia-application-id=<APPID>
      &x-algolia-api-key=<search-only key>
      &site=https://www.example.org           # root for relative `route` links
      &facetFilters=location_filter:Museum of the American Revolution
      &filters=date_end_timestamp >= 0        # optional raw Algolia filter

`facetFilters` may repeat; each occurrence is ANDed (Algolia's nested-list
form). Everything else in the query string is ignored.
"""
from __future__ import annotations

import datetime as dt
import sys
from urllib.parse import urljoin, urlparse, parse_qsl, urlencode
from urllib.error import HTTPError, URLError

from . import core
from .core import norm, strip_html

# Attribute-name candidates, most-specific first. Algolia indexes are
# hand-built per site, so tolerate the handful of conventional spellings
# rather than hard-coding one CMS's schema.
_F_NAME = ("title", "name", "title_stripped", "heading")
_F_START = ("date_start", "start_date", "startDate", "start", "date")
_F_END = ("date_end", "end_date", "endDate", "end")
_F_DESC = ("description", "summary", "excerpt", "content")
_F_URL = ("route", "url", "permalink", "link", "path")
_F_VENUE = ("location_label", "venue", "location", "venue_name")
_F_CAT = ("event_type", "category", "type")


def _pick(hit, keys):
    for k in keys:
        v = hit.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _alg_dt(raw):
    """'2026-09-05 09:30:00' | ISO-8601 (with T, optional Z/offset) -> naive
    local datetime. These indexes store WALL-CLOCK local time already (verified
    against the rendered cards), so an offset/Z suffix is dropped, not shifted."""
    s = (raw or "").strip()
    if not s:
        return None
    s = s.replace("Z", "").replace("T", " ")
    s = s.split("+")[0].strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def scrape_algolia_events(query_url, source_name, start, end,
                          max_events=200, max_pages=3, hits_per_page=100):
    """Query an Algolia events index (config + credentials in `query_url`, see
    module docstring) and normalize its hits.

    SAFETY CAP: at most `max_pages` POSTs of `hits_per_page` hits and
    `max_events` rows returned — an Algolia index can hold thousands of past
    events, and the endpoint has no natural stop. Pagination also short-circuits
    on `nbPages`.

    Multi-day hits (date_start 09-05, date_end 09-07) are emitted ONCE with
    end_date set — the run, not one row per day. A hit is kept when its span
    OVERLAPS [start, end]."""
    parts = urlparse(query_url)
    q = parse_qsl(parts.query, keep_blank_values=False)
    headers, facets, filters, site = {}, [], "", ""
    for k, v in q:
        kl = k.lower()
        if kl in ("x-algolia-application-id", "x-algolia-api-key"):
            headers[k] = v
        elif kl == "facetfilters":
            facets.append([v])
        elif kl == "filters":
            filters = v
        elif kl == "site":
            site = v
    if len(headers) < 2:
        print(f"  [{source_name}] algolia: url is missing x-algolia-* credentials",
              file=sys.stderr)
        return []
    endpoint = f"{parts.scheme}://{parts.netloc}{parts.path}"
    site = site or f"{parts.scheme}://{parts.netloc}"

    hits, page = [], 0
    while page < max_pages and len(hits) < max_events:
        body = {"query": "", "hitsPerPage": hits_per_page, "page": page,
                "attributesToHighlight": [], "attributesToSnippet": []}
        if facets:
            body["facetFilters"] = facets
        if filters:
            body["filters"] = filters
        try:
            data = core.post_json(endpoint, body, headers=headers)
        except (HTTPError, URLError, ValueError) as e:
            print(f"  [{source_name}] algolia page {page}: {e}", file=sys.stderr)
            break
        batch = data.get("hits") or []
        hits.extend(batch)
        if page + 1 >= (data.get("nbPages") or 0) or not batch:
            break
        page += 1
    if page + 1 >= max_pages:
        print(f"  [{source_name}] algolia: stopped at max_pages={max_pages}",
              file=sys.stderr)

    out = []
    for hit in hits[:max_events]:
        name = strip_html(_pick(hit, _F_NAME))
        s_dt = _alg_dt(_pick(hit, _F_START))
        if not (name and s_dt):
            continue
        e_dt = _alg_dt(_pick(hit, _F_END))
        s_d = s_dt.date()
        e_d = e_dt.date() if e_dt else s_d
        if e_d < s_d:
            e_d, e_dt = s_d, None
        if e_d < start or s_d > end:          # span must overlap the window
            continue
        multi_day = e_d != s_d
        t1 = "" if (s_dt.hour == 0 and s_dt.minute == 0) else s_dt.strftime("%H:%M")
        t2 = e_dt.strftime("%H:%M") if (e_dt and not multi_day and t1) else ""
        img = hit.get("image")
        if isinstance(img, dict):
            img = img.get("img") or ""
        link = _pick(hit, _F_URL)
        out.append(norm(
            name=name,
            date=s_d.isoformat(), time_=t1,
            # end_date must be set for the end TIME to survive ingest
            end_date=(e_d.isoformat() if (multi_day or t2) else ""),
            end_time=t2,
            venue=_pick(hit, _F_VENUE),
            description=_pick(hit, _F_DESC),
            image=img if isinstance(img, str) else "",
            category=_pick(hit, _F_CAT),
            url=urljoin(site, link) if link else "",
            source=source_name, all_day=not t1,
        ))
    return out
