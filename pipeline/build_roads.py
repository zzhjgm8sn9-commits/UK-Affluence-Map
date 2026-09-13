"""Major roads, so a zoomed-in choropleth is orientable.

At national zoom the place labels are enough. Zoom in and the map goes abstract
fast: 43,064 anonymous polygons with nothing to anchor them unless you already
know the area. Rivers help by accident -- the ONS boundaries are clipped around
water, so the Thames and the Clyde show through as gaps in the polygon coverage
-- but roads are the thing people actually navigate by.

This is deliberately **not** a street map. Only classified roads are kept:
motorways, A roads and B roads. That gives the bypasses, the radial routes out
of every city, and the named high streets that carry a classification -- Princes
Street in Edinburgh is the A1 -- without burying the data under a road atlas.

**Source.** OS Open Roads, Ordnance Survey's open road network for GB, under the
Open Government Licence. It carries both `roadNumber` (A720) and `name1`
(Princes Street), so labels can prefer whichever is more recognisable. The
download is the full network down to country lanes, which is why the filtering
happens here rather than in the map.

Segments are dissolved by road identity before export. OS splits roads at every
junction, which produces hundreds of thousands of fragments; merging them by
name and number cuts the feature count enormously and, more importantly, stops
MapLibre trying to label the same street forty times along its length.

Output:
  web/data/gb_roads.geojson
"""

from __future__ import annotations

import gzip
import sys
import time
import zipfile
from pathlib import Path

import geopandas as gpd
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from config import INTERIM, RAW, WEB, WGS84

ROADS_ZIP = "os_open_roads.zip"

# Classified roads only. OS also publishes Minor Road, Local Road, Local Access
# Road and so on, which together are the bulk of the network and all of the
# noise for this purpose.
KEEP_CLASSES = {"Motorway", "A Road", "B Road"}

# Metres, in British National Grid, per class. Roads appear at different zooms,
# so they can be cut to different degrees: the strategic network is drawn from
# zoom 8.5 and needs to still look right at 15, while B roads only appear at 11
# where 60 m is under two pixels.
SIMPLIFY_M = {"Motorway": 25, "A Road": 35, "B Road": 60}


def load_roads() -> gpd.GeoDataFrame:
    """Read every RoadLink layer out of the OS zip and keep classified roads."""
    path = RAW / ROADS_ZIP
    with zipfile.ZipFile(path) as z:
        links = [n for n in z.namelist()
                 if n.lower().endswith(".shp") and "roadlink" in n.lower()]
    print(f"  {len(links)} RoadLink layers in the archive")

    parts = []
    for i, name in enumerate(links, 1):
        gdf = gpd.read_file(f"zip://{path}!{name}")
        cls_col = next((c for c in gdf.columns if c.lower() in
                        ("class", "roadclassi", "roadclassification")), None)
        if cls_col is None:
            raise SystemExit(f"no class column in {name}: {list(gdf.columns)}")
        gdf = gdf[gdf[cls_col].isin(KEEP_CLASSES)]
        if len(gdf):
            parts.append(gdf.rename(columns={cls_col: "road_class"}))
        if i % 10 == 0 or i == len(links):
            kept = sum(len(p) for p in parts)
            print(f"\r  {i}/{len(links)} layers, {kept:,} classified links",
                  end="", flush=True)
    print()

    roads = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=parts[0].crs)
    return roads


def dissolved_roads() -> gpd.GeoDataFrame:
    """Classified roads, merged by identity. Cached: reading 52 shapefiles out
    of a 606 MB zip and dissolving takes a minute, and the simplification
    downstream is worth iterating on."""
    cache = INTERIM / "gb_roads_dissolved.parquet"
    if cache.exists():
        merged = gpd.read_parquet(cache)
        print(f"  cached: {len(merged):,} dissolved roads")
        return merged

    print("reading OS Open Roads:")
    roads = load_roads()
    print(f"  {len(roads):,} classified road links")
    print(roads["road_class"].value_counts().to_string())

    # Normalise the label columns; OS abbreviates differently between releases.
    number_col = next((c for c in roads.columns
                       if c.lower() in ("roadnumber", "roadnumbe", "number")), None)
    name_col = next((c for c in roads.columns
                     if c.lower() in ("name1", "name", "roadname")), None)
    roads["road_number"] = roads[number_col].fillna("") if number_col else ""
    roads["road_name"] = roads[name_col].fillna("") if name_col else ""

    print(f"\n  with a road number: {(roads['road_number'] != '').sum():,}")
    print(f"  with a street name: {(roads['road_name'] != '').sum():,}")

    # Dissolve by identity so each road is a handful of features, not hundreds.
    roads["key"] = (roads["road_class"] + "|" + roads["road_number"].astype(str)
                    + "|" + roads["road_name"].astype(str))
    print("\ndissolving by road identity...")
    merged = roads.dissolve(by="key", aggfunc="first")[
        ["road_class", "road_number", "road_name", "geometry"]]
    print(f"  {len(roads):,} links -> {len(merged):,} roads")
    merged = merged.reset_index(drop=True)
    merged.to_parquet(cache)
    return merged


def main() -> None:
    t0 = time.time()
    merged = dissolved_roads()

    # Simplified by class, because they appear at different zooms. B roads are
    # not drawn below zoom 11, where 60 m is under two pixels, so they can be
    # cut much harder than the strategic network without it showing.
    before = int(merged.geometry.count_coordinates().sum())
    tolerance = merged["road_class"].map(SIMPLIFY_M).fillna(SIMPLIFY_M["A Road"])
    merged["geometry"] = [
        geom.simplify(tol, preserve_topology=False)
        for geom, tol in zip(merged.geometry, tolerance)
    ]
    merged = merged[~merged.geometry.is_empty]
    after = int(merged.geometry.count_coordinates().sum())
    print(f"\nsimplified: {before:,} -> {after:,} vertices "
          f"({100 * after / before:.0f}%)")
    merged = merged.to_crs(WGS84).reset_index(drop=True)

    # A single label per road: the number if it has one, else the street name.
    merged["label"] = merged["road_number"].where(
        merged["road_number"].astype(str).str.len() > 0, merged["road_name"])

    out_dir = WEB / "data"
    dest = out_dir / "gb_roads.geojson"
    if dest.exists():
        dest.unlink()
    merged[["road_class", "road_number", "road_name", "label", "geometry"]].to_file(
        dest, driver="GeoJSON", engine="pyogrio",
        COORDINATE_PRECISION=4, RFC7946="NO", WRITE_BBOX="NO")

    raw_mb = dest.stat().st_size / 1e6
    gz_mb = len(gzip.compress(dest.read_bytes(), 6)) / 1e6
    print(f"\nwrote {dest}")
    print(f"  {raw_mb:.1f} MB raw / {gz_mb:.1f} MB gzipped")
    print(f"done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
