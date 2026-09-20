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
    "modere": {"watts": 150, "label": "soutenu modéré", "profiles": ["calm", "sport"]},
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

    def __init__(self, forests, waters, protected, viewpoints):
        import numpy as np  # noqa: F401
        from shapely.strtree import STRtree
        self.trees = {k: (STRtree(v) if v else None)
                      for k, v in (("forest", forests), ("water", waters), ("protected", protected),
                                   ("view", viewpoints))}
        self.counts = {"forest": len(forests), "water": len(waters), "protected": len(protected),
                       "view": len(viewpoints)}

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
                "score": round(100 * index)}


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
    filters = ["nwr/landuse=forest", "nwr/natural=wood", "nwr/natural=water", "nwr/waterway=river",
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
    forests, waters, protected, views = [], [], [], []
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
            elif props.get("leisure") in ("park", "nature_reserve") or props.get("boundary") in (
                    "protected_area", "national_park"):
                protected.append(geom)
    return LandscapeIndex(forests, waters, protected, views)


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


def slope_stats(ds, prof):
    """Pente max (lissée), répartition de la distance par classe de pente, et montées significatives."""
    grades = [(prof[k] - prof[k - 1]) / ds for k in range(1, len(prof))]
    if not grades:
        return {"max_grade_pct": 0.0, "bands": {}, "climbs": [], "n_climbs": 0}
    bands = {"descent": 0, "flat": 0, "up_3_6": 0, "up_6_9": 0, "up_9_plus": 0}
    for g in grades:
        key = ("descent" if g < -0.03 else "flat" if g < 0.03 else "up_3_6" if g < 0.06
               else "up_6_9" if g < 0.09 else "up_9_plus")
        bands[key] += 1
    n = len(grades)
    # montées : suites monotones dont la remontée dépasse 5 m, gardées si gain >= 20 m
    runs, direction, low_i, high_i, start_i = [], 0, 0, 0, 0
    for i in range(1, len(prof)):
        v = prof[i]
        if direction == 0:
            if v - prof[low_i] >= 5.0:
                direction, start_i, high_i = 1, low_i, i
            elif prof[high_i] - v >= 5.0:
                direction, low_i = -1, i
            else:
                low_i = i if v <= prof[low_i] else low_i
                high_i = i if v >= prof[high_i] else high_i
        elif direction == 1:
            if v > prof[high_i]:
                high_i = i
            elif prof[high_i] - v >= 5.0:
                runs.append((start_i, high_i))
                direction, low_i = -1, i
        else:
            if v <= prof[low_i]:
                low_i = i
            elif v - prof[low_i] >= 5.0:
                direction, start_i, high_i = 1, low_i, i
    if direction == 1:
        runs.append((start_i, high_i))
    climbs = []
    for a, b in runs:
        gain, length = prof[b] - prof[a], (b - a) * ds
        if gain >= 20.0 and length > 0:
            climbs.append({"start_km": round(a * ds / 1000.0, 1), "length_km": round(length / 1000.0, 1),
                           "gain_m": round(gain), "avg_grade_pct": round(100.0 * gain / length, 1)})
    climbs.sort(key=lambda c: -c["gain_m"])
    return {"max_grade_pct": round(100.0 * max(grades), 1), "bands": {k: round(v / n, 3) for k, v in bands.items()},
            "climbs": climbs[:5], "n_climbs": len(climbs)}


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


def score(l: Loop) -> float:
    s = l.shares
    parts = {
        "calm": 1.0 - (s["urban"]["city"] + 0.4 * s["urban"]["residential"]),
        "axes": 1.0 - min(1.0, 2.0 * s["main_roads"]),
        "infra": min(1.0, 2.0 * s["dedicated_cycleway"]),
        "flow": 1.0 - min(1.0, 2.0 * l.overlap + 0.15 * l.u_turns),
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
    total = sum(weights[k] * parts[k] for k in weights) / sum(weights.values())
    total -= min(0.3, max(0.0, s["unpaved"] - 0.03) * 2.0)
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


def pitch(l: Loop, label: str) -> list[str]:
    """Phrases construites uniquement à partir de mesures (aucune affirmation non vérifiée)."""
    s = l.shares
    urban = s["urban"]
    minutes = round(l.time_s / 60.0)
    per100 = l.dplus_per_km * 100.0
    lines = [
        f"{l.distance_m / 1000:.0f} km, {l.ascend_m:.0f} m de dénivelé positif, "
        f"environ {format_duration(minutes)} à un rythme « {LEVELS[l.level]['label']} »."
    ]
    lines.append(f"Profil {relief_category(per100)} : {per100:.0f} m de D+ pour 100 km.")
    if urban["rural"] >= 0.6:
        lines.append(f"{urban['rural'] * 100:.0f} % du parcours en zone rurale.")
    if urban["city"] <= 0.05:
        lines.append("Évite les zones urbaines denses (moins de 5 % du parcours).")
    else:
        lines.append(f"{urban['city'] * 100:.0f} % du parcours en zone urbaine dense.")
    if l.exit_dense_m is not None and l.exit_dense_m > 0:
        if l.exit_dense_m >= l.distance_m * 0.98:
            lines.append("Reste en zone urbaine dense sur tout le parcours.")
        else:
            lines.append(f"Sort de la zone urbaine dense après {l.exit_dense_m / 1000:.1f} km.")
    if l.signals is not None:
        per_km = l.signals / max(l.distance_m / 1000.0, 0.1)
        if l.signals == 0:
            lines.append("Aucun feu tricolore recensé dans OpenStreetMap sur le parcours.")
        else:
            lines.append(f"{l.signals} carrefours à feux tricolores ({per_km:.1f} par km).")
    tr = l.terrain
    if tr.get("climbs"):
        c = tr["climbs"][0]
        lines.append(f"Plus longue montée : {c['length_km']:.1f} km à {c['avg_grade_pct']:.1f} % (+{c['gain_m']} m) ; "
                     f"{tr['n_climbs']} montée(s) de plus de 20 m au total, pente max lissée {tr['max_grade_pct']:.0f} %.")
    elif tr:
        lines.append("Aucune montée significative (plus de 20 m d'un seul tenant).")
    if l.u_turns:
        lines.append(f"{l.u_turns} demi-tour(s) sur le parcours.")
    sc = l.scenery
    if sc:
        if sc["forest"] >= 0.15:
            lines.append(f"{sc['forest'] * 100:.0f} % du parcours dans ou en bordure de forêt.")
        if sc["water"] >= 0.05:
            lines.append(f"{sc['water'] * 100:.0f} % à moins de 100 m d'un plan d'eau, d'une rivière ou de la mer.")
        if sc["protected"] >= 0.15:
            lines.append(f"{sc['protected'] * 100:.0f} % dans un parc ou un espace protégé.")
        if sc["viewpoints"] >= 1:
            lines.append(f"{sc['viewpoints']} point(s) de vue référencé(s) à moins de 300 m.")
    sm = l.surface_mix
    if sm and sm.get("unknown", 0) >= 0.3:
        lines.append(f"Surface non renseignée dans OpenStreetMap sur {sm['unknown'] * 100:.0f} % du parcours.")
    if s["main_roads"] < 0.01:
        lines.append("Aucune route principale sur le parcours.")
    elif s["main_roads"] <= 0.10:
        lines.append(f"Peu de routes principales ({s['main_roads'] * 100:.0f} % du parcours).")
    else:
        lines.append(f"{s['main_roads'] * 100:.0f} % sur des routes principales : à parcourir avec vigilance.")
    if s["dedicated_cycleway"] >= 0.10:
        lines.append(f"{s['dedicated_cycleway'] * 100:.0f} % sur pistes cyclables ou voies vertes.")
    if s["unpaved"] > 0.05:
        lines.append(f"{s['unpaved'] * 100:.0f} % sur revêtement non goudronné.")
    if l.overlap > 0.10:
        lines.append(f"{l.overlap * 100:.0f} % de tronçons empruntés deux fois.")
    return lines


def to_json(l: Loop, label: str, start_id: str, idx: int) -> dict:
    simplified = simplify(l.coords, 10.0)
    return {
        "id": f"{start_id}-{l.duration_h:g}h-{l.level}-{l.profile}-{idx}",
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
                    "n_climbs": l.terrain.get("n_climbs"), "climbs": l.terrain.get("climbs")},
        "u_turns": l.u_turns,
        "longest_repeat_km": round(l.longest_repeat_m / 1000.0, 2),
        "scenery": (None if l.scenery is None else {
            "forest": round(l.scenery["forest"], 3), "water": round(l.scenery["water"], 3),
            "protected": round(l.scenery["protected"], 3), "viewpoints": l.scenery["viewpoints"],
            "score": l.scenery["score"]}),
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
    """Départs fournis par un fichier JSON [{name, lon, lat, kind?}] (ex. produit par plan_starts.py)."""
    out = []
    for e in json.loads(path.read_text(encoding="utf-8")):
        if bbox and not (bbox[0] <= e["lon"] <= bbox[2] and bbox[1] <= e["lat"] <= bbox[3]):
            continue
        out.append({"name": e["name"], "lon": e["lon"], "lat": e["lat"], "kind": e.get("kind", "place")})
    return out


def process_start(st: dict, sid: str, gh_url: str, durations, levels, candidates, out: Path):
    """Calcule toutes les options d'un départ. Retourne (entrée d'index ou None, lignes de journal, secondes)."""
    t0 = time.time()
    buf: list[str] = []
    log = buf.append
    gh = GraphHopper(gh_url)
    snapped = gh.nearest(st["lat"], st["lon"])
    if snapped is None or snapped[2] > 400:
        log(f"- {st['name']} : pas de route à moins de 400 m, ignoré")
        return None, buf, time.time() - t0
    st = {**st, "lon": snapped[0], "lat": snapped[1]}
    log(f"- {st['name']}")
    options = []
    for duration in durations:
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
             "kind": st.get("kind", "place"), "options": len(options),
             "durations_h": sorted(float(k) for k in by_dur), "options_by_duration": by_dur}
    log(f"  {len(options)} options, durées disponibles : {', '.join(by_dur)} h ({time.time() - t0:.0f} s)")
    return entry, buf, time.time() - t0


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
    if args.shard:
        i, n = (int(x) for x in args.shard.split("/"))
        starts = starts[i::n]
    starts = starts[:max_starts] if not args.starts_file else starts[: args.max_starts or len(starts)]
    if not starts:
        print("Aucun point de départ trouvé.", file=sys.stderr)
        return 1

    out = Path(args.out)
    (out / "starts").mkdir(parents=True, exist_ok=True)
    used: dict[str, int] = {}
    ids = []
    for st in starts:                                   # identifiants uniques et stables
        base = slugify(st["name"])
        used[base] = used.get(base, 0) + 1
        ids.append(base if used[base] == 1 else f"{base}-{used[base]}")
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
    elapsed = time.time() - t0

    info = gh.info()
    available = sorted({d for e in index for d in e["durations_h"]})
    meta = {
        "region": region.get("name"),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "osm_data_date": (info.get("data_date") if not str(info.get("data_date", "")).startswith("1970") else None),
        "durations_h": available,
        "levels": {k: {"label": v["label"]} for k, v in LEVELS.items() if k in levels},
        "attribution": "© contributeurs OpenStreetMap (ODbL) ; calculs GraphHopper (Apache 2.0)",
        "stats": {"starts_requested": len(starts), "starts_generated": len(index),
                  "starts_skipped": len(skipped), "skipped_examples": skipped[:30],
                  "generation_seconds": round(elapsed), "seconds_per_start": round(elapsed / max(1, len(starts)), 1),
                  "workers": args.workers, **res},
        "starts": index,
    }
    (out / "index.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Terminé : {len(index)} départs sur {len(starts)} en {elapsed:.0f} s "
          f"({elapsed / max(1, len(starts)):.0f} s par départ) -> {out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
