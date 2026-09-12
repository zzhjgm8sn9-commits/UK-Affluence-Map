"""Individual affluence-index components, each returning a tidy frame.

Every component must be **comparable across national borders**. That rules out
the obvious candidates -- IMD, WIMD and SIMD are separate within-nation
rankings, built from different indicators with different weights, so a rank in
one nation means nothing in another. What survives the test is measurements in
real units: pounds, and percentages of people.

Each function returns a DataFrame indexed by `area_code` with one value column.
"""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

import duckdb
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from config import INTERIM, RAW

# Land Registry PPD has no header row. Column order is fixed and documented.
PPD_COLUMNS = [
    "txn_id", "price", "date", "postcode", "property_type", "old_new",
    "duration", "paon", "saon", "street", "locality", "town", "district",
    "county", "ppd_category", "record_status",
]

PPD_YEARS = (2023, 2024, 2025)

# Below this many transactions a median is too noisy to publish for an area.
MIN_TRANSACTIONS = 8


def property_price_ew() -> pd.DataFrame:
    """Median residential sale price per LSOA, from Land Registry transactions.

    Filters follow the Land Registry's own guidance: category A only (standard
    arms-length sales, excluding repossessions and portfolio transfers), and
    property type 'O' excluded because it covers non-residential and mixed use.

    A median of *sale prices* conflates value with housing mix -- an area of
    small flats reads poorer than one of large houses at the same price per
    square metre. That is a real limitation, defensible here because property
    wealth is itself part of what we are trying to measure, but it is the first
    thing to improve (join EPC floor areas and switch to price per square metre).
    """
    files = [str(RAW / f"pp-{y}.csv") for y in PPD_YEARS]
    missing = [f for f in files if not Path(f).exists()]
    if missing:
        raise SystemExit(f"missing Land Registry files: {missing}")

    con = duckdb.connect()
    cols = ", ".join(f"'{c}'" for c in PPD_COLUMNS)
    df = con.execute(f"""
        WITH txns AS (
            SELECT
                upper(replace(postcode, ' ', '')) AS pc_key,
                CAST(price AS BIGINT)             AS price
            FROM read_csv({files!r},
                          header = false,
                          names = [{cols}],
                          all_varchar = true)
            WHERE ppd_category = 'A'
              AND property_type <> 'O'
              AND postcode IS NOT NULL
              AND postcode <> ''
        ),
        joined AS (
            SELECT p.area_code, t.price
            FROM txns t
            JOIN read_parquet('{(INTERIM / "postcodes.parquet").as_posix()}') p
              ON p.pc_key = t.pc_key
        )
        SELECT
            area_code,
            median(price) AS median_price,
            count(*)      AS n_sales
        FROM joined
        GROUP BY area_code
        HAVING count(*) >= {MIN_TRANSACTIONS}
        ORDER BY area_code
    """).df()
    con.close()

    return df.set_index("area_code")


def _census_ew(zip_name: str, csv_name: str, numerator_prefixes: tuple[str, ...],
               total_contains: str, out_name: str) -> pd.DataFrame:
    """Percentage of a Census 2021 total falling in selected categories.

    NOMIS column headers are long and prone to punctuation drift between
    tables, so columns are matched on distinctive substrings rather than
    reproduced literally.
    """
    path = RAW / zip_name
    with zipfile.ZipFile(path) as z:
        df = pd.read_csv(io.BytesIO(z.read(csv_name)))

    code_col = "geography code"
    total_col = next(c for c in df.columns if total_contains in c)
    num_cols = [c for c in df.columns
                if any(p in c for p in numerator_prefixes) and c != total_col]
    if not num_cols:
        raise SystemExit(f"{zip_name}: no columns matched {numerator_prefixes}")

    out = pd.DataFrame({
        out_name: 100 * df[num_cols].sum(axis=1) / df[total_col].replace(0, pd.NA),
    })
    out.index = df[code_col]
    out.index.name = "area_code"
    return out


def nssec_higher_ew() -> pd.DataFrame:
    """% of residents 16+ in higher managerial, administrative and professional
    occupations (NS-SEC classes L1-L3)."""
    return _census_ew(
        "census2021-ts062.zip", "census2021-ts062-lsoa.csv",
        numerator_prefixes=("L1, L2 and L3",),
        total_contains="Total: All usual residents aged 16 years and over",
        out_name="nssec_higher",
    )


def qualifications_ew() -> pd.DataFrame:
    """% of residents 16+ whose highest qualification is Level 4 or above
    (degree level and higher)."""
    return _census_ew(
        "census2021-ts067.zip", "census2021-ts067-lsoa.csv",
        numerator_prefixes=("Level 4 qualifications and above",),
        total_contains="Total: All usual residents aged 16 years and over",
        out_name="qual_level4",
    )


def cars_ew() -> pd.DataFrame:
    """% of households with two or more cars or vans.

    A blunt but genuinely comparable proxy, and one of the few that keeps
    discriminating at the top of the distribution where deprivation measures
    have long since saturated. It does read high in rural areas where a second
    car is a necessity rather than a luxury, which is why it carries a low
    default weight.
    """
    return _census_ew(
        "census2021-ts045.zip", "census2021-ts045-lsoa.csv",
        numerator_prefixes=("2 cars or vans", "3 or more cars or vans"),
        total_contains="Total: All households",
        out_name="cars_2plus",
    )


# --------------------------------------------------------------------------
# Scotland
#
# Scotland's sources are published on three different geographies, none of
# which is the 2022 Data Zone the backbone uses:
#
#   * Census 2022 bulk tables  -> 2022 Output Areas
#   * Residential price cube   -> 2011 Data Zones
#
# The ONSPD carries every live postcode's 2022 Data Zone, 2022 Output Area and
# 2011 Data Zone simultaneously, so it can rebase all of them without any
# additional lookup file. Postcode counts are the weighting: a crude proxy for
# dwellings, but the right shape, and for the ~90% of zones that did not change
# between vintages the mapping is exact anyway.
# --------------------------------------------------------------------------

_SCOT_TOTAL_LABELS = ("All people aged 16 and over", "All households")


def _scotland_postcodes() -> pd.DataFrame:
    df = pd.read_parquet(INTERIM / "postcodes.parquet",
                         columns=["area_code", "oa_code", "area_code_2011"])
    return df[df["area_code"].str.startswith("S")]


def _read_nrs_bulk(prefix: str) -> tuple[list[str], list[list[str]], pd.DataFrame]:
    """Read a National Records of Scotland bulk cross-tab.

    These files carry two or three stacked header rows above the data, each
    level only labelled where it changes, plus a few lines of preamble. Returns
    the forward-filled header levels and the numeric block indexed by OA code.
    """
    import csv as _csv

    with zipfile.ZipFile(RAW / "scotland_census_oa.zip") as z:
        name = next(n for n in z.namelist() if n.startswith(prefix))
        rows = list(_csv.reader(io.TextIOWrapper(io.BytesIO(z.read(name)), "utf-8-sig")))

    start = next(i for i, r in enumerate(rows) if r and r[0].startswith("S00"))
    ncols = len(rows[start])

    # Header rows are the run of full-width rows immediately above the data;
    # the preamble lines are single-field.
    first_header = start
    while first_header > 0 and len(rows[first_header - 1]) == ncols:
        first_header -= 1

    levels = []
    for r in rows[first_header:start]:
        filled, last = [], ""
        for v in r:
            last = v or last
            filled.append(last)
        levels.append(filled)

    codes = [r[0] for r in rows[start:] if r and r[0].startswith("S00")]
    data = pd.DataFrame(
        [[_num(v) for v in r[1:ncols]] for r in rows[start:] if r and r[0].startswith("S00")],
        index=codes,
    )
    return codes, levels, data


def _num(v: str) -> float:
    """NRS uses '-' for zero or suppressed cells."""
    v = (v or "").strip().replace(",", "")
    if v in ("", "-"):
        return 0.0
    try:
        return float(v)
    except ValueError:
        return 0.0


def _nrs_share(prefix: str, matches, out_name: str) -> pd.DataFrame:
    """Share of a total falling in categories matched by `matches`.

    Only columns that are *totals* on every dimension but the first are used,
    so a cross-tab is collapsed back to the marginal distribution we want.
    """
    codes, levels, data = _read_nrs_bulk(prefix)

    # Column 1 of the raw file (index 0 of `data`) is the all-categories total
    # on every level, which gives us each level's "total" label.
    totals = [lv[1] for lv in levels]

    marginal = [i for i in range(len(data.columns))
                if all(levels[L][i + 1] == totals[L] for L in range(1, len(levels)))]
    categories = {i: levels[0][i + 1] for i in marginal}

    total_cols = [i for i, c in categories.items() if c == totals[0]]
    num_cols = [i for i, c in categories.items() if matches(c)]
    if not total_cols or not num_cols:
        raise SystemExit(f"{prefix}: matched total={total_cols} numerator={num_cols} "
                         f"from {list(categories.values())}")

    oa = pd.DataFrame({
        "numerator": data[num_cols].sum(axis=1),
        "total": data[total_cols[0]],
    })
    oa.index.name = "oa_code"

    # Aggregate output areas up to 2022 data zones.
    pcs = _scotland_postcodes()[["oa_code", "area_code"]].drop_duplicates("oa_code")
    merged = oa.join(pcs.set_index("oa_code"), how="inner")
    grouped = merged.groupby("area_code")[["numerator", "total"]].sum()

    out = pd.DataFrame({out_name: 100 * grouped["numerator"] / grouped["total"].replace(0, pd.NA)})
    out.index.name = "area_code"
    return out


def nssec_higher_scotland() -> pd.DataFrame:
    """NS-SEC L1-L3, matching the England & Wales definition.

    The colon matters: NRS labels the nine classes "L1: ...", "L10: ...",
    "L12: ..." and so on, so a bare "L1" prefix also swallows L10 and L12-L15
    and returns about 59% instead of 12%.
    """
    return _nrs_share("MV607", lambda c: c.startswith("L1:"), "nssec_higher")


def qualifications_scotland() -> pd.DataFrame:
    """Closest available match to the England & Wales "Level 4 and above".

    Scotland reports its own qualification bands, so the mapping was chosen by
    calibrating against the national means rather than by reading the labels.
    Scotland's "Degree level qualifications or above" is 32.5% of people 16+,
    against 33.5% for the England & Wales "Level 4 and above" -- close enough
    to treat as the same concept. Adding Scotland's separate sub-degree band
    (HNC/HNDs, a further 13.2%) would overshoot England & Wales badly, so
    despite HNC/HND nominally sitting at Level 4 it is excluded.

    Scotland's top band does also absorb "other qualifications not already
    mentioned (including foreign qualifications)", which England and Wales
    report separately. The agreement between the two national means suggests
    that effect is small.
    """
    return _nrs_share("MV501", lambda c: c.startswith("Degree level"), "qual_level4")


def cars_scotland() -> pd.DataFrame:
    return _nrs_share(
        "MV407",
        lambda c: any(k in c for k in ("Two cars", "Three cars", "Four or more cars")),
        "cars_2plus",
    )


SCOTLAND_PRICE_YEAR = 2023
_SPARQL_ENDPOINT = "https://statistics.gov.scot/sparql"
_SCOT_PRICE_QUERY = """
PREFIX sdmxd: <http://purl.org/linked-data/sdmx/2009/dimension#>
PREFIX qb: <http://purl.org/linked-data/cube#>
PREFIX sm: <http://statistics.gov.scot/def/measure-properties/>
SELECT ?code ?median WHERE {
  ?obs qb:dataSet <http://statistics.gov.scot/data/residential-properties-sales-and-price> ;
       sdmxd:refArea ?a ;
       sdmxd:refPeriod <http://reference.data.gov.uk/id/year/%d> ;
       sm:median ?median .
  BIND(REPLACE(STR(?a), "^.*/", "") AS ?code)
  FILTER(STRSTARTS(?code, "S01"))
}
"""


def _fetch_scotland_prices() -> pd.DataFrame:
    """Median residential sale price per 2011 Data Zone, via SPARQL.

    Registers of Scotland's own site blocks automated requests, but the same
    statistics are published as linked data on statistics.gov.scot.
    """
    import requests

    cache = RAW / f"scotland_prices_{SCOTLAND_PRICE_YEAR}.csv"
    if not cache.exists():
        query = _SCOT_PRICE_QUERY % SCOTLAND_PRICE_YEAR
        resp = requests.post(_SPARQL_ENDPOINT, data={"query": query},
                             headers={"Accept": "text/csv"}, timeout=300)
        resp.raise_for_status()
        cache.write_bytes(resp.content)

    df = pd.read_csv(cache)
    return df.rename(columns={"code": "area_code_2011", "median": "median_price"})


def property_price_scotland() -> pd.DataFrame:
    """Median sale price per 2022 Data Zone, rebased from 2011 Data Zones.

    Two caveats worth stating plainly:

    * The Scottish cube stops at 2023, while the England & Wales figure pools
      2023-2025 transactions. Scottish prices are therefore a year or so
      "behind" and very slightly understated relative to the rest of GB.
    * The 2011 -> 2022 rebase weights by live postcode count. Where a zone did
      not change between vintages this is exact; where it split, the children
      inherit the parent's median, which is reasonable for an intensive
      quantity like a price.
    """
    prices = _fetch_scotland_prices()
    pcs = _scotland_postcodes()

    crosswalk = (pcs.groupby(["area_code", "area_code_2011"])
                    .size().rename("n_postcodes").reset_index())
    merged = crosswalk.merge(prices, on="area_code_2011", how="left").dropna(subset=["median_price"])

    merged["weighted"] = merged["median_price"] * merged["n_postcodes"]
    grouped = merged.groupby("area_code").agg(
        weighted=("weighted", "sum"), n_postcodes=("n_postcodes", "sum"))

    out = pd.DataFrame({"median_price": grouped["weighted"] / grouped["n_postcodes"]})
    out.index.name = "area_code"
    return out
