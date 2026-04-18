"""HTTP server that scores a bike route against the citywide KDE risk surface.

Serves:
  GET /                       → viewer index
  GET /osm_scored.geojson     → demo scored edges (pre-computed)
  GET /api/route?start=lon,lat&end=lon,lat
                              → calls brouter.de bike routing, scores the polyline,
                                returns GeoJSON with a safety_score per 50m segment
  GET /api/score?polyline=<google-encoded>
                              → decodes a Google polyline, scores it

The KDE is built at startup from ALL citywide cyclist crashes (2018-2024 SSC + 2019 FGJ),
not just the demo bbox — so the paste-a-route scorer works anywhere in CDMX.
"""
from __future__ import annotations
import json
import math
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, urlopen
from urllib.error import URLError

import numpy as np
import geopandas as gpd
import pandas as pd
from scipy.stats import gaussian_kde
from shapely.geometry import Point

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.environ.get("CDMX_DATA_CYCLING", os.path.join(_ROOT, "data", "cycling"))
ECOBICI = os.environ.get("CDMX_DATA_ECOBICI", os.path.join(_ROOT, "data", "ecobici"))
VIEWER = os.environ.get("CDMX_VIEWER", os.path.join(_ROOT, "viewer"))

METRIC_CRS = "EPSG:32614"
WGS84 = "EPSG:4326"
KDE_BW_METERS = 150.0
FATALITY_WEIGHT = 5.0
INFRA_MATCH_M = 30.0  # widened: OSM road centerlines and CDMX infra centerlines routinely differ by 15-25m
SEGMENT_SAMPLE_M = 50.0

INFRA_BASE_MULT = {
    "Ciclovia":                           0.50,
    "Ciclovia bidireccional":             0.50,
    "Sendero compartido":                 0.65,
    "Carril bus bici":                    0.70,
    "Ciclocarril":                        0.75,
    "Carril de prioridad ciclista":       0.80,
    "Infraestructura ciclista emergente": 0.85,
}
# applied when NO bike infra is within INFRA_MATCH_M of the point
NO_INFRA_PENALTY = 1.70
# max penalty for being far from the Ecobici footprint
STATION_FAR_PENALTY = 1.50
STATION_NEAR_M = 300.0
STATION_FAR_M = 1500.0

# Score CAPS — a point without bike infra cannot earn the top safety bands,
# no matter how low the KDE says crashes are. Captures the "nobody rides here
# because it's unrideable" reality.
SCORE_CAP_NO_INFRA                    = 75  # infra absent, near a station
SCORE_CAP_NO_INFRA_FAR_FROM_STATION   = 55  # infra absent AND >1.5km from any station
SCORE_CAP_FAR_FROM_STATION_WITH_INFRA = 85  # has infra but peripheral


def _kde_from(gdf, fatality_weight, bw_m, max_norm_samples=3000, max_fit_points=40000):
    """Fit a gaussian KDE. Subsample points if there are many — every evaluation is
    O(N_fit), so ~40k points keeps per-point evals in the sub-millisecond range."""
    gdf = gdf.to_crs(METRIC_CRS)
    xs = gdf.geometry.x.values
    ys = gdf.geometry.y.values
    weights = 1.0 + fatality_weight * gdf["fatal"].values
    if xs.size > max_fit_points:
        rng = np.random.default_rng(1)
        idx = rng.choice(xs.size, size=max_fit_points, replace=False)
        xs = xs[idx]; ys = ys[idx]; weights = weights[idx]
    pts = np.vstack([xs, ys])
    std = np.sqrt((xs.std() ** 2 + ys.std() ** 2) / 2.0)
    factor = bw_m / std if std > 0 else 0.05
    kde = gaussian_kde(pts, weights=weights, bw_method=factor)
    # P95 estimation: sample a random subset so this stays O(N) not O(N^2)
    n = pts.shape[1]
    if n > max_norm_samples:
        rng = np.random.default_rng(0)
        idx = rng.choice(n, size=max_norm_samples, replace=False)
        sample_pts = pts[:, idx]
    else:
        sample_pts = pts
    sample_vals = kde(sample_pts)
    norm = float(np.percentile(sample_vals, 95))
    return kde, norm, gdf


def build_surface():
    """Build two KDEs: cyclist crashes and all-vehicle crashes.

    Risk is a weighted blend. The all-vehicle surface captures road danger
    in areas where cyclists are rare and the cyclist KDE underestimates risk.
    """
    # cyclist crashes (SSC + FGJ)
    ssc = pd.read_csv(f"{DATA}/bike_crashes_ssc.csv", low_memory=False)
    ssc = ssc.dropna(subset=["latitud", "longitud"])
    ssc_gdf = gpd.GeoDataFrame(
        {"fatal": ssc["personas_fallecidas"].fillna(0).astype(float).clip(0, 5)},
        geometry=gpd.points_from_xy(ssc["longitud"], ssc["latitud"]),
        crs=WGS84,
    )
    try:
        fgj = gpd.read_file(
            f"{DATA}/accidentes_shp/Puntos de accidentes de Ciclistas/accidentado_ciclista.shp"
        )
        fgj = fgj[["total_occi", "geometry"]].copy()
        fgj["fatal"] = fgj["total_occi"].fillna(0).astype(float).clip(0, 5)
        cyclist = pd.concat([ssc_gdf[["fatal", "geometry"]], fgj[["fatal", "geometry"]]], ignore_index=True)
    except Exception:
        cyclist = ssc_gdf
    cyclist = gpd.GeoDataFrame(cyclist, geometry="geometry", crs=WGS84)

    # all-vehicle crashes (hechos_transito 2023 + 2024 combined)
    all_veh = []
    for fname in ["hechos_transito_2023.csv", "hechos_transito_2024.csv"]:
        try:
            h = pd.read_csv(f"{DATA}/{fname}", low_memory=False)
            h["latitud"] = pd.to_numeric(h["latitud"], errors="coerce")
            h["longitud"] = pd.to_numeric(h["longitud"], errors="coerce")
            h = h.dropna(subset=["latitud", "longitud"])
            h = h[(h["latitud"].between(19.0, 19.7)) & (h["longitud"].between(-99.4, -98.9))]
            h_gdf = gpd.GeoDataFrame(
                {"fatal": h["personas_fallecidas"].fillna(0).astype(float).clip(0, 5)},
                geometry=gpd.points_from_xy(h["longitud"], h["latitud"]),
                crs=WGS84,
            )
            all_veh.append(h_gdf)
        except Exception as e:
            print(f"[warn] skipping {fname}: {e}", flush=True)
    motor = pd.concat(all_veh, ignore_index=True) if all_veh else cyclist
    motor = gpd.GeoDataFrame(motor, geometry="geometry", crs=WGS84)

    kde_c, norm_c, cyclist_m = _kde_from(cyclist, FATALITY_WEIGHT, KDE_BW_METERS)
    kde_m, norm_m, _ = _kde_from(motor, FATALITY_WEIGHT, KDE_BW_METERS)

    infra = gpd.read_file(f"{DATA}/infra_full_wgs84.geojson").to_crs(METRIC_CRS)
    infra = infra[["TIPO_IC", "ESTADO", "geometry"]].copy()
    infra.sindex

    stations = gpd.read_file(f"{ECOBICI}/stations.geojson").to_crs(METRIC_CRS)
    stations.sindex

    print(
        f"[boot] cyclist={len(cyclist)} motor={len(motor)} infra={len(infra)} stations={len(stations)} "
        f"P95_cyc={norm_c:.3g} P95_motor={norm_m:.3g}",
        flush=True,
    )
    return {
        "kde_cyclist": kde_c,
        "norm_cyclist": norm_c,
        "kde_motor": kde_m,
        "norm_motor": norm_m,
        "cyclist_points_m": cyclist_m,
        "infra": infra,
        "stations": stations,
    }


SURFACE = build_surface()
KDE = SURFACE["kde_cyclist"]  # legacy alias
NORM = SURFACE["norm_cyclist"]
INFRA = SURFACE["infra"]
STATIONS = SURFACE["stations"]
MOTOR_BLEND_WEIGHT = 0.3  # weight on the all-vehicle surface
LOW_DATA_THRESHOLD = 10  # cyclist crashes within 200m-buffer of route → warning below this


def decode_polyline(s: str, precision: int = 5):
    """Google encoded polyline → [(lat, lon), ...]."""
    coords = []
    idx = lat = lon = 0
    factor = 10 ** precision
    while idx < len(s):
        for kind in (0, 1):
            result = shift = 0
            while True:
                byte = ord(s[idx]) - 63
                idx += 1
                result |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if kind == 0:
                lat += delta
            else:
                lon += delta
        coords.append((lat / factor, lon / factor))
    return coords


def route_via_brouter(start, end):
    url = (
        "https://brouter.de/brouter"
        f"?lonlats={start[0]},{start[1]}%7C{end[0]},{end[1]}"
        "&profile=safety&alternativeidx=0&format=geojson"
    )
    try:
        with urlopen(Request(url, headers={"User-Agent": "cdmx-cycling-safety/0.1"}), timeout=20) as r:
            data = json.load(r)
    except URLError:
        # fallback to fastbike profile if safety not available
        url = url.replace("profile=safety", "profile=fastbike")
        with urlopen(Request(url, headers={"User-Agent": "cdmx-cycling-safety/0.1"}), timeout=20) as r:
            data = json.load(r)
    feat = data["features"][0]
    coords = [(c[0], c[1]) for c in feat["geometry"]["coordinates"]]  # lon,lat
    props = feat.get("properties", {})
    return coords, {
        "length_m": float(props.get("track-length", 0)),
        "total_time_s": float(props.get("total-time", 0)),
        "profile": "brouter",
    }


def _station_mult_from_dist(dist_m):
    """Linear penalty from 1.0 (at STATION_NEAR_M or closer) to STATION_FAR_PENALTY at STATION_FAR_M."""
    arr = np.where(
        np.isnan(dist_m),
        STATION_FAR_PENALTY,
        np.clip((dist_m - STATION_NEAR_M) / (STATION_FAR_M - STATION_NEAR_M), 0, 1)
        * (STATION_FAR_PENALTY - 1.0)
        + 1.0,
    )
    return arr


def sample_route(coords_lonlat, sample_m=SEGMENT_SAMPLE_M):
    """Score a polyline every ~sample_m. Returns list of per-sample dicts."""
    if len(coords_lonlat) < 2:
        return []
    from shapely.geometry import LineString
    line = LineString(coords_lonlat)
    line_m = gpd.GeoSeries([line], crs=WGS84).to_crs(METRIC_CRS).iloc[0]
    total_m = line_m.length
    n = max(2, int(total_m / sample_m) + 1)
    distances = np.linspace(0, total_m, n)
    pts_m = [line_m.interpolate(d) for d in distances]
    xs = np.array([p.x for p in pts_m])
    ys = np.array([p.y for p in pts_m])

    # blended risk: cyclist KDE dominates, motor KDE picks up general road danger
    risk_cyc = SURFACE["kde_cyclist"](np.vstack([xs, ys])) / max(SURFACE["norm_cyclist"], 1e-30)
    risk_motor = SURFACE["kde_motor"](np.vstack([xs, ys])) / max(SURFACE["norm_motor"], 1e-30)
    risk_norm = (1 - MOTOR_BLEND_WEIGHT) * risk_cyc + MOTOR_BLEND_WEIGHT * risk_motor

    pts_gdf = gpd.GeoDataFrame(
        {"_row": np.arange(len(pts_m))},
        geometry=[Point(x, y) for x, y in zip(xs, ys)],
        crs=METRIC_CRS,
    )
    # infra match — points within INFRA_MATCH_M get a discount; others get a penalty
    joined = gpd.sjoin_nearest(pts_gdf, INFRA, how="left", max_distance=INFRA_MATCH_M)
    joined = joined[~joined.index.duplicated(keep="first")]
    infra_tipo = joined["TIPO_IC"].values
    infra_mult = np.array([
        INFRA_BASE_MULT.get(t, NO_INFRA_PENALTY) if isinstance(t, str) else NO_INFRA_PENALTY
        for t in infra_tipo
    ])

    # station distance — continuous penalty the further from Ecobici footprint
    sta_joined = gpd.sjoin_nearest(pts_gdf, STATIONS, how="left", distance_col="_station_dist")
    sta_joined = sta_joined[~sta_joined.index.duplicated(keep="first")]
    station_dist = sta_joined["_station_dist"].to_numpy(dtype=float)
    station_mult = _station_mult_from_dist(station_dist)

    total_mult = infra_mult * station_mult
    weighted = risk_norm * total_mult
    score_raw = 100 * (1 - np.clip(weighted, 0, 1))

    # score caps: a point without bike infra cannot score "safe"
    has_infra_arr = np.array([isinstance(t, str) for t in infra_tipo])
    far_from_station = np.where(np.isnan(station_dist), True, station_dist > STATION_FAR_M)
    cap = np.full_like(score_raw, 100.0)
    cap = np.where(has_infra_arr & far_from_station, np.minimum(cap, SCORE_CAP_FAR_FROM_STATION_WITH_INFRA), cap)
    cap = np.where(~has_infra_arr & ~far_from_station, np.minimum(cap, SCORE_CAP_NO_INFRA), cap)
    cap = np.where(~has_infra_arr & far_from_station, np.minimum(cap, SCORE_CAP_NO_INFRA_FAR_FROM_STATION), cap)
    score = np.minimum(score_raw, cap).round(1)

    pts_wgs = gpd.GeoSeries([Point(x, y) for x, y in zip(xs, ys)], crs=METRIC_CRS).to_crs(WGS84)
    out = []
    for i, (p_wgs, d) in enumerate(zip(pts_wgs, distances)):
        out.append({
            "lon": float(p_wgs.x),
            "lat": float(p_wgs.y),
            "cum_m": float(d),
            "risk_cyclist": float(risk_cyc[i]),
            "risk_motor": float(risk_motor[i]),
            "infra_mult": float(infra_mult[i]),
            "station_mult": float(station_mult[i]),
            "station_dist_m": None if np.isnan(station_dist[i]) else float(station_dist[i]),
            "has_infra": bool(isinstance(infra_tipo[i], str)),
            "weighted": float(weighted[i]),
            "score": float(score[i]),
        })
    return out


def summarize(samples, coords_lonlat):
    if not samples:
        return {}
    # pair N-1 segment lengths with the N-1 leading sample scores
    scores_all = np.array([s["score"] for s in samples])
    cum_m = np.array([s["cum_m"] for s in samples])
    lengths = np.diff(cum_m)                # len = N-1
    scores = scores_all[:-1]                # leading-sample score per segment
    total_m = float(cum_m[-1])
    dangerous_m = float(lengths[scores < 25].sum())
    risky_m = float(lengths[(scores >= 25) & (scores < 50)].sum())
    ok_m = float(lengths[(scores >= 50) & (scores < 75)].sum())
    safe_m = float(lengths[scores >= 75].sum())

    has_infra_all = np.array([s["has_infra"] for s in samples])
    has_infra = has_infra_all[:-1]
    infra_m = float(lengths[has_infra].sum())

    station_dists_all = np.array([s["station_dist_m"] if s["station_dist_m"] is not None else np.nan for s in samples])
    station_dists = station_dists_all[:-1]
    within_foot = ~np.isnan(station_dists) & (station_dists <= STATION_FAR_M)
    station_m = float(lengths[within_foot].sum())

    # cyclist crash count in a 200 m buffer around the route (for low-data warning)
    from shapely.geometry import LineString
    line = LineString(coords_lonlat)
    buf = gpd.GeoSeries([line], crs=WGS84).to_crs(METRIC_CRS).buffer(200).iloc[0]
    cyc = SURFACE["cyclist_points_m"]
    n_crashes = int(cyc.within(buf).sum())
    crashes_per_km = float(n_crashes / max(total_m / 1000, 0.1))

    return {
        "total_m": total_m,
        "mean_score": float(np.average(scores, weights=np.maximum(lengths, 1e-6))),
        "min_score": float(scores_all.min()),
        "worst_100m_score": float(np.sort(scores_all)[:max(1, int(100 / SEGMENT_SAMPLE_M))].mean()),
        "pct_dangerous": 100 * dangerous_m / max(total_m, 1),
        "pct_risky": 100 * risky_m / max(total_m, 1),
        "pct_ok": 100 * ok_m / max(total_m, 1),
        "pct_safe": 100 * safe_m / max(total_m, 1),
        "pct_with_infra": 100 * infra_m / max(total_m, 1),
        "pct_in_ecobici_footprint": 100 * station_m / max(total_m, 1),
        "cyclist_crashes_near_route": n_crashes,
        "cyclist_crashes_per_km": crashes_per_km,
        "low_data_warning": n_crashes < LOW_DATA_THRESHOLD,
    }


def parse_coord(s):
    a, b = s.split(",")
    return float(a), float(b)  # lon, lat — we expect "lon,lat"


def parse_latlng(s):
    a, b = s.split(",")
    return float(b), float(a)  # given "lat,lng", return "lon,lat"


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, ctype):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except FileNotFoundError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # less noise
        sys.stderr.write(f"{self.address_string()} - {fmt % args}\n")

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("", "/"):
            self._send_file(os.path.join(VIEWER, "index.html"), "text/html; charset=utf-8")
            return
        if u.path == "/osm_scored.geojson":
            self._send_file(os.path.join(VIEWER, "osm_scored.geojson"), "application/geo+json")
            return
        if u.path == "/api/stations":
            self._send_file(os.path.join(ECOBICI, "stations.geojson"), "application/geo+json")
            return
        if u.path == "/api/kpis":
            self._send_file(os.path.join(ECOBICI, "kpis.json"), "application/json")
            return
        if u.path == "/api/crashes":
            # compact crash points for heatmap layer (lon,lat,fatal only, in-memory)
            import csv, io
            rows = []
            with open(os.path.join(DATA, "bike_crashes_ssc.csv"), newline="", encoding="utf-8") as f:
                rdr = csv.DictReader(f)
                for r in rdr:
                    try:
                        lon = float(r["longitud"]); lat = float(r["latitud"])
                    except Exception:
                        continue
                    if not (19.0 <= lat <= 19.7 and -99.4 <= lon <= -98.9):
                        continue
                    fatal = 0
                    try:
                        fatal = int(float(r.get("personas_fallecidas") or 0))
                    except Exception:
                        pass
                    rows.append({"lon": lon, "lat": lat, "fatal": fatal})
            feats = [{"type":"Feature","geometry":{"type":"Point","coordinates":[r["lon"], r["lat"]]},
                      "properties":{"fatal":r["fatal"]}} for r in rows]
            body = json.dumps({"type":"FeatureCollection","features":feats}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/geo+json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "public, max-age=3600")
            self.end_headers()
            self.wfile.write(body)
            return
        if u.path == "/api/route":
            q = parse_qs(u.query)
            try:
                if "start" in q and "end" in q:
                    # expect "lat,lng" (user-friendly order matches Google Maps)
                    start = parse_latlng(q["start"][0])
                    end = parse_latlng(q["end"][0])
                else:
                    raise ValueError("missing start/end")
            except Exception as e:
                self._send_json({"error": f"bad inputs: {e}"}, 400)
                return
            try:
                coords, meta = route_via_brouter(start, end)
            except Exception as e:
                self._send_json({"error": f"routing failed: {e}"}, 502)
                return
            samples = sample_route(coords)
            self._send_json({
                "route_lonlat": coords,
                "samples": samples,
                "summary": summarize(samples, coords),
                "meta": meta,
            })
            return
        if u.path == "/api/score":
            q = parse_qs(u.query)
            poly = q.get("polyline", [None])[0]
            if not poly:
                self._send_json({"error": "missing polyline"}, 400)
                return
            latlngs = decode_polyline(poly)
            coords = [(lng, lat) for lat, lng in latlngs]
            samples = sample_route(coords)
            self._send_json({
                "route_lonlat": coords,
                "samples": samples,
                "summary": summarize(samples, coords),
                "meta": {"source": "encoded_polyline"},
            })
            return
        # fallback static from viewer/
        p = os.path.join(VIEWER, u.path.lstrip("/"))
        if os.path.isfile(p):
            ctype = "text/html; charset=utf-8" if p.endswith(".html") else (
                "application/json" if p.endswith(".json") else "application/octet-stream")
            self._send_file(p, ctype)
            return
        self.send_error(404)


def main(port=8765):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"[boot] listening on http://127.0.0.1:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 8765)
