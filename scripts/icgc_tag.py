#!/usr/bin/env python3
"""
Générateur v10 (28/09/2026) : revêtement des voies OSM complété par la base topographique de l'ICGC, dans la COPIE LOCALE
de la carte utilisée par GraphHopper (jamais dans OpenStreetMap : la licence CC BY 4.0 de l'ICGC ne le permet pas sans
dérogation).

Diagnostic ICGC (landcover_check, 28/09/2026) : là où OSM note le revêtement, l'ICGC est d'accord à 98 % pour la terre et
87 % pour le goudron ; parmi les voies OSM SANS revêtement noté, l'ICGC dit non revêtues 84 % des pistes « qualité 1 »,
99 % des autres pistes, 97 % des sentiers et 11 % des routes. Règle (prudente) : une voie OSM sans aucun tag « surface »
reçoit surface=unpaved si l'ICGC la dit non revêtue (vote ≥ ICGC_MIN_VOTE de ses points), pour les pistes, sentiers et
petites routes (unclassified, service) ; jamais pour les rues (residential…) ni les routes principales, où une allée
parallèle de l'ICGC pourrait être prise pour la voie. Aucune voie n'est déclarée goudronnée par l'ICGC (une piste de
terre longeant une route serait sinon « goudronnée » à tort).

Usage : python scripts/icgc_tag.py --pbf data/region.osm.pbf --icgc data/icgc_vials.geojsonseq.gz --workdir data
        (réécrit --pbf en place ; liste des voies modifiées dans data/icgc_tagged.csv)
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import icgc_check as ic   # noqa: E402  (load_icgc, label_ways)
import landcover as lc    # noqa: E402  (load_ways)

ICGC_MIN_VOTE = 0.7
TAGGABLE = {"piste_g1", "piste_g2plus", "piste_sans", "sentier_sans"}      # classes OSM sans revêtement noté
SMALL_ROADS = {"unclassified", "service"}                                  # « route_sans » retenues


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pbf", required=True)
    ap.add_argument("--icgc", required=True)
    ap.add_argument("--workdir", required=True)
    args = ap.parse_args()
    t0 = time.time()
    pbf, wd = Path(args.pbf), Path(args.workdir)
    icgc_tree, icgc_cls = ic.load_icgc(Path(args.icgc))
    osm_tree, osm_cls, ids, _, hws = lc.load_ways(pbf, wd, with_highway=True)
    lab, share = ic.label_ways(osm_tree, icgc_tree, icgc_cls)
    hw = np.array([h or "" for h in hws])
    sel = (lab == "non_revetu") & (share >= ICGC_MIN_VOTE) & (
        np.isin(osm_cls, list(TAGGABLE)) | ((osm_cls == "route_sans") & np.isin(hw, list(SMALL_ROADS))))
    todo = {int(ids[k]) for k in np.flatnonzero(sel)}
    km = np.array([gm.length * 90.0 for gm in osm_tree.geometries])
    print(f"Voies à compléter (surface=unpaved) : {len(todo)} ; ~{km[sel].sum():.0f} km ; par classe : "
          + ", ".join(f"{c} {int((sel & (osm_cls == c)).sum())}" for c in sorted(set(osm_cls[sel]))), flush=True)
    with (wd / "icgc_tagged.csv").open("w", encoding="utf-8", newline="") as fh:
        wr = csv.writer(fh)
        wr.writerow(["osm", "id", "classe_osm", "highway", "vote_icgc", "longueur_km"])
        for k in np.flatnonzero(sel):
            wr.writerow([f"https://www.openstreetmap.org/way/{ids[k]}", ids[k], osm_cls[k], hw[k],
                         round(float(share[k]), 2), round(float(km[k]), 2)])

    import osmium
    tmp = pbf.with_name(pbf.stem + ".icgc.osm.pbf")
    if tmp.exists():
        tmp.unlink()
    changed = 0

    class Tagger(osmium.SimpleHandler):
        def __init__(self, writer):
            super().__init__()
            self.w = writer

        def node(self, n):
            self.w.add_node(n)

        def way(self, w):
            nonlocal changed
            if w.id in todo and "surface" not in w.tags:
                tags = {t.k: t.v for t in w.tags}
                tags["surface"] = "unpaved"
                tags["oyan:surface_source"] = "ICGC"
                self.w.add_way(w.replace(tags=tags))
                changed += 1
            else:
                self.w.add_way(w)

        def relation(self, r):
            self.w.add_relation(r)

    writer = osmium.SimpleWriter(str(tmp))
    try:
        Tagger(writer).apply_file(str(pbf))
    finally:
        writer.close()
    tmp.replace(pbf)
    print(f"Carte locale complétée : {changed} voies avec surface=unpaved (ICGC) ; {time.time() - t0:.0f} s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
