'use strict';

/* ------------------------------------------------------------------ *
 * Catchments by travel time.
 *
 * A straight-line radius does not know about rivers, estuaries or motorways.
 * This measures reach in minutes instead: driving, or on foot.
 *
 * Nothing is routed on a server. pipeline/build_traveltime.py ships a small
 * graph -- one node per small area, plus junctions on the strategic road
 * network for driving -- whose edges are exact travel times on the full OS
 * road network. A catchment is one shortest-path search over that graph, from
 * every branch at once, stopped at the time limit. It takes milliseconds, so
 * it works for a single dragged scenario branch and for a whole network alike.
 *
 * The file is a few megabytes and most visitors never switch mode, so it is
 * fetched the first time a travel-time mode is chosen.
 * ------------------------------------------------------------------ */

const REACH_MODES = {
  distance: { label: 'Catchment radius', unit: 'km', min: 0.5, max: 40, step: 0.5,
              field: 'radiusKm', phrase: (v) => v.toFixed(1) + ' km' },
  drive:    { label: 'Drive time', unit: 'min', min: 3, max: 60, step: 1,
              field: 'driveMin', phrase: (v) => v + ' minutes’ drive' },
  walk:     { label: 'Walk time', unit: 'min', min: 3, max: 40, step: 1,
              field: 'walkMin', phrase: (v) => v + ' minutes’ walk' },
};

/* Getting from a branch's front door onto the graph: the nearest few area
 * nodes, at a straight-line distance stretched for the street pattern. */
const ACCESS = {
  drive: { kmh: 22, maxKm: 6, detour: 1.35 },
  walk:  { kmh: 4.8, maxKm: 2, detour: 1.3 },
};
const ACCESS_NODES = 4;

const travelState = { loaded: false, loading: null, graphs: {}, toState: null, fromState: null };

function reachMode() { return REACH_MODES[branchState.reach] || REACH_MODES.distance; }
function reachValue() { return branchState[reachMode().field]; }
function reachPhrase() { return reachMode().phrase(reachValue()); }
/** Identifies a catchment setting, for caches. */
function reachKey() { return branchState.reach + ':' + reachValue(); }

function loadTravel() {
  if (travelState.loaded) return Promise.resolve(true);
  if (travelState.loading) return travelState.loading;
  setStatus('Loading travel times…');
  travelState.loading = fetch('data/gb_travel.json', { cache: 'no-store' })
    .then((r) => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
    .then((data) => {
      for (const mode of ['drive', 'walk']) {
        const g = data[mode];
        travelState.graphs[mode] = {
          offsets: Uint32Array.from(g.offsets),
          targets: Uint32Array.from(g.targets),
          seconds: Uint16Array.from(g.seconds),
          n: g.offsets.length - 1,
        };
      }
      // Joined by area code, not by position, like every other data file.
      const n = data.n_areas;
      travelState.toState = new Int32Array(n).fill(-1);
      travelState.fromState = new Int32Array(state.codes.length).fill(-1);
      for (let i = 0; i < n; i++) {
        const s = state.index.get(data.codes[i]);
        if (s !== undefined) { travelState.toState[i] = s; travelState.fromState[s] = i; }
      }
      travelState.loaded = true;
      setStatus(null);
      return true;
    })
    .catch((err) => {
      console.warn('no travel-time file', err);
      travelState.loading = null;
      setStatus('Travel times are not available in this build.');
      setTimeout(() => setStatus(null), 4000);
      return false;
    });
  return travelState.loading;
}

/* A binary heap of (time, node) pairs in two parallel arrays. Entries are
 * never updated in place; a node reached again more cheaply is pushed again
 * and the stale entry is skipped when it surfaces. */
function makeHeap() {
  const keys = [], vals = [];
  return {
    size: () => keys.length,
    push(k, v) {
      let i = keys.length;
      keys.push(k); vals.push(v);
      while (i > 0) {
        const p = (i - 1) >> 1;
        if (keys[p] <= k) break;
        keys[i] = keys[p]; vals[i] = vals[p];
        i = p;
      }
      keys[i] = k; vals[i] = v;
    },
    pop() {
      const k0 = keys[0], v0 = vals[0];
      const k = keys.pop(), v = vals.pop();
      const n = keys.length;
      if (n) {
        let i = 0;
        for (;;) {
          let c = 2 * i + 1;
          if (c >= n) break;
          if (c + 1 < n && keys[c + 1] < keys[c]) c++;
          if (keys[c] >= k) break;
          keys[i] = keys[c]; vals[i] = vals[c];
          i = c;
        }
        keys[i] = k; vals[i] = v;
      }
      return [k0, v0];
    },
  };
}

/** The nearest area nodes to a point, with the seconds it takes to reach them. */
function accessNodes(lat, lon, mode) {
  const { ax, ay, grid } = branchState;
  const a = ACCESS[mode];
  const y = lat * KM_PER_DEG_LAT;
  const x = lon * lonScale(lat);
  const cx = Math.floor(x / GRID_KM), cy = Math.floor(y / GRID_KM);
  const reach = Math.ceil(a.maxKm / GRID_KM);
  const found = [];
  for (let gx = cx - reach; gx <= cx + reach; gx++) {
    for (let gy = cy - reach; gy <= cy + reach; gy++) {
      const cell = grid.get(gx + ':' + gy);
      if (!cell) continue;
      for (const i of cell) {
        const dx = ax[i] - x, dy = ay[i] - y;
        const km = Math.sqrt(dx * dx + dy * dy);
        if (km <= a.maxKm && travelState.fromState[i] >= 0) found.push([km, i]);
      }
    }
  }
  found.sort((p, q) => p[0] - q[0]);
  return found.slice(0, ACCESS_NODES).map(([km, i]) =>
    [km * a.detour / a.kmh * 3600, travelState.fromState[i]]);
}

/** Mark every area within the time limit of any of `branches`. */
function markReachable(branches, mode, minutes, covered) {
  const g = travelState.graphs[mode];
  const limit = minutes * 60;
  // Float64, not Float32. The heap holds times as doubles; stored in 32 bits
  // they round, and a time that rounded *down* then failed the "is this heap
  // entry stale?" test below, so about half of all nodes were dropped mid-search.
  const dist = new Float64Array(g.n).fill(Infinity);
  const heap = makeHeap();
  for (const b of branches) {
    for (const [secs, node] of accessNodes(b.y, b.x, mode)) {
      if (secs <= limit && secs < dist[node]) { dist[node] = secs; heap.push(secs, node); }
    }
  }
  const { offsets, targets, seconds } = g;
  while (heap.size()) {
    const [d, u] = heap.pop();
    if (d > dist[u]) continue;
    for (let e = offsets[u]; e < offsets[u + 1]; e++) {
      const nd = d + seconds[e];
      const v = targets[e];
      if (nd <= limit && nd < dist[v]) { dist[v] = nd; heap.push(nd, v); }
    }
  }
  const toState = travelState.toState;
  for (let i = 0; i < toState.length; i++) {
    if (dist[i] <= limit && toState[i] >= 0) covered[toState[i]] = 1;
  }
}

/** The one place a set of branches becomes a set of covered areas. */
function coverInto(branches, covered) {
  if (branchState.reach !== 'distance' && travelState.loaded) {
    markReachable(branches, branchState.reach, reachValue(), covered);
    return;
  }
  for (const b of branches) markCovered(b.y, b.x, branchState.radiusKm, covered);
}
