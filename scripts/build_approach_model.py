#!/usr/bin/env python3
"""
Construit web/approach_model.json (modèle de temps d'approche consommé par le front) à partir de
data/detour_pairs.json et data/detour.json, produits par le mode `detour` du workflow
(scripts/measure_detour.py).

Avant ce script, web/approach_model.json était rempli à la main à partir de ces mêmes fichiers : c'est
cette édition manuelle qui a fait dériver le dépôt du site publié (voir handoff.md, tâche O-8). Ce script
élimine cette étape manuelle : mêmes calculs, reproductibles, à chaque nouvelle mesure `detour`.

Les zones du front (dense/périphérie/rural) sont celles du DÉPART le plus proche (`start_zone` dans les
paires), pas celles du lieu habité mesuré — voir "zone_semantics" dans la sortie. C'est
scripts/measure_detour.py qui a produit `report["by_start_zone"]` avec le même regroupement ; ce script
recalcule les mêmes agrégats à partir des paires brutes pour pouvoir, en plus, les décliner par région
(bbox), ce que measure_detour.py ne fait pas.

Usage :
  python scripts/build_approach_model.py --pairs data/detour_pairs.json --detour data/detour.json \
      --region config/region_barcelona.json --out web/approach_model.json
"""
from __future__ import annotations

import argparse
import json
import statistics
from datetime import date
from pathlib import Path

ZONES = ("dense", "peri", "rural")
THRESHOLDS = (10, 15, 20, 30, 45, 60)

# Régions fixes de la province (indépendantes du scénario de départs) : voir approach_model.json existant.
BBOX_REGIONS = {
    "centre": [1.65, 41.20, 2.65, 41.85],
    "nord_ouest": [1.30, 41.55, 2.60, 42.35],
}

# Total des lieux habités de la province (n de la ligne "all" du rapport `plan`, fixe quel que soit le
# scénario de départs testé) : sert à situer la part de lieux effectivement mesurés (>= 0,6 km de leur
# départ, seuil appliqué par plan_starts.py --demand-out).
TOTAL_DEMAND_POINTS = 7794


def fr_int(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def pctl(values: list[float], p: float) -> float:
    v = sorted(values)
    return v[min(len(v) - 1, int(p / 100 * len(v)))]


def in_bbox(lon: float, lat: float, bbox: list[float]) -> bool:
    lon_min, lat_min, lon_max, lat_max = bbox
    return lon_min <= lon <= lon_max and lat_min <= lat <= lat_max


def zone_stats(rows: list[dict]) -> dict | None:
    if not rows:
        return None
    ratio = [r["ratio"] for r in rows]
    speed110 = [r["speed_kmh_110W"] for r in rows]
    speed150 = [r["speed_kmh_150W"] for r in rows]
    time110 = [r["time_min_110W"] for r in rows]
    lights = [r["lights_per_km"] for r in rows if r.get("lights_per_km") is not None]
    out = {
        "pairs": len(rows),
        "detour_ratio_median": round(statistics.median(ratio), 2),
        "detour_ratio_p90": round(pctl(ratio, 90), 2),
        "speed_kmh_110W_median": round(statistics.median(speed110), 2),
        "speed_kmh_150W_median": round(statistics.median(speed150), 2),
        "time_min_110W_median": round(statistics.median(time110), 2),
        "time_min_110W_p90": round(pctl(time110, 90), 2),
        "lights_per_km_median": round(statistics.median(lights), 2) if lights else 0.0,
        "lights_per_km_p90": round(pctl(lights, 90), 2) if lights else 0.0,
        "share_of_pairs_over_minutes_one_way_110W": {
            str(t): round(sum(1 for r in rows if r["time_min_110W"] > t) / len(rows), 3) for t in THRESHOLDS
        },
    }
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pairs", required=True, help="detour_pairs.json (measure_detour.py --pairs-out)")
    ap.add_argument("--detour", required=True, help="detour.json (measure_detour.py --out), pour les cas nommés")
    ap.add_argument("--region", default="config/region_barcelona.json")
    ap.add_argument("--scenario-label", default=None, help="ex. « 1,75/2,5/2,5 » ; sinon déduit du fichier detour.json si présent")
    ap.add_argument("--out", default="web/approach_model.json")
    args = ap.parse_args()

    pairs_data = json.loads(Path(args.pairs).read_text(encoding="utf-8"))
    detour_data = json.loads(Path(args.detour).read_text(encoding="utf-8"))
    region = json.loads(Path(args.region).read_text(encoding="utf-8"))

    rows = pairs_data["pairs"]
    no_route = pairs_data.get("no_route", [])
    total_pairs = len(rows) + len(no_route)
    share_measured = round(len(rows) / TOTAL_DEMAND_POINTS, 3) if TOTAL_DEMAND_POINTS else None

    zones = {}
    for z in ZONES:
        sel = [r for r in rows if r.get("start_zone") == z]
        s = zone_stats(sel)
        if s:
            zones[z] = s

    regions = []
    for region_id, bbox in BBOX_REGIONS.items():
        region_zones = {}
        for z in ZONES:
            sel = [r for r in rows if r.get("start_zone") == z and in_bbox(r["lon"], r["lat"], bbox)]
            s = zone_stats(sel)
            if s:
                region_zones[z] = s
        if region_zones:
            regions.append({"id": region_id, "bbox": bbox, "zones": region_zones})

    case = next((c for c in detour_data.get("cases", []) if c.get("case") == "Barcelona>Montcada i Reixac"), None)
    case_out = None
    if case and "error" not in case:
        case_out = {
            "crow_km": case.get("crow_km"),
            "road_km": case.get("road_km"),
            "detour_ratio": case.get("ratio"),
            "dense_zone_share": case.get("city_share"),
            "urban_share": case.get("urban_share"),
            "traffic_lights": case.get("lights"),
            "time_min_110W": case.get("time_min_110W"),
            "time_min_150W": case.get("time_min_150W"),
            "status": "mesuré",
        }

    scenario_label = args.scenario_label or "voir data/detour.json"
    today = date.today().isoformat()

    out = {
        "version": "2.3",
        "versioning": "Chaîne « majeur.mineur ». Le mineur ajoute des champs ou change des valeurs (le front "
                       "ignore les champs inconnus). Le majeur casse la structure : le front doit refuser un "
                       "majeur qu'il ne connaît pas et n'afficher aucun temps d'approche chiffré.",
        "measured_on": today,
        "status": f"mesuré : détour exhaustif de la province ({fr_int(total_pairs)} itinéraires calculés, "
                   f"{fr_int(len(no_route))} sans itinéraire) — généré par scripts/build_approach_model.py",
        "zone_key": "start",
        "zone_semantics": "Zones du DÉPART PRÉ-CALCULÉ le plus proche (celle que le front connaît). Chaque "
                           "lieu habité est associé à son départ le plus proche ; la zone est celle de ce "
                           "départ. Le mélange des zones des lieux est donc déjà inclus.",
        "plan": f"scénario {scenario_label} km (dense/périphérie/rural), sans habitations isolées",
        "source": f"mode detour, lieux habités à couvrir situés à 0,6 km ou plus de leur départ le plus proche "
                   f"({round(100 * share_measured)} % des {fr_int(TOTAL_DEMAND_POINTS)} lieux de la province) : "
                   f"itinéraires GraphHopper (profil 'approach') vers ce départ ; "
                   f"temps = modèle physique à 110 W + 10 s par feu OSM",
        "sample_scope": "Les lieux à moins de 0,6 km d'un départ ne sont pas dans les mesures (quelques "
                         "minutes d'approche). Les parts au-delà d'un temps sont des parts des lieux à "
                         "0,6 km ou plus.",
        "bbox_order": "lon_min, lat_min, lon_max, lat_max",
        "bbox_union": region.get("bbox"),
        "unmeasured_bboxes": [],
        "bbox_regions": BBOX_REGIONS,
        "region_rule": "Pour un point (départ le plus proche) : 1) hors de bbox_union, ou dans une des "
                        "unmeasured_bboxes : aucun chiffre ; 2) dans une seule des régions : utiliser sa zone "
                        "(regions[i].zones) ; 3) dans plusieurs régions (recouvrement) : utiliser les zones "
                        "poolées (zones). Les zones poolées restent le repli.",
        "formula": "temps d'approche à l'aller (min) = distance à vol d'oiseau (km) × detour_ratio_median ÷ "
                    "speed_kmh_110W_median × 60 ; borne pessimiste : detour_ratio_p90 ; aller-retour = 2 × aller",
        "precision": "Province entière : " + ", ".join(
            f"{fr_int(zones[z]['pairs'])} ({z})" for z in ZONES if z in zones
        ) + " lieux par zone du départ.",
        "zones": zones,
        "regions": regions,
    }
    if case_out:
        out["case_barcelona_to_montcada"] = case_out

    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Écrit {args.out} : {total_pairs} paires ({len(no_route)} sans itinéraire), "
          f"zones {list(zones)}, {len(regions)} région(s).")
    for z, s in zones.items():
        print(f"  {z:<7} n={s['pairs']:<5} ratio med={s['detour_ratio_median']} vitesse med={s['speed_kmh_110W_median']} km/h "
              f"temps med={s['time_min_110W_median']} min")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
