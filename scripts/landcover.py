#!/usr/bin/env python3
"""
Répartition du terrain (ville / eau / forêt / espaces ouverts) des boucles DÉJÀ PUBLIÉES, sans GraphHopper :
elle ne dépend que du tracé (coords publiés) et des cartes OSM. Deux usages :

  --check          diagnostic (rien n'est publié) : part « ville » selon plusieurs définitions, sur des départs témoins
  --check-water    diagnostic (rien n'est publié) : part « bord d'eau » le long des grandes rivières (27/09/2026 ; le
                   diagnostic précédent, sur la mer, a donné la règle mer ~250 m / front de mer ~50 m, voir D27)
  --check-surface  diagnostic (rien n'est publié) : chemins de terre non signalés (pistes OSM sans revêtement noté) sur
                   TOUTES les boucles publiées (28/09/2026)
  --apply METHODE  recalcule scenery.landcover de toutes les boucles de --data avec la définition METHODE, ainsi que
                   scenery.landcover_seq (catégorie tous les 100 m : bande sous le profil), exit_city_km (sortie de ville
                   mesurée par le bâti) et le bloc « compare » de l'index (suggestion de départ voisin)

Pourquoi (26/09/2026) : la v1 comptait « ville » un point À L'INTÉRIEUR d'une zone bâtie OSM (landuse residential/
commercial/industrial/retail). À Barcelone ces zones sont dessinées îlot par îlot, rues exclues : un vélo roule dans la
rue, donc hors zone -> une boucle 100 % urbaine (Gràcia, 1 h) sortait à 5 % « ville ».

Définitions de « ville » comparées (point du tracé tous les 100 m ; priorité ville > eau > forêt > espaces ouverts) :
  inside   dans une zone bâtie (v1)
  near25   à moins de ~25 m d'une zone bâtie (couvre une rue entre deux îlots)
  near50   à moins de ~50 m d'une zone bâtie (couvre aussi les avenues larges)
  bldR_N   au moins N bâtiments à moins de ~R m (bâti réel autour du cycliste) : bld50_3, bld75_3, bld75_5

Diagnostic n°1 (26/09/2026) : les zones bâties OSM sont inutilisables (Gràcia 1 h : inside 8 %, near50 51 % ; Collserola
near50 46-64 %) ; le comptage de bâtiments sépare ville et nature (Gràcia 1 h 83 %, Collserola 15-27 %) mais sous-estime
les grandes avenues à 50 m -> calage 50 / 75 m et 3 / 5 bâtiments, bâtiments chargés sur toute la province.
"""
from __future__ import annotations

import argparse
import json
import statistics as st
import subprocess
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import generate_loops as g  # load_landscape, sample_points, haversine, SCENERY_*_DEG

NEAR25_DEG, NEAR50_DEG = 0.0003, 0.0006   # ~25 et ~50 m à cette latitude (en longitude ; un peu plus en latitude)
BLD = {"bld50_3": (0.0006, 3), "bld75_3": (0.0009, 3),                           # (rayon en degrés, nb de bâtiments)
       "bld75_5": (g.LANDCOVER_BLD_DEG, g.LANDCOVER_BLD_MIN)}   # retenue le 26/09/2026 = règle du générateur
METHODS = ("inside", "near50", *BLD)
WITNESSES = ("gracia", "l-eixample", "sant-marti", "ciutat-vella", "horta-guinardo", "sarria-sant-gervasi",
             "sant-adria-de-besos", "badalona", "el-tibidabo-est", "can-rectoret-est", "santa-creu-d-olorda-est",
             "sabadell", "terrassa", "molins-de-rei", "castelldefels")


def load_buildings(pbf: Path, workdir: Path, bbox: str):
    """Centres des bâtiments OSM (osmium), en arbre spatial de points ; bbox vide = tout l'extrait (province)."""
    import shapely
    from shapely.geometry import shape
    cut, filt, out = workdir / "bld_cut.osm.pbf", workdir / "bld.osm.pbf", workdir / "bld.geojsonseq"
    src = pbf
    if bbox:
        subprocess.run(["osmium", "extract", "--bbox", bbox, str(pbf), "-o", str(cut), "--overwrite"], check=True,
                       capture_output=True)
        src = cut
    subprocess.run(["osmium", "tags-filter", str(src), "w/building", "-o", str(filt), "--overwrite"], check=True,
                   capture_output=True)
    subprocess.run(["osmium", "export", str(filt), "-f", "geojsonseq", "-o", str(out), "--overwrite"], check=True,
                   capture_output=True)
    pts = []
    with out.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip("\x1e\n ")
            if not line:
                continue
            try:
                geom = shape(json.loads(line)["geometry"])
            except (ValueError, KeyError):
                continue
            if not geom.is_empty:
                pts.append(geom.representative_point())
    return shapely.STRtree(pts) if pts else None, len(pts)


def sample(coords):
    import shapely
    cum = [0.0]
    for i in range(1, len(coords)):
        cum.append(cum[-1] + g.haversine(coords[i - 1][0], coords[i - 1][1], coords[i][0], coords[i][1]))
    return shapely.points(np.array(g.sample_points(coords, cum, 100.0)))


def city_mask(pts, land, method, bld_tree):
    n = len(pts)
    mask = np.zeros(n, dtype=bool)
    tree = land.trees.get("builtup")
    if method in BLD:
        if bld_tree is None:
            return None
        radius, nmin = BLD[method]
        hit = bld_tree.query(pts, predicate="dwithin", distance=radius)[0]
        counts = np.bincount(hit, minlength=n)
        return counts >= nmin
    if tree is None:
        return mask
    if method == "inside":
        hit = tree.query(pts, predicate="intersects")[0]
    else:
        hit = tree.query(pts, predicate="dwithin", distance=NEAR25_DEG if method == "near25" else NEAR50_DEG)[0]
    mask[np.unique(hit)] = True
    return mask


def partition(pts, land, mask, with_cls: bool = False):
    n = len(pts)
    cls = np.full(n, 3)
    for code, key, dist in ((2, "forest", g.SCENERY_FOREST_DEG), (1, "water", g.SCENERY_WATER_DEG)):
        tree = land.trees.get(key)
        if tree is not None:
            cls[np.unique(tree.query(pts, predicate="dwithin", distance=dist)[0])] = code
    cls[mask] = 0
    c = np.bincount(cls, minlength=4) / max(n, 1)
    shares = {"city": round(float(c[0]), 3), "water": round(float(c[1]), 3), "forest": round(float(c[2]), 3),
              "countryside": round(float(c[3]), 3)}
    return (shares, cls) if with_cls else shares


# ---------------------------------------------------------------- « bord d'eau » le long des grandes rivières (27/09/2026)
# Remarque de Florent (Gràcia 2 h, le long du Besòs) : des morceaux qui suivent la rivière ne sont pas « bord d'eau ». Le Besòs
# n'est dessiné que par sa ligne centrale (lit de plus de 100 m) : la piste passe à 70-90 m de cette ligne, à la limite du
# seuil de ~100 m ; et la ville (5 bâtiments à ~75 m) passe avant l'eau. Variantes comparées (la mer garde sa règle) :
RIVER_VARIANTS = {"actuel": (None, None),               # (riv150_front60 retenue le 28/09/2026 : règle en production)
                  "riv150": (0.0016, None),             # grande rivière jusqu'à ~135 m (longitude) / ~180 m (latitude)
                  "riv150_front60": (0.0016, 0.0007)}   # + à moins de ~60 m de la ligne centrale, bord d'eau avant ville
RIVER_WITNESSES = ("gracia", "sant-adria-de-besos", "santa-coloma-de-gramenet", "montcada-i-reixac", "ripollet",
                   "sant-andreu", "molins-de-rei", "sant-boi-de-llobregat", "el-prat-de-llobregat", "martorell",
                   "ciutat-vella", "badalona", "terrassa", "garraf-nord")


def check_water(data: Path, land, bld_tree, out_md: Path, out_json: Path):
    rows = []
    for sid in RIVER_WITNESSES:
        f = data / "starts" / f"{sid}.json"
        if not f.exists():
            continue
        d = json.loads(f.read_text(encoding="utf-8"))
        for o in d["options"]:
            pts = sample(o["coords"])
            mask = city_mask(pts, land, "bld75_5", bld_tree)
            r = {"start": sid, "level": o["level"], "min": round(o["duration_target_min"]), "label": o["label"],
                 "km": o["distance_km"]}
            for v, (rd, rn) in RIVER_VARIANTS.items():
                cls = g.landcover_classes(land.trees, pts, mask, rd, rn)
                c = np.bincount(cls, minlength=4) / max(len(pts), 1)
                r[v] = {"water": round(100 * float(c[1])), "city": round(100 * float(c[0])), "seq": g.landcover_seq(cls)}
            rows.append(r)
    out_json.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    L = ["# Diagnostic « bord d'eau » le long des grandes rivières (rien n'est publié)",
         f"\n{len(rows)} boucles publiées de {len({r['start'] for r in rows})} départs témoins (Besòs, Llobregat, et témoins "
         "sans grande rivière). Parts médianes par départ, en % : **eau / ville**. actuel = règle en ligne (rivière à ~100 m, "
         "ville prioritaire) ; riv150 = grande rivière jusqu'à ~150 m de sa ligne centrale ; riv150_front60 = en plus, à moins "
         "de ~60 m de cette ligne, bord d'eau avant ville.",
         "\n| Départ | " + " | ".join(RIVER_VARIANTS) + " |", "|---|" + "---|" * len(RIVER_VARIANTS)]
    for sid in RIVER_WITNESSES:
        rs = [r for r in rows if r["start"] == sid]
        if not rs:
            continue
        cell = lambda v: f"{round(st.median(r[v]['water'] for r in rs))} / {round(st.median(r[v]['city'] for r in rs))}"  # noqa: E731
        L.append(f"| {sid} | " + " | ".join(cell(v) for v in RIVER_VARIANTS) + " |")
    L.append(f"\nGrandes rivières chargées : {land.counts.get('river', 0)} tronçons.")
    L.append("\n## Cas signalé : Gràcia, 2 h, modéré (par variante : eau / ville, puis bande v/e/f/o)")
    for r in rows:
        if r["start"] == "gracia" and r["min"] == 120 and r["level"] == "modere":
            L.append(f"\n**{r['label']}** ({r['km']} km)")
            for v in RIVER_VARIANTS:
                L.append(f"- {v} : eau {r[v]['water']} %, ville {r[v]['city']} % — `{r[v]['seq']}`")
    out_md.write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L), flush=True)


# ---------------------------------------------------------------- chemins de terre non signalés (28/09/2026)
# Remarque de Florent (Sant Andreu, 3 h, modéré) : au sud de Vallromanes, la boucle emprunte un chemin de terre tassée.
# OSM : highway=track, tracktype=grade1 (« dur, en général goudronné »), sans surface -> GraphHopper le compte en
# revêtement « inconnu » : ni pénalisé (non goudronné > 12 %), ni affiché (> 5 %). Le diagnostic mesure, point par point
# (tous les 50 m), la voie OSM la plus proche du tracé (à ~15 m au plus) et la classe :
SURF_PAVED = {"asphalt", "concrete", "concrete:plates", "concrete:lanes", "paving_stones", "paved", "chipseal", "sett",
              "cobblestone", "unhewn_cobblestone", "metal", "wood", "bricks"}
SURF_CLASSES = ("goudron", "terre", "piste_g1", "piste_g2plus", "piste_sans", "sentier_sans", "route_sans", "aucune")
SURF_NEAR_DEG = 0.00018          # ~15 m : au-delà, pas de voie OSM retenue pour ce point (« aucune »)
DOUBT_CLASSES = ("piste_g1", "piste_g2plus", "piste_sans", "sentier_sans")   # revêtement non noté : à vérifier


def surface_class(tags: dict) -> str:
    """goudron / terre si le revêtement est noté ; sinon selon le type de voie : piste (qualité 1, 2 à 5, non notée),
    sentier (path, footway, bridleway), route (le reste : en pratique presque toujours goudronnée)."""
    s = tags.get("surface")
    if s in SURF_PAVED:
        return "goudron"
    if s in g.UNPAVED or s in ("pebblestone", "earth", "mud", "woodchips", "rock"):
        return "terre"
    hw = tags.get("highway")
    if hw == "track":
        tt = tags.get("tracktype")
        return "piste_g1" if tt == "grade1" else ("piste_g2plus" if tt else "piste_sans")
    if hw in ("path", "footway", "bridleway"):
        return "sentier_sans"
    return "route_sans"


def load_ways(pbf: Path, workdir: Path, with_highway: bool = False):
    """Voies OSM (osmium) -> (STRtree des lignes, classes, identifiants, stats de longueur des pistes à revêtement noté)."""
    import shapely
    from shapely.geometry import shape
    filt, out = workdir / "ways.osm.pbf", workdir / "ways.geojsonseq"
    subprocess.run(["osmium", "tags-filter", str(pbf), "w/highway", "-o", str(filt), "--overwrite"], check=True,
                   capture_output=True)
    subprocess.run(["osmium", "export", str(filt), "-f", "geojsonseq", "--geometry-types=linestring", "-a", "id",
                    "-o", str(out), "--overwrite"], check=True, capture_output=True)
    skip = {"motorway", "motorway_link", "trunk_link", "steps", "construction", "proposed", "platform", "corridor",
            "elevator", "raceway", "bus_stop", "services", "rest_area"}
    geoms, cls, ids, hws = [], [], [], []
    prior = {}                   # pistes dont le revêtement EST noté : km goudron / terre, par qualité (tracktype)
    with out.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip("\x1e\n ")
            if not line:
                continue
            try:
                feat = json.loads(line)
                geom = shape(feat["geometry"])
            except (ValueError, KeyError):
                continue
            p = feat.get("properties", {})
            if p.get("highway") in skip or geom.is_empty:
                continue
            c = surface_class(p)
            geoms.append(geom)
            cls.append(c)
            ids.append(p.get("@id") or feat.get("id"))
            hws.append(p.get("highway"))
            if p.get("highway") == "track" and c in ("goudron", "terre"):
                k = p.get("tracktype") or "non notée"
                prior.setdefault(k, {"goudron": 0.0, "terre": 0.0})[c] += geom.length * 90.0   # ~km (degrés -> km)
    if with_highway:                                   # icgc_tag.py : type de voie OSM
        return shapely.STRtree(geoms), np.array(cls), ids, prior, hws
    return shapely.STRtree(geoms), np.array(cls), ids, prior


def classify_loop(tree, cls_arr, coords, dist_km: float, step: float = 50.0):
    """(km par classe de SURF_CLASSES, classe de chaque point tous les `step` m, (indices points, indices voies))."""
    import shapely
    cum = [0.0]
    for i in range(1, len(coords)):
        cum.append(cum[-1] + g.haversine(coords[i - 1][0], coords[i - 1][1], coords[i][0], coords[i][1]))
    pts = shapely.points(np.array(g.sample_points(coords, cum, step)))
    pi, wi = tree.query_nearest(pts, max_distance=SURF_NEAR_DEG, all_matches=False)
    lab = np.full(len(pts), "aucune", dtype=object)
    lab[pi] = cls_arr[wi]
    scale = step / 1000.0 * dist_km / max(cum[-1] / 1000.0, 1e-9)
    return {c: round(float((lab == c).sum()) * scale, 2) for c in SURF_CLASSES}, lab, (pi, wi), scale


def dirt_km(km: dict) -> float:
    """Terre notée + pistes de qualité 2 à 5 ou non notée, sans revêtement noté (méthode du diagnostic, sur OSM)."""
    return km["terre"] + km["piste_g2plus"] + km["piste_sans"]


def check_surface(data: Path, pbf: Path, wd: Path, out_md: Path, out_json: Path):
    import shapely
    import time
    t0 = time.time()
    tree, cls_arr, ids, prior = load_ways(pbf, wd)
    print(f"Voies OSM chargées : {len(cls_arr)} en {time.time() - t0:.0f} s", flush=True)
    step = 50.0
    rows, case = [], []
    ways = {}                    # voies au revêtement incertain empruntées par les boucles : à vérifier dans OSM (Florent)
    for f in sorted((data / "starts").glob("*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        for o in d["options"]:
            km, lab, (pi, wi), scale = classify_loop(tree, cls_arr, o["coords"], o["distance_km"], step)
            doubt = np.isin(cls_arr[wi], DOUBT_CLASSES)
            if doubt.any():
                w_u, n_u = np.unique(wi[doubt], return_counts=True)
                for w, n in zip(w_u.tolist(), n_u.tolist()):
                    e = ways.setdefault(w, {"loops": 0, "km": 0.0, "starts": set()})
                    e["loops"] += 1
                    e["km"] += n * scale
                    e["starts"].add(f.stem)
            su = o.get("surface") or {}
            rows.append({"start": f.stem, "level": o["level"], "min": round(o["duration_target_min"]),
                         "label": o["label"], "dist": o["distance_km"], "km": km,
                         "gh_unpaved": su.get("unpaved", 0), "gh_unknown": su.get("unknown", 0)})
            if f.stem == "sant-andreu" and abs(o["distance_km"] - 54.3) < 0.2:          # cas signalé par Florent
                segs, cur = [], None
                for k, c in enumerate(lab):
                    x = k * scale
                    if c.startswith("piste") or c in ("terre", "sentier_sans"):
                        if cur and cur[2] == c and x - cur[1] <= 0.11:
                            cur[1] = x
                        else:
                            cur = [x, x, c, set()]
                            segs.append(cur)
                        cur[3].update(ids[w] for p_, w in zip(pi, wi) if p_ == k)
                    else:
                        cur = None
                case = [f"- km {a:.1f} à {b:.1f} : {c} (voies {', '.join(str(i) for i in sorted(s, key=str))})"
                        for a, b, c, s in segs if b - a >= 0.1]
    out_json.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    geoms = tree.geometries
    listing = []
    for w, e in ways.items():
        c = geoms[w].interpolate(0.5, normalized=True)
        listing.append({"osm": f"https://www.openstreetmap.org/way/{ids[w]}", "id": ids[w], "classe": str(cls_arr[w]),
                        "boucles": e["loops"], "departs": len(e["starts"]), "km_parcourus": round(e["km"], 1),
                        "longueur_km": round(geoms[w].length * 90.0, 2), "lat": round(c.y, 5), "lon": round(c.x, 5),
                        "exemple_depart": sorted(e["starts"])[0]})
    listing.sort(key=lambda x: (-x["boucles"], -x["km_parcourus"]))
    import csv
    with (out_json.parent / "surface_ways.csv").open("w", encoding="utf-8", newline="") as fh:
        wr = csv.DictWriter(fh, fieldnames=list(listing[0]) if listing else ["osm"])
        wr.writeheader()
        wr.writerows(listing)

    def doubtful(r, with_g1=True):          # km de chemins probablement non goudronnés, non signalés aujourd'hui
        k = r["km"]
        return k["piste_sans"] + k["piste_g2plus"] + (k["piste_g1"] if with_g1 else 0.0)

    n = len(rows)
    pct = lambda x: f"{100 * x / max(n, 1):.0f} %"  # noqa: E731
    L = ["# Diagnostic « chemins de terre non signalés » (rien n'est publié)",
         f"\n{n} boucles publiées, {len({r['start'] for r in rows})} départs. Un point tous les {step:.0f} m, classé par la "
         "voie OSM la plus proche (≤ ~15 m). « Pistes douteuses » = highway=track sans revêtement noté (qualité 2 à 5 ou non "
         "notée), avec ou sans la qualité 1 (« dur », souvent fausse : cas de Vallromanes).",
         "\n## Combien de boucles sont touchées ?",
         "\n| Pistes douteuses sur la boucle | sans qualité 1 | avec qualité 1 |", "|---|---|---|"]
    for lo, txt in ((0.5, "≥ 0,5 km"), (1, "≥ 1 km"), (2, "≥ 2 km"), (5, "≥ 5 km")):
        L.append(f"| {txt} | {pct(sum(doubtful(r, False) >= lo for r in rows))} | {pct(sum(doubtful(r) >= lo for r in rows))} |")
    for share, txt in ((0.05, "≥ 5 % de la distance"), (0.12, "≥ 12 % (seuil d'exclusion)")):
        L.append(f"| {txt} | {pct(sum(doubtful(r, False) >= share * r['dist'] for r in rows))} | "
                 f"{pct(sum(doubtful(r) >= share * r['dist'] for r in rows))} |")
    tot = sum(r["km"]["terre"] + doubtful(r) for r in rows)
    L.append(f"\nSi on comptait les pistes douteuses (avec qualité 1) comme non goudronnées : "
             f"{pct(sum(r['km']['terre'] + doubtful(r) > 0.12 * r['dist'] for r in rows))} des boucles dépasseraient le seuil "
             f"d'exclusion de 12 % (aujourd'hui : {pct(sum(r['gh_unpaved'] > 0.12 for r in rows))}).")
    L.append("\n## Par niveau et par durée (médiane des km de pistes douteuses, avec qualité 1 ; part des boucles ≥ 1 km)")
    L.append("\n| | médiane km | ≥ 1 km |")
    L.append("|---|---|---|")
    for key, vals in (("level", ("facile", "modere", "soutenu")), ("min", sorted({r["min"] for r in rows}))):
        for v in vals:
            rs = [r for r in rows if r[key] == v]
            if rs:
                L.append(f"| {v if key == 'level' else f'{v} min'} | {st.median(doubtful(r) for r in rs):.1f} | "
                         f"{100 * sum(doubtful(r) >= 1 for r in rs) / len(rs):.0f} % |")
    km_all = {c: sum(r["km"][c] for r in rows) for c in SURF_CLASSES}
    all_km = max(sum(km_all.values()), 1e-9)
    L.append("\n## Répartition de tous les km publiés")
    L.append("\n" + " · ".join(f"{c} {100 * v / all_km:.1f} %" for c, v in km_all.items()))
    L.append(f"\n(terre notée + pistes douteuses = {100 * tot / all_km:.1f} % des km publiés)")
    L.append("\n## Pistes OSM dont le revêtement EST noté (province) : part de terre selon la qualité notée")
    L.append("\n| Qualité (tracktype) | km goudron | km terre | part terre |")
    L.append("|---|---|---|---|")
    for k in sorted(prior):
        a, b = prior[k]["goudron"], prior[k]["terre"]
        L.append(f"| {k} | {a:.0f} | {b:.0f} | {100 * b / max(a + b, 1e-9):.0f} % |")
    L.append("\n## Les 25 boucles les plus touchées")
    L.append("\n| Départ | niveau | durée | km | pistes douteuses (km) | dont qualité 1 | terre notée | GH inconnu |")
    L.append("|---|---|---|---|---|---|---|---|")
    for r in sorted(rows, key=doubtful, reverse=True)[:25]:
        L.append(f"| {r['start']} | {r['level']} | {r['min']} | {r['dist']} | {doubtful(r):.1f} | {r['km']['piste_g1']:.1f} | "
                 f"{r['km']['terre']:.1f} | {100 * r['gh_unknown']:.0f} % |")
    L.append("\n## Cas signalé : Sant Andreu, 3 h, modéré, 54,3 km (tronçons de piste, de terre ou de sentier)")
    L += case or ["- (boucle non trouvée)"]
    L.append("\n## Voies au revêtement incertain les plus empruntées (liste complète : surface_ways.csv)")
    L.append("\n| Classe | voies | dont empruntées par ≥ 10 boucles |")
    L.append("|---|---|---|")
    for c in DOUBT_CLASSES:
        xs = [x for x in listing if x["classe"] == c]
        L.append(f"| {c} | {len(xs)} | {sum(x['boucles'] >= 10 for x in xs)} |")
    for c in DOUBT_CLASSES:
        L.append(f"\n**{c}** (30 premières)")
        L += [f"- [{x['id']}]({x['osm']}) : {x['boucles']} boucles, {x['departs']} départs (ex. {x['exemple_depart']})"
              for x in [x for x in listing if x["classe"] == c][:30]]
    L.append(f"\nDurée du diagnostic : {time.time() - t0:.0f} s.")
    out_md.write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L), flush=True)


def check(data: Path, land, bld_tree, out_md: Path, out_json: Path):
    rows = []
    for sid in WITNESSES:
        f = data / "starts" / f"{sid}.json"
        if not f.exists():
            continue
        d = json.loads(f.read_text(encoding="utf-8"))
        for o in d["options"]:
            pts = sample(o["coords"])
            r = {"start": sid, "level": o["level"], "min": round(o["duration_target_min"]), "label": o["label"],
                 "gh_city": round(100 * o["shares"]["urban"]["city"]),
                 "gh_dense": round(100 * (o["shares"]["urban"]["city"] + o["shares"]["urban"]["residential"]))}
            for m in METHODS:
                mask = city_mask(pts, land, m, bld_tree)
                r[m] = None if mask is None else round(100 * partition(pts, land, mask)["city"])
            rows.append(r)
    out_json.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    L = ["# Diagnostic « ville » de la barre de terrain (rien n'est publié)",
         f"\n{len(rows)} boucles publiées de {len({r['start'] for r in rows})} départs témoins. Part « ville » (%) "
         "médiane par départ, selon la définition. Repères : GraphHopper « ville » et « ville + résidentiel ».",
         "\n| Départ | GH ville | GH ville+résid. | " + " | ".join(METHODS) + " |",
         "|---|---|---|" + "---|" * len(METHODS)]
    for sid in WITNESSES:
        rs = [r for r in rows if r["start"] == sid]
        if not rs:
            continue
        med = lambda k: round(st.median([r[k] for r in rs if r[k] is not None])) if any(r[k] is not None for r in rs) else "—"  # noqa: E731
        L.append(f"| {sid} | {med('gh_city')} | {med('gh_dense')} | " + " | ".join(str(med(m)) for m in METHODS) + " |")
    L.append("\n## Cas signalé : Gràcia, 1 h, modéré")
    for r in rows:
        if r["start"] == "gracia" and r["min"] == 60 and r["level"] == "modere":
            L.append(f"- {r['label']} : GH ville {r['gh_city']} % · " + " · ".join(f"{m} {r[m]} %" for m in METHODS))
    out_md.write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L), flush=True)


def apply(data: Path, land, bld_tree, method: str, pois=None):
    files = sorted((data / "starts").glob("*.json"))
    n = 0
    compares = {}
    for f in files:
        d = json.loads(f.read_text(encoding="utf-8"))
        for o in d["options"]:
            if o.get("scenery") is None:
                continue
            pts = sample(o["coords"])
            mask = city_mask(pts, land, method, bld_tree)
            cls = g.landcover_classes(land.trees, pts, mask)     # même règle que le générateur (mer, grandes rivières)
            c = np.bincount(cls, minlength=4) / max(len(pts), 1)
            o["scenery"]["landcover"] = {"city": round(float(c[0]), 3), "water": round(float(c[1]), 3),
                                         "forest": round(float(c[2]), 3), "countryside": round(float(c[3]), 3)}
            o["scenery"]["landcover_method"] = method + "+mer250_front50+riv150_front60"
            o["scenery"]["landcover_seq"] = g.landcover_seq(cls)
            o["scenery"]["protected_seq"] = "".join("p" if x else "-" for x in g.protected_mask(land.trees, pts))
            g.enrich_option(o, pois)                           # eau, cafés hors ville, gares ; noms des cols
            o["exit_city_km"] = g.exit_city_km(np.where(mask, 0, 3), o["distance_km"])   # sortie de ville : bâti seul
            n += 1
        f.write_text(json.dumps(d, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        compares[f.stem] = g.compare_block(d["options"])
    idx_path = data / "index.json"                         # bloc « compare » de l'index : sortie de ville par le bâti
    idx = json.loads(idx_path.read_text(encoding="utf-8"))
    for e in idx["starts"]:
        if e["id"] in compares:
            e["compare"] = compares[e["id"]]
    idx_path.write_text(json.dumps(idx, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"Répartition recalculée ({method}) : {n} boucles dans {len(files)} départs ; index mis à jour", flush=True)
    print(f"Points d'intérêt : {sum(len(o.get('pois') or []) for f in files for o in json.loads(f.read_text(encoding='utf-8'))['options'])} "
          "au total", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True, help="dossier web/data (données publiées récupérées)")
    ap.add_argument("--pbf", required=True)
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--check-water", action="store_true")
    ap.add_argument("--check-surface", action="store_true")
    ap.add_argument("--check-city", dest="check", action="store_true", help="alias de --check")
    ap.add_argument("--apply", choices=METHODS)
    ap.add_argument("--bld-bbox", default="", help="emprise des bâtiments chargés (vide = tout l'extrait, la province)")
    ap.add_argument("--out", default="data/landcover_check.json")
    ap.add_argument("--out-md", default="data/landcover_check.md")
    args = ap.parse_args()
    pbf, wd, data = Path(args.pbf), Path(args.workdir), Path(args.data)
    if args.check_surface:                             # n'a besoin ni du paysage ni des bâtiments
        check_surface(data, pbf, wd, Path(args.out_md), Path(args.out))
        return 0
    land = g.load_landscape(pbf, wd)
    if land is None:
        sys.exit("paysage OSM non chargé")
    print("Paysage : " + ", ".join(f"{k} {v}" for k, v in land.counts.items()), flush=True)
    bld_tree = None
    if args.check or args.check_water or args.apply in BLD:
        import time
        t0 = time.time()
        bld_tree, nb = load_buildings(pbf, wd, args.bld_bbox)
        print(f"Bâtiments chargés ({args.bld_bbox or 'province'}) : {nb} en {time.time() - t0:.0f} s", flush=True)
    if args.check_water:
        check_water(data, land, bld_tree, Path(args.out_md), Path(args.out))
    if args.check:
        check(data, land, bld_tree, Path(args.out_md), Path(args.out))
        with open(args.out_md, "a", encoding="utf-8") as fh:
            fh.write(f"\nBâtiments chargés : {nb} ({args.bld_bbox or 'province'}), en {time.time() - t0:.0f} s.\n")
    if args.apply:
        apply(data, land, bld_tree, args.apply, g.load_pois(pbf, wd))
    return 0


if __name__ == "__main__":
    sys.exit(main())
