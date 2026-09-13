"""Export the geographic backbone for the browser.

Two problems had to be solved here.

**Simplification.** The two national sources arrive at wildly different levels
of generalisation: the ONS England & Wales boundaries are already "super
generalised" at about 12 vertices per area, while the Scottish Data Zone file
is full resolution at about 500 -- so Scotland alone accounted for 90% of the
4.1 million vertices in the raw backbone. Simplifying each polygon
independently would open slivers along shared borders, because Douglas-Peucker
on two neighbours' rings can treat the edge they share differently.

GEOS coverage simplification (Visvalingam-Whyatt, via `shapely.coverage_simplify`)
solves exactly this: it simplifies a polygonal mosaic while keeping it a valid
coverage, so neighbours stay edge-matched and no gaps appear. It is applied per
nation, because England & Wales and Scotland come from different sources and
are only edge-matched within themselves, not with each other.

**Format.** TopoJSON would be the textbook answer, but the Python `topojson`
package cannot build a topology at this scale -- on 43,064 polygons it grew past
24 GB of resident memory without finishing. Coverage simplification gets the
same gap-free guarantee, so plain GeoJSON at reduced coordinate precision is
enough. Rounding to 5 decimal places (~1 m) is lossless for boundaries that are
only accurate to a couple of hundred metres.

If the map ever feels heavy, the next step is tippecanoe -> PMTiles under WSL.

Output:
  web/data/gb_areas.json
"""

from __future__ import annotations

import gzip
import sys
import time
from pathlib import Path

import geopandas as gpd
import pandas as pd
import shapely

sys.path.insert(0, str(Path(__file__).parent))

from config import INTERIM, WEB, WGS84

# Metres, applied in British National Grid. 100 m brings Scotland into line
# with the generalisation the ONS already applied to England & Wales, and
# leaves island and coastline shape legible.
SIMPLIFY_METRES = 100
PROJECTED = "EPSG:27700"
COORDINATE_PRECISION = 5  # decimal degrees: ~1.1 m


def simplify_coverage(gdf: gpd.GeoDataFrame, tolerance: float) -> gpd.GeoDataFrame:
    """Simplify each source mosaic independently, preserving its topology.

    Grouping is by source file, not by nation: England and Wales arrive in one
    ONS dataset and are edge-matched to each other, so they must be simplified
    as a single coverage or slivers open along the border between them.
    Scotland is a separate file and a separate coverage.
    """
    gdf = gdf.assign(
        coverage=gdf["nation"].map(lambda n: "Scotland" if n == "Scotland" else "England & Wales")
    )

    out = []
    for nation, part in gdf.groupby("coverage", sort=False):
        part = part.to_crs(PROJECTED)
        before = int(part.geometry.count_coordinates().sum())

        simplified = shapely.coverage_simplify(
            part.geometry.values, tolerance, simplify_boundary=True
        )
        part = part.assign(geometry=simplified)

        after = int(part.geometry.count_coordinates().sum())
        invalid = int((~part.geometry.is_valid).sum())
        empty = int(part.geometry.is_empty.sum())
        print(f"  {nation:<9} {before:>9,} -> {after:>7,} vertices "
              f"({100 * after / before:4.1f}%)"
              + (f"  WARNING invalid={invalid} empty={empty}" if invalid or empty else ""))

        out.append(part.to_crs(WGS84))

    return gpd.GeoDataFrame(pd.concat(out, ignore_index=True), crs=WGS84)


def main() -> None:
    t0 = time.time()
    src = INTERIM / "gb_areas.parquet"
    if not src.exists():
        raise SystemExit("run build_boundaries.py first")

    gdf = gpd.read_parquet(src)
    print(f"loaded {len(gdf):,} areas, "
          f"{int(gdf.geometry.count_coordinates().sum()):,} vertices")

    print(f"\ncoverage simplification at {SIMPLIFY_METRES} m:")
    gdf = simplify_coverage(gdf, SIMPLIFY_METRES)
    print(f"  total    {int(gdf.geometry.count_coordinates().sum()):>9,} vertices")

    # Geometry plus the join key only. Attributes travel in a separate file so
    # that rebuilding metrics does not force the browser to re-parse geometry.
    slim = gdf[["area_code", "geometry"]].copy()

    out_dir = WEB / "data"
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / "gb_areas.json"
    if dest.exists():
        dest.unlink()

    print(f"\nwriting GeoJSON at {COORDINATE_PRECISION} dp...")
    slim.to_file(
        dest,
        driver="GeoJSON",
        engine="pyogrio",
        COORDINATE_PRECISION=COORDINATE_PRECISION,
        RFC7946="NO",
        WRITE_BBOX="NO",
    )

    raw_mb = dest.stat().st_size / 1e6
    gz_mb = len(gzip.compress(dest.read_bytes(), 6)) / 1e6
    print(f"\nwrote {dest}")
    print(f"  {raw_mb:.1f} MB raw / {gz_mb:.1f} MB gzipped")
    print(f"done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
