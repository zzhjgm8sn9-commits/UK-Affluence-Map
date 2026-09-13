'use strict';

/* ------------------------------------------------------------------ *
 * UK Affluence Map
 *
 * Geometry (GeoJSON) and attributes (columnar JSON) are loaded and held
 * separately: 43,064 polygons are expensive to parse and almost never change,
 * while metrics are rebuilt often. Attribute values reach the map through
 * feature-state, so re-weighting the index repaints without touching geometry.
 * ------------------------------------------------------------------ */

const GB_BOUNDS = [[-8.65, 49.85], [1.80, 60.90]];

/* Diverging red <-> blue, 11 steps, neutral grey midpoint.
 *
 * Diverging rather than sequential because every metric here is shown in
 * quantile classes, so the middle class always straddles the national median.
 * That gives the ramp a real baseline to diverge about: grey means "typical for
 * GB", and the two arms mean below and above it.
 *
 * The red arm was generated in OKLab to mirror the blue arm's lightness step
 * for step, so the ramp is symmetric about its midpoint and neither pole reads
 * as heavier than the other. Blue <-> red is the documented diverging pair:
 * warm and cool read as opposite, and unlike red/green it survives the common
 * forms of colour blindness, which separate them on lightness and on the
 * blue-yellow channel.
 *
 * Dark mode inverts the lightness profile -- poles bright, midpoint dark -- so
 * the neutral still recedes into the surface rather than glowing against it.
 */
const RAMP_LIGHT = ['#621b18', '#9e342e', '#c74941', '#e4857b', '#f4c3bc',
                    '#f0efec',
                    '#b7d3f6', '#6da7ec', '#2a78d6', '#1c5cab', '#0d366b'];

const RAMP_DARK = ['#f5bab2', '#e88479', '#d04f47', '#a83630', '#7d2b26',
                   '#383835',
                   '#164a87', '#1762b6', '#2e80e0', '#6da7f1', '#accdf8'];

function ramp() { return isDarkMode() ? RAMP_DARK : RAMP_LIGHT; }

const NATION_COLOURS = { England: '#2a78d6', Wales: '#eb6834', Scotland: '#1baf7a' };

const NO_DATA_LIGHT = '#e3e2df';
const NO_DATA_DARK = '#333331';

const state = {
  codes: [],
  index: new Map(),     // area_code -> row position
  values: {},           // field key -> Float64Array
  components: [],       // index components, each with a weight
  metrics: [],          // everything selectable in the "Colour by" dropdown
  attrs: [],            // per-area descriptive attributes (name, nation, ...)
  bandLabels: null,     // modelled income band labels, if the income file loaded
  bboxes: null,         // area_code -> [w, s, e, n], for zooming to a result
  metric: 'nation',
  breaks: [],
  median: null,
  basemap: false,
  selected: null,
};

const $ = (sel) => document.querySelector(sel);
const statusEl = $('#status');
const tooltipEl = $('#tooltip');

function setStatus(msg) {
  if (msg === null) { statusEl.classList.add('hidden'); return; }
  statusEl.classList.remove('hidden');
  statusEl.textContent = msg;
}

function isDarkMode() {
  const explicit = document.documentElement.getAttribute('data-theme');
  if (explicit) return explicit === 'dark';
  return window.matchMedia('(prefers-color-scheme: dark)').matches;
}

/* ---------------------------- formatting ---------------------------- */

const fmt = {
  gbp: (v) => v == null ? '--' : '£' + Math.round(v).toLocaleString('en-GB'),
  pct: (v) => v == null ? '--' : v.toFixed(1) + '%',
  index: (v) => v == null ? '--' : v.toFixed(1),
  count: (v) => v == null ? '--' : Math.round(v).toLocaleString('en-GB'),
  raw: (v) => v == null ? '--' : String(v),
};

function formatValue(key, v) {
  const meta = state.metrics.find((m) => m.key === key) ||
               state.components.find((c) => c.key === key);
  return (fmt[meta && meta.format] || fmt.raw)(v);
}

/* ------------------------------ data ------------------------------ */

async function loadData() {
  setStatus('Loading boundaries…');
  const geojson = await fetch('data/gb_areas.geojson').then((r) => {
    if (!r.ok) throw new Error('boundaries: HTTP ' + r.status);
    return r.json();
  });
  setStatus('Loading metrics…');

  let metrics = null;
  try {
    const resp = await fetch('data/gb_metrics.json');
    if (resp.ok) metrics = await resp.json();
  } catch (err) {
    console.warn('no metrics file yet', err);
  }

  if (metrics) {
    state.codes = metrics.codes;
    state.codes.forEach((c, i) => state.index.set(c, i));
    for (const [key, arr] of Object.entries(metrics.values || {})) {
      state.values[key] = Float64Array.from(arr, (v) => (v == null ? NaN : v));
    }
    state.components = metrics.components || [];
    state.metrics = metrics.metrics || [];
    state.attrs = metrics.attrs || [];

    // Catchment maths needs the centroids as plain typed arrays; lift them out
    // of the attribute list once rather than searching it in a hot loop.
    for (const [attrKey, alias] of [['centroid_lat', '__lat'], ['centroid_lon', '__lon']]) {
      const found = state.attrs.find((a) => a.key === attrKey);
      if (found) {
        state.values[alias] = Float64Array.from(
          found.values, (v) => (v == null ? NaN : v));
      }
    }
  }

  // Modelled income bands live in their own file: they are rebuilt on a
  // different cadence from the index, and are joined by area code rather than
  // by position so neither file has to trust the other's row order.
  try {
    const resp = await fetch('data/gb_income.json');
    if (resp.ok) {
      const income = await resp.json();
      const n = state.codes.length;
      const pos = new Map(income.codes.map((c, i) => [c, i]));
      for (const [key, arr] of Object.entries(income.values)) {
        const aligned = new Float64Array(n).fill(NaN);
        for (let i = 0; i < n; i++) {
          const j = pos.get(state.codes[i]);
          if (j !== undefined && arr[j] != null) aligned[i] = arr[j];
        }
        state.values[key] = aligned;
      }
      state.bandLabels = income.band_labels;
      state.metrics.unshift(
        { key: 'pct_100k_plus', label: 'Adults on £100k+', format: 'pct' },
        { key: 'median_income', label: 'Median income (modelled)', format: 'gbp' },
      );
    }
  } catch (err) {
    console.warn('no income file yet', err);
  }

  // The nation view always works, with or without metrics -- it is the proof
  // that geometry loaded, and a useful sanity check on the three-way join.
  state.metrics.unshift({ key: 'nation', label: 'Nation', format: 'raw', categorical: true });
  // Lead with the modelled income share -- it is the question the map is
  // actually built to answer. The index remains one selection away.
  if (state.values.pct_100k_plus) state.metric = 'pct_100k_plus';
  else if (metrics) state.metric = 'affluence_index';

  return geojson;
}

/* --------------------------- classification --------------------------- */

/** Quantile breaks. Choropleths of skewed data (prices, incomes) are
 *  unreadable on equal intervals -- everything lands in the bottom class. */
function quantileBreaks(values, classes) {
  const finite = [];
  for (let i = 0; i < values.length; i++) {
    if (Number.isFinite(values[i])) finite.push(values[i]);
  }
  finite.sort((a, b) => a - b);
  if (!finite.length) return [];
  const breaks = [];
  for (let i = 1; i < classes; i++) {
    breaks.push(finite[Math.floor((i / classes) * finite.length)]);
  }
  return breaks;
}

/** Median of the finite values, for labelling the diverging ramp's midpoint. */
function medianOf(values) {
  const finite = [];
  for (let i = 0; i < values.length; i++) {
    if (Number.isFinite(values[i])) finite.push(values[i]);
  }
  if (!finite.length) return null;
  finite.sort((a, b) => a - b);
  const mid = finite.length >> 1;
  return finite.length % 2 ? finite[mid] : (finite[mid - 1] + finite[mid]) / 2;
}

function classOf(value, breaks) {
  if (!Number.isFinite(value)) return null;
  let c = 0;
  while (c < breaks.length && value >= breaks[c]) c++;
  return c;
}

/** Weighted sum of component z-scores, rescaled to a 0-100 percentile rank. */
function computeIndex() {
  const active = state.components.filter((c) => c.weight > 0 && state.values['z_' + c.key]);
  const n = state.codes.length;
  const out = new Float64Array(n).fill(NaN);
  if (!active.length) return out;

  const total = active.reduce((s, c) => s + c.weight, 0);
  for (let i = 0; i < n; i++) {
    let sum = 0, used = 0;
    for (const c of active) {
      const z = state.values['z_' + c.key][i];
      if (Number.isFinite(z)) { sum += z * c.weight; used += c.weight; }
    }
    // Require at least half the weight present before scoring an area.
    out[i] = used >= 0.5 * total ? sum / used : NaN;
  }

  // Percentile rank so the legend is interpretable regardless of weights.
  const order = [];
  for (let i = 0; i < n; i++) if (Number.isFinite(out[i])) order.push(i);
  order.sort((a, b) => out[a] - out[b]);
  const ranked = new Float64Array(n).fill(NaN);
  const denom = (order.length - 1) || 1;
  order.forEach((idx, rank) => { ranked[idx] = 100 * rank / denom; });
  return ranked;
}

let cachedIndex = null;

/** The affluence index, recomputed only when the weights change. */
function indexValues() {
  if (!cachedIndex) cachedIndex = computeIndex();
  return cachedIndex;
}

function currentValues() {
  if (state.metric === 'affluence_index') return indexValues();
  return state.values[state.metric] || new Float64Array(0);
}

/* ---------------------------- extents ---------------------------- */

/** One pass over the geometry to record each area's bounding box.
 *  Cheaper than asking MapLibre to hunt for a feature later, and it means the
 *  rankings can zoom to an area that is not currently rendered. */
function indexBoundingBoxes(geojson) {
  const boxes = new Map();
  for (const f of geojson.features) {
    const g = f.geometry;
    if (!g) continue;
    let w = Infinity, s = Infinity, e = -Infinity, n = -Infinity;
    const rings = g.type === 'Polygon' ? g.coordinates
                : g.type === 'MultiPolygon' ? g.coordinates.flat()
                : [];
    for (const ring of rings) {
      for (const [x, y] of ring) {
        if (x < w) w = x;
        if (x > e) e = x;
        if (y < s) s = y;
        if (y > n) n = y;
      }
    }
    if (w <= e) boxes.set(f.properties.area_code, [w, s, e, n]);
  }
  return boxes;
}

function zoomToArea(code) {
  const box = state.bboxes && state.bboxes.get(code);
  if (!box) return;
  map.fitBounds([[box[0], box[1]], [box[2], box[3]]],
                { padding: 120, maxZoom: 13, duration: 700 });
  state.selected = code;
  map.setFilter('areas-selected', ['==', ['get', 'area_code'], code]);
  renderSelection(code);
}

/* ------------------------------- map ------------------------------- */

let map;

/** Run `cb` once `sourceId` has finished loading its data. */
function whenSourceReady(sourceId, cb) {
  if (map.isSourceLoaded(sourceId)) { cb(); return; }
  const handler = (e) => {
    if (e.sourceId === sourceId && map.isSourceLoaded(sourceId)) {
      map.off('sourcedata', handler);
      cb();
    }
  };
  map.on('sourcedata', handler);
}

function baseStyle() {
  const surface = getComputedStyle(document.documentElement)
    .getPropertyValue('--surface-1').trim() || '#fcfcfb';
  return {
    version: 8,
    // Vendored locally by build_places.py -- MapLibre cannot draw a character
    // without this, and pointing it at a public font server would add a
    // runtime dependency the rest of the app does not have.
    glyphs: 'fonts/{fontstack}/{range}.pbf',
    sources: {},
    layers: [{ id: 'bg', type: 'background', paint: { 'background-color': surface } }],
  };
}

function fillColourExpression() {
  const noData = isDarkMode() ? NO_DATA_DARK : NO_DATA_LIGHT;

  if (state.metric === 'nation') {
    return ['match', ['feature-state', 'nation'],
      'England', NATION_COLOURS.England,
      'Wales', NATION_COLOURS.Wales,
      'Scotland', NATION_COLOURS.Scotland,
      noData];
  }

  const scale = ramp();
  const steps = ['step', ['feature-state', 'cls'], scale[0]];
  for (let i = 1; i < scale.length; i++) steps.push(i, scale[i]);
  return ['case', ['==', ['feature-state', 'cls'], null], noData, steps];
}

function paintMap() {
  cachedIndex = null;
  const values = currentValues();
  const isCategorical = state.metric === 'nation';
  state.breaks = isCategorical ? [] : quantileBreaks(values, ramp().length);
  state.median = isCategorical ? null : medianOf(values);

  if (!isCategorical) {
    for (let i = 0; i < state.codes.length; i++) {
      map.setFeatureState({ source: 'areas', id: state.codes[i] },
        { cls: classOf(values[i], state.breaks) });
    }
  }

  applyColours();
  renderLegend(values);
  renderRankings();
}

/** The fill and its seam-covering line must always carry the same colour. */
function applyColours() {
  const expr = fillColourExpression();
  map.setPaintProperty('areas-fill', 'fill-color', expr);
  map.setPaintProperty('areas-seam', 'line-color', expr);
}

function addLayers(geojson) {
  map.addSource('areas', { type: 'geojson', data: geojson, promoteId: 'area_code' });

  map.addLayer({
    id: 'areas-fill',
    type: 'fill',
    source: 'areas',
    paint: {
      'fill-color': fillColourExpression(),
      'fill-opacity': ['case', ['boolean', ['feature-state', 'hover'], false], 1, 0.88],
    },
  });

  // Anti-aliasing leaves a hairline seam along every shared edge. With 43,064
  // polygons that reads as white speckle across dense urban areas at national
  // zoom, where each LSOA is only a few pixels wide. A 1px line in each area's
  // own fill colour closes the seams. It is faded out by zoom 9, where the
  // polygons are large enough for the seams to be invisible anyway and the
  // white boundary lines take over.
  map.addLayer({
    id: 'areas-seam',
    type: 'line',
    source: 'areas',
    paint: {
      'line-color': fillColourExpression(),
      'line-width': ['interpolate', ['linear'], ['zoom'], 7.5, 1, 9, 0],
    },
  });

  // Internal boundaries only resolve as meaningful above ~zoom 9; below that
  // they turn the map into a grey mesh.
  map.addLayer({
    id: 'areas-line',
    type: 'line',
    source: 'areas',
    paint: {
      'line-color': '#ffffff',
      'line-width': ['interpolate', ['linear'], ['zoom'], 8, 0, 11, 0.4, 14, 0.8],
      'line-opacity': 0.5,
    },
  });

  map.addLayer({
    id: 'areas-selected',
    type: 'line',
    source: 'areas',
    paint: { 'line-color': '#0b0b0b', 'line-width': 2 },
    filter: ['==', ['get', 'area_code'], '__none__'],
  });
}

/* --------------------------- interactions --------------------------- */

let hoveredCode = null;

function attachInteractions() {
  map.on('mousemove', 'areas-fill', (e) => {
    const feature = e.features && e.features[0];
    if (!feature) return;
    const code = feature.id;

    if (hoveredCode !== code) {
      if (hoveredCode) map.setFeatureState({ source: 'areas', id: hoveredCode }, { hover: false });
      hoveredCode = code;
      map.setFeatureState({ source: 'areas', id: code }, { hover: true });
    }
    showTooltip(e, code);
  });

  map.on('mouseleave', 'areas-fill', () => {
    if (hoveredCode) map.setFeatureState({ source: 'areas', id: hoveredCode }, { hover: false });
    hoveredCode = null;
    tooltipEl.style.display = 'none';
    map.getCanvas().style.cursor = '';
  });

  map.on('mouseenter', 'areas-fill', () => { map.getCanvas().style.cursor = 'pointer'; });

  map.on('click', 'areas-fill', (e) => {
    const feature = e.features && e.features[0];
    if (!feature) return;
    state.selected = feature.id;
    map.setFilter('areas-selected', ['==', ['get', 'area_code'], feature.id]);
    renderSelection(feature.id);
  });
}

function areaAttrs(code) {
  const i = state.index.get(code);
  if (i === undefined) return null;
  const row = { area_code: code };
  for (const a of state.attrs) row[a.key] = a.values[i];
  return row;
}

function nationFromCode(code) {
  if (code[0] === 'S') return 'Scotland';
  if (code[0] === 'W') return 'Wales';
  return 'England';
}

function showTooltip(e, code) {
  const i = state.index.get(code);
  const attrs = areaAttrs(code);
  const name = (attrs && attrs.area_name) || code;
  const nation = (attrs && attrs.nation) || nationFromCode(code);

  let valueLine = '';
  if (state.metric !== 'nation' && i !== undefined) {
    const v = currentValues()[i];
    const meta = state.metrics.find((m) => m.key === state.metric);
    const label = meta ? meta.label : state.metric;
    const shown = formatValue(state.metric, Number.isFinite(v) ? v : null);
    valueLine = '<div class="t-val">' + label + ': <strong>' + shown + '</strong></div>';
  }

  tooltipEl.innerHTML =
    '<div class="t-name">' + name + '</div>' +
    '<div class="t-meta">' + code + ' · ' + nation + '</div>' +
    valueLine;
  tooltipEl.style.display = 'block';

  const pad = 14;
  const rect = tooltipEl.getBoundingClientRect();
  let x = e.point.x + pad;
  let y = e.point.y + pad;
  if (x + rect.width > map.getCanvas().clientWidth) x = e.point.x - rect.width - pad;
  if (y + rect.height > map.getCanvas().clientHeight) y = e.point.y - rect.height - pad;
  tooltipEl.style.left = x + 'px';
  tooltipEl.style.top = y + 'px';
}

/* ------------------------------- UI ------------------------------- */

function renderMetricSelect() {
  const sel = $('#metric');
  sel.innerHTML = '';
  for (const m of state.metrics) {
    const opt = document.createElement('option');
    opt.value = m.key;
    opt.textContent = m.label;
    sel.appendChild(opt);
  }
  sel.value = state.metric;
  sel.addEventListener('change', () => {
    state.metric = sel.value;
    $('#weights-panel').hidden = state.metric !== 'affluence_index';
    paintMap();
    if (state.selected) renderSelection(state.selected);
  });
}

function renderWeights() {
  const host = $('#weights');
  host.innerHTML = '';
  for (const c of state.components) {
    const wrap = document.createElement('div');
    wrap.className = 'weight';
    // Each component carries its own caveats; surface them on hover rather
    // than making the reader go and find the pipeline source.
    if (c.description) wrap.title = c.description;
    wrap.innerHTML =
      '<div class="weight-head"><span class="label">' + c.label + '</span>' +
      '<span class="value" data-for="' + c.key + '">' + Math.round(c.weight * 100) + '%</span></div>' +
      '<input type="range" min="0" max="100" value="' + Math.round(c.weight * 100) +
      '" data-key="' + c.key + '">';
    host.appendChild(wrap);
  }

  host.oninput = (e) => {
    const input = e.target;
    if (input.type !== 'range') return;
    const comp = state.components.find((c) => c.key === input.dataset.key);
    comp.weight = Number(input.value) / 100;
    host.querySelector('[data-for="' + comp.key + '"]').textContent = input.value + '%';
    paintMap();
    if (state.selected) renderSelection(state.selected);
  };

  $('#reset-weights').onclick = () => {
    for (const c of state.components) c.weight = c.default_weight;
    renderWeights();
    paintMap();
  };
}

function renderLegend(values) {
  const host = $('#legend');
  if (state.metric === 'nation') {
    host.innerHTML = '<div class="legend-cats">' +
      Object.entries(NATION_COLOURS).map(([n, c]) =>
        '<div class="legend-cat"><i style="background:' + c + '"></i>' + n + '</div>').join('') +
      '</div>';
    return;
  }

  let lo = Infinity, hi = -Infinity, finiteCount = 0;
  for (let i = 0; i < values.length; i++) {
    const v = values[i];
    if (!Number.isFinite(v)) continue;
    finiteCount++;
    if (v < lo) lo = v;
    if (v > hi) hi = v;
  }
  const missing = state.codes.length - finiteCount;
  const perClass = Math.round(finiteCount / ramp().length);

  const scale = ramp();
  const mid = state.median;

  host.innerHTML =
    '<div class="legend-scale">' +
      scale.map((c) => '<span style="background:' + c + '"></span>').join('') + '</div>' +
    '<div class="legend-ends"><span>' + formatValue(state.metric, finiteCount ? lo : null) +
      '</span><span class="legend-mid">median ' + formatValue(state.metric, mid) +
      '</span><span>' + formatValue(state.metric, finiteCount ? hi : null) + '</span></div>' +
    '<p class="legend-note">' + scale.length + ' quantile classes, about ' +
      perClass.toLocaleString('en-GB') + ' areas each. Grey marks the GB median; ' +
      'red is below it and blue above.' +
      (missing ? ' ' + missing.toLocaleString('en-GB') + ' areas have no data.' : '') + '</p>';
}

function renderSelection(code) {
  const host = $('#selection');
  const attrs = areaAttrs(code);
  const i = state.index.get(code);

  const rows = [];
  rows.push(['Nation', (attrs && attrs.nation) || nationFromCode(code)]);
  if (attrs && attrs.population != null) rows.push(['Population', fmt.count(attrs.population)]);

  if (i !== undefined) {
    for (const m of state.metrics) {
      if (m.key === 'nation') continue;
      const v = m.key === 'affluence_index'
        ? indexValues()[i]
        : (state.values[m.key] || [])[i];
      rows.push([m.label, formatValue(m.key, Number.isFinite(v) ? v : null)]);
    }
  }

  host.innerHTML =
    '<h2>Selected area</h2>' +
    '<div class="sel-name">' + ((attrs && attrs.area_name) || code) + '</div>' +
    '<div class="sel-code">' + code + '</div>' +
    '<dl class="sel-rows">' + rows.map(([k, v]) =>
      '<div class="sel-row"><dt>' + k + '</dt><dd>' + v + '</dd></div>').join('') + '</dl>' +
    renderBands(i);
}

/** Modelled distribution of adults across income bands, as inline bars. */
function renderBands(i) {
  if (i === undefined || !state.bandLabels) return '';
  const counts = state.bandLabels.map((_, b) => {
    const arr = state.values['band_' + b];
    return arr ? arr[i] : NaN;
  });
  const total = counts.reduce((s, v) => s + (Number.isFinite(v) ? v : 0), 0);
  if (!total) return '';

  // Bars are scaled to the largest band, not to the total: most areas have
  // most adults in one or two bands, and scaling to the total would flatten
  // every upper band to invisibility.
  const peak = Math.max(...counts.filter(Number.isFinite));

  return '<div class="bands"><h2>Modelled income bands</h2>' +
    state.bandLabels.map((label, b) => {
      const v = counts[b];
      const pct = Number.isFinite(v) ? 100 * v / total : 0;
      const width = Number.isFinite(v) && peak > 0 ? 100 * v / peak : 0;
      return '<div class="band-row">' +
        '<span class="band-label">' + label + '</span>' +
        '<span class="band-bar"><i style="width:' + width.toFixed(1) + '%"></i></span>' +
        '<span class="band-pct">' + pct.toFixed(1) + '%</span>' +
        '<span class="band-n">' + fmt.count(v) + '</span>' +
        '</div>';
    }).join('') +
    '<p class="legend-note">Modelled estimate over adults aged 16+, not a ' +
    'measurement. See README for method and limitations.</p></div>';
}

/** Best and worst ten areas on the metric currently displayed. */
function renderRankings() {
  const host = $('#rankings');
  if (!host) return;

  if (state.metric === 'nation') {
    host.innerHTML = '<h2>Rankings</h2>' +
      '<p class="empty">Pick a numeric metric to rank areas.</p>';
    return;
  }

  const values = currentValues();
  const rows = [];
  for (let i = 0; i < state.codes.length; i++) {
    if (Number.isFinite(values[i])) rows.push([i, values[i]]);
  }
  if (!rows.length) { host.innerHTML = ''; return; }

  rows.sort((a, b) => b[1] - a[1]);
  const top = rows.slice(0, 10);
  const bottom = rows.slice(-10).reverse();
  const meta = state.metrics.find((m) => m.key === state.metric);
  const label = meta ? meta.label : state.metric;

  // Both lists count 1..10 from their own end. Printing the absolute rank on
  // the bottom list (43,064 downwards) is wider than the column and crowds out
  // the area names, and the heading already says which end you are looking at.
  const list = (items) => '<ol class="rank-list">' + items.map((row, k) => {
    const i = row[0];
    const code = state.codes[i];
    const attrs = areaAttrs(code);
    const name = (attrs && attrs.area_name) || code;
    const rank = k + 1;
    return '<li class="rank-row" data-code="' + code + '" title="' + name + '">' +
      '<span class="rank-n">' + rank + '</span>' +
      '<span class="rank-name">' + name + '</span>' +
      '<span class="rank-val">' + formatValue(state.metric, row[1]) + '</span>' +
      '</li>';
  }).join('') + '</ol>';

  host.innerHTML =
    '<h2>Highest &amp; lowest &mdash; ' + label + '</h2>' +
    '<h3 class="rank-head">Top 10</h3>' + list(top) +
    '<h3 class="rank-head">Bottom 10</h3>' + list(bottom) +
    '<p class="legend-note">Click any row to zoom the map to it.</p>';

  host.onclick = (e) => {
    const row = e.target.closest('.rank-row');
    if (row) zoomToArea(row.dataset.code);
  };
}

function attachToolbar() {
  $('#toggle-theme').onclick = () => {
    document.documentElement.setAttribute('data-theme', isDarkMode() ? 'light' : 'dark');
    map.setPaintProperty('bg', 'background-color',
      getComputedStyle(document.documentElement).getPropertyValue('--surface-1').trim());
    applyColours();
    if (typeof applyPlaceLabelColours === 'function') applyPlaceLabelColours();
  };

  $('#toggle-base').onclick = () => {
    state.basemap = !state.basemap;
    if (state.basemap) {
      map.addSource('carto', {
        type: 'raster',
        tiles: ['https://a.basemaps.cartocdn.com/light_all/{z}/{x}/{y}@2x.png',
                'https://b.basemaps.cartocdn.com/light_all/{z}/{x}/{y}@2x.png'],
        tileSize: 256,
        attribution: '&copy; OpenStreetMap contributors &copy; CARTO',
      });
      map.addLayer({ id: 'carto', type: 'raster', source: 'carto' }, 'areas-fill');
      map.setPaintProperty('areas-fill', 'fill-opacity', 0.72);
    } else {
      if (map.getLayer('carto')) map.removeLayer('carto');
      if (map.getSource('carto')) map.removeSource('carto');
      map.setPaintProperty('areas-fill', 'fill-opacity',
        ['case', ['boolean', ['feature-state', 'hover'], false], 1, 0.88]);
    }
  };
}

/* ------------------------------ boot ------------------------------ */

async function main() {
  let geojson;
  try {
    geojson = await loadData();
  } catch (err) {
    setStatus('Could not load data: ' + err.message +
      ' — run the pipeline, then serve this folder over HTTP.');
    console.error(err);
    return;
  }

  // Without a metrics file the attribute table is empty, so seed it from the
  // geometry: enough for the nation view and for click/hover to work.
  if (!state.codes.length) {
    state.codes = geojson.features.map((f) => f.properties.area_code);
    state.codes.forEach((c, i) => state.index.set(c, i));
  }

  setStatus('Rendering ' + state.codes.length.toLocaleString('en-GB') + ' areas…');

  map = new maplibregl.Map({
    container: 'map',
    style: baseStyle(),
    bounds: GB_BOUNDS,
    fitBoundsOptions: { padding: 24 },
    maxZoom: 15,
    attributionControl: { compact: true },
  });
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-left');
  map.addControl(new maplibregl.ScaleControl({ unit: 'metric' }), 'bottom-left');

  // app.js is a classic script, so `map` is script-scoped and invisible to the
  // console. Expose it deliberately for debugging.
  window.__affluence = { map, state };

  state.bboxes = indexBoundingBoxes(geojson);

  map.on('load', () => {
    addLayers(geojson);

    // The constructor's `bounds` is evaluated before the container has been
    // laid out, which leaves the map at an arbitrary zoom. Re-fit once the
    // real dimensions are known.
    map.fitBounds(GB_BOUNDS, { padding: 24, animate: false });

    renderMetricSelect();
    if (state.components.length) renderWeights();
    $('#weights-panel').hidden = state.metric !== 'affluence_index';
    attachInteractions();
    attachToolbar();

    // 'load' fires when the style is ready, but a 21 MB GeoJSON source is still
    // being parsed in a worker at that point, and feature state set against an
    // unloaded source is silently discarded. Wait for the source itself.
    whenSourceReady('areas', () => {
      for (const f of geojson.features) {
        const code = f.properties.area_code;
        map.setFeatureState({ source: 'areas', id: code }, { nation: nationFromCode(code) });
      }
      paintMap();
      setStatus(null);
      const places = (typeof initPlaces === 'function')
        ? initPlaces() : Promise.resolve();
      places.then(() => {
        if (typeof initBranches === 'function') initBranches();
        if (typeof initSearch === 'function') initSearch();
      });
    });
  });

  map.on('error', (e) => console.error('map error', e && e.error));
}

main();
