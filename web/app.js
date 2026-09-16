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
  anchor: null,        // the value the diverging ramp is centred on
  scaleMode: 'value',  // 'value' = class by amount, 'rank' = class by quantile
  scaleUsed: 'rank',   // which of the two actually produced state.breaks
  basemap: false,
  selection: [],       // area codes, in click order
};

/* Metrics that are already a percentile rank, where classing by value and
 * classing by rank are the same operation. The scale control is hidden for
 * these rather than offered as a choice that does nothing. */
const RANKED_METRICS = new Set(['affluence_index']);

function scaleMode() {
  return RANKED_METRICS.has(state.metric) ? 'rank' : state.scaleMode;
}

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

function metricMeta(key) {
  return state.metrics.find((m) => m.key === key) ||
         state.components.find((c) => c.key === key) || null;
}

function formatValue(key, v) {
  const meta = metricMeta(key);
  return (fmt[meta && meta.format] || fmt.raw)(v);
}

/** Short form for legend ticks and table cells, where the full
 *  "£1,275,000" would wrap and push the columns apart. */
function compactValue(key, v) {
  if (!Number.isFinite(v)) return '--';
  const meta = metricMeta(key);
  switch (meta && meta.format) {
    case 'gbp':
      if (Math.abs(v) >= 1e6) return '£' + (v / 1e6).toFixed(Math.abs(v) >= 1e7 ? 0 : 1) + 'm';
      if (Math.abs(v) >= 1000) return '£' + Math.round(v / 1000) + 'k';
      return '£' + Math.round(v);
    case 'pct':
      return (Math.abs(v) >= 10 ? v.toFixed(0) : v.toFixed(1)) + '%';
    case 'count':
      return Math.round(v).toLocaleString('en-GB');
    default:
      return v.toFixed(1);
  }
}

/* ------------------------------ data ------------------------------ */

async function loadData() {
  setStatus('Loading boundaries…');
  const geojson = await fetch('data/gb_areas.json', {cache: 'no-store'}).then((r) => {
    if (!r.ok) throw new Error('boundaries: HTTP ' + r.status);
    return r.json();
  });
  setStatus('Loading metrics…');

  let metrics = null;
  try {
    const resp = await fetch('data/gb_metrics.json', {cache: 'no-store'});
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
    const resp = await fetch('data/gb_income.json', {cache: 'no-store'});
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
        { key: 'pct_100k_plus', label: 'Adults on £100k+', format: 'pct', income: true },
        { key: 'median_income', label: 'Median income (modelled)', format: 'gbp', income: true },
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

/** The population each area contributes to a national aggregate. Income
 *  metrics are per-adult, the census shares are per-resident. */
function weightsFor(key) {
  const meta = metricMeta(key);
  if (meta && meta.income && state.values.adults) return state.values.adults;
  const pop = state.attrs.find((a) => a.key === 'population');
  if (!pop) return null;
  if (!state.values.__pop) {
    state.values.__pop = Float64Array.from(pop.values, (v) => (v == null ? NaN : v));
  }
  return state.values.__pop;
}

/* The number the value scale diverges about: not the median area, but the
 * national figure a person would quote. For a share that is the aggregate --
 * weight each area's percentage by the people it describes and you get the
 * true GB rate, 2.7% of adults on £100k+ rather than the 1.8% of the middle
 * neighbourhood. For a level (a price, a modelled median) an average of
 * averages is meaningless, so the anchor is the population-weighted median:
 * the value in the middle *person's* area, not the middle area. */
function nationalAnchor(key, values) {
  const w = weightsFor(key);
  const meta = metricMeta(key);
  const isShare = meta && meta.format === 'pct';

  if (w && isShare) {
    let num = 0, den = 0;
    for (let i = 0; i < values.length; i++) {
      if (Number.isFinite(values[i]) && Number.isFinite(w[i])) {
        num += values[i] * w[i]; den += w[i];
      }
    }
    if (den > 0) return num / den;
  }

  if (w) {
    const rows = [];
    let total = 0;
    for (let i = 0; i < values.length; i++) {
      if (Number.isFinite(values[i]) && Number.isFinite(w[i]) && w[i] > 0) {
        rows.push([values[i], w[i]]); total += w[i];
      }
    }
    if (total > 0) {
      rows.sort((a, b) => a[0] - b[0]);
      let acc = 0;
      for (const [v, wt] of rows) {
        acc += wt;
        if (acc >= total / 2) return v;
      }
    }
  }
  return medianOf(values);
}

/** Percentile of the sorted finite values, 0-100. */
function percentileOf(sorted, q) {
  if (!sorted.length) return NaN;
  const pos = (q / 100) * (sorted.length - 1);
  const lo = Math.floor(pos), hi = Math.ceil(pos);
  return sorted[lo] + (sorted[hi] - sorted[lo]) * (pos - lo);
}

function sortedFinite(values) {
  const out = [];
  for (let i = 0; i < values.length; i++) {
    if (Number.isFinite(values[i])) out.push(values[i]);
  }
  out.sort((a, b) => a - b);
  return out;
}

/* Class boundaries spaced by *amount*, not by count.
 *
 * Quantile classes are what made the map misleading: they put half the country
 * above the midpoint by construction, so a metric where the typical area sits
 * at 1.8% still rendered as half deep blue. Here the middle class straddles the
 * national rate and each step out multiplies it, so the darkest blue is
 * genuinely exceptional rather than merely top-decile.
 *
 * Geometric rather than linear steps because every metric on this map is
 * right-skewed: linear steps over a range that runs to £4m or to 21% would put
 * nine areas in ten into the first class and flatten the whole picture. A ratio
 * scale says something true and legible -- each class is a fixed multiple
 * further from typical than the last.
 *
 * The outer breaks sit at the 0.5th and 99.5th percentiles rather than at the
 * extremes so that one £4m outlier cannot swallow the rest of the ramp. */
function valueBreaks(values, classes, anchor) {
  const sorted = sortedFinite(values);
  if (!sorted.length || !Number.isFinite(anchor)) return [];

  const perArm = classes >> 1;                    // 5 either side of the middle
  const lo = percentileOf(sorted, 0.5);
  const hi = percentileOf(sorted, 99.5);
  const breaks = [];

  const arm = (outer, up) => {
    const steps = [];
    const geometric = outer > 0 && anchor > 0 &&
                      (up ? outer > anchor : outer < anchor);
    for (let k = 1; k <= perArm; k++) {
      const f = k / perArm;
      steps.push(geometric
        ? anchor * Math.pow(outer / anchor, f)
        : anchor + (outer - anchor) * f);
    }
    return steps;
  };

  // Downward arm, printed low-to-high; the middle class is the gap between the
  // last downward step and the first upward one.
  breaks.push(...arm(lo, false).reverse());
  breaks.push(...arm(hi, true));
  // Degenerate data (every area identical) can collapse the steps; a
  // non-monotonic break list would silently mis-class, so bail to quantiles.
  for (let i = 1; i < breaks.length; i++) {
    if (!(breaks[i] > breaks[i - 1])) return [];
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

/* ---------------------------- selection ---------------------------- */

/* Areas accumulate: a click adds, a click on something already in the set
 * removes it. There is no modifier key to discover and it works the same on a
 * phone, at the cost of needing the sea (or Clear) to start over. */

function applySelectionFilter() {
  if (!map || !map.getLayer('areas-selected')) return;
  map.setFilter('areas-selected',
    ['in', ['get', 'area_code'], ['literal', state.selection]]);
}

function setSelection(codes) {
  state.selection = codes.filter((c) => state.index.has(c));
  applySelectionFilter();
  renderSelection();
}

function toggleArea(code) {
  const at = state.selection.indexOf(code);
  if (at >= 0) state.selection.splice(at, 1);
  else state.selection.push(code);
  applySelectionFilter();
  renderSelection();
}

function clearAreaSelection() {
  state.selection = [];
  applySelectionFilter();
  renderSelection();
}

function zoomToArea(code) {
  const box = state.bboxes && state.bboxes.get(code);
  if (box) {
    map.fitBounds([[box[0], box[1]], [box[2], box[3]]],
                  { padding: 120, maxZoom: 13, duration: 700 });
  }
  // Arriving from a ranking row or a search result is navigation, not
  // accumulation: it replaces whatever was selected rather than adding to it.
  setSelection([code]);
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

let lastClasses = null;

function paintMap() {
  const values = currentValues();
  const isCategorical = state.metric === 'nation';
  const classes = ramp().length;

  if (isCategorical) {
    state.breaks = [];
    state.median = null;
    state.anchor = null;
    state.scaleUsed = 'rank';
  } else {
    state.median = medianOf(values);
    state.anchor = scaleMode() === 'value'
      ? nationalAnchor(state.metric, values) : state.median;
    const breaks = scaleMode() === 'value'
      ? valueBreaks(values, classes, state.anchor) : [];
    // valueBreaks returns nothing when the distribution cannot carry a ratio
    // scale; quantiles always work, so they are the fallback rather than an
    // error.
    state.scaleUsed = breaks.length ? 'value' : 'rank';
    state.breaks = breaks.length ? breaks : quantileBreaks(values, classes);

    // Only push the areas whose class actually moved. Nudging one index weight
    // leaves most of the country where it was, and 43,064 setFeatureState calls
    // per frame is what made the sliders lag.
    const next = new Int8Array(state.codes.length);
    for (let i = 0; i < state.codes.length; i++) {
      const c = classOf(values[i], state.breaks);
      next[i] = c === null ? -1 : c;
    }
    for (let i = 0; i < next.length; i++) {
      if (lastClasses && lastClasses[i] === next[i]) continue;
      map.setFeatureState({ source: 'areas', id: state.codes[i] },
        { cls: next[i] < 0 ? null : next[i] });
    }
    lastClasses = next;
  }

  applyColours();
  renderLegend(values);
  renderRankings();
  if (typeof renderBrandCoverage === 'function') renderBrandCoverage();
}

/** The fill and its seam-covering line must always carry the same colour. */
function applyColours() {
  const expr = fillColourExpression();
  map.setPaintProperty('areas-fill', 'fill-color', expr);
  map.setPaintProperty('areas-seam', 'line-color', expr);
}

function addLayers(geojson) {
  map.addSource('areas', {
    type: 'geojson',
    data: geojson,
    promoteId: 'area_code',
    attribution: 'Contains OS data &copy; Crown copyright and database right 2026. Contains public sector information licensed under the Open Government Licence v3.0.',
  });

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
    toggleArea(feature.id);
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
    const phrase = rankPhrase(percentileRank(state.metric, v));
    valueLine = '<div class="t-val">' + label + ': <strong>' + shown + '</strong></div>' +
      (phrase ? '<div class="t-rank">' + phrase + '</div>' : '');
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
    state.sorted = {};
    paintMap();
    renderSelection();
  });
}

/* A slider is quick but imprecise, and a number field is precise but slow to
 * explore with; the pair costs one extra input and removes the need to choose.
 * Both write to the same value and each redraws the other. */
function numberField(id, value, min, max, step, unit) {
  return '<span class="value num-field">' +
    '<input type="number" class="num" id="' + id + '" min="' + min + '" max="' + max +
    '" step="' + step + '" value="' + value + '">' +
    (unit ? '<span class="unit">' + unit + '</span>' : '') + '</span>';
}

function clampTo(v, min, max, fallback) {
  if (!Number.isFinite(v)) return fallback;
  return Math.min(max, Math.max(min, v));
}

/* Repaints are not cheap -- reclassing 43,064 areas and re-sorting the index --
 * and a dragged slider fires input on every pixel. Coalesce to one repaint per
 * animation frame so the handle keeps up with the pointer. */
function rafThrottle(fn) {
  let queued = false;
  return () => {
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => { queued = false; fn(); });
  };
}

const repaintWeights = rafThrottle(() => {
  cachedIndex = null;
  if (state.sorted) delete state.sorted.affluence_index;
  paintMap();
  renderSelection();
});

function renderWeights() {
  const host = $('#weights');
  host.innerHTML = '';
  for (const c of state.components) {
    const wrap = document.createElement('div');
    wrap.className = 'weight';
    // Each component carries its own caveats; surface them on hover rather
    // than making the reader go and find the pipeline source.
    if (c.description) wrap.title = c.description;
    const pct = Math.round(c.weight * 100);
    wrap.innerHTML =
      '<div class="weight-head"><span class="label">' + c.label + '</span>' +
      numberField('w-' + c.key, pct, 0, 100, 1, '%') + '</div>' +
      '<input type="range" min="0" max="100" value="' + pct +
      '" data-key="' + c.key + '">';
    host.appendChild(wrap);
  }

  const apply = (key, pct) => {
    const comp = state.components.find((c) => c.key === key);
    if (!comp) return;
    comp.weight = pct / 100;
    repaintWeights();
  };

  host.oninput = (e) => {
    const input = e.target;
    if (input.type === 'range') {
      const key = input.dataset.key;
      const num = host.querySelector('#w-' + CSS.escape(key));
      if (num) num.value = input.value;
      apply(key, Number(input.value));
      return;
    }
    if (input.type === 'number') {
      // Mid-typing the field is briefly empty or out of range; ignore those
      // keystrokes rather than snapping the slider to 0 under the cursor.
      const raw = Number(input.value);
      if (input.value === '' || !Number.isFinite(raw) || raw < 0 || raw > 100) return;
      const key = input.id.slice(2);
      const slider = host.querySelector('[data-key="' + CSS.escape(key) + '"]');
      if (slider) slider.value = String(raw);
      apply(key, raw);
    }
  };

  // On blur, whatever is in the box has to become a legal value.
  host.onchange = (e) => {
    const input = e.target;
    if (input.type !== 'number') return;
    const key = input.id.slice(2);
    const comp = state.components.find((c) => c.key === key);
    const pct = clampTo(Math.round(Number(input.value)), 0, 100,
                        Math.round((comp ? comp.weight : 0) * 100));
    input.value = String(pct);
    const slider = host.querySelector('[data-key="' + CSS.escape(key) + '"]');
    if (slider) slider.value = String(pct);
    apply(key, pct);
  };

  $('#reset-weights').onclick = () => {
    for (const c of state.components) c.weight = c.default_weight;
    renderWeights();
    repaintWeights();
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
  const scale = ramp();
  const breaks = state.breaks;
  // What was actually used, not what was asked for: a distribution too
  // degenerate for a ratio scale falls back to quantiles, and the legend has to
  // describe the classes on the map rather than the ones that were intended.
  const byValue = state.scaleUsed === 'value';

  // Share of areas falling in each class, so the legend can say how much of the
  // country each colour actually covers. This is the whole point of the value
  // scale: under quantiles the answer was always "one eleventh".
  const counts = new Array(scale.length).fill(0);
  for (let i = 0; i < values.length; i++) {
    const c = classOf(values[i], breaks);
    if (c !== null) counts[c]++;
  }
  const share = (n) => finiteCount ? 100 * n / finiteCount : 0;

  const bandLabel = (k) => {
    const from = k === 0 ? null : breaks[k - 1];
    const to = k === scale.length - 1 ? null : breaks[k];
    const range = from == null ? 'up to ' + compactValue(state.metric, to)
                : to == null ? compactValue(state.metric, from) + ' and above'
                : compactValue(state.metric, from) + ' – ' + compactValue(state.metric, to);
    return range + '  ·  ' + share(counts[k]).toFixed(1) + '% of areas';
  };

  const swatches = '<div class="legend-scale">' + scale.map((c, k) =>
    '<span style="background:' + c + '"' +
    (breaks.length ? ' title="' + bandLabel(k) + '"' : '') + '></span>').join('') + '</div>';

  if (!byValue) {
    const perClass = Math.round(finiteCount / scale.length);
    // The affluence index is a percentile rank, so equal-count classes are not
    // a compromise there -- they are what the number already is. Every other
    // metric gets the warning, because that is the shape the map used to have.
    const note = RANKED_METRICS.has(state.metric)
      ? scale.length + ' equal-count classes. This measure is a percentile rank ' +
        'against the rest of GB rather than a quantity, so half the country sits ' +
        'above the midpoint by definition.'
      : scale.length + ' equal-count classes, about ' +
        perClass.toLocaleString('en-GB') + ' areas each, so exactly half the map ' +
        'is blue whatever the numbers are. Good for ranking, misleading about level.';
    host.innerHTML = scaleControl() + swatches +
      '<div class="legend-ends"><span>' + compactValue(state.metric, finiteCount ? lo : NaN) +
        '</span><span class="legend-mid">median ' + compactValue(state.metric, state.median) +
        '</span><span>' + compactValue(state.metric, finiteCount ? hi : NaN) + '</span></div>' +
      '<p class="legend-note">' + note +
        (missing ? ' ' + missing.toLocaleString('en-GB') + ' areas have no data.' : '') + '</p>';
    attachScaleControl(host);
    return;
  }

  let above = 0;
  for (let i = 0; i < values.length; i++) {
    if (Number.isFinite(values[i]) && values[i] > state.anchor) above++;
  }
  const meta = metricMeta(state.metric);
  const anchorWord = meta && meta.format === 'pct' ? 'GB rate' : 'typical';

  host.innerHTML = scaleControl() + swatches +
    '<div class="legend-ends"><span>' + compactValue(state.metric, breaks[0]) +
      '</span><span class="legend-mid">' + anchorWord + ' ' +
      compactValue(state.metric, state.anchor) +
      '</span><span>' + compactValue(state.metric, breaks[breaks.length - 1]) + '</span></div>' +
    '<p class="legend-note">Classes are value ranges, each a fixed multiple ' +
      'further from the national figure than the last. Grey spans ' +
      compactValue(state.metric, state.anchor) +
      (meta && meta.format === 'pct' ? ', the share across GB as a whole' : '') +
      '. Roughly ' + share(above).toFixed(0) + '% of areas sit above it, and ' +
      share(counts[counts.length - 1]).toFixed(1) + '% reach the darkest blue. ' +
      'Hover a swatch for its range.' +
      (missing ? ' ' + missing.toLocaleString('en-GB') + ' areas have no data.' : '') + '</p>';
  attachScaleControl(host);
}

/* Both scales are honest about different things, so this is a control rather
 * than a choice made once in the source: value answers "how much?", rank
 * answers "compared with everywhere else?". */
function scaleControl() {
  if (RANKED_METRICS.has(state.metric) || state.metric === 'nation') return '';
  const v = state.scaleMode === 'value';
  return '<div class="stats-mode scale-mode">' +
    '<button type="button" data-scale="value"' + (v ? ' class="on"' : '') +
      ' title="Classes are value ranges around the national figure">By value</button>' +
    '<button type="button" data-scale="rank"' + (v ? '' : ' class="on"') +
      ' title="Equal-count classes: each colour holds the same number of areas">By rank</button>' +
    '</div>';
}

function attachScaleControl(host) {
  const el = host.querySelector('.scale-mode');
  if (!el) return;
  el.onclick = (e) => {
    const btn = e.target.closest('button');
    if (!btn || btn.dataset.scale === state.scaleMode) return;
    state.scaleMode = btn.dataset.scale;
    paintMap();
  };
}

/* Where an area sits against the rest of GB. Colour alone cannot say this once
 * the classes are value ranges -- two areas can share a swatch and be twenty
 * percentile points apart -- so the number is printed. */
function sortedValuesFor(key) {
  if (!state.sorted) state.sorted = {};
  if (!state.sorted[key]) {
    state.sorted[key] = sortedFinite(
      key === 'affluence_index' ? indexValues() : (state.values[key] || []));
  }
  return state.sorted[key];
}

function percentileRank(key, v) {
  if (!Number.isFinite(v)) return NaN;
  const s = sortedValuesFor(key);
  if (!s.length) return NaN;
  let lo = 0, hi = s.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (s[mid] <= v) lo = mid + 1; else hi = mid;
  }
  return 100 * lo / s.length;
}

function rankPhrase(pct) {
  if (!Number.isFinite(pct)) return '';
  if (pct >= 50) return 'top ' + Math.max(0.1, 100 - pct).toFixed(pct > 99 ? 1 : 0) + '% of GB';
  return 'bottom ' + Math.max(0.1, pct).toFixed(pct < 1 ? 1 : 0) + '% of GB';
}

/** Metric value for one row, wherever it lives. */
function metricAt(key, i) {
  const v = key === 'affluence_index' ? indexValues()[i] : (state.values[key] || [])[i];
  return Number.isFinite(v) ? v : NaN;
}

/* Combining areas is not summing them. A share has to be re-weighted by the
 * people it describes, or two neighbourhoods of 900 and 9,000 would count
 * equally; a level is averaged the same way; only genuine counts add up. */
function combineMetric(key, rows) {
  const meta = metricMeta(key);
  if (meta && meta.format === 'count') {
    let s = 0, any = false;
    for (const i of rows) {
      const v = metricAt(key, i);
      if (Number.isFinite(v)) { s += v; any = true; }
    }
    return any ? s : NaN;
  }
  const w = weightsFor(key);
  let num = 0, den = 0;
  for (const i of rows) {
    const v = metricAt(key, i);
    const wt = w && Number.isFinite(w[i]) ? w[i] : 1;
    if (Number.isFinite(v) && wt > 0) { num += v * wt; den += wt; }
  }
  return den > 0 ? num / den : NaN;
}

function selectionRows() {
  return state.selection.map((c) => state.index.get(c)).filter((i) => i !== undefined);
}

function populationValues() {
  const found = state.attrs.find((a) => a.key === 'population');
  return found ? found.values : null;
}

function renderSelection() {
  const host = $('#selection');
  const codes = state.selection;

  if (!codes.length) {
    host.innerHTML = '<h2>Selected areas</h2>' +
      '<p class="empty">Click an area on the map. Click more to add them; ' +
      'click a selected area again to drop it.</p>';
    return;
  }

  const rows = selectionRows();
  const multi = codes.length > 1;
  const single = codes[0];
  const attrs = multi ? null : areaAttrs(single);

  const out = [];
  if (multi) {
    out.push(['Areas', codes.length.toLocaleString('en-GB')]);
    const popArr = populationValues();
    let popTotal = 0;
    if (popArr) for (const i of rows) if (popArr[i] != null) popTotal += popArr[i];
    if (popTotal) out.push(['Population', fmt.count(popTotal)]);
    if (state.values.adults) {
      let ad = 0;
      for (const i of rows) if (Number.isFinite(state.values.adults[i])) ad += state.values.adults[i];
      if (ad) out.push(['Adults 16+', fmt.count(ad)]);
    }
  } else {
    out.push(['Nation', (attrs && attrs.nation) || nationFromCode(single)]);
    if (attrs && attrs.population != null) out.push(['Population', fmt.count(attrs.population)]);
  }

  for (const m of state.metrics) {
    if (m.key === 'nation') continue;
    const v = multi ? combineMetric(m.key, rows) : metricAt(m.key, rows[0]);
    let cell = formatValue(m.key, Number.isFinite(v) ? v : null);
    if (m.key === state.metric && !multi) {
      const phrase = rankPhrase(percentileRank(m.key, v));
      if (phrase) cell += ' <span class="sel-rank">' + phrase + '</span>';
    }
    out.push([m.label, cell]);
  }

  const chips = multi
    ? '<div class="sel-chips">' + codes.map((c) => {
        const a = areaAttrs(c);
        const name = (a && a.area_name) || c;
        return '<button type="button" class="sel-chip" data-code="' + c + '" ' +
          'title="Remove ' + name + '">' + name + '<i>&times;</i></button>';
      }).join('') + '</div>'
    : '';

  host.innerHTML =
    '<h2>' + (multi ? 'Selected areas &mdash; ' + codes.length : 'Selected area') +
      '<button type="button" class="sel-clear" id="sel-clear">Clear</button></h2>' +
    (multi
      ? '<div class="sel-name">Combined</div>' + chips
      : '<div class="sel-name">' + ((attrs && attrs.area_name) || single) + '</div>' +
        '<div class="sel-code">' + single + '</div>') +
    '<dl class="sel-rows">' + out.map(([k, v]) =>
      '<div class="sel-row"><dt>' + k + '</dt><dd>' + v + '</dd></div>').join('') + '</dl>' +
    (multi ? '<p class="legend-note">Shares and levels are population-weighted ' +
      'across the selection; counts are summed.</p>' : '') +
    renderBands(rows);

  const clear = host.querySelector('#sel-clear');
  if (clear) clear.onclick = clearAreaSelection;
  const chipHost = host.querySelector('.sel-chips');
  if (chipHost) {
    chipHost.onclick = (e) => {
      const chip = e.target.closest('.sel-chip');
      if (chip) toggleArea(chip.dataset.code);
    };
  }
}

/** Modelled distribution of adults across income bands, as inline bars.
 *  Summed over however many areas are selected. */
function renderBands(rows) {
  if (!rows || !rows.length || !state.bandLabels) return '';
  const counts = state.bandLabels.map((_, b) => {
    const arr = state.values['band_' + b];
    if (!arr) return NaN;
    let s = 0;
    for (const i of rows) if (Number.isFinite(arr[i])) s += arr[i];
    return s;
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

/* On narrow screens the sidebar is a drawer over the map rather than a column
 * beside it. Wired before any data loads: it is pure DOM, and on a phone the
 * panel is the only route to the controls -- a button that does nothing for the
 * first few seconds reads as broken. */
function setSidebarOpen(open) {
  const sidebar = $('#sidebar');
  const btn = $('#toggle-sidebar');
  if (!sidebar) return;
  sidebar.classList.toggle('open', open);
  if (btn) btn.setAttribute('aria-expanded', String(open));
}

function initSidebarToggle() {
  const btn = $('#toggle-sidebar');
  const sidebar = $('#sidebar');
  if (!btn || !sidebar) return;
  btn.onclick = () => setSidebarOpen(!sidebar.classList.contains('open'));
}

/* The income figures are modelled, and a map that gets forwarded onward will
 * outrun any caveat kept in a README. Show it once, let people turn it off, and
 * keep it reachable from the toolbar afterwards. */
const INTRO_SEEN_KEY = 'uk-affluence-map:intro-dismissed';

function initIntro() {
  const intro = $('#intro');
  if (!intro) return;

  let dismissed = false;
  try {
    dismissed = localStorage.getItem(INTRO_SEEN_KEY) === '1';
  } catch (err) {
    // Private windows and blocked site data throw on access; showing the
    // intro is the safe failure here, not hiding it.
    dismissed = false;
  }
  intro.hidden = dismissed;

  const close = () => {
    const hide = $('#intro-hide');
    if (hide && hide.checked) {
      try { localStorage.setItem(INTRO_SEEN_KEY, '1'); } catch (err) { /* ignore */ }
    }
    intro.hidden = true;
  };

  $('#intro-go').onclick = close;
  intro.onclick = (e) => { if (e.target === intro) close(); };
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !intro.hidden) close();
  });

  const about = $('#toggle-about');
  if (about) about.onclick = () => { intro.hidden = false; };
}

function attachToolbar() {
  $('#toggle-theme').onclick = () => {
    document.documentElement.setAttribute('data-theme', isDarkMode() ? 'light' : 'dark');
    map.setPaintProperty('bg', 'background-color',
      getComputedStyle(document.documentElement).getPropertyValue('--surface-1').trim());
    applyColours();
    if (typeof applyPlaceLabelColours === 'function') applyPlaceLabelColours();
    if (typeof applyRoadColours === 'function') applyRoadColours();
  };

  // Tapping the map is the natural "done with the panel" gesture. This half
  // needs the map; the button itself does not, and is wired far earlier.
  map.on('click', () => setSidebarOpen(false));

  const roadsBtn = $('#toggle-roads');
  if (roadsBtn) {
    roadsBtn.onclick = () => {
      if (typeof setRoadsVisible !== 'function') return;
      setRoadsVisible(!roadState.visible);
      roadsBtn.classList.toggle('off', !roadState.visible);
    };
  }

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
  // Before any fetch: the intro needs no data, and showing it immediately means
  // it is read while the map loads rather than interrupting someone who is
  // already looking at the result.
  initIntro();
  initSidebarToggle();

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
    renderSelection();
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
      const roads = (typeof initRoads === 'function')
        ? initRoads() : Promise.resolve();
      const places = roads.then(() =>
        (typeof initPlaces === 'function') ? initPlaces() : undefined);
      places.then(() => {
        if (typeof initBranches === 'function') initBranches();
        if (typeof initSearch === 'function') initSearch();
      });
    });
  });

  map.on('error', (e) => console.error('map error', e && e.error));
}

main();
