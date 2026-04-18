"""Per-segment cycling safety score for an OSM network.

Inputs (all WGS84):
  - osm_bike_network_demo.geojson     OSM bike-usable edges for the demo bbox
  - ssc_bike_crashes_demo.geojson     cyclist-involved crashes 2018-2024 (SSC)
  - accidentes_demo.geojson           2019 cyclist accidents (FGJ-linked SHP)
  - infra_demo.geojson                CDMX bike infrastructure with TIPO_IC + ESTADO

Output:
  - osm_scored.geojson                OSM edges with risk, infra_mult, safety_score

Score model (v1):
  raw_risk       = KDE of crash points sampled at the segment midpoint
                   + fatality_weight * KDE of fatal points
  infra_mult     = lookup by (TIPO_IC, ESTADO) matched to the segment (nearest within 10 m)
                   protected lanes < mixed traffic < no infra
  safety_score   = 100 * (1 - clip(raw_risk * infra_mult / P95, 0, 1))
                   normalized so the P95 worst edge ≈ 0, clean edges ≈ 100

Caveats:
  - No exposure normalization (no ridership data). Quiet dangerous streets
    underweighted vs busy safer ones.
  - Crash points geocoded to nearest corner by SSC, not exact impact spot.
  - KDE bandwidth 150 m chosen by eye; tune with cross-validation later.
"""
from __future__ import annotations
import os
import geopandas as gpd
import numpy as np
import pandas as pd
from scipy.stats import gaussian_kde
from shapely.geometry import Point

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.environ.get("CDMX_DATA_CYCLING", os.path.join(_ROOT, "data", "cycling"))

INFRA_BASE_MULT = {
    "Ciclovia":                     0.55,
    "Ciclovia bidireccional":       0.55,
    "Carril bus bici":              0.75,
    "Ciclocarril":                  0.80,
    "Carril de prioridad ciclista": 0.85,
    "Infraestructura ciclista emergente": 0.90,
    "Sendero compartido":           0.70,
    None:                           1.00,
}
ESTADO_PENALTY = {
    "En operacion":            1.00,
    "Requiere mantenimiento":  1.10,
    "Fuera de servicio":       1.00,
}
FATALITY_WEIGHT = 5.0
KDE_BW_METERS = 150.0
INFRA_MATCH_BUFFER_M = 10.0

# metric CRS for CDMX (UTM 14N)
METRIC_CRS = "EPSG:32614"
WGS84 = "EPSG:4326"


def load_crashes() -> gpd.GeoDataFrame:
    ssc = gpd.read_file(f"{DATA}/ssc_bike_crashes_demo.geojson")
    ssc["fatal"] = ssc["personas_fallecidas"].fillna(0).astype(float).clip(0, 5)
    ssc = ssc[["geometry", "fatal"]].copy()
    ssc["source"] = "ssc"
    try:
        acc = gpd.read_file(f"{DATA}/accidentes_demo.geojson")
        acc["fatal"] = acc["total_occi"].fillna(0).astype(float).clip(0, 5)
        acc = acc[["geometry", "fatal"]].copy()
        acc["source"] = "fgj2019"
        crashes = pd.concat([ssc, acc], ignore_index=True)
    except Exception:
        crashes = ssc
    return gpd.GeoDataFrame(crashes, geometry="geometry", crs=WGS84)


def build_kde(crashes_m: gpd.GeoDataFrame, bw_m: float) -> gaussian_kde:
    xs = crashes_m.geometry.x.values
    ys = crashes_m.geometry.y.values
    weights = 1.0 + FATALITY_WEIGHT * crashes_m["fatal"].values
    pts = np.vstack([xs, ys])
    # bw_method is a factor on data std; translate a target bandwidth in meters
    std = np.sqrt((xs.std() ** 2 + ys.std() ** 2) / 2.0)
    factor = bw_m / std if std > 0 else 0.05
    return gaussian_kde(pts, weights=weights, bw_method=factor)


def match_infra_to_edges(edges_m: gpd.GeoDataFrame, infra_m: gpd.GeoDataFrame) -> pd.Series:
    """Return series aligned to edges_m.index of infra_mult values."""
    if infra_m.empty:
        return pd.Series(1.0, index=edges_m.index)
    # buffer infra by matching distance, take nearest
    infra_m = infra_m[["TIPO_IC", "ESTADO", "geometry"]].copy()
    joined = gpd.sjoin_nearest(edges_m, infra_m, how="left", max_distance=INFRA_MATCH_BUFFER_M)
    # sjoin_nearest can create duplicates (one per nearest infra); dedupe by edge index
    joined = joined[~joined.index.duplicated(keep="first")]
    base = joined["TIPO_IC"].map(INFRA_BASE_MULT).fillna(INFRA_BASE_MULT[None])
    penalty = joined["ESTADO"].map(ESTADO_PENALTY).fillna(1.0)
    mult = base * penalty
    # guarantee alignment
    return mult.reindex(edges_m.index).fillna(INFRA_BASE_MULT[None])


def score_edges():
    edges = gpd.read_file(f"{DATA}/osm_bike_network_demo.geojson").to_crs(METRIC_CRS)
    crashes = load_crashes().to_crs(METRIC_CRS)
    infra = gpd.read_file(f"{DATA}/infra_demo.geojson").to_crs(METRIC_CRS)
    print(f"edges={len(edges)} crashes={len(crashes)} infra={len(infra)}")

    kde = build_kde(crashes, KDE_BW_METERS)

    # sample KDE at segment midpoints (cheap proxy for length-weighted mean)
    mids = edges.geometry.interpolate(0.5, normalized=True)
    sample = kde(np.vstack([mids.x.values, mids.y.values]))
    edges["raw_risk"] = sample

    edges["infra_mult"] = match_infra_to_edges(edges, infra).values
    edges["weighted_risk"] = edges["raw_risk"] * edges["infra_mult"]

    p95 = np.percentile(edges["weighted_risk"], 95)
    norm = np.clip(edges["weighted_risk"] / p95, 0, 1)
    edges["safety_score"] = (100 * (1 - norm)).round(1)

    out = edges.to_crs(WGS84).drop(columns=["u", "v", "key"], errors="ignore")
    out_path = f"{DATA}/osm_scored.geojson"
    out.to_file(out_path, driver="GeoJSON")
    print(f"wrote {out_path}")
    return edges


def summarise(edges: gpd.GeoDataFrame) -> None:
    edges = edges.copy()
    edges["band"] = pd.cut(
        edges["safety_score"],
        [-0.1, 25, 50, 75, 101],
        labels=["dangerous", "risky", "ok", "safe"],
    )
    print("\n=== score distribution (km by band) ===")
    km_by_band = edges.groupby("band", observed=True)["length"].sum().div(1000).round(1)
    print(km_by_band.to_dict())
    print("\n=== top 10 most dangerous segments ===")
    top = edges.sort_values("safety_score").head(10)
    cols = ["name", "highway", "length", "infra_mult", "raw_risk", "safety_score"]
    print(top[cols].to_string())


if __name__ == "__main__":
    edges = score_edges()
    summarise(edges)
