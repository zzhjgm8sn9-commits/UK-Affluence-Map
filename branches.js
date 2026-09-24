'use strict';

/* ------------------------------------------------------------------ *
 * Bank branches and catchment analysis.
 *
 * Declared here and called from app.js. Function declarations only at the top
 * level -- nothing runs until app.js has built `state` and `map`.
 *
 * Catchment is computed over area centroids: an area counts as inside a
 * catchment if its representative point falls within the radius. That is a
 * simplification -- areas are in or out, never partly in -- but the areas
 * average about 1,200 adults, so at any radius above a kilometre or so the
 * edge effects mostly cancel. It is also honest in a way that apportioning by
 * overlap area would not be, because it counts exactly the areas whose
 * statistics are being summed.
 *
 * Distances use a local equirectangular projection rather than great-circle
 * maths. Over catchment distances in GB the error is under a tenth of a
 * percent, and it turns the hot loop into subtraction and multiplication.
 * ------------------------------------------------------------------ */

const KM_PER_DEG_LAT = 110.574;
const GRID_KM = 5;            // spatial index cell size
const MAX_CIRCLES = 1500;     // above this, draw coverage instead of circles
const CIRCLE_SEGMENTS = 48;

/* Approximations of each brand's visual identity, so a pin is recognisable at
 * a glance. This deliberately breaks the usual categorical-palette rule: 25
 * hues cannot all be told apart, and several brands are genuinely red (HSBC,
 * Santander, Virgin Money). The mitigation is that colour never carries
 * identity alone -- the filter list doubles as a legend with the same swatches,
 * and hovering a pin names the branch and brand. In practice a comparison is
 * two or three brands at a time, which these colours separate cleanly. */
const BRAND_COLOURS = {
  'Nationwide': '#00263E',
  'Lloyds Bank': '#006A4D',
  'Barclays': '#00AEEF',
  'NatWest': '#42145F',
  'HSBC UK': '#DB0011',
  'Santander': '#EC0000',
  'Halifax': '#005EB8',
  'TSB': '#2C4B9B',
  'Yorkshire Building Society': '#00539F',
  'Bank of Scotland': '#002E5F',
  'RBS': '#002F6C',
  'Virgin Money': '#CC0000',
  'Metro Bank': '#E4002B',
  'The Co-operative Bank': '#00B0B9',
  'Skipton Building Society': '#00833E',
  'Coventry Building Society': '#C8102E',
  'Leeds Building Society': '#E30613',
  'Principality Building Society': '#7D2248',
  'Banking Hub': '#6B6B66',
  'Handelsbanken': '#005AA0',
  'Danske Bank': '#003755',
  'Ulster Bank': '#003057',
  'The Nottingham': '#8A1538',
  'Newcastle Building Society': '#00558C',
  'The West Brom': '#004B87',
  'Cumberland Building Society': '#007A53',
  'Other': '#7A7975',
};
const DEFAULT_BRAND_COLOUR = '#7A7975';

const branchState = {
  all: [],            // every branch
  brands: [],         // [{brand_group, n}]
  selected: new Set(),
  focusedKeys: new Set(),  // individually clicked branches, in click order
  radiusKm: 10,
  active: false,
  grid: null,         // cell key -> array of area indices
  ax: null,           // area centroid x, km
  ay: null,           // area centroid y, km
  covered: null,      // Uint8Array flag per area
  lastTotals: null,
  byKey: null,        // feature id -> branch record
  national: null,     // GB band totals, the coverage denominator
  // 'composition' = of the people reached, how are they distributed?
  // 'coverage'    = of GB's people in each band, how many are reached?
  statsMode: 'composition',
  coverageSort: 'coverage',
  verified: new Set(),   // brands checked against the operator's own list
  unverifiable: {},      // brand -> why it could not be checked
};

/* OSM records that a branch exists far more reliably than that one has closed,
 * so every unverified brand is over-counted -- and by an unknown amount, which
 * is why this is a marker rather than a correction. Barclays is 421 OSM
 * records against 211 branches Barclays itself publishes. Until every brand is
 * checked, a verified one looks smaller than its rivals for the wrong reason,
 * and the map has to say so wherever brands are compared. */
function verifiedMark(brand) {
  if (!branchState.verified.has(brand)) return '';
  return '<i class="verified" title="Checked against this bank\u2019s own ' +
    'published branch list; closed records removed">\u2713</i>';
}

function verifiedNote() {
  const names = [...branchState.verified];
  if (!names.length) {
    return 'Counts come from OpenStreetMap, which records openings far more ' +
      'reliably than closures, so every brand here is over-counted.';
  }
  // The brands that could not be checked are the ones a reader would most
  // likely compare against, so they are named, with the reason.
  const blocked = Object.entries(branchState.unverifiable)
    .map(([brand, why]) => brand + ' (' + why + ')');
  return '\u2713 marks the ' + names.length + ' brands checked against the ' +
    'bank\u2019s own published list, with closed records removed. ' +
    (blocked.length ? 'Not checked: ' + blocked.join('; ') + '. ' : '') +
    'Unticked brands are OpenStreetMap only, which records openings far more ' +
    'reliably than closures, so they are over-counted by an unknown amount ' +
    'and comparisons flatter them.';
}

function brandColour(brand) {
  return BRAND_COLOURS[brand] || DEFAULT_BRAND_COLOUR;
}

/** A MapLibre image id has to be a plain string; brand names are not. */
function pinId(brand) {
  return 'pin-' + brand.toLowerCase().replace(/[^a-z0-9]+/g, '-');
}

function lonScale(lat) {
  return KM_PER_DEG_LAT * Math.cos(lat * Math.PI / 180);
}

/* ---------------------------- spatial index ---------------------------- */

function buildAreaGrid() {
  const lat = state.values.__lat, lon = state.values.__lon;
  if (!lat || !lon) return false;

  const n = state.codes.length;
  const ax = new Float64Array(n), ay = new Float64Array(n);
  const grid = new Map();

  for (let i = 0; i < n; i++) {
    if (!Number.isFinite(lat[i]) || !Number.isFinite(lon[i])) {
      ax[i] = NaN; ay[i] = NaN; continue;
    }
    const y = lat[i] * KM_PER_DEG_LAT;
    const x = lon[i] * lonScale(lat[i]);
    ax[i] = x; ay[i] = y;
    const key = Math.floor(x / GRID_KM) + ':' + Math.floor(y / GRID_KM);
    let cell = grid.get(key);
    if (!cell) { cell = []; grid.set(key, cell); }
    cell.push(i);
  }

  branchState.ax = ax;
  branchState.ay = ay;
  branchState.grid = grid;
  return true;
}

function markCovered(lat, lon, radiusKm, covered) {
  const { ax, ay, grid } = branchState;
  const y = lat * KM_PER_DEG_LAT;
  const x = lon * lonScale(lat);
  const r2 = radiusKm * radiusKm;

  const cx0 = Math.floor((x - radiusKm) / GRID_KM);
  const cx1 = Math.floor((x + radiusKm) / GRID_KM);
  const cy0 = Math.floor((y - radiusKm) / GRID_KM);
  const cy1 = Math.floor((y + radiusKm) / GRID_KM);

  for (let cx = cx0; cx <= cx1; cx++) {
    for (let cy = cy0; cy <= cy1; cy++) {
      const cell = grid.get(cx + ':' + cy);
      if (!cell) continue;
      for (const i of cell) {
        if (covered[i]) continue;
        const dx = ax[i] - x, dy = ay[i] - y;
        if (dx * dx + dy * dy <= r2) covered[i] = 1;
      }
    }
  }
}

function selectedBranches() {
  if (!branchState.selected.size) return [];
  return branchState.all.filter((b) => branchState.selected.has(b.b));
}

/** The individually clicked branches, in the order they were clicked. */
function focusedBranches() {
  const out = [];
  for (const key of branchState.focusedKeys) {
    const b = branchState.byKey && branchState.byKey.get(key);
    if (b) out.push(b);
  }
  return out;
}

/** Branches whose catchment is currently being measured: the clicked ones if
 *  any have been clicked, otherwise every branch of the ticked brands. */
function catchmentBranches() {
  const focused = focusedBranches();
  return focused.length ? focused : selectedBranches();
}

function computeCatchment(branches) {
  const n = state.codes.length;
  const covered = new Uint8Array(n);
  for (const b of branches) markCovered(b.y, b.x, branchState.radiusKm, covered);

  const bandArrays = (state.bandLabels || []).map((_, k) => state.values['band_' + k]);
  const adults = state.values.adults;
  const totals = { areas: 0, adults: 0, bands: new Array(bandArrays.length).fill(0) };

  for (let i = 0; i < n; i++) {
    if (!covered[i]) continue;
    totals.areas++;
    if (adults && Number.isFinite(adults[i])) totals.adults += adults[i];
    for (let k = 0; k < bandArrays.length; k++) {
      const arr = bandArrays[k];
      if (arr && Number.isFinite(arr[i])) totals.bands[k] += arr[i];
    }
  }

  branchState.covered = covered;
  return totals;
}

function circlePolygon(lat, lon, radiusKm) {
  const coords = [];
  const dLat = radiusKm / KM_PER_DEG_LAT;
  const dLon = radiusKm / lonScale(lat);
  for (let i = 0; i <= CIRCLE_SEGMENTS; i++) {
    const t = (i / CIRCLE_SEGMENTS) * 2 * Math.PI;
    coords.push([lon + dLon * Math.cos(t), lat + dLat * Math.sin(t)]);
  }
  return { type: 'Feature', properties: {}, geometry: { type: 'Polygon', coordinates: [coords] } };
}

/* ------------------------------ pin icons ------------------------------ */

/** Draw a map pin in `colour` and hand it to MapLibre as an image.
 *  Drawn at twice the display size and registered with pixelRatio 2 so the
 *  edges stay crisp on high-density screens. */
function makePinImage(colour, w = 44, h = 60) {
  const canvas = document.createElement('canvas');
  canvas.width = w; canvas.height = h;
  const ctx = canvas.getContext('2d');

  const cx = w / 2;
  const r = w * 0.34;
  const cy = r + 3;
  const tipY = h - 3;
  const gap = Math.PI * 0.30;   // half-angle of the opening at the bottom

  ctx.beginPath();
  // The long way round the top, leaving a gap the shoulders run down from.
  ctx.arc(cx, cy, r, Math.PI / 2 - gap, Math.PI / 2 + gap, true);
  ctx.lineTo(cx, tipY);
  ctx.closePath();

  ctx.fillStyle = colour;
  ctx.fill();
  ctx.lineWidth = w * 0.075;
  ctx.strokeStyle = '#ffffff';
  ctx.stroke();

  // A light centre so dark brand colours still read as a pin, not a blob.
  ctx.beginPath();
  ctx.arc(cx, cy, r * 0.36, 0, Math.PI * 2);
  ctx.fillStyle = 'rgba(255,255,255,0.92)';
  ctx.fill();

  return ctx.getImageData(0, 0, w, h);
}

function registerPinImages() {
  const brands = new Set(branchState.brands.map((b) => b.brand_group));
  brands.add('Other');
  for (const brand of brands) {
    const id = pinId(brand);
    if (map.hasImage(id)) continue;
    map.addImage(id, makePinImage(brandColour(brand)), { pixelRatio: 2 });
  }
}

/* ------------------------------ layers ------------------------------ */

function emptyCollection() {
  return { type: 'FeatureCollection', features: [] };
}

function addBranchLayers() {
  registerPinImages();

  map.addSource('branches', {
    type: 'geojson', data: emptyCollection(),
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors (ODbL)',
  });
  map.addSource('catchment', { type: 'geojson', data: emptyCollection() });

  map.addLayer({
    id: 'catchment-fill',
    type: 'fill',
    source: 'catchment',
    paint: { 'fill-color': '#0b0b0b', 'fill-opacity': 0.06 },
  });
  map.addLayer({
    id: 'catchment-line',
    type: 'line',
    source: 'catchment',
    paint: { 'line-color': '#0b0b0b', 'line-width': 1, 'line-opacity': 0.35 },
  });

  // A halo under the clicked pin, so the focused branch is obvious.
  map.addLayer({
    id: 'branch-focus',
    type: 'circle',
    source: 'branches',
    filter: ['==', ['get', 'id'], '__none__'],
    paint: {
      'circle-radius': ['interpolate', ['linear'], ['zoom'], 5, 9, 12, 16],
      'circle-color': '#0b0b0b',
      'circle-opacity': 0.14,
      'circle-stroke-color': '#0b0b0b',
      'circle-stroke-width': 1.5,
      'circle-stroke-opacity': 0.5,
    },
  });

  map.addLayer({
    id: 'branch-points',
    type: 'symbol',
    source: 'branches',
    layout: {
      'icon-image': ['get', 'pin'],
      'icon-anchor': 'bottom',
      'icon-allow-overlap': true,
      'icon-ignore-placement': true,
      'icon-size': ['interpolate', ['linear'], ['zoom'], 5, 0.42, 9, 0.62, 13, 0.9],
    },
  });
}

function refreshBranchLayers() {
  const shown = selectedBranches();
  const measured = catchmentBranches();

  map.getSource('branches').setData({
    type: 'FeatureCollection',
    features: shown.map((b) => ({
      type: 'Feature',
      properties: {
        id: b.x + ',' + b.y + ',' + b.n,
        brand: b.b, name: b.n, area_code: b.a, pin: pinId(b.b),
        connections: b.c == null ? null : b.c,
      },
      geometry: { type: 'Point', coordinates: [b.x, b.y] },
    })),
  });

  map.setFilter('branch-focus',
    ['in', ['get', 'id'], ['literal', Array.from(branchState.focusedKeys)]]);

  // Drawing thousands of overlapping rings is slow and unreadable; past the cap
  // the dimming of areas outside the catchment carries the message instead.
  const showCircles = branchState.active && measured.length > 0 &&
                      measured.length <= MAX_CIRCLES;
  map.getSource('catchment').setData(showCircles
    ? { type: 'FeatureCollection',
        features: measured.map((b) => circlePolygon(b.y, b.x, branchState.radiusKm)) }
    : emptyCollection());

  applyCatchmentDimming(measured);
  renderCatchmentStats(measured);
}

function branchKey(b) {
  return b.x + ',' + b.y + ',' + b.n;
}

let lastCovered = null;

function applyCatchmentDimming(branches) {
  const hoverFull = ['boolean', ['feature-state', 'hover'], false];
  if (!branchState.active || !branches.length) {
    map.setPaintProperty('areas-fill', 'fill-opacity', ['case', hoverFull, 1, 0.88]);
    branchState.lastTotals = null;
    return;
  }

  branchState.lastTotals = computeCatchment(branches);
  const covered = branchState.covered;
  // Dragging the radius moves the catchment edge, not its interior: only a
  // thin ring of areas changes state per step, so pushing all 43,064 every
  // time was most of the cost of moving the slider.
  for (let i = 0; i < state.codes.length; i++) {
    if (lastCovered && lastCovered[i] === covered[i]) continue;
    map.setFeatureState({ source: 'areas', id: state.codes[i] },
      { covered: covered[i] === 1 });
  }
  lastCovered = covered;
  map.setPaintProperty('areas-fill', 'fill-opacity',
    ['case', hoverFull, 1, ['boolean', ['feature-state', 'covered'], false], 0.92, 0.12]);
}

/* -------------------------------- UI -------------------------------- */

async function initBranches() {
  let payload;
  try {
    const resp = await fetch('data/gb_branches.json', {cache: 'no-store'});
    if (!resp.ok) return;
    payload = await resp.json();
  } catch (err) {
    console.warn('no branches file', err);
    return;
  }

  branchState.all = payload.branches;
  branchState.brands = payload.brands;
  branchState.verified = new Set(payload.verified_brands || []);
  branchState.unverifiable = payload.unverifiable || {};
  // Index by the same key the map features carry, so a clicked pin can be
  // resolved back to its branch record.
  branchState.byKey = new Map(branchState.all.map((b) => [branchKey(b), b]));
  if (!buildAreaGrid()) {
    console.warn('no area centroids; catchment disabled');
    return;
  }

  addBranchLayers();
  renderBranchPanel();
  attachBranchInteractions();
  refreshBranchLayers();
  renderBrandCoverage();
}

function renderBranchPanel() {
  const host = document.querySelector('#branches');
  if (!host) return;

  host.innerHTML =
    '<h2>Bank branches</h2>' +
    '<div class="branch-actions">' +
      '<button type="button" id="brands-all">Select all</button>' +
      '<button type="button" id="brands-none">Clear</button>' +
    '</div>' +
    '<div class="brand-list">' +
      branchState.brands.map((b) =>
        '<label class="brand-row"><input type="checkbox" value="' + b.brand_group + '"' +
        (branchState.selected.has(b.brand_group) ? ' checked' : '') + '>' +
        '<i class="brand-dot" style="background:' + brandColour(b.brand_group) + '"></i>' +
        '<span class="brand-name">' + b.brand_group + verifiedMark(b.brand_group) +
        '</span>' +
        '<span class="brand-n">' + b.n.toLocaleString('en-GB') + '</span></label>').join('') +
    '</div>' +
    '<div class="weight" style="margin-top:12px">' +
      '<div class="weight-head"><span class="label">Catchment radius</span>' +
      numberField('radius-num', branchState.radiusKm, 0.5, 40, 0.5, 'km') + '</div>' +
      '<input type="range" id="radius" min="0.5" max="40" step="0.5" value="' +
        branchState.radiusKm + '">' +
    '</div>' +
    '<label class="catchment-toggle"><input type="checkbox" id="catchment-on"' +
      (branchState.active ? ' checked' : '') + '> Show catchment</label>' +
    '<p class="legend-note">' + branchState.all.length.toLocaleString('en-GB') +
      ' branches from OpenStreetMap (ODbL). ' + verifiedNote() +
      ' Click a pin to measure that branch alone; click the sea to clear.</p>' +
    '<div id="catchment-stats"></div>';

  host.querySelector('.brand-list').onchange = (e) => {
    const cb = e.target;
    if (cb.checked) branchState.selected.add(cb.value);
    else {
      branchState.selected.delete(cb.value);
      // Focused pins whose brand was just unticked are no longer on the map.
      for (const b of focusedBranches()) {
        if (b.b === cb.value) branchState.focusedKeys.delete(branchKey(b));
      }
    }
    refreshBranchLayers();
  };
  host.querySelector('#brands-all').onclick = () => {
    branchState.brands.forEach((b) => branchState.selected.add(b.brand_group));
    renderBranchPanel(); refreshBranchLayers();
  };
  host.querySelector('#brands-none').onclick = () => {
    branchState.selected.clear();
    branchState.focusedKeys.clear();
    renderBranchPanel(); refreshBranchLayers();
  };
  const setRadius = (km, echoTo) => {
    branchState.radiusKm = km;
    if (echoTo !== 'slider') host.querySelector('#radius').value = String(km);
    if (echoTo !== 'number') host.querySelector('#radius-num').value = String(km);
    refreshCatchment();
  };

  host.querySelector('#radius').oninput = (e) => {
    setRadius(Number(e.target.value), 'slider');
  };
  host.querySelector('#radius-num').oninput = (e) => {
    // Ignore half-typed values rather than snapping the map to 0.5 km while
    // someone is still reaching for the second digit.
    const v = Number(e.target.value);
    if (e.target.value === '' || !Number.isFinite(v) || v < 0.5 || v > 40) return;
    setRadius(v, 'number');
  };
  host.querySelector('#radius-num').onchange = (e) => {
    const v = clampTo(Math.round(Number(e.target.value) * 2) / 2, 0.5, 40,
                      branchState.radiusKm);
    e.target.value = String(v);
    setRadius(v);
  };
  host.querySelector('#catchment-on').onchange = (e) => {
    branchState.active = e.target.checked;
    refreshBranchLayers();
  };
}

function renderCatchmentStats(branches) {
  const host = document.querySelector('#catchment-stats');
  if (!host) return;

  const focused = focusedBranches();
  const one = focused.length === 1 ? focused[0] : null;

  if (!branches.length) {
    host.innerHTML = '<p class="empty">Select one or more brands.</p>';
    return;
  }
  if (!branchState.active) {
    host.innerHTML = '<p class="empty">' +
      (one ? one.n + ' selected. '
       : focused.length ? focused.length + ' branches selected. '
       : branches.length.toLocaleString('en-GB') + ' branches shown. ') +
      'Tick &ldquo;Show catchment&rdquo; for population reach.</p>';
    return;
  }

  const t = branchState.lastTotals;
  if (!t) { host.innerHTML = ''; return; }

  const labels = state.bandLabels || [];
  const above100k = t.bands.slice(5).reduce((s, v) => s + v, 0);
  const national = nationalBandTotals();
  const coverage = branchState.statsMode === 'coverage';

  // Two different questions, and the denominator is the whole difference:
  //   composition - of the people this network reaches, how many are rich?
  //   coverage    - of the rich people in GB, how many does it reach?
  const denom = (k) => coverage ? national.bands[k] : t.adults;
  const headline = coverage
    ? (national.above100k ? 100 * above100k / national.above100k : 0)
    : (t.adults ? 100 * above100k / t.adults : 0);

  // Composition bars scale to the biggest band, because one band always
  // dominates and absolute widths would be unreadable. Coverage bars scale to
  // a true 100%, because the absolute level is the point.
  const peak = Math.max(...t.bands);
  const barWidth = (k) => {
    if (coverage) {
      const d = national.bands[k];
      return d > 0 ? 100 * t.bands[k] / d : 0;
    }
    return peak > 0 ? 100 * t.bands[k] / peak : 0;
  };

  const heading = one
    ? '<h2>' + one.n + '</h2>' +
      '<div class="focus-brand"><i class="brand-dot" style="background:' +
        brandColour(one.b) + '"></i>' + one.b + ' &middot; single branch</div>'
    : focused.length
    ? '<h2>' + focused.length + ' branches</h2>' +
      '<div class="focus-brand">' + focusBrandChips(focused) + '</div>'
    : '<h2>Catchment reach</h2>';

  const above100kLabel = coverage
    ? 'Share of GB £100k+ reached'
    : 'Adults on £100k+';
  const above100kValue = coverage
    ? headline.toFixed(1) + '% (' + fmt.count(above100k) + ')'
    : fmt.count(above100k) + ' (' + headline.toFixed(1) + '%)';

  const rows = [
    ['Radius', branchState.radiusKm.toFixed(1) + ' km'],
    ['Branches', branches.length.toLocaleString('en-GB')],
    ...connectionRows(focused, branches),
    ['Small areas covered', t.areas.toLocaleString('en-GB')],
    ['Adults reached', fmt.count(t.adults) +
      (coverage && national.adults
        ? ' (' + (100 * t.adults / national.adults).toFixed(1) + '% of GB)' : '')],
    [above100kLabel, above100kValue],
  ];

  host.innerHTML =
    '<div class="bands">' + heading +
    '<div class="stats-mode">' +
      '<button type="button" data-mode="composition"' +
        (coverage ? '' : ' class="on"') + '>Composition</button>' +
      '<button type="button" data-mode="coverage"' +
        (coverage ? ' class="on"' : '') + '>Coverage</button>' +
    '</div>' +
    '<dl class="sel-rows">' + rows.map(([k, v]) =>
      '<div class="sel-row"><dt>' + k + '</dt><dd>' + v + '</dd></div>').join('') + '</dl>' +
    labels.map((label, k) => {
      const v = t.bands[k];
      const d = denom(k);
      const pct = d > 0 ? 100 * v / d : 0;
      return '<div class="band-row">' +
        '<span class="band-label">' + label + '</span>' +
        '<span class="band-bar"><i style="width:' + barWidth(k).toFixed(1) + '%"></i></span>' +
        '<span class="band-pct">' + pct.toFixed(1) + '%</span>' +
        '<span class="band-n">' + fmt.count(v) + '</span>' +
        '</div>';
    }).join('') +
    '<p class="legend-note">' + (coverage
      ? 'Percentages are the share of <em>all GB adults in that band</em> who ' +
        'fall inside the catchment.'
      : 'Percentages are the share of <em>the population reached</em> that sits ' +
        'in each band.') + '</p>' +
    '<p class="legend-note">' + (focused.length
      ? (one ? 'This branch only. ' : 'These ' + focused.length + ' branches only. ') +
        'Click another pin to add it, a selected pin to drop it, or the sea to ' +
        'go back to the whole brand selection.'
      : 'Union of all selected catchments, so overlapping branches are counted ' +
        'once. An area is in or out by its centre point.') + '</p>' +
    '</div>';

  const modes = host.querySelector('.stats-mode');
  if (modes) {
    modes.onclick = (e) => {
      const btn = e.target.closest('button');
      if (!btn) return;
      branchState.statsMode = btn.dataset.mode;
      renderCatchmentStats(branches);
    };
  }
}

/** Travel-connections rows: the branch's own rating, or the network's average.
 *
 *  A proxy for how reachable a branch is without a car -- nearest station,
 *  how many modes are within a walk, bus density, and how many people live
 *  close enough to walk in. Not a drive time, and not trying to be one. */
function focusBrandChips(focused) {
  const brands = [];
  for (const b of focused) if (!brands.includes(b.b)) brands.push(b.b);
  return brands.map((brand) =>
    '<span class="focus-chip"><i class="brand-dot" style="background:' +
    brandColour(brand) + '"></i>' + brand + '</span>').join('');
}

function connectionRows(focused, branches) {
  if (focused.length === 1) {
    const only = focused[0];
    if (only.c == null) return [];
    const d = only.cd || [];
    const rail = d[0] == null ? 'none within 6 km'
      : d[0] < 1000 ? d[0] + ' m' : (d[0] / 1000).toFixed(1) + ' km';
    return [
      ['Connections', '<strong>' + only.c.toFixed(1) + '</strong> / 10'],
      ['&nbsp;&nbsp;Nearest station', rail],
      ['&nbsp;&nbsp;Transport modes', String(d[1] ?? '--')],
      ['&nbsp;&nbsp;Bus stops within 500 m', String(d[2] ?? '--')],
      ['&nbsp;&nbsp;Adults within 800 m', fmt.count(d[3])],
    ];
  }

  const rated = branches.filter((b) => b.c != null);
  if (!rated.length) return [];
  const mean = rated.reduce((s, b) => s + b.c, 0) / rated.length;
  const rows = [['Mean connections', mean.toFixed(1) + ' / 10']];
  if (focused.length > 1) {
    const best = rated.reduce((a, b) => (b.c > a.c ? b : a));
    rows.push(['Best connected', best.n + ' (' + best.c.toFixed(1) + ')']);
  }
  return rows;
}

/** GB totals per income band, for the coverage denominator. Computed once. */
function nationalBandTotals() {
  if (branchState.national) return branchState.national;

  const labels = state.bandLabels || [];
  const sum = (arr) => {
    let s = 0;
    if (arr) for (let i = 0; i < arr.length; i++) {
      if (Number.isFinite(arr[i])) s += arr[i];
    }
    return s;
  };

  const bands = labels.map((_, k) => sum(state.values['band_' + k]));
  branchState.national = {
    bands,
    adults: sum(state.values.adults),
    above100k: bands.slice(5).reduce((s, v) => s + v, 0),
  };
  return branchState.national;
}

/** Clear both the area selection and every focused branch. */
function clearAllSelection() {
  let changed = false;
  if (branchState.focusedKeys.size) { branchState.focusedKeys.clear(); changed = true; }
  if (state.selection.length) { clearAreaSelection(); }
  if (changed) refreshBranchLayers();
}

/* refreshBranchLayers recomputes the catchment, reclasses every area and
 * rebuilds the stats panel. A dragged radius slider fires far faster than that
 * can run, so coalesce to one pass per frame. */
let catchmentQueued = false;
function refreshCatchment() {
  if (catchmentQueued) return;
  catchmentQueued = true;
  requestAnimationFrame(() => {
    catchmentQueued = false;
    refreshBranchLayers();
    renderBrandCoverage();
  });
}

function attachBranchInteractions() {
  map.on('mouseenter', 'branch-points', () => { map.getCanvas().style.cursor = 'pointer'; });
  map.on('mouseleave', 'branch-points', () => {
    map.getCanvas().style.cursor = '';
    document.querySelector('#tooltip').style.display = 'none';
  });
  map.on('mousemove', 'branch-points', (e) => {
    const f = e.features && e.features[0];
    if (!f) return;
    const el = document.querySelector('#tooltip');
    const c = f.properties.connections;
    el.innerHTML = '<div class="t-name">' + f.properties.name + '</div>' +
      '<div class="t-meta">' + f.properties.brand + '</div>' +
      (c == null ? '' :
        '<div class="t-val">Connections: <strong>' + c.toFixed(1) + '</strong>/10</div>');
    el.style.display = 'block';
    el.style.left = (e.point.x + 14) + 'px';
    el.style.top = (e.point.y + 14) + 'px';
  });

  map.on('click', 'branch-points', (e) => {
    const f = e.features && e.features[0];
    if (!f) return;
    // Look up by the feature's own id, never by its coordinates: MapLibre
    // round-trips GeoJSON through internal tile encoding, which quantises
    // them, so the floats coming back never equal the ones that went in.
    const match = branchState.byKey.get(f.properties.id);
    if (!match) { console.warn('unmatched pin', f.properties.id); return; }
    const key = branchKey(match);
    // Pins accumulate the same way areas do: click to add, click again to drop.
    if (branchState.focusedKeys.has(key)) branchState.focusedKeys.delete(key);
    else branchState.focusedKeys.add(key);
    // Measuring named branches is only meaningful with the catchment drawn, so
    // turn it on rather than making the click appear to do nothing.
    if (!branchState.active && branchState.focusedKeys.size) {
      branchState.active = true;
      const cb = document.querySelector('#catchment-on');
      if (cb) cb.checked = true;
    }
    refreshBranchLayers();
  });

  // A click that lands on neither a pin nor an area -- the sea, or off the
  // coast -- clears whatever was selected.
  map.on('click', (e) => {
    const hits = map.queryRenderedFeatures(e.point,
      { layers: ['branch-points', 'areas-fill'] });
    if (!hits.length) clearAllSelection();
  });
}

/* ------------------------- network coverage ------------------------- */

/* "Which bank is closest to the people this metric is about?"
 *
 * Every brand's whole network is measured at the current radius, whether or not
 * it is ticked on the map, because the answer is a comparison. Two numbers make
 * it, and they say different things:
 *
 *   Coverage -- the share of the metric's target population inside the network's
 *               catchment. Big networks win; that is the honest answer to "who
 *               reaches most of them".
 *   Focus    -- that share divided by the network's share of all adults. Above
 *               1.0 means the branches sit where the metric is high rather than
 *               merely everywhere, which is what separates a targeted network
 *               from a large one.
 *
 * These are not brand groups anyone runs, so they are left out of the table.
 */
const COVERAGE_EXCLUDE = new Set(['Other', 'Unknown']);

/* Dropdown labels are column headings -- "Higher managerial & professional" --
 * and do not survive being dropped into a sentence about people. These do. */
const TARGET_NOUNS = {
  pct_100k_plus: 'adults on £100k+',
  nssec_higher: 'higher managerial &amp; professional residents',
  qual_level4: 'degree-qualified residents',
  cars_2plus: 'residents in households with two or more cars',
};

const coverageCache = { radius: null, masks: null };
let coverageTimer = null;

function brandCoverageMasks() {
  if (coverageCache.masks && coverageCache.radius === branchState.radiusKm) {
    return coverageCache.masks;
  }
  const n = state.codes.length;
  const masks = new Map();
  for (const b of branchState.all) {
    if (COVERAGE_EXCLUDE.has(b.b)) continue;
    let m = masks.get(b.b);
    if (!m) { m = new Uint8Array(n); masks.set(b.b, m); }
    markCovered(b.y, b.x, branchState.radiusKm, m);
  }
  coverageCache.masks = masks;
  coverageCache.radius = branchState.radiusKm;
  return masks;
}

function adultsArray() {
  return state.values.adults || weightsFor('population');
}

/* The population a metric is actually about.
 *
 * For a share there is a real headcount behind it -- the adults over £100k, the
 * degree holders -- and multiplying the percentage by the people it describes
 * recovers it. For a level (a price, a modelled median) no such headcount
 * exists, so the target is the adults living in the top fifth of the country by
 * that measure. Both end up as "people per area", which is what a catchment can
 * sum. */
function targetMass(key) {
  const n = state.codes.length;
  const meta = metricMeta(key);
  if (!meta) return null;

  if (key === 'pct_100k_plus' && state.values.n_100k_plus) {
    return { arr: state.values.n_100k_plus, noun: TARGET_NOUNS[key], exact: true };
  }

  if (meta.format === 'pct') {
    const vals = state.values[key];
    const w = weightsFor(key);
    if (vals && w) {
      const arr = new Float64Array(n);
      for (let i = 0; i < n; i++) {
        arr[i] = Number.isFinite(vals[i]) && Number.isFinite(w[i])
          ? vals[i] * w[i] / 100 : 0;
      }
      return { arr, noun: TARGET_NOUNS[key] || meta.label.toLowerCase(), exact: true };
    }
  }

  const vals = key === 'affluence_index' ? indexValues() : state.values[key];
  const w = adultsArray();
  if (!vals || !w) return null;
  const order = [];
  let total = 0;
  for (let i = 0; i < n; i++) {
    if (Number.isFinite(vals[i]) && Number.isFinite(w[i]) && w[i] > 0) {
      order.push(i); total += w[i];
    }
  }
  if (!total) return null;
  order.sort((a, b) => vals[b] - vals[a]);
  const arr = new Float64Array(n);
  let acc = 0;
  for (const i of order) {
    if (acc >= 0.2 * total) break;
    arr[i] = w[i];
    acc += w[i];
  }
  return {
    arr,
    noun: 'adults in the top fifth of areas by ' + meta.label.toLowerCase(),
    exact: false,
  };
}

function sumOver(mask, arr) {
  let s = 0;
  for (let i = 0; i < mask.length; i++) {
    if (mask[i] && Number.isFinite(arr[i])) s += arr[i];
  }
  return s;
}

function nationalSum(arr) {
  let s = 0;
  for (let i = 0; i < arr.length; i++) if (Number.isFinite(arr[i])) s += arr[i];
  return s;
}

function coverageHeading() {
  return '<h2>Network coverage</h2>';
}

function renderBrandCoverage() {
  const host = document.querySelector('#brand-coverage');
  if (!host) return;
  if (!branchState.all.length || !branchState.grid) { host.innerHTML = ''; return; }

  if (state.metric === 'nation') {
    host.innerHTML = coverageHeading() +
      '<p class="empty">Pick a numeric metric to compare branch networks.</p>';
    return;
  }

  // The first pass at a new radius walks all 4,698 branches; render the
  // heading now and do the work off the paint, or the slider stutters.
  if (!coverageCache.masks || coverageCache.radius !== branchState.radiusKm) {
    host.innerHTML = coverageHeading() + '<p class="empty">Measuring every ' +
      'network within ' + branchState.radiusKm.toFixed(1) + ' km&hellip;</p>';
    clearTimeout(coverageTimer);
    coverageTimer = setTimeout(() => { brandCoverageMasks(); drawBrandCoverage(); }, 20);
    return;
  }
  drawBrandCoverage();
}

function drawBrandCoverage() {
  const host = document.querySelector('#brand-coverage');
  if (!host) return;

  const target = targetMass(state.metric);
  const adults = adultsArray();
  if (!target || !adults) { host.innerHTML = ''; return; }

  const masks = brandCoverageMasks();
  const targetTotal = nationalSum(target.arr);
  const adultTotal = nationalSum(adults);
  if (!targetTotal || !adultTotal) { host.innerHTML = ''; return; }

  const counts = new Map(branchState.brands.map((b) => [b.brand_group, b.n]));
  const rows = [];
  for (const [brand, mask] of masks) {
    const cov = 100 * sumOver(mask, target.arr) / targetTotal;
    const reach = 100 * sumOver(mask, adults) / adultTotal;
    rows.push({
      brand,
      n: counts.get(brand) || 0,
      cov,
      reach,
      lift: reach > 0 ? cov / reach : 0,
    });
  }

  const byFocus = branchState.coverageSort === 'focus';
  rows.sort((a, b) => (byFocus ? b.lift - a.lift : b.cov - a.cov));

  const peak = Math.max(1, ...rows.map((r) => r.lift));

  const list = rows.map((r) => {
    const width = byFocus ? 100 * r.lift / peak : r.cov;
    const value = byFocus ? r.lift.toFixed(2) + '&times;' : r.cov.toFixed(1) + '%';
    const title = r.brand + ' — ' + r.n.toLocaleString('en-GB') + ' branches · ' +
      r.cov.toFixed(1) + '% of ' + target.noun + ' · ' +
      r.reach.toFixed(1) + '% of all adults · focus ' + r.lift.toFixed(2) + '×';
    return '<div class="cov-row" title="' + title.replace(/"/g, '&quot;') + '">' +
      '<i class="brand-dot" style="background:' + brandColour(r.brand) + '"></i>' +
      '<span class="cov-name">' + r.brand + verifiedMark(r.brand) + '</span>' +
      '<span class="cov-bar"><i style="width:' + width.toFixed(1) + '%"></i></span>' +
      '<span class="cov-val">' + value + '</span></div>';
  }).join('');

  host.innerHTML = coverageHeading() +
    '<div class="stats-mode cov-mode">' +
      '<button type="button" data-sort="coverage"' + (byFocus ? '' : ' class="on"') +
        ' title="Share of the target population within reach">Coverage</button>' +
      '<button type="button" data-sort="focus"' + (byFocus ? ' class="on"' : '') +
        ' title="Coverage relative to the network\'s share of all adults">Focus</button>' +
    '</div>' +
    '<p class="legend-note cov-lead">' + (byFocus
      ? 'Each network&rsquo;s coverage of ' + target.noun + ' divided by its ' +
        'reach into the adult population at large. Above 1.0&times; means the ' +
        'branches sit where those people are concentrated rather than simply ' +
        'wherever there are people.'
      : 'Share of GB&rsquo;s ' + target.noun + ' within ' +
        branchState.radiusKm.toFixed(1) + ' km of a branch of each brand.') +
    '</p>' +
    '<div class="cov-list">' + list + '</div>' +
    '<p class="legend-note">' + (target.exact
      ? 'Counted from each area&rsquo;s own estimate, weighted by the people it ' +
        'describes.'
      : 'This measure has no headcount of its own, so the target is the adults ' +
        'living in the highest-scoring fifth of the country.') +
    ' Radius follows the catchment slider. Networks overlap heavily, so these ' +
    'shares do not add to 100%. Groups labelled &ldquo;Other&rdquo; and ' +
    '&ldquo;Unknown&rdquo; are left out.</p>' +
    (branchState.verified.size
      ? '<p class="legend-note warn">Read this ranking with care. ' +
        verifiedNote() + ' A network with closed branches still counted reaches ' +
        'further here than it does in life.</p>'
      : '');

  const modes = host.querySelector('.cov-mode');
  if (modes) {
    modes.onclick = (e) => {
      const btn = e.target.closest('button');
      if (!btn) return;
      branchState.coverageSort = btn.dataset.sort;
      drawBrandCoverage();
    };
  }
}

/* ---------------------------- place labels ---------------------------- */

/* A choropleth of 43,064 anonymous polygons is hard to orient in. Labels are
 * ranked rather than assigned fixed zoom thresholds: the rank goes to MapLibre
 * as a symbol sort key and collision detection drops the least important ones
 * wherever it is crowded, so the map thins itself out as you zoom rather than
 * switching layers on at arbitrary levels. */

// Places ranked above this are the ones that orient the country on their own.
// 16 rather than 12 because OSM populations are tagged inconsistently -- some
// are city proper, some the whole district -- which pushes Newcastle and
// Leicester below Wakefield. Widening the band gets the recognisable landmarks
// in without letting the national view get busy.
const MAJOR_PLACE_RANK = 16;

function placeLabelColours() {
  const css = getComputedStyle(document.documentElement);
  const ink = css.getPropertyValue('--text-primary').trim() || '#0b0b0b';
  const halo = css.getPropertyValue('--surface-1').trim() || '#fcfcfb';
  return { ink, halo };
}

function applyPlaceLabelColours() {
  const { ink, halo } = placeLabelColours();
  for (const id of ['place-labels', 'place-labels-minor']) {
    if (!map.getLayer(id)) continue;
    map.setPaintProperty(id, 'text-color', ink);
    map.setPaintProperty(id, 'text-halo-color', halo);
  }
}

async function initPlaces() {
  let payload;
  try {
    const resp = await fetch('data/gb_places.json', {cache: 'no-store'});
    if (!resp.ok) return;
    payload = await resp.json();
  } catch (err) {
    console.warn('no places file', err);
    return;
  }

  map.addSource('places', {
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors (ODbL)',
    type: 'geojson',
    data: {
      type: 'FeatureCollection',
      features: payload.places.map((p) => ({
        type: 'Feature',
        properties: { n: p.n, r: p.r, k: p.k },
        geometry: { type: 'Point', coordinates: [p.x, p.y] },
      })),
    },
  });

  const { ink, halo } = placeLabelColours();

  // Two layers rather than one with a size ramp. Scaling minor labels towards
  // zero still leaves them holding collision space and reading as clutter at
  // national zoom; a layer-level minzoom keeps them genuinely absent until
  // there is room. Major cities alone orient the country at a glance.
  const common = {
    'text-field': ['get', 'n'],
    'text-font': ['Noto Sans Bold'],
    'symbol-sort-key': ['get', 'r'],
    'text-padding': 8,
    'text-max-width': 9,
  };
  const paint = {
    'text-color': ink,
    'text-halo-color': halo,
    'text-halo-width': 1.8,
    'text-halo-blur': 0.4,
  };

  map.addLayer({
    id: 'place-labels-minor',
    type: 'symbol',
    source: 'places',
    minzoom: 6.5,
    filter: ['>=', ['get', 'r'], MAJOR_PLACE_RANK],
    layout: { ...common,
      'text-size': ['interpolate', ['linear'], ['zoom'], 6.5, 10.5, 10, 12.5, 14, 14] },
    paint: { ...paint, 'text-opacity': 0.85 },
  });

  map.addLayer({
    id: 'place-labels',
    type: 'symbol',
    source: 'places',
    filter: ['<', ['get', 'r'], MAJOR_PLACE_RANK],
    layout: { ...common,
      'text-size': ['interpolate', ['linear'], ['zoom'], 4, 11, 7, 14, 11, 17, 14, 19] },
    paint: paint,
  });
}
