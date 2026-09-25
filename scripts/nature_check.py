#!/usr/bin/env python3
"""
Diagnostic O-12 (handoff.md) : les boucles des départs de centre-ville rejoignent-elles la nature ?
Ne publie rien. Réutilise le code de production de generate_loops.py (fit_and_sample, analyse, score, pick_options,
paysage, feux) : les chiffres de la variante A sont ceux que produirait la génération réelle.

Pour des départs en zone dense proches d'un grand espace vert (forêt ou espace protégé d'au moins ~0,5 km²), et pour
chaque durée et niveau, on compare la meilleure boucle (« équilibrée ») obtenue par 4 variantes :
  A    production actuelle : 10 candidats round_trip (2 seeds + 8 caps)
  A+B  A + 4 candidats « vers le vert » : triangle départ -> P1 -> P2 -> départ, P1 dans la direction de l'entrée de
       l'espace vert le plus proche (au-delà de cette entrée), P2 décalé de ±40° ou ±70°
  C    A en neutralisant le frein aux montées de bike_elevation.json (modèle personnalisé ajouté à la requête) ;
       si GraphHopper refuse ce modèle, la variante est marquée « refusée » avec le message d'erreur
  C+B  C + les candidats « vers le vert » calculés avec le même modèle
Critères comparés : note (score de production), part en ville, km de sortie de la zone dense, part au bord de la forêt,
D+ ; et nombre de candidats valides.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import statistics as st
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
import generate_loops as g  # code de production, utilisé tel quel

# bike_elevation.json (GraphHopper 11.0) : ×0,9 dès 4 %, ×0,8 dès 8 %, plafonds 6 et 3 km/h dès 12 et 15 %.
# On compense les deux premiers paliers (1/0,9 et 1/0,8) ; les très fortes pentes restent freinées (vélo de route).
NO_CLIMB_PENALTY = {"speed": [
    {"if": "average_slope >= 8 && average_slope < 12", "multiply_by": "1.25"},
    {"else_if": "average_slope >= 4 && average_slope < 8", "multiply_by": "1.111"},
]}
GREEN_MIN_AREA_DEG2 = 5e-5      # ~0,5 km² à cette latitude : on ignore les petits bosquets
GREEN_MAX_KM = 8.0              # au-delà, pas d'espace vert « proche » : départ ignoré
GREEN_OFFSETS = (40, -40, 70, -70)


class GH(g.GraphHopper):
    """GraphHopper de production, avec en option un modèle personnalisé ajouté à chaque requête (variante C)."""

    def __init__(self, base_url: str, custom_model=None):
        super().__init__(base_url)
        self.custom_model = custom_model

    def _post(self, body):
        if self.custom_model:
            body["custom_model"] = self.custom_model
        try:
            r = self.http.post(f"{self.base}/route", json=body, timeout=120)
        except requests.RequestException as e:
            self.last_error = f"requête échouée : {e}"
            return None
        if r.status_code != 200:
            self.last_error = f"HTTP {r.status_code} : {r.text[:300]}"
            return None
        self.last_error = ""
        paths = r.json().get("paths") or []
        return paths[0] if paths else None

    def round_trip(self, lon, lat, profile, dist_m, seed, heading=None):
        body = {"points": [[lon, lat]], "profile": profile, "algorithm": "round_trip",
                "round_trip.distance": int(dist_m), "round_trip.seed": seed, "ch.disable": True,
                "points_encoded": False, "elevation": True, "instructions": False, "details": g.DETAILS}
        if heading is not None:
            body["heading"] = [heading]
        return self._post(body)

    def via(self, points, profile):
        body = {"points": points, "profile": profile, "ch.disable": True, "points_encoded": False,
                "elevation": True, "instructions": False, "details": g.DETAILS}
        return self._post(body)


def destination(lon, lat, bearing_deg, km):
    b = math.radians(bearing_deg)
    return (lon + km * math.sin(b) / (111.32 * math.cos(math.radians(lat))), lat + km * math.cos(b) / 110.54)


def green_polygons():
    """Grands espaces verts (forêts + espaces protégés) de l'index paysage de production."""
    polys = []
    for key in ("forest", "protected"):
        tree = g.LANDSCAPE.trees.get(key) if g.LANDSCAPE else None
        if tree is not None:
            polys += [p for p in tree.geometries if p.geom_type in ("Polygon", "MultiPolygon")
                      and p.area >= GREEN_MIN_AREA_DEG2]
    return polys


def nearest_green(lon, lat, polys, tree):
    """Point d'entrée (bord le plus proche) du grand espace vert le plus proche, et sa distance en km."""
    import shapely
    from shapely.ops import nearest_points
    pt = shapely.Point(lon, lat)
    i = tree.query_nearest(pt)
    if not len(i):
        return None
    poly = polys[int(i[0])]
    if poly.contains(pt):
        return (lon, lat, 0.0)
    q = nearest_points(poly.boundary, pt)[0]
    return (q.x, q.y, g.haversine(lon, lat, q.x, q.y) / 1000.0)


def green_candidates(gh, start, level, profile, duration_h, entry, log):
    """Candidats « vers le vert » valides (mêmes filtres que fit_and_sample)."""
    lon, lat = start["lon"], start["lat"]
    target_s = duration_h * 3600.0
    flat_ms = g.speed_from_power(g.LEVELS[level]["watts"], 0.0) * g.REAL_WORLD_FACTOR
    dist_km = 0.8 * flat_ms * target_s / 1000.0
    theta = g.bearing(lon, lat, entry[0], entry[1])
    out, why = [], {"pas de boucle": 0, "durée": 0, "tronçons répétés": 0, "demi-tours": 0, "non goudronné": 0,
                    "trop loin": 0}
    for k, off in enumerate(GREEN_OFFSETS):
        r_km = max(dist_km / 4.1, entry[2] + 0.8)        # P1 au-delà de l'entrée de l'espace vert
        loop, too_far = None, False
        for _ in range(3):                                 # ajustement de la taille du triangle sur la durée visée
            p1 = destination(lon, lat, theta, r_km)
            p2 = destination(lon, lat, theta + off, r_km)
            path = gh.via([[lon, lat], list(p1), list(p2), [lon, lat]], profile)
            loop = g.analyse(path, level, profile, duration_h, 900 + k, None) if path else None
            if loop is None:
                break
            ratio = loop.time_s / target_s
            if abs(ratio - 1.0) <= g.TIME_TOLERANCE:
                break
            new_r = r_km / ratio
            if new_r < entry[2] + 0.3:                      # il faudrait rester avant l'espace vert : trop court
                too_far = True
                break
            r_km = new_r
        if too_far:
            why["trop loin"] += 1
            continue
        if loop is None:
            why["pas de boucle"] += 1
            continue
        if abs(loop.time_s / target_s - 1.0) > g.TIME_TOLERANCE:
            why["durée"] += 1
        elif loop.overlap > g.MAX_OVERLAP:
            why["tronçons répétés"] += 1
        elif loop.u_turns > g.MAX_UTURNS:
            why["demi-tours"] += 1
        elif loop.shares["unpaved"] > g.MAX_UNPAVED:
            why["non goudronné"] += 1
        else:
            out.append(loop)
    return out, why


def summary(loop):
    if loop is None:
        return None
    ex = loop.exit_dense_m
    return {"score": loop.score, "city_pct": round(100 * loop.shares["urban"]["city"]),
            "exit_dense_km": None if ex is None or ex >= loop.distance_m * 0.98 else round(ex / 1000.0, 1),
            "forest_pct": round(100 * loop.scenery["forest"]) if loop.scenery else None,
            "dplus_m": round(loop.ascend_m), "km": round(loop.distance_m / 1000.0, 1),
            "min": round(loop.time_s / 60), "via_green": loop.seed >= 900}


def best(pool):
    chosen = g.pick_options(pool)
    return chosen[0][1] if chosen else None


def run_start(s, gh_url, durations, levels, entry, c_ok):
    log_lines = []
    log = log_lines.append
    gh_a, gh_c = GH(gh_url), GH(gh_url, NO_CLIMB_PENALTY)
    snapped = gh_a.nearest(s["lat"], s["lon"])
    if snapped is None or snapped[2] > 400:
        return {"name": s["name"], "skipped": "pas de route à moins de 400 m"}, log_lines
    st_ = {**s, "lon": snapped[0], "lat": snapped[1]}
    rows = []
    for d in durations:
        for level in levels:
            pools = {"A": [], "B": [], "C": [], "CB": []}
            valid = {"A": 0, "B": 0, "C": 0, "CB": 0}
            for profile in g.LEVELS[level]["profiles"]:
                a, _ = g.fit_and_sample(gh_a, st_, level, profile, d, g.CANDIDATES, log)
                b, why_b = green_candidates(gh_a, st_, level, profile, d, entry, log)
                pools["A"] += a
                pools["B"] += b
                if c_ok:
                    c, _ = g.fit_and_sample(gh_c, st_, level, profile, d, g.CANDIDATES, log)
                    cb, _ = green_candidates(gh_c, st_, level, profile, d, entry, log)
                    pools["C"] += c
                    pools["CB"] += cb
            for k in valid:
                valid[k] = len(pools[k])
            row = {"duration_h": d, "level": level, "valid": valid,
                   "A": summary(best(pools["A"])), "A+B": summary(best(pools["A"] + pools["B"]))}
            if c_ok:
                row["C"] = summary(best(pools["C"]))
                row["C+B"] = summary(best(pools["C"] + pools["CB"]))
            rows.append(row)
            a_, ab = row["A"], row["A+B"]
            log(f"  {d:g} h / {level} : A ville {a_ and a_['city_pct']} % sortie {a_ and a_['exit_dense_km']} km"
                f" | A+B ville {ab and ab['city_pct']} % sortie {ab and ab['exit_dense_km']} km"
                f" (vert choisi : {ab and ab['via_green']}) | candidats verts valides {valid['B']}")
    return {"name": s["name"], "zone": s.get("zone"), "green_entry_km": round(entry[2], 2), "rows": rows}, log_lines


def check_custom_model(gh_url, s):
    """La variante C est-elle acceptée par GraphHopper ? (une requête de test)"""
    gh = GH(gh_url, NO_CLIMB_PENALTY)
    path = gh.round_trip(s["lon"], s["lat"], "sport", 15000, 1)
    return path is not None, gh.last_error


def median_delta(rows, var, key):
    vals = [r[var][key] - r["A"][key] for r in rows
            if r.get(var) and r.get("A") and r[var][key] is not None and r["A"][key] is not None]
    return (round(st.median(vals), 1), len(vals)) if vals else (None, 0)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gh", required=True)
    ap.add_argument("--starts", required=True, help="starts_plan.json")
    ap.add_argument("--pbf", required=True, help="extrait OSM (paysage, feux)")
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--names", default="Gràcia", help="départs imposés (séparés par ;), complétés automatiquement")
    ap.add_argument("--n", type=int, default=12, help="nombre total de départs")
    ap.add_argument("--durations", default="1 1.5 2")
    ap.add_argument("--levels", default="modere soutenu")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", required=True)
    ap.add_argument("--out-md", required=True)
    args = ap.parse_args()
    t0 = time.time()

    pbf, wd = Path(args.pbf), Path(args.workdir)
    g.SIGNALS = g.load_signals(pbf, wd)
    g.STOPS = g.load_points(pbf, wd, ["n/highway=stop"], "stops")
    g.LANDSCAPE = g.load_landscape(pbf, wd)
    if g.LANDSCAPE is None:
        sys.exit("paysage OSM non chargé : impossible de repérer les espaces verts")
    import shapely
    polys = green_polygons()
    tree = shapely.STRtree(polys)
    print(f"Grands espaces verts (>= ~0,5 km²) : {len(polys)}", flush=True)

    starts = json.loads(Path(args.starts).read_text(encoding="utf-8"))
    wanted = [n.strip() for n in args.names.split(";") if n.strip()]
    by_name = {s["name"]: s for s in starts}
    picks = [by_name[n] for n in wanted if n in by_name]
    missing = [n for n in wanted if n not in by_name]
    if missing:
        print(f"! départs imposés introuvables dans le plan : {missing}", flush=True)
    pool = [s for s in starts if s.get("zone") == "dense" and s not in picks]
    random.Random(args.seed).shuffle(pool)
    chosen = []
    for s in picks + pool:                                  # garde les départs à moins de GREEN_MAX_KM d'un grand espace vert
        e = nearest_green(s["lon"], s["lat"], polys, tree)
        if e and 0.3 <= e[2] <= GREEN_MAX_KM:
            chosen.append((s, e))
        if len(chosen) >= args.n:
            break
    print("Départs retenus : " + ", ".join(f"{s['name']} (vert à {e[2]:.1f} km)" for s, e in chosen), flush=True)

    c_ok, c_err = check_custom_model(args.gh, chosen[0][0]) if chosen else (False, "aucun départ")
    print(f"Variante C (côtes non freinées) : {'acceptée' if c_ok else 'REFUSÉE par GraphHopper : ' + c_err}", flush=True)

    durations = [float(x) for x in args.durations.split()]
    levels = args.levels.split()
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(run_start, s, args.gh, durations, levels, e, c_ok) for s, e in chosen]
        for f in futs:
            res, lines = f.result()
            print(f"- {res['name']}" + (f" : ignoré ({res['skipped']})" if res.get("skipped") else ""), flush=True)
            for line in lines:
                if line.startswith("  ") and " h / " in line and "A ville" in line:
                    print(line, flush=True)
            results.append(res)

    rows = [r for res in results for r in res.get("rows", [])]
    variants = ["A+B"] + (["C", "C+B"] if c_ok else [])
    agg = {}
    for v in variants:
        changed = sum(1 for r in rows if r.get(v) and r.get("A") and
                      (r[v]["km"], r[v]["dplus_m"]) != (r["A"]["km"], r["A"]["dplus_m"]))
        agg[v] = {"combinations": len(rows), "best_loop_changed": changed,
                  "median_delta_city_pct": median_delta(rows, v, "city_pct"),
                  "median_delta_forest_pct": median_delta(rows, v, "forest_pct"),
                  "median_delta_score": median_delta(rows, v, "score"),
                  "median_delta_dplus_m": median_delta(rows, v, "dplus_m")}
    out_green = sum(1 for r in rows if r.get("A+B") and r["A+B"]["via_green"])
    report = {"variant_c_accepted": c_ok, "variant_c_error": c_err, "starts": results, "aggregate": agg,
              "a_plus_b_best_is_green_candidate": out_green, "seconds": round(time.time() - t0)}
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    fmt = lambda x: "—" if x is None else f"{x['city_pct']} % ville, sortie {x['exit_dense_km'] if x['exit_dense_km'] is not None else 'jamais'}, forêt {x['forest_pct']} %, D+ {x['dplus_m']}, note {x['score']}"  # noqa: E731
    L = [f"# Diagnostic O-12 : sortie vers la nature ({len(results)} départs, {len(rows)} combinaisons, "
         f"{round((time.time() - t0) / 60)} min)",
         f"\nVariante C (côtes non freinées) : {'acceptée' if c_ok else 'refusée par GraphHopper (' + c_err[:150] + ')'}",
         f"\nA+B : la meilleure boucle est un candidat « vers le vert » dans {out_green} combinaisons sur {len(rows)}.",
         "\n## Écart médian par rapport à A (production)",
         "| Variante | Meilleure boucle changée | Δ % ville | Δ % forêt | Δ note | Δ D+ (m) |", "|---|---|---|---|---|---|"]
    for v, a in agg.items():
        L.append(f"| {v} | {a['best_loop_changed']}/{a['combinations']} | {a['median_delta_city_pct'][0]} | "
                 f"{a['median_delta_forest_pct'][0]} | {a['median_delta_score'][0]} | {a['median_delta_dplus_m'][0]} |")
    L.append("\n## Détail")
    for res in results:
        if res.get("skipped"):
            L.append(f"\n**{res['name']}** : ignoré ({res['skipped']})")
            continue
        L.append(f"\n**{res['name']}** (espace vert à {res['green_entry_km']} km)")
        for r in res["rows"]:
            L.append(f"- {r['duration_h']:g} h / {r['level']} — A : {fmt(r['A'])} · A+B : {fmt(r['A+B'])}"
                     + (f" · C : {fmt(r.get('C'))} · C+B : {fmt(r.get('C+B'))}" if c_ok else "")
                     + f" · candidats valides {r['valid']}")
    Path(args.out_md).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L[:12]), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
