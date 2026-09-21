#!/usr/bin/env python3
"""
Mesure le DÉTOUR et la VITESSE D'APPROCHE réels entre des lieux habités et leur départ le plus proche.

Pour un échantillon de paires « lieu habité → départ le plus proche » (produit par plan_starts.py --demand-out),
ce script demande à GraphHopper (profil vélo `approach`) l'itinéraire et calcule, par type de zone :
  - le rapport distance par la route / distance à vol d'oiseau ;
  - le temps d'approche (même modèle physique que les boucles : puissance, pente, feux tricolores) ;
  - le nombre de feux par km et la part de zone urbaine dense traversée.
Cas nommés (--cases "Barcelona>Montcada i Reixac") : mêmes mesures pour un trajet précis entre deux départs.

Nécessite un serveur GraphHopper en marche (voir le workflow).
"""
from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
import generate_loops as g  # noqa: E402


def route(gh_url, a, b, profile):
    body = {"points": [[a[0], a[1]], [b[0], b[1]]], "profile": profile, "points_encoded": False, "elevation": True,
            "instructions": False, "details": ["urban_density"]}
    try:
        r = requests.post(f"{gh_url}/route", json=body, timeout=60)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    paths = r.json().get("paths") or []
    return paths[0] if paths else None


def measure(path, crow_km, watts_list=(110, 150)):
    coords = path["points"]["coordinates"]
    cum = [0.0]
    for i in range(1, len(coords)):
        cum.append(cum[-1] + g.haversine(coords[i - 1][0], coords[i - 1][1], coords[i][0], coords[i][1]))
    total = cum[-1]
    if total < 50:
        return None
    urban = g.meters_by_value(path.get("details", {}).get("urban_density"), cum)
    city, resid = urban.get("city", 0.0) / total, urban.get("residential", 0.0) / total
    lights = g.SIGNALS.count_along(coords, cum) if g.SIGNALS is not None else None
    ds, prof = g.elevation_profile(coords, cum)
    out = {"road_km": total / 1000.0, "ratio": (total / 1000.0) / max(crow_km, 0.05), "city_share": city,
           "urban_share": city + resid, "lights": lights,
           "lights_per_km": None if lights is None else lights / (total / 1000.0)}
    for w in watts_list:
        t = g.estimate_time_s(ds, prof, w, city, resid, lights)
        out[f"time_min_{w}W"] = t / 60.0
        out[f"speed_kmh_{w}W"] = (total / 1000.0) / (t / 3600.0)
    return out


def pctl(v, p):
    v = sorted(v)
    return v[min(len(v) - 1, int(p / 100 * len(v)))]


def stats(rows, key):
    v = [r[key] for r in rows if r.get(key) is not None]
    if not v:
        return None
    return {"median": round(statistics.median(v), 2), "p90": round(pctl(v, 90), 2), "max": round(max(v), 2)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gh", default="http://localhost:8989")
    ap.add_argument("--sample", required=True, help="demand_sample.json (plan_starts.py --demand-out)")
    ap.add_argument("--starts", required=True, help="starts_plan.json (plan_starts.py --out)")
    ap.add_argument("--pbf", default=None, help=".pbf de la région (comptage des feux)")
    ap.add_argument("--n", type=int, default=200, help="nombre total de paires, réparties entre les zones")
    ap.add_argument("--profile", default="approach")
    ap.add_argument("--places", default=None, help="places.geojson : repli pour nommer un cas hors du plan")
    ap.add_argument("--cases", default="", help="cas nommés « Départ A>Départ B;… »")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    sample = json.loads(Path(args.sample).read_text(encoding="utf-8"))
    starts = json.loads(Path(args.starts).read_text(encoding="utf-8"))
    workdir = Path(args.sample).parent
    pbf = Path(args.pbf) if args.pbf else workdir / "region.osm.pbf"
    g.SIGNALS = g.load_signals(pbf, workdir)
    print(f"Feux tricolores OSM chargés : {len(g.SIGNALS.points) if g.SIGNALS else 0}", flush=True)

    rnd = random.Random(11)
    per_zone = math.ceil(args.n / 3)
    rows, failed = [], {"dense": 0, "peri": 0, "rural": 0}
    for zone in ("dense", "peri", "rural"):
        pairs = [s for s in sample if s["zone"] == zone]
        rnd.shuffle(pairs)
        for s in pairs[:per_zone]:
            st = starts[s["start"]]
            path = route(args.gh, (s["lon"], s["lat"]), (st["lon"], st["lat"]), args.profile)
            crow = g.haversine(s["lon"], s["lat"], st["lon"], st["lat"]) / 1000.0
            m = measure(path, crow) if path else None
            if m is None:
                failed[zone] += 1
                continue
            m.update({"zone": zone, "crow_km": crow})
            rows.append(m)

    report = {"profile": args.profile, "pairs_requested_per_zone": per_zone, "failed": failed, "by_zone": {}}
    for zone in ("dense", "peri", "rural", "all"):
        sel = rows if zone == "all" else [r for r in rows if r["zone"] == zone]
        if not sel:
            continue
        report["by_zone"][zone] = {
            "pairs": len(sel),
            "crow_km": stats(sel, "crow_km"), "road_km": stats(sel, "road_km"), "ratio_road_over_crow": stats(sel, "ratio"),
            "time_min_110W": stats(sel, "time_min_110W"), "time_min_150W": stats(sel, "time_min_150W"),
            "speed_kmh_110W": stats(sel, "speed_kmh_110W"), "speed_kmh_150W": stats(sel, "speed_kmh_150W"),
            "lights_per_km": stats(sel, "lights_per_km"), "city_share": stats(sel, "city_share"),
        }

    report["cases"] = []
    by_name = {s["name"]: s for s in starts}
    places_path = Path(args.places) if args.places else workdir / "places.geojson"
    if places_path.exists():                       # repli : un lieu OSM nommé qui n'est pas un départ du plan
        for f in json.loads(places_path.read_text(encoding="utf-8")).get("features", []):
            nm, geo = f.get("properties", {}).get("name"), f.get("geometry", {})
            if nm and geo.get("type") == "Point" and nm not in by_name:
                by_name[nm] = {"name": nm, "lon": geo["coordinates"][0], "lat": geo["coordinates"][1]}
    for case in [c for c in args.cases.split(";") if c.strip()]:
        a_name, b_name = (x.strip() for x in case.split(">"))
        if a_name not in by_name or b_name not in by_name:
            report["cases"].append({"case": case, "error": "départ introuvable dans le plan"})
            continue
        a, b = by_name[a_name], by_name[b_name]
        path = route(args.gh, (a["lon"], a["lat"]), (b["lon"], b["lat"]), args.profile)
        crow = g.haversine(a["lon"], a["lat"], b["lon"], b["lat"]) / 1000.0
        m = measure(path, crow) if path else None
        report["cases"].append({"case": case, "crow_km": round(crow, 1),
                                **({k: (round(v, 2) if isinstance(v, float) else v) for k, v in m.items()} if m
                                   else {"error": "pas d'itinéraire"})})
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"\nDétour et vitesse d'approche (profil {args.profile}) — paires échouées : {failed}")
    print(f"{'zone':<7}{'paires':>7}{'vol d’oiseau':>13}{'route':>8}{'rapport':>9}{'temps 110 W':>13}{'vitesse 110 W':>15}{'feux/km':>9}")
    for zone, r in report["by_zone"].items():
        def med(k):
            return r[k]["median"] if r.get(k) else "-"
        print(f"{zone:<7}{r['pairs']:>7}{med('crow_km'):>11} km{med('road_km'):>6} km{med('ratio_road_over_crow'):>9}"
              f"{med('time_min_110W'):>10} min{med('speed_kmh_110W'):>11} km/h{med('lights_per_km'):>9}")
    for c in report["cases"]:
        print("Cas", c)
    return 0


if __name__ == "__main__":
    sys.exit(main())
