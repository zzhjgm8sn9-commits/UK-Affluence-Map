'use strict';

/* ------------------------------------------------------------------ *
 * Major roads, for orientation.
 *
 * Furniture, not data. Roads exist here so that a zoomed-in choropleth can be
 * navigated by someone who does not already know the area, and the styling is
 * chosen to stay out of the way of the colours underneath: a thin ink line over
 * a surface-coloured casing, which keeps the road legible over dark blue and
 * pale red alike without either competing with the fill or disappearing into it.
 *
 * Only classified roads are in the data (motorway, A, B), and they appear by
 * class as you zoom: the strategic network first, B roads only once the view is
 * local enough for them to mean something.
 * ------------------------------------------------------------------ */

const ROAD_MINZOOM = 8.5;        // motorways and A roads
const ROAD_B_MINZOOM = 11;       // B roads
const ROAD_LABEL_MINZOOM = 11.5;

const roadState = { visible: true, loaded: false };

function roadColours() {
  const css = getComputedStyle(document.documentElement);
  return {
    ink: isDarkMode() ? '#c9c7bd' : '#4a4945',
    casing: css.getPropertyValue('--surface-1').trim() || '#fcfcfb',
  };
}

/* Width by zoom.
 *
 * MapLibre requires a `zoom` expression to be the direct input of a top-level
 * `interpolate` or `step`. Multiplying an interpolate by a per-class factor --
 * the obvious way to make motorways wider than A roads -- is rejected outright,
 * and the layer is silently dropped from the style. So the class factor goes
 * inside each stop instead, where no zoom expression is involved. */
const MOTORWAY_VS_A = ['case', ['==', ['get', 'road_class'], 'Motorway'], 1.5, 1.0];

// Empty strings, not nulls, come out of the pipeline, so `coalesce` will not
// skip them -- the length has to be tested explicitly.
const HAS_NUMBER = ['>', ['length', ['coalesce', ['get', 'road_number'], '']], 0];
const HAS_NAME = ['>', ['length', ['coalesce', ['get', 'road_name'], '']], 0];
const LABEL_NUMBER_FIRST = ['case', HAS_NUMBER, ['get', 'road_number'],
                            HAS_NAME, ['get', 'road_name'], ''];
const LABEL_NAME_FIRST = ['case', HAS_NAME, ['get', 'road_name'],
                          HAS_NUMBER, ['get', 'road_number'], ''];

function roadWidth(scale, extra) {
  const at = (w) => (extra
    ? ['+', ['*', MOTORWAY_VS_A, w * scale], extra]
    : ['*', MOTORWAY_VS_A, w * scale]);
  return ['interpolate', ['linear'], ['zoom'],
    8.5, at(0.4), 11, at(1.1), 14, at(2.6), 17, at(6.0)];
}

async function initRoads() {
  let data;
  try {
    const resp = await fetch('data/gb_roads.geojson', { cache: 'no-store' });
    if (!resp.ok) return;
    data = await resp.json();
  } catch (err) {
    console.warn('no roads file', err);
    return;
  }

  map.addSource('roads', { type: 'geojson', data });
  const { ink, casing } = roadColours();

  const isMajor = ['match', ['get', 'road_class'],
    ['Motorway', 'A Road'], true, false];

  // Casing first: a surface-coloured halo so the line reads over any fill.
  map.addLayer({
    id: 'roads-casing',
    type: 'line',
    source: 'roads',
    minzoom: ROAD_MINZOOM,
    layout: { 'line-cap': 'round', 'line-join': 'round' },
    paint: {
      'line-color': casing,
      'line-opacity': 0.55,
      'line-width': roadWidth(1, 1.8),
    },
  });

  map.addLayer({
    id: 'roads-b',
    type: 'line',
    source: 'roads',
    minzoom: ROAD_B_MINZOOM,
    filter: ['==', ['get', 'road_class'], 'B Road'],
    layout: { 'line-cap': 'round', 'line-join': 'round' },
    paint: { 'line-color': ink, 'line-opacity': 0.55, 'line-width': roadWidth(0.65) },
  });

  map.addLayer({
    id: 'roads-major',
    type: 'line',
    source: 'roads',
    minzoom: ROAD_MINZOOM,
    filter: isMajor,
    layout: { 'line-cap': 'round', 'line-join': 'round' },
    paint: { 'line-color': ink, 'line-opacity': 0.82, 'line-width': roadWidth(1) },
  });

  // Labels run along the line rather than sitting beside it, which is how a
  // road atlas does it and what makes a long road identifiable mid-way.
  map.addLayer({
    id: 'roads-labels',
    type: 'symbol',
    source: 'roads',
    minzoom: ROAD_LABEL_MINZOOM,
    filter: ['any',
      ['>', ['length', ['coalesce', ['get', 'road_number'], '']], 0],
      ['>', ['length', ['coalesce', ['get', 'road_name'], '']], 0]],
    layout: {
      'symbol-placement': 'line',
      // Road atlas behaviour: the number identifies a route across a region,
      // the name identifies a street once you are in a city. Switch between
      // them at the zoom where the question changes from "which road is this"
      // to "which street am I looking at".
      'text-field': ['step', ['zoom'], LABEL_NUMBER_FIRST, 13.5, LABEL_NAME_FIRST],
      'text-font': ['Noto Sans Regular'],
      'text-size': ['interpolate', ['linear'], ['zoom'], 11.5, 9.5, 15, 12],
      'symbol-spacing': 260,
      'text-padding': 4,
      'text-rotation-alignment': 'map',
      'text-pitch-alignment': 'viewport',
    },
    paint: {
      'text-color': ink,
      'text-halo-color': casing,
      'text-halo-width': 1.6,
      'text-opacity': 0.9,
    },
  });

  roadState.loaded = true;
  applyRoadColours();
}

function applyRoadColours() {
  if (!roadState.loaded) return;
  const { ink, casing } = roadColours();
  for (const id of ['roads-b', 'roads-major']) {
    if (map.getLayer(id)) map.setPaintProperty(id, 'line-color', ink);
  }
  if (map.getLayer('roads-casing')) {
    map.setPaintProperty('roads-casing', 'line-color', casing);
  }
  if (map.getLayer('roads-labels')) {
    map.setPaintProperty('roads-labels', 'text-color', ink);
    map.setPaintProperty('roads-labels', 'text-halo-color', casing);
  }
}

function setRoadsVisible(visible) {
  roadState.visible = visible;
  const value = visible ? 'visible' : 'none';
  for (const id of ['roads-casing', 'roads-b', 'roads-major', 'roads-labels']) {
    if (map.getLayer(id)) map.setLayoutProperty(id, 'visibility', value);
  }
}
