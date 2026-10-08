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
# Boucles de référence tracées par un cycliste local (v3) : points INTERMÉDIAIRES seulement (routes publiques, loin du
# point de départ réel, sans horaire ni données de la montre). GraphHopper relie le départ Oyan à ces points.
# Gràcia, variante préférée (reconstruite, sans trace) : Via Augusta jusqu'à Sarrià (2 points, géocodés avec Photon),
# montée au col ~494 m (montée laissée au choix de GraphHopper), descente par l'Arrabassada (2 points d'une trace réelle).
# La trace réelle (par la Diagonal et Pedralbes) : ~24 km, ~450 m D+, ~1 h 30.
REFERENCE_LOOPS = {
    "Gràcia": [(2.14116, 41.39825), (2.12622, 41.39812),                      # Via Augusta (n°200, puis vers Sarrià)
               (2.11905, 41.42325), (2.13446, 41.43420), (2.12845, 41.42478)],  # col, puis descente par l'Arrabassada
}
# Grands axes v2 (règle de Florent, 25/09/2026) : une grande route est acceptable si elle mène vers la nature ; pénalité
# entière en ville ou zone résidentielle, demi-pénalité hors de la ville (une route plus petite aux mêmes avantages
# doit rester préférée).
AXES_V2_RURAL_FACTOR = 0.5
PART_LABELS = {"calm": "calme (ville)", "lights": "feux", "axes": "grands axes", "infra": "pistes cyclables",
               "flow": "fluidité (tronçons répétés)", "scenery": "paysage"}


class GH(g.GraphHopper):
    """GraphHopper de production + itinéraire passant par des points imposés (candidats « vers le vert »)."""

    def via(self, points, profile, pass_through=False):          # même client que la production (mémoire comprise)
        return g.GraphHopper.via(self, points, profile, pass_through)


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
    flat_ms = g.speed_from_power(g.level_watts(level, duration_h), 0.0) * g.REAL_WORLD_FACTOR
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
            "score_v2": score_v2(loop),
            "cycleway_pct": round(100 * loop.shares["dedicated_cycleway"]),
            "main_roads_pct": round(100 * loop.shares["main_roads"]),
            "city_car_pct": round(100 * loop.shares.get("urban_car", loop.shares["urban"])["city"]),
            "seed": loop.seed,
            "industrial_pct": round(100 * loop.scenery.get("industrial", 0.0)) if loop.scenery else None,
            "dirt_km": round(loop.shares["unpaved"] * loop.distance_m / 1000.0, 1),
            "water_pct": round(100 * loop.scenery["water"]) if loop.scenery else None,
            "main_roads_rural_pct": round(100 * loop.shares.get("main_roads_by_urban", {}).get("rural", 0.0)),
            # points de note apportés par chaque critère (somme = note hors pénalité non goudronné)
            "points": {k: round(100 * weights[k] * parts[k] / wsum, 1) for k in weights}}


def score_v2(loop) -> float:
    """Note de production, avec le critère « grands axes » v2."""
    parts, weights = g.score_parts(loop)
    mr = loop.shares.get("main_roads_by_urban", {})
    eff = mr.get("city", 0.0) + mr.get("residential", 0.0) + AXES_V2_RURAL_FACTOR * mr.get("rural", 0.0)
    parts["axes"] = 1.0 - min(1.0, 2.0 * eff)
    total = sum(weights[k] * parts[k] for k in weights) / sum(weights.values())
    total -= min(0.3, max(0.0, loop.shares["unpaved"] - 0.03) * 2.0)
    return round(100 * max(0.0, total), 1)


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
            ba2 = max(pool_a, key=score_v2) if pool_a else None      # meilleure boucle de production avec le score v2
            rows.append({"duration_h": d, "level": level, "valid_a": len(pool_a), "valid_b": len(pool_b),
                         "rejects_b": dict(why_b), "A": summary(ba), "A_v2": summary(ba2), "B": summary(bb),
                         "b_wins": bool(bb and ba and best(pool_a + pool_b) is bb)})
    reference = []
    for level in levels if s["name"] in REFERENCE_LOOPS else []:
        wps = [[lon, lat] for lon, lat in REFERENCE_LOOPS[s["name"]]]
        for profile in g.LEVELS[level]["profiles"]:
            path = gh.via([[st_["lon"], st_["lat"]]] + wps + [[st_["lon"], st_["lat"]]], profile)
            loop = g.analyse(path, level, profile, 1.5, 800, None) if path else None
            if loop is None:
                reference.append({"level": level, "profile": profile, "error": gh.last_error or "pas de boucle"})
                continue
            ref = summary(loop)
            near = min((r for r in rows if r["level"] == level and r["A"]),
                       key=lambda r: abs(r["duration_h"] * 60 - ref["min"]), default=None)
            reference.append({"level": level, "profile": profile, "ref": ref,
                              "compared_to": near and {"duration_h": near["duration_h"], "A": near["A"],
                                                       "A_v2": near["A_v2"]},
                              "ref_beats_a": bool(near and ref["score"] > near["A"]["score"]),
                              "ref_beats_a_v2": bool(near and near["A_v2"] and ref["score_v2"] > near["A_v2"]["score_v2"])})
    return {"name": s["name"], "zone": s.get("zone"), "municipality": s.get("municipality"),
            "green_entry_km": round(entry[2], 2), "rows": rows, "reference": reference}


# ----------------------------------------------------------------------------- v5 : calibrage sur boucles de référence
DURATIONS_H = [0.75, 1, 1.5, 2, 3, 4]         # durées proposées par l'app
WEIGHT_VARIANTS = {                            # pondérations testées (aucune n'est appliquée en production)
    "actuel": {},
    "grands axes ÷2": {"weights": {"axes": 0.07}},
    "paysage ×2": {"weights": {"scenery": 0.24}},
    "répétitions tolérées": {"soft_flow": True},
    "combiné (3 ci-dessus)": {"weights": {"axes": 0.07, "scenery": 0.24}, "soft_flow": True},
}


def score_variant(loop, variant) -> float:
    parts, weights = g.score_parts(loop)
    for k, w in variant.get("weights", {}).items():
        if k in weights:
            weights[k] = w
    if variant.get("soft_flow"):                  # moitié moins pénalisant pour les tronçons répétés et demi-tours
        parts["flow"] = 1.0 - min(1.0, loop.overlap + 0.075 * loop.u_turns)
    total = sum(weights[k] * parts[k] for k in weights) / sum(weights.values())
    total -= min(0.3, max(0.0, loop.shares["unpaved"] - 0.03) * 2.0)
    return round(100 * max(0.0, total), 1)


def run_reference(ref, gh_url, levels):
    """Boucle de référence recalculée par ses points de passage, contre la meilleure boucle de production depuis le
    même départ et pour la durée la plus proche."""
    gh = GH(gh_url)
    lon0, lat0 = ref["start"]
    snapped = gh.nearest(lat0, lon0)
    if snapped is None or snapped[2] > 400:
        return {"name": ref["name"], "skipped": "pas de route à moins de 400 m du départ"}
    st_ = {"name": ref["name"], "lon": snapped[0], "lat": snapped[1]}
    out = []
    for level in levels:
        profile = g.LEVELS[level]["profiles"][0]
        path = gh.via([[st_["lon"], st_["lat"]]] + ref["waypoints"] + [[st_["lon"], st_["lat"]]], profile)
        loop = g.analyse(path, level, profile, 1.0, 800, None) if path else None
        if loop is None:
            out.append({"level": level, "error": gh.last_error or "pas de boucle"})
            continue
        minutes = loop.time_s / 60
        if minutes > 270:
            out.append({"level": level, "ref": summary(loop), "error": f"trop long pour l'app ({round(minutes)} min)"})
            continue
        d = min(DURATIONS_H, key=lambda h: abs(h * 60 - minutes))
        pool = []
        for p in g.LEVELS[level]["profiles"]:
            a, _ = g.fit_and_sample(gh, st_, level, p, d, g.CANDIDATES, lambda *_: None)
            pool += a
        res = {"level": level, "duration_h": d, "ref": summary(loop), "valid_a": len(pool), "variants": {}}
        for vname, var in WEIGHT_VARIANTS.items():
            ref_s = score_variant(loop, var)
            best_a = max(pool, key=lambda l: score_variant(l, var)) if pool else None
            res["variants"][vname] = {"ref": ref_s, "A": score_variant(best_a, var) if best_a else None,
                                      "A_loop": summary(best_a), "ref_wins": bool(best_a is None or ref_s > score_variant(best_a, var))}
        out.append(res)
    return {"name": ref["name"], "source": ref.get("source"), "track_km": ref.get("track_km"),
            "track_dplus_m": ref.get("track_dplus_m"), "levels": out}


def report_references(results, path_json, path_md, note, t0):
    cases = [(r, lv) for r in results for lv in r.get("levels", []) if lv.get("variants")]
    wins = {v: sum(1 for _, lv in cases if lv["variants"][v]["ref_wins"]) for v in WEIGHT_VARIANTS}
    deltas = {}
    for k in PART_LABELS:                            # écart moyen de points référence − production (score actuel)
        vals = [lv["ref"]["points"].get(k, 0) - lv["variants"]["actuel"]["A_loop"]["points"].get(k, 0)
                for _, lv in cases if lv["variants"]["actuel"]["A_loop"]]
        deltas[k] = round(sum(vals) / len(vals), 1) if vals else None
    Path(path_json).write_text(json.dumps({"references": results, "wins": wins, "mean_points_delta": deltas,
                                           "cases": len(cases), "seconds": round(time.time() - t0)},
                                          ensure_ascii=False, indent=1), encoding="utf-8")

    def fmt(x):
        if not x:
            return "—"
        ex_ = x["exit_dense_km"] if x["exit_dense_km"] is not None else "jamais"
        return (f"{x['km']} km · {x['min']} min · D+ {x['dplus_m']} · ville {x['city_pct']} % · sortie {ex_} · forêt "
                f"{x['forest_pct']} % · pistes et voies vertes {x['cycleway_pct']} % · grandes routes {x['main_roads_pct']} % · "
                f"répété {x['overlap_pct']} % · "
                f"non goudronné {x['unpaved_pct']} %")
    L = [f"# Diagnostic O-12 v5 : calibrage sur {len(results)} boucles de référence ({len(cases)} cas, "
         f"{round((time.time() - t0) / 60)} min)", (f"\n**Réglage de ce run : {note}**" if note else ""),
         "\n## Combien de boucles de référence battent la boucle d'Oyan, selon la pondération",
         "| Pondération | Référence gagnante |", "|---|---|"]
    for v, n in wins.items():
        L.append(f"| {v} | {n} / {len(cases)} |")
    def med(key, who):
        v = sorted((lv["ref"] if who == "ref" else lv["variants"]["actuel"]["A_loop"])[key] for _, lv in cases
                   if lv["variants"]["actuel"]["A_loop"])
        return v[len(v) // 2] if v else None
    L += ["\n## Profils médians (référence / Oyan)", "| Mesure | Référence | Oyan |", "|---|---|---|"]
    for key, lab in (("cycleway_pct", "pistes et voies vertes %"), ("city_pct", "en ville %"), ("forest_pct", "forêt %"),
                     ("main_roads_pct", "grandes routes %"), ("overlap_pct", "répété %"), ("dplus_m", "D+ m")):
        L.append(f"| {lab} | {med(key, 'ref')} | {med(key, 'A')} |")
    L += ["\n## Écart moyen de points référence − Oyan par critère (score actuel ; négatif = la référence perd)",
          "| Critère | Δ points |", "|---|---|"]
    for k, v in deltas.items():
        L.append(f"| {PART_LABELS[k]} | {v} |")
    L.append("\n## Détail")
    for r in results:
        L.append(f"\n**{r['name']}** ({r.get('source')}, trace : {r.get('track_km')} km, D+ {r.get('track_dplus_m')} m)")
        if r.get("skipped"):
            L.append(f"- ignorée : {r['skipped']}")
            continue
        for lv in r["levels"]:
            if not lv.get("variants"):
                L.append(f"- {lv['level']} : {lv.get('error')}" + (f" — référence : {fmt(lv.get('ref'))}" if lv.get("ref") else ""))
                continue
            a = lv["variants"]["actuel"]
            L.append(f"- {lv['level']}, durée Oyan {lv['duration_h']:g} h — notes référence / Oyan : "
                     + " · ".join(f"{v} {x['ref']}/{x['A']}{' ✓' if x['ref_wins'] else ''}" for v, x in lv["variants"].items()))
            L.append(f"  - référence : {fmt(lv['ref'])}")
            L.append(f"  - Oyan : {fmt(a['A_loop'])}")
            if a["A_loop"]:
                L.append("  - points référence − Oyan : " + ", ".join(
                    f"{PART_LABELS[k]} {lv['ref']['points'].get(k, 0) - a['A_loop']['points'].get(k, 0):+.1f}"
                    for k in a["A_loop"]["points"]))
    Path(path_md).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L[:20]), flush=True)


# ----------------------------------------------------------------------------- sonde : boucles de production avant/après
WAYS = None                     # sonde « revêtement » : voies OSM classées (landcover.load_ways), chargées dans main()
RETOUCH = False                 # --retouch : retouche des meilleures boucles mesurée (sonde, comparaison)


def option_row(o) -> dict:
    """Résumé d'une option publiée (fichier starts/<id>.json) ou fraîchement générée (to_json)."""
    sc = o.get("scenery") or {}
    lc = sc.get("landcover") or {}
    dirt = None
    if WAYS is not None:            # même mesure (sur OSM) pour les boucles publiées et les nouvelles : comparable
        import landcover
        km = landcover.classify_loop(WAYS[0], WAYS[1], o["coords"], o["distance_km"])[0]
        dirt = {"dirt_km": round(landcover.dirt_km(km), 1), "g1_km": round(km["piste_g1"], 1),
                "sentier_km": round(km["sentier_sans"], 1)}
    return {**({"surface": dirt} if dirt else {}),"cells": sorted({(round(c[0] / 0.006), round(c[1] / 0.005)) for c in o["coords"]}),   # doublons (O-18)
            "main_roads_pct": round(100 * o["shares"]["main_roads"]), "same_as": o.get("same_as"),
            "label": o["label"], "km": round(o["distance_km"], 1), "dplus_m": round(o["ascend_m"]),
            "forest_pct": round(100 * sc.get("forest", 0)), "city_pct": round(100 * lc["city"]) if lc else None,
            "cycleway_pct": round(100 * o["shares"]["dedicated_cycleway"]), "score": o["score"],
            "fallback": bool(o.get("unpaved_fallback")), "targeted": bool(o.get("targeted")),
            "retouched": bool(o.get("retouched")),
            "outback": bool(o.get("out_and_back")),
            "spurs": len(g.unjustified_spurs(o["coords"])) if o.get("coords") else None,
            "remarkable": [x["n"] for x in o.get("remarkable") or []], "views": o.get("views_passed", 0),
            "water_pct": round(100 * sc.get("water", 0)), "min": o.get("time_est_min"),
            "lights_km": o.get("traffic_lights_per_km"), "industrial_pct": round(100 * sc.get("industrial", 0))}


def run_probe(sid, site, gh_url, durations, levels):
    """Options que produirait la génération réelle (réglages du dépôt) pour ce départ, face aux options publiées."""
    idx = requests.get(f"{site}/web/data/index.json", timeout=60).json()
    entry = next((e for e in idx["starts"] if e["id"] == sid), None)
    if entry is None:
        return {"id": sid, "skipped": "départ absent de l'index publié"}
    pub = requests.get(f"{site}/web/data/starts/{sid}.json", timeout=60).json()
    gh = GH(gh_url)
    snapped = gh.nearest(entry["lat"], entry["lon"])
    st_ = {"name": entry["name"], "lon": snapped[0], "lat": snapped[1]}
    if g.KEEP_PREVIOUS:                                    # boucles publiées = version précédente (comme la production)
        st_["previous"] = {}
        for o in pub["options"]:
            st_["previous"].setdefault((o["level"], round(o["duration_target_min"])), []).append(
                (o.get("profile") or g.LEVELS[o["level"]]["profiles"][0], o["coords"]))
    rows = []
    for d in durations:
        prior = []                                         # comme la génération réelle (O-18 A)
        for level in sorted(levels, key=list(g.LEVELS).index):
            pool = g.level_pool(gh, st_, level, d, g.CANDIDATES, lambda *_: None)
            base_best = max((l.score for l in pool), default=None)
            t_r, det, found, rstats = time.time(), [], [], []
            if RETOUCH:                                    # retouche mesurée à part (temps, gain de note)
                found = g.retouch_candidates(gh, st_, level, d, pool, lambda *_: None, detail=det, stats=rstats)
                pool = pool + found
            t_r = time.time() - t_r
            picks = g.choose_options(gh, st_, level, d, pool, prior, lambda *_: None)
            prior += [l for _, l in picks]
            for _, l in picks:
                g.record_ways(sid, l)
            new = [option_row(g.to_json(l, lab, sid, i)) for i, (lab, l) in enumerate(picks, start=1)]
            old = [option_row(o) for o in pub["options"]
                   if o["level"] == level and round(o["duration_target_min"]) == round(d * 60)]
            rows.append({"duration_h": d, "level": level, "published": old, "new": new, "valid": len(pool),
                         "base_best": None if base_best is None else round(base_best, 1), "retouch_s": round(t_r, 1),
                         # pour simuler des réglages moins coûteux : variantes gardées (tirage = 1000 + 100 × boucle + rang de
                         # l'essai) et essais faits
                         "retouch_found": [[l.seed, round(l.score, 1)] for l in found],
                         "retouch_trials": sum(1 for x in det if x.startswith("  ")), "retouch_stats": rstats})
    return {"id": sid, "name": entry.get("municipality", "") + " · " + entry["name"], "rows": rows,
            "gh_calls": gh.calls, "gh_hits": gh.hits}


# ----------------------------------------------------------------------------- relief par allure (08/10/2026)
def relief_generate(gh, st_, sid, durations, levels, limits, dirt=None):
    """Toutes les durées des allures demandées comme la génération réelle, avec les règles de relief données (None =
    production v16)."""
    g._TL.relief_limits = limits
    g._TL.dirt_rules = dirt
    out, options = {}, []
    for d in durations:
        prior = []
        for level in sorted(levels, key=list(g.LEVELS).index):
            pool = g.keep_order(g.level_pool(gh, st_, level, d, g.CANDIDATES, lambda *_: None), options, level, d)
            picks = g.choose_options(gh, st_, level, d, pool, prior, lambda *_: None)
            prior += [l for _, l in picks]
            rows = []
            for i, (lab, l) in enumerate(picks, start=1):
                o = g.to_json(l, lab, sid, i)
                options.append(o)
                lc = (o.get("scenery") or {}).get("landcover") or {}
                cats = [c.get("category") for c in (o.get("terrain") or {}).get("climbs", [])]
                rows.append({"score": o["score"], "label": lab, "km": o["distance_km"], "dplus": o["ascend_m"],
                             "dpk": round(o["ascend_m"] / max(o["distance_km"], 0.1), 1), "min": o["time_est_min"],
                             "nature": round(lc.get("forest", 0) + lc.get("water", 0), 3), "exit": o.get("exit_city_km"),
                             "lights_km": o["traffic_lights_per_km"], "cats": [c for c in cats if c != "nc"],
                             "max_grade": (o.get("terrain") or {}).get("max_grade_pct"),
                             "fallback": bool(getattr(l, "relief_fallback", False)), "key": o["route_key"],
                             "dirt": round(o["distance_km"] * o["shares"]["unpaved"], 1),
                             "dirt_fallback": bool(o.get("unpaved_fallback"))})
            out[f"{d:g}|{level}"] = rows
    del g._TL.relief_limits
    del g._TL.dirt_rules
    return out


def run_relief_test(sid, site, gh_url, durations, levels, prev_dir, variant="v17"):
    """Même départ sans puis avec les règles de relief v17 ; boucles v15 remises en jeu comme en production v16."""
    idx = requests.get(f"{site}/web/data/index.json", timeout=60).json()
    entry = next((e for e in idx["starts"] if e["id"] == sid), None)
    if entry is None:
        return {"id": sid, "skipped": "départ absent de l'index publié"}
    gh = GH(gh_url)
    snapped = gh.nearest(entry["lat"], entry["lon"])
    st_ = {"name": entry["name"], "lon": snapped[0], "lat": snapped[1], "previous": {}}
    if prev_dir.startswith("http"):                      # boucles publiées (v16) : ce que la v17 remettra en jeu
        prev = requests.get(f"{prev_dir.rstrip('/')}/{sid}.json", timeout=60).json()
    else:
        pf = Path(prev_dir) / f"{sid}.json"
        prev = json.loads(pf.read_text(encoding="utf-8")) if pf.exists() else {"options": []}
    for o in prev["options"]:
        st_["previous"].setdefault((o["level"], round(o["duration_target_min"])), []).append(
            (o.get("profile") or g.LEVELS[o["level"]]["profiles"][0], o["coords"]))
    t0 = time.time()
    a = relief_generate(gh, st_, sid, durations, levels, None)
    t1 = time.time()
    if variant == "dirt":                                  # terre v17 seule (relief inchangé)
        b = relief_generate(gh, st_, sid, durations, levels, None, g.DIRT_RULES_V17)
    else:
        b = relief_generate(gh, st_, sid, durations, levels,
                            g.RELIEF_LIMITS_V17_FLAT if variant == "flat" else g.RELIEF_LIMITS_V17)
    res = {"id": sid, "zone": entry.get("zone"), "v16": a, "relief": b, "s": [round(t1 - t0), round(time.time() - t1)]}
    Path(f"data/relief_{sid}.json").write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
    print(f"{sid} : {res['s']} s", flush=True)
    return res


def report_relief_test(results, out_json, out_md, t0):
    Path(out_json).write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    res = [r for r in results if not r.get("skipped")]
    med = lambda v: sorted(v)[len(v) // 2] if v else None  # noqa: E731
    L = [f"# Relief par allure (v17) : {len(res)} départs — {round((time.time() - t0) / 60)} min", "",
         "| allure | D+/km médian v16 -> v17 | > 10 m/km | > 15 m/km | note moyenne | nature médiane | repli | options |"
         " terre > 1 km | terre > 0,5 km | repli terre |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    for lv in ("facile", "modere", "soutenu"):
        if not any(key.endswith("|" + lv) for r in res for key in r["v16"]):
            continue
        def recs(k):
            return [rows[0] for r in res for key, rows in r[k].items() if key.endswith("|" + lv) and rows]
        a, b = recs("v16"), recs("relief")
        opts = lambda k: sum(len(rows) for r in res for key, rows in r[k].items() if key.endswith("|" + lv))  # noqa: E731
        L.append(f"| {lv} | {med([x['dpk'] for x in a])} -> {med([x['dpk'] for x in b])} | "
                 f"{sum(x['dpk'] > 10 for x in a)} -> {sum(x['dpk'] > 10 for x in b)} | "
                 f"{sum(x['dpk'] > 15 for x in a)} -> {sum(x['dpk'] > 15 for x in b)} | "
                 f"{sum(x['score'] for x in a) / max(1, len(a)):.1f} -> {sum(x['score'] for x in b) / max(1, len(b)):.1f} | "
                 f"{med([x['nature'] for x in a])} -> {med([x['nature'] for x in b])} | {sum(x['fallback'] for x in b)} | "
                 f"{opts('v16')} -> {opts('relief')} | "
                 f"{sum(x.get('dirt', 0) > 1 for x in a)} -> {sum(x.get('dirt', 0) > 1 for x in b)} | "
                 f"{sum(x.get('dirt', 0) > 0.5 for x in a)} -> {sum(x.get('dirt', 0) > 0.5 for x in b)} | "
                 f"{sum(x.get('dirt_fallback', False) for x in b)} |")
    Path(out_md).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L), flush=True)


# ----------------------------------------------------------------------------- marge des plages de durée (07/10/2026)
def margin_generate(gh, st_, sid, durations, levels, margin):
    """Toutes les durées et allures d'un départ comme la génération réelle, avec la marge des plages donnée (None = v16)."""
    g._TL.bin_margin = margin
    options, out = [], {}
    for d in durations:
        prior = []
        for level in sorted(levels, key=list(g.LEVELS).index):
            pool = g.keep_order(g.level_pool(gh, st_, level, d, g.CANDIDATES, lambda *_: None), options, level, d)
            picks = g.choose_options(gh, st_, level, d, pool, prior, lambda *_: None)
            prior += [l for _, l in picks]
            opts = [g.to_json(l, lab, sid, i) for i, (lab, l) in enumerate(picks, start=1)]
            options += opts
            out[f"{d:g}|{level}"] = [{"score": o["score"], "min": o["time_est_min"], "key": o["route_key"],
                                      "label": o["label"], "lights_km": o["traffic_lights_per_km"],
                                      "dirt_km": round(o["distance_km"] * o["shares"]["unpaved"], 1)} for o in opts]
    del g._TL.bin_margin
    return out


def run_margin(sid, site, gh_url, durations, levels, margin, prev_dir):
    """Même départ avec les plages strictes (v16) puis avec la marge ; boucles v15 (prev_dir) remises en jeu comme en
    production v16 (KEEP_PREVIOUS)."""
    idx = requests.get(f"{site}/web/data/index.json", timeout=60).json()
    entry = next((e for e in idx["starts"] if e["id"] == sid), None)
    if entry is None:
        return {"id": sid, "skipped": "départ absent de l'index publié"}
    gh = GH(gh_url)
    snapped = gh.nearest(entry["lat"], entry["lon"])
    st_ = {"name": entry["name"], "lon": snapped[0], "lat": snapped[1], "previous": {}}
    pf = Path(prev_dir) / f"{sid}.json"                     # fichier du départ dans les données v15 (loops-data)
    if pf.exists():
        for o in json.loads(pf.read_text(encoding="utf-8"))["options"]:
            st_["previous"].setdefault((o["level"], round(o["duration_target_min"])), []).append(
                (o.get("profile") or g.LEVELS[o["level"]]["profiles"][0], o["coords"]))
    t0 = time.time()
    a = margin_generate(gh, st_, sid, durations, levels, None)
    t1 = time.time()
    b = margin_generate(gh, st_, sid, durations, levels, margin)
    res = {"id": sid, "zone": entry.get("zone"), "strict": a, "margin": b, "s": [round(t1 - t0), round(time.time() - t1)]}
    Path(f"data/margin_{sid}.json").write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")  # au fil de l'eau
    print(f"{sid} : {res['s']} s", flush=True)
    return res


def report_margin(results, out_json, out_md, margin, drop_ids, t0):
    Path(out_json).write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    res = [r for r in results if not r.get("skipped")]

    def stats(rs):
        d = [(r, k, r["strict"][k][0]["score"] if r["strict"].get(k) else 0.0,
              r["margin"][k][0]["score"] if r["margin"].get(k) else 0.0) for r in rs for k in set(r["strict"]) | set(r["margin"])]
        n = len(d)
        return d, (f"{n} combinaisons ; recommandée moyenne {sum(x[2] for x in d) / max(1, n):.1f} -> "
                   f"{sum(x[3] for x in d) / max(1, n):.1f} ; identique {sum(abs(x[3] - x[2]) < 0.05 for x in d)}, "
                   f"mieux (> +3) {sum(x[3] - x[2] > 3 for x in d)}, moins bien (< -3) {sum(x[3] - x[2] < -3 for x in d)}, "
                   f"(< -10) {sum(x[3] - x[2] < -10 for x in d)}")
    dd, sd = stats([r for r in res if r["id"] in drop_ids])
    dt, stt = stats([r for r in res if r["id"] not in drop_ids])
    opts = lambda k: sum(len(v) for r in res for v in r[k].values())  # noqa: E731
    L = [f"# Marge des plages de durée ({margin:g}) — {round((time.time() - t0) / 60)} min", "",
         f"- Départs des baisses v16 : {sd}", f"- Départs témoins (tirés au hasard) : {stt}",
         f"- Options : {opts('strict')} -> {opts('margin')}", "",
         "## Pertes de plus de 3 points avec la marge (toutes)", "", "| départ | durée/allure | strict | marge |", "|---|---|---|---|"]
    for r, k, a, b in sorted(dd + dt, key=lambda x: x[3] - x[2]):
        if b - a < -3:
            L.append(f"| {r['id']} | {k} | {a} | {b} |")
    L += ["", "## Plus forts gains", "", "| départ | durée/allure | strict | marge |", "|---|---|---|---|"]
    for r, k, a, b in sorted(dd + dt, key=lambda x: x[2] - x[3])[:25]:
        L.append(f"| {r['id']} | {k} | {a} | {b} |")
    Path(out_md).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L), flush=True)


# ----------------------------------------------------------------------------- baisses v16 (07/10/2026)
def drops_rebuild(gh, st_, c):
    """Boucle recommandée v15 recalculée par ses points de passage (comme KEEP_PREVIOUS) et contrôlée filtre par filtre."""
    way = [list(p) for p in c["way"]]
    way[0] = way[-1] = [st_["lon"], st_["lat"]]
    path = gh.via(way, c["profile"], pass_through=True)
    loop = g.analyse(path, c["level"], c["profile"], c["duration_h"], 2000, None) if path else None
    if loop is None:
        return {"rebuilt": False, "fails": ["non recalculée"]}
    target = c["duration_h"] * 3600.0
    fails = []
    if abs(loop.time_s / target - 1.0) > g.TIME_TOLERANCE:
        fails.append("durée hors tolérance")
    elif not g.time_ok(loop.time_s, c["duration_h"]):
        fails.append("plage de durée")
    if not g.overlap_ok(loop):
        fails.append("répétition")
    if loop.u_turns > g.MAX_UTURNS:
        fails.append("demi-tours")
    if not g.dirt_ok(loop):
        fails.append("terre")
    return {"rebuilt": True, "fails": fails, "min": round(loop.time_s / 60), "score_v16": loop.score,
            "score_v15_weights": g.score_from(loop, g.RELIEF_WEIGHTS.get(loop.level), lights_weight=0.22,
                                              ind_penalty=0.5, ind_max=0.15),
            "dirt_km": round(loop.shares["unpaved"] * loop.distance_m / 1000.0, 1), "overlap": round(loop.overlap, 2),
            "u_turns": loop.u_turns, "spurs": len(loop.spurs), "lights_km": None if loop.signals is None else round(loop.signals / max(loop.distance_m / 1000.0, 0.1), 2)}


def drops_best(gh, st_, c):
    """Recommandée produite pour ce cas (réglages globaux du moment), sans les autres allures (prior vide)."""
    pool = g.level_pool(gh, st_, c["level"], c["duration_h"], g.CANDIDATES, lambda *_: None)
    pool = pool + g.retouch_candidates(gh, st_, c["level"], c["duration_h"], pool, lambda *_: None)
    picks = g.choose_options(gh, st_, c["level"], c["duration_h"], pool, [], lambda *_: None)
    if not picks:
        return None
    l = picks[0][1]
    return {"score": l.score, "min": round(l.time_s / 60), "key": g.route_key(l.coords)}


def run_drops(cases_file, site, gh_url, workers):
    """Baisses de note v15 -> v16 > 10 points : la boucle v15 passe-t-elle encore les filtres ? Que donne la v16 avec et
    sans les plages de durée (DURATION_BINS) ?"""
    cases = json.loads(Path(cases_file).read_text(encoding="utf-8"))
    idx = requests.get(f"{site}/web/data/index.json", timeout=60).json()
    entries = {e["id"]: e for e in idx["starts"]}
    by = {}
    for c in cases:
        by.setdefault(c["id"], []).append(c)
    ctx = {}

    def setup(sid):
        gh = GH(gh_url)
        e = entries[sid]
        snapped = gh.nearest(e["lat"], e["lon"])
        st_ = {"name": e["name"], "lon": snapped[0], "lat": snapped[1], "previous": {}}
        for c in by[sid]:
            st_["previous"].setdefault((c["level"], round(c["duration_h"] * 60)), []).append((c["profile"], c["way"]))
        ctx[sid] = (gh, st_)
        for c in by[sid]:
            c["zone"] = e.get("zone")
            c["rebuild"] = drops_rebuild(gh, st_, c)

    def phase(key):
        def one(sid):
            gh, st_ = ctx[sid]
            for c in by[sid]:
                c[key] = drops_best(gh, st_, c)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(one, list(by)))

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(setup, list(by)))
    phase("v16")
    bins = g.DURATION_BINS
    g.DURATION_BINS = False                                 # globales partagées : une phase à la fois
    phase("no_bins")
    g.DURATION_BINS = bins
    for c in cases:
        c.pop("way", None)
    return cases


def report_drops(cases, out_json, out_md, t0):
    Path(out_json).write_text(json.dumps(cases, ensure_ascii=False, indent=1), encoding="utf-8")
    fails = Counter(f for c in cases for f in c["rebuild"]["fails"])
    ok = [c for c in cases if c["rebuild"]["rebuilt"] and not c["rebuild"]["fails"]]
    sc = lambda c, k: (c.get(k) or {}).get("score", 0.0)  # noqa: E731
    L = [f"# Baisses de note v15 -> v16 (> 10 points) : {len(cases)} cas — {round((time.time() - t0) / 60)} min", "",
         "## La boucle v15 recalculée passe-t-elle les filtres v16 ?", "",
         f"Filtres qui l'écartent : {dict(fails)} ; elle passe tous les filtres dans {len(ok)} cas.", "",
         f"Quand elle passe : note v16 de la boucle v15 en moyenne "
         f"{sum(c['rebuild']['score_v16'] for c in ok) / max(1, len(ok)):.1f} (avec les poids v15 : "
         f"{sum(c['rebuild']['score_v15_weights'] for c in ok) / max(1, len(ok)):.1f}) ; "
         f"note v15 publiée {sum(c['v15_score'] for c in ok) / max(1, len(ok)):.1f}.", "",
         "## Sans les plages de durée", "",
         f"Recommandée moyenne : v15 publiée {sum(c['v15_score'] for c in cases) / len(cases):.1f} ; v16 publiée "
         f"{sum(c['v16_score'] for c in cases) / len(cases):.1f} ; v16 refaite {sum(sc(c, 'v16') for c in cases) / len(cases):.1f}"
         f" ; v16 sans plages {sum(sc(c, 'no_bins') for c in cases) / len(cases):.1f}.", "",
         "| départ | durée | allure | v15 | v16 publiée | v16 refaite | sans plages | v15 recalculée (note v16, filtres) |",
         "|---|---|---|---|---|---|---|---|"]
    for c in sorted(cases, key=lambda c: c["v16_score"] - c["v15_score"]):
        r = c["rebuild"]
        rb = (f"{r['score_v16']} ({r['min']} min{', ' + ', '.join(r['fails']) if r['fails'] else ''})"
              if r["rebuilt"] else "non recalculée")
        L.append(f"| {c['id']} | {c['duration_h']:g} h | {c['level']} | {c['v15_score']} | {c['v16_score']} | "
                 f"{sc(c, 'v16')} | {sc(c, 'no_bins')} | {rb} |")
    Path(out_md).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L), flush=True)


# ----------------------------------------------------------------------------- test du rattrapage (O-38, 07/10/2026)
def run_gate_test(sid, site, gh_url, durations, levels, gate):
    """Mêmes départs produits deux fois : comme la production (tirages ciblés partout) et avec les tirages ciblés seulement
    en rattrapage (meilleure boucle ordinaire < gate). Grâce à la mémoire des itinéraires, le 2e passage ne refait presque
    aucune requête. Compare les options choisies."""
    idx = requests.get(f"{site}/web/data/index.json", timeout=60).json()
    entry = next((e for e in idx["starts"] if e["id"] == sid), None)
    if entry is None:
        return {"id": sid, "skipped": "départ absent de l'index publié"}
    pub = requests.get(f"{site}/web/data/starts/{sid}.json", timeout=60).json()
    gh = GH(gh_url)
    snapped = gh.nearest(entry["lat"], entry["lon"])
    st_ = {"name": entry["name"], "lon": snapped[0], "lat": snapped[1], "previous": {}}
    for o in pub["options"]:
        st_["previous"].setdefault((o["level"], round(o["duration_target_min"])), []).append(
            (o.get("profile") or g.LEVELS[o["level"]]["profiles"][0], o["coords"]))
    rows = []
    for d in durations:
        prior = {"A": [], "B": []}
        for level in sorted(levels, key=list(g.LEVELS).index):
            out = {}
            for k, gt in (("A", None), ("B", gate)):
                logs = []
                c0, t0 = gh.calls, time.time()
                pool = g.level_pool(gh, st_, level, d, g.CANDIDATES, logs.append, gate=gt)
                pool = pool + g.retouch_candidates(gh, st_, level, d, pool, lambda *_: None)
                picks = g.choose_options(gh, st_, level, d, pool, prior[k], lambda *_: None)
                prior[k] += [l for _, l in picks]
                out[k] = {"picks": [(lab, round(l.score, 1), g.route_key(l.coords)) for lab, l in picks],
                          "skipped": any("tirages ciblés sautés" in x for x in logs),
                          "calls": gh.calls - c0, "s": round(time.time() - t0, 1)}
            same = [p[2] for p in out["A"]["picks"]] == [p[2] for p in out["B"]["picks"]]
            rows.append({"duration_h": d, "level": level, "skipped": out["B"]["skipped"], "same": same,
                         "A": out["A"]["picks"], "B": out["B"]["picks"]})
    return {"id": sid, "rows": rows}


def report_gate_test(results, out_json, out_md, gate, t0):
    Path(out_json).write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    rows = [(r["id"], x) for r in results if not r.get("skipped") for x in r["rows"]]
    sk = [x for _, x in rows if x["skipped"]]
    L = [f"# Test du rattrapage : tirages ciblés seulement si la meilleure boucle ordinaire note moins de {gate:g} — "
         f"{round((time.time() - t0) / 60)} min", "",
         f"Durées × allures testées : {len(rows)} ; rattrapage non déclenché (tirages ciblés sautés) : {len(sk)}.",
         f"Parmi celles-ci, options choisies **identiques** (même recommandée, mêmes secondaires, même tracé) : "
         f"**{sum(x['same'] for x in sk)} sur {len(sk)}**.",
         f"Cas où le rattrapage s'est déclenché : {len(rows) - len(sk)} ; identiques : "
         f"{sum(x['same'] for _, x in rows if not x['skipped'])}.", "",
         "## Différences quand les tirages ciblés sont sautés", "",
         "| départ | durée | allure | v16 (recommandée, note) | rattrapage (recommandée, note) | écart de la recommandée |",
         "|---|---|---|---|---|---|"]
    for sid, x in rows:
        if x["skipped"] and not x["same"]:
            a, b = x["A"][0] if x["A"] else ("-", 0, ""), x["B"][0] if x["B"] else ("-", 0, "")
            L.append(f"| {sid} | {x['duration_h']:g} h | {x['level']} | {a[0]} {a[1]} | {b[0]} {b[1]} | "
                     f"{'même tracé' if a[2] == b[2] else f'{b[1] - a[1]:+.1f}'} |")
    Path(out_md).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L), flush=True)


# ----------------------------------------------------------------------------- mesure du coût par type de tirage (O-38)
PHASES: dict = {}
_PH_LOCK = __import__("threading").Lock()


def _timed(name, fn, gh_pos):
    """Enveloppe une fonction de génération : temps et requêtes GraphHopper cumulés dans PHASES[name]."""
    def wrap(*a, **k):
        gh = a[gh_pos] if len(a) > gh_pos else None
        c0, t0 = getattr(gh, "calls", 0), time.time()
        try:
            return fn(*a, **k)
        finally:
            with _PH_LOCK:
                e = PHASES.setdefault(name, [0, 0, 0.0])
                e[0] += 1
                e[1] += getattr(gh, "calls", 0) - c0
                e[2] += time.time() - t0
    return wrap


def install_profiling():
    for name in ("fit_and_sample", "target_candidates", "outback_candidates", "ar_candidates", "lieu_tour_candidates",
                 "previous_candidates", "retouch_candidates", "trim_spurs"):
        if hasattr(g, name):
            setattr(g, name, _timed(name, getattr(g, name), 0))


def profiling_report() -> list:
    tot_t = sum(v[2] for v in PHASES.values()) or 1.0
    tot_c = sum(v[1] for v in PHASES.values()) or 1
    L = ["\n## Coût par type de tirage (temps cumulé, tous fils confondus)", "",
         "| type | appels | requêtes GraphHopper | part des requêtes | temps (min) | part du temps |", "|---|---|---|---|---|---|"]
    for k, (n, c, t) in sorted(PHASES.items(), key=lambda kv: -kv[1][2]):
        L.append(f"| {k} | {n} | {c} | {100 * c / tot_c:.0f} % | {t / 60:.1f} | {100 * t / tot_t:.0f} % |")
    return L


# ----------------------------------------------------------------------------- sonde « pénalités » (05/10/2026)
# Florent : « Plus de pistes » par le port revenue en v15 (3,2 feux/km, 20 % de zone portuaire) ; feux et zones
# industrielles plus pénalisés ? Mêmes candidats, notés avec chaque réglage, options choisies comme la production.
PENALTY_VARIANTS = (("actuel", {}), ("feux 0,30", {"lights_weight": 0.30}),
                    ("industriel x2", {"ind_penalty": 1.0, "ind_max": 0.30}),
                    ("feux 0,30 + industriel x2", {"lights_weight": 0.30, "ind_penalty": 1.0, "ind_max": 0.30}))


def penalty_row(l, label, sid):
    o = g.to_json(l, label, sid, 1)
    r = option_row(o)
    r["main_roads_pct"] = round(100 * o["shares"]["main_roads"])
    r["industrial"] = round((o.get("scenery") or {}).get("industrial", 0.0), 3)
    return r


def run_penalty(sid, site, gh_url, durations, levels):
    idx = requests.get(f"{site}/web/data/index.json", timeout=60).json()
    entry = next((e for e in idx["starts"] if e["id"] == sid), None)
    if entry is None:
        return {"id": sid, "skipped": "départ absent de l'index publié"}
    gh = GH(gh_url)
    snapped = gh.nearest(entry["lat"], entry["lon"])
    st_ = {"name": entry["name"], "lon": snapped[0], "lat": snapped[1]}
    rows = []
    for d in durations:
        for level in levels:
            pool = g.level_pool(gh, st_, level, d, g.CANDIDATES, lambda *_: None)
            row = {"duration_h": d, "level": level, "variants": {}}
            for name, kw in PENALTY_VARIANTS:
                for l in pool:
                    l.score = g.score_from(l, g.RELIEF_WEIGHTS.get(l.level), **kw)
                row["variants"][name] = [penalty_row(l, lab, sid) for lab, l in g.pick_options(pool)]
            for l in pool:
                l.score = g.score(l)
            rows.append(row)
    return {"id": sid, "name": entry.get("municipality", "") + " · " + entry["name"], "rows": rows}


def report_penalty(results, out_json, out_md, note, t0):
    import statistics as stt
    Path(out_json).write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    ok = [r for r in results if not r.get("skipped")]
    L = [f"# Sonde « pénalités » : feux et zones industrielles — {round((time.time() - t0) / 60)} min",
         (f"\n**{note}**" if note else ""),
         "\nMêmes candidats, notés avec chaque réglage. « Masquée par l'appli » = option secondaire que le filtre de l'appli "
         "(05/10/2026) cacherait : +25 % de feux et au moins +0,4 / km, ou 10 % de zone industrielle.",
         "\n| Réglage | feux/km médian (recommandée) | moyen | industriel moyen | recommandées ≥ 10 % industriel | D+ médian "
         "| eau médiane | forêt médiane | routes princ. médiane | km médian | secondaires | masquées par l'appli |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for name, _ in PENALTY_VARIANTS:
        mains = [row["variants"][name][0] for r in ok for row in r["rows"] if row["variants"].get(name)]
        secs = [(row["variants"][name][0], o) for r in ok for row in r["rows"] if row["variants"].get(name)
                for o in row["variants"][name][1:]]
        if not mains:
            continue
        med = lambda k: stt.median([o[k] for o in mains if o.get(k) is not None])  # noqa: E731
        lk = [o["lights_km"] for o in mains if o.get("lights_km") is not None]
        hid = sum(1 for m, o in secs if o["industrial"] >= 0.10 or (
            o.get("lights_km") is not None and m.get("lights_km") is not None and o["lights_km"] >= 1.25 * m["lights_km"]
            and o["lights_km"] - m["lights_km"] >= 0.4))
        L.append(f"| {name} | {med('lights_km'):.2f} | {stt.mean(lk):.2f} | {100 * stt.mean(o['industrial'] for o in mains):.1f} % | "
                 f"{sum(o['industrial'] >= 0.10 for o in mains)}/{len(mains)} | {med('dplus_m'):.0f} m | {med('water_pct'):.0f} % | "
                 f"{med('forest_pct'):.0f} % | {med('main_roads_pct'):.0f} % | {med('km'):.1f} | {len(secs)} | {hid} |")
    changed = sum(1 for r in ok for row in r["rows"] if row["variants"].get("actuel") and
                  row["variants"].get("feux 0,30 + industriel x2") and
                  row["variants"]["actuel"][0]["km"] != row["variants"]["feux 0,30 + industriel x2"][0]["km"])
    L.append(f"\nBoucles recommandées qui changent entre « actuel » et « feux 0,30 + industriel x2 » : {changed} sur "
             f"{sum(len(r['rows']) for r in ok)}.")
    Path(out_md).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L), flush=True)


# ----------------------------------------------------------------------------- sonde « relief » (O-18, option B)
RELIEF_VARIANTS = (0.10, 0.20, 0.30, 0.40)   # poids du relief en sportif ; 0,10 = réglage actuel (RELIEF_WEIGHTS, 03/10/2026)


def relief_row(l, label, sid, modere_cells):
    o = g.to_json(l, label, sid, 1)
    r = option_row(o)
    r["dplus_per_km"] = round(l.dplus_per_km, 1)
    r["main_roads_pct"] = round(100 * o["shares"]["main_roads"])
    r["same_as_modere"] = max((len(l.cells & c) / max(1, min(len(l.cells), len(c))) for c in modere_cells), default=0) >= 0.8
    return r


def run_relief(sid, site, gh_url, durations):
    """Pour chaque durée : candidats du niveau sportif calculés UNE fois, puis options choisies avec chaque poids du relief
    (RELIEF_VARIANTS) ; comparées aux options du niveau modéré (réglage actuel) pour mesurer les doublons d'allure."""
    idx = requests.get(f"{site}/web/data/index.json", timeout=60).json()
    entry = next((e for e in idx["starts"] if e["id"] == sid), None)
    if entry is None:
        return {"id": sid, "skipped": "départ absent de l'index publié"}
    gh = GH(gh_url)
    snapped = gh.nearest(entry["lat"], entry["lon"])
    st_ = {"name": entry["name"], "lon": snapped[0], "lat": snapped[1]}
    rows = []
    for d in durations:
        mod = [l for _, l in g.pick_options(g.level_pool(gh, st_, "modere", d, g.CANDIDATES, lambda *_: None))]
        pool = g.level_pool(gh, st_, "soutenu", d, g.CANDIDATES, lambda *_: None)
        row = {"duration_h": d, "valid": len(pool), "modere_main": None, "variants": {}}
        if mod:
            m = g.to_json(mod[0], "equilibre", sid, 1)
            row["modere_main"] = {"km": m["distance_km"], "dplus_m": m["ascend_m"]}
        for w in RELIEF_VARIANTS:
            for l in pool:
                l.score = g.score_from(l, w)
            picks = g.pick_options(pool)
            row["variants"][f"{w:g}"] = [relief_row(l, lab, sid, [x.cells for x in mod]) for lab, l in picks]
        for l in pool:                                     # remise au réglage actuel
            l.score = g.score(l)
        rows.append(row)
    return {"id": sid, "name": entry.get("municipality", "") + " · " + entry["name"], "zone": entry.get("zone"), "rows": rows}


def report_relief(results, out_json, out_md, note, t0):
    import statistics as stt
    Path(out_json).write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    ok = [r for r in results if not r.get("skipped")]
    L = [f"# Sonde « relief » en niveau sportif (O-18, option B) — {round((time.time() - t0) / 60)} min",
         (f"\n**{note}**" if note else ""),
         "\nMêmes candidats, options choisies avec plusieurs poids du relief dans la note (0 = réglage actuel). "
         "« Doublon » = option qui recouvre à 80 % ou plus une option du niveau modéré.",
         "\n| Poids relief | D+ médian de l'équilibrée | D+/km médian | forêt médiane | ville médiane | routes principales médiane "
         "| note (réglage actuel) médiane | équilibrée = modéré | options en doublon | feux/km médian | feux/km moyen |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    for w in RELIEF_VARIANTS:
        k = f"{w:g}"
        mains = [row["variants"][k][0] for r in ok for row in r["rows"] if row["variants"].get(k)]
        allo = [o for r in ok for row in r["rows"] for o in row["variants"].get(k, [])]
        if not mains:
            continue
        med = lambda key: stt.median([o[key] for o in mains if o.get(key) is not None])  # noqa: E731
        L.append(f"| {k} | {med('dplus_m'):.0f} m | {med('dplus_per_km'):.1f} | {med('forest_pct'):.0f} % | {med('city_pct'):.0f} % | "
                 f"{med('main_roads_pct'):.0f} % | {med('score'):.1f} | {sum(o['same_as_modere'] for o in mains)}/{len(mains)} | "
                 f"{sum(o['same_as_modere'] for o in allo)}/{len(allo)} | {med('lights_km'):.2f} | "
                 f"{stt.mean([o['lights_km'] for o in mains if o.get('lights_km') is not None]):.2f} |")
    L.append("\n## Détail : boucle « équilibrée » sportive selon le poids du relief")
    for r in results:
        if r.get("skipped"):
            L.append(f"\n- {r['id']} : ignoré ({r['skipped']})")
            continue
        L.append(f"\n**{r['name']}** ({r.get('zone')})")
        for row in r["rows"]:
            mm = row["modere_main"]
            parts = []
            for w in RELIEF_VARIANTS:
                v = row["variants"].get(f"{w:g}")
                if v:
                    o = v[0]
                    parts.append(f"{w:g} → {o['km']} km, {o['dplus_m']} m, forêt {o['forest_pct']} %, routes pr. {o['main_roads_pct']} %"
                                 + (" (= modéré)" if o["same_as_modere"] else ""))
            L.append(f"- {row['duration_h']:g} h" + (f" (modéré : {mm['km']} km, {mm['dplus_m']} m)" if mm else "") + " : " + " ; ".join(parts))
    Path(out_md).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L[:30]), flush=True)


def probe_summary(results) -> list:
    """Par niveau : D+ et routes principales médians de la boucle principale, doublons stricts avec l'allure inférieure,
    publié contre nouveau (O-18)."""
    import statistics as stt

    def dup(a, b):
        a, b = {tuple(x) for x in a["cells"]}, {tuple(x) for x in b["cells"]}
        return len(a & b) / max(1, len(a), len(b)) >= g.LEVEL_DUP_SIM
    order = list(g.LEVELS)
    L = ["\n| Niveau | D+ médian principale (publié -> nouveau) | routes principales médiane | options secondaires en doublon "
         "avec l'allure inférieure |", "|---|---|---|---|"]
    for lvl in order:
        rows = [(res, r) for res in results if not res.get("skipped") for r in res["rows"] if r["level"] == lvl]
        if not rows:
            continue
        stat = {}
        for k in ("published", "new"):
            mains = [r[k][0] for _, r in rows if r[k]]
            dups = tot = 0
            if lvl != order[0]:
                lower = order[order.index(lvl) - 1]
                for res, r in rows:
                    low = next((x for x in res["rows"] if x["level"] == lower and x["duration_h"] == r["duration_h"]), None)
                    for o in r[k][1:]:
                        tot += 1
                        dups += bool(low and any(dup(o, b) for b in low[k]))
            stat[k] = (stt.median([m["dplus_m"] for m in mains]) if mains else 0,
                       stt.median([m["main_roads_pct"] for m in mains]) if mains else 0, dups, tot)
        p, n = stat["published"], stat["new"]
        n_ext = sum(1 for _, r in rows for o in r["new"] if o.get("same_as"))
        L.append(f"| {lvl} | {p[0]:.0f} -> {n[0]:.0f} m | {p[1]:.0f} -> {n[1]:.0f} % | "
                 + (f"{p[2]}/{p[3]} -> {n[2]}/{n[3]}" if lvl != order[0] else "—")
                 + (f" (dont {n_ext} gardée(s) : même parcours, aucune autre boucle)" if n_ext else "") + " |")
    return L


def remarkable_summary(results) -> list:
    """Lieux remarquables (01/10/2026) : boucles qui y passent, publié contre nouveau, et lieux les plus visités."""
    from collections import Counter
    rows = [r for res in results if not res.get("skipped") for r in res["rows"]]
    new = [o for r in rows for o in r["new"]]
    if not new:
        return []
    pub_hits = sum(1 for r in rows for o in r["published"] if o.get("remarkable"))
    new_hits = sum(1 for o in new if o.get("remarkable"))
    c = Counter(n for o in new for n in o.get("remarkable") or [])
    L = ["\n## Lieux remarquables (sommets, belvédères, cols avec un article Wikipédia)",
         f"\nOptions passant par au moins un lieu : nouveau **{new_hits} sur {len(new)}** (publié : {pub_hits}, sans relevé "
         "avant le générateur 12 : 0 attendu).",
         f"\nOptions passant devant au moins un belvédère (à moins de ~100 m) : **{sum(1 for o in new if o.get('views'))} sur "
         f"{len(new)}**.", "\nLieux les plus visités : " + ", ".join(f"{n} ({k})" for n, k in c.most_common(12))]
    return L


def targeted_summary(results) -> list:
    """Tirages ciblés (30/09/2026) : combien d'options retenues en viennent, et ce qu'elles changent (note, eau)."""
    import statistics as stt
    rows = [(res, r) for res in results if not res.get("skipped") for r in res["rows"]]
    new = [o for _, r in rows for o in r["new"]]
    if not new:
        return []
    tg = [o for o in new if o.get("targeted")]
    mains = [r["new"][0] for _, r in rows if r["new"]]
    L = ["\n## Tirages ciblés (mer, grande rivière, grand espace vert)",
         f"\nOptions retenues venant d'un tirage ciblé : **{len(tg)} sur {len(new)}** (dont boucle recommandée : "
         f"{sum(1 for m in mains if m.get('targeted'))} sur {len(mains)}).",
         "\n| Départ | options ciblées retenues | exemples |", "|---|---|---|"]
    for res in results:
        if res.get("skipped"):
            continue
        t = [(r["duration_h"], r["level"], o) for r in res["rows"] for o in r["new"] if o.get("targeted")]
        if t:
            L.append(f"| {res['id']} | {len(t)} | " + " ; ".join(
                f"{d:g} h {lv} {o['label']} (note {o['score']}, eau {o['water_pct']} %)" for d, lv, o in t[:3]) + " |")
    pub = [r["published"][0] for _, r in rows if r["published"]]
    if pub and mains:
        L.append(f"\nBoucle recommandée, bord d'eau médian : publié {stt.median(o['water_pct'] for o in pub):.0f} % -> "
                 f"nouveau {stt.median(o['water_pct'] for o in mains):.0f} %.")
    return L


def retouch_summary(results) -> list:
    """Retouche des meilleures boucles (02/10/2026) : gain de note de la boucle recommandée et temps de calcul en plus."""
    import statistics as stt
    rows = [r for res in results if not res.get("skipped") for r in res["rows"] if r.get("base_best") is not None]
    if not RETOUCH or not rows:
        return []
    gains = [r["new"][0]["score"] - r["base_best"] for r in rows if r["new"] and r["new"][0].get("retouched")]
    rt = sum(r["retouch_s"] for r in rows)
    L = ["\n## Retouche des meilleures boucles",
         f"\nBoucle recommandée venant d'une retouche : **{len(gains)} sur {len(rows)}** (durée × allure) ; gain de note "
         f"médian {stt.median(gains) if gains else 0:.1f}, max {max(gains, default=0):.1f}.",
         f"\nOptions retenues venant d'une retouche : {sum(1 for r in rows for o in r['new'] if o.get('retouched'))} sur "
         f"{sum(len(r['new']) for r in rows)}.",
         f"\nTemps de calcul de la retouche : {rt / 60:.1f} min au total (cumul des {len(rows)} calculs, tous fils confondus).",
         "\n| Départ | durée | allure | meilleure note sans retouche | avec |", "|---|---|---|---|---|"]
    for res in results:
        if res.get("skipped"):
            continue
        for r in res["rows"]:
            if r["new"] and r["new"][0].get("retouched"):
                L.append(f"| {res['id']} | {r['duration_h']:g} h | {r['level']} | {r['base_best']} | {r['new'][0]['score']} |")
    return L


def surface_summary(results) -> list:
    """Sonde « revêtement » (28/09/2026) : km de terre (notée ou pistes douteuses), publié contre nouveau, par durée."""
    import statistics as stt
    rows = [r for res in results if not res.get("skipped") for r in res["rows"]]
    if not rows or not any(o.get("surface") for r in rows for o in r["new"]):
        return []
    L = ["\n## Chemins de terre (méthode du diagnostic sur OSM, boucle principale ; publié -> nouveau)",
         "\n| Durée | km de terre médian | boucles ≥ 1 km | boucles ≥ 12 % de la distance | km médian | boucles sans option |",
         "|---|---|---|---|---|---|"]
    for d in sorted({r["duration_h"] for r in rows}):
        rs = [r for r in rows if r["duration_h"] == d]
        cell = {}
        for k in ("published", "new"):
            mains = [r[k][0] for r in rs if r[k] and r[k][0].get("surface")]
            dk = [m["surface"]["dirt_km"] for m in mains]
            cell[k] = (stt.median(dk) if dk else 0, sum(x >= 1 for x in dk), sum(m["surface"]["dirt_km"] >= 0.12 * m["km"] for m in mains),
                       stt.median([m["km"] for m in mains]) if mains else 0, len(mains))
        p, n = cell["published"], cell["new"]
        L.append(f"| {d:g} h | {p[0]:.1f} -> {n[0]:.1f} | {p[1]}/{p[4]} -> {n[1]}/{n[4]} | {p[2]} -> {n[2]} | "
                 f"{p[3]:.1f} -> {n[3]:.1f} | {sum(1 for r in rs if not r['new'])} |")
    return L


def report_probe(results, out_json, out_md, note, t0):
    Path(out_json).write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    f = lambda o: (f"{o['label']}{' (= ' + o['same_as']['level'] + ')' if o.get('same_as') else ''} : {o['km']} km, D+ {o['dplus_m']}, "  # noqa: E731
                   f"forêt {o['forest_pct']} %, ville {o['city_pct']} %, pistes {o['cycleway_pct']} %, note {o['score']}"
                   + (" [REPLI]" if o.get("fallback") else "") + (" [CIBLÉ]" if o.get("targeted") else "")
                   + (" [RETOUCHE]" if o.get("retouched") else "")
                   + (" [PAR : " + ", ".join(o["remarkable"]) + "]" if o.get("remarkable") else "")
                   + f", eau {o.get('water_pct')} %"
                   + (f", {o.get('min')} min, feux {o.get('lights_km')}/km, industriel {o.get('industrial_pct')} %" if "min" in o else "")
                   + (f", terre {o['surface']['dirt_km']} km (+ piste qualité 1 {o['surface']['g1_km']} km, sentiers "
                      f"{o['surface']['sentier_km']} km)" if o.get("surface") else ""))
    L = [f"# Sonde : boucles de production avant / après ({round((time.time() - t0) / 60)} min)",
         (f"\n**Réglage de ce run : {note}**" if note else ""),
         "\n« Publié » = boucles en ligne ; « Nouveau » = ce que produirait la génération avec les réglages du dépôt.",
         *probe_summary(results), *retouch_summary(results), *surface_summary(results), *targeted_summary(results),
         *remarkable_summary(results),
         f"\nVoies OSM au revêtement incertain identifiées par GraphHopper (osm_way_id) : {len(g.WAYS_LOG)} ; par classe : "
         + ", ".join(f"{c} {sum(1 for e in g.WAYS_LOG.values() if e['classe'] == c)}"
                     for c in ("piste_q1", "piste_terre_probable", "sentier_hors_ville", "sentier_ville"))]
    for res in results:
        L.append(f"\n## {res.get('name', res['id'])}")
        if res.get("skipped"):
            L.append(f"- ignoré : {res['skipped']}")
            continue
        for r in res["rows"]:
            maxd = lambda os_: max((o["dplus_m"] for o in os_), default=0)  # noqa: E731
            L.append(f"\n**{r['duration_h']:g} h / {r['level']}** — D+ max publié {maxd(r['published'])} m -> nouveau "
                     f"{maxd(r['new'])} m ({r['valid']} candidats valides)")
            L += ["- Publié : " + f(o) for o in r["published"]]
            L += ["- Nouveau : " + f(o) for o in r["new"]]
    Path(out_md).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L[:40]), flush=True)


# ----------------------------------------------------------------------------- boucles proposées par Florent (30/09/2026)
def run_compare(refs_path, site, gh_url, level, duration):
    """Boucles de référence (points de passage) depuis le départ publié, notées comme la production, face à TOUS les
    candidats valides que la production tire pour ce départ : la référence est-elle introuvable (génération) ou mal
    notée (note) ?"""
    refs = json.loads(Path(refs_path).read_text(encoding="utf-8"))
    sid = refs["start"]
    idx = requests.get(f"{site}/web/data/index.json", timeout=60).json()
    entry = next(e for e in idx["starts"] if e["id"] == sid)
    gh = GH(gh_url)
    snapped = gh.nearest(entry["lat"], entry["lon"])
    st_ = {"name": entry["name"], "lon": snapped[0], "lat": snapped[1]}
    here = [[st_["lon"], st_["lat"]]]
    out = {"start": sid, "level": level, "duration_h": duration, "refs": [], "pool": [], "picks": []}
    for ref in refs["loops"]:
        best = None
        for profile in g.LEVELS[level]["profiles"]:
            path = gh.via(here + ref["waypoints"] + here, profile)
            loop = g.analyse(path, level, profile, duration, 800, None) if path else None
            if loop is not None and (best is None or loop.score > best.score):
                best = loop
        out["refs"].append({"name": ref["name"], "loop": summary(best), "error": None if best else (gh.last_error or "pas de boucle"),
                            "uturns_at": [list(u) for u in g.uturn_points(best.coords)] if best else []})
    pool = g.level_pool(gh, st_, level, duration, g.CANDIDATES, lambda *_: None)
    if g.OUTBACK_DRAWS:                                    # détail des allers-retours au bord de l'eau (essai)
        out["outback_trials"] = []
        pool = pool + g.outback_candidates(gh, st_, level, g.LEVELS[level]["profiles"][0], duration, print,
                                           detail=out["outback_trials"])
    if RETOUCH:
        out["retouch_trials"] = []
        pool = pool + g.retouch_candidates(gh, st_, level, duration, pool, print, detail=out["retouch_trials"])
    picks = g.choose_options(gh, st_, level, duration, pool, [], lambda *_: None)
    out["pool"] = sorted((summary(l) for l in pool), key=lambda x: -x["score"])
    out["picks"] = [{"label": lab, **summary(l)} for lab, l in picks]
    return out


def report_compare(res, out_json, out_md, note, t0):
    Path(out_json).write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    cols = ("score", "km", "min", "dplus_m", "lights_per_km", "city_pct", "city_car_pct", "water_pct", "forest_pct",
            "cycleway_pct", "industrial_pct", "dirt_km", "main_roads_pct", "seed")   # seed ≥ 1000 : retouche
    head = "| Boucle | " + " | ".join(("note", "km", "min", "D+", "feux/km", "ville %", "ville avec voitures %", "eau %",
                                         "forêt %", "pistes %", "industriel %", "terre km", "routes princ. %",
                                         "tirage (≥ 900 : ciblé, ≥ 1000 : retouche)")) + " |"
    row = lambda name, x: "| " + name + " | " + " | ".join(str(x.get(c)) for c in cols) + " |"  # noqa: E731
    L = [f"# Boucles de Florent contre la production ({res['start']}, {res['level']}, {res['duration_h']:g} h ; "
         f"{round((time.time() - t0) / 60)} min)", (f"\n**{note}**" if note else ""),
         "\nNote = note de production v10 (la même que pour choisir les boucles publiées). Les références sont recalculées "
         "par GraphHopper à partir de leurs points de passage, depuis le départ Oyan.",
         "\n## Références", "", head, "|---" * (len(cols) + 1) + "|"]
    for r in res["refs"]:
        L.append(row(r["name"], r["loop"]) if r["loop"] else f"| {r['name']} | {r['error']} |")
    for r in res["refs"]:
        if r.get("uturns_at"):
            L.append(f"\n{r['name']} : {len(r['uturns_at'])} demi-tour(s) compté(s) (lat,lon,écart m) "
                     + " ".join(f"{u[1]:.5f},{u[0]:.5f},{u[2]:g}" for u in r["uturns_at"]))
    L += ["\n## Options choisies par la production", "", head, "|---" * (len(cols) + 1) + "|"]
    L += [row(p["label"], p) for p in res["picks"]]
    L += [f"\n## Tous les candidats valides tirés par la production ({len(res['pool'])}), du meilleur au moins bon", "",
          head, "|---" * (len(cols) + 1) + "|"]
    L += [row(f"candidat {k + 1}", p) for k, p in enumerate(res["pool"])]
    if res.get("outback_trials"):
        L += ["\n## Allers-retours au bord de l'eau : détail", "", "```", *res["outback_trials"], "```"]
    if res.get("retouch_trials"):
        L += ["\n## Retouche : détail des essais", "", "```", *res["retouch_trials"], "```"]
    Path(out_md).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L[:30]), flush=True)


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
    ap.add_argument("--note", default="", help="réglage particulier de ce run (ex. rayon « ville » de GraphHopper)")
    ap.add_argument("--probe", default=None, help="sonde : identifiants de départs publiés séparés par ;")
    ap.add_argument("--relief-test", action="store_true", help="relief par allure v17 : avec / sans (sonde --probe)")
    ap.add_argument("--relief-variant", default="v17", help="v17 ou flat (tirages calm_flat, Modéré 4 points par m/km)")
    ap.add_argument("--margin-test", type=float, default=None, help="marge des plages de durée essayée (ex. 0.05)")
    ap.add_argument("--margin-drops", default="", help="départs des baisses v16 (séparés par ;), pour le rapport")
    ap.add_argument("--prev-dir", default="data/v15/starts", help="boucles v15 publiées, un fichier par départ")
    ap.add_argument("--drops", default=None, help="baisses v15 -> v16 : fichier des cas (scripts/diag_v16_drops.json)")
    ap.add_argument("--gate-test", type=float, default=None, help="test du rattrapage : seuil de note (O-38)")
    ap.add_argument("--profile", action="store_true", help="mesure du coût de chaque type de tirage (O-38)")
    ap.add_argument("--v16", action="store_true", help="essai v16 : feux 0,30, industriel x2, options secondaires gardées, "
                    "allers-retours seulement près de l'eau")
    ap.add_argument("--penalty", default=None, help="sonde « pénalités » (feux, zones industrielles) : départs séparés par ;")
    ap.add_argument("--relief", default=None, help="sonde « relief » (O-18 B) : identifiants de départs publiés séparés par ;")
    ap.add_argument("--site", default="https://florentbedouret-lgtm.github.io/velo-loops")
    ap.add_argument("--retouch", action="store_true", help="mesure la retouche des meilleures boucles (RETOUCH)")
    ap.add_argument("--retouch-trials", type=int, default=None, help="essais de retouche par boucle (RETOUCH_MAX_TRIALS)")
    ap.add_argument("--retouch-prefilter", type=float, default=None,
                    help="essai : écarte les retouches dont la durée prévue s'écarte de plus de X (RETOUCH_PREFILTER)")
    ap.add_argument("--retouch-family-cap", type=int, default=None, help="essai : au plus N essais par famille d'abord")
    ap.add_argument("--lieux", action="store_true", help="essai : lieux les plus célèbres visés, points d'accès (REMARKABLE_*)")
    ap.add_argument("--outback", action="store_true", help="essai : allers-retours sur piste acceptés et tirés (OUTBACK_*)")
    ap.add_argument("--retouch-worst-leg", action="store_true", help="essai : tronçon le plus chargé en feux d'abord")
    ap.add_argument("--retouch-dedupe", action="store_true", help="essai : retouches aux mêmes points de passage, une seule")
    ap.add_argument("--lacets", action="store_true", help="essai : les lacets ne comptent pas comme demi-tours (filtre et note)")
    ap.add_argument("--compare-refs", default=None, help="boucles de référence (points de passage) contre la production")
    ap.add_argument("--references", default=None,
                    help="v5 : fichier de boucles de référence (reference_loops.json) ; remplace la comparaison A/B")
    args = ap.parse_args()
    g.GH_MEMO = True                                    # mémoire des itinéraires (essai du 04/10/2026)
    if args.v16:
        g.WEIGHTS = dict(g.WEIGHTS, lights=0.30)
        g.INDUSTRIAL_PENALTY, g.INDUSTRIAL_MAX_PENALTY = 1.0, 0.30
        g.SECONDARY_GUARD = True
        g.OUTBACK_NEAR_KM = 2.0
        g.SPUR_FIX = True
        g.KEEP_PREVIOUS = True
        g.AR_DRAWS = g.LIEU_TOUR = True
        g.DURATION_BINS = g.STEEP_DIRT_PENALTY = True
        g.DIRT_MAX_KM = 4.0
    if args.profile:
        install_profiling()
    if args.lieux:                                      # avant load_pois : les points d'accès sont chargés avec les lieux
        g.REMARKABLE_FAME = g.REMARKABLE_POINTS = True
    t0 = time.time()

    pbf, wd = Path(args.pbf), Path(args.workdir)
    g.SIGNALS = g.load_signals(pbf, wd)
    g.STOPS = g.load_points(pbf, wd, ["n/highway=stop"], "stops")
    g.LANDSCAPE = g.load_landscape(pbf, wd)
    g.POIS = g.load_pois(pbf, wd)                       # lieux remarquables (tirages ciblés, bonus) comme la production
    if g.LANDSCAPE is None:
        sys.exit("paysage OSM non chargé : impossible de repérer les espaces verts")
    global RETOUCH
    RETOUCH = args.retouch
    g.UTURN_LACETS_OK = args.lacets or g.UTURN_LACETS_OK   # v13 : déjà vrai en production
    g.RETOUCH = False          # la retouche est appelée à part (--retouch) pour la mesurer ; pas deux fois via level_pool
    if args.retouch_trials:
        g.RETOUCH_MAX_TRIALS = args.retouch_trials
    if args.retouch_prefilter is not None:
        g.RETOUCH_PREFILTER = args.retouch_prefilter
    g.RETOUCH_DEDUPE = args.retouch_dedupe or g.RETOUCH_DEDUPE
    if args.retouch_family_cap:
        g.RETOUCH_FAMILY_CAP = args.retouch_family_cap
    g.RETOUCH_WORST_LEG_FIRST = args.retouch_worst_leg or g.RETOUCH_WORST_LEG_FIRST
    if args.outback:
        g.OUTBACK_OK = g.OUTBACK_DRAWS = True
    if args.lieux:
        g.REMARKABLE_FAME = g.REMARKABLE_POINTS = True
    if args.compare_refs:
        lvl = args.levels.split()[0]
        res = run_compare(args.compare_refs, args.site, args.gh, lvl, float(args.durations.split()[0]))
        report_compare(res, args.out, args.out_md, args.note, t0)
        return 0
    if args.probe and args.relief_test:
        ids = [x.strip() for x in args.probe.split(";") if x.strip()]
        durations = [float(x) for x in args.durations.split()]
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            results = list(ex.map(lambda sid: run_relief_test(sid, args.site, args.gh, durations, args.levels.split(),
                                                              args.prev_dir, args.relief_variant), ids))
        report_relief_test(results, args.out, args.out_md, t0)
        return 0
    if args.probe and args.margin_test is not None:
        ids = [x.strip() for x in args.probe.split(";") if x.strip()]
        durations = [float(x) for x in args.durations.split()]
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            results = list(ex.map(lambda sid: run_margin(sid, args.site, args.gh, durations, args.levels.split(),
                                                         args.margin_test, args.prev_dir), ids))
        report_margin(results, args.out, args.out_md, args.margin_test,
                      {x.strip() for x in args.margin_drops.split(";") if x.strip()}, t0)
        return 0
    if args.drops:
        report_drops(run_drops(args.drops, args.site, args.gh, args.workers), args.out, args.out_md, t0)
        return 0
    if args.penalty:
        ids = [x.strip() for x in args.penalty.split(";") if x.strip()]
        durations = [float(x) for x in args.durations.split()]
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            results = list(ex.map(lambda sid: run_penalty(sid, args.site, args.gh, durations, args.levels.split()), ids))
        report_penalty(results, args.out, args.out_md, args.note, t0)
        return 0
    if args.relief:
        ids = [x.strip() for x in args.relief.split(";") if x.strip()]
        durations = [float(x) for x in args.durations.split()]
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            results = list(ex.map(lambda sid: run_relief(sid, args.site, args.gh, durations), ids))
        report_relief(results, args.out, args.out_md, args.note, t0)
        return 0
    if args.probe and args.gate_test is not None:
        ids = [x.strip() for x in args.probe.split(";") if x.strip()]
        durations = [float(x) for x in args.durations.split()]
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            results = list(ex.map(lambda sid: run_gate_test(sid, args.site, args.gh, durations, args.levels.split(),
                                                            args.gate_test), ids))
        report_gate_test(results, args.out, args.out_md, args.gate_test, t0)
        return 0
    if args.probe:
        global WAYS
        import landcover
        WAYS = landcover.load_ways(pbf, wd)[:2]
        print(f"Voies OSM chargées (sonde revêtement) : {len(WAYS[1])}", flush=True)
        ids = [x.strip() for x in args.probe.split(";") if x.strip()]
        durations = [float(x) for x in args.durations.split()]
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            results = list(ex.map(lambda sid: run_probe(sid, args.site, args.gh, durations, args.levels.split()), ids))
        report_probe(results, args.out, args.out_md, args.note, t0)
        if PHASES:
            with open(args.out_md, "a", encoding="utf-8") as fh:
                fh.write("\n".join(profiling_report()) + "\n")
            print("\n".join(profiling_report()), flush=True)
        g.write_ways(str(Path(args.out).parent / "surface_ways_probe.csv"))
        return 0
    if args.references:
        refs = json.loads(Path(args.references).read_text(encoding="utf-8"))["loops"]
        print(f"Boucles de référence : {len(refs)}", flush=True)
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            results = list(ex.map(lambda r: run_reference(r, args.gh, args.levels.split()), refs))
        report_references(results, args.out, args.out_md, args.note, t0)
        return 0
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

    def med(rs, key, field):
        vals = [r[key][field] for r in rs if r.get(key) and r[key][field] is not None]
        return round(sorted(vals)[len(vals) // 2]) if vals else None

    def fmt(x):
        if x is None:
            return "—"
        ex_ = x["exit_dense_km"] if x["exit_dense_km"] is not None else "jamais"
        return (f"note {x['score']} (v2 {x['score_v2']}) · ville {x['city_pct']} % · sortie {ex_} · forêt {x['forest_pct']} % · "
                f"D+ {x['dplus_m']} · grandes routes {x['main_roads_pct']} % dont hors ville {x['main_roads_rural_pct']} % · "
                f"répété {x['overlap_pct']} % · feux {x['lights_per_km']}/km")
    L = [f"# Diagnostic O-12 v2 : pourquoi les boucles « vers le vert » perdent ({len(results)} départs, "
         f"{len(rows)} combinaisons, {round((time.time() - t0) / 60)} min)",
         (f"\n**Réglage de ce run : {args.note}**" if args.note else ""),
         f"\nUne boucle « vers le vert » valide existe dans {len(with_b)} combinaisons ; elle devient la meilleure dans "
         f"{report['b_wins']}.",
         f"\nAvec « grands axes » v2, la meilleure boucle de production change dans "
         f"{sum(1 for r in rows if r['A'] and r['A_v2'] and (r['A']['km'], r['A']['dplus_m']) != (r['A_v2']['km'], r['A_v2']['dplus_m']))}"
         f" combinaisons sur {len(rows)} ; part en ville médiane A {med(rows, 'A', 'city_pct')} % -> A v2 {med(rows, 'A_v2', 'city_pct')} %"
         f", forêt {med(rows, 'A', 'forest_pct')} % -> {med(rows, 'A_v2', 'forest_pct')} %.",
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
    for res in results:
        for ref in res.get("reference", []):
            if not any("## Boucle de référence" in x for x in L):
                L.append("\n## Boucle de référence d'un cycliste local (même score que la production)")
            head = f"- **{res['name']}**, {ref['level']} / profil {ref['profile']}"
            if ref.get("error"):
                L.append(f"{head} : calcul impossible ({ref['error'][:150]})")
                continue
            c = ref["compared_to"]
            L.append(f"{head} — {ref['ref']['km']} km, {ref['ref']['min']} min — score actuel : "
                     + ("**la référence bat A**" if ref["ref_beats_a"] else "A reste devant") + " ; score v2 : "
                     + ("**la référence bat A v2**" if ref.get("ref_beats_a_v2") else "A v2 reste devant"))
            L.append(f"  - référence : {fmt(ref['ref'])}")
            if c:
                L.append(f"  - A ({c['duration_h']:g} h) : {fmt(c['A'])}")
                L.append(f"  - A v2 ({c['duration_h']:g} h) : {fmt(c.get('A_v2'))}")
                L.append("  - points référence − A : " + ", ".join(
                    f"{PART_LABELS[k]} {ref['ref']['points'].get(k, 0) - c['A']['points'].get(k, 0):+.1f}" for k in c["A"]["points"]))
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
