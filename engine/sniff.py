"""
Sniff — deterministic platform fingerprinting for leads that HAVE a website.
The zero-token half of triage: pretriage.py answers "do we already have this?",
sniff.py answers "what platform is it on and where's the feed?" so sweep Scouts
confirm annotated rows in one fetch and spend their budget on the ambiguous ones.

Per lead: ONE page fetch + at most two confirming endpoint probes (Tribe REST /
Squarespace ?format=json / Localist API). Verdicts name a real adapter from
scrapers.ADAPTERS when one fits; a trailing '*' means "platform seen, needs
adapter work or a per-site look" — a guess for a Scout to confirm, never a build.

  .venv/bin/python sniff.py <url> [<url> ...]           # ad-hoc
  .venv/bin/python sniff.py --csv leads.csv             # annotate rows w/ Website
  .venv/bin/python sniff.py --known                     # DB rows: website, no built coverage
      [--out annotated.csv]
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from urllib.parse import urljoin

from probe_sources import (classify_html, get, host_root, probe_localist,
                           probe_squarespace, probe_tribe)

# html-only fallbacks, in confidence order: (platform key, verdict adapter)
EMBED_VERDICTS = [
    ("bibliocommons", "bibliocommons"),
    ("eventbrite", "eventbrite*"),
    ("ticketmaster", "ticketmaster_api*"),
    ("luma", "luma*"),
    ("dice", "dice*"),
    ("seetickets", "seetickets*"),
    ("humanitix", "humanitix*"),
    ("googlecal", "ical*"),
]


def html_verdict(html: dict, url: str) -> tuple[str, str | None, str]:
    """(adapter, endpoint, evidence) from a classify_html dict alone — pure."""
    plats = html.get("platforms", [])
    if "the_events_calendar" in plats:
        return "tribe_wp?", host_root(url), "TEC markers but REST unconfirmed — check path/auth"
    if "wix" in plats:
        return "wix", host_root(url), "wix markers — probe the wix events API"
    if "squarespace" in plats:
        return "squarespace?", url, "squarespace markers but no dated ?format=json collection found"
    for key, adapter in EMBED_VERDICTS:
        if key in plats:
            return adapter, url, f"{key} embed/link on page"
    if html.get("jsonld_event_count"):
        return "html_jsonld", url, f"JSON-LD events x{html['jsonld_event_count']}"
    if html.get("ical_links"):
        return "ical", urljoin(url, html["ical_links"][0]), "iCal link on page"
    if "wordpress" in plats:
        return "wordpress*", host_root(url), "WordPress but no TEC markers — look for another plugin"
    gen = html.get("generator")
    return "?", None, f"no fingerprint (generator={gen or '-'})"


def sniff_site(url: str) -> dict:
    """1 page fetch + <=2 confirm probes -> {url, adapter, endpoint, evidence}."""
    status, ct, body = get(url)
    if not body:
        return {"url": url, "adapter": "unreachable", "endpoint": None,
                "evidence": f"fetch failed (status={status} {ct})"}
    html = classify_html(body)
    plats = html["platforms"]
    root = host_root(url)

    if "wordpress" in plats or "the_events_calendar" in plats:
        t = probe_tribe(root)
        if t.get("ok"):
            return {"url": url, "adapter": "tribe_wp", "endpoint": root,
                    "evidence": f"Tribe REST confirmed, total={t.get('total')}"}
    if "squarespace" in plats:
        events_page = urljoin(root + "/", "events")
        for cand in dict.fromkeys([url.split("?")[0], events_page]):
            sq = probe_squarespace(cand)
            if sq.get("ok"):
                return {"url": url, "adapter": "squarespace", "endpoint": cand,
                        "evidence": f"?format=json confirmed, dated_items={sq.get('dated_items')}"}
    if "localist" in plats:
        lc = probe_localist(root)
        if lc.get("ok"):
            return {"url": url, "adapter": "localist", "endpoint": root,
                    "evidence": "Localist API confirmed"}

    adapter, endpoint, evidence = html_verdict(html, url)
    return {"url": url, "adapter": adapter, "endpoint": endpoint, "evidence": evidence}


def fmt(name: str, v: dict) -> str:
    return f"{name:36} {v['adapter']:16} {v['endpoint'] or '-'}\n      {v['evidence']}"


def run_urls(urls: list[str]) -> None:
    for u in urls:
        print(fmt(u.split("//")[-1][:36], sniff_site(u)))


def run_csv(path: Path, out: Path | None) -> None:
    rows = list(csv.DictReader(open(path)))
    site_col = next((c for c in (rows[0] if rows else {})
                     if c.lower() in ("website", "site", "url")), None)
    if not site_col:
        sys.exit(f"{path}: no Website/Site/URL column")
    n = 0
    for r in rows:
        url = (r.get(site_col) or "").strip()
        if not url or r.get("SniffAdapter"):
            continue
        v = sniff_site(url)
        r["SniffAdapter"], r["SniffEndpoint"], r["SniffEvidence"] = \
            v["adapter"], v["endpoint"] or "", v["evidence"]
        n += 1
        print(fmt(r.get("Handle") or r.get("Name") or url[:36], v))
    if out:
        fields = list(rows[0].keys())
        for extra in ("SniffAdapter", "SniffEndpoint", "SniffEvidence"):
            if extra not in fields:
                fields.append(extra)
        with open(out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)
        print(f"\n{n} sniffed -> {out}")


def run_known(out: Path | None) -> None:
    """DB entities that HAVE a website but no built pipeline — the narrow-job set."""
    from models import Group, PipelineSource, Session, SourceCoverage, Venue
    sess = Session()
    built = {s.id for s in sess.query(PipelineSource) if s.status == "built"}
    covered = {(c.target_kind, c.target_id)
               for c in sess.query(SourceCoverage) if c.source_id in built}
    targets = [("venue", v.id, v.name, v.website) for v in sess.query(Venue)
               if v.website and ("venue", v.id) not in covered]
    targets += [("group", g.id, g.name, g.website) for g in sess.query(Group)
                if g.website and ("group", g.id) not in covered]
    sess.close()
    print(f"{len(targets)} uncovered rows with websites\n")
    rows = []
    for kind, oid, name, url in sorted(targets):
        v = sniff_site(url)
        rows.append({"Kind": kind, "Id": oid, "Name": name, "Website": url,
                     "SniffAdapter": v["adapter"], "SniffEndpoint": v["endpoint"] or "",
                     "SniffEvidence": v["evidence"]})
        print(fmt(f"{kind}:{oid}"[:36], v))
    if out and rows:
        with open(out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f"\n-> {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("urls", nargs="*")
    ap.add_argument("--csv", type=Path)
    ap.add_argument("--known", action="store_true")
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()
    if args.known:
        run_known(args.out)
    elif args.csv:
        run_csv(args.csv, args.out or args.csv)
    elif args.urls:
        run_urls(args.urls)
    else:
        ap.print_help()
