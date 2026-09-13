"""Search indexes: postcodes, places and the data the UI needs to find things.

Three things people want to search for, and they need very different handling.

**Postcodes** are the hard one. There are 1.75 million live GB postcodes, which
is far too much to ship with the page for a search box that most sessions will
never use. They are split into one file per postcode area (AB, AL, B, ...
120 of them) and fetched only when someone actually types a postcode starting
with those letters. The largest, B, holds about 42,000 postcodes; most are far
smaller.

Each entry resolves to a small area, so a postcode search selects the area whose
statistics the map is showing -- the honest interaction, given nothing here is
estimated at postcode-unit level. The postcode's own coordinates are kept too,
so the map can drop a pin on the exact spot rather than the middle of an LSOA.

**Places** ride along in a single small file. The label layer only carries the
151 most prominent, but search should find anywhere worth typing, so all 1,669
OSM cities and towns are included.

**Branches** need no index at all -- the map already holds all 4,698 with their
street labels, so the UI filters them in memory.

Output:
  web/data/postcodes/<AREA>.json
  web/data/gb_search.json
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from config import INTERIM, RAW, WEB

COORD_DP = 4          # ~11 m, ample for a postcode centroid
PLACES_OSM = "osm_places_gb.json"


def build_postcode_chunks(area_index: dict[str, int]) -> None:
    pcs = pd.read_parquet(INTERIM / "postcodes.parquet",
                          columns=["postcode", "area_code", "lat", "lon"])
    print(f"  {len(pcs):,} live GB postcodes")

    # Keys are stored space-free, because the search box normalises what the
    # user types the same way. Keeping the printed form here would mean
    # "SW1A 1AA" normalised to "SW1A1AA" never matching a stored "1A 1AA" --
    # every full postcode would silently find nothing. The space is put back
    # for display in the browser.
    pcs["norm"] = pcs["postcode"].str.replace(" ", "", regex=False)

    # The outward code's letters are the chunk key: "SW1A1AA" -> "SW".
    pcs["chunk"] = pcs["norm"].str.extract(r"^([A-Z]{1,2})")[0]
    pcs = pcs.dropna(subset=["chunk"])

    # Stored without the chunk prefix, since every key in a file shares it.
    pcs["rest"] = [p[len(c):] for p, c in zip(pcs["norm"], pcs["chunk"])]
    pcs["idx"] = pcs["area_code"].map(area_index)
    missing = pcs["idx"].isna().sum()
    if missing:
        print(f"  WARNING {missing:,} postcodes reference an unknown area; dropped")
        pcs = pcs.dropna(subset=["idx"])

    out_dir = WEB / "data" / "postcodes"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    total_bytes = 0
    sizes = []
    for chunk, group in pcs.groupby("chunk", sort=True):
        group = group.sort_values("rest")
        payload = {
            "prefix": chunk,
            "rest": group["rest"].tolist(),
            "area": [int(v) for v in group["idx"]],
            "lat": [round(float(v), COORD_DP) for v in group["lat"]],
            "lon": [round(float(v), COORD_DP) for v in group["lon"]],
        }
        dest = out_dir / f"{chunk}.json"
        dest.write_text(json.dumps(payload, separators=(",", ":")))
        total_bytes += dest.stat().st_size
        sizes.append((chunk, len(group), dest.stat().st_size))

    sizes.sort(key=lambda s: -s[2])
    print(f"  wrote {len(sizes)} chunks, {total_bytes / 1e6:.1f} MB total")
    print("  largest:")
    for chunk, n, size in sizes[:5]:
        print(f"    {chunk:<3} {n:>7,} postcodes  {size / 1e3:>7,.0f} KB")
    print(f"  median chunk: {sorted(s[2] for s in sizes)[len(sizes)//2] / 1e3:.0f} KB")


def build_place_index() -> list[dict]:
    """Every OSM city and town, not just the ones prominent enough to label."""
    payload = json.loads((RAW / PLACES_OSM).read_text(encoding="utf-8"))
    rows = []
    for el in payload["elements"]:
        tags = el.get("tags", {})
        name = tags.get("name")
        if not name or "lat" not in el:
            continue
        pop = re.sub(r"[^\d]", "", str(tags.get("population") or "")) or "0"
        rows.append({
            "n": name,
            "k": tags.get("place"),
            "p": int(pop),
            "y": round(el["lat"], COORD_DP),
            "x": round(el["lon"], COORD_DP),
        })

    # Northern Ireland places are filtered out by longitude/latitude box rather
    # than a spatial join: this list is only used to pan the map, so a couple of
    # border misses cost nothing and it keeps the step fast.
    rows = [r for r in rows if not (r["x"] < -5.3 and 54.0 < r["y"] < 55.4)]
    rows.sort(key=lambda r: -r["p"])
    print(f"  {len(rows):,} places for search "
          f"({sum(1 for r in rows if r['k'] == 'city')} cities)")
    return rows


def main() -> None:
    t0 = time.time()

    # Area order must match gb_metrics.json's `codes`, since the postcode files
    # store positions in it rather than repeating 43,064 area codes 1.75m times.
    metrics = pd.read_parquet(INTERIM / "gb_metrics.parquet", columns=[])
    area_codes = metrics.index.tolist()
    area_index = {code: i for i, code in enumerate(area_codes)}
    print(f"areas: {len(area_codes):,}")

    print("\npostcodes:")
    build_postcode_chunks(area_index)

    print("\nplaces:")
    places = build_place_index()

    dest = WEB / "data" / "gb_search.json"
    dest.write_text(json.dumps({"places": places}, separators=(",", ":")))
    print(f"  wrote {dest} ({dest.stat().st_size / 1e3:.0f} KB)")

    print(f"\ndone in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
