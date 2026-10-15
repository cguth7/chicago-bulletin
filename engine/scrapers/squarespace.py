"""Squarespace (?format=json items[]) + its aggregator wrappers (parks_on_tap, east_passyunk_bid, moviejawn, philalandmarks)."""
from __future__ import annotations

import datetime as dt
import json
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse, parse_qs

from . import core
from .core import _slug, _with_route, host_root, norm, strip_html

# ---------------- squarespace  (?format=json items[]) ----------------

def _ms_to_local(ms):
    """Squarespace epoch-ms → naive local datetime (already wall-clock for the venue)."""
    if ms is None:
        return None
    try:
        return dt.datetime.fromtimestamp(int(ms) / 1000)
    except Exception:
        return None


def _squarespace_item(it, base, source_name, seen, start, end,
                      route_on_address_title=False):
    """Normalize one Squarespace event item (shared by both collection shapes).
    Returns a norm dict or None (already seen / out of range / bad date).

    route_on_address_title (default OFF — generic output unchanged): aggregator
    mode for Squarespace feeds where `location.addressTitle` names the hosting
    venue (East Passyunk BID). Lifts addressTitle -> venue + emits the routing
    tag so ingest can fan events out via coverage.routing_key."""
    sid = it.get("id")
    if sid in seen:
        return None
    seen.add(sid)
    sdt = _ms_to_local(it.get("startDate"))
    edt = _ms_to_local(it.get("endDate"))
    if not sdt or not (start <= sdt.date() <= end):
        return None
    loc = it.get("location") or {}
    addr = ", ".join(filter(None, [
        loc.get("addressLine1"), loc.get("addressLine2")])) or loc.get("addressTitle", "")
    venue = ""
    tags = ", ".join(t for t in (it.get("tags") or []))
    if route_on_address_title:
        # strip_html also DECODES entities — moviejawn's feed carries a raw
        # "&amp;" in addressTitle ("Village of Industry &amp; Arts"), which
        # would otherwise slug to a broken "-amp-" routing tag.
        venue = strip_html(loc.get("addressTitle") or "").strip()
        tags = _with_route(tags, venue)
    return norm(
        name=strip_html(it.get("title", "")),
        date=sdt.strftime("%Y-%m-%d"), time_=sdt.strftime("%H:%M"),
        end_date=edt.strftime("%Y-%m-%d") if edt else "",
        end_time=edt.strftime("%H:%M") if edt else "",
        address=addr, lat=loc.get("markerLat") or loc.get("mapLat"),
        lng=loc.get("markerLng") or loc.get("mapLng"),
        # `body` is raw page HTML and routinely carries a <style> block. strip_html
        # (above) now drops style/script CONTENT, so no CSS reaches `description`.
        description=strip_html(it.get("body") or it.get("excerpt", "")),
        image=it.get("assetUrl", ""),
        category=", ".join(c for c in (it.get("categories") or [])),
        tags=tags, venue=venue,
        url=urljoin(host_root(base), it.get("fullUrl", "")),
        source=source_name,
    )


def scrape_squarespace(collection_url, source_name, start, end, max_months=6,
                       route_on_address_title=False):
    """Squarespace Events collection. Handles BOTH response shapes:
      - legacy "page"/list collections: events live in items[], paginated by nextPageUrl
      - modern "events-stacked" collections: events live in upcoming[]/past[], and the
        default view only returns the current window — so we walk forward month-by-month
        via &month=MM-YYYY across [start,end] to capture everything.
    Items whose startDate falls in [start,end] are kept; dedup by item id.
    """
    out, seen = [], set()
    base = collection_url.split("?")[0]

    # First request (no month) tells us which shape this collection is.
    try:
        data = core.fetch_json(base + "?format=json")
    except (HTTPError, URLError, json.JSONDecodeError) as e:
        print(f"  [{source_name}] squarespace: {e}", file=sys.stderr)
        return out

    is_stacked = bool(data.get("upcoming") is not None or data.get("past") is not None)

    if is_stacked:
        # walk the calendar month-by-month over the window, reading upcoming[] (+ past[]).
        cur = dt.date(start.year, start.month, 1)
        months = 0
        while cur <= end and months <= max_months:
            url = f"{base}?format=json&month={cur.month:02d}-{cur.year}"
            try:
                d = core.fetch_json(url)
            except (HTTPError, URLError, json.JSONDecodeError) as e:
                print(f"  [{source_name}] squarespace month {cur}: {e}", file=sys.stderr)
                break
            for key in ("upcoming", "past"):
                for it in (d.get(key) or []):
                    row = _squarespace_item(it, base, source_name, seen, start, end,
                                            route_on_address_title)
                    if row:
                        out.append(row)
            # advance one month
            cur = dt.date(cur.year + (cur.month // 12), (cur.month % 12) + 1, 1)
            months += 1
            time.sleep(0.3)
        # some events-stacked collections return the full upcoming[] on the base call
        # regardless of month (no &month needed) — fold that in too.
        for it in (data.get("upcoming") or []):
            row = _squarespace_item(it, base, source_name, seen, start, end,
                                    route_on_address_title)
            if row:
                out.append(row)
        return out

    # legacy items[] shape, paginated by nextPageUrl
    url, months = base + "?format=json", 0
    while months <= max_months:
        for it in (data.get("items") or []):
            row = _squarespace_item(it, base, source_name, seen, start, end,
                                    route_on_address_title)
            if row:
                out.append(row)
        nxt = (data.get("pagination") or {}).get("nextPageUrl")
        if not nxt:
            break
        url = urljoin(base, nxt) + ("&format=json" if "format=json" not in nxt else "")
        try:
            data = core.fetch_json(url)
        except (HTTPError, URLError, json.JSONDecodeError) as e:
            print(f"  [{source_name}] squarespace: {e}", file=sys.stderr)
            break
        months += 1
        time.sleep(0.3)
    return out


# ---------------- parks_on_tap  (Squarespace where the TITLE is the park) -----


# ---------------- east_passyunk_bid  (Squarespace where addressTitle is the venue)


# ---------------- moviejawn  (Squarespace where addressTitle is the venue) ----


# ---------------- philalandmarks  (Squarespace aggregator over historic houses)


