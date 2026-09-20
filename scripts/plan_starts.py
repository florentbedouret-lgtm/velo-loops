#!/usr/bin/env python3
"""
Planifie les points de départ pour que « n'importe quel lieu habité » soit proche d'un départ pré-calculé,
et MESURE la couverture obtenue (distance médiane, P90, P95, maximale) à partir des données OpenStreetMap.

Lieux habités (demande) : nœuds place=* (ville, village, hameau, quartier…) + points échantillonnés tous les ~400 m
dans les zones landuse=residential.
Départs candidats : centres de villes/villages, gares (train), puis départs de « remplissage » placés là où un lieu
habité reste trop loin du départ le plus proche (algorithme du point le plus éloigné d'abord).

Sorties :
  --out     JSON de départs [{name, lon, lat, kind}] pour generate_loops.py --starts-file
  --report  JSON de couverture (pour l'expert produit : quelle distance acceptable ?)
"""
from __future__ import annotations

import argparse
import json
import math
import shutil
import statistics
import subprocess
import sys
from pathlib import Path

import numpy as np
import shapely
from shapely.geometry import shape

PLACE_DENSE = {"city": 3, "town": 2, "village": 1, "suburb": 1, "neighbourhood": 0, "quarter": 0}
PLACE_RURAL = {"hamlet", "isolated_dwelling"}
NOT_TRAIN = {"subway", "light_rail", "monorail", "tram"}


def extract(pbf: Path, workdir: Path) -> Path:
    if not shutil.which("osmium"):
        sys.exit("osmium introuvable")
    filt, out = workdir / "plan.osm.pbf", workdir / "plan.geojsonseq"
    filters = ["n/place=city,town,village,hamlet,suburb,neighbourhood,quarter,isolated_dwelling",
               "n/railway=station", "wr/landuse=residential"]
    subprocess.run(["osmium", "tags-filter", str(pbf), *filters, "-o", str(filt), "--overwrite"],
                   check=True, capture_output=True)
    subprocess.run(["osmium", "export", str(filt), "-f", "geojsonseq", "-o", str(out), "--overwrite"],
                   check=True, capture_output=True)
    return out


def load(seq: Path):
    places, stations, residential = [], [], []
    with seq.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip("\x1e\n ")
            if not line:
                continue
            try:
                feat = json.loads(line)
                geom = shape(feat["geometry"])
            except (ValueError, KeyError):
                continue
            props = feat.get("properties", {})
            if geom.is_empty:
                continue
            if geom.geom_type == "Point":
                if props.get("place"):
                    places.append((geom.x, geom.y, props["place"], props.get("name")))
                elif props.get("railway") == "station" and props.get("station") not in NOT_TRAIN:
                    stations.append((geom.x, geom.y, props.get("name") or "Gare"))
            elif geom.geom_type in ("Polygon", "MultiPolygon") and props.get("landuse") == "residential":
                residential.append(geom)
    return places, stations, residential


class Proj:
    def __init__(self, lat0: float):
        self.lat0 = lat0
        self.kx = 111.32 * math.cos(math.radians(lat0))
        self.ky = 110.54

    def xy(self, lon, lat):
        return (np.asarray(lon) * self.kx, np.asarray(lat) * self.ky)

    def lonlat(self, x, y):
        return x / self.kx, y / self.ky


def sample_residential(polys, proj: Proj, step_km=0.4):
    pts = []
    dlon, dlat = step_km / proj.kx, step_km / proj.ky
    for g in polys:
        x0, y0, x1, y1 = g.bounds
        xs = np.arange(x0 + dlon / 2, x1, dlon)
        ys = np.arange(y0 + dlat / 2, y1, dlat)
        if len(xs) and len(ys):
            gx, gy = np.meshgrid(xs, ys)
            gx, gy = gx.ravel(), gy.ravel()
            inside = shapely.contains_xy(g, gx, gy)
            pts.extend(zip(gx[inside], gy[inside]))
            if inside.any():
                continue
        rp = g.representative_point()
        pts.append((rp.x, rp.y))
    return pts


def nearest_dist(px, py, sx, sy):
    """Distance (km) de chaque point (px, py) au départ le plus proche (sx, sy)."""
    out = np.full(len(px), np.inf)
    for start in range(0, len(sx), 100):
        dx = px[:, None] - sx[None, start:start + 100]
        dy = py[:, None] - sy[None, start:start + 100]
        out = np.minimum(out, np.sqrt(dx * dx + dy * dy).min(axis=1))
    return out


def plan(dmax_km, dmax_rural_km, seeds, dem_x, dem_y, dem_rural):
    """seeds : [(x, y, kind, name)] ; remplissage par point le plus éloigné (en multiple de dmax)."""
    sx = [s[0] for s in seeds]
    sy = [s[1] for s in seeds]
    kinds = [s[2] for s in seeds]
    lim = np.where(dem_rural, dmax_rural_km, dmax_km)
    d = nearest_dist(dem_x, dem_y, np.array(sx), np.array(sy)) if seeds else np.full(len(dem_x), np.inf)
    added = []
    while True:
        ratio = d / lim
        i = int(np.argmax(ratio))
        if ratio[i] <= 1.0:
            break
        sx.append(dem_x[i]); sy.append(dem_y[i]); kinds.append("fill"); added.append(len(sx) - 1)
        d = np.minimum(d, np.sqrt((dem_x - dem_x[i]) ** 2 + (dem_y - dem_y[i]) ** 2))
    return np.array(sx), np.array(sy), kinds, d


def pctl(v, p):
    v = np.sort(v)
    return float(v[min(len(v) - 1, int(p / 100 * len(v)))])


def describe(d):
    if len(d) == 0:
        return {}
    return {"median_km": round(float(np.median(d)), 2), "p90_km": round(pctl(d, 90), 2),
            "p95_km": round(pctl(d, 95), 2), "max_km": round(float(np.max(d)), 2),
            "within_1km": round(float((d <= 1).mean()), 3), "within_2km": round(float((d <= 2).mean()), 3),
            "within_3km": round(float((d <= 3).mean()), 3), "within_5km": round(float((d <= 5).mean()), 3)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pbf", required=True)
    ap.add_argument("--workdir", default=None, help="dossier de travail (défaut : dossier du .pbf)")
    ap.add_argument("--out", required=True, help="JSON de départs")
    ap.add_argument("--report", required=True, help="JSON de couverture")
    ap.add_argument("--dmax-km", type=float, default=2.0, help="distance maximale visée (zones habitées)")
    ap.add_argument("--dmax-rural-km", type=float, default=None, help="distance maximale visée (hameaux), défaut 2 × dmax")
    ap.add_argument("--no-stations", action="store_true", help="ne pas utiliser les gares comme départs")
    args = ap.parse_args()

    pbf = Path(args.pbf)
    seq = extract(pbf, Path(args.workdir) if args.workdir else pbf.parent)
    places, stations, residential = load(seq)
    if not places and not residential:
        sys.exit("aucun lieu habité trouvé dans les données")
    proj = Proj(statistics.mean([p[1] for p in places]) if places else 41.0)
    dmax_rural = args.dmax_rural_km or 2 * args.dmax_km

    # --- demande : lieux habités
    dem = {}
    for lon, lat, kind, _ in places:
        x, y = proj.xy(lon, lat)
        dem[(round(float(x) / 0.25), round(float(y) / 0.25))] = (float(x), float(y), kind in PLACE_RURAL)
    for lon, lat in sample_residential(residential, proj):
        x, y = proj.xy(lon, lat)
        dem.setdefault((round(float(x) / 0.25), round(float(y) / 0.25)), (float(x), float(y), False))
    dem_x = np.array([v[0] for v in dem.values()])
    dem_y = np.array([v[1] for v in dem.values()])
    dem_rural = np.array([v[2] for v in dem.values()])

    # --- graines : centres de villes/villages puis gares (fusion à moins de 0,5 / 0,7 km)
    seeds = []

    def far_enough(x, y, km):
        return all(math.hypot(x - s[0], y - s[1]) >= km for s in seeds)

    for lon, lat, kind, name in sorted((p for p in places if p[2] in ("city", "town", "village") and p[3]),
                                       key=lambda p: -PLACE_DENSE[p[2]]):
        x, y = proj.xy(lon, lat)
        if far_enough(float(x), float(y), 0.5):
            seeds.append((float(x), float(y), "place", name))
    if not args.no_stations:
        for lon, lat, name in stations:
            x, y = proj.xy(lon, lat)
            if far_enough(float(x), float(y), 0.7):
                seeds.append((float(x), float(y), "station", name))

    # --- plan pour plusieurs distances maximales (le comptage de départs dépend fortement de ce choix)
    named = [(*proj.xy(p[0], p[1]), p[3]) for p in places if p[3]]
    nx = np.array([float(n[0]) for n in named]); ny = np.array([float(n[1]) for n in named])
    report = {"demand_points": int(len(dem_x)), "place_nodes": len(places), "residential_polygons": len(residential),
              "stations": len(stations), "seeds": len(seeds), "by_dmax_km": {}}
    chosen = None
    for dm in sorted({1.0, 1.5, 2.0, 3.0, args.dmax_km}):
        sx, sy, kinds, d = plan(dm, args.dmax_rural_km or 2 * dm, seeds, dem_x, dem_y, dem_rural)
        report["by_dmax_km"][f"{dm:g}"] = {"starts": int(len(sx)), "places": kinds.count("place"),
                                          "stations": kinds.count("station"), "fill": kinds.count("fill"),
                                          "coverage": describe(d)}
        if abs(dm - args.dmax_km) < 1e-9:
            chosen = (sx, sy, kinds, d)
    sx, sy, kinds, d = chosen

    # --- fichier de départs (noms uniques ; les départs de remplissage portent le nom du lieu le plus proche)
    starts, names_used = [], {}
    for i in range(len(sx)):
        lon, lat = proj.lonlat(sx[i], sy[i])
        if kinds[i] == "fill" and len(nx):
            j = int(np.argmin(np.hypot(nx - sx[i], ny - sy[i])))
            dx, dy = sx[i] - nx[j], sy[i] - ny[j]
            compass = ["est", "nord-est", "nord", "nord-ouest", "ouest", "sud-ouest", "sud", "sud-est"]
            direction = compass[int(round(math.degrees(math.atan2(dy, dx)) / 45.0)) % 8]
            name = f"{named[j][2]} ({direction})"
        elif kinds[i] == "place":
            name = seeds[i][3]
        else:
            name = seeds[i][3] if i < len(seeds) else "Départ"
            if kinds[i] == "station":
                name = f"Gare {name}" if not str(name).lower().startswith("gare") else name
        names_used[name] = names_used.get(name, 0) + 1
        if names_used[name] > 1:
            name = f"{name} {names_used[name]}"
        starts.append({"name": name, "lon": round(float(lon), 5), "lat": round(float(lat), 5), "kind": kinds[i]})
    Path(args.out).write_text(json.dumps(starts, ensure_ascii=False, indent=1), encoding="utf-8")
    report["chosen_dmax_km"] = args.dmax_km
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"Lieux habités (points de demande) : {report['demand_points']} "
          f"({report['place_nodes']} nœuds place=*, {report['residential_polygons']} zones résidentielles)")
    print(f"{'Dmax':>6} {'départs':>8} {'lieux':>6} {'gares':>6} {'remplis.':>9} {'médiane':>8} {'P90':>6} {'P95':>6} {'max':>6}")
    for dm, r in report["by_dmax_km"].items():
        c = r["coverage"]
        print(f"{dm:>4} km {r['starts']:>8} {r['places']:>6} {r['stations']:>6} {r['fill']:>9} "
              f"{c['median_km']:>6} km {c['p90_km']:>4} {c['p95_km']:>6} {c['max_km']:>6}")
    print(f"-> {len(starts)} départs écrits dans {args.out} (Dmax visé {args.dmax_km} km)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
