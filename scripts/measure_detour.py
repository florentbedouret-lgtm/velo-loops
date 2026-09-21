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
    ap.add_argument("--n", type=int, default=600, help="nombre total de paires, réparties entre les zones (0 = tout l'échantillon)")
    ap.add_argument("--neighbours", type=int, default=30, help="départs denses testés vers leur départ voisin hors zone dense (0 = non)")
    ap.add_argument("--pairs-out", default=None, help="JSON des paires mesurées une à une (défaut : à côté de --out)")
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
    per_zone = math.ceil(args.n / 3) if args.n else 10 ** 9
    rows, failed, failed_rows = [], {"dense": 0, "peri": 0, "rural": 0}, []
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
                failed_rows.append({"zone": zone, "kind": s.get("kind", "?"), "crow_km": round(crow, 3)})
                continue
            m.update({"zone": zone, "start_zone": st.get("zone"), "crow_km": crow, "lon": s["lon"], "lat": s["lat"],
                      "kind": s.get("kind", "?")})
            rows.append(m)

    THRESHOLDS = (10, 15, 20, 30, 45, 60)

    def aggregate(sel):
        out = {"pairs": len(sel), "crow_km": stats(sel, "crow_km"), "road_km": stats(sel, "road_km"),
               "ratio_road_over_crow": stats(sel, "ratio"), "time_min_110W": stats(sel, "time_min_110W"),
               "time_min_150W": stats(sel, "time_min_150W"), "speed_kmh_110W": stats(sel, "speed_kmh_110W"),
               "speed_kmh_150W": stats(sel, "speed_kmh_150W"), "lights_per_km": stats(sel, "lights_per_km"),
               "city_share": stats(sel, "city_share"),
               "share_over_minutes_110W": {str(t): round(sum(1 for r in sel if r["time_min_110W"] > t) / len(sel), 3)
                                           for t in THRESHOLDS}}
        return out

    report = {"profile": args.profile, "pairs_requested_per_zone": per_zone if args.n else "tout l'échantillon",
              "failed": failed, "by_zone": {}, "by_start_zone": {}}
    for zone in ("dense", "peri", "rural", "all"):
        sel = rows if zone == "all" else [r for r in rows if r["zone"] == zone]
        if sel:
            report["by_zone"][zone] = aggregate(sel)
    for zone in ("dense", "peri", "rural"):
        sel = [r for r in rows if r.get("start_zone") == zone]
        if sel:
            report["by_start_zone"][zone] = aggregate(sel)
    KIND_GROUP = {"city": "bourgs et villages", "town": "bourgs et villages", "village": "bourgs et villages",
                  "suburb": "bourgs et villages", "hamlet": "hameaux", "isolated_dwelling": "habitations isolées",
                  "neighbourhood": "quartiers et lotissements", "quarter": "quartiers et lotissements",
                  "residential": "zones résidentielles"}
    group = lambda k: KIND_GROUP.get(k, "autres")  # noqa: E731

    def tail_block(sel_rows, sel_failed):
        """Lieux « non couverts » selon la règle de l'UX : sans itinéraire, rapport > 10 ; et lieux à plus de 30 min."""
        total = len(sel_rows) + len(sel_failed)
        over30 = [r for r in sel_rows if r["time_min_110W"] > 30]
        ratio10 = [r for r in sel_rows if r["ratio"] > 10]
        unc = [r for r in sel_rows if r["ratio"] > 10 or r["time_min_110W"] > 30]
        out = {"places_routed_or_failed": total, "no_route": len(sel_failed), "ratio_over_10": len(ratio10),
               "over_30min": len(over30), "over_30min_or_no_route_or_ratio_over_10": len(unc) + len(sel_failed),
               "share_over_30min": round(len(over30) / total, 3) if total else None,
               "share_not_covered_by_ux_rule": round((len(unc) + len(sel_failed)) / total, 3) if total else None,
               "composition_of_over_30min_by_kind_group": {}, "composition_of_not_covered_by_kind_group": {},
               "all_places_by_kind_group": {}}
        for r in over30:
            g_ = group(r["kind"]); out["composition_of_over_30min_by_kind_group"][g_] = out["composition_of_over_30min_by_kind_group"].get(g_, 0) + 1
        for r in unc + sel_failed:
            g_ = group(r["kind"]); out["composition_of_not_covered_by_kind_group"][g_] = out["composition_of_not_covered_by_kind_group"].get(g_, 0) + 1
        for r in sel_rows + sel_failed:
            g_ = group(r["kind"]); out["all_places_by_kind_group"][g_] = out["all_places_by_kind_group"].get(g_, 0) + 1
        return out

    report["tail"] = {"all_zones": tail_block(rows, failed_rows)}
    for zone in ("dense", "peri", "rural"):
        report["tail"][zone] = tail_block([r for r in rows if r["zone"] == zone], [r for r in failed_rows if r["zone"] == zone])
    pairs_path = Path(args.pairs_out) if args.pairs_out else Path(args.out).with_name("detour_pairs.json")
    pairs_path.write_text(json.dumps([{k: (round(v, 3) if isinstance(v, float) else v) for k, v in r.items()}
                                      for r in rows], ensure_ascii=False), encoding="utf-8")

    # départ dense -> départ voisin hors zone dense (règle « meilleur départ à vélo : quelques minutes de plus »)
    report["neighbour_pairs"] = None
    if args.neighbours:
        dense_starts = [x for x in starts if x.get("zone") == "dense"]
        others = [x for x in starts if x.get("zone") in ("peri", "rural")]
        rnd.shuffle(dense_starts)
        nrows, nfailed = [], 0
        for a in dense_starts:
            if len(nrows) + nfailed >= args.neighbours:
                break
            near = [(g.haversine(a["lon"], a["lat"], b["lon"], b["lat"]) / 1000.0, b) for b in others]
            near = sorted((d, b) for d, b in near if 1.0 <= d <= 6.0)[:1]
            if not near:
                continue
            crow, b = near[0]
            path = route(args.gh, (a["lon"], a["lat"]), (b["lon"], b["lat"]), args.profile)
            m = measure(path, crow) if path else None
            if m is None:
                nfailed += 1
                continue
            m.update({"from": a["name"], "to": b["name"], "crow_km": crow})
            nrows.append(m)
        if nrows:
            report["neighbour_pairs"] = {
                "pairs": len(nrows), "failed": nfailed, "crow_km": stats(nrows, "crow_km"), "road_km": stats(nrows, "road_km"),
                "time_min_110W": stats(nrows, "time_min_110W"), "dense_zone_share": stats(nrows, "city_share"),
                "lights_per_km": stats(nrows, "lights_per_km"),
                "share_time_at_most_10min": round(sum(1 for r in nrows if r["time_min_110W"] <= 10) / len(nrows), 3),
                "share_time_at_most_15min": round(sum(1 for r in nrows if r["time_min_110W"] <= 15) / len(nrows), 3),
                "examples": [{"from": r["from"], "to": r["to"], "crow_km": round(r["crow_km"], 1),
                              "time_min_110W": round(r["time_min_110W"]), "dense_zone_share": round(r["city_share"], 2)}
                             for r in nrows[:8]]}

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
    for title, block in (("par zone du LIEU HABITÉ", report["by_zone"]), ("par zone du DÉPART le plus proche", report["by_start_zone"])):
        print(f"\n{title}")
        print(f"{'zone':<7}{'paires':>7}{'vol d’oiseau':>13}{'route':>8}{'rapport':>9}{'p90':>6}{'temps 110 W':>13}{'vitesse':>9}{'>15 min':>9}{'>30 min':>9}")
        for zone, r in block.items():
            def med(k, q="median"):
                return r[k][q] if r.get(k) else "-"
            print(f"{zone:<7}{r['pairs']:>7}{med('crow_km'):>11} km{med('road_km'):>6} km{med('ratio_road_over_crow'):>9}"
                  f"{med('ratio_road_over_crow', 'p90'):>6}{med('time_min_110W'):>10} min{med('speed_kmh_110W'):>9}"
                  f"{round(100 * r['share_over_minutes_110W']['15']):>8}%{round(100 * r['share_over_minutes_110W']['30']):>8}%")
    t = report["tail"]["all_zones"]
    print(f"\nQueue : {t['places_routed_or_failed']} lieux routés ; sans itinéraire {t['no_route']} ; rapport > 10 : {t['ratio_over_10']} ; "
          f"au-delà de 30 min : {t['over_30min']} ({round(100 * (t['share_over_30min'] or 0), 1)} %) ; non couverts selon la règle "
          f"(30 min, sans itinéraire ou rapport > 10) : {t['over_30min_or_no_route_or_ratio_over_10']} "
          f"({round(100 * (t['share_not_covered_by_ux_rule'] or 0), 1)} %)")
    print(f"  composition des lieux > 30 min ou non couverts : {t['composition_of_not_covered_by_kind_group']} "
          f"(parmi tous les lieux : {t['all_places_by_kind_group']})")
    if report["neighbour_pairs"]:
        n = report["neighbour_pairs"]
        print(f"\nDépart dense -> départ voisin hors zone dense ({n['pairs']} paires) : temps médian {n['time_min_110W']['median']} min, "
              f"part en zone dense {n['dense_zone_share']['median']}, <= 10 min : {round(100 * n['share_time_at_most_10min'])} %, "
              f"<= 15 min : {round(100 * n['share_time_at_most_15min'])} %")
    for c in report["cases"]:
        print("Cas", c)
    return 0


if __name__ == "__main__":
    sys.exit(main())
