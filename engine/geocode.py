"""
Geocode venues missing coordinates via Nominatim (anchored on city.json's
city_state). Persists to the DB AND to data/geocode_cache.json so every rebuild
reuses results. Run after leads land:

  .venv/bin/python geocode.py
  .venv/bin/python geocode.py --limit 20

The cache is keyed by venue id (slug) — a hit costs no HTTP call.

`apply_cache()` is the no-network half, exposed for callers that already KNOW a
venue's coordinates (e.g. an importer): they write the pairs into the cache and call it. Coordinates stay owned by this
module — the operators deliberately refuse lat/lng — and nobody pays Nominatim
twice for a point we already resolved.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

from models import CITY_SLUG, CITY_STATE, CONTACT, Session, Venue

CACHE = Path(__file__).parent / "data" / "geocode_cache.json"
# Nominatim's policy wants a UA that names the app; a contact email lets OSM
# reach you instead of blocking you. Set "contact" in city.json.
UA = f"{CITY_SLUG}-bulletin/0.1" + (f" ({CONTACT})" if CONTACT else "")
SKIP = {"", "online", "tbd", "virtual", "various", "various location",
        "various locations", "multiple locations", "zoom", "n/a",
        "to be announced"}


def load_cache() -> dict:
    return json.load(open(CACHE)) if CACHE.exists() else {}


def save_cache(cache: dict) -> None:
    """Sorted so re-running a writer produces byte-identical output."""
    json.dump(cache, open(CACHE, "w"), indent=2, sort_keys=True)


def apply_cache(sess=None) -> tuple[int, int]:
    """Fill venue lat/lng from the on-disk cache ONLY — no HTTP, no writes to the
    cache. Returns (filled, still_missing). Idempotent: a venue that already has
    coordinates is never touched."""
    own = sess is None
    sess = sess or Session()
    cache = load_cache()
    todo = sess.query(Venue).filter(Venue.lat.is_(None)).all()
    filled = 0
    for v in todo:
        hit = cache.get(v.id)
        if hit:
            v.lat, v.lng = hit[0], hit[1]
            filled += 1
    if own:
        sess.commit()
        sess.close()
    return filled, len(todo) - filled


def nominatim(q):
    url = (f"https://nominatim.openstreetmap.org/search?q={quote_plus(q)}"
           f"&format=json&limit=1&countrycodes=us")
    res = json.loads(urlopen(Request(url, headers={"User-Agent": UA}), timeout=20).read())
    if res:
        return round(float(res[0]["lat"]), 6), round(float(res[0]["lon"]), 6)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    args = ap.parse_args()
    if not CONTACT:
        print('note: set "contact" (your email) in city.json — Nominatim asks for one')

    sess = Session()
    cached, _ = apply_cache(sess)          # the free hits first, no HTTP
    cache = load_cache()
    todo = sess.query(Venue).filter(Venue.lat.is_(None)).all()
    if args.limit:
        todo = todo[:args.limit]
    print(f"{cached} filled from cache; {len(todo)} venues still missing coordinates\n")

    hits = fails = 0
    for v in todo:
        name = (v.name or "").strip()
        if name.lower() in SKIP:
            fails += 1; continue
        # prefer the street address (more reliable than venue name) + city anchor
        q = ", ".join(x for x in [v.address or name, CITY_STATE] if x)
        try:
            time.sleep(1.1)  # Nominatim usage policy: <=1 req/sec
            r = nominatim(q)
        except Exception as e:
            print(f"  err {name[:34]}: {e}"); r = None
        if r:
            v.lat, v.lng = r
            cache[v.id] = [r[0], r[1]]
            hits += 1
            print(f"  ✓ {name[:42]:42} {r[0]:.4f},{r[1]:.4f}")
        else:
            fails += 1
            print(f"  ✗ {name[:42]}")

    sess.commit(); sess.close()
    save_cache(cache)
    print(f"\nNew geocodes {hits}, from cache {cached}, failed {fails}. "
          f"Cache now holds {len(cache)} venues.")


if __name__ == "__main__":
    main()
