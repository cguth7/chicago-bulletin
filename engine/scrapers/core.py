"""
Shared plumbing for every adapter: fetch/fetch_json (the ONLY network calls —
family modules call these as `core.fetch` so tests can patch one seam), UA,
LOCAL_TZ, strip_html, routing-tag helpers, `norm` (the one event shape), and
the validate_events gate that `built` is earned against.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import re
import sys
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from urllib.parse import urljoin, urlparse, parse_qs
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

# 2026-08-16 (Max/Job D): was the Mac Chrome/120 string. campusphilly.org's
# Cloudflare config 403s that exact fingerprint while serving this one a clean 200
# (Scout verified 3x; re-verified here against 8 hosts — campus-philly flips
# 403 -> 200 and nothing else changes). One global string beats a per-source
# override: same fix, no new column, no new plumbing.
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# The engine stores naive venue wall-clock times (models.TIMEZONE). scrapers
# stays models-free (no SQLAlchemy import), so the zone is read straight from
# the same repo-root city.json models reads. Used to localize offset-carrying ISO timestamps (e.g.
# playdate's +00:00 UTC JSON-LD) before they're sliced into date/time strings.
import os
from pathlib import Path
_CITY_JSON = Path(__file__).parent.parent.parent / "city.json"
_CITY = json.loads(_CITY_JSON.read_text())
LOCAL_TZ = ZoneInfo(_CITY["timezone"])
HOME_CITY = _CITY["city"]   # for adapters that filter roaming listings by city


# ---------------- shared helpers ----------------

def fetch(url, headers=None, timeout=20):
    req = Request(url, headers={"User-Agent": UA, "Accept": "*/*", **(headers or {})})
    with urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_json(url, headers=None, timeout=20):
    return json.loads(fetch(url, headers=headers, timeout=timeout))


def post_json(url, payload, headers=None, timeout=20):
    """POST a JSON body, parse a JSON reply. Same single-seam rule as fetch():
    adapters call `core.post_json` so tests patch one place. Added 2026-08-30 for
    the signup.software storefront API (Prisma-style POST /query/<model>)."""
    body = json.dumps(payload).encode()
    req = Request(url, data=body, headers={
        "User-Agent": UA, "Accept": "application/json",
        "Content-Type": "application/json; charset=utf-8", **(headers or {})})
    with urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


# Blocks whose CONTENT is machine text, not human text. The old stripper removed
# only the TAGS, so a <style> block's CSS survived as "description" — the raw-CSS
# junk that leaked into Chicago's live data via Squarespace event bodies (chi event
# id 5). Fixed here, in the engine, never in the frontend: data.json ships clean.
_SCRIPT_STYLE = re.compile(r"<(script|style|head|noscript)\b[^>]*>.*?</\1\s*>", re.I | re.S)
_COMMENT = re.compile(r"<!--.*?-->", re.S)
# Residual CSS from an UNCLOSED <style> (tag stripped, declarations left behind):
# a selector-ish run followed by a {...} block containing a `prop: value` pair.
_CSS_RULE = re.compile(r"[^{}<>]{0,160}\{[^{}]*?:[^{}]*?\}", re.S)
_AT_RULE = re.compile(r"@(?:media|supports|font-face|import|charset|keyframes)[^{;]*[{;]?", re.I)


def strip_html(s):
    """HTML -> plain text: kill non-text blocks CONTENT AND ALL, then tags, then
    entities, then any CSS that survived, then whitespace."""
    if not s:
        return ""
    s = _COMMENT.sub(" ", s)
    s = _SCRIPT_STYLE.sub(" ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    had_css, prev = False, None
    while prev != s:                       # unwind nested rules (@media { .a {…} })
        prev, s = s, _CSS_RULE.sub(" ", s)
        had_css = had_css or s != prev
    prev, s = s, _AT_RULE.sub(" ", s)
    had_css = had_css or s != prev
    if had_css:                            # sweep the braces the CSS left behind
        s = re.sub(r"[{}]", " ", s)        # (only when CSS was actually present)
    return re.sub(r"\s+", " ", s).strip()


def host_root(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def _slug(s: str) -> str:
    """models.slug, re-implemented so scrapers.py stays stdlib-only (it is the one
    module you can smoke-test without SQLAlchemy). Must stay byte-identical to
    models.slug — routing keys are compared against venue ids minted by it."""
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").strip().lower()).strip("-")
    return s or "unknown"


def route_tag(venue_name: str) -> str:
    """The aggregator routing tag. ingest.py fans an aggregator's events out to the
    venues it covers by matching `park:<slug>` in an event's tags against
    `source_coverage.routing_key` (generic join key; the name is a Chicago-parks
    fossil). Adapters that serve aggregator sources emit it from the venue name the
    event itself carries, so routing is an exact slug match — never a name guess.
    Harmless on dedicated sources: that branch of ingest ignores tags."""
    return f"park:{_slug(venue_name)}" if (venue_name or "").strip() else ""


def _with_route(tags: str, venue_name: str) -> str:
    """Append the routing tag to an adapter's existing tag string."""
    rt = route_tag(venue_name)
    return ", ".join(t for t in ((tags or "").strip(", "), rt) if t)


def norm(*, name, date="", time_="", end_date="", end_time="", venue="", address="",
         lat=None, lng=None, description="", image="", category="", tags="", cost="",
         organizer="", url="", source="", all_day=False):
    """The one normalized event shape every adapter returns.

    `description` is sanitized HERE as well as in the adapters: this is the single
    funnel every adapter's output passes through, so no future adapter can leak
    raw HTML/CSS into the DB by forgetting to call strip_html. (Sanitizing twice
    is a no-op.)"""
    return {
        "name": name, "date": date, "time": time_, "end_date": end_date,
        "end_time": end_time, "venue": venue, "address": address, "lat": lat,
        "lng": lng, "description": strip_html(description)[:600], "image": image,
        "category": category, "tags": tags, "cost": cost, "organizer": organizer,
        "url": url, "source": source, "all_day": all_day,
    }


def _in_range(date_str, start, end):
    try:
        d = dt.date.fromisoformat(date_str[:10])
    except Exception:
        return False
    return start <= d <= end


# ---- the minimum event schema (the build gate keys off this) ----

def valid_event(raw, start, end):
    """A raw event is VALID iff it has a non-empty name AND a date that parses and
    falls in [start, end]. Mirrors exactly what ingest.py keeps (name + parseable
    date), so 'has >=1 valid event' == 'ingest would store something'."""
    if not isinstance(raw, dict):
        return False
    if not (raw.get("name") or "").strip():
        return False
    return _in_range(raw.get("date") or "", start, end)


def validate_events(events, start, end):
    """Apply the minimum schema to an adapter's output. Returns
    {n_total, n_valid, sample, reason} where reason explains a 0-valid result:
      'no_events'  adapter returned an empty list (dormant / nothing in window)
      'all_invalid' returned rows but none had name+in-range date (past-only,
                    wrong shape, junk). The caller maps transport errors (HTTP/
                    parse) separately — this only sees successfully-returned lists."""
    events = events or []
    valid = [e for e in events if valid_event(e, start, end)]
    reason = None
    if not valid:
        reason = "no_events" if not events else "all_invalid"
    return {
        "n_total": len(events),
        "n_valid": len(valid),
        "sample": (valid[0]["name"][:60] if valid else None),
        "reason": reason,
    }
