#!/usr/bin/env python3
"""Rapport « lieux remarquables » (diagnostic, rien n'est publié ; Florent, 03/10/2026 : Montserrat manquait).

Pour chaque lieu de scripts/remarkable_places.json :
  - où il est (élément OSM portant son tag wikidata, sinon la coordonnée Wikidata) ;
  - à quelle distance de la route la plus proche (GraphHopper /nearest) : au-delà de ACCESS_MAX_M, il est inaccessible tel
    quel (sommet, massif) et le rapport propose un point d'accès : un monastère, sanctuaire, château, belvédère… nommé, à
    moins de SEARCH_KM, collé à la route ; sinon le point de route le plus proche s'il est à moins de ROAD_FALLBACK_M ;
  - combien de boucles publiées y passent (champ « remarkable » des options) : un lieu célèbre à 0 boucle est un trou.

Sorties : places_report.md (lisible), places_report.json (points d'accès proposés, à relire avant de les écrire dans
remarkable_places.json, champ « point »).
"""
import argparse
import json
import math
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

ACCESS_MAX_M = 300.0
SEARCH_KM = 3.0
NEAR_ROAD_M = 120.0
ROAD_FALLBACK_M = 2000.0
ACCESS_TAGS = ["nw/amenity=monastery", "nw/amenity=place_of_worship", "nw/historic=monastery", "nw/historic=castle",
               "nw/historic=church", "nw/tourism=viewpoint", "nw/tourism=attraction", "nw/amenity=restaurant",
               "nw/tourism=alpine_hut", "n/natural=peak", "n/mountain_pass=yes"]


def hav(lon1, lat1, lon2, lat2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def export(pbf, workdir, filters, name):
    filt, out = Path(workdir) / f"{name}.osm.pbf", Path(workdir) / f"{name}.geojsonseq"
    subprocess.run(["osmium", "tags-filter", str(pbf), *filters, "-o", str(filt), "--overwrite"], check=True, capture_output=True)
    subprocess.run(["osmium", "export", str(filt), "-f", "geojsonseq", "-o", str(out), "--overwrite"], check=True,
                   capture_output=True)
    from shapely.geometry import shape
    feats = []
    for line in out.open(encoding="utf-8"):
        line = line.strip("\x1e\n ")
        if not line:
            continue
        try:
            f = json.loads(line)
            g = shape(f["geometry"])
        except (ValueError, KeyError):
            continue
        pt = g if g.geom_type == "Point" else g.representative_point()
        feats.append((pt.x, pt.y, f.get("properties", {})))
    return feats


PAVED_HW = "w/highway=primary,secondary,tertiary,unclassified,residential,living_street,service,cycleway,track,road"
UNPAVED = {"unpaved", "compacted", "fine_gravel", "gravel", "ground", "dirt", "grass", "sand", "pebblestone", "mud"}


def road_index(pbf, workdir):
    """Routes où passe un vélo de route : goudronnées ou sans revêtement noté hors pistes et chemins (les pistes ne
    comptent que si elles sont notées goudronnées). Index shapely en coordonnées locales (km)."""
    import shapely
    filt, out = Path(workdir) / "places_roads.osm.pbf", Path(workdir) / "places_roads.geojsonseq"
    subprocess.run(["osmium", "tags-filter", str(pbf), PAVED_HW, "-o", str(filt), "--overwrite"], check=True, capture_output=True)
    subprocess.run(["osmium", "export", str(filt), "-f", "geojsonseq", "--geometry-types=linestring", "-o", str(out),
                    "--overwrite"], check=True, capture_output=True)
    from shapely.geometry import shape
    lines = []
    for line in out.open(encoding="utf-8"):
        line = line.strip("\x1e\n ")
        if not line:
            continue
        try:
            f = json.loads(line)
        except ValueError:
            continue
        p = f.get("properties", {})
        sf, hw = p.get("surface"), p.get("highway")
        if (sf in UNPAVED or (hw in ("track", "service") and sf is None) or p.get("access") in ("private", "no")
                or p.get("bicycle") == "no"):
            continue
        try:
            lines.append(shape(f["geometry"]))
        except (ValueError, KeyError):
            continue
    return shapely.STRtree(lines), lines


def road_dist_m(tree, lines, lon, lat):
    """Distance (m) du point à la route goudronnée la plus proche, et ce point de route (lon, lat)."""
    import shapely
    from shapely.ops import nearest_points
    pt = shapely.Point(lon, lat)
    i = tree.query_nearest(pt)
    if not len(i):
        return None, None
    q = nearest_points(lines[int(i[0])], pt)[0]
    return hav(lon, lat, q.x, q.y), (q.x, q.y)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gh", required=True)
    ap.add_argument("--pbf", required=True)
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--site", default="https://florentbedouret-lgtm.github.io/velo-loops")
    ap.add_argument("--out", required=True)
    ap.add_argument("--out-md", required=True)
    a = ap.parse_args()
    places = json.loads((Path(__file__).parent / "remarkable_places.json").read_text(encoding="utf-8"))["places"]
    http = requests.Session()

    def nearest(lon, lat):
        try:
            r = http.get(f"{a.gh}/nearest", params={"point": f"{lat},{lon}"}, timeout=20)
            d = r.json()
            return d["coordinates"][0], d["coordinates"][1], float(d.get("distance", 0.0))
        except (requests.RequestException, ValueError, KeyError):
            return None

    # éléments OSM des lieux (tag wikidata) et candidats points d'accès
    osm = {}
    for x, y, p in export(a.pbf, a.workdir, ["nwr/wikidata=" + ",".join(places)], "places_wd"):
        osm.setdefault(p.get("wikidata"), []).append((x, y, p))
    cands = [(x, y, p) for x, y, p in export(a.pbf, a.workdir, ACCESS_TAGS, "places_access") if p.get("name")]
    rtree, rlines = road_index(a.pbf, a.workdir)
    print(f"Routes goudronnées indexées : {len(rlines)}", flush=True)
    print(f"Lieux : {len(places)} ; éléments OSM reliés : {sum(len(v) for v in osm.values())} ; candidats d'accès : {len(cands)}",
          flush=True)

    # passages dans les boucles publiées
    idx = http.get(f"{a.site}/web/data/index.json", timeout=60).json()
    passes, n_opt = {}, 0

    def fetch(sid):
        try:
            return http.get(f"{a.site}/web/data/starts/{sid}.json", timeout=60).json()
        except (requests.RequestException, ValueError):
            return None
    with ThreadPoolExecutor(max_workers=16) as ex:
        for d in ex.map(fetch, [s["id"] for s in idx["starts"]]):
            for o in (d or {}).get("options", []):
                n_opt += 1
                for r in o.get("remarkable") or []:
                    passes[r["n"]] = passes.get(r["n"], 0) + 1

    rows = []
    for qid, e in places.items():
        el = osm.get(qid, [])
        kinds = e.get("kind", [])
        best_el = None                                     # élément OSM du bon type (sommet, col, belvédère) en priorité
        for x, y, p in el:
            if p.get("natural") in ("peak", "saddle") or p.get("tourism") == "viewpoint" or p.get("mountain_pass") == "yes":
                best_el = (x, y, p)
                break
        if best_el is None and el:
            best_el = el[0]
        lon, lat = (best_el[0], best_el[1]) if best_el else tuple(e["coord"])
        osm_name = best_el[2].get("name") if best_el else None
        snap, road_pt = road_dist_m(rtree, rlines, lon, lat)
        row = {"qid": qid, "label": e["label"], "sitelinks": e["sitelinks"], "kind": kinds, "lon": round(lon, 6),
               "lat": round(lat, 6), "osm": bool(best_el), "osm_name": osm_name, "road_m": None if snap is None else round(snap),
               "passes": sum(passes.get(n, 0) for n in {e["label"], osm_name, e.get("point_name")} if n)}
        if snap is not None and snap > ACCESS_MAX_M:      # inaccessible tel quel : chercher un point d'accès
            best = None
            for x, y, p in cands:
                d0 = hav(lon, lat, x, y)
                if d0 > SEARCH_KM * 1000.0 or (abs(x - lon) < 1e-6 and abs(y - lat) < 1e-6):
                    continue
                s2, _ = road_dist_m(rtree, rlines, x, y)
                if s2 is not None and s2 <= NEAR_ROAD_M and (best is None or d0 < best[3]):
                    best = (x, y, p, d0)
            if best:
                row["access"] = {"point": [round(best[0], 6), round(best[1], 6)], "via": best[2].get("name"),
                                 "tags": {k: v for k, v in best[2].items() if k in ("amenity", "historic", "tourism", "natural")},
                                 "dist_m": round(best[3])}
            elif snap <= ROAD_FALLBACK_M:
                row["access"] = {"point": [round(road_pt[0], 6), round(road_pt[1], 6)], "via": "route goudronnée la plus proche",
                                 "dist_m": round(snap)}
        rows.append(row)

    Path(a.out).write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    L = ["# Lieux remarquables : accessibilité et passages", "",
         f"{len(rows)} lieux ; {n_opt} options publiées lues. « route » = distance du lieu à la route goudronnée la plus proche ; "
         f"au-delà de {ACCESS_MAX_M:.0f} m, point d'accès proposé (à relire).", "",
         "| lieu | articles | type | dans OSM | route (m) | boucles qui y passent | point d'accès proposé |", "|---|---|---|---|---|---|---|"]
    for r in sorted(rows, key=lambda r: -r["sitelinks"]):
        acc = r.get("access")
        L.append(f"| {r['label']} | {r['sitelinks']} | {', '.join(r['kind'])} | {'oui' if r['osm'] else 'non'} | "
                 f"{r['road_m']} | {r['passes']} | "
                 + (f"{acc['via']} ({acc['dist_m']} m ; {acc['point'][1]:.5f},{acc['point'][0]:.5f})" if acc else "") + " |")
    holes = [r for r in rows if r["passes"] == 0 and r["sitelinks"] >= 10]
    L += ["", f"Lieux célèbres (≥ 10 articles) sans aucune boucle : {len(holes)} — "
          + ", ".join(r["label"] for r in sorted(holes, key=lambda r: -r["sitelinks"]))]
    Path(a.out_md).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L), flush=True)


if __name__ == "__main__":
    main()
