"""Bank branch locations from OpenStreetMap, joined to small areas.

**Licensing note.** Everything else in this project is Open Government Licence.
OpenStreetMap is ODbL, which carries share-alike obligations on derived
databases. That is fine for internal analysis but matters if any output
containing this layer leaves the organisation. It is kept in its own file and
its own pipeline step so it can be dropped cleanly if that becomes a problem.

**Coverage caveat.** OSM branch data is contributed, not authoritative. It is
good in towns, patchier in rural areas, and often slow to reflect closures --
and UK bank branches have been closing at pace. Treat counts as indicative. An
internal branch list would be strictly better and can be dropped in here.

Two details worth knowing:

  * ISO3166-1 "GB" means the United Kingdom, so the Overpass query returns
    Northern Ireland too. Branches are spatially joined to the GB small-area
    backbone and anything that lands outside it is dropped -- which removes
    Northern Ireland and any stray coordinate in one step.
  * Bank buildings are mapped as ways as well as nodes; `out center` gives ways
    a centre point so both kinds carry coordinates.

Output:
  data/interim/gb_branches.parquet
  web/data/gb_branches.json
"""

from __future__ import annotations

import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import Point

sys.path.insert(0, str(Path(__file__).parent))

from config import INTERIM, RAW, WEB, WGS84

OSM_FILE = "osm_banks_gb.json"
STREETS_CACHE = "osm_branch_streets.json"

OVERPASS = "https://overpass-api.de/api/interpreter"
STREET_SEARCH_RADIUS_M = 45
STREET_BATCH = 250

# Link roads and motorways are never a branch's address.
EXCLUDED_HIGHWAYS = {
    "motorway", "motorway_link", "trunk_link", "primary_link",
    "secondary_link", "tertiary_link", "construction", "proposed", "raceway",
}

KM_PER_DEG_LAT = 110.574

# Brands with fewer than this many branches are grouped into "Other" in the
# filter, so the list stays usable. They keep their real brand in the data.
MIN_BRANCHES_FOR_FILTER = 15


def load_osm() -> pd.DataFrame:
    payload = json.loads((RAW / OSM_FILE).read_text(encoding="utf-8"))
    rows = []
    for el in payload["elements"]:
        tags = el.get("tags", {})
        if "lat" in el:
            lat, lon = el["lat"], el["lon"]
        elif "center" in el:
            lat, lon = el["center"]["lat"], el["center"]["lon"]
        else:
            continue

        brand = tags.get("brand") or tags.get("operator") or tags.get("name")
        rows.append({
            "osm_id": f"{el['type']}/{el['id']}",
            "brand": (brand or "Unknown").strip(),
            "osm_name": (tags.get("name") or brand or "Bank").strip(),
            "street": (tags.get("addr:street") or "").strip(),
            "town": (tags.get("addr:city") or tags.get("addr:suburb") or "").strip(),
            "postcode": (tags.get("addr:postcode") or "").strip().upper(),
            "lat": lat,
            "lon": lon,
        })
    return pd.DataFrame(rows)


def _local_xy(lat, lon):
    """Metres in a local equirectangular frame. Fine over tens of metres."""
    import math
    return (lon * KM_PER_DEG_LAT * 1000 * math.cos(math.radians(lat)),
            lat * KM_PER_DEG_LAT * 1000)


def _point_segment_distance(px, py, ax, ay, bx, by) -> float:
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return ((px - (ax + t * dx)) ** 2 + (py - (ay + t * dy)) ** 2) ** 0.5


def fetch_nearest_streets(need: pd.DataFrame) -> dict[str, str]:
    """Street name for each branch that has no addr:street, from the road network.

    Roughly 43% of branches carry no address in OSM, which left them labelled
    with nothing but the name of their local authority -- five Barclays all
    reading "Wirral". The fix is to ask OSM what road each one actually sits on.

    The branches are themselves OSM objects, so their ids seed an Overpass set
    and `around.set:` finds named highways near each one *individually*. A list
    of coordinates will not do: Overpass reads a multi-coordinate `around` as a
    polyline and searches near that line instead, which returns nothing useful.

    Results are cached, since this is the only slow step in the pipeline.
    """
    import json as _json
    import time as _time

    import requests

    cache_path = RAW / STREETS_CACHE
    resolved: dict[str, str] = {}
    if cache_path.exists():
        resolved = _json.loads(cache_path.read_text(encoding="utf-8"))
        print(f"  resuming: {len(resolved):,} already cached")

    # Overpass rate-limits hard, so the cache is written after every batch and
    # anything already resolved is skipped. A run that gets throttled can just
    # be started again rather than repeating work.
    need = need[~need["osm_id"].isin(resolved)]
    if need.empty:
        return resolved

    session = requests.Session()
    session.headers.update({"User-Agent": "uk-affluence-map/0.1 (open data pipeline)"})

    batches = [need.iloc[i:i + STREET_BATCH] for i in range(0, len(need), STREET_BATCH)]
    for n, batch in enumerate(batches, 1):
        nodes = [i.split("/")[1] for i in batch["osm_id"] if i.startswith("node/")]
        ways = [i.split("/")[1] for i in batch["osm_id"] if i.startswith("way/")]
        parts = []
        if nodes:
            parts.append(f"node(id:{','.join(nodes)});")
        if ways:
            parts.append(f"way(id:{','.join(ways)});")

        query = ("[out:json][timeout:180];(" + "".join(parts) + ")->.banks;"
                 f"way[highway][name](around.banks:{STREET_SEARCH_RADIUS_M});"
                 "out tags geom;")
        # 429 and 504 are Overpass saying "slow down" / "busy", not failures.
        for attempt in range(6):
            resp = session.post(OVERPASS, data=query, timeout=300)
            if resp.status_code not in (429, 502, 503, 504):
                break
            wait = 20 * (attempt + 1)
            print(f"  Overpass returned {resp.status_code}; waiting {wait}s")
            _time.sleep(wait)
        resp.raise_for_status()

        roads = [w for w in resp.json().get("elements", [])
                 if w.get("tags", {}).get("highway") not in EXCLUDED_HIGHWAYS
                 and w.get("geometry")]

        # Precompute each road's projected geometry and bounding box once.
        prepared = []
        for w in roads:
            pts = [_local_xy(g["lat"], g["lon"]) for g in w["geometry"]]
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            prepared.append((w["tags"]["name"], pts,
                             min(xs), max(xs), min(ys), max(ys)))

        pad = STREET_SEARCH_RADIUS_M * 1.5
        for r in batch.itertuples():
            px, py = _local_xy(r.lat, r.lon)
            best, best_d = None, float("inf")
            for name, pts, x0, x1, y0, y1 in prepared:
                if px < x0 - pad or px > x1 + pad or py < y0 - pad or py > y1 + pad:
                    continue
                for i in range(len(pts) - 1):
                    d = _point_segment_distance(px, py, *pts[i], *pts[i + 1])
                    if d < best_d:
                        best_d, best = d, name
                if len(pts) == 1:
                    d = ((px - pts[0][0]) ** 2 + (py - pts[0][1]) ** 2) ** 0.5
                    if d < best_d:
                        best_d, best = d, name
            # Record misses as well as hits. Some branches genuinely have no
            # named road within the radius -- retail parks, shopping centres --
            # and without a negative entry every later run re-queries them.
            # Delete the cache file to force a full refresh.
            resolved[r.osm_id] = best if best is not None else ""

        cache_path.write_text(_json.dumps(resolved), encoding="utf-8")
        # flush: this step takes minutes, and Python buffers stdout when it is
        # piped, which would hide all progress until the run ends.
        print(f"  batch {n}/{len(batches)}: {len(resolved):,} streets resolved",
              flush=True)
        _time.sleep(6.0)   # Overpass allows only a couple of slots at a time

    cache_path.write_text(_json.dumps(resolved), encoding="utf-8")
    return resolved


DEDUPE_METRES = 40


def dedupe_branches(df: pd.DataFrame) -> pd.DataFrame:
    """Drop the same branch mapped twice in OSM.

    A bank often appears both as a node and as the building way around it, or
    gets added twice by different contributors. Two entries of the same brand
    within 40 m are treated as one. Left in, they inflate branch counts and
    double-weight that spot in a catchment.
    """
    keep = []
    for _, group in df.groupby("brand", sort=False):
        rows = list(group.itertuples())
        taken: list[tuple[float, float]] = []
        for r in rows:
            x, y = _local_xy(r.lat, r.lon)
            if any((x - tx) ** 2 + (y - ty) ** 2 < DEDUPE_METRES ** 2
                   for tx, ty in taken):
                continue
            taken.append((x, y))
            keep.append(r.Index)
    removed = len(df) - len(keep)
    if removed:
        print(f"  {removed} duplicate mappings removed "
              f"(same brand within {DEDUPE_METRES} m)")
    return df.loc[keep]


def nearest_postcodes(targets: pd.DataFrame) -> dict[str, str]:
    """Nearest live postcode to each branch, from the ONSPD.

    Used only as a tie-breaker. Cheap, because the postcode file is already
    built for the income pipeline and a coarse grid makes the lookup local.
    """
    if targets.empty:
        return {}
    pcs = pd.read_parquet(INTERIM / "postcodes.parquet",
                          columns=["postcode", "lat", "lon"])
    cell = 0.02   # roughly 1.4 km of latitude
    grid: dict[tuple, list] = {}
    for pc, la, lo in zip(pcs["postcode"].values, pcs["lat"].values, pcs["lon"].values):
        grid.setdefault((int(la / cell), int(lo / cell)), []).append((pc, la, lo))

    out = {}
    for r in targets.itertuples():
        best, best_d = None, float("inf")
        gy, gx = int(r.lat / cell), int(r.lon / cell)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for pc, la, lo in grid.get((gy + dy, gx + dx), ()):
                    d = (la - r.lat) ** 2 + ((lo - r.lon) * 0.6) ** 2
                    if d < best_d:
                        best_d, best = d, pc
        if best:
            out[r.osm_id] = best
    return out


def disambiguate(joined: pd.DataFrame) -> pd.DataFrame:
    """Guarantee that no two branches of a brand share a label.

    Even with a street name, two branches of the same brand can sit on
    same-named streets in one authority ("High Street"). A postcode settles it,
    and is a form a banking audience reads fluently anyway.
    """
    key = joined["brand_group"] + "||" + joined["label"]
    clashing = key.duplicated(keep=False)
    if not clashing.any():
        return joined

    need_pc = joined[clashing & (joined["postcode"] == "")]
    found = nearest_postcodes(need_pc)
    postcodes = [
        r.postcode or found.get(r.osm_id, "")
        for r in joined.itertuples()
    ]
    joined = joined.assign(postcode_final=postcodes)

    labels = []
    for flag, label, pc in zip(clashing, joined["label"], joined["postcode_final"]):
        labels.append(f"{label} ({pc})" if flag and pc else label)
    joined = joined.assign(label=labels)

    still = (joined["brand_group"] + "||" + joined["label"]).duplicated(keep=False).sum()
    print(f"  {clashing.sum():,} labels disambiguated by postcode; "
          f"{still} still non-unique")
    return joined


def town_from_area_name(name: str) -> str:
    """Recover a place name from a small-area name.

    Only 55% of branches carry addr:street and 53% addr:city, which would leave
    nearly half the pins labelled with nothing but their brand. Small-area names
    encode their local authority or locality plus a serial -- "Manchester 054C",
    "Culter - 01" -- so stripping the serial gives a usable fallback.
    """
    if not isinstance(name, str):
        return ""
    cleaned = re.sub(r"\s*-\s*\d+$", "", name)          # Scottish data zones
    cleaned = re.sub(r"\s+\d{3,4}[A-Za-z]?$", "", cleaned)  # E&W LSOAs
    return cleaned.strip()


def branch_label(street: str, town: str, area_name: str) -> str:
    """Street first, because that is what distinguishes two branches of the
    same brand in the same town."""
    place = town or town_from_area_name(area_name)
    if street and place and street.lower() != place.lower():
        return f"{street}, {place}"
    return street or place or "Branch"


def main() -> None:
    t0 = time.time()
    df = load_osm()
    print(f"OSM elements with coordinates: {len(df):,}")

    areas = gpd.read_parquet(INTERIM / "gb_areas.parquet")[
        ["area_code", "area_name", "nation", "geometry"]]
    pts = gpd.GeoDataFrame(
        df, geometry=[Point(xy) for xy in zip(df["lon"], df["lat"])], crs=WGS84)

    joined = gpd.sjoin(pts, areas, how="left", predicate="within")
    joined = joined[~joined.index.duplicated()]

    outside = joined["area_code"].isna().sum()
    print(f"  {outside:,} branches fall outside the GB backbone "
          f"(Northern Ireland and strays) -- dropped")
    joined = joined.dropna(subset=["area_code"])
    joined = dedupe_branches(joined)
    print(f"  {len(joined):,} GB branches retained")
    print(joined.groupby("nation").size().to_string())

    from_osm = (joined["street"] != "").sum()
    missing = joined[joined["street"] == ""]
    print(f"  {from_osm:,} branches carry addr:street "
          f"({100*from_osm/len(joined):.0f}%); looking up the road network "
          f"for the other {len(missing):,}")
    streets = fetch_nearest_streets(missing)

    joined["street"] = [
        r.street or streets.get(r.osm_id, "") for r in joined.itertuples()
    ]
    have = (joined["street"] != "").sum()
    print(f"  {have:,} now have a street ({100*have/len(joined):.0f}%)")

    joined["label"] = [
        branch_label(r.street, r.town, r.area_name)
        for r in joined.itertuples()
    ]

    counts = Counter(joined["brand"])
    major = {b for b, n in counts.items() if n >= MIN_BRANCHES_FOR_FILTER}
    joined["brand_group"] = joined["brand"].where(joined["brand"].isin(major), "Other")
    print(f"\n  {len(major)} brands with >= {MIN_BRANCHES_FOR_FILTER} branches; "
          f"{len(counts) - len(major)} smaller ones grouped as Other")

    # "Other" bundles hundreds of distinct banks, so two of them on the same
    # street look like a duplicate when they are nothing of the kind -- Punjab
    # National Bank and Bank of India both sit on Belgrave Road in Leicester.
    # Naming the real brand in the label keeps them apart and is more useful.
    is_other = joined["brand_group"] == "Other"
    joined.loc[is_other, "label"] = (
        joined.loc[is_other, "brand"] + " - " + joined.loc[is_other, "label"])

    # After brand_group exists: uniqueness is defined within a brand, and
    # brand_group is what the map actually filters and labels by.
    joined = disambiguate(joined)

    out = joined[["osm_id", "brand", "brand_group", "label", "lat", "lon", "area_code"]]
    out.to_parquet(INTERIM / "gb_branches.parquet", index=False)

    brands = (out.groupby("brand_group").size().sort_values(ascending=False)
              .rename("n").reset_index())
    payload = {
        "generated": time.strftime("%Y-%m-%d"),
        "attribution": "© OpenStreetMap contributors (ODbL)",
        "brands": brands.to_dict("records"),
        "branches": [
            {"b": r.brand_group, "n": r.label, "y": round(r.lat, 5),
             "x": round(r.lon, 5), "a": r.area_code}
            for r in out.itertuples()
        ],
    }
    dest = WEB / "data" / "gb_branches.json"
    dest.write_text(json.dumps(payload, separators=(",", ":")))
    print(f"\nwrote {dest} ({dest.stat().st_size / 1e6:.2f} MB)")

    print("\ntop brands:")
    for r in brands.head(15).itertuples():
        print(f"  {r.n:>5}  {r.brand_group}")
    print(f"\ndone in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
