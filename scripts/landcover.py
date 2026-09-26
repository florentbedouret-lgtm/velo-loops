#!/usr/bin/env python3
"""
Répartition du terrain (ville / eau / forêt / espaces ouverts) des boucles DÉJÀ PUBLIÉES, sans GraphHopper :
elle ne dépend que du tracé (coords publiés) et des cartes OSM. Deux usages :

  --check          diagnostic (rien n'est publié) : part « ville » selon plusieurs définitions, sur des départs témoins
  --apply METHODE  recalcule scenery.landcover de toutes les boucles de --data avec la définition METHODE

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


def partition(pts, land, mask) -> dict:
    n = len(pts)
    cls = np.full(n, 3)
    for code, key, dist in ((2, "forest", g.SCENERY_FOREST_DEG), (1, "water", g.SCENERY_WATER_DEG)):
        tree = land.trees.get(key)
        if tree is not None:
            cls[np.unique(tree.query(pts, predicate="dwithin", distance=dist)[0])] = code
    cls[mask] = 0
    c = np.bincount(cls, minlength=4) / max(n, 1)
    return {"city": round(float(c[0]), 3), "water": round(float(c[1]), 3), "forest": round(float(c[2]), 3),
            "countryside": round(float(c[3]), 3)}


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


def apply(data: Path, land, bld_tree, method: str):
    files = sorted((data / "starts").glob("*.json"))
    n = 0
    for f in files:
        d = json.loads(f.read_text(encoding="utf-8"))
        for o in d["options"]:
            if o.get("scenery") is None:
                continue
            pts = sample(o["coords"])
            o["scenery"]["landcover"] = partition(pts, land, city_mask(pts, land, method, bld_tree))
            o["scenery"]["landcover_method"] = method
            n += 1
        f.write_text(json.dumps(d, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"Répartition recalculée ({method}) : {n} boucles dans {len(files)} départs", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True, help="dossier web/data (données publiées récupérées)")
    ap.add_argument("--pbf", required=True)
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--apply", choices=METHODS)
    ap.add_argument("--bld-bbox", default="", help="emprise des bâtiments chargés (vide = tout l'extrait, la province)")
    ap.add_argument("--out", default="data/landcover_check.json")
    ap.add_argument("--out-md", default="data/landcover_check.md")
    args = ap.parse_args()
    pbf, wd, data = Path(args.pbf), Path(args.workdir), Path(args.data)
    land = g.load_landscape(pbf, wd)
    if land is None:
        sys.exit("paysage OSM non chargé")
    print("Paysage : " + ", ".join(f"{k} {v}" for k, v in land.counts.items()), flush=True)
    bld_tree = None
    if args.check or args.apply in BLD:
        import time
        t0 = time.time()
        bld_tree, nb = load_buildings(pbf, wd, args.bld_bbox)
        print(f"Bâtiments chargés ({args.bld_bbox or 'province'}) : {nb} en {time.time() - t0:.0f} s", flush=True)
    if args.check:
        check(data, land, bld_tree, Path(args.out_md), Path(args.out))
        with open(args.out_md, "a", encoding="utf-8") as fh:
            fh.write(f"\nBâtiments chargés : {nb} ({args.bld_bbox or 'province'}), en {time.time() - t0:.0f} s.\n")
    if args.apply:
        apply(data, land, bld_tree, args.apply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
