'use strict';

/* ------------------------------------------------------------------ *
 * Scenario branches: open one that does not exist, close one that does.
 *
 * Everything else on the map describes networks as they are. This asks what a
 * change would do. A proposed branch is a draggable marker; a closed branch is
 * an existing pin taken out of the measured network. The answer that matters
 * is incremental -- not how many people a new branch reaches, but how many it
 * reaches that the network did not already -- so the panel always shows the
 * ticked brands as they stand beside the same network with the changes made.
 *
 * Nothing here is saved or published: a scenario lives in the page and is gone
 * on reload.
 * ------------------------------------------------------------------ */

const scenarioState = {
  placing: false,
  added: [],            // {b, n, x, y, id, proposed: true}
  closed: new Set(),    // branchKey of existing branches taken out
  markers: new Map(),   // id -> maplibregl.Marker
  nextId: 1,
  consumed: null,       // the DOM event a placement used up
};

const PROPOSED_BRAND = 'Proposed';
const PROPOSED_COLOUR = '#111111';

function scenarioActive() {
  return scenarioState.added.length > 0 || scenarioState.closed.size > 0;
}

/** True when this map click was, or is about to be, spent placing a branch.
 *  Every other click handler asks first, so that dropping a branch on an area
 *  does not also select the area underneath it. */
function scenarioOwnsClick(e) {
  return scenarioState.placing ||
    (e && e.originalEvent && scenarioState.consumed === e.originalEvent);
}

/** The ticked brands with the scenario applied. */
function scenarioNetwork() {
  const base = selectedBranches().filter((b) => !scenarioState.closed.has(branchKey(b)));
  return base.concat(scenarioState.added);
}

/** Adults, and adults on £100k+, within the radius of any of `branches`.
 *  Kept apart from computeCatchment, which also drives what the map dims. */
function reachOf(branches) {
  const n = state.codes.length;
  const covered = new Uint8Array(n);
  for (const b of branches) markCovered(b.y, b.x, branchState.radiusKm, covered);
  const adults = state.values.adults;
  const high = state.values.n_100k_plus;
  let a = 0, h = 0;
  for (let i = 0; i < n; i++) {
    if (!covered[i]) continue;
    if (adults && Number.isFinite(adults[i])) a += adults[i];
    if (high && Number.isFinite(high[i])) h += high[i];
  }
  return { adults: a, high: h };
}

function showCatchment() {
  if (branchState.active) return;
  branchState.active = true;
  const cb = document.querySelector('#catchment-on');
  if (cb) cb.checked = true;
}

function setPlacing(on) {
  scenarioState.placing = on;
  map.getCanvas().style.cursor = on ? 'crosshair' : '';
  renderScenario();
}

function addProposed(lngLat) {
  const id = scenarioState.nextId++;
  const rec = {
    b: PROPOSED_BRAND, n: 'Proposed branch ' + id, proposed: true, id,
    x: lngLat.lng, y: lngLat.lat,
  };
  scenarioState.added.push(rec);

  const marker = new maplibregl.Marker({ draggable: true, color: PROPOSED_COLOUR })
    .setLngLat([rec.x, rec.y]).addTo(map);
  marker.getElement().title = rec.n + ' - drag to move';
  const moved = () => {
    const p = marker.getLngLat();
    rec.x = p.lng; rec.y = p.lat;
    refreshCatchment();
  };
  marker.on('drag', moved);
  marker.on('dragend', moved);
  scenarioState.markers.set(id, marker);

  showCatchment();
  refreshBranchLayers();
}

function removeProposed(id) {
  scenarioState.added = scenarioState.added.filter((r) => r.id !== id);
  const marker = scenarioState.markers.get(id);
  if (marker) marker.remove();
  scenarioState.markers.delete(id);
  refreshBranchLayers();
}

function closeFocused() {
  for (const key of branchState.focusedKeys) scenarioState.closed.add(key);
  branchState.focusedKeys.clear();
  showCatchment();
  refreshBranchLayers();
}

function resetScenario() {
  for (const marker of scenarioState.markers.values()) marker.remove();
  scenarioState.markers.clear();
  scenarioState.added = [];
  scenarioState.closed.clear();
  scenarioState.placing = false;
  map.getCanvas().style.cursor = '';
  refreshBranchLayers();
}

function initScenario() {
  // Registered once, for the life of the map. Placement is one click, then the
  // mode switches itself off: staying armed makes the next stray click on the
  // map open a branch nobody asked for.
  map.on('click', (e) => {
    if (!scenarioState.placing) return;
    scenarioState.consumed = e.originalEvent;
    scenarioState.placing = false;
    map.getCanvas().style.cursor = '';
    addProposed(e.lngLat);
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && scenarioState.placing) setPlacing(false);
  });
  renderScenario();
}

function signed(v) {
  const r = Math.round(v);
  return (r > 0 ? '+' : r < 0 ? '−' : '') + Math.abs(r).toLocaleString('en-GB');
}

function renderScenario() {
  const host = document.querySelector('#scenario');
  if (!host) return;

  const focused = branchState.focusedKeys.size;
  let html = '<h2>Scenario</h2>' +
    '<div class="branch-actions">' +
      '<button type="button" id="sc-add"' + (scenarioState.placing ? ' class="armed"' : '') + '>' +
        (scenarioState.placing ? 'Click the map…' : 'Open a branch') + '</button>' +
      '<button type="button" id="sc-close"' + (focused ? '' : ' disabled') + '>' +
        'Close selected' + (focused ? ' (' + focused + ')' : '') + '</button>' +
    '</div>';

  if (!scenarioActive()) {
    html += '<p class="legend-note">Test a change to the network. Open a branch ' +
      'anywhere and drag it around, or click existing pins and close them, and ' +
      'see what the ticked brands gain or lose at the current radius.</p>';
    host.innerHTML = html;
    wireScenario(host);
    return;
  }

  const base = selectedBranches();
  const scen = scenarioNetwork();
  const before = reachOf(base);
  const after = reachOf(scen);

  const row = (label, a, b) =>
    '<div class="sc-row"><span>' + label + '</span>' +
    '<span>' + fmt.count(a) + '</span><span>' + fmt.count(b) + '</span>' +
    '<span class="' + (b - a > 0.5 ? 'up' : b - a < -0.5 ? 'down' : '') + '">' +
      signed(b - a) + '</span></div>';

  html += '<div class="sc-table">' +
    '<div class="sc-row sc-head"><span></span><span>Now</span><span>Scenario</span>' +
      '<span>Change</span></div>' +
    row('Branches', base.length, scen.length) +
    row('Adults reached', before.adults, after.adults) +
    row('On £100k+', before.high, after.high) +
    '</div>';

  if (scenarioState.added.length) {
    html += '<h3 class="rank-head">Proposed</h3>' + scenarioState.added.map((r) => {
      // What this one branch contributes: the scenario with it, less the
      // scenario without it. Its own catchment would double-count everyone an
      // existing branch already reaches.
      const without = reachOf(scen.filter((b) => b !== r));
      return '<div class="sc-item"><span class="sc-name">' + r.n + '</span>' +
        '<span class="sc-gain" title="Adults on £100k+ this branch adds that ' +
          'nothing else in the scenario reaches">' + signed(after.high - without.high) +
          ' on £100k+</span>' +
        '<button type="button" class="sc-x" data-remove="' + r.id + '" ' +
          'title="Remove">&times;</button></div>';
    }).join('');
  }

  if (scenarioState.closed.size) {
    html += '<h3 class="rank-head">Closed</h3>' + [...scenarioState.closed].map((key) => {
      const b = branchState.byKey.get(key);
      const name = b ? b.n : key;
      return '<div class="sc-item"><span class="sc-name">' +
        (b ? '<i class="brand-dot" style="background:' + brandColour(b.b) + '"></i>' : '') +
        name + '</span><span class="sc-gain"></span>' +
        '<button type="button" class="sc-x" data-restore="' + encodeURIComponent(key) +
          '" title="Reopen">&times;</button></div>';
    }).join('');
  }

  html += '<button type="button" id="sc-reset" class="sc-reset">Reset scenario</button>' +
    '<p class="legend-note">' +
    (base.length ? '“Now” is the ticked brands as they stand. '
                 : 'No brands are ticked, so “Now” is empty and the scenario ' +
                   'is measured on its own. ') +
    'Reach is everyone within ' + branchState.radiusKm.toFixed(1) + ' km of any branch, ' +
    'counted once. Straight-line distance, modelled incomes: a way to compare ' +
    'options, not a forecast of customers.</p>';

  host.innerHTML = html;
  wireScenario(host);
}

function wireScenario(host) {
  const add = host.querySelector('#sc-add');
  if (add) add.onclick = () => setPlacing(!scenarioState.placing);
  const close = host.querySelector('#sc-close');
  if (close) close.onclick = closeFocused;
  const reset = host.querySelector('#sc-reset');
  if (reset) reset.onclick = resetScenario;
  host.onclick = (e) => {
    const rm = e.target.closest('[data-remove]');
    if (rm) { removeProposed(Number(rm.dataset.remove)); return; }
    const rs = e.target.closest('[data-restore]');
    if (rs) {
      scenarioState.closed.delete(decodeURIComponent(rs.dataset.restore));
      refreshBranchLayers();
    }
  };
}
