#!/usr/bin/env python3
"""
Diagnostic : sensibilité au lissage de la pente, sur des boucles réelles (GraphHopper + SRTM), pas sur les
6 sorties de la montre. Ne publie rien. Réutilise EXACTEMENT le code de production (elevation_profile,
slope_stats, detect_climb_runs, importés depuis generate_loops.py) : aucune divergence possible entre ce
diagnostic et le pipeline réel.

Pour chaque départ échantillonné : UNE requête GraphHopper (round_trip), puis la pente max et la pente
moyenne des montées nettes recalculées à 3 niveaux de lissage (sans lissage / 300 m / 500 m) sur le MÊME
tracé complet (avant toute simplification pour l'affichage). Rapporte, pour une liste de seuils candidats,
si le classement binaire "dépasse le seuil" reste identique aux 3 lissages (seuil "robuste") ou change
selon le lissage choisi (seuil "fragile").
"""
from __future__ import annotations

import argparse
import json
import random
import statistics as st
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
import generate_loops as g  # réutilise elevation_profile / slope_stats / haversine tels quels


def one_route(gh_url: str, lon: float, lat: float, seed: int, distance_m: float):
    body = {"points": [[lon, lat]], "profile": "sport", "points_encoded": False, "elevation": True,
            "instructions": False, "algorithm": "round_trip",
            "round_trip.distance": distance_m, "round_trip.seed": seed}
    try:
        r = requests.post(f"{gh_url}/route", json=body, timeout=40)
        paths = r.json().get("paths") if r.status_code == 200 else None
    except requests.RequestException:
        paths = None
    if not paths:
        return None
    coords = paths[0]["points"]["coordinates"]
    if len(coords) < 20:
        return None
    cum = [0.0]
    for i in range(1, len(coords)):
        cum.append(cum[-1] + g.haversine(coords[i - 1][0], coords[i - 1][1], coords[i][0], coords[i][1]))
    if cum[-1] < 3000:
        return None
    return coords, cum


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--gh", required=True)
    ap.add_argument("--starts", required=True, help="starts_plan.json (échantillonné dedans)")
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--distance-m", type=float, default=18000.0)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--thresholds", default="10,14,18,20,22")
    ap.add_argument("--out", required=True)
    ap.add_argument("--out-md", default=None)
    args = ap.parse_args()

    all_starts = json.loads(Path(args.starts).read_text(encoding="utf-8"))
    rng = random.Random(args.seed)
    by_zone: dict = {}
    for s in all_starts:
        by_zone.setdefault(s.get("zone", "?"), []).append(s)
    picks = []                                        # répartis entre les 3 zones, proportionnellement au plan
    for z, lst in by_zone.items():
        k = max(1, round(args.n * len(lst) / len(all_starts)))
        picks += rng.sample(lst, min(k, len(lst)))
    rng.shuffle(picks)
    picks = picks[: args.n]

    thresholds = [float(x) for x in args.thresholds.split(",") if x.strip()]
    rows = []
    for i, s in enumerate(picks):
        got = one_route(args.gh, s["lon"], s["lat"], seed=1000 + i, distance_m=args.distance_m)
        if got is None:
            continue
        coords, cum = got
        row = {"name": s["name"], "zone": s.get("zone"), "distance_km": round(cum[-1] / 1000.0, 1)}
        for w, lab in ((1, "w1"), (3, "w3"), (5, "w5")):
            ds, prof = g.elevation_profile(coords, cum, window=w)
            stt = g.slope_stats(ds, prof)
            row[lab] = {"max_grade_pct": stt["max_grade_pct"], "avg_climb_grade_pct": stt["avg_climb_grade_pct"],
                        "ascend_m": round(g.gain_loss(prof, threshold=3.0)[0])}
        rows.append(row)
        print(f"- {s['name']} ({s.get('zone')}) : pente max sans lissage/300/500 = "
              f"{row['w1']['max_grade_pct']} / {row['w3']['max_grade_pct']} / {row['w5']['max_grade_pct']} %", flush=True)

    def ratio(sel, key, sub):
        vals = [(r[sel][sub], r["w5"][sub]) for r in rows if r["w5"][sub]]
        return sorted(a / b for a, b in vals if b)

    report = {"routes_sampled": len(picks), "routes_ok": len(rows), "distance_m_target": args.distance_m,
              "rows": rows}
    r1 = ratio("w1", "w5", "max_grade_pct")
    report["max_grade_pct_ratio_w1_over_w5"] = {"median": round(st.median(r1), 2) if r1 else None,
                                                "max": round(max(r1), 2) if r1 else None,
                                                "min": round(min(r1), 2) if r1 else None, "n": len(r1)}
    r2 = ratio("w1", "w5", "avg_climb_grade_pct")
    report["avg_climb_grade_pct_ratio_w1_over_w5"] = {"median": round(st.median(r2), 2) if r2 else None,
                                                       "max": round(max(r2), 2) if r2 else None, "n": len(r2)}

    # robustesse de chaque seuil candidat : le classement "dépasse le seuil" est-il le même aux 3 lissages ?
    thr_report = {}
    for t in thresholds:
        n = len(rows)
        flags = [(r["w1"]["max_grade_pct"] > t, r["w3"]["max_grade_pct"] > t, r["w5"]["max_grade_pct"] > t) for r in rows]
        stable = sum(1 for a, b, c in flags if a == b == c)
        share_over_w5 = sum(1 for r in rows if r["w5"]["max_grade_pct"] > t) / n if n else None
        thr_report[str(t)] = {"routes_where_classification_is_stable_across_smoothing": stable,
                              "routes_where_it_flips": n - stable,
                              "share_stable_pct": round(100 * stable / n, 1) if n else None,
                              "share_of_routes_over_threshold_at_500m": round(100 * share_over_w5, 1) if share_over_w5 is not None else None}
    report["threshold_robustness"] = thr_report
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    if args.out_md:
        L = [f"# Diagnostic pente — {len(rows)} boucles réelles (SRTM), {args.distance_m/1000:g} km"]
        L.append("\n## Sensibilité (rapport sans lissage / 500 m)")
        L.append(f"- Pente max (100 m) : médiane {report['max_grade_pct_ratio_w1_over_w5']['median']}×, max {report['max_grade_pct_ratio_w1_over_w5']['max']}× (n={report['max_grade_pct_ratio_w1_over_w5']['n']})")
        L.append(f"- Pente moyenne des montées nettes : médiane {report['avg_climb_grade_pct_ratio_w1_over_w5']['median']}×, max {report['avg_climb_grade_pct_ratio_w1_over_w5']['max']}× (n={report['avg_climb_grade_pct_ratio_w1_over_w5']['n']})")
        L.append("\n## Robustesse par seuil candidat (badge « au moins une rampe > seuil »)")
        L.append("| Seuil | Classement stable aux 3 lissages | Bascule selon le lissage | Part des boucles au-dessus (500 m) |\n|---|---|---|---|")
        for t, d in thr_report.items():
            L.append(f"| {t} % | {d['share_stable_pct']} % | {d['routes_where_it_flips']} boucles | {d['share_of_routes_over_threshold_at_500m']} % |")
        Path(args.out_md).write_text("\n".join(L) + "\n", encoding="utf-8")
        print("\n".join(L))
    print(f"\n{len(rows)} boucles exploitables sur {len(picks)} échantillonnées", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
