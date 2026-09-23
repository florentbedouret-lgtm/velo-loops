#!/usr/bin/env python3
"""
Planifie les points de départ pré-calculés et MESURE la couverture obtenue, par type de zone.

Idée : « n'importe quel lieu habité » doit être à moins de `dmax` du départ le plus proche, avec un `dmax` qui dépend du
type de zone (dense, périphérie, rural). Les lieux habités (demande) sont :
  - les nœuds OpenStreetMap place=* (ville, village, hameau, quartier…) ;
  - des points échantillonnés tous les 400 m dans les zones landuse=residential.

Type de zone (`dense` / `peri` / `rural`) = part du sol résidentiel autour du point, mesurée dans un carré de 3,5 km
de côté (7 x 7 cellules de 0,5 km ; points de landuse=residential x 0,16 km² / 12,25 km²). Seuils par défaut :
>= 45 % dense, >= 15 % périphérie, sinon rural. Un carré large distingue une grande agglomération d'un petit village
(un village de 1 km de côté n'occupe qu'environ 5 % du carré). Les hameaux isolés sont toujours « rural ». Le rapport
donne la densité et la zone de lieux de référence (Barcelone, Sabadell, Vic, Marganell…) pour vérifier les seuils, ainsi
que la répartition obtenue pour trois couples de seuils.

Départs candidats : centres de villes / villages / quartiers, gares (hors zone dense par défaut). Choix par
« couverture maximale » : on retient à chaque étape le candidat qui couvre le plus de lieux habités non couverts, puis
on complète par des départs de remplissage (nom du lieu le plus proche + direction) là où un lieu reste trop loin.

Sorties :
  --out          JSON de départs [{name, lon, lat, kind, zone}] pour generate_loops.py --starts-file
  --report       JSON de couverture (par scénario et par zone : nombre de départs, distances médiane / P90 / P95 / max,
                 temps de calcul et taille estimés)
  --demand-out   échantillon de lieux habités avec leur départ le plus proche (pour measure_detour.py)
"""
from __future__ import annotations

import argparse
import json
import math
import random
import re
import shutil
import statistics
import subprocess
import sys
from pathlib import Path

import numpy as np
import shapely
from shapely.geometry import shape

PRIORITY = {"city": 4, "town": 3, "village": 2, "suburb": 2, "neighbourhood": 1, "quarter": 1, "station": 1}
CANDIDATE_PLACES = {"city", "town", "village", "suburb"}
RURAL_PLACES = {"hamlet", "isolated_dwelling"}
CHECK_PLACES = ("Barcelona;l'Eixample;l'Hospitalet de Llobregat;Badalona;Sabadell;Terrassa;Mataró;Sant Cugat del Vallès;"
                "Granollers;Vilanova i la Geltrú;Sitges;Manresa;Vic;Igualada;Berga;Sant Celoni;Cardedeu;Montcada i Reixac;"
                "Cerdanyola del Vallès;Rubí;Castelldefels;Calella;Marganell;Rajadell;Callús;Montseny;Collbató")
NOT_TRAIN = {"subway", "light_rail", "monorail", "tram", "funicular", "aerialway", "cable_car"}
NOT_TRAIN_NAME = re.compile(r"\b(aeri|cremallera|funicular|telef[eè]ric|cable ?car)\b", re.IGNORECASE)
ZONES = ("dense", "peri", "rural")
SCENARIOS = {"A": (2.0, 3.0, 4.0), "B": (3.0, 3.0, 4.0)}
CELL_KM = 0.5
SAMPLE_KM2 = 0.16                 # surface résidentielle représentée par un point d'échantillonnage (0,4 km x 0,4 km)
SEC_PER_START = (35, 65)          # MESURÉ (index.json stats.seconds_per_computed_start_by_zone, run du
                                   # 22/09/2026, scénario 2/3/3, 4 durées, sans réutilisation) : 35,4-38,2 s/départ
                                   # en moyenne par zone (max isolés jusqu'à ~108 s en rural) ; remplace une
                                   # estimation (80-190 s) qui surestimait le calcul de 2 à 5x. Toujours pour
                                   # 4 durées : le nombre réel de durées n'est connu qu'en génération, pas ici.
KB_PER_START = 150                # MESURÉ (taille réelle des fichiers web/data/starts/*.json, 4 durées) ;
                                   # remplace une estimation (170 Ko)


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
    places, stations, residential, place_pop = [], [], [], []
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
                    pop = re.sub(r"[^0-9]", "", str(props.get("population", "")))
                    if pop:
                        place_pop.append((geom.x, geom.y, props["place"], props.get("name"), int(pop)))
                elif (props.get("railway") == "station" and props.get("station") not in NOT_TRAIN
                      and not NOT_TRAIN_NAME.search(props.get("name") or "")):
                    stations.append((geom.x, geom.y, props.get("name") or "Gare"))
            elif geom.geom_type in ("Polygon", "MultiPolygon") and props.get("landuse") == "residential":
                residential.append(geom)
    return places, stations, residential, place_pop


class Proj:
    def __init__(self, lat0: float):
        self.kx = 111.32 * math.cos(math.radians(lat0))
        self.ky = 110.54

    def xy(self, lon, lat):
        return float(lon) * self.kx, float(lat) * self.ky

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


class Density:
    """Part du sol résidentiel autour d'un point (carré de (2r+1) x 0,5 km de côté)."""

    def __init__(self, res_xy):
        self.counts: dict = {}
        for x, y in res_xy:
            k = (math.floor(x / CELL_KM), math.floor(y / CELL_KM))
            self.counts[k] = self.counts.get(k, 0) + 1

    def frac(self, x, y, r=3) -> float:
        cx, cy = math.floor(x / CELL_KM), math.floor(y / CELL_KM)
        s = sum(self.counts.get((cx + i, cy + j), 0) for i in range(-r, r + 1) for j in range(-r, r + 1))
        return min(1.0, s * SAMPLE_KM2 / (((2 * r + 1) * CELL_KM) ** 2))


def zone_of(frac: float, dense: float, peri: float) -> str:
    return "dense" if frac >= dense else "peri" if frac >= peri else "rural"


def dist_to_nearest(px, py, sx, sy):
    out = np.full(len(px), np.inf)
    sx, sy = np.asarray(sx), np.asarray(sy)
    for a in range(0, len(sx), 100):
        dx = px[:, None] - sx[None, a:a + 100]
        dy = py[:, None] - sy[None, a:a + 100]
        out = np.minimum(out, np.sqrt(dx * dx + dy * dy).min(axis=1))
    return out


def nearest_index(px, py, sx, sy):
    best = np.full(len(px), np.inf)
    idx = np.zeros(len(px), dtype=int)
    sx, sy = np.asarray(sx), np.asarray(sy)
    for a in range(0, len(sx), 100):
        dx = px[:, None] - sx[None, a:a + 100]
        dy = py[:, None] - sy[None, a:a + 100]
        d = np.sqrt(dx * dx + dy * dy)
        j = d.argmin(axis=1)
        dm = d[np.arange(len(px)), j]
        better = dm < best
        best[better], idx[better] = dm[better], a + j[better]
    return idx, best


def pctl(v, p):
    v = np.sort(v)
    return float(v[min(len(v) - 1, int(p / 100 * len(v)))])


def describe(d):
    if len(d) == 0:
        return {"n": 0}
    return {"n": int(len(d)), "median_km": round(float(np.median(d)), 2), "p90_km": round(pctl(d, 90), 2),
            "p95_km": round(pctl(d, 95), 2), "max_km": round(float(np.max(d)), 2)}


def plan(limits, cand, dem):
    """Couverture maximale puis remplissage. limits = {zone: dmax km}. Retourne (départs, distances)."""
    lim = np.array([limits[z] for z in dem["zone"]])
    cx, cy = cand["x"], cand["y"]
    covered = []
    for a in range(0, len(cx), 50):
        dx = cx[a:a + 50, None] - dem["x"][None, :]
        dy = cy[a:a + 50, None] - dem["y"][None, :]
        mask = np.sqrt(dx * dx + dy * dy) <= lim[None, :]
        covered.extend(np.nonzero(row)[0] for row in mask)
    uncovered = np.ones(len(dem["x"]), dtype=bool)
    prio = np.array([PRIORITY.get(k, 1) for k in cand["kind"]], dtype=float)
    chosen = []
    while True:
        gains = np.array([uncovered[c].sum() for c in covered], dtype=float)
        if len(gains) == 0 or gains.max() < 1:
            break
        j = int(np.argmax(gains + 0.001 * prio))
        chosen.append(j)
        uncovered[covered[j]] = False
    sx = [cx[j] for j in chosen]
    sy = [cy[j] for j in chosen]
    kinds = [cand["kind"][j] for j in chosen]
    names = [cand["name"][j] for j in chosen]
    d = dist_to_nearest(dem["x"], dem["y"], sx, sy) if sx else np.full(len(dem["x"]), np.inf)
    rng = np.random.default_rng(3)
    while True:                                  # remplissage : le point qui couvre le plus de lieux non couverts
        unc = np.nonzero(d > lim)[0]
        if len(unc) == 0:
            break
        pool = unc if len(unc) <= 400 else rng.choice(unc, 400, replace=False)
        ddx = dem["x"][pool][:, None] - dem["x"][unc][None, :]
        ddy = dem["y"][pool][:, None] - dem["y"][unc][None, :]
        gain = (np.sqrt(ddx * ddx + ddy * ddy) <= lim[unc][None, :]).sum(axis=1)
        i = int(pool[int(np.argmax(gain))])
        sx.append(dem["x"][i]); sy.append(dem["y"][i]); kinds.append("fill"); names.append(None)
        d = np.minimum(d, np.sqrt((dem["x"] - dem["x"][i]) ** 2 + (dem["y"] - dem["y"][i]) ** 2))
    return {"x": np.array(sx), "y": np.array(sy), "kind": kinds, "name": names}, d


def summarise(limits, starts, d, dem, frac_fn, dense_frac, peri_frac):
    zones = [zone_of(frac_fn(x, y), dense_frac, peri_frac) for x, y in zip(starts["x"], starts["y"])]
    dz = np.array(dem["zone"])
    cov = {"all": describe(d)}
    for z in ZONES:
        cov[z] = describe(d[dz == z])
    n = len(starts["x"])
    return {
        "limits_km": dict(zip(ZONES, limits)),
        "starts": n,
        "starts_by_zone": {z: zones.count(z) for z in ZONES},
        "starts_by_kind": {k: starts["kind"].count(k) for k in ("city", "town", "village", "suburb", "station", "fill")
                           if starts["kind"].count(k)},
        "coverage_by_demand_zone": cov,
        "cost_estimate": {
            "compute_hours_one_thread": [round(n * SEC_PER_START[0] / 3600, 1), round(n * SEC_PER_START[1] / 3600, 1)],
            "compute_hours_3_workers": [round(n * SEC_PER_START[0] / 3600 / 2.25, 1),
                                        round(n * SEC_PER_START[1] / 3600 / 2.25, 1)],
            "size_mb": round(n * KB_PER_START / 1024, 1),
            "status": "estimé : à remplacer par la mesure (stats.seconds_per_start de index.json)",
        },
    }, zones


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pbf", required=True)
    ap.add_argument("--workdir", default=None, help="dossier de travail (défaut : dossier du .pbf)")
    ap.add_argument("--out", required=True, help="JSON de départs du scénario choisi")
    ap.add_argument("--report", required=True, help="JSON de couverture (tous les scénarios)")
    ap.add_argument("--demand-out", default=None, help="échantillon de lieux habités pour measure_detour.py")
    ap.add_argument("--sample-per-zone", type=int, default=300, help="taille de l'échantillon par zone (0 = tous les lieux)")
    ap.add_argument("--scenario", default="A", help="A, B ou 'dense,peri,rural' en km (ex. 2,3,3) ; plusieurs séparés par ; "
                                                     "(ex. 2,3,3;2,3.5,4) : le premier produit --out, tous sont chiffrés")
    ap.add_argument("--dense-frac", type=float, default=0.45)
    ap.add_argument("--peri-frac", type=float, default=0.15)
    ap.add_argument("--box-cells", type=int, default=3, help="rayon du carré de densité en cellules de 0,5 km (3 = carré de 3,5 km)")
    ap.add_argument("--no-force-towns", action="store_true", help="ne pas imposer un départ dans chaque ville (place=city/town)")
    ap.add_argument("--check-places", default=CHECK_PLACES, help="lieux de référence dont on affiche la densité (séparés par ;)")
    ap.add_argument("--stations", choices=["outside_dense", "all", "none"], default="outside_dense")
    ap.add_argument("--rural-demand", choices=["all", "no_isolated", "villages_up"], default="no_isolated",
                    help="lieux à couvrir : all = tout ; no_isolated (défaut) = sans habitations isolées ; villages_up = sans "
                         "habitations isolées ni hameaux (le rapport chiffre les trois variantes)")
    args = ap.parse_args()

    pbf = Path(args.pbf)
    seq = extract(pbf, Path(args.workdir) if args.workdir else pbf.parent)
    places, stations, residential, place_pop = load(seq)
    if not places and not residential:
        sys.exit("aucun lieu habité trouvé dans les données")
    proj = Proj(statistics.mean([p[1] for p in places]) if places else 41.0)
    res_xy = [proj.xy(lon, lat) for lon, lat in sample_residential(residential, proj)]
    density = Density(res_xy)
    R = args.box_cells
    frac_fn = lambda x, y: density.frac(x, y, R)  # noqa: E731

    # --- demande : nœuds place=* et points résidentiels (dédoublonnés à 250 m)
    dem_map = {}
    for lon, lat, kind, _ in places:
        x, y = proj.xy(lon, lat)
        dem_map[(round(x / 0.25), round(y / 0.25))] = (x, y, kind in RURAL_PLACES, kind)
    for x, y in res_xy:
        dem_map.setdefault((round(x / 0.25), round(y / 0.25)), (x, y, False, "residential"))
    dx = np.array([v[0] for v in dem_map.values()]); dy = np.array([v[1] for v in dem_map.values()])
    forced_rural = np.array([v[2] for v in dem_map.values()])
    dkind = np.array([v[3] for v in dem_map.values()])
    fr = np.array([frac_fn(x, y) for x, y in zip(dx, dy)])

    def keep_mask(policy):
        if policy == "no_isolated":
            return dkind != "isolated_dwelling"
        if policy == "villages_up":
            return ~np.isin(dkind, ["isolated_dwelling", "hamlet"])
        return np.ones(len(dx), dtype=bool)

    def demand_for(dense_frac, peri_frac, mask=None):
        zones = ["rural" if forced_rural[i] else zone_of(fr[i], dense_frac, peri_frac) for i in range(len(dx))]
        if mask is None:
            return {"x": dx, "y": dy, "zone": zones, "kind": dkind}
        return {"x": dx[mask], "y": dy[mask], "zone": [z for z, m in zip(zones, mask) if m], "kind": dkind[mask]}

    dem_full = demand_for(args.dense_frac, args.peri_frac)
    dem = demand_for(args.dense_frac, args.peri_frac, keep_mask(args.rural_demand))

    # --- candidats : centres de villes / villages / quartiers ; les gares sont ajoutées ensuite (voir add_stations)
    def candidates():
        cx, cy, kind, name = [], [], [], []
        for lon, lat, k, nm in places:
            if k in CANDIDATE_PLACES and nm:
                x, y = proj.xy(lon, lat)
                cx.append(x); cy.append(y); kind.append(k); name.append(nm)
        return {"x": np.array(cx), "y": np.array(cy), "kind": kind, "name": name}

    def add_stations(starts, policy):
        """Ajoute les gares (train) à plus de 0,7 km d'un départ existant ; 'outside_dense' écarte celles en zone dense."""
        if policy == "none":
            return starts
        sx, sy = list(starts["x"]), list(starts["y"])
        kinds, names = list(starts["kind"]), list(starts["name"])
        for lon, lat, nm in stations:
            x, y = proj.xy(lon, lat)
            if policy == "outside_dense" and zone_of(frac_fn(x, y), args.dense_frac, args.peri_frac) == "dense":
                continue
            if sx and min(math.hypot(x - a, y - b) for a, b in zip(sx, sy)) < 0.7:
                continue
            sx.append(x); sy.append(y); kinds.append("station")
            names.append(nm if str(nm).lower().startswith("gare") else f"Gare {nm}")
        return {"x": np.array(sx), "y": np.array(sy), "kind": kinds, "name": names}

    scen = dict(SCENARIOS)                      # A et B sont toujours calculés, pour comparer
    chosen_key = None
    for part in [x.strip() for x in args.scenario.split(";") if x.strip()]:
        if part in SCENARIOS:
            key = part
        else:                                   # trio dense,peri,rural en km ; plusieurs trios séparés par « ; »
            vals = tuple(float(v) for v in part.split(","))
            key = "/".join(f"{v:g}" for v in vals)
            scen[key] = vals
        chosen_key = chosen_key or key          # le premier scénario cité produit le fichier de départs

    report = {
        "method": "densité résidentielle (carré de 3,5 km) → zone ; couverture maximale + remplissage",
        "thresholds": {"dense_frac": args.dense_frac, "peri_frac": args.peri_frac, "box_km": (2 * R + 1) * CELL_KM},
        "inputs": {"place_nodes": len(places), "stations": len(stations), "residential_polygons": len(residential),
                   "demand_points": int(len(dx))},
        "demand_by_zone": {z: dem["zone"].count(z) for z in ZONES},
        "density_quantiles_of_demand": {str(q): round(float(np.quantile(fr, q / 100)), 3) for q in (10, 25, 50, 75, 90)},
        "zone_sensitivity": {f"dense>={a},peri>={b}": {z: demand_for(a, b)["zone"].count(z) for z in ZONES}
                             for a, b in ((0.35, 0.12), (0.45, 0.15), (0.55, 0.20))},
        "scenarios": {},
    }
    def add_towns(starts):
        """Ajoute un départ dans chaque ville (place=city/town) à plus de 1 km d'un départ existant."""
        if args.no_force_towns:
            return starts, 0
        sx, sy = list(starts["x"]), list(starts["y"])
        kinds, names = list(starts["kind"]), list(starts["name"])
        added = 0
        for lon, lat, k, nm in places:
            if k in ("city", "town") and nm:
                x, y = proj.xy(lon, lat)
                if sx and min(math.hypot(x - a, y - b) for a, b in zip(sx, sy)) < 1.0:
                    continue
                sx.append(x); sy.append(y); kinds.append(k); names.append(nm); added += 1
        return {"x": np.array(sx), "y": np.array(sy), "kind": kinds, "name": names}, added

    ref = {}
    by_place = {}
    for lon, lat, k, nm in places:
        if nm and (nm not in by_place or PRIORITY.get(k, 0) > PRIORITY.get(by_place[nm][2], 0)):
            by_place[nm] = (lon, lat, k)
    for nm in [c.strip() for c in args.check_places.split(";") if c.strip()]:
        if nm in by_place:
            x, y = proj.xy(by_place[nm][0], by_place[nm][1])
            f15, f35 = density.frac(x, y, 1), density.frac(x, y, 3)
            ref[nm] = {"density_1_5km": round(f15, 2), "density_3_5km": round(f35, 2),
                       "zone": zone_of(frac_fn(x, y), args.dense_frac, args.peri_frac)}
        else:
            ref[nm] = None
    report["reference_places"] = ref
    big = [(nm, k, pop, zone_of(frac_fn(*proj.xy(lon, lat)), args.dense_frac, args.peri_frac))
           for lon, lat, k, nm, pop in place_pop if pop >= 20000 and k in ("city", "town", "village", "suburb")]
    report["places_over_20000"] = {
        "note": "lieux place=city/town/village/suburb avec un tag population >= 20 000 ; zone calculée au nœud du lieu",
        "places_with_population_tag": len(place_pop), "places_over_20000": len(big),
        "by_zone": {z: sum(1 for b in big if b[3] == z) for z in ZONES},
        "rural": sorted([{"name": b[0], "kind": b[1], "population": b[2]} for b in big if b[3] == "rural"], key=lambda x: -x["population"]),
        "peri": sorted([{"name": b[0], "kind": b[1], "population": b[2]} for b in big if b[3] == "peri"], key=lambda x: -x["population"])[:40]}

    def run_plan(limits, policy):
        mask = keep_mask(policy)
        dem_p = demand_for(args.dense_frac, args.peri_frac, mask)
        lim_map = dict(zip(ZONES, limits))
        base, _ = plan(lim_map, candidates(), dem_p)
        base, towns_added = add_towns(base)
        starts = add_stations(base, args.stations)
        d = dist_to_nearest(dem_p["x"], dem_p["y"], starts["x"], starts["y"])
        return starts, d, dem_p, mask, towns_added, base

    def skipped_stats(starts, mask):
        if mask.all():
            return None
        d_skip = dist_to_nearest(dx[~mask], dy[~mask], starts["x"], starts["y"])
        out = describe(d_skip)
        out["within_4km"] = round(float((d_skip <= 4).mean()), 3)
        out["within_6km"] = round(float((d_skip <= 6).mean()), 3)
        return out

    results = {}
    for key, limits in scen.items():
        starts, d, dem_p, mask, towns_added, base = run_plan(limits, args.rural_demand)
        variants = {}
        for policy in ("outside_dense", "all", "none"):
            variants[policy] = int(len(add_stations(base, policy)["x"]))
        summ, zones = summarise(limits, starts, d, dem_p, frac_fn, args.dense_frac, args.peri_frac)
        summ["rural_demand"] = args.rural_demand
        summ["demand_points_kept"] = int(mask.sum())
        summ["skipped_demand_distance_to_nearest_start"] = skipped_stats(starts, mask)
        summ["starts_without_stations"] = int(len(base["x"]))
        summ["towns_forced_added"] = towns_added
        summ["starts_by_stations_policy"] = variants
        report["scenarios"][key] = summ
        results[key] = (starts, d, zones)

    # variantes de la demande rurale (habitations isolées, hameaux) : le plus gros levier sur le nombre de départs
    report["rural_demand_variants"] = {"kinds_in_demand": {k: int((dkind == k).sum()) for k in
                                                           ("isolated_dwelling", "hamlet", "village", "town", "city",
                                                            "suburb", "neighbourhood", "quarter", "residential")}}
    for key in sorted({"A", chosen_key}):
        report["rural_demand_variants"][key] = {}
        for policy in ("all", "no_isolated", "villages_up"):
            st, dd, dem_p, mask, _, _ = run_plan(scen[key], policy)
            sm, _ = summarise(scen[key], st, dd, dem_p, frac_fn, args.dense_frac, args.peri_frac)
            report["rural_demand_variants"][key][policy] = {
                "demand_points_kept": int(mask.sum()), "starts": sm["starts"], "starts_by_zone": sm["starts_by_zone"],
                "compute_hours_3_workers": sm["cost_estimate"]["compute_hours_3_workers"],
                "size_mb": sm["cost_estimate"]["size_mb"], "coverage_of_kept_demand": sm["coverage_by_demand_zone"]["all"],
                "skipped_demand_distance_to_nearest_start": skipped_stats(st, mask)}

    starts, d, zones = results[chosen_key]
    # --- fichier de départs (noms uniques ; remplissage = nom du lieu le plus proche + direction)
    named = [(*proj.xy(p[0], p[1]), p[3]) for p in places if p[3]]
    nx = np.array([n[0] for n in named]); ny = np.array([n[1] for n in named])
    towns = [(*proj.xy(p[0], p[1]), p[3]) for p in places if p[3] and p[2] in ("city", "town", "village")]   # « Près de » : commune, pas un quartier
    tx = np.array([n[0] for n in towns]); ty = np.array([n[1] for n in towns])
    out, used = [], {}
    for i in range(len(starts["x"])):
        lon, lat = proj.lonlat(starts["x"][i], starts["y"][i])
        name = starts["name"][i]
        if name is None:
            if len(nx):
                j = int(np.argmin(np.hypot(nx - starts["x"][i], ny - starts["y"][i])))
                ddx, ddy = starts["x"][i] - nx[j], starts["y"][i] - ny[j]
                compass = ["est", "nord-est", "nord", "nord-ouest", "ouest", "sud-ouest", "sud", "sud-est"]
                name = f"{named[j][2]} ({compass[int(round(math.degrees(math.atan2(ddy, ddx)) / 45.0)) % 8]})"
            else:
                name = "Départ"
        used[name] = used.get(name, 0) + 1
        if used[name] > 1:
            name = f"{name} {used[name]}"
        kind = "station" if starts["kind"][i] == "station" else "fill" if starts["kind"][i] == "fill" else "place"
        entry = {"name": name, "lon": round(float(lon), 5), "lat": round(float(lat), 5), "kind": kind, "zone": zones[i]}
        if kind == "fill" and len(tx):                   # nom d'affichage : « Près de <ville ou village le plus proche> »
            jt = int(np.argmin(np.hypot(tx - starts["x"][i], ty - starts["y"][i])))
            entry["display_name"] = f"Près de {towns[jt][2]}"
        out.append(entry)
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    report["chosen_scenario"] = chosen_key
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    # --- échantillon de lieux habités et de leur départ le plus proche (pour mesurer le détour)
    if args.demand_out:
        idx, dd = nearest_index(dem["x"], dem["y"], starts["x"], starts["y"])
        rnd = random.Random(7)
        sample = []
        for z in ZONES:
            ids = [i for i in range(len(dem["x"])) if dem["zone"][i] == z and dd[i] >= 0.6]
            rnd.shuffle(ids)
            for i in (ids if args.sample_per_zone == 0 else ids[: args.sample_per_zone]):
                lon, lat = proj.lonlat(dem["x"][i], dem["y"][i])
                sample.append({"lon": round(float(lon), 5), "lat": round(float(lat), 5), "zone": z,
                               "kind": str(dem["kind"][i]), "start": int(idx[i]), "crow_km": round(float(dd[i]), 3)})
        Path(args.demand_out).write_text(json.dumps(sample, ensure_ascii=False), encoding="utf-8")

    # --- résumé lisible
    print(f"Lieux habités (points de demande) : {report['inputs']['demand_points']} → par zone {report['demand_by_zone']}")
    print(f"\nLieux de référence (densité du sol résidentiel dans un carré de 1,5 km puis de 3,5 km ; zone retenue "
          f"avec seuils {args.dense_frac}/{args.peri_frac} sur {(2 * R + 1) * CELL_KM} km) :")
    for nm, v in ref.items():
        print(f"  {nm:<28}" + (f"{v['density_1_5km']:>5} | {v['density_3_5km']:>5} → {v['zone']}" if v else "  (introuvable)"))
    bg = report["places_over_20000"]
    print(f"\nLieux de plus de 20 000 habitants (tag population, {bg['places_with_population_tag']} lieux ont ce tag) : "
          f"{bg['places_over_20000']} au total, par zone {bg['by_zone']} ; classés rural : "
          f"{[(x['name'], x['population']) for x in bg['rural']]}")
    print(f"Sensibilité aux seuils de zone : {json.dumps(report['zone_sensitivity'], ensure_ascii=False)}")
    for key, s in report["scenarios"].items():
        print(f"\nScénario {key} : dmax {s['limits_km']} → {s['starts']} départs "
              f"{s['starts_by_zone']} ; gares (politique {args.stations}) : {s['starts_by_stations_policy']} ; villes ajoutées : {s['towns_forced_added']}")
        print(f"  {'zone':<7}{'n':>6}{'médiane':>10}{'P90':>7}{'P95':>7}{'max':>7}   (km, à vol d'oiseau)")
        for z in ("all", *ZONES):
            c = s["coverage_by_demand_zone"][z]
            if c.get("n"):
                print(f"  {z:<7}{c['n']:>6}{c['median_km']:>10}{c['p90_km']:>7}{c['p95_km']:>7}{c['max_km']:>7}")
        ce = s["cost_estimate"]
        print(f"  calcul estimé : {ce['compute_hours_one_thread'][0]}–{ce['compute_hours_one_thread'][1]} h (1 fil), "
              f"{ce['compute_hours_3_workers'][0]}–{ce['compute_hours_3_workers'][1]} h (3 en parallèle) ; taille estimée {ce['size_mb']} Mo")
    print("\nDemande rurale : combien de départs pour couvrir, ou non, les habitations isolées et les hameaux ?")
    print(f"  lieux dans les données : {report['rural_demand_variants']['kinds_in_demand']}")
    for key, v in report["rural_demand_variants"].items():
        if key == "kinds_in_demand":
            continue
        for policy, r in v.items():
            sk = r["skipped_demand_distance_to_nearest_start"]
            print(f"  scénario {key} / {policy:<12} {r['starts']:>4} départs {r['starts_by_zone']} ; calcul (3 fils) "
                  f"{r['compute_hours_3_workers'][0]}–{r['compute_hours_3_workers'][1]} h ; {r['size_mb']} Mo"
                  + (f" ; lieux non couverts à {sk['median_km']} km (médiane), {sk['max_km']} km (max)" if sk else ""))
    print(f"\n-> {len(out)} départs du scénario {chosen_key} écrits dans {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
