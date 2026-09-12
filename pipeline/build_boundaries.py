"""Build the geographic backbone: one harmonised GB small-area layer.

England & Wales contribute 35,672 LSOAs (2021 Census vintage); Scotland
contributes 7,392 Data Zones (2022 Census vintage). These are deliberately
different geographies -- each is the one its national statistics are actually
published against, so downstream joins need no crosswalk.

Output:
  data/interim/gb_areas.parquet   attribute table + geometry, EPSG:4326
  web/data/gb_areas.topo.json     simplified topology for the browser
"""

from __future__ import annotations

import sys
import time
import zipfile
from pathlib import Path

import geopandas as gpd
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from config import (INTERIM, RAW, SIMPLIFY_TOLERANCE, SOURCES,
                    TOPOJSON_QUANTIZATION, WEB, WGS84)
from download import fetch_arcgis_geojson, fetch_file

COLUMNS = ["area_code", "area_name", "nation", "ruc_code", "ruc_name",
           "population", "hh_residents", "households", "geometry"]


def load_england_wales() -> gpd.GeoDataFrame:
    src = SOURCES["lsoa_2021_ew"]
    print("England & Wales: LSOA 2021")
    path = fetch_arcgis_geojson(src["url"], "lsoa_2021_ew.geojson", src["fields"])

    gdf = gpd.read_file(path)
    print(f"  loaded {len(gdf):,} features (expected {src['expected_features']:,})")

    gdf = gdf.rename(columns={
        "LSOA21CD": "area_code",
        "LSOA21NM": "area_name",
        "RUC21CD": "ruc_code",
        "RUC21NM": "ruc_name",
    })
    gdf["nation"] = gdf["area_code"].str[0].map({"E": "England", "W": "Wales"})
    # England & Wales population is added by a later pipeline step (Census 2021).
    for col in ("population", "hh_residents", "households"):
        gdf[col] = pd.NA
    return gdf[COLUMNS]


def load_scotland() -> gpd.GeoDataFrame:
    src = SOURCES["dz_2022_scotland"]
    print("Scotland: Data Zone 2022")
    path = fetch_file(src["url"], src["filename"])

    # The zip holds a single shapefile; let GDAL read it in place.
    with zipfile.ZipFile(path) as zf:
        shp = next(n for n in zf.namelist() if n.lower().endswith(".shp"))
    gdf = gpd.read_file(f"zip://{path}!{shp}")
    print(f"  loaded {len(gdf):,} features (expected {src['expected_features']:,})")
    print(f"  columns: {list(gdf.columns)}")
    print(f"  crs: {gdf.crs}")

    # Field names vary between releases; find the code and name columns.
    code_col = next((c for c in gdf.columns if c.upper() in ("DZCODE", "DATAZONE", "DZ22CD", "DZCD")), None)
    name_col = next((c for c in gdf.columns if c.upper() in ("DZNAME", "NAME", "DZ22NM", "DZNM")), None)
    if code_col is None:
        raise SystemExit(f"Could not identify data zone code column in {list(gdf.columns)}")

    gdf = gdf.rename(columns={code_col: "area_code", name_col: "area_name"})
    if "area_name" not in gdf.columns:
        gdf["area_name"] = gdf["area_code"]
    gdf["nation"] = "Scotland"
    gdf["ruc_code"] = pd.NA
    gdf["ruc_name"] = pd.NA

    # Population and household counts ship with the boundary file.
    for src_col, dest_col in (("totpop2022", "population"),
                              ("hhres2022", "hh_residents"),
                              ("hhcnt2022", "households")):
        gdf[dest_col] = pd.to_numeric(gdf[src_col], errors="coerce") if src_col in gdf.columns else pd.NA

    gdf = gdf.to_crs(WGS84)
    return gdf[COLUMNS]


def main() -> None:
    t0 = time.time()
    ew = load_england_wales()
    sc = load_scotland()

    gb = pd.concat([ew, sc], ignore_index=True)
    gb = gpd.GeoDataFrame(gb, geometry="geometry", crs=WGS84)

    # Guard against silent join failures later on.
    dupes = gb["area_code"].duplicated().sum()
    if dupes:
        raise SystemExit(f"{dupes} duplicate area codes -- geographies overlap")
    invalid = (~gb.geometry.is_valid).sum()
    if invalid:
        print(f"  repairing {invalid} invalid geometries")
        gb["geometry"] = gb.geometry.make_valid()

    print(f"\nGB total: {len(gb):,} areas")
    print(gb.groupby("nation").size().to_string())

    INTERIM.mkdir(parents=True, exist_ok=True)
    out_parquet = INTERIM / "gb_areas.parquet"
    gb.to_parquet(out_parquet)
    print(f"\nwrote {out_parquet} ({out_parquet.stat().st_size / 1e6:.1f} MB)")

    print(f"\ndone in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
