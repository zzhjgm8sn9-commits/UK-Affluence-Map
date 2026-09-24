"""Check the OSM branch list against an operator's own published list.

    python pipeline/verify_branches.py

OpenStreetMap is contributed data. It is good at recording that a branch
*exists* and poor at recording that one has *stopped* existing, because nobody
walks past a closed bank and thinks to delete it. UK branches have been closing
at pace, so the error is large and it is one-directional: the map over-counts.

Barclays is the clearest case. OSM carries 437 Barclays features in GB; Barclays
publishes 211 branches. Only four of the OSM records carry a `disused:` or
`abandoned:` tag, so tag-based filtering removes four of the roughly 210 that
have gone -- the rest are silently stale.

This step reads the operator's list, matches it against the OSM records, and
writes the OSM ids that no longer correspond to anything the operator admits to
running. build_branches.py drops them.


WHERE THE LIST COMES FROM
-------------------------
Barclays' own sitemap, which robots.txt names explicitly
(`Sitemap: https://www.barclays.co.uk/sitemap2.xml`) and which disallows
nothing under /branch-finder/. One request gets every branch URL; each branch
page is then fetched once and cached.

Only the postcode is taken from each page, and only to decide whether an
existing OSM record is still live. Nothing from Barclays is redistributed: the
published branch layer stays OSM-derived and ODbL, with closed entries removed.
A branch address is a fact about a shop front, and this uses it to delete
records rather than to create them.


WHAT IT CANNOT DO
-----------------
It removes; it never adds. A branch Barclays lists and OSM has never heard of
stays missing, because this step has no licence to mint new points from
Barclays' data and no coordinates to mint them from -- postcode centroids are
not branch locations. The count after filtering is therefore a floor on
accuracy, not a guarantee of it.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import INTERIM, RAW

SITEMAP = "https://www.barclays.co.uk/sitemap2.xml"
CACHE = RAW / "barclays_branches.json"
CLOSURES = INTERIM / "branch_closures.parquet"

# One request every 0.6s, identified honestly. 211 pages is four minutes at
# walking pace, and the cache means it happens once.
DELAY_S = 0.6
UA = "uk-affluence-map/0.1 (open data project; branch list verification)"

POSTCODE_RE = re.compile(r"\b([A-Z]{1,2}[0-9][A-Z0-9]?)\s*([0-9][A-Z]{2})\b")

# Barclays' registered office, in the footer of every page.
REGISTERED_OFFICE = "E14 5HP"
BRANCH_URL_RE = re.compile(
    r"<loc>(https://www\.barclays\.co\.uk/branch-finder/branch/[^<]*)</loc>")

KM_PER_DEG_LAT = 110.574

# Postcode centroids sit tens of metres from the address in towns and further
# in rural units, and OSM nodes sit on the building, so a match is never exact.
# Nine matches in ten land inside 80 m; the threshold only has to decide the
# tail, and every candidate beyond 250 m was read by hand to place it:
#
#     281 m  Chancery Lane, 326-328 High Holborn  <-> OSM Hatton Garden    same
#     458 m  Richmond, 8 George Street            <-> OSM George Street    same
#     614 m  Moorgate, 120 Moorgate               <-> OSM Ironmonger Lane  different
#    1288 m  Welwyn Garden City, Howardsgate      <-> OSM Ludwick Way      different
#    1453 m  South Shields, King Street           <-> OSM North Shields    different
#
# 500 m is the gap between the last true pair and the first false one. Erring
# low is not the safe direction here: a threshold that is too tight deletes a
# branch that is open, while one slightly too loose keeps a closed one. The
# one-to-one assignment does most of the work anyway -- a closed record near a
# live branch can only match if nothing closer has claimed it.
MATCH_METRES = 500


def branch_urls(session: requests.Session) -> list[str]:
    print(f"  fetching {SITEMAP}")
    resp = session.get(SITEMAP, timeout=60)
    resp.raise_for_status()
    urls = sorted(set(BRANCH_URL_RE.findall(resp.text)))
    print(f"  {len(urls)} branch pages listed")
    return urls


def parse_branch(html: str, url: str) -> dict | None:
    """Name and postcode from one branch page.

    The address is server-rendered -- no JSON-LD, no coordinates -- as a <p> of
    <br />-separated lines ending in the postcode. The last line is all that is
    needed: the project already holds every GB postcode with a centroid, so the
    operator's own geocoding is not required.

    Finding it by looking inside the "Address" tile is too brittle. A branch
    shut for refurbishment gets a different layout, where an alert block
    displaces the tile, and six pages parsed to nothing on that basis -- which
    would have marked six live branches closed. The shape of the address is
    steadier than its position: <br />-separated lines whose last is a
    postcode. Prose mentioning a nearby branch ("colleagues will be available
    to help you at 65 High Street Camberley GU15 3RB") has no line breaks and
    so cannot be mistaken for one, and the registered office in the footer is
    excluded by name.
    """
    title = re.search(r"<title>(.*?)</title>", html, re.S)
    name = title.group(1).strip() if title else url.rstrip("/").split("/")[-1]
    name = re.sub(r"^Branch\s*-\s*", "", name).strip()

    candidates = []
    for block in re.finditer(r"<p>((?:[^<]|<br\s*/?>)*?)</p>", html):
        raw = block.group(1)
        if not re.search(r"<br\s*/?>", raw):
            continue
        lines = [l.strip() for l in re.split(r"<br\s*/?>", raw) if l.strip()]
        if not lines:
            continue
        m = POSTCODE_RE.search(lines[-1].upper())
        if not m:
            continue
        postcode = f"{m.group(1)} {m.group(2)}"
        if postcode == REGISTERED_OFFICE:
            continue
        candidates.append((postcode, lines))

    if not candidates:
        return None
    postcode, lines = candidates[0]
    return {"name": name, "postcode": postcode, "url": url,
            "address": ", ".join(lines), "candidates": len(candidates)}


def fetch_all(force: bool = False) -> pd.DataFrame:
    if CACHE.exists() and not force:
        print(f"  cached: {CACHE.name}")
        return pd.DataFrame(json.loads(CACHE.read_text(encoding="utf-8")))

    session = requests.Session()
    session.headers.update({"User-Agent": UA})

    urls = branch_urls(session)
    rows, failed = [], []
    for i, url in enumerate(urls, 1):
        try:
            resp = session.get(url, timeout=60)
            resp.raise_for_status()
            rec = parse_branch(resp.text, url)
        except Exception as err:            # noqa: BLE001 - report and continue
            rec, err_text = None, str(err)[:60]
            failed.append((url, err_text))
        if rec:
            rows.append(rec)
        else:
            failed.append((url, "no postcode found"))
        if i % 25 == 0 or i == len(urls):
            print(f"\r    {i}/{len(urls)} pages, {len(rows)} parsed", end="", flush=True)
        time.sleep(DELAY_S)
    print()

    if failed:
        print(f"  {len(failed)} pages yielded nothing:")
        for url, why in failed[:8]:
            print(f"    {why}: {url}")

    CACHE.write_text(json.dumps(rows, indent=1), encoding="utf-8")
    return pd.DataFrame(rows)


def geocode(df: pd.DataFrame) -> pd.DataFrame:
    """Postcode -> centroid, from the lookup the project already holds."""
    pcs = pd.read_parquet(INTERIM / "postcodes.parquet",
                          columns=["pc_key", "lat", "lon"])
    df = df.copy()
    df["pc_key"] = df["postcode"].str.replace(" ", "", regex=False).str.upper()
    merged = df.merge(pcs, on="pc_key", how="left")
    # The lookup is Great Britain only, so Belfast, Jersey, Guernsey and the
    # Isle of Man drop out here. That is correct -- the map does not cover them
    # -- and it is the expected outcome rather than a failure.
    missing = merged[merged["lat"].isna()]
    if len(missing):
        print(f"  {len(missing)} outside GB, not on this map: "
              + ", ".join(sorted(missing["name"].astype(str))[:6]))
    return merged.dropna(subset=["lat", "lon"])


def match(osm: pd.DataFrame, live: pd.DataFrame) -> pd.DataFrame:
    """Greedy nearest-neighbour, one live branch to one OSM record.

    One-to-one matters: OSM often carries both a node and the building way for
    the same branch, and letting two records claim the same live branch would
    quietly keep a duplicate alive.
    """
    import math

    pairs = []
    for li, l in live.iterrows():
        scale = KM_PER_DEG_LAT * 1000 * math.cos(math.radians(l.lat))
        dx = (osm["lon"] - l.lon) * scale
        dy = (osm["lat"] - l.lat) * KM_PER_DEG_LAT * 1000
        dist = (dx ** 2 + dy ** 2) ** 0.5
        near = dist[dist <= MATCH_METRES]
        for oi, d in near.items():
            pairs.append((d, oi, li))

    pairs.sort()
    taken_osm, taken_live, out = set(), set(), []
    for d, oi, li in pairs:
        if oi in taken_osm or li in taken_live:
            continue
        taken_osm.add(oi)
        taken_live.add(li)
        out.append({"osm_index": oi, "live_index": li, "metres": d})
    return pd.DataFrame(out)


def main() -> None:
    force = "--force" in sys.argv[1:]

    print("Barclays published branch list:")
    live = fetch_all(force=force)
    print(f"  {len(live):,} branches with a postcode")
    live = geocode(live)
    print(f"  {len(live):,} geocoded")

    # The unfiltered set, deliberately: reading the filtered one would be
    # circular, and reading the raw OSM dump would undo the node/way dedupe and
    # let a live branch's twin be marked closed.
    osm_path = INTERIM / "gb_branches_all.parquet"
    if not osm_path.exists():
        raise SystemExit("run pipeline/build_branches.py first "
                         "(it writes gb_branches_all.parquet)")
    all_osm = pd.read_parquet(osm_path)
    osm = all_osm[all_osm["brand_group"] == "Barclays"].copy()
    print(f"\nOSM records for Barclays: {len(osm):,}")

    matched = match(osm, live)
    print(f"  matched to a published branch: {len(matched):,}")
    if len(matched):
        q = matched["metres"].quantile([0.5, 0.9, 0.99]).round(0)
        print(f"  match distance: median {q[0.5]:.0f} m, "
              f"90th {q[0.9]:.0f} m, 99th {q[0.99]:.0f} m")

    closed = osm.drop(index=matched["osm_index"])
    unmatched_live = len(live) - len(matched)
    print(f"\n  {len(closed):,} OSM records match nothing Barclays publishes "
          f"-> treated as closed")
    print(f"  {unmatched_live:,} published branches have no OSM record "
          f"-> missing from the map, and this step cannot add them")

    out = pd.DataFrame({
        "osm_id": closed["osm_id"].values,
        "brand_group": "Barclays",
        "reason": "absent from the operator's published branch list",
    })
    out.to_parquet(CLOSURES)
    print(f"\nwrote {CLOSURES.relative_to(CLOSURES.parent.parent.parent)} "
          f"({len(out):,} ids)")
    print(f"  Barclays on the map after filtering: {len(osm) - len(closed):,}")


if __name__ == "__main__":
    main()
