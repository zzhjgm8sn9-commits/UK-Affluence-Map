"""Check the OSM branch list against each operator's own published list.

    python pipeline/verify_branches.py [--force] [--only "Barclays,HSBC UK"]

OpenStreetMap is contributed data. It is good at recording that a branch
*exists* and poor at recording that one has *stopped* existing, because nobody
walks past a closed bank and thinks to delete it. UK branches have been closing
at pace, so the error is large and it is one-directional: the map over-counts,
and it over-counts each brand by a different amount, which is worse than a
uniform error because it distorts every comparison between them.

This step reads each operator's published branch list, matches it against the
OSM records for that brand, and writes the OSM ids that no longer correspond to
anything the operator admits to running. build_branches.py drops them.


WHERE THE LISTS COME FROM
-------------------------
Every source is a page or endpoint the operator serves to the public, reached by
the route its own site uses, with nothing in robots.txt disallowing it:

  Barclays            sitemap -> branch pages; address <p>, postcode only
  Lloyds, Halifax,    one shared Yext-hosted locator behind three domains;
  Bank of Scotland      schema.org microdata, brand read from the page title
  TSB                 Yext-hosted locator; microdata
  Nationwide          /branches/ pages; microdata
  HSBC UK             /branch-list/ pages; JSON-LD, location type in the slug
  NatWest, RBS        the locator's own search endpoint on natwest.com, which
                        returns the nearest 50 and so is tiled across GB
  Metro Bank          store pages; postcode only

Two are deliberately not verified:

  Santander           the locator sits behind Imperva bot protection
  Virgin Money        branch data comes from a third-party store-locator API
                        called with Virgin Money's own key

Neither is a technical obstacle so much as a line. Getting past bot detection,
or borrowing another company's API credentials, is a different act from reading
a page the operator publishes, and the answer does not change because the data
behind it would be useful. Both brands stay OSM-only and are labelled so.


WHAT IS TAKEN, AND WHAT IS DONE WITH IT
---------------------------------------
A postcode, and where the operator publishes one, a coordinate -- used only to
decide whether an OSM record still corresponds to something. Nothing from any
operator is written to the published branch layer: it stays OSM-derived and
ODbL, with closed entries removed. An operator's list is used here to delete
records, never to create them.

It therefore cannot fix omissions. A branch an operator lists and OSM has never
heard of stays missing -- there is no licence to mint points from the
operator's data, and adding them would change what the published layer is. The
count after filtering is a floor on accuracy, not a guarantee of it.
"""

from __future__ import annotations

import json
import math
import re
import sys
import time
import urllib.parse
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import INTERIM, RAW

CACHE_DIR = RAW / "operator_branches"
CLOSURES = INTERIM / "branch_closures.parquet"
SUMMARY = INTERIM / "branch_verification.json"

# One request every 0.6 s per source, identified honestly. The cache means each
# site is walked once; --force re-walks it.
DELAY_S = 0.6
UA = "uk-affluence-map/0.1 (open data project; branch list verification)"

POSTCODE_RE = re.compile(r"\b([A-Z]{1,2}[0-9][A-Z0-9]?)\s*([0-9][A-Z]{2})\b")
LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>")

KM_PER_DEG_LAT = 110.574

# Operator coordinates and postcode centroids sit tens of metres from the OSM
# node in towns and further in rural areas, so a match is never exact. For
# Barclays, where only postcodes are published, every candidate beyond 250 m
# was read by hand:
#
#     281 m  Chancery Lane, 326-328 High Holborn  <-> OSM Hatton Garden    same
#     458 m  Richmond, 8 George Street            <-> OSM George Street    same
#     614 m  Moorgate, 120 Moorgate               <-> OSM Ironmonger Lane  different
#    1288 m  Welwyn Garden City, Howardsgate      <-> OSM Ludwick Way      different
#    1453 m  South Shields, King Street           <-> OSM North Shields    different
#
# 500 m is the gap between the last true pair and the first false one, and it
# is conservative for the operators that publish coordinates, whose matches sit
# far tighter. Erring low is not the safe direction: too tight deletes a branch
# that is open, too loose keeps one that is shut. The one-to-one assignment
# does most of the work -- a closed record near a live branch can only match if
# nothing closer has claimed it.
MATCH_METRES = 500

UNVERIFIABLE = {
    "Santander": "locator is behind Imperva bot protection",
    "Virgin Money": "branch data is a third-party API called with Virgin Money's own key",
}


# ---------------------------------------------------------------- fetching


class Fetcher:
    """One polite session: fixed delay, honest user agent, no retries on 4xx."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": UA})
        self.last = 0.0

    def get(self, url: str, **kw) -> requests.Response | None:
        wait = DELAY_S - (time.monotonic() - self.last)
        if wait > 0:
            time.sleep(wait)
        self.last = time.monotonic()
        try:
            resp = self.session.get(url, timeout=60, **kw)
        except requests.RequestException:
            return None
        return resp if resp.ok else None

    def locs(self, sitemap: str) -> list[str]:
        resp = self.get(sitemap)
        if resp is None:
            raise SystemExit(f"  could not read {sitemap}")
        urls = LOC_RE.findall(resp.text)
        if "<sitemapindex" in resp.text:
            children, urls = urls, []
            for child in children:
                r = self.get(child)
                if r is not None:
                    urls += LOC_RE.findall(r.text)
        return urls


def walk(fetch: Fetcher, urls: list[str], parse, label: str) -> list[dict]:
    rows, failed = [], 0
    for i, url in enumerate(urls, 1):
        resp = fetch.get(url)
        rec = parse(resp.text, url) if resp is not None else None
        if rec:
            rows.append(rec)
        else:
            failed += 1
        if i % 50 == 0 or i == len(urls):
            print(f"\r    {label}: {i}/{len(urls)} pages, {len(rows)} usable",
                  end="", flush=True)
    print()
    if failed:
        print(f"    {failed} pages yielded no location (index pages, errors, "
              f"or not a branch)")
    return rows


def postcode_of(text: str) -> str | None:
    m = POSTCODE_RE.search((text or "").upper())
    return f"{m.group(1)} {m.group(2)}" if m else None


# ----------------------------------------------------------------- parsers


def parse_microdata(html: str) -> dict | None:
    """schema.org microdata, as Yext-hosted locators emit it.

    Pages also list nearby branches, so the *first* geo on the page is taken:
    it belongs to the page's own entity, which is rendered before the list.
    """
    lat = re.search(r'itemprop="latitude"\s+content="(-?[0-9.]+)"', html)
    lon = re.search(r'itemprop="longitude"\s+content="(-?[0-9.]+)"', html)
    pc = re.search(r'itemprop="postalCode"[^>]*>\s*([^<]+?)\s*<', html)
    if not (lat and lon):
        return None
    return {"lat": float(lat.group(1)), "lon": float(lon.group(1)),
            "postcode": postcode_of(pc.group(1)) if pc else None}


def title_of(html: str) -> str:
    m = re.search(r"<title>(.*?)</title>", html, re.S)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


# ------------------------------------------------------------------ sources
#
# Each returns rows of {brand_group, name, postcode, lat, lon}. lat/lon may be
# None where only a postcode is published; geocode() fills those from ONSPD.


BARCLAYS_REGISTERED_OFFICE = "E14 5HP"


def source_barclays(fetch: Fetcher) -> list[dict]:
    """Barclays: sitemap -> branch pages. No JSON-LD, no coordinates.

    The address is a <p> of <br />-separated lines ending in the postcode.
    Found by shape rather than position: a branch shut for refurbishment gets a
    different layout, and six pages parsed to nothing on the first attempt --
    which would have marked six live branches closed. Prose naming a nearby
    branch has no line breaks, and the registered office in the footer is
    excluded by name.
    """
    urls = sorted({u for u in fetch.locs("https://www.barclays.co.uk/sitemap2.xml")
                   if "/branch-finder/branch/" in u})

    def parse(html, url):
        for block in re.finditer(r"<p>((?:[^<]|<br\s*/?>)*?)</p>", html):
            raw = block.group(1)
            if not re.search(r"<br\s*/?>", raw):
                continue
            lines = [l.strip() for l in re.split(r"<br\s*/?>", raw) if l.strip()]
            pc = postcode_of(lines[-1]) if lines else None
            if pc and pc != BARCLAYS_REGISTERED_OFFICE:
                name = re.sub(r"^Branch\s*-\s*", "", title_of(html))
                return {"brand_group": "Barclays", "name": name, "postcode": pc,
                        "lat": None, "lon": None}
        return None

    return walk(fetch, urls, parse, "Barclays")


LBG_DOMAINS = {
    "Lloyds Bank": "branches.lloydsbank.com",
    "Halifax": "branches.halifax.co.uk",
    "Bank of Scotland": "branches.bankofscotland.co.uk",
}


LBG_NOT_A_BRANCH = ("cashpoint", "cash-in-and-out", "cash-in-out", "cash-machine",
                    "community-banker", "banking-hub")


def source_lbg(fetch: Fetcher) -> list[dict]:
    """Lloyds Banking Group: one locator behind three domains.

    Each domain lists every group location -- a Bank of Scotland branch appears
    on lloydsbank.com as a "child" -- so the three sitemaps are unioned by path
    and each location fetched once, from whichever domain lists it. The brand
    is the page title's prefix.

    Branches are town/address pages, but depth alone does not identify them. A
    street number written with a slash -- "176/180 Bute Street Mall" -- splits
    the URL a level deeper, and filtering on depth missed 95 real branches,
    whose OSM records would all have been marked closed. So everything is kept
    except the kinds that are named in the slug and are never branches:
    cashpoints, cash machines, community bankers, banking hubs and events.
    The title check then decides.
    """
    paths = {}
    for domain in LBG_DOMAINS.values():
        for u in fetch.locs(f"https://{domain}/sitemap.xml"):
            path = re.sub(r"https://[^/]+/", "", u).strip("/")
            parts = path.split("/")
            if len(parts) < 2 or parts[0] == "events" or any(k in path for k in LBG_NOT_A_BRANCH):
                continue
            paths.setdefault(path, f"https://{domain}/{path}")

    def parse(html, url):
        title = title_of(html)
        brand = next((b for b in LBG_DOMAINS if title.startswith(b)), None)
        if brand is None:
            return None
        geo = parse_microdata(html)
        if geo is None:
            return None
        return {"brand_group": brand, "name": title.split("|")[0].strip(), **geo}

    return walk(fetch, sorted(paths.values()), parse, "Lloyds Banking Group")


def source_tsb(fetch: Fetcher) -> list[dict]:
    """TSB: Yext-hosted locator. Index pages carry no geo of their own.

    The locator lists pop-ups, pods and banking hubs alongside branches, and
    none of those is a branch: a pod in a town whose branch has closed would
    otherwise keep the closed branch's OSM record alive. The page's own type is
    the badge in its hero block (`Hero-popupLabel`); the same badge in a
    `Teaser-` block belongs to a nearby location, which is why a banking hub's
    page can say "Pop-up Location" without being one. Banking hubs say so in
    the title instead.
    """
    urls = [u for u in fetch.locs("https://branches.tsb.co.uk/sitemap.xml")
            if u.endswith(".html") and u.count("/") >= 4]

    def parse(html, url):
        geo = parse_microdata(html)
        if geo is None:
            return None
        badge = re.search(r'Hero-popupLabel[^>]*>\s*([^<]+?)\s*<', html)
        kind = (badge.group(1) if badge
                else "Banking Hub" if "Banking Hub" in title_of(html) else "Branch")
        name = re.search(r'class="LocationName-geo">([^<]+)<', html)
        return {"brand_group": "TSB", "kind": kind,
                "name": name.group(1).strip() if name else title_of(html).split("|")[0].strip(),
                **geo}

    rows = walk(fetch, urls, parse, "TSB")
    kinds = {}
    for r in rows:
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
    print(f"    TSB location types: {kinds}")
    return [r for r in rows if r["kind"] == "Branch"]


def source_nationwide(fetch: Fetcher) -> list[dict]:
    """Nationwide: /branches/<town>/<address> pages; town pages are indexes."""
    urls = [u for u in fetch.locs("https://www.nationwide.co.uk/sitemap-index.xml")
            if "/branches/" in u
            and len(re.sub(r"https://[^/]+/", "", u).strip("/").split("/")) >= 3]

    def parse(html, url):
        # "Nationwide PayPoint - Cash Deposit - <shop>" pages are agency counters
        # in corner shops, not branches -- 161 of them against 604 branches.
        if not title_of(html).startswith("Nationwide Building Society"):
            return None
        geo = parse_microdata(html)
        if geo is None:
            return None
        return {"brand_group": "Nationwide",
                "name": title_of(html).split("|")[0].strip(), **geo}

    return walk(fetch, sorted(set(urls)), parse, "Nationwide")


# HSBC encodes the kind of location in the slug. Banking hubs and cash hubs are
# shared facilities run by Cash Access UK, where HSBC staff may or may not be
# present -- they are not HSBC branches and already have their own brand on the
# map. Self-service branches are HSBC premises with machines and no counter,
# and a customer can walk into one, so they count.
HSBC_NOT_A_BRANCH = ("banking-hub", "cash-hub", "temporary-hub", "cash-service")


def source_hsbc(fetch: Fetcher) -> list[dict]:
    urls = [u for u in fetch.locs("https://www.hsbc.co.uk/sitemaps.xml")
            if "/branch-list/" in u and u.rstrip("/").split("/")[-1] != "branch-list"]
    branches = [u for u in urls
                if not any(k in u.rstrip("/").split("/")[-1] for k in HSBC_NOT_A_BRANCH)]
    print(f"    HSBC: {len(urls)} locations listed, {len(branches)} are branches "
          f"(the rest are banking hubs and cash hubs)")

    def parse(html, url):
        for block in re.findall(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>',
                                html, re.S):
            try:
                data = json.loads(block)
            except ValueError:
                continue
            for item in data if isinstance(data, list) else [data]:
                geo = item.get("geo") if isinstance(item, dict) else None
                if not geo:
                    continue
                addr = item.get("address") or {}
                return {"brand_group": "HSBC UK", "name": item.get("name", ""),
                        "postcode": postcode_of(addr.get("postalCode", "")),
                        "lat": float(geo["latitude"]), "lon": float(geo["longitude"])}
        return None

    return walk(fetch, branches, parse, "HSBC UK")


NATWEST_API = ("https://www.natwest.com/content/natwest_com/en_uk/personal/"
               "search-results/locator/jcr:content/root/responsivegrid/locator/"
               "results.blapi.search.json")
NATWEST_PAGE = 50   # the endpoint's ceiling; anything larger returns an error page
NATWEST_IDLE_STOP = 250


def source_natwest(fetch: Fetcher) -> list[dict]:
    """NatWest and RBS: the locator's own search endpoint.

    It returns the nearest 50 to a search term, whatever the radius, and has no
    offset -- so all of GB is reached by tiling. Each postcode area is queried
    from its central postcode; an area whose answer comes back full is
    re-queried district by district, since a full page means there may be more
    beyond it. The endpoint reports its own total, which makes completeness
    checkable rather than hoped for.

    Two refinements came from running it. The total includes NatWest
    International in Jersey, Guernsey and the Isle of Man, which no GB postcode
    is near enough to reach -- so those three are queried by name, or the sweep
    would never reconcile and would walk all ~2,800 GB districts looking for
    branches that are not in GB. And the sweep stops once district queries have
    gone a long stretch without finding anything new, rather than exhausting
    the queue: the last few hundred requests of a sweep that has stopped
    learning are load on someone else's server for nothing.
    """
    filt = {"$and": [
        {"$or": [{"c_brand": {"$eq": "NatWest"}}, {"c_brand": {"$eq": "RBS"}}]},
        {"meta.entityType": {"$eq": "location"}},
    ]}
    fq = urllib.parse.quote(json.dumps(filt, separators=(",", ":")))

    def query(term: str):
        url = (f"{NATWEST_API}?search_term={urllib.parse.quote(term)}"
               f"&search_limit={NATWEST_PAGE}&search_radius=1000&filter={fq}")
        resp = fetch.get(url)
        if resp is None:
            return [], None
        try:
            payload = resp.json()["response"]
        except (ValueError, KeyError):
            return [], None
        return payload.get("entities", []), payload.get("count")

    pcs = pd.read_parquet(INTERIM / "postcodes.parquet", columns=["postcode", "lat", "lon"])
    pcs["district"] = pcs["postcode"].str.split(" ").str[0]
    pcs["area"] = pcs["district"].str.extract(r"^([A-Z]+)")[0]

    def central(group: pd.DataFrame) -> str:
        d = ((group["lat"] - group["lat"].mean()) ** 2
             + (group["lon"] - group["lon"].mean()) ** 2)
        return group.loc[d.idxmin(), "postcode"]

    seen: dict[str, dict] = {}
    total = None
    queue = [("offshore", t, t) for t in ("St Helier", "St Peter Port", "Douglas Isle of Man")]
    queue += [("area", a, central(g)) for a, g in pcs.groupby("area")]
    done_districts: set[str] = set()
    idle = 0
    print(f"    NatWest/RBS: tiling {len(queue) - 3} postcode areas, plus the "
          f"three offshore islands by name")
    while queue:
        kind, key, term = queue.pop(0)
        ents, count = query(term)
        total = count if count is not None else total
        before = len(seen)
        for e in ents:
            seen[e["meta"]["id"]] = e
        idle = idle + 1 if (kind == "district" and len(seen) == before) else 0
        if kind == "area" and len(ents) >= NATWEST_PAGE:
            for d, g in pcs[pcs["area"] == key].groupby("district"):
                if d not in done_districts:
                    done_districts.add(d)
                    queue.append(("district", d, central(g)))
        print(f"\r    NatWest/RBS: {len(seen)} of {total} found, "
              f"{len(queue)} queries queued   ", end="", flush=True)
        if total is not None and len(seen) >= total:
            print("\n    complete: every location the endpoint reports was reached")
            break
        if idle >= NATWEST_IDLE_STOP:
            print(f"\n    stopped: {NATWEST_IDLE_STOP} district queries in a row "
                  f"found nothing new; {len(seen)} of {total} reached")
            break
    else:
        print(f"\n    WARNING: sweep ended at {len(seen)} of {total}")

    rows = []
    for e in seen.values():
        # Banking hubs are shared Cash Access UK facilities, not NatWest or RBS
        # branches, and already carry their own brand on the map.
        if str(e.get("c_bankinghub")) == "1":
            continue
        coord = e.get("yextDisplayCoordinate") or {}
        rows.append({"brand_group": e.get("c_brand"), "name": e.get("name", ""),
                     "postcode": postcode_of((e.get("address") or {}).get("postalCode", "")),
                     "lat": coord.get("latitude"), "lon": coord.get("longitude")})
    return rows


METRO_HQ = "WC1B 5HA"


def source_metro(fetch: Fetcher) -> list[dict]:
    """Metro Bank: one page per store; postcode only, head office excluded."""
    urls = [u for u in fetch.locs("https://www.metrobankonline.co.uk/sitemap.xml")
            if "/store-locator/stores/" in u and u.rstrip("/").split("/")[-1] != "stores"]

    def parse(html, url):
        pcs = [f"{a} {b}" for a, b in POSTCODE_RE.findall(html)]
        pcs = [p for p in pcs if p != METRO_HQ]
        if not pcs:
            return None
        return {"brand_group": "Metro Bank", "name": title_of(html).split("|")[0].strip(),
                "postcode": pcs[0], "lat": None, "lon": None}

    return walk(fetch, sorted(set(urls)), parse, "Metro Bank")


# name -> (fetch function, brands it covers)
SOURCES = {
    "barclays": (source_barclays, ["Barclays"]),
    "lbg": (source_lbg, list(LBG_DOMAINS)),
    "tsb": (source_tsb, ["TSB"]),
    "nationwide": (source_nationwide, ["Nationwide"]),
    "hsbc": (source_hsbc, ["HSBC UK"]),
    "natwest": (source_natwest, ["NatWest", "RBS"]),
    "metro": (source_metro, ["Metro Bank"]),
}


def load_source(name: str, force: bool) -> pd.DataFrame:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{name}.json"
    if path.exists() and not force:
        print(f"  cached: {path.name}")
        return pd.DataFrame(json.loads(path.read_text(encoding="utf-8")))
    fn, _ = SOURCES[name]
    rows = fn(Fetcher())
    path.write_text(json.dumps(rows, indent=1), encoding="utf-8")
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- matching


def geocode(df: pd.DataFrame) -> pd.DataFrame:
    """Fill missing coordinates from the postcode lookup the project holds.

    The lookup is Great Britain only, so Northern Ireland and the Crown
    Dependencies fall out here. That is correct -- the map does not cover
    them -- and it is the expected outcome rather than a failure.
    """
    if df.empty:
        return df.assign(lat=[], lon=[])
    pcs = pd.read_parquet(INTERIM / "postcodes.parquet", columns=["pc_key", "lat", "lon"])
    pcs = pcs.rename(columns={"lat": "pc_lat", "lon": "pc_lon"})
    df = df.copy()
    df["pc_key"] = df["postcode"].fillna("").str.replace(" ", "", regex=False).str.upper()
    df = df.merge(pcs, on="pc_key", how="left")
    df["lat"] = pd.to_numeric(df["lat"], errors="coerce").fillna(df["pc_lat"])
    df["lon"] = pd.to_numeric(df["lon"], errors="coerce").fillna(df["pc_lon"])
    outside = df["postcode"].fillna("").str.match(r"^(BT|JE|GY|IM)")
    dropped = df[outside | df["lat"].isna()]
    if len(dropped):
        print(f"  {len(dropped)} outside GB or unlocatable, not on this map")
    return df[~outside & df["lat"].notna()].drop(columns=["pc_lat", "pc_lon"])


def match(osm: pd.DataFrame, live: pd.DataFrame) -> pd.DataFrame:
    """Greedy nearest-neighbour, one live branch to one OSM record.

    One-to-one matters: OSM often carries both a node and the building way for
    the same branch, and letting two records claim the same live branch would
    quietly keep a duplicate alive.
    """
    pairs = []
    for li, l in live.iterrows():
        scale = KM_PER_DEG_LAT * 1000 * math.cos(math.radians(l.lat))
        dx = (osm["lon"] - l.lon) * scale
        dy = (osm["lat"] - l.lat) * KM_PER_DEG_LAT * 1000
        dist = (dx ** 2 + dy ** 2) ** 0.5
        for oi, d in dist[dist <= MATCH_METRES].items():
            pairs.append((d, oi, li))
    pairs.sort()
    taken_osm, taken_live, out = set(), set(), []
    for d, oi, li in pairs:
        if oi in taken_osm or li in taken_live:
            continue
        taken_osm.add(oi)
        taken_live.add(li)
        out.append({"osm_index": oi, "live_index": li, "metres": d})
    return pd.DataFrame(out, columns=["osm_index", "live_index", "metres"])


def main() -> None:
    force = "--force" in sys.argv[1:]
    only = None
    if "--only" in sys.argv:
        only = {s.strip() for s in sys.argv[sys.argv.index("--only") + 1].split(",")}

    osm_path = INTERIM / "gb_branches_all.parquet"
    if not osm_path.exists():
        raise SystemExit("run pipeline/build_branches.py first "
                         "(it writes gb_branches_all.parquet)")
    # The unfiltered set, deliberately: reading the filtered one would be
    # circular, and reading the raw OSM dump would undo the node/way dedupe and
    # let a live branch's twin be marked closed.
    all_osm = pd.read_parquet(osm_path)

    closures, summary = [], {"verified": {}, "unverifiable": UNVERIFIABLE}
    for name, (_, brands) in SOURCES.items():
        if only and not (set(brands) & only):
            continue
        print(f"\n{', '.join(brands)}:")
        live_all = geocode(load_source(name, force))
        for brand in brands:
            if only and brand not in only:
                continue
            live = live_all[live_all["brand_group"] == brand].reset_index(drop=True)
            osm = all_osm[all_osm["brand_group"] == brand].copy()
            if not len(live):
                print(f"  {brand}: operator list came back empty -- NOT filtering")
                continue
            m = match(osm, live)
            closed = osm.drop(index=m["osm_index"])
            q = (m["metres"].quantile([0.5, 0.9]) if len(m)
                 else pd.Series({0.5: 0.0, 0.9: 0.0}))
            print(f"  {brand:18s} OSM {len(osm):4d} -> {len(m):4d} kept, "
                  f"{len(closed):4d} closed | published {len(live):4d}, "
                  f"{len(live) - len(m):3d} missing from OSM | "
                  f"median {q[0.5]:.0f} m, p90 {q[0.9]:.0f} m")
            closures += [{"osm_id": oid, "brand_group": brand,
                          "reason": "absent from the operator's published branch list"}
                         for oid in closed["osm_id"]]
            summary["verified"][brand] = {
                "osm": int(len(osm)), "kept": int(len(m)), "closed": int(len(closed)),
                "published": int(len(live)), "missing_from_osm": int(len(live) - len(m)),
            }

    # With --only, keep previously verified brands rather than forgetting them.
    if only and CLOSURES.exists():
        prev = pd.read_parquet(CLOSURES)
        prev = prev[~prev["brand_group"].isin(only)]
        closures = prev.to_dict("records") + closures
        if SUMMARY.exists():
            old = json.loads(SUMMARY.read_text(encoding="utf-8"))["verified"]
            summary["verified"] = {**{k: v for k, v in old.items() if k not in only},
                                   **summary["verified"]}

    pd.DataFrame(closures, columns=["osm_id", "brand_group", "reason"]).to_parquet(CLOSURES)
    SUMMARY.write_text(json.dumps(summary, indent=1), encoding="utf-8")
    total = sum(v["closed"] for v in summary["verified"].values())
    print(f"\nwrote {CLOSURES.name}: {total:,} closed records across "
          f"{len(summary['verified'])} verified brands")
    print("not verified: " + "; ".join(f"{b} ({why})" for b, why in UNVERIFIABLE.items()))


if __name__ == "__main__":
    main()
