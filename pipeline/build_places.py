"""Place labels, so the map is navigable without a basemap.

A choropleth of 43,064 anonymous polygons is hard to orient in. A sparse set of
city and town labels fixes that without the visual noise of a full basemap
underneath the colours.

**Source and licence.** OpenStreetMap, same as the branch layer, and the same
ODbL share-alike caveat applies. ONS publishes Major Towns and Cities under OGL,
which would be a cleaner licence, but it covers England and Wales only -- no
Glasgow, Edinburgh, Aberdeen or Dundee -- so it cannot label a GB map on its
own. Keeping labels on the same source as branches at least confines the ODbL
footprint to two clearly marked files.

Ranking rather than fixed zoom thresholds: every place carries a rank, and the
map hands that to MapLibre as a symbol sort key. Collision detection then drops
the least important labels wherever things get crowded, so the map thins itself
out naturally as you zoom instead of switching layers on at arbitrary levels.

Output:
  web/data/gb_places.json
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import Point

sys.path.insert(0, str(Path(__file__).parent))

from config import INTERIM, RAW, WEB, WGS84

OSM_FILE = "osm_places_gb.json"

# MapLibre cannot draw a single character without a glyph source. Rather than
# point the style at a public font server -- MapLibre's own is explicitly a demo
# and the openmaptiles one no longer serves these -- the two Latin ranges are
# vendored into web/fonts once and served from disk. Keeps the map working
# offline, which the rest of the pipeline already manages.
GLYPH_SOURCE = "https://demotiles.maplibre.org/font"
FONT_STACKS = ("Noto Sans Regular", "Noto Sans Bold")
GLYPH_RANGES = ("0-255", "256-511")   # Latin-1 plus Latin Extended-A for Welsh


def fetch_glyphs() -> None:
    import urllib.parse

    from download import SESSION   # shares the project's User-Agent

    for stack in FONT_STACKS:
        dest_dir = WEB / "fonts" / stack
        dest_dir.mkdir(parents=True, exist_ok=True)
        for rng in GLYPH_RANGES:
            dest = dest_dir / f"{rng}.pbf"
            if dest.exists():
                print(f"  cached  {stack}/{rng}.pbf")
                continue
            url = f"{GLYPH_SOURCE}/{urllib.parse.quote(stack)}/{rng}.pbf"
            resp = SESSION.get(url, timeout=60)
            resp.raise_for_status()
            dest.write_bytes(resp.content)
            print(f"  fetched {stack}/{rng}.pbf ({dest.stat().st_size / 1e3:.0f} KB)")

# Towns below this are left off; cities are kept regardless of whether they
# carry a population tag, since city status is itself the signal.
MIN_TOWN_POPULATION = 60_000
MAX_PLACES = 220


def parse_population(value) -> float:
    if not value:
        return 0.0
    digits = re.sub(r"[^\d]", "", str(value))
    return float(digits) if digits else 0.0


def main() -> None:
    t0 = time.time()
    print("glyphs:")
    fetch_glyphs()

    payload = json.loads((RAW / OSM_FILE).read_text(encoding="utf-8"))

    rows = []
    for el in payload["elements"]:
        tags = el.get("tags", {})
        name = tags.get("name")
        if not name or "lat" not in el:
            continue
        rows.append({
            "name": name,
            "kind": tags.get("place"),
            "population": parse_population(tags.get("population")),
            "lat": el["lat"],
            "lon": el["lon"],
        })

    df = pd.DataFrame(rows)
    print(f"OSM places: {len(df):,} "
          f"({(df.kind == 'city').sum()} cities, {(df.kind == 'town').sum()} towns)")

    keep = (df["kind"] == "city") | (df["population"] >= MIN_TOWN_POPULATION)
    df = df[keep].copy()
    print(f"  after the town population floor ({MIN_TOWN_POPULATION:,}): {len(df):,}")

    # Drop Northern Ireland the same way branches do -- by keeping only places
    # that land inside the GB backbone.
    areas = gpd.read_parquet(INTERIM / "gb_areas.parquet")[["area_code", "geometry"]]
    pts = gpd.GeoDataFrame(
        df, geometry=[Point(xy) for xy in zip(df["lon"], df["lat"])], crs=WGS84)
    joined = gpd.sjoin(pts, areas, how="left", predicate="within")
    joined = joined[~joined.index.duplicated()]
    dropped = joined["area_code"].isna().sum()
    df = joined.dropna(subset=["area_code"]).drop(columns=["geometry", "index_right"])
    print(f"  {dropped} outside GB dropped; {len(df):,} retained")

    # Cities outrank towns of the same size: a small cathedral city is a better
    # landmark than a larger commuter town.
    df["score"] = df["population"] + (df["kind"] == "city") * 250_000
    df = df.sort_values("score", ascending=False).head(MAX_PLACES).reset_index(drop=True)
    df["rank"] = df.index

    out = {
        "attribution": "© OpenStreetMap contributors (ODbL)",
        "places": [
            {"n": r.name, "y": round(r.lat, 4), "x": round(r.lon, 4),
             "r": int(r.rank), "k": r.kind}
            for r in df.itertuples()
        ],
    }
    dest = WEB / "data" / "gb_places.json"
    dest.write_text(json.dumps(out, separators=(",", ":")))
    print(f"\nwrote {dest} ({dest.stat().st_size / 1e3:.0f} KB, {len(df)} places)")
    print("\nmost prominent:")
    for r in df.head(14).itertuples():
        print(f"  {r.rank:>3}  {r.name:<22} {r.kind:<5} pop {int(r.population):>9,}")
    print(f"\ndone in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
