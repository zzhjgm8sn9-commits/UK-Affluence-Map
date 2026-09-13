'use strict';

/* ------------------------------------------------------------------ *
 * Search: postcodes, places, branches.
 *
 * Declared here and called from app.js once the map and data exist.
 *
 * Postcodes are fetched on demand. There are 1.75 million of them, split into
 * one file per postcode area, and the file for "SW" is only requested when
 * somebody types a postcode starting with those letters. Chunks are cached for
 * the session, so repeat searches in the same area are instant.
 *
 * Places and branches need no fetch at all -- both are already in memory for
 * the label and pin layers.
 * ------------------------------------------------------------------ */

// Enough of a postcode to know which chunk to fetch: one or two letters then a
// digit. Deliberately loose, so results appear while still typing.
const POSTCODE_START = /^[A-Z]{1,2}[0-9]/;
const MAX_PER_GROUP = 5;
const POSTCODE_ZOOM = 15;

const searchState = {
  places: [],
  chunks: new Map(),      // postcode area -> {rest, area, lat, lon}
  pending: new Map(),      // in-flight chunk fetches
  results: [],
  active: -1,
};

function normalisePostcode(text) {
  return text.toUpperCase().replace(/[^A-Z0-9]/g, '');
}

async function loadPostcodeChunk(prefix) {
  if (searchState.chunks.has(prefix)) return searchState.chunks.get(prefix);
  if (searchState.pending.has(prefix)) return searchState.pending.get(prefix);

  const job = fetch('data/postcodes/' + prefix + '.json')
    .then((r) => (r.ok ? r.json() : null))
    .then((data) => {
      searchState.chunks.set(prefix, data);
      searchState.pending.delete(prefix);
      return data;
    })
    .catch(() => {
      searchState.chunks.set(prefix, null);
      searchState.pending.delete(prefix);
      return null;
    });

  searchState.pending.set(prefix, job);
  return job;
}

async function searchPostcodes(raw) {
  const key = normalisePostcode(raw);
  const prefixMatch = key.match(/^[A-Z]{1,2}/);
  if (!prefixMatch) return [];

  // Two-letter areas take precedence, but a single letter is a valid area too
  // ("B1 1AA" vs "BA1 1AA"), so try the longer spelling first and fall back.
  const candidates = prefixMatch[0].length === 2
    ? [prefixMatch[0], prefixMatch[0][0]]
    : [prefixMatch[0]];

  // A typed space is information: it marks the outward/inward boundary. Without
  // honouring it, "EH3 6" normalises to "EH36" and prefix-matches district EH36
  // ahead of the EH3 6xx the user actually asked for. With it, the outward code
  // must match exactly and only the inward part is treated as a prefix.
  const spaced = raw.toUpperCase().trim().match(/^([A-Z0-9]+)\s+([A-Z0-9]*)$/);

  for (const prefix of candidates) {
    const chunk = await loadPostcodeChunk(prefix);
    if (!chunk) continue;

    const out = [];
    for (let i = 0; i < chunk.rest.length && out.length < MAX_PER_GROUP; i++) {
      const full = prefix + chunk.rest[i];
      if (spaced) {
        // Every UK inward code is exactly three characters.
        if (full.slice(0, -3) !== spaced[1]) continue;
        if (!full.slice(-3).startsWith(spaced[2])) continue;
      } else if (!chunk.rest[i].startsWith(key.slice(prefix.length))) {
        continue;
      }
      out.push({
        kind: 'postcode',
        label: formatPostcode(full),
        areaIndex: chunk.area[i],
        lat: chunk.lat[i],
        lon: chunk.lon[i],
      });
    }
    if (out.length) return out;
  }
  return [];
}

/** Re-insert the space before the final three characters. */
function formatPostcode(compact) {
  return compact.length > 3
    ? compact.slice(0, -3) + ' ' + compact.slice(-3)
    : compact;
}

function searchPlaces(raw) {
  const q = raw.trim().toLowerCase();
  if (q.length < 2) return [];
  const starts = [];
  const contains = [];
  for (const p of searchState.places) {
    const name = p.n.toLowerCase();
    if (name.startsWith(q)) starts.push(p);
    else if (name.includes(q)) contains.push(p);
    if (starts.length >= MAX_PER_GROUP) break;
  }
  // Already sorted by population, so the first matches are the big ones.
  return starts.concat(contains).slice(0, MAX_PER_GROUP).map((p) => ({
    kind: 'place',
    label: p.n,
    detail: p.k === 'city' ? 'City' : 'Town',
    lat: p.y,
    lon: p.x,
    zoom: p.k === 'city' ? 11 : 12,
  }));
}

function searchBranches(raw) {
  const q = raw.trim().toLowerCase();
  if (q.length < 2 || typeof branchState === 'undefined') return [];
  const out = [];
  for (const b of branchState.all) {
    if (out.length >= MAX_PER_GROUP) break;
    if (b.n.toLowerCase().includes(q) || b.b.toLowerCase().includes(q)) {
      out.push({ kind: 'branch', label: b.n, detail: b.b, branch: b });
    }
  }
  return out;
}

/* ------------------------------ actions ------------------------------ */

function selectAreaByCode(code) {
  if (!code) return;
  state.selected = code;
  map.setFilter('areas-selected', ['==', ['get', 'area_code'], code]);
  renderSelection(code);
}

function showSearchMarker(lon, lat) {
  const data = {
    type: 'FeatureCollection',
    features: [{ type: 'Feature', properties: {}, geometry: { type: 'Point', coordinates: [lon, lat] } }],
  };
  if (map.getSource('search-pin')) {
    map.getSource('search-pin').setData(data);
    return;
  }
  map.addSource('search-pin', { type: 'geojson', data });
  map.addLayer({
    id: 'search-pin',
    type: 'circle',
    source: 'search-pin',
    paint: {
      'circle-radius': 6,
      'circle-color': '#c74941',
      'circle-stroke-color': '#ffffff',
      'circle-stroke-width': 2,
    },
  });
}

function clearSearchMarker() {
  if (map.getSource('search-pin')) {
    map.getSource('search-pin').setData({ type: 'FeatureCollection', features: [] });
  }
}

function applyResult(result) {
  if (!result) return;

  if (result.kind === 'postcode') {
    map.flyTo({ center: [result.lon, result.lat], zoom: POSTCODE_ZOOM, duration: 900 });
    showSearchMarker(result.lon, result.lat);
    selectAreaByCode(state.codes[result.areaIndex]);
  } else if (result.kind === 'place') {
    map.flyTo({ center: [result.lon, result.lat], zoom: result.zoom, duration: 900 });
    clearSearchMarker();
  } else if (result.kind === 'branch') {
    const b = result.branch;
    // Make sure the brand is actually on the map before focusing its pin.
    branchState.selected.add(b.b);
    branchState.focused = b;
    if (!branchState.active) {
      branchState.active = true;
      const cb = document.querySelector('#catchment-on');
      if (cb) cb.checked = true;
    }
    renderBranchPanel();
    refreshBranchLayers();
    map.flyTo({ center: [b.x, b.y], zoom: 14, duration: 900 });
    clearSearchMarker();
    selectAreaByCode(b.a);
  }

  const box = document.querySelector('#search');
  if (box) box.blur();
  renderSearchResults([]);
}

/* -------------------------------- UI -------------------------------- */

function renderSearchResults(results) {
  const host = document.querySelector('#search-results');
  if (!host) return;
  searchState.results = results;
  searchState.active = results.length ? 0 : -1;

  if (!results.length) {
    host.innerHTML = '';
    host.hidden = true;
    return;
  }

  const icon = { postcode: 'PC', place: 'Town', branch: 'Bank' };
  host.hidden = false;
  host.innerHTML = results.map((r, i) =>
    '<button type="button" class="search-row' + (i === 0 ? ' active' : '') +
      '" data-i="' + i + '">' +
      '<span class="search-kind k-' + r.kind + '">' + icon[r.kind] + '</span>' +
      '<span class="search-label">' + r.label + '</span>' +
      '<span class="search-detail">' + (r.detail || '') + '</span>' +
    '</button>').join('');

  host.onclick = (e) => {
    const row = e.target.closest('.search-row');
    if (row) applyResult(searchState.results[Number(row.dataset.i)]);
  };
}

function moveSearchSelection(delta) {
  const rows = document.querySelectorAll('#search-results .search-row');
  if (!rows.length) return;
  rows[searchState.active]?.classList.remove('active');
  searchState.active = (searchState.active + delta + rows.length) % rows.length;
  rows[searchState.active].classList.add('active');
  rows[searchState.active].scrollIntoView({ block: 'nearest' });
}

async function runSearch(raw) {
  const q = raw.trim();
  if (q.length < 2) { renderSearchResults([]); return; }

  const results = [];
  if (POSTCODE_START.test(normalisePostcode(q))) {
    results.push(...await searchPostcodes(q));
  }
  results.push(...searchPlaces(q));
  results.push(...searchBranches(q));

  // A later keystroke may have landed while the chunk was in flight.
  const box = document.querySelector('#search');
  if (box && box.value.trim() !== q) return;
  renderSearchResults(results);
}

async function initSearch() {
  try {
    const resp = await fetch('data/gb_search.json');
    if (resp.ok) searchState.places = (await resp.json()).places || [];
  } catch (err) {
    console.warn('no search index', err);
  }

  const box = document.querySelector('#search');
  if (!box) return;

  let timer = null;
  box.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(() => runSearch(box.value), 120);
  });

  box.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); moveSearchSelection(1); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); moveSearchSelection(-1); }
    else if (e.key === 'Enter') {
      e.preventDefault();
      applyResult(searchState.results[searchState.active]);
    } else if (e.key === 'Escape') {
      box.value = '';
      renderSearchResults([]);
      clearSearchMarker();
      box.blur();
    }
  });

  // Clicking away closes the dropdown without losing what was typed.
  document.addEventListener('click', (e) => {
    if (!e.target.closest('#search-panel')) renderSearchResults([]);
  });
}
