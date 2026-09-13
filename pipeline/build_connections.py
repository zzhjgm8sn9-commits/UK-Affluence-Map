"""A travel-connections rating out of 10 for every branch.

Real drive-time isochrones need a routing engine and a road network, and they
answer a different question anyway -- how far can a car get, which for a high
street bank is often the least interesting mode. This is a deliberately simpler
proxy: **how well connected is this branch, and how many people are within a
walk of it?**

Four components, each scored 0-1 and then weighted:

| Component | Weight | What it measures |
|---|---|---|
| Rail, metro or tram proximity | 3 | distance to the nearest station |
| People within a walk | 3 | adults within 800 m |
| Mode diversity | 2 | how many distinct modes within 800 m |
| Bus stop density | 2 | bus stops within 500 m |

Mode diversity is scored to reward the *second* mode most: somewhere with a bus
and a train is far better connected than somewhere with two bus routes, which
is the point the weighting has to capture.

**Source.** NaPTAN, the Department for Transport's register of every public
transport access point in Great Britain. It is Open Government Licence, unlike
the OpenStreetMap layers, and its StopType field distinguishes modes directly
rather than requiring them to be inferred from tags.

Output:
  data/interim/gb_connections.parquet
  (merged into web/data/gb_branches.json by build_branches.py)
"""

from __future__ import annotations

import math
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from config import INTERIM, RAW, WEB

NAPTAN_FILE = "naptan.csv"

WALK_M = 800     # about a ten minute walk
BUS_M = 500      # a stop this close is genuinely "at the door"

# NaPTAN StopType codes, grouped into the modes a passenger would recognise.
MODE_BY_STOPTYPE = {
    "BCT": "bus", "BCS": "bus", "BCQ": "bus", "BST": "bus", "BCE": "bus",
    "RSE": "rail", "RLY": "rail", "RPL": "rail",
    "MET": "tram", "PLT": "tram", "TMU": "tram",
    "FER": "ferry", "FBT": "ferry", "FTD": "ferry",
}
RAIL_LIKE = {"rail", "tram"}

WEIGHTS = {"rail": 3.0, "people": 3.0, "modes": 2.0, "bus": 2.0}

# Saturation points, chosen so a well-served high street reaches the top of the
# scale rather than the very best site in the country defining it.
BUS_SATURATION = 12         # stops within 500 m
PEOPLE_SATURATION = 12_000  # adults within 800 m

GRID_M = 1000
KM_PER_DEG_LAT = 110.574


def _xy(lat, lon):
    return (lon * KM_PER_DEG_LAT * 1000 * math.cos(math.radians(lat)),
            lat * KM_PER_DEG_LAT * 1000)


def build_grid(points):
    """points: iterable of (x, y, payload). Returns cell -> list of points."""
    grid: dict[tuple[int, int], list] = {}
    for x, y, payload in points:
        grid.setdefault((int(x // GRID_M), int(y // GRID_M)), []).append((x, y, payload))
    return grid


def query(grid, x, y, radius):
    """Every indexed point within `radius` metres of (x, y), with its distance."""
    out = []
    cells = int(radius // GRID_M) + 1
    cx, cy = int(x // GRID_M), int(y // GRID_M)
    r2 = radius * radius
    for dx in range(-cells, cells + 1):
        for dy in range(-cells, cells + 1):
            for px, py, payload in grid.get((cx + dx, cy + dy), ()):
                d2 = (px - x) ** 2 + (py - y) ** 2
                if d2 <= r2:
                    out.append((math.sqrt(d2), payload))
    return out


def nearest(grid, x, y, max_radius, predicate=None):
    """Distance to the closest matching point, or None inside max_radius."""
    best = None
    radius = 500.0
    while radius <= max_radius:
        for d, payload in query(grid, x, y, radius):
            if predicate and not predicate(payload):
                continue
            if best is None or d < best:
                best = d
        if best is not None:
            return best
        radius *= 2
    return None


def load_naptan() -> pd.DataFrame:
    cols = ["Longitude", "Latitude", "StopType", "Status"]
    df = pd.read_csv(RAW / NAPTAN_FILE, usecols=cols, low_memory=False)
    print(f"  NaPTAN rows: {len(df):,}")

    df = df[df["Status"].astype(str).str.lower() == "active"]
    df["mode"] = df["StopType"].map(MODE_BY_STOPTYPE)
    df = df.dropna(subset=["mode", "Longitude", "Latitude"])
    print(f"  active nodes in a recognised mode: {len(df):,}")
    print("  " + "  ".join(f"{k}={v:,}" for k, v in df["mode"].value_counts().items()))
    return df


def walkable_population(branches: pd.DataFrame) -> dict[str, float]:
    """Adults living within WALK_M of each branch, apportioned by area overlap.

    The obvious implementation -- sum the adults of every area whose centroid
    falls within the radius -- is badly wrong at this scale. Small areas can be
    two kilometres across, so a branch in the middle of a town centre often has
    no centroid within 800 m at all and scores zero despite being surrounded by
    people. It happened to 104 branches, including a High Street site with
    eleven bus stops at the door.

    Instead each branch gets a buffer, and every area it touches contributes its
    adults in proportion to how much of it falls inside. That assumes population
    is spread evenly within an area, which is the standard assumption and far
    closer to the truth than sampling a single point.
    """
    import geopandas as gpd
    from shapely.geometry import Point

    areas = gpd.read_parquet(INTERIM / "gb_areas.parquet")[["area_code", "geometry"]]
    adults = pd.read_parquet(INTERIM / "gb_income.parquet", columns=["adults"])
    areas = areas.merge(adults, left_on="area_code", right_index=True, how="inner")
    areas = areas.to_crs("EPSG:27700").reset_index(drop=True)
    areas["area_m2"] = areas.geometry.area
    print(f"  {len(areas):,} areas with geometry and an adult count")

    pts = gpd.GeoDataFrame(
        {"osm_id": branches["osm_id"].values},
        geometry=[Point(xy) for xy in zip(branches["lon"], branches["lat"])],
        crs="EPSG:4326",
    ).to_crs("EPSG:27700")
    buffers = pts.assign(geometry=pts.geometry.buffer(WALK_M))

    pairs = gpd.sjoin(buffers, areas, predicate="intersects", how="inner")
    print(f"  {len(pairs):,} branch/area overlaps within {WALK_M} m")

    right = areas.loc[pairs["index_right"].values]
    overlap = pairs.geometry.values.intersection(right.geometry.values).area
    share = overlap / right["area_m2"].values
    contribution = share * right["adults"].values

    out: dict[str, float] = {}
    for osm_id, value in zip(pairs["osm_id"].values, contribution):
        out[osm_id] = out.get(osm_id, 0.0) + float(value)

    missing = len(branches) - len(out)
    print(f"  {len(out):,} branches with a walkable population"
          + (f"; {missing} with none" if missing else ""))
    return out


def rail_score(distance_m: float | None) -> float:
    """Piecewise on distance. A station at the door is worth a lot; three
    kilometres away is worth almost nothing to someone on foot."""
    if distance_m is None:
        return 0.0
    if distance_m <= 300:
        return 1.0
    if distance_m <= 1500:
        return 1.0 - 0.65 * (distance_m - 300) / 1200
    if distance_m <= 5000:
        return 0.35 * (1 - (distance_m - 1500) / 3500)
    return 0.0


def mode_score(n_modes: int) -> float:
    """Rewards the second mode most: a bus and a train beats two bus routes."""
    return {0: 0.0, 1: 0.30, 2: 0.65, 3: 0.90}.get(n_modes, 1.0)


def saturating(value: float, saturation: float) -> float:
    """Square-root so the first few stops or thousand people count for most."""
    return min(1.0, math.sqrt(max(value, 0.0) / saturation))


def main() -> None:
    t0 = time.time()

    print("transport nodes:")
    stops = load_naptan()
    stop_grid = build_grid(
        (*_xy(r.Latitude, r.Longitude), r.mode) for r in stops.itertuples())

    branches = pd.read_parquet(INTERIM / "gb_branches.parquet")

    print("\npopulation:")
    walkable = walkable_population(branches)

    print(f"\nscoring {len(branches):,} branches:")

    rows = []
    no_people = 0
    for b in branches.itertuples():
        x, y = _xy(b.lat, b.lon)

        d_rail = nearest(stop_grid, x, y, 6000, lambda m: m in RAIL_LIKE)
        near_walk = query(stop_grid, x, y, WALK_M)
        modes = {m for _, m in near_walk}
        n_bus = sum(1 for d, m in near_walk if m == "bus" and d <= BUS_M)
        people = walkable.get(b.osm_id, 0.0)
        if people == 0:
            no_people += 1

        parts = {
            "rail": rail_score(d_rail),
            "people": saturating(people, PEOPLE_SATURATION),
            "modes": mode_score(len(modes)),
            "bus": saturating(n_bus, BUS_SATURATION),
        }
        total = sum(parts[k] * w for k, w in WEIGHTS.items()) / sum(WEIGHTS.values())

        rows.append({
            "osm_id": b.osm_id,
            "connections": round(10 * total, 1),
            "rail_m": None if d_rail is None else round(d_rail),
            "n_modes": len(modes),
            "n_bus_500m": n_bus,
            "adults_800m": round(people),
        })

    out = pd.DataFrame(rows).set_index("osm_id")
    if no_people:
        print(f"  {no_people} branches have no area centroid within {WALK_M} m "
              f"-- scored 0 on the population component")

    print(f"\nrating distribution:")
    print(out["connections"].describe(percentiles=[.1, .25, .5, .75, .9]).round(2).to_string())

    out.to_parquet(INTERIM / "gb_connections.parquet")
    print(f"\nwrote {INTERIM / 'gb_connections.parquet'}")
    merge_into_web_payload(branches, out)
    print(f"done in {time.time() - t0:.0f}s")


def merge_into_web_payload(branches: pd.DataFrame, scores: pd.DataFrame) -> None:
    """Add the rating to web/data/gb_branches.json.

    The scores are written back rather than produced by build_branches.py
    because this step depends on that one's output -- the branch list has to
    exist before anything can be scored against it. Rows are matched by
    position, which is safe because both the parquet and the JSON are written
    from the same frame in the same order; the length check makes that
    assumption fail loudly rather than silently mislabelling every branch.
    """
    import json

    dest = WEB / "data" / "gb_branches.json"
    if not dest.exists():
        print("  (no gb_branches.json yet; run build_branches.py first)")
        return

    payload = json.loads(dest.read_text(encoding="utf-8"))
    if len(payload["branches"]) != len(branches):
        raise SystemExit(
            f"gb_branches.json has {len(payload['branches'])} branches but the "
            f"parquet has {len(branches)}; re-run build_branches.py first")

    ordered = scores.loc[branches["osm_id"].values]
    for entry, row in zip(payload["branches"], ordered.itertuples()):
        entry["c"] = row.connections
        entry["cd"] = [
            None if row.rail_m is None or pd.isna(row.rail_m) else int(row.rail_m),
            int(row.n_modes), int(row.n_bus_500m), int(row.adults_800m),
        ]

    dest.write_text(json.dumps(payload, separators=(",", ":")))
    print(f"  merged ratings into {dest} ({dest.stat().st_size / 1e6:.2f} MB)")


if __name__ == "__main__":
    main()
