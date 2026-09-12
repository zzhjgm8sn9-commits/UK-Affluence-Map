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
            "lat": lat,
            "lon": lon,
        })
    return pd.DataFrame(rows)


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
    print(f"  {len(joined):,} GB branches retained")
    print(joined.groupby("nation").size().to_string())

    joined["label"] = [
        branch_label(r.street, r.town, r.area_name)
        for r in joined.itertuples()
    ]
    named = (joined["street"] != "").sum()
    print(f"  {named:,} have a street from OSM ({100*named/len(joined):.0f}%); "
          f"the rest fall back to their area's place name")

    counts = Counter(joined["brand"])
    major = {b for b, n in counts.items() if n >= MIN_BRANCHES_FOR_FILTER}
    joined["brand_group"] = joined["brand"].where(joined["brand"].isin(major), "Other")
    print(f"\n  {len(major)} brands with >= {MIN_BRANCHES_FOR_FILTER} branches; "
          f"{len(counts) - len(major)} smaller ones grouped as Other")

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
