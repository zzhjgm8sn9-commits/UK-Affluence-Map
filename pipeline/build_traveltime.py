"""Drive and walk travel times between small areas, from OS Open Roads.

    python pipeline/build_traveltime.py [--validate]

A straight-line radius is the weakest assumption on the map: rivers, estuaries
and motorways decide who can actually reach a branch, and a circle knows about
none of them. This step replaces it with time on the road network.

The site is static, so nothing can be routed on demand. Instead this builds a
small graph the browser can search in milliseconds:

  nodes   one per small area (the road junction nearest its centroid), plus,
          for driving, "hubs" -- junctions on motorways and primary A roads
  edges   exact network travel times between nearby nodes, pruned to the ones
          a shortest path would actually use

A trip in the browser is then a shortest path over that graph. Each hop is an
exact time on the full 3.9-million-link network; what is approximate is that a
long trip is stitched together from hops, which is why hubs exist: without
them a motorway journey would be forced off at every area centroid along the
way. --validate measures the stitched times against exact ones.


WHAT THE TIMES ARE
------------------
Free-flowing estimates from road class and form, slower inside built-up
areas. No traffic, no time of day, no turn restrictions, no one-way streets
(Open Roads does not record direction), no ferries -- islands are reachable
only from themselves. Walking is network distance at 4.8 km/h on everything
but motorways. Good for comparing catchments; not a sat-nav.
"""

from __future__ import annotations

import json
import sys
import time
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
import pyogrio
from scipy import sparse
from scipy.sparse import csgraph
from scipy.spatial import cKDTree

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import INTERIM, RAW, WEB

ROADS_ZIP = RAW / "os_open_roads.zip"
GRAPH_CACHE = INTERIM / "road_graph.npz"

# km/h as (open country, built-up, dense urban). Typical achieved speeds, not
# limits: an urban A road is 30 mph on paper and rather less between its
# traffic lights. Tuned against six known journeys -- see the README.
SPEEDS = {
    "Motorway": (100, 86, 70),
    "A dual": (84, 50, 34),
    "A primary": (68, 35, 22),
    "A Road": (62, 32, 20),
    "B Road": (55, 29, 19),
    "Minor Road": (47, 26, 17),
    "Local Road": (37, 21, 14),
    "access": (18, 13, 10),
}
CLASSES = list(SPEEDS)
SLIP_KMH = 50
ROUNDABOUT_KMH = 22
ACCESS = {"Local Access Road", "Secondary Access Road", "Restricted Local Access Road"}

# Road junctions per 1 km square: the cheapest honest measure of how built-up
# somewhere is, and available everywhere the roads are.
BUILT_UP = 55
DENSE = 150

WALK_MPS = 4.8 / 3.6

DRIVE_LIMIT_S = 12 * 60     # longest single hop stored, driving
WALK_LIMIT_S = 25 * 60      # and walking
CANDIDATES = 48             # nearest neighbours considered per node
HUB_LINKS = 8               # hubs every area is offered
HUB_CELL_M = 2500           # one hub per cell of this size
STRETCH = 1.03              # an edge is dropped if a detour is within 3%


# ------------------------------------------------------------------ graph


def load_graph():
    """Every road link and node in GB, as arrays. Cached: 124 shapefiles."""
    if GRAPH_CACHE.exists():
        z = np.load(GRAPH_CACHE)
        print(f"  cached road graph: {len(z['x']):,} nodes, {len(z['u']):,} links")
        return {k: z[k] for k in z.files}

    with zipfile.ZipFile(ROADS_ZIP) as z:
        names = z.namelist()
    link_files = sorted(n for n in names if n.endswith("RoadLink.shp"))
    node_files = sorted(n for n in names if n.endswith("RoadNode.shp"))

    ids, xs, ys = [], [], []
    for i, name in enumerate(node_files, 1):
        df = pyogrio.read_dataframe(f"zip://{ROADS_ZIP}!{name}", columns=["identifier"])
        ids.append(df["identifier"].to_numpy())
        xs.append(df.geometry.x.to_numpy())
        ys.append(df.geometry.y.to_numpy())
        print(f"\r  nodes {i}/{len(node_files)}", end="", flush=True)
    print()
    node_id = np.concatenate(ids)
    x = np.concatenate(xs)
    y = np.concatenate(ys)
    # Junctions on a tile edge are listed by both tiles.
    _, first = np.unique(node_id, return_index=True)
    first.sort()
    node_id, x, y = node_id[first], x[first], y[first]
    index = pd.Series(np.arange(len(node_id)), index=node_id)

    parts = []
    cols = ["function", "formOfWay", "length", "primary", "startNode", "endNode"]
    for i, name in enumerate(link_files, 1):
        parts.append(pyogrio.read_dataframe(f"zip://{ROADS_ZIP}!{name}",
                                            columns=cols, read_geometry=False))
        print(f"\r  links {i}/{len(link_files)}", end="", flush=True)
    print()
    links = pd.concat(parts, ignore_index=True)

    u = index.reindex(links["startNode"]).to_numpy()
    v = index.reindex(links["endNode"]).to_numpy()
    ok = ~(np.isnan(u) | np.isnan(v))
    links, u, v = links[ok], u[ok].astype(np.int64), v[ok].astype(np.int64)
    length = links["length"].to_numpy(dtype=np.float64).clip(min=1.0)

    cell = (np.floor(x / 1000).astype(np.int64) * 100000 + np.floor(y / 1000).astype(np.int64))
    counts = pd.Series(cell).map(pd.Series(cell).value_counts()).to_numpy()
    density = np.maximum(counts[u], counts[v])

    fn = links["function"].to_numpy()
    form = links["formOfWay"].fillna("").to_numpy().astype(str)
    primary = links["primary"].astype(str).str.lower().eq("true").to_numpy()

    klass = np.where(np.isin(fn, list(ACCESS)), "access", fn).astype(object)
    is_a = fn == "A Road"
    klass[is_a & primary] = "A primary"
    klass[is_a & (np.char.find(form, "Dual") >= 0)] = "A dual"
    kcode = pd.Series(klass).map({k: i for i, k in enumerate(CLASSES)}).fillna(
        CLASSES.index("access")).to_numpy(dtype=np.int8)

    out = dict(x=x, y=y, u=u, v=v, length=length, kcode=kcode, density=density,
               slip=form == "Slip Road", roundabout=form == "Roundabout",
               motorway=fn == "Motorway", major=(fn == "Motorway") | (is_a & primary),
               local=np.isin(fn, ["Local Road", "Minor Road", "B Road", "A Road"]))
    np.savez(GRAPH_CACHE, **out)
    print(f"  road graph: {len(x):,} nodes, {len(u):,} links")
    return out


def drive_seconds(g):
    """Seconds to drive each link. Kept out of the cache so that the speed
    table can be tuned without re-reading 124 shapefiles."""
    table = np.array([SPEEDS[k] for k in CLASSES], dtype=float)
    tier = (g["density"] >= BUILT_UP).astype(int) + (g["density"] >= DENSE).astype(int)
    kmh = table[g["kcode"], tier]
    kmh[g["slip"]] = np.minimum(kmh[g["slip"]], SLIP_KMH)
    kmh[g["roundabout"]] = ROUNDABOUT_KMH
    return g["length"] / (kmh / 3.6)


def csr(n, u, v, w):
    """Undirected graph; where two links join the same pair, the quicker wins."""
    a = np.minimum(u, v)
    b = np.maximum(u, v)
    order = np.lexsort((w, b, a))
    a, b, w = a[order], b[order], w[order]
    first = np.ones(len(a), dtype=bool)
    first[1:] = (a[1:] != a[:-1]) | (b[1:] != b[:-1])
    a, b, w = a[first], b[first], w[first]
    m = sparse.coo_matrix((w, (a, b)), shape=(n, n))
    return (m + m.T).tocsr()


# ------------------------------------------------------------------ nodes


def area_nodes(g):
    """The junction that stands for each small area: nearest its centroid, on
    an ordinary road. Motorway-only and access-only junctions are not places
    anyone lives beside."""
    from pyproj import Transformer

    metrics = pd.read_parquet(INTERIM / "gb_metrics.parquet",
                              columns=["centroid_lat", "centroid_lon"])
    tx = Transformer.from_crs("EPSG:4326", "EPSG:27700", always_xy=True)
    ax, ay = tx.transform(metrics["centroid_lon"].to_numpy(),
                          metrics["centroid_lat"].to_numpy())

    usable = np.zeros(len(g["x"]), dtype=bool)
    usable[g["u"][g["local"]]] = True
    usable[g["v"][g["local"]]] = True
    cand = np.flatnonzero(usable)
    tree = cKDTree(np.column_stack([g["x"][cand], g["y"][cand]]))
    dist, idx = tree.query(np.column_stack([ax, ay]))
    print(f"  area -> junction: median {np.median(dist):.0f} m, "
          f"99th {np.percentile(dist, 99):.0f} m")
    return list(metrics.index), cand[idx]


def hub_nodes(g, taken):
    """Junctions on motorways and primary A roads, thinned to one per cell."""
    n = len(g["x"])
    deg = np.bincount(np.concatenate([g["u"][g["major"]], g["v"][g["major"]]]), minlength=n)
    alldeg = np.bincount(np.concatenate([g["u"], g["v"]]), minlength=n)
    cand = np.flatnonzero((deg >= 1) & (alldeg >= 3))
    cell = (np.floor(g["x"][cand] / HUB_CELL_M).astype(np.int64) * 100000
            + np.floor(g["y"][cand] / HUB_CELL_M).astype(np.int64))
    df = pd.DataFrame({"node": cand, "cell": cell, "deg": alldeg[cand]})
    hubs = df.sort_values("deg", ascending=False).drop_duplicates("cell")["node"].to_numpy()
    hubs = np.setdiff1d(hubs, taken)
    print(f"  {len(hubs):,} hubs on the strategic network")
    return hubs


# ------------------------------------------------------------- distances


def local_times(graph, nodes, limit, label, batch=24):
    """Exact times from every node in `nodes` to every other within `limit`.

    One bounded Dijkstra per source over the whole road graph. Returns a
    sparse matrix indexed by position in `nodes`.
    """
    uniq, inverse = np.unique(nodes, return_inverse=True)
    rows, cols, vals = [], [], []
    t0 = time.time()
    for start in range(0, len(uniq), batch):
        src = uniq[start:start + batch]
        d = csgraph.dijkstra(graph, directed=False, indices=src, limit=limit)
        d = d[:, uniq]
        r, c = np.nonzero(np.isfinite(d))
        rows.append(r + start)
        cols.append(c)
        vals.append(d[r, c].astype(np.float32))
        done = start + len(src)
        if (start // batch) % 20 == 0 or done == len(uniq):
            rate = done / max(time.time() - t0, 1e-9)
            print(f"\r    {label}: {done:,}/{len(uniq):,} "
                  f"({(len(uniq) - done) / max(rate, 1e-9) / 60:.0f} min left)   ",
                  end="", flush=True)
    print()
    m = sparse.csr_matrix((np.concatenate(vals),
                           (np.concatenate(rows), np.concatenate(cols))),
                          shape=(len(uniq), len(uniq)))
    # Back to one row per requested node (two areas can share a junction).
    return m[inverse][:, inverse].tocsr()


def prune(T, n_areas, hub_links):
    """Keep only the hops a shortest path would use.

    Each node considers its nearest neighbours in time order and keeps one only
    if no neighbour already kept offers a route to it within STRETCH of the
    direct time. What survives looks like a road map: links to adjacent areas
    and along the major roads, not a hairball of every pair within 12 minutes.
    """
    n = T.shape[0]
    T = T.tocsr()
    is_hub = np.arange(n) >= n_areas
    keep_r, keep_c, keep_w = [], [], []
    for a in range(n):
        lo, hi = T.indptr[a], T.indptr[a + 1]
        nb, w = T.indices[lo:hi], T.data[lo:hi]
        m = nb != a
        nb, w = nb[m], w[m]
        if not len(nb):
            continue
        order = np.argsort(w, kind="stable")
        nb, w = nb[order], w[order]
        near = nb[:CANDIDATES]
        # Always offer the strategic network, or a city-centre area's nearest
        # 48 neighbours are all side streets and nothing leads out of town.
        hubs = nb[is_hub[nb]][:hub_links]
        cand, first = np.unique(np.concatenate([near, hubs]), return_index=True)
        cw = np.concatenate([w[:CANDIDATES], w[is_hub[nb]][:hub_links]])[first]
        o = np.argsort(cw, kind="stable")
        cand, cw = cand[o], cw[o]

        sub = T[cand][:, cand].toarray()
        sub[sub == 0] = np.inf
        kept = []
        for j in range(len(cand)):
            if kept:
                via = cw[kept] + sub[kept, j]
                if via.min() <= STRETCH * cw[j]:
                    continue
            kept.append(j)
        keep_r.append(np.full(len(kept), a))
        keep_c.append(cand[kept])
        keep_w.append(cw[kept])
        if a % 5000 == 0:
            print(f"\r    pruning {a:,}/{n:,}", end="", flush=True)
    print()
    m = sparse.coo_matrix((np.concatenate(keep_w),
                           (np.concatenate(keep_r), np.concatenate(keep_c))), shape=(n, n))
    return m.maximum(m.T).tocsr()


# ------------------------------------------------------------------ main


def encode(m):
    m = m.tocsr()
    m.sort_indices()
    return {"offsets": m.indptr.tolist(), "targets": m.indices.tolist(),
            "seconds": np.rint(m.data).astype(int).clip(1).tolist()}


def validate(drive_graph, drive_csr, nodes, n_areas, samples=40):
    """Stitched times against exact ones, from a spread of origins."""
    rng = np.random.default_rng(7)
    src = rng.choice(n_areas, samples, replace=False)
    exact = csgraph.dijkstra(drive_graph, directed=False, indices=nodes[src],
                             limit=50 * 60)[:, nodes[:n_areas]]
    stitched = csgraph.dijkstra(drive_csr, directed=False, indices=src,
                                limit=70 * 60)[:, :n_areas]
    print("\n  stitched vs exact drive time, by exact time band:")
    for lo, hi in [(0, 10), (10, 20), (20, 30), (30, 45)]:
        m = (exact > lo * 60) & (exact <= hi * 60)
        ratio = stitched[m] / exact[m]
        missing = np.mean(~np.isfinite(ratio))
        ratio = ratio[np.isfinite(ratio)]
        print(f"    {lo:2d}-{hi:2d} min: median +{100 * (np.median(ratio) - 1):.1f}%, "
              f"90th +{100 * (np.percentile(ratio, 90) - 1):.1f}%, "
              f"unreached {100 * missing:.2f}%  (n={m.sum():,})")


def main() -> None:
    print("road network:")
    g = load_graph()
    n = len(g["x"])

    codes, a_nodes = area_nodes(g)
    hubs = hub_nodes(g, a_nodes)
    n_areas = len(codes)

    drive_graph = csr(n, g["u"], g["v"], drive_seconds(g))
    walkable = ~g["motorway"]
    walk_graph = csr(n, g["u"][walkable], g["v"][walkable],
                     g["length"][walkable] / WALK_MPS)

    drive_cache = INTERIM / "tt_drive_local.npz"
    walk_cache = INTERIM / "tt_walk_local.npz"
    print("\nexact local times:")
    d_nodes = np.concatenate([a_nodes, hubs])
    if drive_cache.exists():
        Td = sparse.load_npz(drive_cache)
    else:
        Td = local_times(drive_graph, d_nodes, DRIVE_LIMIT_S, "drive")
        sparse.save_npz(drive_cache, Td)
    if walk_cache.exists():
        Tw = sparse.load_npz(walk_cache)
    else:
        Tw = local_times(walk_graph, a_nodes, WALK_LIMIT_S, "walk")
        sparse.save_npz(walk_cache, Tw)
    print(f"  drive: {Td.nnz:,} pairs within {DRIVE_LIMIT_S // 60} min; "
          f"walk: {Tw.nnz:,} within {WALK_LIMIT_S // 60} min")

    print("\npruning to the hops a route would use:")
    drive = prune(Td, n_areas, HUB_LINKS)
    walk = prune(Tw, n_areas, 0)
    print(f"  drive: {drive.nnz // 2:,} edges over {drive.shape[0]:,} nodes; "
          f"walk: {walk.nnz // 2:,} edges")

    if "--validate" in sys.argv[1:]:
        validate(drive_graph, drive, d_nodes, n_areas)

    payload = {"generated": time.strftime("%Y-%m-%d"), "codes": codes,
               "n_areas": n_areas, "drive": encode(drive), "walk": encode(walk)}
    dest = WEB / "data" / "gb_travel.json"
    dest.write_text(json.dumps(payload, separators=(",", ":")))
    print(f"\nwrote {dest.name} ({dest.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
