"""
Neighborhoods — the city's hoods, and the point-in-polygon lookup that turns a
venue's coordinates into a hood name.

THE LIVE FILE IS `docs/hoods.geojson` (city.json's `hoods_geojson`). Chicago
ships with 98 neighborhoods, ordered downtown-first (Loop, West Loop, River
North, ...): first match in `order` wins, so put the hoods you care about most
first. A feature may carry an optional `group` property to collapse sparse outer
hoods into one display region ("South Side"); the display key is `group || name`.
It ships with the site (the map reads the same file) and this module only READS it.

  .venv/bin/python hoods.py                 # list the hoods + venue coverage
  .venv/bin/python hoods.py --assign        # fill venue.neighborhood from lat/lng
  .venv/bin/python hoods.py --assign --overwrite   # ...including venues that have one
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

# The live file: the site's copy IS the canonical copy; city.json's
# hoods_geojson points at it, resolved in models.
from models import HOODS_PATH


def load_hoods(path: Path = HOODS_PATH) -> list[dict]:
    """[(name, order, [rings...])] for every hood, in tour order."""
    fc = json.loads(Path(path).read_text())
    out = []
    for f in fc.get("features", []):
        geom = f.get("geometry") or {}
        polys = []
        if geom.get("type") == "Polygon":
            polys = [geom["coordinates"]]
        elif geom.get("type") == "MultiPolygon":
            polys = geom["coordinates"]
        out.append({
            # A `group` collapses sparse outer hoods into one display region
            # ("South Side", ...); the fine geometry stays.
            "name": f["properties"].get("group") or f["properties"].get("name"),
            "order": f["properties"].get("order", 999),
            "polys": polys,
        })
    return sorted(out, key=lambda h: h["order"])


def _in_ring(lat: float, lng: float, ring: list) -> bool:
    """Ray casting. GeoJSON rings are [lng, lat] pairs."""
    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i][0], ring[i][1]
        x2, y2 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
        if (y1 > lat) != (y2 > lat):
            xint = (x2 - x1) * (lat - y1) / (y2 - y1) + x1
            if lng < xint:
                inside = not inside
    return inside


def hood_for(lat: float | None, lng: float | None, hoods: list[dict] | None = None):
    """The hood containing this point, or None. First ring of each polygon is the
    outer boundary; the rest are holes."""
    if lat is None or lng is None:
        return None
    for h in (hoods if hoods is not None else load_hoods()):
        for poly in h["polys"]:
            if not poly:
                continue
            if _in_ring(lat, lng, poly[0]) and not any(
                    _in_ring(lat, lng, hole) for hole in poly[1:]):
                return h["name"]
    return None


def assign(overwrite: bool = False) -> tuple[int, int]:
    """Fill venue.neighborhood from coordinates, via update_venue_fields (the
    sanctioned operator — no raw writes). Idempotent. Returns (set, skipped)."""
    from models import Session, Venue
    from operators import update_venue_fields

    hoods = load_hoods()
    sess = Session()
    changed = skipped = 0
    for v in sess.query(Venue).all():
        if v.lat is None or v.lng is None:
            skipped += 1
            continue
        if v.neighborhood and not overwrite:
            skipped += 1
            continue
        name = hood_for(v.lat, v.lng, hoods)
        if not name or name == v.neighborhood:
            skipped += 1
            continue
        update_venue_fields(v.id, neighborhood=name, sess=sess)
        changed += 1
        print(f"  {v.id[:44]:44s} -> {name}")
    sess.commit()
    sess.close()
    return changed, skipped


def report() -> None:
    hoods = load_hoods()
    print(f"{len(hoods)} hoods in {HOODS_PATH.name} (tour order):")
    for h in hoods:
        print(f"  {h['order']:2d}  {h['name']}")
    try:
        from models import Session, Venue
        sess = Session()
        vs = sess.query(Venue).all()
        with_coords = [v for v in vs if v.lat is not None]
        placed = sum(1 for v in with_coords if hood_for(v.lat, v.lng, hoods))
        print(f"\nvenues: {len(vs)} total · {len(with_coords)} geocoded · "
              f"{placed} inside a focus hood")
        sess.close()
    except Exception as e:                        # no DB yet is fine
        print(f"\n(no venue read: {type(e).__name__}: {e})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--assign", action="store_true",
                    help="fill venue.neighborhood from lat/lng (via update_venue_fields)")
    ap.add_argument("--overwrite", action="store_true",
                    help="with --assign: also re-assign venues that already have a hood")
    args = ap.parse_args()

    if args.assign:
        changed, skipped = assign(overwrite=args.overwrite)
        print(f"\n{changed} venues assigned, {skipped} skipped "
              f"(no coords / already set / outside the focus band)")
    else:
        report()
