"""
Probe sources whose adapter/URL guess is WRONG — every "built" PipelineSource
that ingest produced 0 events from (404, wrong-platform JSON error, 403). For
each one we detect the real calendar platform and emit a corrected (adapter, url)
so the source row can be corrected (Scout re-triages, Max re-gates).

Scout's sniffing tool (Job C re-gates the ~40 v1 sources with it):
  - targets pipeline_sources (not sources/feeds)
  - by default probes sources that are status="built" but yielded 0 events,
    i.e. the known-broken set; pass --all to probe every built source.

For each source we:
  - hit the host's Tribe/TEC endpoint   (/wp-json/tribe/events/v1/events)
  - hit Squarespace JSON                 (<page>?format=json -> items[])
  - hit Localist API                     (/api/2/events)
  - fetch the calendar HTML and sniff generator meta, JSON-LD Event, iCal,
    embedded platform iframes (eventbrite/humanitix/wix/luma/dice/...)

Writes data/source_probe.json + prints a verdict table with a SUGGESTED adapter.

  .venv/bin/python probe_sources.py            # the broken (0-event built) sources
  .venv/bin/python probe_sources.py --all      # every built source
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from models import Event, PipelineSource, Session

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
DATA_DIR = Path(__file__).parent / "data"


def get(url, timeout=15):
    """Return (status, content_type, body_text|None). Never raises."""
    try:
        req = Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
        with urlopen(req, timeout=timeout) as r:
            ct = r.headers.get("Content-Type", "")
            body = r.read(600_000)
            try:
                return r.status, ct, body.decode("utf-8", "replace")
            except Exception:
                return r.status, ct, None
    except HTTPError as e:
        return e.code, "", None
    except (URLError, TimeoutError, Exception) as e:
        return None, f"ERR:{type(e).__name__}", None


def host_root(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def probe_tribe(root):
    url = f"{root}/wp-json/tribe/events/v1/events?per_page=1"
    status, ct, body = get(url)
    if status == 200 and body and "application/json" in ct.lower():
        try:
            data = json.loads(body)
            if "events" in data:
                return {"ok": True, "url": url, "total": data.get("total"),
                        "sample": (data["events"][0].get("title") if data["events"] else None)}
        except Exception:
            pass
    return {"ok": False, "url": url, "status": status}


def probe_squarespace(cal_url):
    """Squarespace events collection: <page>?format=json -> {items:[{startDate,...}]}."""
    base = (cal_url or "").split("?")[0]
    if not base:
        return {"ok": False}
    url = base + "?format=json"
    status, ct, body = get(url)
    if status == 200 and body:
        try:
            data = json.loads(body)
            items = data.get("items")
            if isinstance(items, list):
                dated = [it for it in items if it.get("startDate")]
                return {"ok": bool(dated), "url": url, "items": len(items),
                        "dated_items": len(dated)}
        except Exception:
            pass
    return {"ok": False, "url": url, "status": status}


def probe_localist(root):
    url = f"{root}/api/2/events?pp=1"
    status, ct, body = get(url)
    if status == 200 and body and "json" in ct.lower():
        try:
            data = json.loads(body)
            if "events" in data:
                return {"ok": True, "url": url}
        except Exception:
            pass
    return {"ok": False, "url": url, "status": status}


def classify_html(body: str) -> dict:
    """Pure fingerprint pass over an HTML body — no network, testable."""
    out = {}
    low = body.lower()

    gen = re.search(r'<meta[^>]+name=["\']generator["\'][^>]+content=["\']([^"\']+)', body, re.I)
    out["generator"] = gen.group(1) if gen else None

    platforms = []
    fp = {
        "wordpress": ["wp-content", "wp-json", "wp-includes"],
        "the_events_calendar": ["tribe-events", "tribe_events", "the-events-calendar"],
        "squarespace": ["squarespace.com", "static1.squarespace", "sqs-"],
        "wix": ["wix.com", "wixstatic", "wixsite"],
        "eventbrite": ["eventbrite.com", "eventbrite.co"],
        "ticketmaster": ["ticketmaster.com", "livenation.com"],
        "humanitix": ["humanitix.com"],
        "dice": ["dice.fm"],
        "seetickets": ["seetickets", "wl.seetickets"],
        "luma": ["lu.ma", "luma-"],
        "googlecal": ["calendar.google.com"],
        "cvent": ["cvent.com"],
        "localist": ["localist.com", "trumba"],
        "bibliocommons": ["bibliocommons", "biblio"],
        "patronbase_tessitura": ["tessitura", "tnew"],
        "ovationtix_audienceview": ["ovationtix", "audienceview"],
    }
    for name, needles in fp.items():
        if any(n in low for n in needles):
            platforms.append(name)
    out["platforms"] = platforms

    ld_events = 0
    for m in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                         body, re.I | re.S):
        chunk = m.group(1)
        if '"event"' in chunk.lower():
            ld_events += len(re.findall(r'"@type"\s*:\s*"[^"]*event[^"]*"', chunk, re.I))
    out["jsonld_event_count"] = ld_events

    ics = re.findall(r'href=["\']([^"\']*(?:\.ics|ical=1|/ical/|format=ical)[^"\']*)', body, re.I)
    out["ical_links"] = list(dict.fromkeys(ics))[:5]

    return out


def sniff_html(url):
    status, ct, body = get(url)
    out = {"url": url, "status": status, "content_type": ct}
    if not body:
        return out
    out.update(classify_html(body))
    return out


def suggest(entry):
    """Turn probe results into a (adapter, url, verdict) suggestion."""
    if entry["tribe"].get("ok"):
        return "tribe_wp", entry["tribe"]["url"].split("/wp-json")[0], \
            f"TRIBE/TEC ✅ total={entry['tribe'].get('total')}"
    if entry["squarespace"].get("ok"):
        return "squarespace", entry["squarespace"]["url"].split("?")[0], \
            f"SQUARESPACE ✅ dated_items={entry['squarespace'].get('dated_items')}"
    if entry["localist"].get("ok"):
        return "localist", entry["localist"]["url"].split("/api/2")[0], "LOCALIST ✅"
    html = entry.get("html", {})
    plats = html.get("platforms", [])
    if "the_events_calendar" in plats:
        return "tribe_wp?", entry["root"], "HTML shows TEC but REST failed — check path/auth"
    if "bibliocommons" in plats:
        return "bibliocommons*", html.get("url"), "BiblioCommons (CPL) — needs new adapter"
    for p in ("eventbrite", "humanitix", "dice", "seetickets", "wix", "luma"):
        if p in plats:
            return f"{p}*", html.get("url"), f"platform={p} — needs adapter/handling"
    if html.get("jsonld_event_count"):
        return "html_jsonld*", html.get("url"), f"JSON-LD events x{html['jsonld_event_count']} — needs jsonld adapter"
    if html.get("ical_links"):
        return "ical*", html["ical_links"][0], f"iCal feed available"
    return "?", html.get("url"), f"unknown (html status {html.get('status')}, plats={plats})"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="probe every built source, not just 0-event ones")
    args = ap.parse_args()

    sess = Session()
    built = sess.query(PipelineSource).filter(
        PipelineSource.status == "built", PipelineSource.is_aggregator == False
    ).all()
    producing = {sid for (sid,) in sess.query(Event.source_id).distinct()}

    targets = built if args.all else [s for s in built if s.id not in producing]
    print(f"Probing {len(targets)} sources "
          f"({'all built' if args.all else 'built-but-0-events'})\n")

    report = {}
    for s in sorted(targets, key=lambda s: s.name):
        cal_url = s.url or s.api_url or ""
        root = host_root(cal_url)
        entry = {"name": s.name, "adapter_now": s.adapter, "cal_url": cal_url, "root": root}
        entry["tribe"] = probe_tribe(root)
        entry["squarespace"] = probe_squarespace(cal_url)
        entry["localist"] = probe_localist(root)
        entry["html"] = sniff_html(cal_url) if cal_url else {}

        adapter, url, verdict = suggest(entry)
        entry["suggest_adapter"] = adapter
        entry["suggest_url"] = url
        entry["verdict"] = verdict
        report[s.id] = entry

        change = "" if adapter == s.adapter else f"  (was {s.adapter})"
        print(f"  {s.name:38} {adapter:14}{change}")
        print(f"      {verdict}")
        if url and url != cal_url:
            print(f"      url -> {url}")

    DATA_DIR.mkdir(exist_ok=True)
    out = DATA_DIR / "source_probe.json"
    json.dump(report, open(out, "w"), indent=2)
    print(f"\nWrote {out}")
    sess.close()


if __name__ == "__main__":
    main()
