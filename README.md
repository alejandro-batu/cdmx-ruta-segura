# Ruta Segura CDMX

A cycling safety scorer for Mexico City. Paste two coordinates (or a Google Maps link), get a bike route, and see how dangerous each 50 m of it is — coloured on a map and scored 0–100 against cyclist crash history, bike infrastructure, and Ecobici coverage.

Built on open data from [datos.cdmx.gob.mx](https://datos.cdmx.gob.mx), OpenStreetMap, and [brouter](https://brouter.de).

## Quick start

```bash
git clone git@github.com:alejandro-batu/cdmx-ruta-segura.git
cd cdmx-ruta-segura

# install deps (geopandas needs GEOS/PROJ via wheels — use a venv)
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# download + prepare ~130 MB of public data from CKAN (one-time, ~2 min)
python scripts/bootstrap.py

# run the server (boot takes 60–90s for KDE fit on 40k+ crash points)
python scripts/route_score_server.py
# → open http://localhost:8765
```

**Optional**: to colour the entire 9×11 km downtown on the map (not just your drawn route), also run:

```bash
python scripts/fetch_osm_network.py    # 8–12 min via Overpass
python scripts/score_citywide_edges.py # 2–3 min, writes viewer/osm_scored.geojson
```

Reload the page; the whole map is now coloured.

## Deploy as a static site (Vercel)

No Python backend needed at runtime — scoring is done client-side off a pre-baked
200 m grid. One build step, then static files only.

```bash
python scripts/bootstrap.py            # once, to get raw data
python scripts/build_static_site.py    # bakes public/ (grid + assets + index.html)
# then, from the repo root:
vercel --prod                          # or link once and push to main → auto-deploy
```

The `public/` directory is self-contained: `index.html` + `app.js` + `assets/` (grids and layer JSON). Each route scoring round-trip is: brouter.de (bike routing, CORS) + bilinear sample of the local grid. Zero backend cost.

See `public/` after running the build. The grid meta (`public/assets/grid_meta.json`) pins the bake timestamp for auditability.

## What it does

Given a start and end in CDMX:

1. Calls **brouter** (`fastbike` profile) for a bike route.
2. Samples the route every ~50 m in UTM-14N metric space.
3. At each sample:
   - Reads the **cyclist-crash KDE** (12.7 k points 2018–2024) and the **all-vehicle-crash KDE** (165 k points) — blended 70/30.
   - Matches to the nearest **bike lane within 30 m** → infra multiplier (×0.50 confined ciclovía … ×1.0 mixed … **×1.70 no infra at all**).
   - Distance to nearest **Ecobici station** → station multiplier (×1.0 near, ×1.50 when >1.5 km away).
4. Applies **hard caps** so a route can't score safe if it has no bike infra and no Ecobici nearby:
   - No infra + far from stations → **max 55**
   - No infra only → max 75
   - Has infra but far from stations → max 85
5. Flags a **low-data warning** when fewer than 10 cyclist crashes are within 200 m of the route.

The dashboard (single-page MapLibre + vanilla JS) shows the route colour-coded per 50 m, a KPI card (mean score, worst 100 m, km by band, infra coverage, Ecobici coverage, nearby crashes), and toggle layers for Ecobici stations and the crash heatmap.

## Data sources

| Layer | CKAN dataset | Coverage |
|---|---|---|
| Cyclist-involved crashes | `hechos-de-transito-...-ampliada` (filter `tipo_vehiculo=BICICLETA`) | 2018–2024, 11,981 points |
| Cyclist accident points (FGJ) | `puntos-de-accidentes-de-ciclistas` | 2019 snapshot, 686 points |
| All-vehicle crashes | same hechos-de-tránsito | 2018–2024, 164,613 points |
| Bike infrastructure | `infraestructura-vial-ciclista` | 651 tramos, 580.7 km |
| Ecobici stations | `cicloestaciones-ecobici-nuevo-sistema` | 677 active |
| Ecobici volume | `afluencia-diaria-del-sistema-ecobici` | system-level, 2010–2024 |
| Bike-usable road network | OpenStreetMap via osmnx | downtown bbox |

## Project layout

```
scripts/
  bootstrap.py              download + prepare all CKAN data
  fetch_osm_network.py      pull OSM bike network for downtown bbox
  route_score_server.py     the HTTP server (:8765)
  score_citywide_edges.py   score every edge → viewer/osm_scored.geojson
  cycling_safety_score.py   earlier demo pipeline (single bbox)
viewer/
  index.html                the dashboard (single file)
data/                       populated by bootstrap.py
requirements.txt
```

## Known limits

- **No ridership normalisation.** Ecobici doesn't publish OD pairs; peripheral zones appear safer than they likely are. The infra/station penalties + score caps are the current mitigation.
- The router doesn't yet **prefer** safer routes — it picks brouter's `fastbike` polyline and scores what comes back. A custom safety-weighted profile is on the todo list.
- The map overlay currently covers only the 9×11 km downtown bbox; routing still works anywhere in CDMX.
- KDE bandwidth (150 m), blend weight (0.3), multipliers, and caps are hand-tuned.

## License / credits

Data © CDMX Portal de Datos Abiertos (public). Code MIT-ish — use, fork, improve. Routing © brouter. Base tiles © OpenFreeMap / OpenStreetMap contributors.
