#!/usr/bin/env python3
"""
Diagnostic O-12 (handoff.md), v2 : pourquoi les boucles des départs de CENTRE-VILLE ne rejoignent-elles pas la nature ?
Ne publie rien. Réutilise le code de production de generate_loops.py (fit_and_sample, analyse, score_parts,
pick_options, paysage, feux) : la variante A est exactement la génération réelle.

v1 (run #58, 25/09/2026) : des candidats « vers le vert » valides existent mais ne gagnent presque jamais (5/72), et
« côtes non freinées » rebat les cartes sans améliorer (abandonnée). v1 ne gardait que la meilleure boucle : impossible
de savoir POURQUOI les candidats verts perdent. v2 garde, pour chaque combinaison durée × niveau :
  A  meilleure boucle de production (10 candidats round_trip)
  B  meilleure boucle « vers le vert » (triangle départ -> P1 -> P2 -> départ, P1 au-delà de l'entrée du grand espace
     vert le plus proche, P2 décalé de ±40° ou ±70°), même si elle perd contre A
et, pour chacune, les sous-scores de production pondérés (calme, feux, axes, pistes, fluidité, paysage) : le rapport dit
quel critère fait perdre B. Départs : centres-villes seulement (espace vert à plus de GREEN_MIN_ENTRY_KM).
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
import generate_loops as g  # code de production, utilisé tel quel

GREEN_MIN_AREA_DEG2 = 5e-5      # ~0,5 km² à cette latitude : on ignore les petits bosquets
GREEN_MIN_ENTRY_KM = 1.5        # en deçà, le départ touche déjà la nature : pas un « centre-ville »
GREEN_MAX_KM = 8.0              # au-delà, pas d'espace vert « proche »
GREEN_OFFSETS = (40, -40, 70, -70)
PART_LABELS = {"calm": "calme (ville)", "lights": "feux", "axes": "grands axes", "infra": "pistes cyclables",
               "flow": "fluidité (tronçons répétés)", "scenery": "paysage"}


class GH(g.GraphHopper):
    """GraphHopper de production + itinéraire passant par des points imposés (candidats « vers le vert »)."""

    def via(self, points, profile):
        body = {"points": points, "profile": profile, "ch.disable": True, "points_encoded": False,
                "elevation": True, "instructions": False, "details": g.DETAILS}
        try:
            r = self.http.post(f"{self.base}/route", json=body, timeout=120)
        except requests.RequestException as e:
            self.last_error = f"requête échouée : {e}"
            return None
        if r.status_code != 200:
            self.last_error = f"HTTP {r.status_code} : {r.text[:300]}"
            return None
        paths = r.json().get("paths") or []
        return paths[0] if paths else None


def destination(lon, lat, bearing_deg, km):
    b = math.radians(bearing_deg)
    return (lon + km * math.sin(b) / (111.32 * math.cos(math.radians(lat))), lat + km * math.cos(b) / 110.54)


def green_polygons():
    polys = []
    for key in ("forest", "protected"):
        tree = g.LANDSCAPE.trees.get(key) if g.LANDSCAPE else None
        if tree is not None:
            polys += [p for p in tree.geometries if p.geom_type in ("Polygon", "MultiPolygon")
                      and p.area >= GREEN_MIN_AREA_DEG2]
    return polys


def nearest_green(lon, lat, polys, tree):
    """Entrée (bord le plus proche) du grand espace vert le plus proche : (lon, lat, distance en km)."""
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


def green_candidates(gh, start, level, profile, duration_h, entry):
    """Candidats « vers le vert » valides (mêmes filtres que fit_and_sample) et raisons de rejet."""
    lon, lat = start["lon"], start["lat"]
    target_s = duration_h * 3600.0
    flat_ms = g.speed_from_power(g.LEVELS[level]["watts"], 0.0) * g.REAL_WORLD_FACTOR
    dist_km = 0.8 * flat_ms * target_s / 1000.0
    theta = g.bearing(lon, lat, entry[0], entry[1])
    out, why = [], Counter()
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
            if new_r < entry[2] + 0.3:                      # il faudrait s'arrêter avant l'espace vert
                too_far = True
                break
            r_km = new_r
        if too_far:
            why["espace vert trop loin pour la durée"] += 1
        elif loop is None:
            why["pas de boucle"] += 1
        elif abs(loop.time_s / target_s - 1.0) > g.TIME_TOLERANCE:
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
    parts, weights = g.score_parts(loop)
    wsum = sum(weights.values())
    ex = loop.exit_dense_m
    return {"score": loop.score, "city_pct": round(100 * loop.shares["urban"]["city"]),
            "exit_dense_km": None if ex is None or ex >= loop.distance_m * 0.98 else round(ex / 1000.0, 1),
            "forest_pct": round(100 * loop.scenery["forest"]) if loop.scenery else None,
            "dplus_m": round(loop.ascend_m), "km": round(loop.distance_m / 1000.0, 1), "min": round(loop.time_s / 60),
            "overlap_pct": round(100 * loop.overlap), "u_turns": loop.u_turns,
            "lights_per_km": round(loop.signals / max(loop.distance_m / 1000.0, 0.1), 2) if loop.signals is not None else None,
            "unpaved_pct": round(100 * loop.shares["unpaved"]),
            # points de note apportés par chaque critère (somme = note hors pénalité non goudronné)
            "points": {k: round(100 * weights[k] * parts[k] / wsum, 1) for k in weights}}


def best(pool):
    chosen = g.pick_options(pool)
    return chosen[0][1] if chosen else None


def run_start(s, gh_url, durations, levels, entry):
    log_lines = []
    gh = GH(gh_url)
    snapped = gh.nearest(s["lat"], s["lon"])
    if snapped is None or snapped[2] > 400:
        return {"name": s["name"], "skipped": "pas de route à moins de 400 m"}
    st_ = {**s, "lon": snapped[0], "lat": snapped[1]}
    rows = []
    for d in durations:
        for level in levels:
            pool_a, pool_b, why_b = [], [], Counter()
            for profile in g.LEVELS[level]["profiles"]:
                a, _ = g.fit_and_sample(gh, st_, level, profile, d, g.CANDIDATES, log_lines.append)
                b, why = green_candidates(gh, st_, level, profile, d, entry)
                pool_a += a
                pool_b += b
                why_b.update(why)
            ba = best(pool_a)
            bb = max(pool_b, key=lambda l: l.score) if pool_b else None
            rows.append({"duration_h": d, "level": level, "valid_a": len(pool_a), "valid_b": len(pool_b),
                         "rejects_b": dict(why_b), "A": summary(ba), "B": summary(bb),
                         "b_wins": bool(bb and ba and best(pool_a + pool_b) is bb)})
    return {"name": s["name"], "zone": s.get("zone"), "municipality": s.get("municipality"),
            "green_entry_km": round(entry[2], 2), "rows": rows}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gh", required=True)
    ap.add_argument("--starts", required=True, help="starts_plan.json")
    ap.add_argument("--pbf", required=True, help="extrait OSM (paysage, feux)")
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--names", default="Gràcia;el Fort Pienc (nord);Navas (nord);l'Hospitalet de Llobregat;Sant Adrià de Besòs",
                    help="départs imposés (séparés par ;), complétés automatiquement par des départs denses")
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
    for s in picks + pool:
        e = nearest_green(s["lon"], s["lat"], polys, tree)
        if e and (s in picks or GREEN_MIN_ENTRY_KM <= e[2] <= GREEN_MAX_KM):
            chosen.append((s, e))
        if len(chosen) >= args.n:
            break
    print("Départs retenus : " + ", ".join(f"{s['name']} (vert à {e[2]:.1f} km)" for s, e in chosen), flush=True)

    durations = [float(x) for x in args.durations.split()]
    levels = args.levels.split()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        results = list(ex.map(lambda se: run_start(se[0], args.gh, durations, levels, se[1]), chosen))

    rows = [r for res in results for r in res.get("rows", [])]
    with_b = [r for r in rows if r["A"] and r["B"]]
    b_loses = [r for r in with_b if not r["b_wins"]]
    culprit = Counter()                                     # critère qui creuse le plus l'écart quand B perd
    for r in b_loses:
        deltas = {k: r["B"]["points"].get(k, 0) - r["A"]["points"].get(k, 0) for k in r["A"]["points"]}
        culprit[min(deltas, key=deltas.get)] += 1
    mean_delta = {}
    for k in PART_LABELS:
        vals = [r["B"]["points"].get(k, 0) - r["A"]["points"].get(k, 0) for r in with_b if k in r["A"]["points"]]
        mean_delta[k] = round(sum(vals) / len(vals), 1) if vals else None
    rejects = Counter()
    for r in rows:
        rejects.update(r["rejects_b"])
    report = {"starts": results, "combinations": len(rows), "with_b": len(with_b),
              "b_wins": sum(1 for r in rows if r["b_wins"]), "culprit_when_b_loses": dict(culprit),
              "mean_points_delta_b_minus_a": mean_delta, "rejects_b": dict(rejects), "seconds": round(time.time() - t0)}
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    def fmt(x):
        if x is None:
            return "—"
        ex_ = x["exit_dense_km"] if x["exit_dense_km"] is not None else "jamais"
        return (f"note {x['score']} · ville {x['city_pct']} % · sortie {ex_} · forêt {x['forest_pct']} % · "
                f"D+ {x['dplus_m']} · répété {x['overlap_pct']} % · feux {x['lights_per_km']}/km")
    L = [f"# Diagnostic O-12 v2 : pourquoi les boucles « vers le vert » perdent ({len(results)} départs, "
         f"{len(rows)} combinaisons, {round((time.time() - t0) / 60)} min)",
         f"\nUne boucle « vers le vert » valide existe dans {len(with_b)} combinaisons ; elle devient la meilleure dans "
         f"{report['b_wins']}.",
         "\n## Quand B perd, critère qui lui coûte le plus de points",
         "| Critère | Combinaisons |", "|---|---|"]
    for k, n in culprit.most_common():
        L.append(f"| {PART_LABELS.get(k, k)} | {n} |")
    L += ["\n## Écart moyen de points B − A par critère (négatif = B perd des points)",
          "| Critère | Δ points |", "|---|---|"]
    for k, v in mean_delta.items():
        L.append(f"| {PART_LABELS[k]} | {v} |")
    L += ["\n## Candidats « vers le vert » rejetés", "| Raison | Nombre |", "|---|---|"]
    for k, n in rejects.most_common():
        L.append(f"| {k} | {n} |")
    L.append("\n## Détail (A = production, B = meilleure boucle « vers le vert »)")
    for res in results:
        if res.get("skipped"):
            L.append(f"\n**{res['name']}** : ignoré ({res['skipped']})")
            continue
        L.append(f"\n**{res['name']}** ({res.get('municipality')}, espace vert à {res['green_entry_km']} km)")
        for r in res["rows"]:
            L.append(f"- {r['duration_h']:g} h / {r['level']}{' — **B gagne**' if r['b_wins'] else ''}")
            L.append(f"  - A : {fmt(r['A'])}")
            L.append(f"  - B : {fmt(r['B'])}")
            if r["A"] and r["B"]:
                L.append("  - points B − A : " + ", ".join(
                    f"{PART_LABELS[k]} {r['B']['points'].get(k, 0) - r['A']['points'].get(k, 0):+.1f}" for k in r["A"]["points"]))
    Path(args.out_md).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L[:30]), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
