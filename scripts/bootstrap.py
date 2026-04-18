"""Download and prepare all open data needed by the route scorer.

Idempotent: skips downloads whose output already exists. Run once after cloning.

Produces (under data/):
  cycling/bike_crashes_ssc.csv                cyclist-involved crashes 2018–2024
  cycling/accidentes_shp/…                    FGJ 2019 cyclist points (SHP)
  cycling/infra_full_wgs84.geojson            bike infrastructure, reprojected
  cycling/hechos_transito_2023.csv            all-vehicle events 2018–2023
  cycling/hechos_transito_2024.csv            all-vehicle events 2024
  cycling/hechos_transito_vehiculos_*.csv     vehicle-type joins (for bike filter)
  ecobici/stations.geojson                    677 active Ecobici stations
  ecobici/kpis.json                           system-level trip totals
"""
from __future__ import annotations
import json, os, sys, zipfile
from pathlib import Path
from urllib.request import urlretrieve

import pandas as pd
import geopandas as gpd

ROOT = Path(__file__).resolve().parent.parent
CYC = ROOT / "data" / "cycling"
ECO = ROOT / "data" / "ecobici"
CYC.mkdir(parents=True, exist_ok=True)
ECO.mkdir(parents=True, exist_ok=True)


RESOURCES = {
    # hechos de tránsito (SSC) — bike-involved filter uses these
    "hechos_transito_2024.csv": "https://datos.cdmx.gob.mx/dataset/e5a4b451-46c8-4912-825d-3ceb60487e68/resource/27b88c58-e741-4526-bfd0-5200fe272f8d/download/nuevo_acumulado_hechos_de_transito_2024.csv",
    "hechos_transito_vehiculos_2024.csv": "https://datos.cdmx.gob.mx/dataset/e5a4b451-46c8-4912-825d-3ceb60487e68/resource/d377236d-d2b7-4a14-aa65-88dca057af3a/download/nuevo_acumulado_ht_vehiculos_incolucrados_2024.csv",
    "hechos_transito_2023.csv": "https://datos.cdmx.gob.mx/dataset/a9afdb1b-80ed-4f6c-a34a-0200211e527e/resource/0555dd20-d921-4f76-aa8c-1a0689f48bce/download/nuevo_acumulado_hechos_de_transito_2023_12.csv",
    "hechos_transito_vehiculos_2023.csv": "https://datos.cdmx.gob.mx/dataset/a9afdb1b-80ed-4f6c-a34a-0200211e527e/resource/13e41d24-3844-4eb3-9550-f77fa5149a12/download/nuevo_acumulado_ht_vehiculos_incolucrados_2023_12.csv",
    # FGJ 2019 cyclist accidents (SHP)
    "accidentes_ciclistas.zip": "https://datos.cdmx.gob.mx/dataset/7abe3ede-e745-40ce-8998-7a4d8093ab03/resource/4ace8607-65d2-46dc-9df9-73310a9668bf/download/puntos-de-accidentes-de-ciclistas.zip",
    # bike infrastructure (SHP bundle)
    "infra_vial_ciclista.zip": "https://datos.cdmx.gob.mx/dataset/7a017dd2-0dec-44f2-b550-10af1a6ee120/resource/6e541083-1399-4c14-a210-0493167c7b16/download/infraestructura_vial_ciclista.zip",
}

ECOBICI_RESOURCES = {
    "stations.csv": "https://datos.cdmx.gob.mx/dataset/a1d7c132-fb1b-4e8c-bb74-4bb618563eb2/resource/5fbacfcc-f677-406c-9356-6ced541240fe/download/cicloestaciones_ecobici.csv",
    "afluencia_daily.csv": "https://datos.cdmx.gob.mx/dataset/7f67dc90-f1a3-457d-a4cb-bc76c43aca61/resource/4df66c20-f969-4b3e-9bce-34987da33bc1/download/afluencia_simple_acumulada_2024_07_.csv",
}


def fetch(path: Path, url: str):
    if path.exists():
        print(f"  • {path.name} (already present)")
        return
    print(f"  ↓ {path.name}")
    urlretrieve(url, path)


def download_all():
    print("[1/4] downloading SSC + FGJ + infra archives")
    for name, url in RESOURCES.items():
        fetch(CYC / name, url)
    print("[1/4b] downloading Ecobici")
    for name, url in ECOBICI_RESOURCES.items():
        fetch(ECO / name, url)


def unzip_if_needed(zip_path: Path, extract_dir: Path, marker_name: str):
    if (extract_dir / marker_name).exists():
        return
    extract_dir.mkdir(parents=True, exist_ok=True)
    print(f"  ↯ unzip {zip_path.name}")
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(extract_dir)


def prepare_bike_crashes():
    out = CYC / "bike_crashes_ssc.csv"
    if out.exists():
        print(f"  • bike_crashes_ssc.csv (already present)")
        return
    print("[2/4] filtering SSC crashes to bike-involved events")
    v23 = pd.read_csv(CYC / "hechos_transito_vehiculos_2023.csv", low_memory=False)
    v24 = pd.read_csv(CYC / "hechos_transito_vehiculos_2024.csv", low_memory=False)
    h23 = pd.read_csv(CYC / "hechos_transito_2023.csv", low_memory=False)
    h24 = pd.read_csv(CYC / "hechos_transito_2024.csv", low_memory=False)
    for df in (h23, h24):
        df["ts"] = pd.to_datetime(df["fecha_evento"], errors="coerce", dayfirst=True)
        df["year"] = df["ts"].dt.year
    bike_folios = (set(v23.loc[v23["tipo_vehiculo"] == "BICICLETA", "folio"])
                 | set(v24.loc[v24["tipo_vehiculo"] == "BICICLETA", "folio"]))
    bikes = pd.concat([h23[h23["folio"].isin(bike_folios)],
                       h24[h24["folio"].isin(bike_folios)]], ignore_index=True)
    bikes = bikes.drop_duplicates(subset=["folio"])
    bikes["latitud"] = pd.to_numeric(bikes["latitud"], errors="coerce")
    bikes["longitud"] = pd.to_numeric(bikes["longitud"], errors="coerce")
    bikes = bikes.dropna(subset=["latitud", "longitud"])
    bikes = bikes[bikes["latitud"].between(19.0, 19.7) & bikes["longitud"].between(-99.4, -98.9)]
    bikes.to_csv(out, index=False)
    print(f"    wrote {out.name}: {len(bikes)} cyclist-involved events")


def prepare_shp_layers():
    print("[3/4] unzip SHPs and reproject bike infra")
    unzip_if_needed(CYC / "accidentes_ciclistas.zip", CYC / "accidentes_shp",
                    "Puntos de accidentes de Ciclistas")
    unzip_if_needed(CYC / "infra_vial_ciclista.zip", CYC / "infra_shp",
                    "infraestructura_vial_ciclista")
    out = CYC / "infra_full_wgs84.geojson"
    if not out.exists():
        infra = gpd.read_file(CYC / "infra_shp" / "infraestructura_vial_ciclista" /
                               "Infraestructura ciclista total.shp")
        infra = infra.to_crs("EPSG:4326")
        infra.to_file(out, driver="GeoJSON")
        print(f"    wrote {out.name}: {len(infra)} tramos")


def prepare_ecobici():
    print("[4/4] build Ecobici stations.geojson + kpis.json")
    stations_geo = ECO / "stations.geojson"
    if not stations_geo.exists():
        s = pd.read_csv(ECO / "stations.csv", encoding="latin-1")
        s_active = s[s["estatus"] == "Instalada"].copy()
        feats = [{
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [float(r["longitud"]), float(r["latitud"])]},
            "properties": {"id": str(r["num_cicloe"]), "calle_prin": r["calle_prin"],
                           "calle_secu": r["calle_secu"], "colonia": r["colonia"],
                           "alcaldia": r["alcaldia"]},
        } for _, r in s_active.iterrows()]
        stations_geo.write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
        print(f"    wrote stations.geojson: {len(feats)} active stations")

    kpis_path = ECO / "kpis.json"
    if not kpis_path.exists():
        s = pd.read_csv(ECO / "stations.csv", encoding="latin-1")
        a = pd.read_csv(ECO / "afluencia_daily.csv", encoding="latin-1")
        a["fecha"] = pd.to_datetime(a["fecha"])
        recent = a[a["fecha"] >= a["fecha"].max() - pd.Timedelta(days=30)]
        kpis = {
            "total_trips_all_time": int(a["viajes"].sum()),
            "by_year": {int(y): int(v) for y, v in a.groupby(a["fecha"].dt.year)["viajes"].sum().items()},
            "by_gender": {k: int(v) for k, v in a.groupby("genero")["viajes"].sum().items()},
            "date_first": str(a["fecha"].min().date()),
            "date_last": str(a["fecha"].max().date()),
            "trips_last_30_days": int(recent["viajes"].sum()),
            "avg_daily_last_30": int(recent.groupby("fecha")["viajes"].sum().mean()),
            "active_stations": int((s["estatus"] == "Instalada").sum()),
            "stations_total": int(len(s)),
        }
        kpis_path.write_text(json.dumps(kpis, indent=2))
        print(f"    wrote kpis.json")


def main():
    download_all()
    prepare_bike_crashes()
    prepare_shp_layers()
    prepare_ecobici()
    print("\n✓ ready. Run: python scripts/route_score_server.py")
    print("  then open http://localhost:8765")


if __name__ == "__main__":
    main()
