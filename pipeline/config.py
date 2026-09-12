"""Central configuration: paths and the registry of upstream data sources.

Every source here is open data. Licence is recorded per source because the
project may end up informing commercial decisions and the distinction
between OGL and ODbL matters for anything we redistribute.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RAW = DATA / "raw"
INTERIM = DATA / "interim"
OUT = DATA / "out"
WEB = ROOT / "web"

for _d in (RAW, INTERIM, OUT):
    _d.mkdir(parents=True, exist_ok=True)

# Geographic backbone. England & Wales use LSOA 2021; Scotland uses Data Zone
# 2022. These are the vintages that the current statistical outputs are
# actually published against, so joining needs no crosswalk.
ARCGIS_ROOT = "https://services1.arcgis.com/ESMARspQHYMw9BZ9/ArcGIS/rest/services"

SOURCES = {
    "lsoa_2021_ew": {
        "kind": "arcgis",
        "layer": "LSOA_2021_EW_BSC_V4_RUC",
        "url": f"{ARCGIS_ROOT}/LSOA_2021_EW_BSC_V4_RUC/FeatureServer/0",
        "fields": ["LSOA21CD", "LSOA21NM", "RUC21CD", "RUC21NM", "LAT", "LONG"],
        "expected_features": 35672,
        "licence": "OGL v3 (ONS / OS)",
        "note": "Super-generalised clipped boundaries, rural-urban class joined.",
    },
    "dz_2022_scotland": {
        "kind": "zip_shapefile",
        "url": "https://maps.gov.scot/ATOM/shapefiles/SG_DataZoneBdry_2022.zip",
        "filename": "SG_DataZoneBdry_2022.zip",
        "expected_features": 7392,
        "licence": "OGL v3 (Scottish Government)",
        "note": "2022 Census-based data zones, EPSG:27700.",
    },
}

# Target coordinate system for the web map.
WGS84 = "EPSG:4326"

# Topology-preserving simplification. Tuned so that 43k polygons stay legible
# at national zoom without gaps appearing between neighbours.
SIMPLIFY_TOLERANCE = 0.0004
TOPOJSON_QUANTIZATION = 1e5
