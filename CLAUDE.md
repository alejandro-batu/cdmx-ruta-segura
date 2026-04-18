# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Setup (one-time, from a fresh clone):
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python scripts/bootstrap.py            # ~130 MB CKAN download + prep, idempotent
```

Run the server (boot takes 60–90s — it fits a KDE on 40k+ crash points):
```bash
python scripts/route_score_server.py [port]    # default :8765
```

Optional citywide overlay (only needed if you want the whole downtown bbox coloured on the map, not just the drawn route):
```bash
python scripts/fetch_osm_network.py            # 8–12 min via Overpass; ~2 GB RAM
python scripts/score_citywide_edges.py         # 2–3 min; writes viewer/osm_scored.geojson
```

There is no test suite, linter, or build step. Data directories (`data/`) and `viewer/osm_scored.geojson` are gitignored — expect them to be absent on a fresh clone and rebuilt by the scripts above.

## Architecture

**One server, one viewer, one scoring model.** The server (`scripts/route_score_server.py`) is a stdlib `ThreadingHTTPServer` that serves the MapLibre single-page app in `viewer/index.html` and exposes a small JSON API. There is no framework, no DB, no build tool — geopandas/scipy in the process, files on disk.

### The scoring pipeline

All scoring happens in metric space (`EPSG:32614`, UTM-14N). The flow is identical for a user-drawn route (`sample_route`) and the citywide overlay (`score_citywide_edges.py`) — `score_citywide_edges.py` imports the constants and helpers from `route_score_server.py` to keep them in lockstep. If you change scoring behavior, change it in `route_score_server.py` and rerun `score_citywide_edges.py` to regenerate the overlay.

1. **Two KDEs built once at server boot** (`build_surface`): cyclist crashes (SSC 2023/24 bike-folio filter + FGJ 2019 SHP) and all-vehicle crashes. Both weighted by `1 + FATALITY_WEIGHT * fatal`. Each KDE's P95 is estimated from a random subsample (not all points) — the 40k-point cap and subsampling are deliberate to keep per-point evaluation sub-millisecond.
2. **Blended risk** at each sample: `0.7 * cyclist_kde + 0.3 * motor_kde` (`MOTOR_BLEND_WEIGHT`). The motor KDE is what catches road danger in peripheral zones where cyclists are rare and the cyclist KDE underestimates.
3. **Multipliers**: infra type from nearest bike-infra segment within 30 m (`INFRA_MATCH_M` — deliberately wide because OSM centerlines and CDMX infra centerlines routinely differ by 15–25 m), Ecobici distance from `_station_mult_from_dist` (linear ramp between `STATION_NEAR_M=300` and `STATION_FAR_M=1500`).
4. **Hard score caps** (`SCORE_CAP_*`): a point with no bike infra cannot earn the top safety bands regardless of what the KDE says. This captures the "nobody rides here because it's unrideable" reality that a pure-crash-density model would miss.
5. **Low-data warning**: fewer than 10 cyclist crashes within a 200 m buffer of the route → the summary flags `low_data_warning`.

### HTTP surface

- `GET /` → `viewer/index.html`
- `GET /osm_scored.geojson` → pre-computed citywide overlay (may be absent)
- `GET /api/route?start=lat,lng&end=lat,lng` → brouter (`safety` profile, falls back to `fastbike`) → scored samples + summary. Input order is `lat,lng` to match Google Maps paste; internally we flip to `lon,lat`.
- `GET /api/score?polyline=<encoded>` → decode a Google polyline, score it
- `GET /api/stations`, `/api/kpis`, `/api/crashes` → static data passthroughs (crashes filtered to the CDMX bbox at request time)

### Scoring constants you'll likely want to know about

Tunable knobs live at the top of `route_score_server.py`: `KDE_BW_METERS` (150), `FATALITY_WEIGHT` (5), `INFRA_MATCH_M` (30), `SEGMENT_SAMPLE_M` (50), `MOTOR_BLEND_WEIGHT` (0.3), the `INFRA_BASE_MULT` dict (by Spanish `TIPO_IC` string — don't English-rename these, they come from the CKAN dataset), `NO_INFRA_PENALTY` (1.70), station penalty/distance bounds, and the three score caps. The README's "Known limits" section explains why each is hand-tuned (no ridership data to calibrate against).

### Data layout

`data/cycling/` holds crash CSVs, FGJ shapefile, bike-infra GeoJSON, and the optional OSM network. `data/ecobici/` holds station GeoJSON and system KPIs. Paths are overridable via `CDMX_DATA_CYCLING`, `CDMX_DATA_ECOBICI`, `CDMX_VIEWER` env vars. `scripts/cycling_safety_score.py` is an earlier single-bbox demo pipeline — the server does NOT use it; prefer `route_score_server.py` for any scoring-logic changes.
