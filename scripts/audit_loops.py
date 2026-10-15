#!/usr/bin/env python3
"""Revue de cohérence des boucles publiées (Florent, 15/10/2026 : « vérifier leur cohérence et me faire remonter les
incohérences »). Lit les données du site (index + un fichier par départ), sans GraphHopper ni recalcul, et classe les
incohérences par gravité, avec un lien vers chaque boucle dans l'app.

    python scripts/audit_loops.py --site https://florentbedouret-lgtm.github.io/velo-loops --out data/audit
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

DURATIONS = (0.75, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 6.0)
LEVELS = ("facile", "modere", "soutenu")
LEVEL_NAME = {"facile": "Tranquille", "modere": "Modéré", "soutenu": "Sportif"}
SPEED_KMH = {"facile": (11.0, 28.0), "modere": (13.0, 32.0), "soutenu": (15.0, 36.0)}   # vitesse moyenne plausible
# gravité : 1 incohérence (contredit une règle ou une promesse), 2 à vérifier, 3 signal
CHECKS = {
    "duree_hors_tolerance": (1, "Durée annoncée à plus de 15 % de la durée visée"),
    "ordre_durees": (1, "Boucle recommandée plus courte que celle de la durée inférieure (même allure)"),
    "doublon_durees": (1, "Même boucle proposée à deux durées de la même allure"),
    "option_identique": (1, "Option secondaire identique à la recommandée"),
    "moins_de_relief_pas_moins": (1, "« Moins de relief » pas nettement moins vallonnée que la recommandée (3 m/km)"),
    "plus_de_relief_pas_plus": (1, "« Plus de relief » pas nettement plus vallonnée que la recommandée (3 m/km)"),
    "tranquille_trop_vallonne": (1, "Tranquille au-dessus de 10 m/km sans repli annoncé"),
    "tranquille_montee_interdite": (1, "Tranquille avec une montée de catégorie 2, 1 ou HC"),
    "tranquille_rampe": (1, "Tranquille avec une rampe de 20 % ou plus"),
    "terre_plus_1km": (1, "Plus de 1 km de terre sans repli annoncé"),
    "repetition": (1, "Plus de 25 % du parcours emprunté deux fois (hors aller-retour annoncé)"),
    "vitesse_implausible": (2, "Vitesse moyenne implicite hors de la plage plausible de l'allure"),
    "demi_tours_hors_regle": (1, "Plus de 2 demi-tours (limite du calcul)"),
    "demi_tours": (3, "1 ou 2 demi-tours (admis : impasses, retours utiles)"),
    "modere_tres_vallonne": (3, "Modéré recommandé au-dessus de 15 m/km (pénalisé, pas interdit)"),
    "duree_manquante": (2, "Durée sans aucune boucle pour une allure"),
    "note_tres_basse": (3, "Boucle recommandée notée sous 25"),
    "feux_nombreux": (3, "Plus de 4 feux par km"),
    "grands_axes": (3, "Plus de 40 % sur des routes principales"),
}


def app_link(site, sid, dur_min, level):
    return f"{site}/?s={sid}&d={dur_min / 60:g}&l={level}"


def fetch(site, sid):
    for _ in range(3):
        try:
            r = requests.get(f"{site}/web/data/starts/{sid}.json", timeout=60)
            if r.status_code == 200:
                return sid, r.json()
        except requests.RequestException:
            pass
    return sid, None


def audit(site):
    idx = requests.get(f"{site}/web/data/index.json", timeout=60).json()
    starts = idx["starts"]
    with ThreadPoolExecutor(max_workers=16) as ex:
        data = dict(ex.map(lambda e: fetch(site, e["id"]), starts))
    names = {e["id"]: e["name"] for e in starts}
    issues = defaultdict(list)
    n_opts, n_reco, stale, missing_files = 0, 0, [], []
    for e in starts:
        sid = e["id"]
        if e.get("stale"):                                   # pas encore recalculé dans la version en cours : à part
            stale.append(sid)
            continue
        d = data.get(sid)
        if d is None:
            missing_files.append(sid)
            continue
        by = defaultdict(list)
        for o in d.get("options", []):
            by[(o["level"], o["duration_target_min"])].append(o)
            n_opts += 1
        for level in LEVELS:
            prev_time, keys = None, {}
            for dur in DURATIONS:
                dm = round(dur * 60)
                opts = by.get((level, dm), [])
                link = app_link(site, sid, dm, level)
                where = f"{names[sid]}, {LEVEL_NAME[level]}, {dur:g} h"
                if not opts:
                    if dur in e.get("durations_tried", DURATIONS):
                        issues["duree_manquante"].append({"ou": where, "lien": link, "valeur": "aucune boucle"})
                    continue
                reco = next((o for o in opts if str(o["id"]).endswith("-1")), opts[0])
                n_reco += 1
                for o in opts:
                    lab = o.get("label", "")
                    ow = f"{where}, « {lab} »"
                    km, dpk = o["distance_km"], o["ascend_m"] / max(o["distance_km"], 0.1)
                    t = o["time_est_min"]
                    if abs(t / dm - 1.0) > 0.15:
                        issues["duree_hors_tolerance"].append({"ou": ow, "lien": link, "valeur": f"{t} min pour {dm}"})
                    v = km / max(t / 60.0, 0.1)
                    lo, hi = SPEED_KMH[level]
                    if not lo <= v <= hi:
                        issues["vitesse_implausible"].append({"ou": ow, "lien": link, "valeur": f"{v:.1f} km/h"})
                    if o.get("u_turns", 0) > 2:
                        issues["demi_tours_hors_regle"].append({"ou": ow, "lien": link, "valeur": f"{o['u_turns']}"})
                    elif o.get("u_turns", 0) > 0:
                        issues["demi_tours"].append({"ou": ow, "lien": link, "valeur": f"{o['u_turns']}"})
                    if o.get("overlap", 0) > 0.25 and not o.get("out_and_back"):
                        issues["repetition"].append({"ou": ow, "lien": link, "valeur": f"{o['overlap']:.0%}"})
                    dirt = o["shares"].get("unpaved", 0) * km
                    if dirt > 1.05 and not o.get("unpaved_fallback"):
                        issues["terre_plus_1km"].append({"ou": ow, "lien": link, "valeur": f"{dirt:.1f} km"})
                    if level == "facile":
                        if dpk > 10.05 and not o.get("relief_fallback"):
                            issues["tranquille_trop_vallonne"].append({"ou": ow, "lien": link, "valeur": f"{dpk:.1f} m/km"})
                        cats = [c.get("category") for c in (o.get("terrain") or {}).get("climbs", [])]
                        bad = [c for c in cats if c in ("2", "1", "HC")]
                        if bad and not o.get("relief_fallback"):
                            issues["tranquille_montee_interdite"].append({"ou": ow, "lien": link, "valeur": ", ".join(bad)})
                        mg = (o.get("terrain") or {}).get("max_grade_pct") or 0
                        if mg >= 20 and not o.get("relief_fallback"):
                            issues["tranquille_rampe"].append({"ou": ow, "lien": link, "valeur": f"{mg:.0f} %"})
                    if o.get("traffic_lights_per_km", 0) > 4:
                        issues["feux_nombreux"].append({"ou": ow, "lien": link, "valeur": f"{o['traffic_lights_per_km']:.1f}/km"})
                    if o["shares"].get("main_roads", 0) > 0.40:
                        issues["grands_axes"].append({"ou": ow, "lien": link, "valeur": f"{o['shares']['main_roads']:.0%}"})
                    if o is not reco:
                        if o.get("route_key") and o.get("route_key") == reco.get("route_key"):
                            issues["option_identique"].append({"ou": ow, "lien": link, "valeur": lab})
                        rdpk = reco["ascend_m"] / max(reco["distance_km"], 0.1)
                        if lab == "moins_de_relief" and dpk > rdpk - 3.0:
                            issues["moins_de_relief_pas_moins"].append({"ou": ow, "lien": link,
                                                                        "valeur": f"{dpk:.1f} contre {rdpk:.1f} m/km"})
                        if lab == "plus_de_relief" and dpk < rdpk + 3.0:
                            issues["plus_de_relief_pas_plus"].append({"ou": ow, "lien": link,
                                                                      "valeur": f"{dpk:.1f} contre {rdpk:.1f} m/km"})
                rdpk = reco["ascend_m"] / max(reco["distance_km"], 0.1)
                if level == "modere" and rdpk > 15.0:
                    issues["modere_tres_vallonne"].append({"ou": where, "lien": link, "valeur": f"{rdpk:.1f} m/km"})
                if reco.get("score", 100) < 25:
                    issues["note_tres_basse"].append({"ou": where, "lien": link, "valeur": f"{reco['score']:.1f}"})
                if prev_time is not None and reco["time_est_min"] <= prev_time:
                    issues["ordre_durees"].append({"ou": where, "lien": link,
                                                   "valeur": f"{reco['time_est_min']} min (durée inférieure : {prev_time})"})
                prev_time = reco["time_est_min"]
                for o in opts:
                    k = o.get("route_key")
                    if k and k in keys and keys[k] != dm:
                        issues["doublon_durees"].append({"ou": where, "lien": link,
                                                         "valeur": f"aussi en {keys[k] / 60:g} h"})
                    if k:
                        keys.setdefault(k, dm)
    return {"generator_version": idx.get("generator_version"), "params_hash": idx.get("params_hash"),
            "starts": len(starts), "options": n_opts, "recommandees": n_reco, "stale": stale,
            "fichiers_manquants": missing_files, "issues": issues}


def report(res, out):
    Path(f"{out}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    L = [f"# Revue de cohérence des boucles publiées (v{res['generator_version']})", "",
         f"{res['starts']} départs, {res['options']} boucles dont {res['recommandees']} recommandées ; "
         f"départs à recalculer : {len(res['stale'])} ; fichiers illisibles : {len(res['fichiers_manquants'])}.", "",
         "| gravité | vérification | cas |", "|---|---|---|"]
    for key, (sev, label) in sorted(CHECKS.items(), key=lambda kv: kv[1][0]):
        L.append(f"| {sev} | {label} | {len(res['issues'].get(key, []))} |")
    for key, (sev, label) in sorted(CHECKS.items(), key=lambda kv: kv[1][0]):
        items = res["issues"].get(key, [])
        if not items:
            continue
        by_level = Counter(next((LEVEL_NAME[l] for l in LEVELS if LEVEL_NAME[l] in i["ou"]), "?") for i in items)
        L += ["", f"## {label} ({len(items)} ; " + ", ".join(f"{k} {v}" for k, v in by_level.items()) + ")", ""]
        for i in items[:15]:
            L.append(f"- [{i['ou']}]({i['lien']}) : {i['valeur']}")
        if len(items) > 15:
            L.append(f"- … et {len(items) - 15} autres (fichier JSON)")
    Path(f"{out}.md").write_text("\n".join(L) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", default="https://florentbedouret-lgtm.github.io/velo-loops")
    ap.add_argument("--out", default="data/audit")
    a = ap.parse_args()
    res = audit(a.site.rstrip("/"))
    report(res, a.out)
    print(f"{res['options']} boucles lues ; " + ", ".join(f"{k} {len(v)}" for k, v in res["issues"].items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
