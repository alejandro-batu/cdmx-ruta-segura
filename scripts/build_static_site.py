"""Pre-compute a score grid + copy static assets into public/ for Vercel.

Once per data refresh: evaluates the same scoring pipeline the server uses, at
every cell of a 200 m grid over the CDMX bbox, and writes the result as binary
Float32 / Uint8 arrays. The browser then does bilinear sampling at runtime — no
backend needed.

Outputs (under public/assets/):
  grid_meta.json         grid bbox + dims + generation timestamp
  score_grid.bin         float32, 0–100 final score per cell
  has_infra_grid.bin     uint8 0/1, for "% on infra" stat
  station_dist_grid.bin  float32 meters, clipped to 10000
  crash_count_grid.bin   uint16, cyclist crashes within 200m of cell center
  stations.geojson       677 Ecobici stations
  kpis.json              system KPIs
  crashes.geojson        12.7k cyclist crashes for the heatmap layer
"""
from __future__ import annotations
import json, os, shutil, sys, time
from pathlib import Path

import numpy as np
import geopandas as gpd
import pandas as pd
from shapely.geometry import Point

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from route_score_server import (
    build_surface,
    INFRA_BASE_MULT, NO_INFRA_PENALTY,
    STATION_NEAR_M, STATION_FAR_M, STATION_FAR_PENALTY,
    MOTOR_BLEND_WEIGHT,
    SCORE_CAP_NO_INFRA, SCORE_CAP_NO_INFRA_FAR_FROM_STATION,
    SCORE_CAP_FAR_FROM_STATION_WITH_INFRA,
    INFRA_MATCH_M, METRIC_CRS, WGS84,
    _station_mult_from_dist,
)

ROOT = Path(__file__).resolve().parent.parent
PUBLIC = ROOT / "public"
ASSETS = PUBLIC / "assets"

# CDMX-covering bbox, same that covers our crash data
BBOX_WGS = (-99.35, 19.20, -98.95, 19.60)   # west, south, east, north
CELL_M = 200


def main():
    ASSETS.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    print("[1/5] building risk surface from raw data…", flush=True)
    S = build_surface()

    print("[2/5] building grid in UTM-14N…", flush=True)
    minx_ll, miny_ll, maxx_ll, maxy_ll = BBOX_WGS
    corners = gpd.GeoSeries(
        [Point(minx_ll, miny_ll), Point(maxx_ll, maxy_ll)], crs=WGS84
    ).to_crs(METRIC_CRS)
    minx_m, miny_m = corners.iloc[0].x, corners.iloc[0].y
    maxx_m, maxy_m = corners.iloc[1].x, corners.iloc[1].y
    nx = int(round((maxx_m - minx_m) / CELL_M))
    ny = int(round((maxy_m - miny_m) / CELL_M))
    # pack cells row-major: row 0 is southmost row
    cx = minx_m + (np.arange(nx) + 0.5) * CELL_M
    cy = miny_m + (np.arange(ny) + 0.5) * CELL_M
    XX, YY = np.meshgrid(cx, cy)  # shape (ny, nx)
    flat_xy = np.vstack([XX.ravel(), YY.ravel()])
    print(f"    grid: {nx} × {ny} = {nx*ny:,} cells", flush=True)

    print("[3/5] evaluating risk + multipliers at cell centers…", flush=True)
    t = time.time()
    risk_c = S["kde_cyclist"](flat_xy) / max(S["norm_cyclist"], 1e-30)
    print(f"    cyclist KDE: {time.time()-t:.1f}s", flush=True)
    t = time.time()
    risk_m = S["kde_motor"](flat_xy) / max(S["norm_motor"], 1e-30)
    print(f"    motor KDE:   {time.time()-t:.1f}s", flush=True)
    risk_blend = (1 - MOTOR_BLEND_WEIGHT) * risk_c + MOTOR_BLEND_WEIGHT * risk_m

    cell_pts = gpd.GeoDataFrame(
        {"_row": np.arange(flat_xy.shape[1])},
        geometry=[Point(x, y) for x, y in flat_xy.T],
        crs=METRIC_CRS,
    )

    print("    infra join…", flush=True)
    infra_join = gpd.sjoin_nearest(cell_pts, S["infra"], how="left", max_distance=INFRA_MATCH_M)
    infra_join = infra_join[~infra_join.index.duplicated(keep="first")]
    infra_tipo = infra_join["TIPO_IC"].values
    has_infra = np.array([isinstance(t_, str) for t_ in infra_tipo])
    infra_mult = np.array([
        INFRA_BASE_MULT.get(t_, NO_INFRA_PENALTY) if isinstance(t_, str) else NO_INFRA_PENALTY
        for t_ in infra_tipo
    ])

    print("    station join…", flush=True)
    sta_join = gpd.sjoin_nearest(cell_pts, S["stations"], how="left", distance_col="_sd")
    sta_join = sta_join[~sta_join.index.duplicated(keep="first")]
    station_dist = sta_join["_sd"].to_numpy(dtype=float)
    station_mult = _station_mult_from_dist(station_dist)
    far_from_station = np.where(np.isnan(station_dist), True, station_dist > STATION_FAR_M)

    # apply the same blend + multipliers + score caps
    weighted = risk_blend * infra_mult * station_mult
    score_raw = 100 * (1 - np.clip(weighted, 0, 1))
    cap = np.full_like(score_raw, 100.0)
    cap = np.where(has_infra & far_from_station, np.minimum(cap, SCORE_CAP_FAR_FROM_STATION_WITH_INFRA), cap)
    cap = np.where(~has_infra & ~far_from_station, np.minimum(cap, SCORE_CAP_NO_INFRA), cap)
    cap = np.where(~has_infra & far_from_station, np.minimum(cap, SCORE_CAP_NO_INFRA_FAR_FROM_STATION), cap)
    score = np.minimum(score_raw, cap)

    # crash count per cell (for low-data detection at runtime)
    print("    crash-count per cell (200m buffer)…", flush=True)
    t = time.time()
    cyc = S["cyclist_points_m"].copy()
    cyc.sindex
    # use spatial index: for each cell, count crash points within 200m of center
    buf = cell_pts.geometry.buffer(200)
    buf_gdf = gpd.GeoDataFrame({"row": np.arange(len(buf))}, geometry=buf, crs=METRIC_CRS)
    sj = gpd.sjoin(cyc, buf_gdf, how="inner", predicate="within")
    counts = sj.groupby("index_right").size()
    crash_count = np.zeros(flat_xy.shape[1], dtype=np.uint16)
    crash_count[counts.index.values] = np.clip(counts.values, 0, 65535).astype(np.uint16)
    print(f"    crash counts in {time.time()-t:.1f}s", flush=True)

    print("[4/5] writing binary grids…", flush=True)
    score_grid = score.reshape(ny, nx).astype(np.float32)
    infra_grid = has_infra.reshape(ny, nx).astype(np.uint8)
    station_grid = np.where(np.isnan(station_dist), 9999.0, station_dist).reshape(ny, nx).astype(np.float32)
    crash_grid = crash_count.reshape(ny, nx)
    (ASSETS / "score_grid.bin").write_bytes(score_grid.tobytes())
    (ASSETS / "has_infra_grid.bin").write_bytes(infra_grid.tobytes())
    (ASSETS / "station_dist_grid.bin").write_bytes(station_grid.tobytes())
    (ASSETS / "crash_count_grid.bin").write_bytes(crash_grid.tobytes())

    meta = {
        "bbox_wgs84": list(BBOX_WGS),
        "bbox_utm_m": [minx_m, miny_m, maxx_m, maxy_m],
        "nx": nx, "ny": ny, "cell_m": CELL_M,
        "utm_epsg": "EPSG:32614",
        "row_order": "south_to_north",
        "endianness": "little",
        "grids": {
            "score_grid.bin": "float32, final 0-100 score, shape (ny, nx)",
            "has_infra_grid.bin": "uint8 0/1, bike infra within 30m",
            "station_dist_grid.bin": "float32 meters to nearest Ecobici station (9999 if none)",
            "crash_count_grid.bin": "uint16 cyclist crashes 2018-2024 within 200m of cell center",
        },
        "generated_at": pd.Timestamp.utcnow().isoformat(),
    }
    (ASSETS / "grid_meta.json").write_text(json.dumps(meta, indent=2))

    print("[5/5] copying static layer assets…", flush=True)
    for src_rel, dst_rel in [
        ("data/ecobici/stations.geojson", "stations.geojson"),
        ("data/ecobici/kpis.json", "kpis.json"),
    ]:
        sp = ROOT / src_rel
        if sp.exists():
            shutil.copy(sp, ASSETS / dst_rel)
            print(f"    ✓ {dst_rel}")

    # smaller crashes geojson for the heatmap layer (lon, lat, fatal only)
    if not (ASSETS / "crashes.geojson").exists():
        crashes_csv = ROOT / "data" / "cycling" / "bike_crashes_ssc.csv"
        if crashes_csv.exists():
            df = pd.read_csv(crashes_csv, low_memory=False)
            df["fatal"] = df["personas_fallecidas"].fillna(0).astype(int).clip(0, 5)
            feats = [
                {"type": "Feature",
                 "geometry": {"type": "Point", "coordinates": [float(r.longitud), float(r.latitud)]},
                 "properties": {"fatal": int(r.fatal)}}
                for r in df.itertuples() if not pd.isna(r.latitud) and not pd.isna(r.longitud)
            ]
            (ASSETS / "crashes.geojson").write_text(
                json.dumps({"type": "FeatureCollection", "features": feats})
            )
            print(f"    ✓ crashes.geojson ({len(feats)} points)")

    # dataset catalogue used for the About/methodology UI
    datasets = {
        "bike_crashes": {
            "label": "Cyclist-involved crashes 2018–2024",
            "slug": "hechos-de-transito-registrados-por-la-ssc-2024-serie-de-datos-ampliada-no-comparativa",
            "source": "SSC — Secretaría de Seguridad Ciudadana",
            "url": "https://datos.cdmx.gob.mx/dataset/hechos-de-transito-registrados-por-la-ssc-2024-serie-de-datos-ampliada-no-comparativa",
            "n": 11981,
        },
        "fgj_accidentes": {
            "label": "FGJ cyclist accident points 2019",
            "slug": "puntos-de-accidentes-de-ciclistas",
            "source": "FGJ — Fiscalía General de Justicia",
            "url": "https://datos.cdmx.gob.mx/dataset/puntos-de-accidentes-de-ciclistas",
            "n": 686,
        },
        "motor_crashes": {
            "label": "All-vehicle crashes 2018–2024",
            "slug": "hechos-de-transito-reportados-por-ssc-base-ampliada-no-comparativa",
            "source": "SSC",
            "url": "https://datos.cdmx.gob.mx/dataset/hechos-de-transito-reportados-por-ssc-base-ampliada-no-comparativa",
            "n": 164613,
        },
        "bike_infra": {
            "label": "CDMX bike infrastructure (580.7 km)",
            "slug": "infraestructura-vial-ciclista",
            "source": "SEMOVI — Secretaría de Movilidad",
            "url": "https://datos.cdmx.gob.mx/dataset/infraestructura-vial-ciclista",
            "n": 651,
        },
        "ecobici_stations": {
            "label": "Ecobici stations (active)",
            "slug": "cicloestaciones-ecobici-nuevo-sistema",
            "source": "SEMOVI — Ecobici",
            "url": "https://datos.cdmx.gob.mx/dataset/cicloestaciones-ecobici-nuevo-sistema",
            "n": 677,
        },
        "ecobici_afluencia": {
            "label": "Ecobici trip volume 2010–2024",
            "slug": "afluencia-diaria-del-sistema-ecobici",
            "source": "SEMOVI — Ecobici",
            "url": "https://datos.cdmx.gob.mx/dataset/afluencia-diaria-del-sistema-ecobici",
            "n": 103845266,
        },
        "osm": {
            "label": "Bike-usable road network",
            "source": "OpenStreetMap",
            "url": "https://www.openstreetmap.org",
            "n": None,
        },
        "brouter": {
            "label": "Bike routing engine",
            "source": "brouter.de",
            "url": "https://brouter.de",
            "n": None,
        },
    }
    (ASSETS / "datasets.json").write_text(json.dumps(datasets, indent=2))
    print(f"    ✓ datasets.json")

    print(f"\n✓ built public/ in {time.time()-t0:.1f}s")
    print(f"  → put public/index.html in place, then `vercel --prod` from repo root")


if __name__ == "__main__":
    main()
