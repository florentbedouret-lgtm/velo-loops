#!/usr/bin/env python3
"""Revue de cohérence des boucles publiées (Florent, 15/10/2026 : « vérifier leur cohérence et me faire remonter les
incohérences »). Lit les données du site (ou un dossier de données, par exemple une génération précédente), sans
GraphHopper ni recalcul :
  1. défauts visibles (demi-tours, allers-retours, terre, rampes en Tranquille, chemin répété) ;
  2. cohérence des durées (hors plage, durées voisines quasi identiques, ordre, doublons) ;
  3. promesses par allure (relief, grands axes, feux, sortie de ville) ;
  4. options (« moins / plus de relief » vraiment différentes, durées sans option, durées sans boucle) ;
  5. pires boucles par critère, avec leur lien dans l'app ;
  6. comparaison avec une génération précédente (--compare : dossier de données, par exemple la v16).
Les détours en pâté de maisons ne sont pas publiés (tracés simplifiés) : non vérifiables ici.

    python scripts/audit_loops.py --site https://florentbedouret-lgtm.github.io/velo-loops --compare data/v16 --out data/audit
"""
from __future__ import annotations

import argparse
import json
import statistics as st
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import urllib.request

SITE = "https://florentbedouret-lgtm.github.io/velo-loops"
DURATIONS = (0.75, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 6.0)
LEVELS = ("facile", "modere", "soutenu")
LEVEL_NAME = {"facile": "Tranquille", "modere": "Modéré", "soutenu": "Sportif"}
SPEED_KMH = {"facile": (11.0, 28.0), "modere": (13.0, 32.0), "soutenu": (15.0, 36.0)}   # vitesse moyenne plausible
RELIEF_TARGET = {"facile": (5.0, 10.0), "modere": (10.0, 15.0)}                        # m/km (décision D60)
# gravité : 1 incohérence (contredit une règle ou une promesse), 2 à vérifier, 3 signal
CHECKS = {
    # 1. défauts visibles
    "demi_tours_hors_regle": (1, "Défauts", "Plus de 2 demi-tours (limite du calcul)"),
    "terre_plus_1km": (1, "Défauts", "Plus de 1 km de terre sans repli annoncé"),
    "tranquille_rampe": (1, "Défauts", "Tranquille avec une rampe de 20 % ou plus"),
    "repetition": (1, "Défauts", "Plus de 25 % du parcours emprunté deux fois (hors aller-retour annoncé)"),
    "chemin_repete": (2, "Défauts", "Même chemin parcouru deux fois sur plus de 1 km d'affilée, hors aller-retour jugé utile"),
    "aller_retour_long": (2, "Défauts", "Aller-retour jugé utile de plus de 25 % du parcours (au moins 5 km)"),
    "demi_tours": (3, "Défauts", "1 ou 2 demi-tours (admis : impasses, retours utiles)"),
    "allers_retours": (3, "Défauts", "Aller-retour annoncé ou jugé utile (nature, lieu, belvédère, relief, piste)"),
    # 2. durées
    "duree_hors_tolerance": (1, "Durées", "Durée annoncée à plus de 15 % de la durée visée"),
    "ordre_durees": (1, "Durées", "Recommandée plus courte que celle de la durée inférieure (même allure)"),
    "doublon_durees": (1, "Durées", "Même boucle proposée à deux durées de la même allure"),
    "durees_voisines_proches": (2, "Durées", "Recommandées de deux durées voisines à moins de 12 % de durée l'une de l'autre"),
    "vitesse_implausible": (2, "Durées", "Vitesse moyenne implicite hors de la plage plausible de l'allure"),
    # 3. promesses par allure
    "tranquille_trop_vallonne": (1, "Allures", "Tranquille au-dessus de 10 m/km sans repli annoncé"),
    "tranquille_montee_interdite": (1, "Allures", "Tranquille avec une montée de catégorie 2, 1 ou HC"),
    "modere_tres_vallonne": (3, "Allures", "Modéré recommandé au-dessus de 15 m/km (pénalisé, pas interdit)"),
    "grands_axes": (3, "Allures", "Plus de 40 % sur des routes principales"),
    "feux_nombreux": (3, "Allures", "Plus de 4 feux par km"),
    "note_tres_basse": (3, "Allures", "Recommandée notée sous 25"),
    # 4. options
    "option_identique": (1, "Options", "Option secondaire identique à la recommandée"),
    "moins_de_relief_pas_moins": (1, "Options", "« Moins de relief » pas nettement moins vallonnée (3 m/km)"),
    "plus_de_relief_pas_plus": (1, "Options", "« Plus de relief » pas nettement plus vallonnée (3 m/km)"),
    "duree_manquante": (2, "Options", "Durée sans aucune boucle pour une allure"),
    "sans_option": (3, "Options", "Durée avec la seule boucle recommandée (aucune option)"),
}
# pires boucles par critère (valeur, sens : True = plus c'est grand, pire c'est)
RANKINGS = {
    "feux au km": ("lights_km", True), "part de grands axes": ("main_roads", True),
    "part répétée": ("overlap", True), "plus long chemin répété (km)": ("longest_repeat_km", True),
    "demi-tours": ("u_turns", True), "aller-retour jugé utile (km)": ("backtrack_km", True), "terre (km)": ("dirt_km", True), "note de la recommandée": ("score", False),
    "écart à la durée visée": ("time_gap", True), "sortie de ville (km)": ("exit_city_km", True),
}


def app_link(sid, dur_min, level):
    return f"{SITE}/?s={sid}&d={dur_min / 60:g}&l={level}"


def load(source):
    """(index, {départ: données}) depuis l'adresse du site ou un dossier (index.json, starts/)."""
    if source.startswith("http"):
        def get(url):
            with urllib.request.urlopen(url, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        idx = get(f"{source}/web/data/index.json")

        def fetch(sid):
            for _ in range(3):
                try:
                    return sid, get(f"{source}/web/data/starts/{sid}.json")
                except Exception:                            # coupure réseau, lecture incomplète : on réessaie
                    pass
            return sid, None
        with ThreadPoolExecutor(max_workers=16) as ex:
            return idx, dict(ex.map(fetch, [e["id"] for e in idx["starts"]]))
    base = Path(source)
    idx = json.loads((base / "index.json").read_text(encoding="utf-8"))
    data = {}
    for e in idx["starts"]:
        f = base / "starts" / f"{e['id']}.json"
        data[e["id"]] = json.loads(f.read_text(encoding="utf-8")) if f.exists() else None
    return idx, data


def audit(source, only=None):
    """only : identifiants de départs à garder (comparaison sur les mêmes départs)."""
    idx, data = load(source)
    starts = [e for e in idx["starts"] if only is None or e["id"] in only]
    issues, rows = defaultdict(list), []
    stale, missing_files = [], []
    for e in starts:
        sid, name = e["id"], e["name"]
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
        for level in LEVELS:
            prev, keys = None, {}
            for dur in DURATIONS:
                dm = round(dur * 60)
                opts = by.get((level, dm), [])
                link, where = app_link(sid, dm, level), f"{name}, {LEVEL_NAME[level]}, {dur:g} h"

                def add(key, label, value, o=None):
                    issues[key].append({"ou": where + (f", « {o.get('label', '')} »" if o is not None else ""),
                                        "lien": link, "valeur": value, "niveau": level})
                if not opts:
                    if dur in e.get("durations_tried", DURATIONS):
                        add("duree_manquante", "", "aucune boucle")
                    continue
                reco = next((o for o in opts if str(o["id"]).endswith("-1")), opts[0])
                if len(opts) == 1:
                    add("sans_option", "", "recommandée seule")
                rdpk = reco["ascend_m"] / max(reco["distance_km"], 0.1)
                for o in opts:
                    lab, km, t = o.get("label", ""), o["distance_km"], o["time_est_min"]
                    dpk = o["ascend_m"] / max(km, 0.1)
                    dirt = o["shares"].get("unpaved", 0) * km
                    terr = o.get("terrain") or {}
                    bts = o.get("backtracks") or []
                    bt_km = sum(b["m"] for b in bts) / 1000.0         # longueur parcourue (aller et retour)
                    rows.append({"sid": sid, "backtrack_km": bt_km, "oab": bool(o.get("out_and_back")), "name": name, "level": level, "dur": dur, "label": lab, "reco": o is reco,
                                 "link": link, "km": km, "time": t, "time_gap": abs(t / dm - 1.0), "dpk": dpk,
                                 "lights_km": o.get("traffic_lights_per_km") or 0.0,
                                 "main_roads": o["shares"].get("main_roads", 0.0), "overlap": o.get("overlap", 0.0),
                                 "longest_repeat_km": o.get("longest_repeat_km") or 0.0, "u_turns": o.get("u_turns", 0),
                                 "dirt_km": dirt, "score": o.get("score", 0.0), "exit_city_km": o.get("exit_city_km"),
                                 "max_grade": terr.get("max_grade_pct") or 0.0})
                    if t and abs(t - dm) > 0.15 * dm + 1:              # 1 min de marge : durées publiées arrondies
                        add("duree_hors_tolerance", "", f"{t} min pour {dm}", o)
                    v = km / max(t / 60.0, 0.1)
                    if not SPEED_KMH[level][0] <= v <= SPEED_KMH[level][1]:
                        add("vitesse_implausible", "", f"{v:.1f} km/h", o)
                    if o.get("u_turns", 0) > 2:
                        add("demi_tours_hors_regle", "", f"{o['u_turns']}", o)
                    elif o.get("u_turns", 0) > 0:
                        add("demi_tours", "", f"{o['u_turns']}", o)
                    if o.get("out_and_back") or bts:
                        add("allers_retours", "", "aller-retour" if o.get("out_and_back")
                            else ", ".join(f"{b['why']} {b['m'] / 1000:.1f} km" for b in bts), o)
                    if bt_km >= max(5.0, 0.25 * km):
                        add("aller_retour_long", "", f"{bt_km:.1f} km sur {km:.0f} ("
                            + ", ".join(sorted({b['why'] for b in bts})) + ")", o)
                    if o.get("overlap", 0) > 0.25 and not o.get("out_and_back"):
                        add("repetition", "", f"{o['overlap']:.0%}", o)
                    explained = max((b["m"] / 2000.0 for b in bts), default=0.0) + 0.2   # un aller-retour utile répète m/2
                    if (o.get("longest_repeat_km") or 0) > max(1.0, explained) and not o.get("out_and_back"):
                        add("chemin_repete", "", f"{o['longest_repeat_km']:.1f} km", o)
                    if dirt > 1.05 and not o.get("unpaved_fallback"):
                        add("terre_plus_1km", "", f"{dirt:.1f} km", o)
                    if level == "facile" and not o.get("relief_fallback"):
                        if dpk > 10.05:
                            add("tranquille_trop_vallonne", "", f"{dpk:.1f} m/km", o)
                        bad = [c.get("category") for c in terr.get("climbs", []) if c.get("category") in ("2", "1", "HC")]
                        if bad:
                            add("tranquille_montee_interdite", "", ", ".join(bad), o)
                        if (terr.get("max_grade_pct") or 0) >= 20:
                            add("tranquille_rampe", "", f"{terr['max_grade_pct']:.0f} %", o)
                    if (o.get("traffic_lights_per_km") or 0) > 4:
                        add("feux_nombreux", "", f"{o['traffic_lights_per_km']:.1f}/km", o)
                    if o["shares"].get("main_roads", 0) > 0.40:
                        add("grands_axes", "", f"{o['shares']['main_roads']:.0%}", o)
                    if o is not reco:
                        if o.get("route_key") and o.get("route_key") == reco.get("route_key"):
                            add("option_identique", "", lab, o)
                        if lab == "moins_de_relief" and dpk > rdpk - 3.0:
                            add("moins_de_relief_pas_moins", "", f"{dpk:.1f} contre {rdpk:.1f} m/km", o)
                        if lab == "plus_de_relief" and dpk < rdpk + 3.0:
                            add("plus_de_relief_pas_plus", "", f"{dpk:.1f} contre {rdpk:.1f} m/km", o)
                if level == "modere" and rdpk > 15.0:
                    add("modere_tres_vallonne", "", f"{rdpk:.1f} m/km")
                if reco.get("score", 100) < 25:
                    add("note_tres_basse", "", f"{reco['score']:.1f}")
                if prev is not None:
                    if reco["time_est_min"] <= prev[1]:
                        add("ordre_durees", "", f"{reco['time_est_min']} min (durée inférieure : {prev[1]})")
                    elif reco["time_est_min"] < 1.12 * prev[1]:
                        add("durees_voisines_proches", "", f"{reco['time_est_min']} min contre {prev[1]} min en {prev[0]:g} h")
                prev = (dur, reco["time_est_min"])
                for o in opts:
                    k = o.get("route_key")
                    if k and k in keys and keys[k] != dm:
                        add("doublon_durees", "", f"aussi en {keys[k] / 60:g} h", o)
                    if k:
                        keys.setdefault(k, dm)
    return {"generator_version": idx.get("generator_version"), "starts": len(starts), "stale": stale,
            "missing_files": missing_files, "issues": issues, "rows": rows}


def indicators(res):
    """Grands indicateurs par allure (boucles recommandées) pour la comparaison entre générations."""
    out = {}
    for level in LEVELS:
        r = [x for x in res["rows"] if x["level"] == level and x["reco"]]
        if not r:
            continue
        med = lambda k: round(st.median(x[k] for x in r), 2)  # noqa: E731
        town = [x for x in r if x["exit_city_km"] != 0]           # 0 : départ hors de la ville ; None : reste en ville
        ex = [x["exit_city_km"] for x in town if x["exit_city_km"] is not None]
        lo, hi = RELIEF_TARGET.get(level, (None, None))
        out[level] = {"recommandées": len(r), "note médiane": med("score"), "relief médian (m/km)": med("dpk"),
                      "dans la cible de relief": (f"{sum(1 for x in r if lo <= x['dpk'] <= hi) / len(r):.0%}"
                                                  if lo is not None else "-"),
                      "grands axes médians": f"{st.median(x['main_roads'] for x in r):.0%}",
                      "feux/km médians": med("lights_km"), "départs en ville : sortie médiane (km)": round(st.median(ex), 1) if ex else "-",
                      "départs en ville : restent en ville": f"{(len(town) - len(ex)) / len(town):.0%}" if town else "-",
                      "terre > 1 km": sum(1 for x in r if x["dirt_km"] > 1.05),
                      "écart médian à la durée": f"{st.median(x['time_gap'] for x in r):.0%}",
                      "boucles à demi-tours": f"{sum(1 for x in r if x['u_turns'] > 0) / len(r):.0%}",
                      "plus long chemin répété médian (km)": med("longest_repeat_km")}
    return out


def report(res, out, prev=None):
    R = lambda key: res["issues"].get(key, [])  # noqa: E731
    reco_n = sum(1 for x in res["rows"] if x["reco"])
    L = [f"# Revue de cohérence des boucles publiées (v{res['generator_version']})", "",
         f"{res['starts'] - len(res['stale'])} départs revus, {len(res['rows'])} boucles dont {reco_n} recommandées ; "
         f"départs pas encore recalculés (écartés) : {len(res['stale'])} ; fichiers illisibles : {len(res['missing_files'])}.",
         "Les notes ne se comparent pas d'une version à l'autre (le barème change, par exemple la pénalité de relief du "
         "Tranquille en v17).", "Gravité : 1 incohérence (règle ou promesse non tenue), 2 à vérifier, 3 signal. Détours en pâté de maisons : non "
         "vérifiables ici (tracés publiés simplifiés).", "",
         "| famille | gravité | vérification | cas | Tranquille | Modéré | Sportif |", "|---|---|---|---|---|---|---|"]
    for key, (sev, fam, label) in sorted(CHECKS.items(), key=lambda kv: (kv[1][1], kv[1][0])):
        c = Counter(i["niveau"] for i in R(key))
        L.append(f"| {fam} | {sev} | {label} | {len(R(key))} | {c['facile']} | {c['modere']} | {c['soutenu']} |")
    ind = indicators(res)
    old = indicators(prev) if prev else None
    L += ["", "## Promesses par allure (boucles recommandées)"
          + (f", comparées à la v{prev['generator_version']} sur les mêmes départs" if old else ""), ""]
    keys = list(next(iter(ind.values())).keys()) if ind else []
    L += ["| indicateur | " + " | ".join(LEVEL_NAME[l] + (f" (v{prev['generator_version']} → v{res['generator_version']})" if old else "")
                                        for l in ind) + " |", "|---" * (len(ind) + 1) + "|"]
    for k in keys:
        L.append(f"| {k} | " + " | ".join((f"{old[l][k]} → {ind[l][k]}" if old and l in old else f"{ind[l][k]}") for l in ind) + " |")
    if prev:
        L += ["", f"## Incohérences : v{prev['generator_version']} → v{res['generator_version']} (mêmes départs)", "",
              "Les allers-retours jugés utiles sont publiés depuis la v17 : avant, ils n'apparaissaient pas.", "",
              "| vérification | avant | maintenant |", "|---|---|---|"]
        for key, (sev, fam, label) in sorted(CHECKS.items(), key=lambda kv: (kv[1][1], kv[1][0])):
            L.append(f"| {label} | {len(prev['issues'].get(key, []))} | {len(R(key))} |")
    L += ["", "## Pires boucles par critère (à juger dans l'app)", ""]
    for title, (k, worse_high) in RANKINGS.items():
        r = [x for x in res["rows"] if x.get(k) is not None and (k != "score" or x["reco"])]
        r.sort(key=lambda x: x[k], reverse=worse_high)
        L += [f"### {title}", ""]
        for x in r[:10]:
            val = f"{x[k]:.0%}" if k in ("main_roads", "overlap", "time_gap") else (f"{x[k]:.1f}" if isinstance(x[k], float) else x[k])
            L.append(f"- [{x['name']}, {LEVEL_NAME[x['level']]}, {x['dur']:g} h, « {x['label']} »]({x['link']}) : {val}"
                     + (" (aller-retour annoncé)" if x.get("oab") else ""))
        L.append("")
    L += ["## Détail des incohérences", ""]
    for key, (sev, fam, label) in sorted(CHECKS.items(), key=lambda kv: (kv[1][0], kv[1][1])):
        items = R(key)
        if not items or sev == 3:
            continue
        L += [f"### {label} ({len(items)})", ""]
        for i in items[:15]:
            L.append(f"- [{i['ou']}]({i['lien']}) : {i['valeur']}")
        if len(items) > 15:
            L.append(f"- … et {len(items) - 15} autres (fichier JSON)")
        L.append("")
    Path(f"{out}.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    slim = {k: v for k, v in res.items() if k != "rows"}
    slim["indicateurs"] = ind
    if prev:
        slim["indicateurs_avant"] = old
    Path(f"{out}.json").write_text(json.dumps(slim, ensure_ascii=False, indent=1), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", default=SITE, help="adresse du site ou dossier de données (index.json, starts/)")
    ap.add_argument("--compare", default=None, help="génération précédente à comparer (adresse ou dossier)")
    ap.add_argument("--out", default="data/audit")
    a = ap.parse_args()
    res = audit(a.site.rstrip("/"))
    prev = audit(a.compare.rstrip("/"), only={r["sid"] for r in res["rows"]}) if a.compare else None
    report(res, a.out, prev)
    print(f"{len(res['rows'])} boucles revues ; " + ", ".join(f"{k} {len(v)}" for k, v in res["issues"].items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
