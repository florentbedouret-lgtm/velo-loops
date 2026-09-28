#!/usr/bin/env python3
"""
Diagnostic (rien n'est publié) : les voies de la base topographique officielle de l'ICGC (Referencial topogràfic
territorial, CC BY 4.0) disent-elles mieux qu'OpenStreetMap si une voie est goudronnée ? (28/09/2026)

Contexte : chemin de terre au sud de Vallromanes noté dans OSM « piste de qualité 1 » (dur, en général goudronné), sans
revêtement ; ce cas n'est pas détectable par OSM seul. L'ICGC classe ses voies (attribut « tipus ») en voies revêtues
(autoroute, voies conventionnelles, voies préférentielles, voie non cataloguée, piste cyclable revêtue) et non revêtues
(camí, corriol, piste cyclable non revêtue) — hypothèse vérifiée ici contre les voies OSM dont le revêtement EST noté.

Méthode : les voies ICGC de la province sont lues à distance (GDAL /vsicurl/, lecture partielle du GeoPackage de
~10 Go) ; chaque voie OSM est échantillonnée tous les ~20 m et prend la classe ICGC majoritaire de ses points (voie ICGC
la plus proche à ~10 m au plus). Le rapport donne :
  1. l'accord ICGC / OSM là où OSM note le revêtement (fiabilité de l'ICGC) ;
  2. ce que dit l'ICGC des voies OSM sans revêtement noté (pistes de qualité 1, autres pistes, sentiers, routes) ;
  3. les voies témoins (Vallromanes…) ;
  4. sur les boucles publiées, les km de « piste de qualité 1 » et de routes sans revêtement noté que l'ICGC dit non
     revêtus (terre cachée qui resterait après le générateur v9).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import generate_loops as g  # noqa: E402
import landcover as lc      # noqa: E402

ICGC_URL = ("https://datacloud.icgc.cat/datacloud/topografia-territorial/gpkg_unzip/"
            "topografia-territorial-v1r0-2024.gpkg")
ICGC_PAVED = {"aut", "vcd", "vcu", "vnc", "vpd", "vpu", "bir"}      # revêtues
ICGC_UNPAVED = {"vca", "cor", "bin"}                                 # camí, corriol, piste cyclable non revêtue
ICGC_NEAR_DEG = 0.00012         # ~10 m : voie ICGC retenue pour un point OSM
SAMPLE_DEG = 0.00022            # ~20 m : pas d'échantillonnage des voies OSM
WITNESS_WAYS = {"173062090": "Vallromanes (photo de Florent : terre tassée)",
                "130076801": "Aiguafreda (piste qualité 1, 663 boucles)",
                "171416736": "Alella (piste qualité 1, 496 boucles)",
                "105516805": "Sant Martí Sesgueioles (piste qualité 1)"}


def load_icgc(workdir: Path, bbox: str):
    """Voies ICGC (axes) de l'emprise, lues à distance -> (STRtree, classes 'revetu'/'non_revetu', types)."""
    import shapely
    from shapely.geometry import shape
    out = workdir / "icgc_vials.geojsonseq"
    w, s, e, n = bbox.split(",")
    types = sorted(ICGC_PAVED | ICGC_UNPAVED)
    cmd = ["ogr2ogr", "-f", "GeoJSONSeq", str(out), "/vsicurl/" + ICGC_URL, "transports_l",
           "-spat", w, s, e, n, "-spat_srs", "EPSG:4326", "-t_srs", "EPSG:4326", "-select", "tipus",
           "-where", "tipus IN (" + ",".join(f"'{t}'" for t in types) + ")", "-overwrite"]
    env = {"CPL_VSIL_CURL_CHUNK_SIZE": "4194304", "CPL_VSIL_CURL_CACHE_SIZE": "268435456",
           "GDAL_HTTP_MULTIRANGE": "YES", "GDAL_HTTP_MAX_RETRY": "5", "GDAL_HTTP_RETRY_DELAY": "3"}
    import os
    t0 = time.time()
    subprocess.run(cmd, check=True, env={**os.environ, **env})
    print(f"Voies ICGC lues en {time.time() - t0:.0f} s", flush=True)
    geoms, cls, typ = [], [], []
    with out.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip("\x1e\n ")
            if not line:
                continue
            try:
                feat = json.loads(line)
                geom = shape(feat["geometry"])
            except (ValueError, KeyError, TypeError):
                continue
            t = (feat.get("properties") or {}).get("tipus")
            if geom.is_empty or t not in ICGC_PAVED | ICGC_UNPAVED:
                continue
            geoms.append(geom)
            cls.append("revetu" if t in ICGC_PAVED else "non_revetu")
            typ.append(t)
    from collections import Counter
    print("Voies ICGC par type : " + ", ".join(f"{k} {v}" for k, v in Counter(typ).most_common()), flush=True)
    return shapely.STRtree(geoms), np.array(cls)


def label_ways(osm_tree, icgc_tree, icgc_cls):
    """Classe ICGC majoritaire de chaque voie OSM : 'revetu', 'non_revetu' ou 'aucune' ; + part du vote gagnant."""
    import shapely
    geoms = osm_tree.geometries
    dense = shapely.segmentize(geoms, SAMPLE_DEG)
    pts, idx = shapely.get_coordinates(dense, return_index=True)
    n = len(geoms)
    votes = np.zeros((n, 2))                        # [revêtu, non revêtu], en nombre de points
    tot = np.bincount(idx, minlength=n).astype(float)
    step = 2_000_000
    for a in range(0, len(pts), step):              # par paquets : mémoire
        p = shapely.points(pts[a:a + step])
        pi, wi = icgc_tree.query_nearest(p, max_distance=ICGC_NEAR_DEG, all_matches=False)
        unp = (icgc_cls[wi] == "non_revetu").astype(int)
        np.add.at(votes, (idx[a:a + step][pi], unp), 1)
        print(f"  points {min(a + step, len(pts))}/{len(pts)}", flush=True)
    matched = votes.sum(axis=1)
    lab = np.where(matched < 0.5 * np.maximum(tot, 1), "aucune",
                   np.where(votes[:, 1] > votes[:, 0], "non_revetu", "revetu"))
    share = np.where(matched > 0, votes.max(axis=1) / np.maximum(matched, 1), 0)
    return lab, share


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True, help="web/data (boucles publiées)")
    ap.add_argument("--pbf", required=True)
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--bbox", default="1.35,41.18,2.80,42.33", help="emprise lon/lat (province de Barcelone)")
    ap.add_argument("--out", default="data/landcover_check.json")
    ap.add_argument("--out-md", default="data/landcover_check.md")
    args = ap.parse_args()
    t0 = time.time()
    wd = Path(args.workdir)
    icgc_tree, icgc_cls = load_icgc(wd, args.bbox)
    osm_tree, osm_cls, ids, _ = lc.load_ways(Path(args.pbf), wd)
    print(f"Voies OSM : {len(osm_cls)}", flush=True)
    lab, share = label_ways(osm_tree, icgc_tree, icgc_cls)
    km = np.array([gm.length * 90.0 for gm in osm_tree.geometries])     # ~km

    L = ["# Diagnostic ICGC : revêtement des voies selon la base topographique officielle (rien n'est publié)",
         f"\nVoies ICGC : {len(icgc_cls)} (revêtues {int((icgc_cls == 'revetu').sum())}, non revêtues "
         f"{int((icgc_cls == 'non_revetu').sum())}). Voies OSM : {len(osm_cls)}. Une voie OSM prend la classe ICGC "
         "majoritaire de ses points (tous les ~20 m, voie ICGC à ~10 m au plus) ; « aucune » si moins de la moitié de "
         "ses points trouvent une voie ICGC.",
         "\n## 1. Fiabilité : voies OSM dont le revêtement EST noté (km)",
         "\n| OSM | ICGC revêtu | ICGC non revêtu | ICGC aucune | accord |", "|---|---|---|---|---|"]
    for c, good in (("goudron", "revetu"), ("terre", "non_revetu")):
        m = osm_cls == c
        k = {x: km[m & (lab == x)].sum() for x in ("revetu", "non_revetu", "aucune")}
        L.append(f"| {c} | {k['revetu']:.0f} | {k['non_revetu']:.0f} | {k['aucune']:.0f} | "
                 f"{100 * k[good] / max(k['revetu'] + k['non_revetu'], 1e-9):.0f} % |")
    L += ["\n## 2. Ce que dit l'ICGC des voies OSM sans revêtement noté (km)",
          "\n| OSM | ICGC revêtu | ICGC non revêtu | ICGC aucune | part non revêtue (hors « aucune ») |",
          "|---|---|---|---|---|"]
    for c in ("piste_g1", "piste_g2plus", "piste_sans", "sentier_sans", "route_sans"):
        m = osm_cls == c
        k = {x: km[m & (lab == x)].sum() for x in ("revetu", "non_revetu", "aucune")}
        L.append(f"| {c} | {k['revetu']:.0f} | {k['non_revetu']:.0f} | {k['aucune']:.0f} | "
                 f"{100 * k['non_revetu'] / max(k['revetu'] + k['non_revetu'], 1e-9):.0f} % |")
    L.append("\n## 3. Voies témoins")
    pos = {str(i): k for k, i in enumerate(ids)}
    for w, txt in WITNESS_WAYS.items():
        k = pos.get(w)
        L.append(f"- {w} — {txt} : " + (f"OSM {osm_cls[k]}, ICGC **{lab[k]}** (vote {100 * share[k]:.0f} %)"
                                         if k is not None else "absente de l'extrait"))

    # 4. boucles publiées : terre cachée restante (pistes qualité 1 et routes sans revêtement noté, ICGC non revêtu)
    hidden, n_loops, per_loop = {}, 0, []
    for f in sorted((Path(args.data) / "starts").glob("*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        for o in d["options"]:
            _, lab_pts, (pi, wi), scale = lc.classify_loop(osm_tree, osm_cls, o["coords"], o["distance_km"])
            sel = np.isin(osm_cls[wi], ("piste_g1", "route_sans", "sentier_sans")) & (lab[wi] == "non_revetu")
            kmh = float(sel.sum()) * scale
            per_loop.append(kmh)
            for w in np.unique(wi[sel]).tolist():
                e = hidden.setdefault(w, {"boucles": 0, "departs": set()})
                e["boucles"] += 1
                e["departs"].add(f.stem)
            n_loops += 1
    arr = np.array(per_loop) if per_loop else np.zeros(1)
    L += ["\n## 4. Boucles publiées : terre cachée selon l'ICGC (pistes qualité 1, routes et sentiers sans revêtement "
          "noté, que l'ICGC dit non revêtus)",
          f"\n{n_loops} boucles. Médiane {np.median(arr):.1f} km ; boucles ≥ 0,5 km : {100 * (arr >= 0.5).mean():.0f} % ; "
          f"≥ 1 km : {100 * (arr >= 1).mean():.0f} % ; ≥ 3 km : {100 * (arr >= 3).mean():.0f} %.",
          f"\nVoies concernées : {len(hidden)}. Les 40 plus empruntées :"]
    geoms = osm_tree.geometries
    rows = sorted(hidden.items(), key=lambda kv: -kv[1]["boucles"])
    import csv
    with (Path(args.out).parent / "icgc_hidden_dirt.csv").open("w", encoding="utf-8", newline="") as fh:
        wr = csv.writer(fh)
        wr.writerow(["osm", "id", "classe_osm", "boucles", "departs", "longueur_km", "lat", "lon", "exemple_depart"])
        for w, e in rows:
            c = geoms[w].interpolate(0.5, normalized=True)
            wr.writerow([f"https://www.openstreetmap.org/way/{ids[w]}", ids[w], osm_cls[w], e["boucles"],
                         len(e["departs"]), round(km[w], 2), round(c.y, 5), round(c.x, 5), sorted(e["departs"])[0]])
    L += [f"- [{ids[w]}](https://www.openstreetmap.org/way/{ids[w]}) ({osm_cls[w]}) : {e['boucles']} boucles, "
          f"{len(e['departs'])} départs (ex. {sorted(e['departs'])[0]})" for w, e in rows[:40]]
    L.append(f"\nDurée : {time.time() - t0:.0f} s. Source : © Institut Cartogràfic i Geològic de Catalunya (ICGC), "
             "Referencial topogràfic territorial v1.0 2024, CC BY 4.0.")
    Path(args.out).write_text(json.dumps({"loops_km_hidden": [round(x, 2) for x in per_loop]}), encoding="utf-8")
    Path(args.out_md).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
