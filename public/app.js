// Ruta Segura CDMX — static build
// Loads a pre-baked safety score grid and computes route scores in the browser.
// Calls brouter.de directly (CORS-enabled) for bike routing.

const ASSETS = './assets';
const SAMPLE_M = 50;      // sample every 50m along the route
const BUFFER_M = 200;     // cyclist-crashes-near-route buffer

const SCORE_COLORS = [
  'step', ['get', 'score'],
  '#b10026', 25, '#fc4e2a', 50, '#feb24c', 75, '#41ab5d'
];

// ------------------------------------------------------------------ grid loader
class Grid {
  constructor(meta, score, hasInfra, stationDist, crashCount) {
    Object.assign(this, meta);
    this.score = score;
    this.hasInfra = hasInfra;
    this.stationDist = stationDist;
    this.crashCount = crashCount;
    // UTM bbox extents in meters
    [this.minx_m, this.miny_m, this.maxx_m, this.maxy_m] = meta.bbox_utm_m;
    this.dx_m = (this.maxx_m - this.minx_m) / this.nx;
    this.dy_m = (this.maxy_m - this.miny_m) / this.ny;
    // WGS84 extents (for quick lng/lat → grid mapping)
    [this.minx_ll, this.miny_ll, this.maxx_ll, this.maxy_ll] = meta.bbox_wgs84;
    this.dx_ll = (this.maxx_ll - this.minx_ll) / this.nx;
    this.dy_ll = (this.maxy_ll - this.miny_ll) / this.ny;
  }

  static async load() {
    const meta = await (await fetch(`${ASSETS}/grid_meta.json`)).json();
    const read = async (name, Ctor) => {
      const buf = await (await fetch(`${ASSETS}/${name}`)).arrayBuffer();
      return new Ctor(buf);
    };
    const [score, hasInfra, stationDist, crashCount] = await Promise.all([
      read('score_grid.bin', Float32Array),
      read('has_infra_grid.bin', Uint8Array),
      read('station_dist_grid.bin', Float32Array),
      read('crash_count_grid.bin', Uint16Array),
    ]);
    return new Grid(meta, score, hasInfra, stationDist, crashCount);
  }

  // Indexed (j=col, i=row). Row 0 is southmost.
  idx(i, j) { return i * this.nx + j; }

  // sample via bilinear interpolation from WGS84 (lon, lat)
  sampleScore(lon, lat) {
    const fj = (lon - this.minx_ll) / this.dx_ll - 0.5;  // fractional col
    const fi = (lat - this.miny_ll) / this.dy_ll - 0.5;  // fractional row
    const j0 = Math.floor(fj), i0 = Math.floor(fi);
    const j1 = j0 + 1, i1 = i0 + 1;
    if (j0 < 0 || i0 < 0 || j1 >= this.nx || i1 >= this.ny) return null;
    const tj = fj - j0, ti = fi - i0;
    const s00 = this.score[this.idx(i0, j0)];
    const s01 = this.score[this.idx(i0, j1)];
    const s10 = this.score[this.idx(i1, j0)];
    const s11 = this.score[this.idx(i1, j1)];
    return s00 * (1 - ti) * (1 - tj) + s01 * (1 - ti) * tj
         + s10 * ti       * (1 - tj) + s11 * ti       * tj;
  }

  // nearest-neighbor lookups (binary / distance are fine without interpolation)
  nearest(lon, lat) {
    const j = Math.round((lon - this.minx_ll) / this.dx_ll - 0.5);
    const i = Math.round((lat - this.miny_ll) / this.dy_ll - 0.5);
    if (j < 0 || i < 0 || j >= this.nx || i >= this.ny) return null;
    const k = this.idx(i, j);
    return {
      hasInfra: this.hasInfra[k] === 1,
      stationDist: this.stationDist[k],
      crashCount: this.crashCount[k],
    };
  }
}

// ------------------------------------------------------------------ route math
function haversineMeters(a, b) {
  const R = 6371008.8;
  const [lon1, lat1] = a, [lon2, lat2] = b;
  const toRad = d => d * Math.PI / 180;
  const dLat = toRad(lat2 - lat1), dLon = toRad(lon2 - lon1);
  const s = Math.sin(dLat / 2) ** 2 +
            Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(s));
}

function interpolateEveryMeters(coords, stepM) {
  if (coords.length < 2) return coords.slice();
  const out = [coords[0]];
  let remaining = stepM;
  for (let i = 0; i < coords.length - 1; i++) {
    const a = coords[i], b = coords[i + 1];
    let dist = haversineMeters(a, b);
    if (dist <= 0) continue;
    let start = [...a];
    while (dist >= remaining) {
      const t = remaining / dist;
      const p = [start[0] + t * (b[0] - start[0]), start[1] + t * (b[1] - start[1])];
      out.push(p);
      dist -= remaining;
      start = p;
      remaining = stepM;
    }
    remaining -= dist;
  }
  const last = coords[coords.length - 1];
  const lastOut = out[out.length - 1];
  if (lastOut[0] !== last[0] || lastOut[1] !== last[1]) out.push(last);
  return out;
}

function scoreRoute(grid, polyline) {
  const samples = interpolateEveryMeters(polyline, SAMPLE_M);
  const rows = [];
  let cum = 0;
  let prev = samples[0];
  for (let i = 0; i < samples.length; i++) {
    const [lon, lat] = samples[i];
    if (i > 0) cum += haversineMeters(prev, samples[i]);
    prev = samples[i];
    const score = grid.sampleScore(lon, lat);
    const meta = grid.nearest(lon, lat);
    if (score === null || meta === null) {
      rows.push({ lon, lat, cum_m: cum, score: null, outside: true });
    } else {
      rows.push({
        lon, lat, cum_m: cum, score,
        has_infra: meta.hasInfra,
        station_dist_m: meta.stationDist >= 9999 ? null : meta.stationDist,
        crash_count: meta.crashCount,
      });
    }
  }
  return rows;
}

function summarize(rows) {
  const scored = rows.filter(r => !r.outside);
  if (!scored.length) return { outside: true };
  const total = scored[scored.length - 1].cum_m - scored[0].cum_m;
  const segLens = [];
  for (let i = 0; i < scored.length - 1; i++) segLens.push(scored[i + 1].cum_m - scored[i].cum_m);
  const segScores = scored.slice(0, -1).map(r => r.score);
  const bandLen = (pred) => segLens.reduce((s, l, i) => s + (pred(segScores[i]) ? l : 0), 0);
  const infraLen = segLens.reduce((s, l, i) => s + (scored[i].has_infra ? l : 0), 0);
  const ecobiciLen = segLens.reduce((s, l, i) => {
    const d = scored[i].station_dist_m;
    return s + (d !== null && d <= 1500 ? l : 0);
  }, 0);

  const weighted = segLens.reduce((s, l, i) => s + l * segScores[i], 0);
  const meanScore = weighted / Math.max(total, 1);
  const sortedScores = scored.map(r => r.score).sort((a, b) => a - b);
  const worst100m = sortedScores.slice(0, Math.max(1, Math.floor(100 / SAMPLE_M)))
                                .reduce((a, b) => a + b, 0) / Math.max(1, Math.floor(100 / SAMPLE_M));

  // rough crashes-near-route estimate: max of per-sample crash_count
  // (200m buffer per cell, so this is the densest local crash neighborhood)
  const maxCrashNearby = Math.max(...scored.map(r => r.crash_count || 0));

  return {
    total_m: total,
    mean_score: meanScore,
    min_score: Math.min(...scored.map(r => r.score)),
    worst_100m_score: worst100m,
    pct_dangerous: 100 * bandLen(s => s < 25) / Math.max(total, 1),
    pct_risky:     100 * bandLen(s => s >= 25 && s < 50) / Math.max(total, 1),
    pct_ok:        100 * bandLen(s => s >= 50 && s < 75) / Math.max(total, 1),
    pct_safe:      100 * bandLen(s => s >= 75) / Math.max(total, 1),
    pct_with_infra: 100 * infraLen / Math.max(total, 1),
    pct_in_ecobici_footprint: 100 * ecobiciLen / Math.max(total, 1),
    max_crashes_nearby: maxCrashNearby,
    low_data_warning: maxCrashNearby < 10,
  };
}

// ------------------------------------------------------------------ brouter
async function fetchBikeRoute(start, end) {
  // start/end in [lon, lat]
  const url = `https://brouter.de/brouter?lonlats=${start[0]},${start[1]}|${end[0]},${end[1]}&profile=fastbike&alternativeidx=0&format=geojson`;
  const r = await fetch(url);
  if (!r.ok) throw new Error(`brouter ${r.status}`);
  const data = await r.json();
  const feat = data.features[0];
  const coords = feat.geometry.coordinates.map(c => [c[0], c[1]]); // drop elevation
  const props = feat.properties || {};
  return { coords, length_m: parseFloat(props['track-length'] || 0), total_time_s: parseFloat(props['total-time'] || 0) };
}

// ------------------------------------------------------------------ boot
const state = { pick: 0, startMarker: null, endMarker: null, grid: null };

const map = new maplibregl.Map({
  container: 'map',
  style: 'https://tiles.openfreemap.org/styles/positron',
  center: [-99.165, 19.415],
  zoom: 13,
});

function showToast(msg, ms = 5000) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.style.display = 'block';
  if (ms > 0) setTimeout(() => t.style.display = 'none', ms);
}

document.querySelectorAll('nav.tabs button').forEach(btn => {
  btn.addEventListener('click', () => {
    const tab = btn.dataset.tab;
    document.querySelectorAll('nav.tabs button').forEach(b => b.classList.toggle('active', b === btn));
    document.querySelectorAll('.tab-body').forEach(b => b.hidden = b.dataset.tab !== tab);
    if (tab === 'stats') loadKpis();
    if (tab === 'about') loadDataSources();
  });
});

map.on('load', async () => {
  // load grid in background, don't block the page
  Grid.load()
    .then(g => { state.grid = g; console.log(`[grid] ${g.nx}×${g.ny} cells loaded`); })
    .catch(e => { console.error(e); showToast('Failed to load safety grid.', 0); });

  // stations
  try {
    const r = await fetch(`${ASSETS}/stations.geojson`);
    const data = await r.json();
    map.addSource('stations', { type: 'geojson', data });
    map.addLayer({
      id: 'stations', type: 'circle', source: 'stations',
      paint: {
        'circle-radius': ['interpolate', ['linear'], ['zoom'], 11, 2, 14, 4, 17, 7],
        'circle-color': '#1971c2',
        'circle-stroke-color': '#fff',
        'circle-stroke-width': 1,
      },
    });
    map.on('click', 'stations', e => {
      const p = e.features[0].properties;
      const [lng, lat] = e.features[0].geometry.coordinates;
      new maplibregl.Popup().setLngLat([lng, lat]).setHTML(
        `<div style="font-size:12px"><b>Estación ${p.id}</b><br/>${p.calle_prin} × ${p.calle_secu}<br/>` +
        `<span style="color:#666">${p.colonia} · ${p.alcaldia}</span><br/>` +
        `<button onclick="useStation(${lat}, ${lng})">Use as start/end</button></div>`
      ).addTo(map);
    });
  } catch (e) { console.warn('no stations', e); }

  // route source
  map.addSource('route', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
  map.addLayer({
    id: 'route', type: 'line', source: 'route',
    paint: { 'line-width': 6, 'line-color': SCORE_COLORS, 'line-opacity': 0.95 },
  });

  // crash heatmap
  try {
    const r = await fetch(`${ASSETS}/crashes.geojson`);
    const data = await r.json();
    map.addSource('crashes', { type: 'geojson', data });
    map.addLayer({
      id: 'crashes-heat', type: 'heatmap', source: 'crashes',
      layout: { visibility: 'none' },
      paint: {
        'heatmap-weight': ['interpolate', ['linear'], ['get', 'fatal'], 0, 1, 1, 5],
        'heatmap-intensity': ['interpolate', ['linear'], ['zoom'], 10, 1, 16, 3],
        'heatmap-radius': ['interpolate', ['linear'], ['zoom'], 10, 10, 16, 30],
        'heatmap-opacity': 0.7,
        'heatmap-color': [
          'interpolate', ['linear'], ['heatmap-density'],
          0, 'rgba(0,0,0,0)', 0.2, '#feb24c', 0.4, '#fd8d3c',
          0.6, '#fc4e2a', 0.8, '#e31a1c', 1, '#b10026',
        ],
      },
    });
  } catch (e) {}
});

const setVis = (id, on) => { if (map.getLayer(id)) map.setLayoutProperty(id, 'visibility', on ? 'visible' : 'none'); };
document.getElementById('tg-stations').addEventListener('change', e => setVis('stations', e.target.checked));
document.getElementById('tg-heat').addEventListener('change', e => setVis('crashes-heat', e.target.checked));
document.getElementById('tg-fatal').addEventListener('change', e => {
  if (map.getLayer('crashes-heat')) {
    map.setFilter('crashes-heat', e.target.checked ? ['>=', ['get', 'fatal'], 1] : null);
  }
});
document.getElementById('tg-grid').addEventListener('change', e => {
  if (!state.grid) { showToast('Grid still loading…'); return; }
  toggleGridLayer(e.target.checked);
});

function toggleGridLayer(on) {
  if (!on) {
    if (map.getLayer('score-grid')) map.setLayoutProperty('score-grid', 'visibility', 'none');
    return;
  }
  if (!map.getSource('score-grid-src')) {
    const g = state.grid;
    const feats = [];
    const cellGeo = (i, j) => {
      const x0 = g.minx_ll + j * g.dx_ll, x1 = x0 + g.dx_ll;
      const y0 = g.miny_ll + i * g.dy_ll, y1 = y0 + g.dy_ll;
      return [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]];
    };
    for (let i = 0; i < g.ny; i++) {
      for (let j = 0; j < g.nx; j++) {
        const s = g.score[g.idx(i, j)];
        if (s > 0) {
          feats.push({
            type: 'Feature',
            properties: { s },
            geometry: { type: 'Polygon', coordinates: [cellGeo(i, j)] },
          });
        }
      }
    }
    map.addSource('score-grid-src', { type: 'geojson', data: { type: 'FeatureCollection', features: feats } });
    map.addLayer({
      id: 'score-grid',
      type: 'fill',
      source: 'score-grid-src',
      paint: {
        'fill-color': ['step', ['get', 's'], '#b10026', 25, '#fc4e2a', 50, '#feb24c', 75, '#41ab5d'],
        'fill-opacity': 0.35,
      },
    }, 'stations');
  } else {
    map.setLayoutProperty('score-grid', 'visibility', 'visible');
  }
}

map.on('click', e => {
  if (e.originalEvent.defaultPrevented) return;
  if (map.queryRenderedFeatures(e.point, { layers: ['stations'] }).length) return;
  const ll = `${e.lngLat.lat.toFixed(6)}, ${e.lngLat.lng.toFixed(6)}`;
  if (state.pick === 0) {
    document.getElementById('start').value = ll;
    state.startMarker?.remove();
    state.startMarker = new maplibregl.Marker({ color: '#1971c2' }).setLngLat(e.lngLat).addTo(map);
    state.pick = 1;
  } else {
    document.getElementById('end').value = ll;
    state.endMarker?.remove();
    state.endMarker = new maplibregl.Marker({ color: '#b10026' }).setLngLat(e.lngLat).addTo(map);
    state.pick = 0;
  }
});

window.useStation = (lat, lng) => {
  const v = `${lat.toFixed(6)}, ${lng.toFixed(6)}`;
  const s = document.getElementById('start');
  if (!s.value) { s.value = v; state.pick = 1; return; }
  document.getElementById('end').value = v; state.pick = 0;
};

function extractFromGmaps(url) {
  const pairs = [];
  const re = /!1d(-?\d+\.\d+)!2d(-?\d+\.\d+)/g;
  let m;
  while ((m = re.exec(url)) !== null) pairs.push({ lng: parseFloat(m[1]), lat: parseFloat(m[2]) });
  if (pairs.length >= 2) {
    return {
      start: `${pairs[0].lat.toFixed(6)}, ${pairs[0].lng.toFixed(6)}`,
      end: `${pairs[pairs.length - 1].lat.toFixed(6)}, ${pairs[pairs.length - 1].lng.toFixed(6)}`,
    };
  }
  const simple = url.match(/\/dir\/(-?\d+\.\d+),(-?\d+\.\d+)\/(-?\d+\.\d+),(-?\d+\.\d+)/);
  if (simple) return { start: `${simple[1]}, ${simple[2]}`, end: `${simple[3]}, ${simple[4]}` };
  return null;
}

document.getElementById('gmaps').addEventListener('input', e => {
  const p = extractFromGmaps(e.target.value);
  if (p) {
    document.getElementById('start').value = p.start;
    document.getElementById('end').value = p.end;
  }
});

document.getElementById('clear').addEventListener('click', () => {
  map.getSource('route')?.setData({ type: 'FeatureCollection', features: [] });
  state.startMarker?.remove(); state.endMarker?.remove();
  state.startMarker = state.endMarker = null; state.pick = 0;
  ['start', 'end', 'gmaps'].forEach(id => document.getElementById(id).value = '');
  document.getElementById('summary').style.display = 'none';
});

document.getElementById('go').addEventListener('click', async () => {
  const s = document.getElementById('start').value.trim();
  const t = document.getElementById('end').value.trim();
  if (!s || !t) return alert('Need start and end (lat, lng).');
  if (!state.grid) return showToast('Safety grid still loading, try again in a second…');
  const btn = document.getElementById('go');
  btn.disabled = true; btn.textContent = 'Routing…';
  try {
    const [lat1, lon1] = s.split(',').map(parseFloat);
    const [lat2, lon2] = t.split(',').map(parseFloat);
    const route = await fetchBikeRoute([lon1, lat1], [lon2, lat2]);
    const samples = scoreRoute(state.grid, route.coords);
    const summary = summarize(samples);
    renderRoute({ samples, summary, meta: { total_time_s: route.total_time_s, length_m: route.length_m } });
  } catch (e) {
    console.error(e);
    alert('Route failed: ' + e.message);
  } finally {
    btn.disabled = false; btn.textContent = 'Route & score';
  }
});

function renderRoute(data) {
  const s = data.samples;
  const feats = [];
  for (let i = 0; i < s.length - 1; i++) {
    if (s[i].outside || s[i + 1].outside) continue;
    feats.push({
      type: 'Feature',
      properties: { score: s[i].score },
      geometry: { type: 'LineString', coordinates: [[s[i].lon, s[i].lat], [s[i + 1].lon, s[i + 1].lat]] },
    });
  }
  map.getSource('route').setData({ type: 'FeatureCollection', features: feats });
  const lons = s.map(p => p.lon), lats = s.map(p => p.lat);
  map.fitBounds([[Math.min(...lons), Math.min(...lats)], [Math.max(...lons), Math.max(...lats)]], { padding: 80, duration: 500 });

  const sum = data.summary;
  if (sum.outside) {
    document.getElementById('summary').innerHTML = `<div class="card"><h3>Route safety</h3>
      <div class="hint">Route falls outside the scored CDMX bbox. Stay within Ciudad de México.</div></div>`;
    document.getElementById('summary').style.display = 'block';
    return;
  }
  const score = Math.round(sum.mean_score);
  const klass = score >= 75 ? 'good' : score >= 50 ? 'mid' : 'bad';
  const bar = `<div class="bar">
    <div style="width:${sum.pct_safe.toFixed(1)}%;background:#41ab5d"></div>
    <div style="width:${sum.pct_ok.toFixed(1)}%;background:#feb24c"></div>
    <div style="width:${sum.pct_risky.toFixed(1)}%;background:#fc4e2a"></div>
    <div style="width:${sum.pct_dangerous.toFixed(1)}%;background:#b10026"></div>
  </div>`;
  const warn = sum.low_data_warning ? `
    <div style="margin-top:10px; padding: 8px 10px; background:#fff4e6; border:1px solid #ffc078; color:#8a4f00; border-radius:6px; font-size:12px; line-height:1.4;">
      ⚠ <b>Low cyclist-data zone.</b> Few cyclist crashes recorded nearby. The score leans on motor-vehicle density and infra/station penalties.
    </div>` : '';
  document.getElementById('summary').innerHTML = `
    <div class="card">
      <h3>Route safety</h3>
      <div class="kpi ${klass}">${score}</div>
      <div class="kpi-label">out of 100</div>
      <div style="margin-top:12px">
        <div class="stat-row"><span>Distance</span><span>${(sum.total_m/1000).toFixed(2)} km</span></div>
        <div class="stat-row"><span>Time estimate</span><span>${Math.round(data.meta.total_time_s/60)} min</span></div>
        <div class="stat-row"><span>Worst 100 m avg</span><span>${Math.round(sum.worst_100m_score)}</span></div>
        <div class="stat-row"><span>On bike infrastructure</span><span>${sum.pct_with_infra.toFixed(0)}%</span></div>
        <div class="stat-row"><span>In Ecobici footprint</span><span>${sum.pct_in_ecobici_footprint.toFixed(0)}%</span></div>
        <div class="stat-row"><span>Densest crash count nearby</span><span>${sum.max_crashes_nearby} in 200 m</span></div>
      </div>
      ${bar}
      <div class="badges">
        <span class="badge" style="background:#b10026">${sum.pct_dangerous.toFixed(0)}% dangerous</span>
        <span class="badge" style="background:#fc4e2a">${sum.pct_risky.toFixed(0)}% risky</span>
        <span class="badge" style="background:#feb24c">${sum.pct_ok.toFixed(0)}% ok</span>
        <span class="badge" style="background:#41ab5d">${sum.pct_safe.toFixed(0)}% safe</span>
      </div>
      ${warn}
      <div style="margin-top:10px"><a class="info-link" href="#" onclick="document.querySelector('nav.tabs button[data-tab=about]').click(); return false;">How is this scored?</a></div>
    </div>`;
  document.getElementById('summary').style.display = 'block';
}

// ------------------------------------------------------------------ stats tab
let kpisLoaded = false;
async function loadKpis() {
  if (kpisLoaded) return;
  try {
    const k = await (await fetch(`${ASSETS}/kpis.json`)).json();
    document.getElementById('kpi-trips').textContent = (k.total_trips_all_time / 1e6).toFixed(1) + 'M';
    document.getElementById('kpi-30').textContent = (k.trips_last_30_days / 1e6).toFixed(2) + 'M';
    document.getElementById('kpi-daily').textContent = k.avg_daily_last_30.toLocaleString();
    document.getElementById('kpi-stations').textContent = k.active_stations + ' / ' + k.stations_total;
    document.getElementById('kpi-range').textContent = k.date_first + ' → ' + k.date_last;

    const totalG = Object.values(k.by_gender).reduce((a, b) => a + b, 0);
    document.getElementById('kpi-gender').innerHTML = Object.entries(k.by_gender).map(([g, v]) =>
      `<div class="stat-row"><span>${g}</span><span>${(v / totalG * 100).toFixed(1)}% (${(v / 1e6).toFixed(1)}M)</span></div>`
    ).join('');

    const years = Object.entries(k.by_year).sort(([a], [b]) => +a - +b);
    const max = Math.max(...years.map(([, v]) => v));
    document.getElementById('kpi-years').innerHTML = years.map(([y, v]) => {
      const pct = v / max * 100;
      return `<div style="display:flex; align-items:center; gap:6px; margin: 2px 0;">
        <span style="width:36px;color:#666">${y}</span>
        <div style="flex:1;background:#eee;height:8px;border-radius:3px;overflow:hidden">
          <div style="width:${pct}%;height:100%;background:var(--c-accent)"></div>
        </div>
        <span style="width:50px;text-align:right;font-size:11px">${(v / 1e6).toFixed(1)}M</span>
      </div>`;
    }).join('');

    document.getElementById('kpis-loading').style.display = 'none';
    document.getElementById('kpis').style.display = 'block';
    kpisLoaded = true;
  } catch (e) { document.getElementById('kpis-loading').textContent = 'Failed to load stats.'; }
}

// ------------------------------------------------------------------ about tab
let sourcesLoaded = false;
async function loadDataSources() {
  if (sourcesLoaded) return;
  try {
    const ds = await (await fetch(`${ASSETS}/datasets.json`)).json();
    const html = Object.values(ds).map(d => `
      <div class="ds-item">
        <b>${d.label}</b>
        <span>${d.source}${d.n ? ` · ${d.n.toLocaleString()} records` : ''}</span>
        <br/><a href="${d.url}" target="_blank" rel="noopener">${d.slug || d.url.replace(/https?:\/\//, '')}</a>
      </div>
    `).join('');
    document.getElementById('data-sources').innerHTML = html;
    sourcesLoaded = true;
  } catch (e) { document.getElementById('data-sources').textContent = 'Failed to load sources.'; }
}
