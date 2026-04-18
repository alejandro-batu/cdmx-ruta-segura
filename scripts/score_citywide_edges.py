"""One-shot: score the 45k-edge downtown OSM network against the full risk surface.

Uses the same scoring logic as the server (motor-vehicle blend, infra + station
multipliers, score caps) so the map overlay matches the per-route score.

Output: viewer/osm_scored.geojson
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

import time
import numpy as np
import geopandas as gpd
from shapely.geometry import Point

from route_score_server import (
    SURFACE, INFRA, STATIONS,
    INFRA_BASE_MULT, NO_INFRA_PENALTY,
    STATION_NEAR_M, STATION_FAR_M, STATION_FAR_PENALTY,
    MOTOR_BLEND_WEIGHT,
    SCORE_CAP_NO_INFRA, SCORE_CAP_NO_INFRA_FAR_FROM_STATION, SCORE_CAP_FAR_FROM_STATION_WITH_INFRA,
    INFRA_MATCH_M, METRIC_CRS, WGS84,
    _station_mult_from_dist,
)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.environ.get("CDMX_OSM_NETWORK", os.path.join(_ROOT, "data", "cycling", "osm_bike_network_downtown.geojson"))
OUT = os.environ.get("CDMX_OSM_SCORED", os.path.join(_ROOT, "viewer", "osm_scored.geojson"))


def main():
    t0 = time.time()
    edges = gpd.read_file(SRC).to_crs(METRIC_CRS)
    print(f"[load] {len(edges)} edges ({time.time()-t0:.1f}s)", flush=True)

    mids = edges.geometry.interpolate(0.5, normalized=True)
    xs = np.array([p.x for p in mids])
    ys = np.array([p.y for p in mids])

    t = time.time()
    risk_cyc = SURFACE["kde_cyclist"](np.vstack([xs, ys])) / max(SURFACE["norm_cyclist"], 1e-30)
    print(f"[kde_cyclist] {time.time()-t:.1f}s", flush=True)
    t = time.time()
    risk_motor = SURFACE["kde_motor"](np.vstack([xs, ys])) / max(SURFACE["norm_motor"], 1e-30)
    print(f"[kde_motor] {time.time()-t:.1f}s", flush=True)
    risk_norm = (1 - MOTOR_BLEND_WEIGHT) * risk_cyc + MOTOR_BLEND_WEIGHT * risk_motor

    mid_gdf = gpd.GeoDataFrame({"_row": np.arange(len(mids))},
                                geometry=[Point(x, y) for x, y in zip(xs, ys)],
                                crs=METRIC_CRS)
    infra_join = gpd.sjoin_nearest(mid_gdf, INFRA, how="left", max_distance=INFRA_MATCH_M)
    infra_join = infra_join[~infra_join.index.duplicated(keep="first")]
    infra_tipo = infra_join["TIPO_IC"].values
    infra_mult = np.array([
        INFRA_BASE_MULT.get(t, NO_INFRA_PENALTY) if isinstance(t, str) else NO_INFRA_PENALTY
        for t in infra_tipo
    ])

    sta_join = gpd.sjoin_nearest(mid_gdf, STATIONS, how="left", distance_col="_station_dist")
    sta_join = sta_join[~sta_join.index.duplicated(keep="first")]
    station_dist = sta_join["_station_dist"].to_numpy(dtype=float)
    station_mult = _station_mult_from_dist(station_dist)

    total_mult = infra_mult * station_mult
    weighted = risk_norm * total_mult
    score_raw = 100 * (1 - np.clip(weighted, 0, 1))

    has_infra_arr = np.array([isinstance(t, str) for t in infra_tipo])
    far_from_station = np.where(np.isnan(station_dist), True, station_dist > STATION_FAR_M)

    cap = np.full_like(score_raw, 100.0)
    cap = np.where(has_infra_arr & far_from_station, np.minimum(cap, SCORE_CAP_FAR_FROM_STATION_WITH_INFRA), cap)
    cap = np.where(~has_infra_arr & ~far_from_station, np.minimum(cap, SCORE_CAP_NO_INFRA), cap)
    cap = np.where(~has_infra_arr & far_from_station, np.minimum(cap, SCORE_CAP_NO_INFRA_FAR_FROM_STATION), cap)
    score = np.minimum(score_raw, cap).round(1)

    edges = edges.to_crs(WGS84)
    edges["safety_score"] = score
    edges["has_infra"] = has_infra_arr
    edges["infra_mult"] = infra_mult
    edges["station_mult"] = station_mult
    edges["risk_cyclist"] = risk_cyc
    edges["risk_motor"] = risk_motor

    # drop non-essential osmnx cols to keep the file lean
    keep = ["osmid", "highway", "name", "length", "safety_score", "has_infra", "infra_mult", "station_mult", "risk_cyclist", "risk_motor", "geometry"]
    edges = edges[[c for c in keep if c in edges.columns]].copy()
    edges["osmid"] = edges["osmid"].astype(str)
    edges["name"] = edges["name"].astype(str)

    # km by band
    import pandas as pd
    band = pd.cut(edges["safety_score"], [-0.1, 25, 50, 75, 101], labels=["dangerous", "risky", "ok", "safe"])
    km = edges.groupby(band, observed=True)["length"].sum().div(1000).round(1)
    print(f"[scored] km by band: {km.to_dict()}")

    edges.to_file(OUT, driver="GeoJSON")
    print(f"[wrote] {OUT} ({len(edges)} edges)")


if __name__ == "__main__":
    main()
