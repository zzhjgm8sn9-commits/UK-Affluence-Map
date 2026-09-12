"""Build the postcode -> small-area lookup from the ONS Postcode Directory.

The ONSPD is the hinge of the whole project. It is what lets postcode-level
sources (Land Registry transactions) be aggregated onto the statistical
geography that actually has data published against it, and it is what will let
the map answer "what is this postcode like?" without pretending postcode-level
estimates exist.

One convenient accident of the ONSPD's design: its `lsoa21cd` column carries
Scottish Data Zone codes for Scottish postcodes, and those match the 2022 Data
Zone codes in our backbone exactly (all 7,392). So a single column gives a
unified GB lookup across two different national geographies.

The source CSV is 1.48 GB inside the zip, so it is streamed rather than loaded.

Output:
  data/interim/postcodes.parquet   live GB postcodes -> area_code, lat, lon
"""

from __future__ import annotations

import csv
import io
import sys
import time
import zipfile
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from config import INTERIM, RAW

ONSPD_ZIP = RAW / "ONSPD_AUG_2026.zip"
ONSPD_CSV = "Data/ONSPD_AUG_2026_UK.csv"

# Country codes for England, Wales and Scotland. Northern Ireland is out of
# scope: it uses its own geography and has no comparable source data.
GB_COUNTRIES = {"E92000001", "W92000004", "S92000003"}


def main() -> None:
    t0 = time.time()
    if not ONSPD_ZIP.exists():
        raise SystemExit(f"missing {ONSPD_ZIP}")

    postcodes: list[str] = []
    areas: list[str] = []
    oas: list[str] = []
    areas11: list[str] = []
    pcons: list[str] = []
    msoas: list[str] = []
    lats: list[float] = []
    lons: list[float] = []

    total = kept = 0
    with zipfile.ZipFile(ONSPD_ZIP) as z, z.open(ONSPD_CSV) as fh:
        reader = csv.reader(io.TextIOWrapper(fh, "latin-1", newline=""))
        cols = next(reader)
        i_pcds = cols.index("pcds")
        i_area = cols.index("lsoa21cd")
        i_ctry = cols.index("ctry26cd")
        i_term = cols.index("doterm")
        i_lat = cols.index("lat")
        i_lon = cols.index("long")
        # Needed to rebase Scottish sources published on older geographies:
        # oa21cd for the census bulk files, lsoa11cd for the price cube.
        i_oa = cols.index("oa21cd")
        i_area11 = cols.index("lsoa11cd")
        # Parliamentary constituency: the finest geography at which HMRC
        # publishes individual income, so it anchors the income model.
        i_pcon = cols.index("pcon24cd")
        # MSOA is only needed to validate against ONS income estimates,
        # which are published at that level.
        i_msoa = cols.index("msoa21cd")

        for row in reader:
            total += 1
            # doterm is the termination date: non-empty means the postcode is
            # no longer live, so it should not attract transactions or counts.
            if row[i_term] or row[i_ctry] not in GB_COUNTRIES or not row[i_area]:
                continue
            postcodes.append(row[i_pcds])
            areas.append(row[i_area])
            oas.append(row[i_oa])
            areas11.append(row[i_area11])
            pcons.append(row[i_pcon])
            msoas.append(row[i_msoa])
            lats.append(float(row[i_lat]))
            lons.append(float(row[i_lon]))
            kept += 1
            if kept % 250_000 == 0:
                print(f"\r  {kept:,} live GB postcodes ({total:,} scanned)", end="", flush=True)
    print(f"\r  {kept:,} live GB postcodes ({total:,} scanned)")

    df = pd.DataFrame({
        "postcode": postcodes,
        "area_code": areas,
        "oa_code": oas,
        "area_code_2011": areas11,
        "pcon_code": pcons,
        "msoa_code": msoas,
        "lat": lats,
        "lon": lons,
    })

    # Normalised join key: uppercase, no spaces. Land Registry formats its
    # postcodes differently from the ONSPD, so neither side can be trusted to
    # match on the printed form.
    df["pc_key"] = df["postcode"].str.upper().str.replace(" ", "", regex=False)

    # ONSPD pseudo-codes (L99999999 etc.) mark postcodes with no real area.
    df = df[df["area_code"].str.match(r"^[EWS]01")]

    print(f"\nby nation:\n{df['area_code'].str[0].value_counts().to_string()}")
    print(f"distinct areas covered: {df['area_code'].nunique():,}")

    dest = INTERIM / "postcodes.parquet"
    df.to_parquet(dest, index=False)
    print(f"\nwrote {dest} ({dest.stat().st_size / 1e6:.1f} MB)")
    print(f"done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
