"""Fetch the OSM bike-usable road network for a CDMX bbox via Overpass (osmnx).

Defaults to the 9×11 km downtown box (Cuauhtémoc + Roma + Condesa + Juárez + Centro).
Takes 8–12 minutes via the public Overpass endpoint; needs ~2 GB RAM during build.

Override the bbox with env vars CDMX_BBOX="west,south,east,north".

Output:
  data/cycling/osm_bike_network_downtown.geojson
"""
from __future__ import annotations
import os
from pathlib import Path

import osmnx as ox

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "cycling" / "osm_bike_network_downtown.geojson"

DEFAULT_BBOX = (-99.22, 19.35, -99.13, 19.46)  # west, south, east, north


def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    if OUT.exists():
        print(f"{OUT.name} already exists — delete to refetch.")
        return
    bbox_env = os.environ.get("CDMX_BBOX")
    if bbox_env:
        bbox = tuple(float(x) for x in bbox_env.split(","))
    else:
        bbox = DEFAULT_BBOX
    print(f"fetching bike network for bbox {bbox} (can take 8–12 min)")
    G = ox.graph_from_bbox(bbox=bbox, network_type="bike", simplify=True)
    edges = ox.graph_to_gdfs(G, nodes=False, edges=True).reset_index()
    edges["highway"] = edges["highway"].apply(lambda v: v[0] if isinstance(v, list) else v)
    keep = ["u", "v", "key", "osmid", "highway", "name", "length", "geometry"]
    edges = edges[[c for c in keep if c in edges.columns]].copy()
    edges["osmid"] = edges["osmid"].astype(str)
    edges["name"] = edges["name"].astype(str)
    edges.to_file(OUT, driver="GeoJSON")
    print(f"wrote {OUT.name}: {len(edges)} edges, {edges['length'].sum()/1000:.0f} km")


if __name__ == "__main__":
    main()
