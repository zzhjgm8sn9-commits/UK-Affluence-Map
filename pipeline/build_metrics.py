"""Assemble the GB affluence index and export it for the browser.

The index is a weighted mean of standardised components. Two choices matter:

**Standardise across GB, not within nation.** Z-scoring each nation separately
would force every nation to the same mean, erasing exactly the differences the
map exists to show. The components were chosen so that a single GB-wide
standardisation is legitimate: they are measured in pounds and in percentages
of people, using classifications that mean the same thing on both sides of the
border. The national means bear this out -- NS-SEC L1-L3 is 13.0% in England
and Wales against 12.0% in Scotland, and degree-level qualifications 33.5%
against 31.9%.

**Take logs of price before standardising.** Median sale price runs from £23k
to £4m and is heavily right-skewed; a raw z-score would let a handful of
central London areas dominate the whole index.

Weights are defaults, not doctrine -- the UI exposes them as sliders so the
index can be interrogated rather than taken on trust.

Output:
  data/interim/gb_metrics.parquet
  web/data/gb_metrics.json
"""

from __future__ import annotations

import json
import sys
import time
import zipfile
from datetime import date
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

import components as C
from config import INTERIM, RAW, WEB, WGS84

# key -> (label, format, default weight, description)
COMPONENTS = {
    "median_price": (
        "Median property price", "gbp", 0.40,
        "Median residential sale price. Measured in pounds, so directly "
        "comparable across GB. Land Registry 2023-2025 for England and Wales; "
        "Registers of Scotland 2023 for Scotland.",
    ),
    "nssec_higher": (
        "Higher managerial & professional", "pct", 0.30,
        "Share of residents 16+ in NS-SEC classes L1-L3. The same "
        "classification is used across GB, making this the most reliably "
        "comparable component.",
    ),
    "qual_level4": (
        "Degree-level qualifications", "pct", 0.20,
        "Share of residents 16+ qualified to degree level or above. Scotland "
        "uses its own bands; the mapping was calibrated so the national means "
        "agree.",
    ),
    "cars_2plus": (
        "Households with 2+ cars", "pct", 0.10,
        "Blunt but comparable, and it keeps discriminating at the top of the "
        "distribution where deprivation measures saturate. Reads high in rural "
        "areas where a second car is a necessity, hence the low weight.",
    ),
}

# Components too skewed for a raw z-score.
LOG_COMPONENTS = {"median_price"}


def gather_components() -> pd.DataFrame:
    """Stack each component's England & Wales and Scotland halves."""
    parts: dict[str, pd.Series] = {}

    pairs = {
        "median_price": (C.property_price_ew, C.property_price_scotland),
        "nssec_higher": (C.nssec_higher_ew, C.nssec_higher_scotland),
        "qual_level4": (C.qualifications_ew, C.qualifications_scotland),
        "cars_2plus": (C.cars_ew, C.cars_scotland),
    }

    for key, (ew_fn, sc_fn) in pairs.items():
        ew = ew_fn()[key]
        sc = sc_fn()[key]
        combined = pd.concat([ew, sc])
        parts[key] = combined[~combined.index.duplicated()]
        print(f"  {key:<16} E&W {len(ew):>6,}  Scotland {len(sc):>6,}  "
              f"total {len(parts[key]):>6,}")

    # Sale counts are useful context in the UI: they say how much evidence sits
    # behind an area's median.
    n_sales = C.property_price_ew()["n_sales"]

    df = pd.DataFrame(parts)
    df["n_sales"] = n_sales
    return df


def population_ew() -> pd.Series:
    with zipfile.ZipFile(RAW / "census2021-ts001.zip") as z:
        raw = pd.read_csv(z.open("census2021-ts001-lsoa.csv"))
    total_col = next(c for c in raw.columns if c.startswith("Residence type: Total"))
    return pd.Series(raw[total_col].values, index=raw["geography code"], name="population")


def zscore(s: pd.Series) -> pd.Series:
    return (s - s.mean()) / s.std(ddof=0)


def main() -> None:
    t0 = time.time()

    areas = gpd.read_parquet(INTERIM / "gb_areas.parquet")

    # Representative points, not centroids: a centroid can fall outside a
    # concave or multi-part area (coastal areas and islands especially), which
    # would put a branch catchment the wrong side of an estuary.
    reps = areas.to_crs("EPSG:27700").geometry.representative_point()
    reps = gpd.GeoSeries(reps, crs="EPSG:27700").to_crs(WGS84)
    areas["centroid_lon"] = reps.x.values
    areas["centroid_lat"] = reps.y.values

    backbone = areas[["area_code", "area_name", "nation", "population",
                      "centroid_lat", "centroid_lon"]].set_index("area_code")

    print("components:")
    comp = gather_components()

    df = backbone.join(comp, how="left")

    # England & Wales population comes from the census; Scotland's arrived with
    # the boundary file.
    pop_ew = population_ew()
    df["population"] = df["population"].fillna(pd.Series(pop_ew)).astype("Float64")

    print("\nstandardising (GB-wide):")
    for key in COMPONENTS:
        source = np.log10(df[key]) if key in LOG_COMPONENTS else df[key]
        df["z_" + key] = zscore(source)
        covered = df[key].notna().sum()
        print(f"  {key:<16} {covered:>6,} areas ({100*covered/len(df):5.1f}%)"
              + ("  [log]" if key in LOG_COMPONENTS else ""))

    # Weighted mean of available z-scores, then a percentile rank so the scale
    # is interpretable whatever the weights.
    weights = {k: v[2] for k, v in COMPONENTS.items()}
    zcols = ["z_" + k for k in COMPONENTS]
    w = np.array([weights[k] for k in COMPONENTS])
    z = df[zcols].to_numpy(dtype=float)

    present = ~np.isnan(z)
    weight_present = (present * w).sum(axis=1)
    weighted_sum = np.nansum(np.nan_to_num(z) * w, axis=1)
    score = np.where(weight_present >= 0.5 * w.sum(), weighted_sum / weight_present, np.nan)

    df["affluence_index"] = pd.Series(score, index=df.index).rank(pct=True) * 100

    scored = df["affluence_index"].notna().sum()
    print(f"\nindex computed for {scored:,} of {len(df):,} areas "
          f"({100*scored/len(df):.1f}%)")
    print("\nby nation (mean index):")
    print(df.groupby("nation")["affluence_index"].agg(["mean", "count"]).round(1).to_string())

    df.to_parquet(INTERIM / "gb_metrics.parquet")
    write_web_json(df)
    print(f"\ndone in {time.time() - t0:.0f}s")


def write_web_json(df: pd.DataFrame) -> None:
    """Columnar JSON: parallel arrays keyed by position, not per-area objects.

    Same information as a list of records at roughly a third the size, because
    the field names are not repeated 43,064 times.
    """
    def clean(series: pd.Series, digits: int) -> list:
        return [None if pd.isna(v) else round(float(v), digits) for v in series]

    values: dict[str, list] = {}
    for key in COMPONENTS:
        values[key] = clean(df[key], 2 if key != "median_price" else 0)
        values["z_" + key] = clean(df["z_" + key], 3)
    values["affluence_index"] = clean(df["affluence_index"], 1)
    values["n_sales"] = clean(df["n_sales"], 0)

    payload = {
        "generated": date.today().isoformat(),
        "codes": df.index.tolist(),
        "attrs": [
            {"key": "area_name", "values": df["area_name"].tolist()},
            {"key": "nation", "values": df["nation"].tolist()},
            {"key": "population", "values": clean(df["population"], 0)},
            {"key": "centroid_lat", "values": clean(df["centroid_lat"], 5)},
            {"key": "centroid_lon", "values": clean(df["centroid_lon"], 5)},
        ],
        "components": [
            {
                "key": k,
                "label": label,
                "format": fmt,
                "weight": weight,
                "default_weight": weight,
                "description": desc,
            }
            for k, (label, fmt, weight, desc) in COMPONENTS.items()
        ],
        "metrics": [
            {"key": "affluence_index", "label": "Affluence index", "format": "index"},
            *[{"key": k, "label": v[0], "format": v[1]} for k, v in COMPONENTS.items()],
            {"key": "n_sales", "label": "Sales in sample", "format": "count"},
        ],
        "values": values,
    }

    dest = WEB / "data" / "gb_metrics.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload, separators=(",", ":")))
    print(f"wrote {dest} ({dest.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
