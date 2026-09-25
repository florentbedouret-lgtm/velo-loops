#!/usr/bin/env python3
"""
Génère un catalogue de boucles vélo PRÉ-CALCULÉES à partir d'un serveur GraphHopper local.

Pour chaque point de départ (ville/village OSM), chaque durée et chaque niveau :
  1. demande des boucles à GraphHopper (algorithm=round_trip, profils "calm" / "sport"),
  2. ajuste la distance demandée pour viser la durée cible (modèle physique interne watts -> vitesse),
  3. calcule des métriques réelles (D+, % zone bâtie, % pistes cyclables, % routes principales,
     % non goudronné, % de tronçons empruntés deux fois),
  4. note les candidats, garde 1 à 3 options différentes (équilibrée / plate / vallonnée),
  5. écrit un JSON statique par départ, lisible directement par la web app (aucun serveur en production).

Le "pitch" n'est généré qu'à partir de métriques calculées : on ne promet que ce qu'on mesure.
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import requests

# --------------------------------------------------------------------------- constantes modèle
# Valeurs par défaut du POC ; à remplacer plus tard par poids / vélo / watts réels de l'utilisateur.
TOTAL_MASS_KG = 85.0          # cycliste + vélo + équipement
CDA = 0.40                    # m² (position mains sur les cocottes)
CRR = 0.005                   # résistance au roulement, route goudronnée
DRIVETRAIN_EFF = 0.975
AIR_RHO = 1.2
G = 9.81
DESCENT_CAP_MS = 13.0         # ~47 km/h : on ne suppose pas de descentes plus rapides
REAL_WORLD_FACTOR = 0.88      # arrêts, virages, prudence : à calibrer avec les retours utilisateurs

LEVELS = {                    # puissance moyenne soutenue (W) utilisée EN INTERNE uniquement
    "facile": {"watts": 110, "label": "tranquille", "profiles": ["calm"]},
    "modere": {"watts": 150, "label": "modéré", "profiles": ["calm", "sport"]},
    "soutenu": {"watts": 190, "label": "sportif", "profiles": ["sport"]},
}

# Candidats testés pour chaque combinaison : (seed, cap souhaité ou None). Les 8 caps permettent de trouver
# des boucles qui sortent plus vite de la zone urbaine dense.
CANDIDATES = [(1, None), (2, None), (3, 0), (4, 90), (5, 180), (6, 270), (7, 45), (8, 135), (9, 225), (10, 315)]

SIGNAL_DELAY_S = 10.0         # attente moyenne attendue par feu tricolore franchi (arrêt 1 fois sur 2 + relance) : à calibrer
SIGNAL_RADIUS_M = 15.0        # un feu OSM à moins de 15 m du tracé est considéré comme franchi
SIGNAL_CLUSTER_M = 60.0       # feux de sens différents d'un même carrefour : comptés une seule fois

PROFILE_STEP_M = 100.0        # pas de ré-échantillonnage du profil altimétrique
SMOOTH_WINDOW = 5             # moyenne mobile (5 pas = 500 m) : les données SRTM sont bruitées, surtout en ville
ASCENT_THRESHOLD_M = 3.0      # une variation < 3 m n'est pas comptée comme montée ou descente (comme un GPS/baromètre)
TIME_TOLERANCE = 0.15         # écart accepté sur la durée cible
MAX_OVERLAP = 0.25            # part max de tronçons empruntés 2 fois
MAX_UNPAVED = 0.12
MAX_UTURNS = 2                # demi-tours acceptés (impasses parcourues aller-retour)
WEIGHTS = {"calm": 0.22, "lights": 0.22, "axes": 0.14, "infra": 0.12, "flow": 0.18, "scenery": 0.12}
FLOW_OVERLAP_FACTOR = 1.0     # pénalité des tronçons répétés (2.0 jusqu'à la v4 ; D21 : diagnostic nature_check v5)
LIGHTS_PER_KM_ZERO_SCORE = 2.5  # à 2,5 feux/km ou plus, le sous-score "feux" tombe à 0
SIGNALS = None                  # SignalIndex des feux tricolores, chargé dans main()
STOPS = None                    # SignalIndex des panneaux stop
LANDSCAPE = None                # LandscapeIndex (forêts, eau, parcs, points de vue), chargé dans main()
# Proxy "paysage" : distances (en degrés, ~100 m = 0,0009 à cette latitude) et pondération PROVISOIRE
SCENERY_FOREST_DEG, SCENERY_WATER_DEG, SCENERY_PROTECTED_DEG, SCENERY_VIEW_DEG = 0.0004, 0.001, 0.0002, 0.003
UNPAVED = {"unpaved", "compacted", "fine_gravel", "gravel", "ground", "dirt", "grass", "sand"}
COBBLES = {"cobblestone", "sett", "paving_stones"}
ASPHALT = {"asphalt", "concrete", "paved"}
DETAILS = ["road_class", "surface", "urban_density", "bike_network"]


# --------------------------------------------------------------------------- géométrie
def haversine(lon1, lat1, lon2, lat2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371008.8 * math.asin(math.sqrt(a))


def bearing(lon1, lat1, lon2, lat2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def simplify(coords, tolerance_m=10.0):
    """Douglas-Peucker itératif (approximation plane locale)."""
    if len(coords) < 3:
        return coords
    lat0 = math.radians(coords[0][1])
    kx, ky = 111320.0 * math.cos(lat0), 110540.0
    pts = [(c[0] * kx, c[1] * ky) for c in coords]
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        ax, ay = pts[a]
        bx, by = pts[b]
        dx, dy = bx - ax, by - ay
        norm = math.hypot(dx, dy)
        dmax, idx = 0.0, -1
        for i in range(a + 1, b):
            px, py = pts[i]
            if norm == 0:
                d = math.hypot(px - ax, py - ay)
            else:
                d = abs(dy * (px - ax) - dx * (py - ay)) / norm
            if d > dmax:
                dmax, idx = d, i
        if dmax > tolerance_m and idx != -1:
            keep[idx] = True
            stack.append((a, idx))
            stack.append((idx, b))
    return [c for c, k in zip(coords, keep) if k]


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "x"


# --------------------------------------------------------------------------- feux tricolores (OSM)
class SignalIndex:
    """Index spatial des feux tricolores OSM (highway=traffic_signals, crossing=traffic_signals)."""

    CELL = 0.0004  # ~ 30-45 m

    def __init__(self, points):
        self.points = points
        self.grid: dict = {}
        for idx, (lon, lat) in enumerate(points):
            self.grid.setdefault((int(lon // self.CELL), int(lat // self.CELL)), []).append(idx)

    def count_along(self, coords, cum) -> int:
        """Nombre de carrefours à feux franchis par le tracé (feux voisins fusionnés)."""
        positions = []
        for i in range(1, len(coords)):
            (lo0, la0), (lo1, la1) = coords[i - 1][:2], coords[i][:2]
            kx, ky = 111320.0 * math.cos(math.radians(la0)), 110540.0
            dx, dy = (lo1 - lo0) * kx, (la1 - la0) * ky
            seg2 = dx * dx + dy * dy
            pad = SIGNAL_RADIUS_M / 90000.0
            for cx in range(int((min(lo0, lo1) - pad) // self.CELL), int((max(lo0, lo1) + pad) // self.CELL) + 1):
                for cy in range(int((min(la0, la1) - pad) // self.CELL), int((max(la0, la1) + pad) // self.CELL) + 1):
                    for idx in self.grid.get((cx, cy), ()):
                        px = (self.points[idx][0] - lo0) * kx
                        py = (self.points[idx][1] - la0) * ky
                        t = 0.0 if seg2 == 0 else max(0.0, min(1.0, (px * dx + py * dy) / seg2))
                        if math.hypot(px - t * dx, py - t * dy) <= SIGNAL_RADIUS_M:
                            pos = cum[i - 1] + t * (cum[i] - cum[i - 1])
                            positions.append(pos)
        positions.sort()
        count, last = 0, None
        for pos in positions:
            if last is None or pos - last > SIGNAL_CLUSTER_M:
                count += 1
            last = pos
        return count


def load_points(pbf: Path, workdir: Path, filters: list, name: str):
    """Extrait des nœuds OSM (feux, stops…) du fichier .pbf avec osmium (installé sur le runner GitHub)."""
    import shutil
    import subprocess
    if not pbf.exists() or not shutil.which("osmium"):
        print(f"! {name} non chargés (fichier {pbf} ou osmium introuvable)", file=sys.stderr)
        return None
    filt, out = workdir / f"{name}.osm.pbf", workdir / f"{name}.geojson"
    try:
        subprocess.run(["osmium", "tags-filter", str(pbf), *filters, "-o", str(filt), "--overwrite"],
                       check=True, capture_output=True)
        subprocess.run(["osmium", "export", str(filt), "-f", "geojson", "-o", str(out), "--overwrite"],
                       check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        print(f"! extraction de {name} échouée : {e.stderr.decode()[:200]}", file=sys.stderr)
        return None
    pts = []
    for f in json.loads(out.read_text(encoding="utf-8")).get("features", []):
        g = f.get("geometry", {})
        if g.get("type") == "Point":
            pts.append((g["coordinates"][0], g["coordinates"][1]))
    return SignalIndex(pts) if pts else None


def load_signals(pbf: Path, workdir: Path):
    return load_points(pbf, workdir, ["n/highway=traffic_signals", "n/crossing=traffic_signals"], "signals")


# --------------------------------------------------------------------------- proxy "paysage" (OSM)
class LandscapeIndex:
    """Part du tracé au bord de forêts, d'eau ou de parcs, et points de vue proches (shapely + OSM)."""

    def __init__(self, forests, waters, protected, viewpoints, builtup=()):
        import numpy as np  # noqa: F401
        from shapely.strtree import STRtree
        builtup = list(builtup)
        self.trees = {k: (STRtree(v) if v else None)
                      for k, v in (("forest", forests), ("water", waters), ("protected", protected),
                                   ("view", viewpoints), ("builtup", builtup))}
        self.counts = {"forest": len(forests), "water": len(waters), "protected": len(protected),
                       "view": len(viewpoints), "builtup": len(builtup)}

    def measure(self, coords, cum) -> dict:
        import numpy as np
        import shapely
        xy = sample_points(coords, cum, 100.0)
        pts = shapely.points(np.array(xy))

        def share(key, dist):
            tree = self.trees[key]
            if tree is None:
                return 0.0
            hit = tree.query(pts, predicate="dwithin", distance=dist)[0]
            return len(set(hit.tolist())) / len(xy)

        views = 0
        if self.trees["view"] is not None:
            views = len(set(self.trees["view"].query(pts, predicate="dwithin", distance=SCENERY_VIEW_DEG)[1].tolist()))
        forest = share("forest", SCENERY_FOREST_DEG)
        water = share("water", SCENERY_WATER_DEG)
        protected = share("protected", SCENERY_PROTECTED_DEG)
        index = min(1.0, 0.8 * forest + 1.5 * water + 0.6 * protected + 0.05 * min(views, 4))
        return {"forest": forest, "water": water, "protected": protected, "viewpoints": views,
                "score": round(100 * index), "landcover": self.landcover(pts)}

    def landcover(self, pts) -> dict | None:
        """Répartition du tracé (points tous les 100 m), UNE catégorie par point, par priorité : ville (dans une zone
        bâtie OSM : landuse residential/commercial/industrial/retail) > eau (< ~100 m) > forêt (dans ou au bord)
        > campagne. Les 4 parts font 100 % : barre de terrain de la charte (O-9.4). Sans zones bâties chargées : None."""
        import numpy as np
        if self.trees["builtup"] is None:
            return None
        n = len(pts)
        cls = np.full(n, 3)                                   # 3 = campagne
        for code, key, pred, dist in ((2, "forest", "dwithin", SCENERY_FOREST_DEG),
                                      (1, "water", "dwithin", SCENERY_WATER_DEG),
                                      (0, "builtup", "intersects", None)):   # du moins au plus prioritaire
            tree = self.trees[key]
            if tree is None:
                continue
            hit = (tree.query(pts, predicate=pred, distance=dist) if dist else tree.query(pts, predicate=pred))[0]
            cls[np.unique(hit)] = code
        counts = np.bincount(cls, minlength=4) / max(n, 1)
        return {"city": round(float(counts[0]), 3), "water": round(float(counts[1]), 3),
                "forest": round(float(counts[2]), 3), "countryside": round(float(counts[3]), 3)}


def load_landscape(pbf: Path, workdir: Path):
    """Extrait forêts, eau, parcs, points de vue du .pbf (osmium) et construit l'index spatial (shapely)."""
    import shutil
    import subprocess
    try:
        from shapely.geometry import shape
    except ImportError:
        print("! paysage non chargé : shapely absent (ajoute-le à scripts/requirements.txt)", file=sys.stderr)
        return None
    if not pbf.exists() or not shutil.which("osmium"):
        print(f"! paysage non chargé (fichier {pbf} ou osmium introuvable)", file=sys.stderr)
        return None
    filt, out = workdir / "landscape.osm.pbf", workdir / "landscape.geojsonseq"
    filters = ["nwr/landuse=forest,residential,commercial,industrial,retail", "nwr/natural=wood",
               "nwr/natural=water", "nwr/waterway=river",
               "w/natural=coastline", "nwr/natural=beach", "nwr/leisure=park", "nwr/leisure=nature_reserve",
               "nwr/boundary=protected_area", "nwr/boundary=national_park", "n/tourism=viewpoint"]
    try:
        subprocess.run(["osmium", "tags-filter", str(pbf), *filters, "-o", str(filt), "--overwrite"],
                       check=True, capture_output=True)
        subprocess.run(["osmium", "export", str(filt), "-f", "geojsonseq", "-o", str(out), "--overwrite"],
                       check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        print(f"! extraction du paysage échouée : {e.stderr.decode()[:200]}", file=sys.stderr)
        return None
    forests, waters, protected, views, builtup = [], [], [], [], []
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
            props = feat.get("properties", {})
            if geom.is_empty:
                continue
            if geom.geom_type == "Point":
                if props.get("tourism") == "viewpoint":
                    views.append(geom)
                continue
            if geom.geom_type in ("Polygon", "MultiPolygon") and geom.area < 2e-7 and not props.get("boundary"):
                continue                                   # ignore les surfaces < ~1 700 m²
            geom = geom.simplify(0.00005, preserve_topology=True)   # ~5 m : allège l'index
            if props.get("natural") in ("water", "coastline", "beach") or props.get("waterway") == "river":
                waters.append(geom)
            elif props.get("landuse") == "forest" or props.get("natural") == "wood":
                forests.append(geom)
            elif props.get("landuse") in ("residential", "commercial", "industrial", "retail"):
                builtup.append(geom)                       # zones bâties : « ville » de la répartition du terrain
            elif props.get("leisure") in ("park", "nature_reserve") or props.get("boundary") in (
                    "protected_area", "national_park"):
                protected.append(geom)
    return LandscapeIndex(forests, waters, protected, views, builtup)


# --------------------------------------------------------------------------- modèle physique
def speed_from_power(power_w: float, grade: float, mass: float = TOTAL_MASS_KG) -> float:
    """Vitesse (m/s) tenue à puissance constante sur une pente donnée (approximation petits angles)."""
    target = power_w * DRIVETRAIN_EFF

    def need(v):
        return 0.5 * AIR_RHO * CDA * v ** 3 + mass * G * (CRR + grade) * v

    lo, hi = 0.3, 20.0
    if need(hi) < target:
        return min(hi, DESCENT_CAP_MS)
    for _ in range(40):
        mid = (lo + hi) / 2
        if need(mid) < target:
            lo = mid
        else:
            hi = mid
    return min(lo, DESCENT_CAP_MS)


def elevation_profile(coords, cum, step=PROFILE_STEP_M, window=SMOOTH_WINDOW):
    """Altitude ré-échantillonnée tous les `step` m puis lissée (moyenne mobile centrée).
    Retourne (pas réel en m, liste d'altitudes lissées)."""
    total = cum[-1]
    n = max(1, int(round(total / step)))
    ds = total / n
    zs = [c[2] if len(c) > 2 else 0.0 for c in coords]
    raw = []
    for k in range(n + 1):
        sdist = k * ds
        i = bisect.bisect_right(cum, sdist)
        if i <= 0:
            raw.append(zs[0])
        elif i >= len(cum):
            raw.append(zs[-1])
        else:
            s0, s1 = cum[i - 1], cum[i]
            t = 0.0 if s1 == s0 else (sdist - s0) / (s1 - s0)
            raw.append(zs[i - 1] + t * (zs[i] - zs[i - 1]))
    half = window // 2
    smooth = [sum(raw[max(0, i - half): i + half + 1]) / len(raw[max(0, i - half): i + half + 1])
              for i in range(len(raw))]
    return ds, smooth


def gain_loss(values, threshold=ASCENT_THRESHOLD_M):
    """Dénivelé positif / négatif avec seuil d'hystérésis : ignore les oscillations < threshold (bruit)."""
    gain = loss = 0.0
    low = high = values[0]
    direction = 0
    for v in values[1:]:
        if direction == 0:
            if v - low >= threshold:
                direction, gain, high = 1, gain + (v - low), v
            elif high - v >= threshold:
                direction, loss, low = -1, loss + (high - v), v
            else:
                low, high = min(low, v), max(high, v)
        elif direction == 1:
            if v > high:
                gain, high = gain + (v - high), v
            elif high - v >= threshold:
                direction, loss, low = -1, loss + (high - v), v
        else:
            if v < low:
                loss, low = loss + (low - v), v
            elif v - low >= threshold:
                direction, gain, high = 1, gain + (v - low), v
    return gain, loss


def sample_points(coords, cum, step):
    """Points (lon, lat) espacés de `step` m le long du tracé."""
    total = cum[-1]
    n = max(1, int(total // step))
    out, j = [], 1
    for k in range(n + 1):
        d = min(total, k * step)
        while j < len(cum) - 1 and cum[j] < d:
            j += 1
        s0, s1 = cum[j - 1], cum[j]
        t = 0.0 if s1 == s0 else (d - s0) / (s1 - s0)
        out.append((coords[j - 1][0] + t * (coords[j][0] - coords[j - 1][0]),
                    coords[j - 1][1] + t * (coords[j][1] - coords[j - 1][1])))
    return out


def detect_climb_runs(prof, threshold=5.0):
    """Suites monotones (montée ou descente) dont l'amplitude dépasse `threshold` (bruit sous ce seuil, comme un
    altimètre GPS). Retourne la liste des montées : [(indice de départ, indice du sommet)]. Une seule
    implémentation, utilisée à la fois pour les "montées" affichées (slope_stats, filtrées >= 20 m, 5 au plus)
    et pour la pente moyenne des montées nettes (terrain.avg_climb_grade_pct, TOUTES les montées >= 5 m)."""
    runs, direction, low_i, high_i, start_i = [], 0, 0, 0, 0
    for i in range(1, len(prof)):
        v = prof[i]
        if direction == 0:
            if v - prof[low_i] >= threshold:
                direction, start_i, high_i = 1, low_i, i
            elif prof[high_i] - v >= threshold:
                direction, low_i = -1, i
            else:
                low_i = i if v <= prof[low_i] else low_i
                high_i = i if v >= prof[high_i] else high_i
        elif direction == 1:
            if v > prof[high_i]:
                high_i = i
            elif prof[high_i] - v >= threshold:
                runs.append((start_i, high_i))
                direction, low_i = -1, i
        else:
            if v <= prof[low_i]:
                low_i = i
            elif v - prof[low_i] >= threshold:
                direction, start_i, high_i = 1, low_i, i
    if direction == 1:
        runs.append((start_i, high_i))
    return runs


def slope_stats(ds, prof):
    """Pente max (lissée), répartition de la distance par classe de pente, montées significatives (affichage),
    et pente moyenne pondérée sur TOUTES les montées nettes (terrain.avg_climb_grade_pct — validée par le test de
    sensibilité au lissage du 22/09 : rapport médian 1,21x sans lissage/500 m sur 6 sorties réelles, le plus
    stable des indicateurs de pente testés ; affichage recommandé arrondi au multiple de 2 le plus proche, pas
    à l'entier, pour rester dans la marge de bruit)."""
    grades = [(prof[k] - prof[k - 1]) / ds for k in range(1, len(prof))]
    if not grades:
        return {"max_grade_pct": 0.0, "bands": {}, "climbs": [], "n_climbs": 0, "avg_climb_grade_pct": None}
    bands = {"descent": 0, "flat": 0, "up_3_6": 0, "up_6_9": 0, "up_9_plus": 0}
    for g in grades:
        key = ("descent" if g < -0.03 else "flat" if g < 0.03 else "up_3_6" if g < 0.06
               else "up_6_9" if g < 0.09 else "up_9_plus")
        bands[key] += 1
    n = len(grades)
    runs = detect_climb_runs(prof, threshold=5.0)               # toutes les montées nettes (>= 5 m, bruit exclu)
    all_climbs = [(prof[b] - prof[a], (b - a) * ds) for a, b in runs]
    climb_gain = sum(g for g, _ in all_climbs)
    climb_len = sum(le for _, le in all_climbs)
    avg_climb = round(100.0 * climb_gain / climb_len, 1) if climb_len > 0 else None
    # sous-ensemble affiché dans le pitch : seulement les montées >= 20 m, les 5 plus grosses
    climbs = [{"start_km": round(a * ds / 1000.0, 1), "length_km": round((b - a) * ds / 1000.0, 1),
              "gain_m": round(prof[b] - prof[a]), "avg_grade_pct": round(100.0 * (prof[b] - prof[a]) / ((b - a) * ds), 1)}
              for a, b in runs if prof[b] - prof[a] >= 20.0 and (b - a) * ds > 0]
    climbs.sort(key=lambda c: -c["gain_m"])
    return {"max_grade_pct": round(100.0 * max(grades), 1), "bands": {k: round(v / n, 3) for k, v in bands.items()},
            "climbs": climbs[:5], "n_climbs": len(climbs), "avg_climb_grade_pct": avg_climb}


def count_uturns(coords, min_seg=8.0, angle=150.0) -> int:
    """Demi-tours : inversions de cap >= 150° (impasses, aller-retour), en ignorant les micro-segments."""
    kept = [coords[0]]
    for c in coords[1:]:
        if haversine(kept[-1][0], kept[-1][1], c[0], c[1]) >= min_seg:
            kept.append(c)
    n, i = 0, 1
    while i < len(kept) - 1:
        b1 = bearing(kept[i - 1][0], kept[i - 1][1], kept[i][0], kept[i][1])
        b2 = bearing(kept[i][0], kept[i][1], kept[i + 1][0], kept[i + 1][1])
        if abs((b2 - b1 + 180) % 360 - 180) >= angle:
            n += 1
            i += 2
        else:
            i += 1
    return n


def estimate_time_s(ds: float, profile, watts: float, city_share: float, resid_share: float,
                    n_signals: int | None = None) -> float:
    """Temps estimé : vitesse déduite de la puissance sur le profil lissé (un point tous les ~100 m),
    + attente moyenne par feu tricolore (SIGNAL_DELAY_S). Sans données de feux, on garde une majoration
    forfaitaire plus forte en ville."""
    t = 0.0
    for k in range(1, len(profile)):
        grade = max(-0.20, min(0.20, (profile[k] - profile[k - 1]) / ds))
        t += ds / (speed_from_power(watts, grade) * REAL_WORLD_FACTOR)
    if n_signals is None:
        return t * (1.0 + 0.15 * city_share + 0.06 * resid_share)
    return t * (1.0 + 0.05 * city_share + 0.03 * resid_share) + n_signals * SIGNAL_DELAY_S


# --------------------------------------------------------------------------- client GraphHopper
class GraphHopper:
    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")
        self.http = requests.Session()
        self.last_error = ""

    def info(self) -> dict:
        try:
            return self.http.get(f"{self.base}/info", timeout=20).json()
        except Exception:
            return {}

    def nearest(self, lat, lon):
        r = self.http.get(f"{self.base}/nearest", params={"point": f"{lat},{lon}"}, timeout=20)
        if r.status_code != 200:
            return None
        d = r.json()
        return d["coordinates"][0], d["coordinates"][1], d.get("distance", 0.0)

    def round_trip(self, lon, lat, profile, dist_m, seed, heading=None):
        body = {
            "points": [[lon, lat]],
            "profile": profile,
            "algorithm": "round_trip",
            "round_trip.distance": int(dist_m),
            "round_trip.seed": seed,
            "ch.disable": True,
            "points_encoded": False,
            "elevation": True,
            "instructions": False,
            "details": DETAILS,
        }
        if heading is not None:
            body["heading"] = [heading]
        try:
            r = self.http.post(f"{self.base}/route", json=body, timeout=120)
        except requests.RequestException as e:
            self.last_error = f"requête échouée : {e}"
            return None
        if r.status_code != 200:
            self.last_error = f"HTTP {r.status_code} : {r.text[:200]}"
            return None
        self.last_error = ""
        paths = r.json().get("paths") or []
        return paths[0] if paths else None


# --------------------------------------------------------------------------- analyse d'une boucle
@dataclass
class Loop:
    profile: str
    level: str
    duration_h: float
    seed: int
    heading: int | None
    coords: list
    distance_m: float
    ascend_m: float
    descend_m: float
    ascend_gh_m: float
    time_s: float
    shares: dict
    overlap: float
    score: float = 0.0
    cells: set = field(default_factory=set)
    wind_bins: dict = field(default_factory=dict)
    signals: int | None = None
    exit_dense_m: float | None = None
    stops: int | None = None
    surface_mix: dict = field(default_factory=dict)
    terrain: dict = field(default_factory=dict)
    u_turns: int = 0
    longest_repeat_m: float = 0.0
    scenery: dict | None = None

    @property
    def dplus_per_km(self) -> float:
        return self.ascend_m / max(self.distance_m / 1000.0, 0.1)


def meters_by_value(detail, cum) -> dict:
    out: dict[str, float] = {}
    for a, b, val in detail or []:
        a = min(a, len(cum) - 1)
        b = min(b, len(cum) - 1)
        out[str(val).lower()] = out.get(str(val).lower(), 0.0) + (cum[b] - cum[a])
    return out


def joint_meters(det_a, det_b, cum) -> dict:
    """Mètres par couple de valeurs (détail a, détail b), ex. (classe de route, densité urbaine)."""
    n = len(cum) - 1
    va, vb = [None] * n, [None] * n
    for det, arr in ((det_a, va), (det_b, vb)):
        for a, b, val in det or []:
            for i in range(max(0, a), min(b, n)):
                arr[i] = str(val).lower()
    out: dict = {}
    for i in range(n):
        out[(va[i], vb[i])] = out.get((va[i], vb[i]), 0.0) + (cum[i + 1] - cum[i])
    return out


def analyse(path: dict, level: str, profile: str, duration_h: float, seed: int, heading) -> Loop | None:
    coords = path["points"]["coordinates"]
    if len(coords) < 10:
        return None
    cum = [0.0]
    for i in range(1, len(coords)):
        cum.append(cum[-1] + haversine(coords[i - 1][0], coords[i - 1][1], coords[i][0], coords[i][1]))
    total = cum[-1]
    if total < 1000:
        return None
    det = path.get("details", {})
    frac = lambda d, keys: sum(d.get(k, 0.0) for k in keys) / total  # noqa: E731
    urban = meters_by_value(det.get("urban_density"), cum)
    rclass = meters_by_value(det.get("road_class"), cum)
    surf = meters_by_value(det.get("surface"), cum)
    net = meters_by_value(det.get("bike_network"), cum)

    # tronçons empruntés deux fois (mesure de la "qualité de boucle")
    seen: dict = {}
    keys = []
    for i in range(1, len(coords)):
        a = (round(coords[i - 1][0], 5), round(coords[i - 1][1], 5))
        b = (round(coords[i][0], 5), round(coords[i][1], 5))
        k = (a, b) if a <= b else (b, a)
        keys.append(k)
        seen[k] = seen.get(k, 0) + 1
    repeated = sum(cum[i + 1] - cum[i] for i, k in enumerate(keys) if seen[k] > 1)
    longest_repeat, run = 0.0, 0.0
    for i, k in enumerate(keys):                     # plus long tronçon consécutif emprunté deux fois
        run = run + (cum[i + 1] - cum[i]) if seen[k] > 1 else 0.0
        longest_repeat = max(longest_repeat, run)

    # répartition des caps (8 secteurs) sur la 1re / 2e moitié : score de vent calculé côté navigateur
    bins = {"first": [0.0] * 8, "second": [0.0] * 8}
    for i in range(1, len(coords)):
        seg = cum[i] - cum[i - 1]
        b = bearing(coords[i - 1][0], coords[i - 1][1], coords[i][0], coords[i][1])
        half = "first" if cum[i] <= total / 2 else "second"
        bins[half][int(round(b / 45.0)) % 8] += seg / 1000.0

    city, resid, rural = frac(urban, ["city"]), frac(urban, ["residential"]), frac(urban, ["rural"])
    shares = {
        "urban": {"rural": rural, "residential": resid, "city": city},
        "dedicated_cycleway": frac(rclass, ["cycleway"]),
        "bike_network": 1.0 - frac(net, ["missing"]) if net else 0.0,
        "main_roads": frac(rclass, ["primary", "trunk", "secondary"]),
        "unpaved": frac(surf, list(UNPAVED)),
    }
    # grandes routes par densité urbaine (diagnostic O-12 ; n'entre pas dans score() ni dans les fichiers publiés)
    joint = joint_meters(det.get("road_class"), det.get("urban_density"), cum)
    shares["main_roads_by_urban"] = {
        u: sum(m for (rc, ud), m in joint.items() if rc in ("primary", "trunk", "secondary") and ud == u) / total
        for u in ("city", "residential", "rural")}
    watts = LEVELS[level]["watts"]
    ds, prof = elevation_profile(coords, cum)
    ascend, descend = gain_loss(prof)
    n_signals = SIGNALS.count_along(coords, cum) if SIGNALS is not None else None
    n_stops = STOPS.count_along(coords, cum) if STOPS is not None else None
    surface_mix = {
        "asphalt": frac(surf, list(ASPHALT)), "cobbles": frac(surf, list(COBBLES)),
        "unpaved": frac(surf, list(UNPAVED)),
    }
    surface_mix["unknown"] = max(0.0, 1.0 - sum(surface_mix.values()))
    terrain = slope_stats(ds, prof)
    u_turns = count_uturns(coords)
    scenery = LANDSCAPE.measure(coords, cum) if LANDSCAPE is not None else None
    exit_dense = None
    ud = det.get("urban_density") or []
    if ud and str(ud[0][2]).lower() == "city":
        exit_dense = total
        for a, b, val in ud:
            if str(val).lower() != "city":
                exit_dense = cum[min(a, len(cum) - 1)]
                break
    else:
        exit_dense = 0.0
    loop = Loop(
        profile=profile, level=level, duration_h=duration_h, seed=seed, heading=heading,
        coords=coords, distance_m=total,
        ascend_m=ascend, descend_m=descend, ascend_gh_m=float(path.get("ascend", 0.0)),
        time_s=estimate_time_s(ds, prof, watts, city, resid, n_signals),
        shares=shares, overlap=repeated / total, signals=n_signals, exit_dense_m=exit_dense,
        stops=n_stops, surface_mix=surface_mix, terrain=terrain, u_turns=u_turns, longest_repeat_m=longest_repeat,
        scenery=scenery,
    )
    loop.cells = {(round(c[0] / 0.006), round(c[1] / 0.005)) for c in coords}   # cellules ~500 m
    loop.wind_bins = {k: [round(x, 2) for x in v] for k, v in bins.items()}
    loop.score = score(loop)
    return loop


def score_parts(l: Loop) -> tuple[dict, dict]:
    """Sous-scores (0 à 1) et poids utilisés par score() : exposés pour les diagnostics (nature_check.py)."""
    s = l.shares
    parts = {
        "calm": 1.0 - (s["urban"]["city"] + 0.4 * s["urban"]["residential"]),
        "axes": 1.0 - min(1.0, 2.0 * s["main_roads"]),
        "infra": min(1.0, 2.0 * s["dedicated_cycleway"]),
        "flow": 1.0 - min(1.0, FLOW_OVERLAP_FACTOR * l.overlap + 0.15 * l.u_turns),
    }
    weights = dict(WEIGHTS)
    if l.signals is not None:
        per_km = l.signals / max(l.distance_m / 1000.0, 0.1)
        parts["lights"] = 1.0 - min(1.0, per_km / LIGHTS_PER_KM_ZERO_SCORE)
    else:
        weights.pop("lights")
    if l.scenery is not None:
        parts["scenery"] = l.scenery["score"] / 100.0
    else:
        weights.pop("scenery")
    return parts, weights


def score(l: Loop) -> float:
    parts, weights = score_parts(l)
    total = sum(weights[k] * parts[k] for k in weights) / sum(weights.values())
    total -= min(0.3, max(0.0, l.shares["unpaved"] - 0.03) * 2.0)
    return round(100 * max(0.0, total), 1)


# --------------------------------------------------------------------------- génération
def fit_and_sample(gh: GraphHopper, start, level: str, profile: str, duration_h: float, candidates, log):
    """Retourne (candidats valides, compteur des raisons de rejet)."""
    lon, lat = start["lon"], start["lat"]
    watts = LEVELS[level]["watts"]
    target_s = duration_h * 3600.0
    rejects = {"pas de boucle": 0, "durée": 0, "tronçons répétés": 0, "demi-tours": 0, "non goudronné": 0}
    flat_ms = speed_from_power(watts, 0.0) * REAL_WORLD_FACTOR
    dist = 0.8 * flat_ms * target_s            # GraphHopper dépasse souvent la distance demandée

    # 1) trouver un premier candidat qui fonctionne (GraphHopper échoue parfois : départ près de la côte,
    #    du bord de la carte, distance trop grande…), puis ajuster la distance pour viser la durée
    fit_cand = None
    loop = None
    for factor in (1.0, 0.7, 0.5):
        for seed, heading in candidates:
            path = gh.round_trip(lon, lat, profile, dist * factor, seed, heading)
            loop = analyse(path, level, profile, duration_h, seed, heading) if path else None
            if loop is not None:
                fit_cand, dist = (seed, heading), dist * factor
                break
        if fit_cand:
            break
    if fit_cand is None:
        rejects["pas de boucle"] += 1
        log("    GraphHopper n'a renvoyé aucune boucle exploitable pour le premier calcul"
            + (f" ({gh.last_error})" if gh.last_error else " (boucle trop courte ou invalide)"))
        return [], rejects
    for _ in range(4):
        ratio = loop.time_s / target_s
        if abs(ratio - 1.0) <= 0.05:
            break
        dist = min(250_000.0, max(3_000.0, dist / ratio))
        path = gh.round_trip(lon, lat, profile, dist, fit_cand[0], fit_cand[1])
        new_loop = analyse(path, level, profile, duration_h, fit_cand[0], fit_cand[1]) if path else None
        if new_loop is None:
            break
        loop = new_loop
    log(f"    paramètre de distance GraphHopper ajusté : {dist / 1000:.1f} km (la boucle réelle peut différer)")

    # 2) tirage des autres candidats à la distance ajustée
    pool = []
    for seed, heading in candidates:
        path = gh.round_trip(lon, lat, profile, dist, seed, heading)
        cand = analyse(path, level, profile, duration_h, seed, heading) if path else None
        if cand is None:
            rejects["pas de boucle"] += 1
            continue
        ratio = cand.time_s / target_s
        if abs(ratio - 1.0) > TIME_TOLERANCE:          # une seule retouche de la distance par candidat
            path = gh.round_trip(lon, lat, profile, min(250_000.0, max(3_000.0, dist / ratio)), seed, heading)
            cand = analyse(path, level, profile, duration_h, seed, heading) if path else None
            if cand is None or abs(cand.time_s / target_s - 1.0) > TIME_TOLERANCE:
                rejects["durée"] += 1
                continue
        if cand.overlap > MAX_OVERLAP:
            rejects["tronçons répétés"] += 1
            continue
        if cand.u_turns > MAX_UTURNS:
            rejects["demi-tours"] += 1
            continue
        if cand.shares["unpaved"] > MAX_UNPAVED:
            rejects["non goudronné"] += 1
            continue
        pool.append(cand)
    return pool, rejects


def similarity(a: Loop, b: Loop) -> float:
    inter = len(a.cells & b.cells)
    return inter / max(1, min(len(a.cells), len(b.cells)))


def pick_options(pool: list[Loop]) -> list[tuple[str, Loop]]:
    if not pool:
        return []
    pool = sorted(pool, key=lambda l: l.score, reverse=True)
    best = pool[0]
    chosen = [("equilibre", best)]
    rest = [l for l in pool[1:] if l.score >= 0.8 * best.score and similarity(l, best) < 0.6]
    if rest:
        flat = min(rest, key=lambda l: l.dplus_per_km)
        if flat.dplus_per_km <= best.dplus_per_km - 3.0:
            chosen.append(("moins_de_relief", flat))
            rest = [l for l in rest if l is not flat and similarity(l, flat) < 0.6]
        if rest:
            hilly = max(rest, key=lambda l: l.dplus_per_km)
            if hilly.dplus_per_km >= best.dplus_per_km + 3.0:
                chosen.append(("plus_de_relief", hilly))
    if len(chosen) == 1 and len(pool) > 1:      # pas de contraste de relief : on propose une variante
        for l in pool[1:]:
            if similarity(l, best) < 0.6:
                chosen.append(("variante", l))
                break
    return chosen


def relief_category(dplus_per_100km: float) -> str:
    if dplus_per_100km < 500:
        return "plat"
    if dplus_per_100km < 1000:
        return "peu vallonné"
    if dplus_per_100km < 1600:
        return "vallonné"
    return "très vallonné"


def format_duration(minutes: int) -> str:
    return f"{minutes} min" if minutes < 60 else f"{minutes // 60} h {minutes % 60:02d}"


PITCH_VERSION = 2   # incrémenté à chaque changement du TEXTE produit par pitch_from_option (sert à rewrite_pitch.py)


def pitch_from_option(o: dict) -> list[str]:
    """Phrases construites uniquement à partir de mesures déjà exportées (aucune affirmation non vérifiée).
    Prend le même dict que celui écrit dans starts/<id>.json (voir to_json) : peut être appelé à la génération
    ET, plus tard, en relecture seule sur des données déjà publiées (voir scripts/rewrite_pitch.py), sans
    GraphHopper ni recalcul. Décision UX (D17, docs/decisions.md) : aucun pourcentage de pente n'est affiché
    dans le texte — le diagnostic scripts/slope_check.py a montré que la pente sans lissage est 1,55× plus
    élevée en médiane (jusqu'à 2,85×) que lissée à 500 m, un chiffre précis serait trompeur. Les champs bruts
    terrain.max_grade_pct et climbs[].avg_grade_pct restent dans les données (catégories qualitatives et badge
    de seuil, voir index.html), seul le TEXTE du pitch ne les montre plus.
    """
    urban = o["shares"]["urban"]
    minutes = round(o["time_est_min"])
    per100 = o["ascend_m"] / max(o["distance_km"], 0.1) * 100.0
    lines = [
        f"{o['distance_km']:.0f} km, {o['ascend_m']:.0f} m de dénivelé positif, "
        f"environ {format_duration(minutes)} à un rythme « {LEVELS[o['level']]['label']} »."
    ]
    lines.append(f"Profil {relief_category(per100)} : {per100:.0f} m de D+ pour 100 km.")
    if urban["rural"] >= 0.6:
        lines.append(f"{urban['rural'] * 100:.0f} % du parcours en zone rurale.")
    if urban["city"] <= 0.05:
        lines.append("Évite les zones urbaines denses (moins de 5 % du parcours).")
    else:
        lines.append(f"{urban['city'] * 100:.0f} % du parcours en zone urbaine dense.")
    ex = o.get("exit_dense_km")
    if ex is not None and ex > 0:
        if ex >= o["distance_km"] * 0.98:
            lines.append("Reste en zone urbaine dense sur tout le parcours.")
        else:
            lines.append(f"Sort de la zone urbaine dense après {ex:.1f} km.")
    signals = o.get("traffic_lights")
    if signals is not None:
        per_km = o.get("traffic_lights_per_km") or 0.0
        if signals == 0:
            lines.append("Aucun feu tricolore recensé dans OpenStreetMap sur le parcours.")
        else:
            lines.append(f"{signals} carrefours à feux tricolores ({per_km:.1f} par km).")
    tr = o.get("terrain") or {}
    if tr.get("climbs"):
        c = tr["climbs"][0]
        # pas de pourcentage de pente affiché ici (ni la pente moyenne de la montée, ni la pente max lissée) :
        # décision UX, en attendant la mesure de dispersion du lissage. Les valeurs restent dans terrain/climbs.
        lines.append(f"Plus longue montée : {c['length_km']:.1f} km (+{c['gain_m']} m) ; "
                     f"{tr['n_climbs']} montée(s) de plus de 20 m au total.")
    elif tr:
        lines.append("Aucune montée significative (plus de 20 m d'un seul tenant).")
    if o.get("u_turns"):
        lines.append(f"{o['u_turns']} demi-tour(s) sur le parcours.")
    sc = o.get("scenery")
    if sc:
        if sc["forest"] >= 0.15:
            lines.append(f"{sc['forest'] * 100:.0f} % du parcours dans ou en bordure de forêt.")
        if sc["water"] >= 0.05:
            lines.append(f"{sc['water'] * 100:.0f} % à moins de 100 m d'un plan d'eau, d'une rivière ou de la mer.")
        if sc["protected"] >= 0.15:
            lines.append(f"{sc['protected'] * 100:.0f} % dans un parc ou un espace protégé.")
        if sc["viewpoints"] >= 1:
            lines.append(f"{sc['viewpoints']} point(s) de vue référencé(s) à moins de 300 m.")
    sm = o.get("surface")
    if sm and sm.get("unknown", 0) >= 0.3:
        lines.append(f"Surface non renseignée dans OpenStreetMap sur {sm['unknown'] * 100:.0f} % du parcours.")
    sh = o["shares"]
    if sh["main_roads"] < 0.01:
        lines.append("Aucune route principale sur le parcours.")
    elif sh["main_roads"] <= 0.10:
        lines.append(f"Peu de routes principales ({sh['main_roads'] * 100:.0f} % du parcours).")
    else:
        lines.append(f"{sh['main_roads'] * 100:.0f} % sur des routes principales : à parcourir avec vigilance.")
    if sh["dedicated_cycleway"] >= 0.10:
        lines.append(f"{sh['dedicated_cycleway'] * 100:.0f} % sur pistes cyclables ou voies vertes.")
    if sh["unpaved"] > 0.05:
        lines.append(f"{sh['unpaved'] * 100:.0f} % sur revêtement non goudronné.")
    if o.get("overlap", 0) > 0.10:
        lines.append(f"{o['overlap'] * 100:.0f} % de tronçons empruntés deux fois.")
    return lines


def pitch(l: Loop, label: str) -> list[str]:
    """Construit le dict exporté attendu par pitch_from_option() à partir du Loop en cours de calcul, pour
    n'avoir qu'une seule version du texte (voir pitch_from_option)."""
    o = {
        "distance_km": l.distance_m / 1000.0, "ascend_m": l.ascend_m, "level": l.level,
        "time_est_min": l.time_s / 60.0, "shares": l.shares,
        "exit_dense_km": None if l.exit_dense_m is None else l.exit_dense_m / 1000.0,
        "traffic_lights": l.signals,
        "traffic_lights_per_km": (None if l.signals is None else l.signals / max(l.distance_m / 1000.0, 0.1)),
        "terrain": l.terrain, "u_turns": l.u_turns, "scenery": l.scenery, "surface": l.surface_mix,
        "overlap": l.overlap,
    }
    return pitch_from_option(o)


def to_json(l: Loop, label: str, start_id: str, idx: int) -> dict:
    simplified = simplify(l.coords, 10.0)
    return {
        "id": f"{start_id}-{l.duration_h:g}h-{l.level}-{l.profile}-{idx}",
        "route_key": route_key(simplified),
        "label": label,
        "profile": l.profile,
        "level": l.level,
        "duration_target_min": round(l.duration_h * 60),
        "time_est_min": round(l.time_s / 60.0),
        "distance_km": round(l.distance_m / 1000.0, 1),
        "ascend_m": round(l.ascend_m),
        "descend_m": round(l.descend_m),
        "ascend_graphhopper_raw_m": round(l.ascend_gh_m),
        "shares": {
            "urban": {k: round(v, 3) for k, v in l.shares["urban"].items()},
            "dedicated_cycleway": round(l.shares["dedicated_cycleway"], 3),
            "bike_network": round(l.shares["bike_network"], 3),
            "main_roads": round(l.shares["main_roads"], 3),
            "unpaved": round(l.shares["unpaved"], 3),
        },
        "overlap": round(l.overlap, 3),
        "bike": "route",
        "traffic_lights": l.signals,
        "traffic_lights_per_km": (None if l.signals is None else round(l.signals / max(l.distance_m / 1000.0, 0.1), 2)),
        "stop_signs": l.stops,
        "stop_signs_per_km": (None if l.stops is None else round(l.stops / max(l.distance_m / 1000.0, 0.1), 2)),
        "surface": {k: round(v, 3) for k, v in l.surface_mix.items()},
        "terrain": {"max_grade_pct": l.terrain.get("max_grade_pct"), "slope_bands": l.terrain.get("bands"),
                    "n_climbs": l.terrain.get("n_climbs"), "climbs": l.terrain.get("climbs"),
                    "avg_climb_grade_pct": l.terrain.get("avg_climb_grade_pct")},
        "u_turns": l.u_turns,
        "longest_repeat_km": round(l.longest_repeat_m / 1000.0, 2),
        "scenery": (None if l.scenery is None else {
            "forest": round(l.scenery["forest"], 3), "water": round(l.scenery["water"], 3),
            "protected": round(l.scenery["protected"], 3), "viewpoints": l.scenery["viewpoints"],
            "score": l.scenery["score"], "landcover": l.scenery.get("landcover")}),
        "exit_dense_km": None if l.exit_dense_m is None else round(l.exit_dense_m / 1000.0, 1),
        "score": l.score,
        "pitch": pitch(l, label),
        "wind_bins_km": l.wind_bins,
        "coords": [[round(c[0], 5), round(c[1], 5), round(c[2]) if len(c) > 2 else 0] for c in simplified],
    }


def load_starts(places_path: Path, names: list[str] | None, bbox, max_starts: int) -> list[dict]:
    data = json.loads(places_path.read_text(encoding="utf-8"))
    rank = {"city": 3, "town": 2, "village": 1}
    places = []
    for f in data.get("features", []):
        p = f.get("properties", {})
        g = f.get("geometry", {})
        if g.get("type") != "Point" or not p.get("name") or p.get("place") not in rank:
            continue
        lon, lat = g["coordinates"]
        if bbox and not (bbox[0] <= lon <= bbox[2] and bbox[1] <= lat <= bbox[3]):
            continue
        try:
            pop = int(str(p.get("population", "0")).replace(" ", "") or 0)
        except ValueError:
            pop = 0
        places.append({"name": p["name"], "lon": lon, "lat": lat, "rank": rank[p["place"]], "pop": pop})
    if names:
        by_name: dict[str, dict] = {}
        for pl in sorted(places, key=lambda x: (x["rank"], x["pop"]), reverse=True):
            by_name.setdefault(pl["name"], pl)
        missing = [n for n in names if n not in by_name]
        if missing:
            print(f"! départs introuvables dans les données OSM : {missing}", file=sys.stderr)
        chosen = [by_name[n] for n in names if n in by_name]
    else:
        chosen = sorted(places, key=lambda x: (x["rank"], x["pop"]), reverse=True)
    return chosen[:max_starts]


def load_starts_file(path: Path, bbox=None) -> list[dict]:
    """Départs fournis par un fichier JSON [{name, lon, lat, kind?, key?}] (plan_starts.py ou published_starts.py)."""
    out = []
    for e in json.loads(path.read_text(encoding="utf-8")):
        if bbox and not (bbox[0] <= e["lon"] <= bbox[2] and bbox[1] <= e["lat"] <= bbox[3]):
            continue
        out.append({"name": e["name"], "lon": e["lon"], "lat": e["lat"], "kind": e.get("kind", "place"),
                    "zone": e.get("zone"), "display_name": e.get("display_name"), "municipality": e.get("municipality"),
                    "key": e.get("key")})
    return out


GENERATOR_VERSION = "5"   # 5 : O-15 (landcover, tronçons répétés facteur 1), 25/09/2026


def compare_block(options: list) -> dict:
    """Meilleure option (note la plus haute) par durée et par niveau : de quoi comparer des départs voisins
    sans charger leurs fichiers. Comparable seulement à durée ET niveau identiques."""
    best: dict = {}
    for o in options:
        key = (f"{o['duration_target_min'] / 60:g}", o["level"])
        if key not in best or o["score"] > best[key]["score"]:
            best[key] = o
    out: dict = {}
    for (dur, lvl), o in best.items():
        u = o["shares"]["urban"]
        ex = o["exit_dense_km"]
        if ex is not None and ex >= o["distance_km"] * 0.98:
            ex = None                              # null = la boucle ne sort jamais de la zone dense
        out.setdefault(dur, {})[lvl] = {
            "score": o["score"], "lights_per_km": o["traffic_lights_per_km"], "exit_dense_km": ex,
            "urban_share": round(u["city"] + u["residential"], 2)}
    return out


def route_key(coords) -> str:
    """Empreinte du tracé (coordonnées simplifiées arrondies à ~10 m) : identique si la boucle est identique."""
    import hashlib
    return hashlib.sha1(";".join(f"{c[0]:.4f},{c[1]:.4f}" for c in coords).encode()).hexdigest()[:12]


def start_key(lon: float, lat: float) -> str:
    """Clé stable d'un départ (coordonnées arrondies à ~10 m) : permet de réutiliser des résultats entre générations."""
    import hashlib
    return hashlib.sha1(f"{lon:.4f},{lat:.4f}".encode()).hexdigest()[:10]


def st_key(st: dict) -> str:
    """Clé d'un départ : celle fournie par le fichier de départs (départs déjà publiés, published_starts.py : leurs
    lon/lat publiés sont recalés sur la route, la clé d'origine vient de la position du plan), sinon calculée."""
    return st.get("key") or start_key(st["lon"], st["lat"])


PARAMS_HASH = ""
REUSE = None                    # {"src", "index", "by_key", "max_age_days"} si --reuse-from est fourni
BUDGET_END = None               # instant (time.time()) au-delà duquel on ne démarre plus de nouveau départ


def params_hash(config_dir: str = "config") -> str:
    """Empreinte de tout ce qui influence les boucles : constantes du modèle + fichiers de configuration GraphHopper.
    Deux générations avec la même empreinte produisent les mêmes boucles pour un même départ (aux données OSM près)."""
    import hashlib
    consts = {"version": GENERATOR_VERSION, "levels": LEVELS, "candidates": CANDIDATES, "weights": WEIGHTS,
              "flow_overlap": FLOW_OVERLAP_FACTOR,
              "tol": TIME_TOLERANCE, "overlap": MAX_OVERLAP, "unpaved": MAX_UNPAVED, "uturns": MAX_UTURNS,
              "signal": [SIGNAL_DELAY_S, SIGNAL_RADIUS_M, SIGNAL_CLUSTER_M, LIGHTS_PER_KM_ZERO_SCORE],
              "physics": [TOTAL_MASS_KG, CDA, CRR, DRIVETRAIN_EFF, REAL_WORLD_FACTOR, DESCENT_CAP_MS],
              "profile": [PROFILE_STEP_M, SMOOTH_WINDOW, ASCENT_THRESHOLD_M]}
    h = hashlib.sha1(json.dumps(consts, sort_keys=True, default=str).encode())
    cfg = Path(config_dir)
    if cfg.exists():
        for f in sorted(cfg.rglob("*")):
            if f.is_file() and f.suffix in (".json", ".yml"):
                h.update(f.read_bytes())
    return h.hexdigest()[:12]


def _read_json(src: str, rel: str):
    if src.startswith("http"):
        r = requests.get(f"{src.rstrip('/')}/{rel}", timeout=60)
        r.raise_for_status()
        return r.json()
    return json.loads((Path(src) / rel).read_text(encoding="utf-8"))


def load_reuse(src: str, max_age_days: float):
    """Charge l'index de la génération précédente (adresse du site ou dossier local contenant web/data/)."""
    try:
        idx = _read_json(src, "web/data/index.json")
    except Exception as e:  # noqa: BLE001 : sans index précédent on calcule tout
        print(f"! réutilisation impossible ({type(e).__name__}: {e}) : tout sera recalculé", file=sys.stderr)
        return None
    if idx.get("params_hash") != PARAMS_HASH:
        print(f"! la génération précédente a des paramètres différents ({idx.get('params_hash')} contre {PARAMS_HASH}) : "
              "tout sera recalculé", file=sys.stderr)
        return None
    return {"src": src, "index": idx, "max_age_days": max_age_days,
            "by_key": {e["key"]: e for e in idx.get("starts", []) if e.get("key")}}


def reuse_candidate(key: str, levels):
    """Entrée de la génération précédente pour ce départ (indépendamment des durées disponibles : c'est
    l'appelant qui décide quelles durées, parmi celles de cette entrée, sont réutilisables), ou None."""
    if REUSE is None or key not in REUSE["by_key"]:
        return None
    old = REUSE["by_key"][key]
    if not set(levels) <= set(REUSE["index"].get("levels", {})):
        return None
    try:
        age = (time.time() - time.mktime(time.strptime(old["computed_at"], "%Y-%m-%dT%H:%M:%SZ"))) / 86400.0
    except (KeyError, ValueError):
        return None
    return old if age <= REUSE["max_age_days"] else None


def start_alt(options) -> dict:
    """Altitude du départ (m), lue au premier point des boucles (O-14 : temps d'approche entre départs voisins)."""
    for o in options:
        c = o.get("coords") or []
        if c and len(c[0]) > 2:
            return {"alt_m": round(c[0][2])}
    return {}


def process_start(st: dict, sid: str, gh_url: str, durations, levels, candidates, out: Path):
    """Calcule toutes les options d'un départ. Retourne (entrée d'index ou None, lignes de journal, secondes)."""
    t0 = time.time()
    buf: list[str] = []
    log = buf.append
    if BUDGET_END is not None and time.time() > BUDGET_END:
        log(f"- {st['name']} : ignoré (budget de temps épuisé)")
        return None, buf, 0.0
    key0 = st_key(st)
    options: list = []
    reused_entry = None
    missing_durations = list(durations)
    old = reuse_candidate(key0, levels)
    if old is not None:                                   # même point, mêmes niveaux : reprendre ce qui est réutilisable
        try:
            prev = _read_json(REUSE["src"], f"web/data/starts/{old['id']}.json")
            have = {float(d) for d in old.get("durations_h", [])}
            reusable = {float(d) for d in durations} & have
            opts = [o for o in prev["options"] if o["level"] in levels and
                    round(o["duration_target_min"] / 60, 4) in reusable]
            if opts:
                options = opts
                reused_entry = old
                missing_durations = [d for d in durations if float(d) not in reusable]
                if not missing_durations:                 # toutes les durées demandées étaient déjà calculées
                    payload = {"start": {**prev["start"], "name": st["name"]}, "options": options}
                    (out / "starts" / f"{old['id']}.json").write_text(
                        json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
                    by_dur: dict[str, int] = {}
                    for o in options:
                        k = f"{o['duration_target_min'] / 60:g}"
                        by_dur[k] = by_dur.get(k, 0) + 1
                    entry = {**old, "name": st["name"], "kind": st.get("kind", "place"), "zone": st.get("zone"),
                             **({"display_name": st["display_name"]} if st.get("display_name") else {}),
                             **({"municipality": st["municipality"]} if st.get("municipality") else {}),
                             **start_alt(options),
                             "options": len(options), "durations_h": sorted(float(k) for k in by_dur),
                             "options_by_duration": by_dur, "compare": compare_block(options), "reused": True}
                    log(f"- {st['name']} : réutilisé en entier (calculé le {old['computed_at']})")
                    return entry, buf, time.time() - t0
                log(f"- {st['name']} : {len(reusable)}/{len(durations)} durée(s) réutilisée(s) "
                    f"(calculé le {old['computed_at']}), {len(missing_durations)} à calculer")
        except Exception as e:  # noqa: BLE001 : en cas de problème on recalcule tout
            log(f"  réutilisation échouée ({type(e).__name__}) : recalcul complet")
            options, reused_entry, missing_durations = [], None, list(durations)
    gh = GraphHopper(gh_url)
    snapped = gh.nearest(st["lat"], st["lon"])
    if snapped is None or snapped[2] > 400:
        log(f"- {st['name']} : pas de route à moins de 400 m, ignoré")
        return None, buf, time.time() - t0
    st = {**st, "lon0": st["lon"], "lat0": st["lat"], "lon": snapped[0], "lat": snapped[1]}
    if reused_entry is None:
        log(f"- {st['name']}")
    dur_seconds: dict[str, float] = {}
    for duration in missing_durations:
        t_dur = time.time()
        for level in levels:
            pool: list[Loop] = []
            for profile in LEVELS[level]["profiles"]:
                log(f"  {duration:g} h / {level} / {profile}")
                found, rejects = fit_and_sample(gh, st, level, profile, duration, candidates, log)
                why = ", ".join(f"{k} {v}" for k, v in rejects.items() if v)
                log(f"    {len(found)} candidats valides" + (f" (rejetés : {why})" if why else ""))
                pool.extend(found)
            for i, (label, loop) in enumerate(pick_options(pool), start=1):
                options.append(to_json(loop, label, sid, i))
        dur_seconds[f"{duration:g}"] = round(time.time() - t_dur, 1)
    if not options:
        log("  aucune option valide, départ ignoré")
        return None, buf, time.time() - t0
    payload = {"start": {"id": sid, "name": st["name"], "lon": round(st["lon"], 5), "lat": round(st["lat"], 5)},
               "options": options}
    (out / "starts" / f"{sid}.json").write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                                                 encoding="utf-8")
    by_dur: dict[str, int] = {}
    for o in options:
        key = f"{o['duration_target_min'] / 60:g}"
        by_dur[key] = by_dur.get(key, 0) + 1
    entry = {"id": sid, "name": st["name"], "lon": payload["start"]["lon"], "lat": payload["start"]["lat"],
             "kind": st.get("kind", "place"), "zone": st.get("zone"), "key": key0,
             **({"display_name": st["display_name"]} if st.get("display_name") else {}),
             **({"municipality": st["municipality"]} if st.get("municipality") else {}),
             **start_alt(options),
             "options": len(options), "durations_h": sorted(float(k) for k in by_dur), "options_by_duration": by_dur,
             "compare": compare_block(options), "computed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "compute_seconds": round(time.time() - t0, 1), "compute_seconds_by_duration": dur_seconds}
    log(f"  {len(options)} options, durées disponibles : {', '.join(by_dur)} h ({time.time() - t0:.0f} s)")
    return entry, buf, time.time() - t0


def approach_stats(gh_url: str, a: dict, b: dict):
    """Trajet à vélo (profil approach) du départ a vers le départ b : distance, temps à 110 W avec feux, part en zone dense."""
    body = {"points": [[a["lon"], a["lat"]], [b["lon"], b["lat"]]], "profile": "approach", "points_encoded": False,
            "elevation": True, "instructions": False, "details": ["urban_density"]}
    try:
        r = requests.post(f"{gh_url}/route", json=body, timeout=60)
        path = (r.json().get("paths") or [None])[0] if r.status_code == 200 else None
    except requests.RequestException:
        path = None
    if not path:
        return None
    coords = path["points"]["coordinates"]
    cum = [0.0]
    for i in range(1, len(coords)):
        cum.append(cum[-1] + haversine(coords[i - 1][0], coords[i - 1][1], coords[i][0], coords[i][1]))
    if cum[-1] < 50:
        return None
    urban = meters_by_value(path.get("details", {}).get("urban_density"), cum)
    city, resid = urban.get("city", 0.0) / cum[-1], urban.get("residential", 0.0) / cum[-1]
    lights = SIGNALS.count_along(coords, cum) if SIGNALS is not None else None
    ds, prof = elevation_profile(coords, cum)
    t = estimate_time_s(ds, prof, 110, city, resid, lights)
    return {"road_km": round(cum[-1] / 1000.0, 2), "time_min_110W": round(t / 60.0, 1), "dense_zone_share": round(city, 2),
            "lights": lights}


def pick_neighbour_pairs(plan_all: list, n: int) -> list:
    """N départs denses (hors gares), chacun avec le départ hors zone dense le plus proche s'il est à 2,2 km ou moins."""
    dense = [x for x in plan_all if x.get("zone") == "dense" and x.get("kind") != "station"]
    others = [x for x in plan_all if x.get("zone") in ("peri", "rural")]
    pairs = []
    for a in dense[:: max(1, len(dense) // max(1, n * 3))]:            # parcours réparti sur toute la liste
        if not others:
            break
        d, b = min(((haversine(a["lon"], a["lat"], o["lon"], o["lat"]) / 1000.0, o) for o in others), key=lambda t: t[0])
        if d <= 2.2:
            pairs.append((a, b))
        if len(pairs) >= n:
            break
    return pairs


def median(v):
    v = sorted(v)
    return None if not v else (v[len(v) // 2] if len(v) % 2 else (v[len(v) // 2 - 1] + v[len(v) // 2]) / 2)


def neighbour_gain(pairs: list, index: list, out: Path, gh_url: str, out_path: str):
    """Pour chaque paire (départ dense, voisin) : temps d'approche, puis, à durée ET niveau identiques, gain du voisin sur
    les km de ville et les feux par km (meilleure option de chaque départ)."""
    by_key = {e["key"]: e for e in index}

    def best_options(entry):
        d = json.loads((out / "starts" / f"{entry['id']}.json").read_text(encoding="utf-8"))
        best: dict = {}
        for o in d["options"]:
            k = (f"{o['duration_target_min'] / 60:g}", o["level"])
            if k not in best or o["score"] > best[k]["score"]:
                best[k] = o
        return best

    rows = []
    for a, b in pairs:
        ea, eb = by_key.get(start_key(a["lon"], a["lat"])), by_key.get(start_key(b["lon"], b["lat"]))
        if not ea or not eb:
            continue
        ap_ = approach_stats(gh_url, a, b)
        if ap_ is None:
            continue
        oa, ob = best_options(ea), best_options(eb)
        for k in sorted(set(oa) & set(ob), key=lambda x: (float(x[0]), x[1])):
            x, y = oa[k], ob[k]
            rows.append({
                "dense_start": a["name"], "neighbour": b["name"], "neighbour_kind": b.get("kind"), "neighbour_zone": b.get("zone"),
                "crow_km": round(haversine(a["lon"], a["lat"], b["lon"], b["lat"]) / 1000.0, 2), **ap_,
                "duration_h": k[0], "level": k[1],
                "dense_exit_dense_km": x["exit_dense_km"], "dense_loop_km": x["distance_km"],
                "dense_never_exits": x["exit_dense_km"] is not None and x["exit_dense_km"] >= 0.98 * x["distance_km"],
                "neighbour_exit_dense_km": y["exit_dense_km"], "dense_lights_per_km": x["traffic_lights_per_km"],
                "neighbour_lights_per_km": y["traffic_lights_per_km"],
                "km_of_city_avoided": None if x["exit_dense_km"] is None or y["exit_dense_km"] is None
                else round(x["exit_dense_km"] - y["exit_dense_km"], 1),
                "lights_per_km_reduction": None if x["traffic_lights_per_km"] is None or y["traffic_lights_per_km"] is None
                else round(x["traffic_lights_per_km"] - y["traffic_lights_per_km"], 2)})

    def agg(sel):
        if not sel:
            return {"rows": 0, "pairs": 0}
        pairs_ = {(r["dense_start"], r["neighbour"]) for r in sel}
        km = [r["km_of_city_avoided"] for r in sel if r["km_of_city_avoided"] is not None]
        lp = [r["lights_per_km_reduction"] for r in sel if r["lights_per_km_reduction"] is not None]
        return {"rows": len(sel), "pairs": len(pairs_),
                "median_approach_min_one_way": median([r["time_min_110W"] for r in sel]),
                "median_km_of_city_avoided": median(km),
                "km_of_city_avoided_range": [min(km), max(km)] if km else None,
                "median_lights_per_km_reduction": median(lp),
                "lights_per_km_reduction_range": [min(lp), max(lp)] if lp else None,
                "share_dense_loop_never_exits": round(sum(1 for r in sel if r["dense_never_exits"]) / len(sel), 2),
                "share_neighbour_is_station": round(sum(1 for r in sel if r["neighbour_kind"] == "station") / len(sel), 2)}

    def per_pair(sel):
        """Une ligne par paire : médiane, sur les combinaisons durée × niveau, des km de ville évités."""
        acc: dict = {}
        for r in sel:
            acc.setdefault((r["dense_start"], r["neighbour"], r["neighbour_kind"], r["time_min_110W"], r["crow_km"]), []).append(r)
        out_ = []
        for (a_, b_, kind_, t_, crow_), rr in acc.items():
            km_ = [x["km_of_city_avoided"] for x in rr if x["km_of_city_avoided"] is not None]
            out_.append({"dense_start": a_, "neighbour": b_, "neighbour_kind": kind_, "approach_min_one_way": t_, "crow_km": crow_,
                         "median_km_of_city_avoided": median(km_), "combinations": len(rr)})
        return sorted(out_, key=lambda x: x["approach_min_one_way"])

    result = {"note": "meilleure option (note la plus haute) de chaque départ, à durée et niveau identiques ; km de ville évités = "
                      "exit_dense_km du départ dense − celui du voisin ; feux : baisse de feux par km",
              "pairs_measured": len({(r["dense_start"], r["neighbour"]) for r in rows}),
              "all_within_15min": agg([r for r in rows if r["time_min_110W"] <= 15]),
              "within_10min": agg([r for r in rows if r["time_min_110W"] <= 10]),
              "between_10_and_15min": agg([r for r in rows if 10 < r["time_min_110W"] <= 15]),
              "pairs_within_10min": per_pair([r for r in rows if r["time_min_110W"] <= 10]),
              "pairs_between_10_and_15min": per_pair([r for r in rows if 10 < r["time_min_110W"] <= 15]),
              "within_10min_by_duration_and_level": {}, "neighbour_kinds_within_10min": {}, "rows": rows}
    for r in [r for r in rows if r["time_min_110W"] <= 10]:
        k = f"{r['duration_h']} h / {r['level']}"
        result["within_10min_by_duration_and_level"].setdefault(k, []).append(r)
        result["neighbour_kinds_within_10min"][r["neighbour_kind"]] = result["neighbour_kinds_within_10min"].get(r["neighbour_kind"], 0) + 1
    result["within_10min_by_duration_and_level"] = {k: agg(v) for k, v in result["within_10min_by_duration_and_level"].items()}
    if out_path:
        Path(out_path).write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return result


def by_zone_seconds(index) -> dict:
    acc: dict = {}
    for e in index:
        if e.get("reused") or "compute_seconds" not in e:
            continue
        acc.setdefault(e.get("zone") or "inconnue", []).append(e["compute_seconds"])
    return {z: {"starts": len(v), "mean_s": round(sum(v) / len(v), 1), "max_s": round(max(v), 1)} for z, v in acc.items()}


def by_duration_seconds(index) -> dict:
    acc: dict = {}
    for e in index:
        if e.get("reused"):
            continue
        for d, sec in (e.get("compute_seconds_by_duration") or {}).items():
            acc.setdefault(d, []).append(sec)
    return {d: {"mean_s": round(sum(v) / len(v), 1), "max_s": round(max(v), 1)} for d, v in sorted(acc.items(), key=lambda x: float(x[0]))}


def machine_resources() -> dict:
    ram = None
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal"):
                ram = round(int(line.split()[1]) / 1024 / 1024, 1)
    except OSError:
        pass
    return {"cpu_count": os.cpu_count(), "ram_gb": ram}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--region", required=True, help="fichier JSON de région (config/region_*.json)")
    ap.add_argument("--places", required=True, help="GeoJSON des lieux OSM (osmium export)")
    ap.add_argument("--starts-file", default=None, help="JSON de départs [{name, lon, lat, kind}] (remplace la sélection par noms)")
    ap.add_argument("--per-zone", type=int, default=None, help="pilote : N départs par zone (dense/peri/rural), répartis sur la liste")
    ap.add_argument("--reuse-from", default=None, help="adresse du site précédent (ou dossier local) dont on réutilise les départs "
                                                        "identiques (même clé, mêmes paramètres, moins de --reuse-max-age-days)")
    ap.add_argument("--reuse-max-age-days", type=float, default=60.0)
    ap.add_argument("--pilot-names", default="Barcelona;Badalona;Montcada i Reixac;Manresa;Vic",
                    help="départs toujours inclus par --per-zone (noms séparés par ; ; ceux qui n'existent pas sont ignorés)")
    ap.add_argument("--pilot-rural", type=int, default=0, help="pilote : nombre total de départs ruraux (au moins --per-zone)")
    ap.add_argument("--pilot-box", default=None, help="pilote : « lon0,lat0,lon1,lat1:N » = N départs ruraux imposés dans cette emprise (relief)")
    ap.add_argument("--time-budget-min", type=float, default=0.0, help="ne démarre plus de nouveau départ après ce nombre de minutes de calcul "
                                                                        "(0 = illimité) ; le site est alors publié incomplet et signalé")
    ap.add_argument("--skipped-out", default=None, help="JSON des départs ignorés avec leur motif")
    ap.add_argument("--timings-out", default=None, help="JSON des temps de calcul par départ (zone, durée) : diagnostic, non publié")
    ap.add_argument("--neighbour-pilot", type=int, default=0, help="pilote : N départs denses avec leur voisin hors zone dense à moins de 2,2 km "
                                                                    "(ajoutés à la sélection) et mesure du gain de boucle")
    ap.add_argument("--neighbour-out", default=None, help="JSON du gain des départs voisins (avec --neighbour-pilot)")
    ap.add_argument("--shard", default=None, help="i/N : ne traite que les départs d'indice i modulo N (calcul en parallèle)")
    ap.add_argument("--workers", type=int, default=1, help="départs traités en parallèle (threads)")
    ap.add_argument("--out", default="web/data", help="dossier de sortie")
    ap.add_argument("--gh", default="http://localhost:8989", help="URL du serveur GraphHopper")
    ap.add_argument("--max-starts", type=int, default=None)
    ap.add_argument("--durations", type=float, nargs="*", default=None, help="durées en heures (surcharge la config)")
    ap.add_argument("--levels", nargs="*", default=None, choices=list(LEVELS), help="niveaux (surcharge la config)")
    ap.add_argument("--pbf", default=None, help="fichier .pbf de la région (par défaut : region.osm.pbf à côté de --places)")
    ap.add_argument("--no-landscape", action="store_true", help="ne pas calculer le proxy paysage (dépannage mémoire)")
    ap.add_argument("--allow-no-elevation", action="store_true", help="test uniquement : accepte un graphe sans altitude")
    ap.add_argument("--candidates", type=int, default=len(CANDIDATES), help="nombre de candidats par combinaison")
    args = ap.parse_args()

    region = json.loads(Path(args.region).read_text(encoding="utf-8"))
    durations = args.durations or region.get("durations_h", [1, 2, 3])
    levels = args.levels or region.get("levels", list(LEVELS))
    max_starts = args.max_starts or region.get("max_starts", 30)
    candidates = CANDIDATES[: max(1, args.candidates)]
    res = machine_resources()
    print(f"Machine : {res['cpu_count']} CPU, {res['ram_gb']} Go de RAM ; {args.workers} départ(s) en parallèle", flush=True)

    global SIGNALS, STOPS, LANDSCAPE
    places_path = Path(args.places)
    pbf_path = Path(args.pbf) if args.pbf else places_path.parent / "region.osm.pbf"
    SIGNALS = load_signals(pbf_path, places_path.parent)
    print(f"Feux tricolores OSM chargés : {len(SIGNALS.points) if SIGNALS else 0}", flush=True)
    STOPS = load_points(pbf_path, places_path.parent, ["n/highway=stop"], "stops")
    print(f"Panneaux stop OSM chargés : {len(STOPS.points) if STOPS else 0}", flush=True)
    try:
        LANDSCAPE = None if args.no_landscape else load_landscape(pbf_path, places_path.parent)
    except Exception as e:  # noqa: BLE001 : le paysage est facultatif, on continue sans
        print(f"! paysage ignoré ({type(e).__name__}: {e})", file=sys.stderr)
        LANDSCAPE = None
    print("Paysage OSM chargé : " + (", ".join(f"{k} {v}" for k, v in LANDSCAPE.counts.items()) if LANDSCAPE
                                     else "non disponible"), flush=True)
    gh = GraphHopper(args.gh)
    if not gh.info().get("elevation") and not args.allow_no_elevation:
        print("Le serveur GraphHopper n'a pas de données d'altitude : D+ et durées seraient faux.\n"
              "Vérifie graph.elevation.provider (ou utilise --allow-no-elevation pour un simple test).",
              file=sys.stderr)
        return 2
    if args.starts_file:
        starts = load_starts_file(Path(args.starts_file), region.get("bbox"))
    else:
        starts = load_starts(places_path, region.get("start_names"), region.get("bbox"), max_starts)
    plan_all = list(starts)
    neighbour_pairs: list = []
    if args.per_zone:
        picked = [st for st in starts if st["name"] in {n.strip() for n in args.pilot_names.split(";") if n.strip()}]
        for z in ("dense", "peri"):
            zs = [st for st in starts if st.get("zone") == z and st.get("kind") != "station"]
            step = max(1, len(zs) // args.per_zone)
            picked.extend(zs[::step][: args.per_zone])
        rural = [st for st in starts if st.get("zone") == "rural" and st.get("kind") != "station"]
        box_pick: list = []
        if args.pilot_box:
            spec, cnt = args.pilot_box.rsplit(":", 1)
            x0, y0, x1, y1 = (float(v) for v in spec.split(","))
            inbox = [st for st in rural if x0 <= st["lon"] <= x1 and y0 <= st["lat"] <= y1]
            box_pick = inbox[:: max(1, len(inbox) // max(1, int(cnt)))][: int(cnt)]
            if len(box_pick) < int(cnt):
                print(f"! pilote : {len(box_pick)} départ(s) rural(aux) seulement dans l'emprise {spec} (demandé {cnt})", file=sys.stderr)
        rest = [st for st in rural if st["name"] not in {b["name"] for b in box_pick}]
        n_rest = max(0, max(args.per_zone, args.pilot_rural) - len(box_pick))
        picked.extend(box_pick + rest[:: max(1, len(rest) // max(1, n_rest))][:n_rest])
        stations = [st for st in starts if st.get("kind") == "station"]
        picked.extend(stations[:: max(1, len(stations) // 2)][:2])          # 2 gares réparties dans la liste
        seen_names, unique = set(), []
        for st in picked:
            if st["name"] not in seen_names:
                seen_names.add(st["name"])
                unique.append(st)
        starts = unique or starts
        if args.neighbour_pilot:                       # départs denses et leur voisin hors zone dense, pour mesurer le gain
            neighbour_pairs = pick_neighbour_pairs(plan_all, args.neighbour_pilot)
            have = {st["name"] for st in starts}
            for a, b in neighbour_pairs:
                for x in (a, b):
                    if x["name"] not in have:
                        starts.append(x)
                        have.add(x["name"])
    if args.shard:
        i, n = (int(x) for x in args.shard.split("/"))
        starts = starts[i::n]
    if args.per_zone:
        pass                                            # le pilote par zone ignore --max-starts
    else:
        starts = starts[:max_starts] if not args.starts_file else starts[: args.max_starts or len(starts)]
    if not starts:
        print("Aucun point de départ trouvé.", file=sys.stderr)
        return 1

    out = Path(args.out)
    (out / "starts").mkdir(parents=True, exist_ok=True)
    global REUSE, PARAMS_HASH, BUDGET_END
    PARAMS_HASH = params_hash()
    BUDGET_END = time.time() + 60.0 * args.time_budget_min if args.time_budget_min else None
    REUSE = load_reuse(args.reuse_from, args.reuse_max_age_days) if args.reuse_from else None
    used: dict[str, int] = {}
    ids = []
    reserved = {}
    for st in starts:                                   # un départ réutilisé garde l'identifiant de la génération précédente
        old = reuse_candidate(st_key(st), levels)
        if old is not None:
            reserved[id(st)] = old["id"]
    taken = set(reserved.values())
    for st in starts:                                   # identifiants uniques
        if id(st) in reserved:
            ids.append(reserved[id(st)])
            continue
        base = slugify(st["name"])
        used[base] = used.get(base, 0) + 1
        cand = base if used[base] == 1 else f"{base}-{used[base]}"
        while cand in taken:
            used[base] += 1
            cand = f"{base}-{used[base]}"
        taken.add(cand)
        ids.append(cand)
    t0 = time.time()
    results = []
    if args.workers > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = [ex.submit(process_start, st, sid, args.gh, durations, levels, candidates, out)
                       for st, sid in zip(starts, ids)]
            for f in futures:
                results.append(f.result())
                print("\n".join(results[-1][1]), flush=True)
    else:
        for st, sid in zip(starts, ids):
            results.append(process_start(st, sid, args.gh, durations, levels, candidates, out))
            print("\n".join(results[-1][1]), flush=True)
    index = [r[0] for r in results if r[0]]
    skipped = [st["name"] for st, r in zip(starts, results) if not r[0]]

    def reason(lines):
        text = " ".join(lines)
        return ("budget de temps épuisé" if "budget de temps épuisé" in text else
                "aucune route à moins de 400 m" if "pas de route à moins de 400 m" in text else
                "aucune option valide" if "aucune option valide" in text else "autre")
    skipped_details = [{"name": st["name"], "zone": st.get("zone"), "kind": st.get("kind"), "reason": reason(r[1])}
                       for st, r in zip(starts, results) if not r[0]]
    if args.skipped_out:
        Path(args.skipped_out).write_text(json.dumps(skipped_details, ensure_ascii=False, indent=1), encoding="utf-8")
    reasons: dict = {}
    for d_ in skipped_details:
        reasons[d_["reason"]] = reasons.get(d_["reason"], 0) + 1
    elapsed = time.time() - t0

    if args.timings_out:
        Path(args.timings_out).write_text(json.dumps([
            {"name": st["name"], "zone": st.get("zone"), "kind": st.get("kind"), "reused": bool(r[0].get("reused")),
             "seconds": r[0].get("compute_seconds"), "seconds_by_duration": r[0].get("compute_seconds_by_duration")}
            for st, r in zip(starts, results) if r[0]], ensure_ascii=False, indent=1), encoding="utf-8")
    if neighbour_pairs:
        ng = neighbour_gain(neighbour_pairs, index, out, args.gh, args.neighbour_out)
        w10 = ng["within_10min"]
        print(f"Voisins : {ng['pairs_measured']} paires mesurées, dont {w10.get('pairs', 0)} à 10 min ou moins ; "
              f"km de ville évités (médiane) {w10.get('median_km_of_city_avoided')} ; baisse de feux par km {w10.get('median_lights_per_km_reduction')}", flush=True)
    info = gh.info()
    available = sorted({d for e in index for d in e["durations_h"]})
    meta = {
        "region": region.get("name"),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "osm_data_date": (info.get("data_date") if not str(info.get("data_date", "")).startswith("1970") else None),
        "generator_version": GENERATOR_VERSION,
        "durations_h": available,
        # boîte englobante des départs générés [lon_min, lat_min, lon_max, lat_max] (champ additif, pour « zone couverte »)
        "coverage_bbox": ([round(min(e["lon"] for e in index), 4), round(min(e["lat"] for e in index), 4),
                           round(max(e["lon"] for e in index), 4), round(max(e["lat"] for e in index), 4)] if index else None),
        "levels": {k: {"label": v["label"]} for k, v in LEVELS.items() if k in levels},
        "attribution": "© contributeurs OpenStreetMap (ODbL) ; calculs GraphHopper (Apache 2.0)",
        "params_hash": PARAMS_HASH,
        "stats": {"starts_requested": len(starts), "starts_generated": len(index),
                  "starts_reused": sum(1 for e in index if e.get("reused")),
                  "seconds_per_computed_start_by_zone": by_zone_seconds(index),
                  "seconds_per_start_and_duration": by_duration_seconds(index),
                  "starts_skipped": len(skipped), "skipped_by_reason": reasons, "skipped_examples": skipped[:30],
                  "incomplete": bool(reasons.get("budget de temps épuisé")),
                  "time_budget_minutes": args.time_budget_min or None,
                  "generation_seconds": round(elapsed), "seconds_per_start": round(elapsed / max(1, len(starts)), 1),
                  "workers": args.workers, **res},
        "starts": [{k: v for k, v in e.items() if k not in ("compute_seconds", "compute_seconds_by_duration", "reused")}
                   for e in index],
    }
    text = json.dumps(meta, ensure_ascii=False, separators=(",", ":"))
    meta["stats"]["index_kb"] = round(len(text.encode("utf-8")) / 1024, 1)
    meta["stats"]["index_bytes_per_start"] = round(len(text.encode("utf-8")) / max(1, len(index)))
    (out / "index.json").write_text(json.dumps(meta, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    if reasons.get("budget de temps épuisé"):
        print(f"::warning::GÉNÉRATION INCOMPLÈTE : {reasons['budget de temps épuisé']} départs non démarrés (budget de temps de "
              f"{args.time_budget_min:g} min épuisé). Relancer avec « reuse » coché pour terminer.", flush=True)
    print(f"Terminé : {len(index)} départs sur {len(starts)} en {elapsed:.0f} s "
          f"({elapsed / max(1, len(starts)):.0f} s par départ) -> {out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
