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
RIDER_KG = 75.0               # cycliste seul : puissance = FTP (W/kg) × RIDER_KG × part de FTP tenue
LONG_RIDE_H = 3.0             # à partir de 3 h, on tient 5 points de FTP en moins (on ne tient pas 75 % pendant 4 h)
LONG_RIDE_DROP = 0.05


def level_watts(level: str, duration_h: float) -> float:
    """Puissance moyenne tenue sur la sortie (W) pour une allure et une durée."""
    lv = LEVELS[level]
    return lv["ftp_wkg"] * RIDER_KG * (lv["intensity"] - (LONG_RIDE_DROP if duration_h >= LONG_RIDE_H else 0.0))
CDA = 0.40                    # m² (position mains sur les cocottes)
CRR = 0.005                   # résistance au roulement, route goudronnée
DRIVETRAIN_EFF = 0.975
AIR_RHO = 1.2
G = 9.81
DESCENT_CAP_MS = 13.0         # ~47 km/h : on ne suppose pas de descentes plus rapides
REAL_WORLD_FACTOR = 0.93      # arrêts, virages, prudence ; 0,88 jusqu'à la v14 : la 2e sortie de Florent (Gràcia 2 h,
#                              03/10/2026) était 10 % plus rapide que le modèle, en ville comme hors ville

LEVELS = {                    # puissance moyenne soutenue (W) utilisée EN INTERNE uniquement
    # allures définies par la FTP (W/kg, tableau de profil de puissance de Coggan) et la part de FTP tenue sur la sortie
    # (décision de Florent, 27/09/2026) : 2,3 (« Untrained / Fair ») à 65 %, 3,0 (« Fair / Moderate ») à 70 %,
    # 3,8 (« Good », cat. 3) à 75 %, pour un cycliste de RIDER_KG ; voir level_watts()
    "facile": {"ftp_wkg": 2.3, "intensity": 0.65, "label": "tranquille", "profiles": ["calm"]},
    "modere": {"ftp_wkg": 3.0, "intensity": 0.70, "label": "modéré", "profiles": ["calm", "sport"]},
    "soutenu": {"ftp_wkg": 3.8, "intensity": 0.75, "label": "sportif", "profiles": ["sport"]},
}

# Candidats testés pour chaque combinaison : (seed, cap souhaité ou None). Les 8 caps permettent de trouver
# des boucles qui sortent plus vite de la zone urbaine dense.
CANDIDATES = [(1, None), (2, None), (3, 0), (4, 90), (5, 180), (6, 270), (7, 45), (8, 135), (9, 225), (10, 315)]
# Tirages « montée » (niveau sportif) : le calcul ordinaire évite les côtes (bike_elevation ralentit les pentes) ; ces
# tirages les favorisent pour qu'une option « Plus de relief » existe quand le terrain le permet (Gràcia 2 h sportif :
# boucle par Collserola, 888 m de D+, perdue au recalcul O-15). Le score choisit toujours la boucle « équilibrée ».
# O-18 option B : poids du relief dans la note, par niveau (vide = aucun bonus, réglage actuel). Réglé après la sonde
# « relief » (nature_check --relief) ; partie « relief » = D+ par km rapporté à RELIEF_FULL_M_PER_KM (plafonné à 1).
RELIEF_WEIGHTS: dict = {"soutenu": 0.30}   # v15 (sonde du 03/10/2026, 16 départs) : D+ 615 -> 682 m, forêt 34 -> 42 %,
#   feux/km médian 0,71 -> 0,60, routes principales 13 -> 20 % (routes de col) ; avant : 0,10   # sonde relief (run #92) : D+ médian +12 %, forêt 44 -> 54 %, routes de col acceptées
# O-18 option A : une option secondaire (moins / plus de relief, variante) qui répète une option de l'allure inférieure
# (même durée) est remplacée ; « répète » = chacune recouvre l'autre à au moins 80 % (une boucle courte contenue dans
# une plus longue et plus vallonnée n'est PAS un doublon). La boucle principale (équilibrée) n'est jamais écartée.
LEVEL_DUP_SIM = 0.8           # (si aucune autre boucle n'existe, l'option est gardée et marquée : voir choose_options)
RELIEF_FULL_M_PER_KM = 20.0   # 20 m de D+ par km (2 000 m pour 100 km) = relief maximal noté
CLIMB_LEVELS = ("soutenu",)
CLIMB_CANDIDATES = [(11, None), (12, None), (13, 0), (14, 90), (15, 180), (16, 270)]
CLIMB_MODEL = {
    "priority": [{"if": "average_slope >= 3", "multiply_by": "1.5"}],            # préférer les tronçons qui montent
    "speed": [{"if": "average_slope >= 4 && average_slope < 12", "multiply_by": "1.2"}],  # compense bike_elevation
}

SIGNAL_DELAY_S = 10.0         # attente moyenne par feu tricolore franchi ; sortie de Florent (Gràcia 2 h, 28/09/2026) :
#                               166 feux franchis, 61 arrêts, 28,8 min -> 10,4 s par feu : confirmé (une seule sortie)
CITY_SLOWDOWN = 0.25          # 01/10/2026 : 2e sortie de Florent (Gràcia 2 h, Sant Andreu / Nou Barris, ville moins dense,
#                               feux cyclistes) : ville −11 % en roulant, 8,4 s par feu ; 1re sortie (Eixample, port) −27 % ;
#                               25 % = compromis (1re sortie −2 %, 2e +4 % sur la durée). À terme : selon la densité du bâti.
#                               Avant : 0,30 (1re sortie seule). Commentaire d'origine :
#                               en ville, même en roulant (freinages, relances, virages, pistes partagées) : même sortie,
#                               19,7 km/h en ville contre 26,9 km/h dans le delta (−27 %) ; 0,05 jusqu'au générateur 10
SIGNAL_RADIUS_M = 15.0        # un feu OSM à moins de 15 m du tracé est considéré comme franchi
SIGNAL_CLUSTER_M = 60.0       # feux de sens différents d'un même carrefour : comptés une seule fois

PROFILE_STEP_M = 100.0        # pas de ré-échantillonnage du profil altimétrique
SMOOTH_WINDOW = 5             # moyenne mobile (5 pas = 500 m) : les données SRTM sont bruitées, surtout en ville
ASCENT_THRESHOLD_M = 3.0      # une variation < 3 m n'est pas comptée comme montée ou descente (comme un GPS/baromètre)
TIME_TOLERANCE = 0.15         # écart accepté sur la durée cible
MAX_OVERLAP = 0.25            # part max de tronçons empruntés 2 fois
MAX_UNPAVED = 0.12
# Aller-retour (Florent, 03/10/2026 : Sant Adrià 1 h, l'aller-retour sur la piste du Besòs note 59 contre 42) : accepté
# au-delà de MAX_OVERLAP si la partie répétée est surtout piste cyclable ou voie verte. Essai : désactivé en production.
OUTBACK_OK = True
OUTBACK_MAX_OVERLAP = 1.0        # un vrai aller-retour est répété à ~100 % (Sant Adrià 1 h : 72 %, note 65 contre 46)
OUTBACK_MIN_CYCLE = 0.6          # part de piste cyclable / voie verte dans la partie répétée
OUTBACK_NEAR_KM = None           # essai (O-38) : allers-retours seulement si l'eau est à moins de N km du départ
OUTBACK_DRAWS = True             # tirages « aller-retour au bord de l'eau » (rivière, mer)
# Repli (choix B de Florent, 28/09/2026, test v10 : Castellgalí, Gaià perdaient leurs boucles courtes) : s'il n'existe AUCUN
# candidat sous MAX_UNPAVED pour un départ, une durée et un niveau, on garde les candidats les moins terreux jusqu'à
# FALLBACK_UNPAVED_KM et FALLBACK_UNPAVED_SHARE, signalés dans l'appli (unpaved_fallback).
FALLBACK_UNPAVED_KM = 3.0
VARIANT_MIN_SCORE_RATIO = 0.7
# Tirages ciblés (30/09/2026, Gràcia 2 h : 5 candidats valides seulement, aucun vers le front de mer ; idée de Florent :
# « rejoindre le front de mer via le Besòs ») : en plus des tirages au hasard, boucles passant par les lieux attrayants
# les plus proches (mer, grande rivière, grand espace vert), seuls (triangle) ou enchaînés (rivière puis mer, etc.).
TARGET_MIN_KM = 1.0              # lieu déjà au départ : rien à viser
TARGET_MAX_SHARE = 0.4           # lieu au plus à 40 % de la longueur de la boucle
TARGET_GREEN_MIN_DEG2 = 5e-5     # grand espace vert (~0,5 km²), comme le diagnostic O-12
TARGET_OFFSET_DEG = 45.0
# Retouche des meilleures boucles (Florent, 02/10/2026 : la Plata 2 h, rejoindre la piste du Besòs dès l'aller ; 74 contre
# 60 au diagnostic). On découpe chacune des meilleures boucles en points de passage, puis on essaie de petites variantes :
# suivre la rivière ou la mer entre deux points de passage, ou passer par un espace vert ou un lieu remarquable proche. Une
# variante n'est gardée que si sa note dépasse celle de la boucle de départ. Désactivée tant que RETOUCH est faux (le
# diagnostic l'appelle directement pour la mesurer).
RETOUCH = True                   # v13 (02/10/2026)
RETOUCH_TOP = 2                  # boucles retouchées par durée et allure
RETOUCH_ANCHORS = (0.2, 0.4, 0.6, 0.8)   # points de passage pris sur la boucle (part de la distance)
RETOUCH_REACH = 0.15             # lieu attrayant à au plus 15 % de la longueur de la boucle d'un point de passage…
RETOUCH_REACH_MIN_KM = 2.0       # … et jamais moins de 2 km
RETOUCH_MIN_RUN_KM = 1.0         # tronçon de rivière ou de mer suivi : au moins 1 km
RETOUCH_ON_ROUTE_DEG = 0.003     # ~250 m : lieu déjà sur la boucle, rien à retoucher
RETOUCH_MAX_TRIALS = 10          # variantes essayées par boucle (une requête GraphHopper chacune) ; sonde du 02/10/2026
# (16 départs, tous les essais) : 10 essais triés par rentabilité gardent 122 des 135 améliorations, pour 30 % des essais
# Types d'essais du plus au moins rentable (gain de note par essai, même sonde) : on essaie d'abord ceux-là
RETOUCH_KIND_ORDER = ["mer/sortie-mi-Q/garde", "lieu/saute", "lieu/garde", "mer/sortie-Q/saute", "mer/sortie-mi-Q/saute",
                      "mer/seule/garde", "mer/seule/saute", "mer/sortie-Q/garde", "mer/sortie-mi-Q2/garde",
                      "riviere/seule/garde", "mer/sortie-mi-Q2/saute", "vert/garde", "riviere/seule/saute", "vert/saute",
                      "riviere/sortie-Q/saute", "mer/sortie-Q2/saute", "riviere/sortie-mi-Q/saute", "riviere/sortie-Q/garde",
                      "riviere/sortie-mi-Q/garde", "riviere/sortie-mi-Q2/garde", "riviere/sortie-mi-Q2/saute",
                      "riviere/sortie-Q2/saute", "mer/sortie-Q2/garde", "riviere/sortie-Q2/garde"]
RETOUCH_ANCHOR_SHIFT_M = 300.0
# Essais (03/10/2026, la Plata sportif 2 h : 10 essais épuisés avant les essais « rivière » ; sur la sonde, 45 % des essais
# échouent sur la durée). Désactivés en production tant qu'ils ne sont pas validés (diagnostic --retouch-prefilter) :
RETOUCH_DEDUPE = True            # essais aux mêmes points de passage : un seul
RETOUCH_WORST_LEG_FIRST = True   # essais d'abord sur le tronçon le plus chargé en feux (comme Florent à la Plata : il a
#                                  corrigé l'aller par Sant Andreu en modéré, le retour en sportif)
RETOUCH_FAMILY_CAP = None        # au plus N essais par famille (mer, lieu, rivière, vert) avant les autres : diversité
RETOUCH_PREFILTER = 0.25         # écart max de durée PRÉVUE (longueur à vol d'oiseau des points de passage, rapportée à
#                                  celle de la boucle de départ) ; au-delà, l'essai est écarté sans requête ni compter   # point de passage tombant sur un vrai demi-tour de la boucle : décalé d'autant
# Demi-tours (diagnostic la Plata, 02/10/2026) : les rampes en lacets du parc fluvial du Besòs comptaient comme demi-tours.
# Un vrai demi-tour reprend la même rue à l'envers : le tracé 30 m après passe à moins de UTURN_SAME_STREET_M de celui
# 30 m avant. Essai (diagnostic --lacets) : UTURN_LACETS_OK vrai compte seulement ceux-là (filtre ET note) ; la production
# compte encore tous les virages >= 150°.
UTURN_SAME_STREET_M = 8.0
UTURN_LACETS_OK = True           # v13 (02/10/2026)
TARGET_MAX_PLANS = 6            # tirages ciblés par durée et allure (premier profil seulement) ; +2 si lieu remarquable
# Lieux remarquables (01/10/2026, Florent : « le Tibidabo, panorama sur Barcelone, devrait être encouragé, sans que tous les
# parcours y passent » ; 51 boucles sur 5 865 dans un rayon de 20 km y passaient, dont 44 partant du sommet) : sommets,
# belvédères et cols d'OSM dont l'élément Wikidata a au moins 6 articles Wikipédia (scripts/remarkable_places.json, 43
# lieux : Montserrat, Tibidabo, Montjuïc, Turó de l'Home, Bunkers del Carmel…) ; tirages ciblés et bonus modéré.
REMARKABLE_NEAR_DEG = 0.0015    # ~130 m du tracé
REMARKABLE_BONUS = 0.05         # 5 points par lieu
REMARKABLE_MAX_BONUS = 0.08
# Lieux remarquables, essais du 03/10/2026 (désactivés en production) : Sarrià 1 h visait le Turó del Carmel (6 articles,
# le plus proche) et pas le Tibidabo (31) ; Montserrat (sommet inaccessible) jamais atteint.
REMARKABLE_FAME = True           # tirages ciblés vers les 2 lieux les plus célèbres à portée (et non le plus proche)
REMARKABLE_POINTS = True         # lieux de la liste ayant un « point » (accès relu, ex. monastère de Montserrat) visés aussi
LIEU_FAME: dict = {}             # nom du lieu -> nombre d'articles Wikipédia
# Belvédères quels qu'ils soient (Florent, 01/10/2026) : bonus à part, pour ceux devant lesquels on passe vraiment ; avant,
# 0,05 de la part « paysage » par point de vue à moins de ~300 m (≈ 0,6 point de note, trop loin et trop faible)
VIEW_NEAR_DEG = 0.0012          # ~100 m
VIEW_BONUS = 0.02               # 2 points par belvédère
VIEW_MAX_BONUS = 0.04
UNPAVED_PENALTY_PER_KM = 0.03    # note : 3 points par km de terre (notée, probable ou ICGC), 30 au plus
FALLBACK_UNPAVED_SHARE = 0.20
MAX_UTURNS = 2                # demi-tours acceptés (impasses parcourues aller-retour)
WEIGHTS = {"calm": 0.22, "lights": 0.22, "axes": 0.14, "infra": 0.12, "flow": 0.18, "scenery": 0.12}
# « ville » de la répartition du terrain (O-9.4) : au moins LANDCOVER_BLD_MIN bâtiments OSM à moins de ~75 m du point.
# Les zones bâties « landuse » ne marchent pas à Barcelone (îlots dessinés sans les rues) ; calage : diagnostic
# landcover_check n°2 du 26/09/2026 (Gràcia 1 h : 89 % ; Collserola 23-33 %). Partagé avec scripts/landcover.py.
LANDCOVER_BLD_DEG = 0.0009
LANDCOVER_BLD_MIN = 5
FLOW_OVERLAP_FACTOR = 1.0     # pénalité des tronçons répétés (2.0 jusqu'à la v4 ; D21 : diagnostic nature_check v5)
LIGHTS_PER_KM_ZERO_SCORE = 2.5  # à 2,5 feux/km, le sous-score "feux" tombe à 0 ; −1 à 5 feux/km
# zones industrielles et portuaires (OSM landuse=industrial, port) : sortie de Florent, zone franche et port de Barcelone
# « au milieu des entrepôts, travaux et camions : pas agréable, un peu dangereux, pollué » ; 14 % du parcours -> −7 points
INDUSTRIAL_PENALTY = 0.5
INDUSTRIAL_MAX_PENALTY = 0.15
# Options secondaires (Florent, 05/10/2026 : « Plus de pistes » par le port revenue en v15) : jamais nettement pire que la
# recommandée sur les feux (+25 % et au moins +0,4 / km) ni à 10 % de zone industrielle ; une autre candidate est cherchée
# (même règle que le filtre de l'appli). Essai : désactivé en production.
SECONDARY_GUARD = False
SECONDARY_MAX_LIGHTS_RATIO = 1.25
SECONDARY_MAX_LIGHTS_GAP = 0.4
SECONDARY_MAX_INDUSTRIAL = 0.10
# Éperons (Florent, 05/10/2026 : allers-retours « sans raison » de quelques centaines de mètres, 62 % des boucles
# publiées en ont au moins un de 100 m ou plus). Un éperon = la boucle repart en sens inverse dans un couloir de
# SPUR_CORRIDOR_M ; il est justifié s'il monte d'au moins SPUR_JUSTIFIED_DPLUS_M, mène à un lieu remarquable ou un
# belvédère, ou longe surtout l'eau, la forêt ou un parc. Les autres sont pénalisés et, pour les meilleures candidates,
# coupés (boucle recalculée sans l'éperon). Essai : désactivé en production.
SPUR_FIX = False
SPUR_CORRIDOR_M = 40.0
SPUR_MIN_M = 150.0              # éperon pris en compte à partir de cette longueur (aller seul)
SPUR_END_M = 300.0              # au départ ou à l'arrivée (rue en cul-de-sac du départ) : ignoré
SPUR_JUSTIFIED_DPLUS_M = 40.0
SPUR_PLEASANT_SHARE = 0.6
SPUR_PENALTY = 0.04             # 4 points par éperon injustifié…
SPUR_PENALTY_PER_100M = 0.01    # … plus 1 point par 100 m
SPUR_MAX_PENALTY = 0.20
SPUR_TRIM_TOP = 6               # meilleures candidates dont on essaie de couper les éperons
# Mémoire des bonnes boucles (Florent, 05/10/2026 : « ne jamais régresser ») : les boucles publiées de la version
# précédente sont recalculées et ajoutées aux candidates, notées avec les nouvelles règles. Essai : désactivé.
KEEP_PREVIOUS = False
ROUTE_WAYPOINT_M = 2000.0       # points de passage pris tous les N m pour recalculer une boucle existante
# Vérification de Florent (29/09/2026, Sant Andreu 2 h : 17 % « industriel », 13 tronçons jugés un par un) : la piste
# cyclable au bord du fleuve qui longe une zone, un bâtiment isolé, une route en contrebas étaient comptés (règle « à 10 m »).
# Règle : point DANS une zone industrielle ou portuaire (plus « à 10 m »), hors parc, et seulement sur un passage d'au moins
# INDUSTRIAL_MIN_RUN_M continus (un trou de 100 m ne coupe pas le passage) : traverser 100 ou 200 m d'une zone passe presque
# inaperçu (tronçons jugés « peu problématiques »), 3 km entre les entrepôts non (sa zone portuaire : 3,2 puis 1,7 km).
# Tronçons jugés bons : 6/9 points comptés -> 0/9 ; moyen, « à éviter », « camions possibles » (0,6 / 1,1 / 0,9 km) gardés ;
# Sant Andreu 2 h : 17 -> ~6 %. Taille de zone en garde-fou léger (parcelle isolée) : un seuil de 0,25 km² perdait le
# tronçon « à éviter » (zone de 0,21 km²). Seuils calés sur une seule boucle : à confirmer par d'autres retours.
INDUSTRIAL_MIN_KM2 = 0.1
INDUSTRIAL_MIN_RUN_M = 500.0
KM2_PER_DEG2 = 111.32 ** 2 * 0.749          # à la latitude de Barcelone (cos 41,5°)
SIGNALS = None                  # SignalIndex des feux tricolores, chargé dans main()
STOPS = None                    # SignalIndex des panneaux stop
LANDSCAPE = None                # LandscapeIndex (forêts, eau, parcs, points de vue), chargé dans main()
POIS = None                     # points d'intérêt le long des boucles (eau, cafés, gares, cols), voir load_pois()
# Proxy "paysage" : distances (en degrés, ~100 m = 0,0009 à cette latitude) et pondération PROVISOIRE
SCENERY_FOREST_DEG, SCENERY_WATER_DEG, SCENERY_PROTECTED_DEG, SCENERY_VIEW_DEG = 0.0004, 0.001, 0.0002, 0.003
UNPAVED = {"unpaved", "compacted", "fine_gravel", "gravel", "ground", "dirt", "grass", "sand"}
COBBLES = {"cobblestone", "sett", "paving_stones"}
ASPHALT = {"asphalt", "concrete", "paved"}
DETAILS = ["road_class", "surface", "urban_density", "bike_network", "bike_road_access", "track_type", "osm_way_id"]
# Revêtement « probablement non goudronné » (28/09/2026, diagnostic landcover_check « surface » : 877 départs) : quand le
# revêtement d'une piste OSM est noté, les pistes de qualité 2 à 5 sont en terre à 99-100 %, celles sans qualité à 91 %,
# celles de qualité 1 à 13 % seulement. Sans revêtement noté, on compte donc comme non goudronnés : les pistes (track) de
# qualité autre que 1, et les sentiers (path, footway, bridleway) hors de la ville. Même règle que road_surface.json.
PROBABLE_UNPAVED_PATHS = {"path", "footway", "bridleway"}


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

    def __init__(self, forests, waters, protected, viewpoints, builtup=(), buildings=(), sea=(), rivers=(), industrial=()):
        import numpy as np  # noqa: F401
        from shapely.strtree import STRtree
        builtup, sea, rivers, industrial = list(builtup), list(sea), list(rivers), list(industrial)
        self.trees = {k: (STRtree(v) if v else None)
                      for k, v in (("forest", forests), ("water", waters), ("protected", protected),
                                   ("view", viewpoints), ("builtup", builtup), ("buildings", list(buildings)),
                                   ("sea", sea), ("river", rivers), ("industrial", industrial))}   # sea / river : aussi
                                                                      # dans water (côte, plages ;
                                                                      # ligne centrale des grandes rivières)
        self.counts = {"forest": len(forests), "water": len(waters), "protected": len(protected),
                       "view": len(viewpoints), "builtup": len(builtup), "buildings": len(buildings), "sea": len(sea),
                       "river": len(rivers), "industrial": len(industrial)}

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
        ind_mask = industrial_mask(self.trees, pts)       # par point (tous les 100 m) : surlignage dans l'appli
        industrial = float(ind_mask.mean()) if len(xy) else 0.0
        index = min(1.0, 0.8 * forest + 1.5 * water + 0.6 * protected)   # belvédères : bonus à part (VIEW_BONUS)
        return {"forest": forest, "water": water, "protected": protected, "viewpoints": views,
                "industrial": industrial, "industrial_mask": ind_mask.tolist(), "score": round(100 * index),
                "landcover": self.landcover(pts)}

    def landcover(self, pts) -> dict | None:
        """Répartition du tracé (points tous les 100 m), UNE catégorie par point, par priorité : ville (au moins
        LANDCOVER_BLD_MIN bâtiments à moins de ~75 m) > eau (< ~100 m ; mer < ~250 m) > forêt > espaces ouverts ; à moins
        de ~50 m de la mer, bord d'eau avant ville (front de mer). Les 4 parts font 100 % : barre de terrain (O-9.4).
        « city » (point par point) = bâti seul : sortie de ville. Sans bâtiments chargés : None."""
        import numpy as np
        if self.trees["buildings"] is None:
            return None
        hit = self.trees["buildings"].query(pts, predicate="dwithin", distance=LANDCOVER_BLD_DEG)[0]
        city = np.bincount(hit, minlength=len(pts)) >= LANDCOVER_BLD_MIN
        cls = landcover_classes(self.trees, pts, city)
        counts = np.bincount(cls, minlength=4) / max(len(pts), 1)
        return {"city": round(float(counts[0]), 3), "water": round(float(counts[1]), 3),
                "forest": round(float(counts[2]), 3), "countryside": round(float(counts[3]), 3),
                "cls": cls.tolist(), "citymask": city.tolist(),   # par point : retirés à l'export (to_json)
                "park": protected_mask(self.trees, pts).tolist()}


# « bord d'eau » (diagnostic landcover_check « water », 27/09/2026 ; Garraf-Sitges 2 h : 19 -> 30 %, autres départs témoins
# presque inchangés) : la mer se voit de plus loin qu'une rivière ; sur un front de mer, le bord d'eau passe avant la ville
SEA_DEG = 0.0027              # ~225 m en longitude, ~300 m en latitude (rivières et lacs : SCENERY_WATER_DEG, ~100 m)
SEA_NEAR_DEG = 0.0006         # ~50 m


# grandes rivières (diagnostic landcover_check « river », 28/09/2026 ; Gràcia 2 h le long du Besòs 9 -> 13 %, départs sans
# grande rivière inchangés) : comptées jusqu'à ~150 m de leur ligne centrale ; à moins de ~60 m, bord d'eau avant ville
RIVER_DEG = 0.0016            # ~135 m en longitude, ~180 m en latitude
RIVER_NEAR_DEG = 0.0007       # ~60 m : front de rivière


def landcover_classes(trees, pts, city, river_deg=RIVER_DEG, river_near_deg=RIVER_NEAR_DEG):
    """Catégorie de chaque point (0 ville, 1 bord d'eau, 2 forêt, 3 espaces ouverts) ; city : masque du bâti dense.
    Partagée par le générateur et scripts/landcover.py (mode landcover, sans GraphHopper).
    river_deg / river_near_deg : grande rivière comptée jusqu'à river_deg de sa ligne centrale ; à moins de river_near_deg,
    bord d'eau avant ville (front de rivière). None = sans cette règle (diagnostic « actuel »)."""
    import numpy as np
    cls = np.full(len(pts), 3)
    near = lambda key, dist: np.unique(trees[key].query(pts, predicate="dwithin", distance=dist)[0])  # noqa: E731
    if trees.get("forest") is not None:
        cls[near("forest", SCENERY_FOREST_DEG)] = 2
    if trees.get("water") is not None:
        cls[near("water", SCENERY_WATER_DEG)] = 1
    if trees.get("sea") is not None:
        cls[near("sea", SEA_DEG)] = 1
    if river_deg and trees.get("river") is not None:
        cls[near("river", river_deg)] = 1
    cls[np.asarray(city, dtype=bool)] = 0
    if trees.get("sea") is not None:
        cls[near("sea", SEA_NEAR_DEG)] = 1
    if river_near_deg and trees.get("river") is not None:
        cls[near("river", river_near_deg)] = 1
    return cls


LANDCOVER_CODES = "vefo"     # une lettre par point tous les 100 m : ville, eau (bord d'eau), forêt, espaces ouverts
EXIT_CITY_RUN = 10            # sortie de ville : début du premier tronçon d'au moins 1 km (10 points) sans bâti dense


def landcover_seq(cls) -> str:
    """Catégorie de terrain de chaque point (0 ville, 1 eau, 2 forêt, 3 espaces ouverts) -> « vvvvooeeff… » (bande de
    terrain sous le profil, O-17)."""
    return "".join(LANDCOVER_CODES[int(c)] for c in cls)


def exit_city_km(cls, dist_km: float):
    """Km où l'on sort de la ville, mesuré par le bâti comme la barre de terrain : début du premier tronçon d'au moins
    1 km sans « ville » (un parc de 200 m n'est pas une sortie). 0 = départ hors de la ville ; None = reste en ville.
    Les points sont recalés sur la distance officielle (comme le profil)."""
    step = dist_km / max(len(cls), 1)
    run = 0
    for i, c in enumerate(cls):
        run = run + 1 if int(c) != 0 else 0
        if run >= EXIT_CITY_RUN:
            return round((i - run + 1) * step, 1)
    return None


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
    filters = ["nwr/landuse=forest,residential,commercial,industrial,retail,port", "nwr/natural=wood",
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
    forests, waters, protected, views, builtup, sea, rivers, industrial = [], [], [], [], [], [], [], []
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
                if props.get("natural") in ("coastline", "beach"):
                    sea.append(geom)
                if props.get("waterway") == "river":
                    rivers.append(geom)
            elif props.get("landuse") == "forest" or props.get("natural") == "wood":
                forests.append(geom)
            elif props.get("landuse") in ("residential", "commercial", "industrial", "retail", "port"):
                builtup.append(geom)                       # zones bâties : « ville » de la répartition du terrain
                if props.get("landuse") in ("industrial", "port") and geom.area * KM2_PER_DEG2 >= INDUSTRIAL_MIN_KM2:
                    industrial.append(geom)
            elif props.get("leisure") in ("park", "nature_reserve") or props.get("boundary") in (
                    "protected_area", "national_park"):
                protected.append(geom)
    return LandscapeIndex(forests, waters, protected, views, builtup, load_building_points(pbf, workdir), sea, rivers,
                          industrial)


def industrial_mask(trees, pts, step_m: float = 100.0):
    """Point par point (un point tous les step_m) : dans une zone industrielle ou portuaire (taille mini
    INDUSTRIAL_MIN_KM2), hors parc (ex. Parc Fluvial del Besòs), sur un passage d'au moins INDUSTRIAL_MIN_RUN_M.
    Partagé avec scripts/landcover.py (mode landcover)."""
    import numpy as np
    m = np.zeros(len(pts), dtype=bool)
    if trees.get("industrial") is None:
        return m
    m[np.unique(trees["industrial"].query(pts, predicate="intersects")[0])] = True
    m &= ~protected_mask(trees, pts)
    filled = m.copy()                                    # un trou d'un seul point ne coupe pas le passage
    filled[1:-1] |= m[:-2] & m[2:]
    out = np.zeros_like(m)
    i, n = 0, len(filled)
    while i < n:
        if not filled[i]:
            i += 1
            continue
        j = i
        while j < n and filled[j]:
            j += 1
        if (j - i) * step_m >= INDUSTRIAL_MIN_RUN_M:
            out[i:j] = True
        i = j
    return out


def protected_mask(trees, pts):
    """Point par point : dans (ou tout près d') un parc ou un espace protégé — même seuil que scenery.protected."""
    import numpy as np
    m = np.zeros(len(pts), dtype=bool)
    if trees.get("protected") is not None:
        m[np.unique(trees["protected"].query(pts, predicate="dwithin", distance=SCENERY_PROTECTED_DEG)[0])] = True
    return m


# ---------------------------------------------------------------- points d'intérêt le long des boucles (27/09/2026)
# Panel d'utilisateurs : points d'eau, cafés et gares de retour (« échappatoires ») ; noms des cols. Ne dépend que du
# tracé et d'OSM : calculé par le générateur ET par le mode landcover (sans GraphHopper).
POI_WATER_DEG = 0.0006        # ~50 m du tracé : fontaine d'eau potable
POI_OUT_OF_TOWN = ("w", "c")  # eau et cafés : seulement hors de la ville (Barcelone a une fontaine presque à chaque rue ;
#                               28/09/2026, les 12 points d'eau d'une boucle de Gràcia tombaient tous dans les 7 premiers km)
POI_CAFE_DEG = 0.0006         # ~50 m : café ou boulangerie, seulement hors de la ville (en ville, il y en a partout)
POI_STATION_DEG = 0.0036      # ~300 m : gare, pour rentrer en train en cas de pépin
POI_STATION_END_KM = 1.0      # gares à moins de 1 km du départ ou de l'arrivée (sur le tracé) : ignorées
POI_PASS_DEG = 0.0024         # ~200 m du sommet d'une montée : col nommé
POI_PEAK_DEG = 0.003          # ~250 m : sinon, sommet nommé
POI_RULES = {"w": (POI_WATER_DEG, 2.0, 12), "c": (POI_CAFE_DEG, 1.0, 10), "g": (POI_STATION_DEG, 2.0, 6)}   # (distance,
#                                                                               écart mini en km, nombre maxi) par type


def load_pois(pbf: Path, workdir: Path):
    """Eau potable, cafés / boulangeries, gares, cols et sommets nommés (osmium) -> {type: (STRtree, [(point, nom)])}."""
    import subprocess
    from shapely.geometry import shape
    from shapely.strtree import STRtree
    filt, out = workdir / "pois.osm.pbf", workdir / "pois.geojsonseq"
    subprocess.run(["osmium", "tags-filter", str(pbf), "n/amenity=drinking_water", "nw/amenity=fountain",
                    "nw/amenity=cafe", "nw/shop=bakery", "nw/railway=station", "n/mountain_pass=yes", "n/natural=saddle",
                    "n/natural=peak", "nw/tourism=viewpoint", "-o", str(filt), "--overwrite"], check=True, capture_output=True)
    subprocess.run(["osmium", "export", str(filt), "-f", "geojsonseq", "-o", str(out), "--overwrite"], check=True,
                   capture_output=True)
    kinds = {"w": [], "c": [], "g": [], "pass": [], "peak": [], "lieu": []}
    # lieux remarquables : scripts/remarkable_places.json (Wikidata, au moins 6 articles Wikipédia ; sonde #163 : le simple
    # « a un article Wikipédia » retenait 63 petits turons de Catalogne, 239 boucles sur 459, le Tibidabo 2 fois seulement)
    try:
        remarkable_all = json.loads((Path(__file__).parent / "remarkable_places.json").read_text(encoding="utf-8"))["places"]
        remarkable = set(remarkable_all)
    except (OSError, ValueError, KeyError):
        remarkable, remarkable_all = set(), {}
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
            pt = geom if geom.geom_type == "Point" else geom.centroid
            name = p.get("name")
            if name and p.get("wikidata") in remarkable and (p.get("natural") in ("peak", "saddle")
                                                              or p.get("tourism") == "viewpoint" or p.get("mountain_pass") == "yes"):
                kinds["lieu"].append((pt, name))           # lieu remarquable (liste Wikidata) : tirages ciblés, bonus
                LIEU_FAME[name] = remarkable_all.get(p.get("wikidata"), {}).get("sitelinks", 0)
            if p.get("amenity") == "drinking_water" or (p.get("amenity") == "fountain" and p.get("drinking_water") == "yes"):
                kinds["w"].append((pt, name))
            elif p.get("amenity") == "cafe" or p.get("shop") == "bakery":
                kinds["c"].append((pt, name))
            elif p.get("railway") == "station" and p.get("station") not in ("subway", "light_rail") and name:
                kinds["g"].append((pt, name))
            elif (p.get("mountain_pass") == "yes" or p.get("natural") == "saddle") and name:
                kinds["pass"].append((pt, name))
            elif p.get("natural") == "peak" and name:
                kinds["peak"].append((pt, name))
    if REMARKABLE_POINTS:                                    # points d'accès relus (« point ») : visés et comptés aussi
        from shapely.geometry import Point
        for e in remarkable_all.values():
            if e.get("point") and not e.get("exclude"):
                name = e.get("point_name") or e["label"]          # ex. « Coll de Pal » plutôt que le sommet voisin
                kinds["lieu"].append((Point(e["point"][0], e["point"][1]), name))
                LIEU_FAME[name] = e.get("sitelinks", 0)
    print("Points d'intérêt : " + ", ".join(f"{k} {len(v)}" for k, v in kinds.items()), flush=True)
    return {k: (STRtree([g for g, _ in v]), v) for k, v in kinds.items() if v}


def enrich_option(o: dict, pois) -> dict:
    """Ajoute à une option exportée : o["pois"] (eau, cafés hors ville, gares, avec le km de passage) et le nom du col ou
    du sommet de chaque montée (terrain.climbs[].name). Sans points d'intérêt chargés : option inchangée."""
    if not pois or len(o.get("coords") or []) < 2:
        return o
    from shapely.geometry import LineString
    line = LineString([(c[0], c[1]) for c in o["coords"]])
    L, dist = max(line.length, 1e-9), o["distance_km"]
    seq = (o.get("scenery") or {}).get("landcover_seq") or ""
    km_of = lambda pt: line.project(pt) / L * dist  # noqa: E731
    out = []
    for kind, (deg, gap, cap) in POI_RULES.items():
        if kind not in pois:
            continue
        tree, items = pois[kind]
        found = sorted(((km_of(items[i][0]), items[i]) for i in tree.query(line, predicate="dwithin", distance=deg)),
                       key=lambda x: x[0])
        kept, last, names = [], -1e9, set()
        for km, (pt, name) in found:
            if kind in POI_OUT_OF_TOWN and seq and seq[min(len(seq) - 1, int(km / dist * len(seq)))] == "v":
                continue                                  # en ville il y en a partout : sans intérêt comme échappatoire
            if km - last < gap or (kind == "g" and name in names):
                continue
            if kind == "g" and (km < POI_STATION_END_KM or km > dist - POI_STATION_END_KM):
                continue                                  # gare du départ ou de l'arrivée : pas une échappatoire
            kept.append({"t": kind, "km": round(km, 1), "lon": round(pt.x, 5), "lat": round(pt.y, 5),
                         **({"n": name} if name else {})})
            last = km
            names.add(name)
            if len(kept) >= cap:
                break
        out += kept
    o["pois"] = sorted(out, key=lambda x: x["km"])
    for c in (o.get("terrain") or {}).get("climbs") or []:        # nom du col (ou du sommet) au haut de la montée
        top = line.interpolate(min(dist, c["start_km"] + c["length_km"]) / dist * L)
        c.pop("name", None)
        for kind, deg in (("pass", POI_PASS_DEG), ("peak", POI_PEAK_DEG)):
            if kind in pois:
                tree, items = pois[kind]
                near = [items[i] for i in tree.query(top, predicate="dwithin", distance=deg)]
                if near:
                    c["name"] = min(near, key=lambda it: it[0].distance(top))[1]
                    break
    return o


def load_building_points(pbf: Path, workdir: Path, bbox: str = "") -> list:
    """Centres des bâtiments OSM (osmium) : « ville » de la répartition du terrain. bbox vide = tout l'extrait
    (province : ~1 million de bâtiments, ~60 s sur le runner GitHub)."""
    import subprocess
    from shapely.geometry import shape
    cut, filt, out = workdir / "bld_cut.osm.pbf", workdir / "bld.osm.pbf", workdir / "bld.geojsonseq"
    src = pbf
    try:
        if bbox:
            subprocess.run(["osmium", "extract", "--bbox", bbox, str(pbf), "-o", str(cut), "--overwrite"], check=True,
                           capture_output=True)
            src = cut
        subprocess.run(["osmium", "tags-filter", str(src), "w/building", "-o", str(filt), "--overwrite"], check=True,
                       capture_output=True)
        subprocess.run(["osmium", "export", str(filt), "-f", "geojsonseq", "-o", str(out), "--overwrite"], check=True,
                       capture_output=True)
    except subprocess.CalledProcessError as e:
        print(f"! bâtiments non chargés : {e.stderr.decode()[:200]}", file=sys.stderr)
        return []
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
    return pts


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


# Montées (01/10/2026, 2e sortie de Florent : « montée » de 29 m sur 7,2 km le long du Besòs ; Collserola, 7,7 km à 2,7 %,
# classée « douce » malgré 2 km à 4-6 %). Méthode des compteurs (Garmin, Strava) : une montée a au moins CLIMB_MIN_LEN_M et
# CLIMB_MIN_GRADE de moyenne, sur sa partie qui monte vraiment (découpée dans la montée brute, sans les faux plats) ;
# score = pente (%, dénivelé / longueur horizontale) × longueur parcourue (m, sur la route : hypoténuse) ; catégorie selon
# CLIMB_CATEGORIES (taille de la montée). La raideur (douce / soutenue / raide) vient du kilomètre le plus raide.
CLIMB_MIN_LEN_M = 500.0
CLIMB_MIN_GRADE = 3.0
CLIMB_MIN_SCORE = 1500.0
CLIMB_CATEGORIES = ((80000.0, "HC"), (64000.0, "1"), (32000.0, "2"), (16000.0, "3"), (8000.0, "4"), (1500.0, "nc"))
CLIMB_STEEP_WINDOW_M = 1000.0
CLIMB_SHOWN_MIN = 5              # petites montées (non classées) : seulement pour compléter jusqu'à 5 montées


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


def climb_score(gain: float, horiz: float) -> float:
    """Score de montée : pente (%) × longueur parcourue sur la route (hypoténuse, m)."""
    return 100.0 * gain / horiz * math.hypot(horiz, gain)


def climb_core(prof, ds, a, b):
    """Partie d'une montée brute [a, b] (indices du profil) au meilleur score parmi celles d'au moins CLIMB_MIN_LEN_M et
    CLIMB_MIN_GRADE de pente moyenne : (i, j, score), ou None (pas une montée)."""
    w = max(1, int(math.ceil(CLIMB_MIN_LEN_M / ds)))
    best = None
    for i in range(a, b - w + 1):
        for j in range(i + w, b + 1):
            horiz, gain = (j - i) * ds, prof[j] - prof[i]
            if gain <= 0 or 100.0 * gain / horiz < CLIMB_MIN_GRADE:
                continue
            sc = climb_score(gain, horiz)
            if best is None or sc > best[2]:
                best = (i, j, sc)
    return best if best and best[2] > CLIMB_MIN_SCORE else None


def steepest_grade(prof, ds, a, b):
    """Pente (%) du kilomètre le plus raide de [a, b] (de toute la montée si elle est plus courte)."""
    w = max(1, int(round(CLIMB_STEEP_WINDOW_M / ds)))
    if b - a <= w:
        return 100.0 * (prof[b] - prof[a]) / ((b - a) * ds)
    return max(100.0 * (prof[k + w] - prof[k]) / (w * ds) for k in range(a, b - w + 1))


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
    # montées affichées : dans chaque montée brute, la partie qui monte vraiment (meilleur score, voir CLIMB_*) ; TOUTES
    # les montées classées (catégorie 4 et plus : en 6 h, la 5e montée gardée faisait souvent plus de 80 m, des catégories 4
    # étaient perdues), complétées par les petites montées jusqu'à CLIMB_SHOWN_MIN ; ds = pas horizontal (à plat)
    climbs = []
    for a, b in runs:
        core = climb_core(prof, ds, a, b)
        if core is None:
            continue
        i, j, sc = core
        horiz, gain = (j - i) * ds, prof[j] - prof[i]
        climbs.append({"start_km": round(i * ds / 1000.0, 1), "length_km": round(horiz / 1000.0, 1),
                       "gain_m": round(gain), "avg_grade_pct": round(100.0 * gain / horiz, 1),
                       "score": round(sc), "category": next(c for th, c in CLIMB_CATEGORIES if sc > th),
                       "steepest_km_grade_pct": round(steepest_grade(prof, ds, i, j), 1)})
    n_all = len(climbs)                                  # toutes les montées (affichage « dont les N plus grosses »)
    climbs.sort(key=lambda c: -c.get("score", 100.0 * c["gain_m"]))
    ranked = [c for c in climbs if c.get("category", "nc") != "nc"]
    climbs = ranked + [c for c in climbs if c.get("category", "nc") == "nc"][:max(0, CLIMB_SHOWN_MIN - len(ranked))]
    return {"max_grade_pct": round(100.0 * max(grades), 1), "bands": {k: round(v / n, 3) for k, v in bands.items()},
            "climbs": climbs, "n_climbs": n_all, "avg_climb_grade_pct": avg_climb}


def count_uturns(coords, min_seg=8.0, angle=150.0) -> int:
    """Demi-tours : inversions de cap >= 150° (impasses, aller-retour), en ignorant les micro-segments."""
    return len(uturn_points(coords, min_seg, angle))


def uturn_points(coords, min_seg=8.0, angle=150.0) -> list:
    """Position de chaque demi-tour compté par count_uturns : (lon, lat, écart en m entre le tracé 30 m avant et 30 m
    après ; ~0 = même rue reprise en sens inverse, quelques mètres = lacet ou voie parallèle)."""
    kept = [coords[0]]
    for c in coords[1:]:
        if haversine(kept[-1][0], kept[-1][1], c[0], c[1]) >= min_seg:
            kept.append(c)
    n, i = [], 1
    while i < len(kept) - 1:
        b1 = bearing(kept[i - 1][0], kept[i - 1][1], kept[i][0], kept[i][1])
        b2 = bearing(kept[i][0], kept[i][1], kept[i + 1][0], kept[i + 1][1])
        if abs((b2 - b1 + 180) % 360 - 180) >= angle:
            n.append((kept[i][0], kept[i][1], _back_gap(kept, i)))
            i += 2
        else:
            i += 1
    return n


def _back_gap(kept, i, d=30.0) -> float:
    """Écart (m) entre le point du tracé d m avant le sommet i et celui d m après."""
    def walk(step):
        j, acc = i, 0.0
        while 0 < j < len(kept) - 1 and acc < d:
            acc += haversine(kept[j][0], kept[j][1], kept[j + step][0], kept[j + step][1])
            j += step
        return kept[j]
    a, b = walk(-1), walk(1)
    return round(haversine(a[0], a[1], b[0], b[1]), 1)


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
        return t * (1.0 + 0.40 * city_share + 0.06 * resid_share)
    return t * (1.0 + CITY_SLOWDOWN * city_share + 0.03 * resid_share) + n_signals * SIGNAL_DELAY_S


# --------------------------------------------------------------------------- client GraphHopper
GH_MEMO = True                  # mémoire des itinéraires déjà demandés, par départ (voir GraphHopper._route) ; sonde du
#                                 04/10/2026 : 10 % des requêtes réutilisées, sonde 31 -> 24 min, boucles identiques (aux
#                                 données OSM du jour près)


class GraphHopper:
    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")
        self.http = requests.Session()
        self.last_error = ""
        # mémoire des itinéraires déjà demandés (04/10/2026) : un client par départ ; les mêmes requêtes reviennent d'une
        # durée et d'une allure à l'autre (monter au Tibidabo et redescendre…). Même requête = même réponse : boucles
        # identiques, seul le temps de calcul baisse.
        self._memo: dict = {}
        self.calls = 0
        self.hits = 0

    def _route(self, body):
        import copy
        key = json.dumps(body, sort_keys=True)
        self.calls += 1
        if GH_MEMO and key in self._memo:
            self.hits += 1
            path, self.last_error = self._memo[key]
            return copy.deepcopy(path)
        try:
            r = self.http.post(f"{self.base}/route", json=body, timeout=120)
        except requests.RequestException as e:
            self.last_error = f"requête échouée : {e}"
            return None                                   # erreur réseau : pas gardée (peut réussir au prochain essai)
        if r.status_code != 200:
            self.last_error = f"HTTP {r.status_code} : {r.text[:200]}"
            path = None
        else:
            self.last_error = ""
            paths = r.json().get("paths") or []
            path = paths[0] if paths else None
        if GH_MEMO:
            self._memo[key] = (copy.deepcopy(path), self.last_error)
        return path

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

    def via(self, points, profile, pass_through=False):
        """Itinéraire passant par des points imposés (tirages ciblés), mêmes détails que round_trip. pass_through : pas de
        demi-tour aux points de passage (retouche : points pris sur une boucle, parfois du mauvais côté d'une avenue)."""
        body = {"points": points, "profile": profile, "ch.disable": True, "points_encoded": False,
                "elevation": True, "instructions": False, "details": DETAILS,
                **({"pass_through": True} if pass_through else {})}
        return self._route(body)

    def round_trip(self, lon, lat, profile, dist_m, seed, heading=None, custom_model=None):
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
        if custom_model:
            body["custom_model"] = custom_model
        return self._route(body)


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
    surface_seq: str = ""
    road_seq: str = ""
    unpaved_fallback: bool = False
    doubt_ways: dict = field(default_factory=dict)
    remarkable: list = field(default_factory=list)
    views_passed: int = 0
    spurs: list = field(default_factory=list)
    terrain: dict = field(default_factory=dict)
    u_turns: int = 0
    longest_repeat_m: float = 0.0
    repeat_cycle_share: float = 0.0
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


def per_edge(det, n) -> list:
    """Valeur d'un détail GraphHopper pour chacun des n tronçons du tracé (None si absente)."""
    arr = [None] * n
    for a, b, val in det or []:
        for i in range(max(0, a), min(b, n)):
            arr[i] = str(val).lower()
    return arr


def unpaved_edges(det, n) -> list:
    """Par tronçon : 'u' non goudronné noté, 'p' probablement non goudronné (voir PROBABLE_UNPAVED_PATHS), 'n' revêtement
    non renseigné hors ville (surlignage seulement : en ville, ce sont presque toujours des rues goudronnées), '-' sinon."""
    rc, sf, tt, ud = (per_edge(det.get(k), n) for k in ("road_class", "surface", "track_type", "urban_density"))
    out = []
    for i in range(n):
        if sf[i] in UNPAVED:
            out.append("u")
        elif sf[i] in (None, "missing") and ((rc[i] == "track" and tt[i] != "grade1")
                                              or (rc[i] in PROBABLE_UNPAVED_PATHS and ud[i] == "rural")):
            out.append("p")
        elif sf[i] in (None, "missing") and ud[i] == "rural":
            out.append("n")
        else:
            out.append("-")
    return out


# Voies OSM au revêtement incertain empruntées par les boucles retenues (28/09/2026) : liste à vérifier et corriger dans
# OSM par Florent (diagnostic précédent fait par proximité : trottoirs le long des routes comptés à tort). Clé = identifiant
# de voie OSM (détail GraphHopper osm_way_id) ; artefact du run, jamais publié.
WAYS_LOG: dict = {}
WAYS_LOCK = __import__("threading").Lock()


def doubt_ways(det, coords, cum) -> dict:
    """{id de voie OSM: [classe, mètres, lon, lat]} pour les tronçons sans revêtement noté : piste_q1 (piste de qualité 1,
    goudronnée à 87 %), piste_terre_probable, sentier_hors_ville, sentier_ville."""
    n = len(cum) - 1
    rc, sf, tt, ud, wid = (per_edge(det.get(k), n) for k in ("road_class", "surface", "track_type", "urban_density",
                                                               "osm_way_id"))
    out: dict = {}
    for i in range(n):
        if wid[i] is None or sf[i] not in (None, "missing"):
            continue
        if rc[i] == "track":
            c = "piste_q1" if tt[i] == "grade1" else "piste_terre_probable"
        elif rc[i] in PROBABLE_UNPAVED_PATHS:
            c = "sentier_hors_ville" if ud[i] == "rural" else "sentier_ville"
        else:
            continue
        e = out.setdefault(wid[i], [c, 0.0, round(coords[i][0], 5), round(coords[i][1], 5)])
        e[1] += cum[i + 1] - cum[i]
    return out


def record_ways(sid: str, loop) -> None:
    with WAYS_LOCK:
        for w, (c, m, lon, lat) in (loop.doubt_ways or {}).items():
            e = WAYS_LOG.setdefault(w, {"classe": c, "boucles": 0, "m": 0.0, "departs": set(), "lon": lon, "lat": lat})
            e["boucles"] += 1
            e["m"] += m
            e["departs"].add(sid)


def write_ways(path: str) -> int:
    import csv
    rows = sorted(WAYS_LOG.items(), key=lambda kv: (-kv[1]["boucles"], -kv[1]["m"]))
    with open(path, "w", encoding="utf-8", newline="") as fh:
        wr = csv.writer(fh)
        wr.writerow(["osm", "id", "classe", "boucles", "departs", "km_parcourus", "lat", "lon", "exemple_depart"])
        for w, e in rows:
            wr.writerow([f"https://www.openstreetmap.org/way/{w}", w, e["classe"], e["boucles"], len(e["departs"]),
                         round(e["m"] / 1000.0, 1), e["lat"], e["lon"], sorted(e["departs"])[0]])
    return len(rows)


def road_edges(det, n) -> list:
    """Par tronçon : 'm' route principale, 'c' piste cyclable ou voie verte (même définition que dedicated_cycleway),
    '-' sinon (surlignage dans l'appli)."""
    rc, ba = per_edge(det.get("road_class"), n), per_edge(det.get("bike_road_access"), n)
    return ["m" if rc[i] in ("primary", "trunk", "secondary") else
            "c" if rc[i] == "cycleway" or (rc[i] in ("footway", "path") and ba[i] in ("yes", "designated")) else "-"
            for i in range(n)]


def surface_seq(edges, cum, step: float = PROFILE_STEP_M) -> str:
    """Une lettre tous les 100 m (même pas que landcover_seq) : lettre du tronçon sous le point (unpaved_edges,
    road_edges)."""
    import bisect
    total, out = cum[-1], []
    for k in range(max(1, int(total // step)) + 1):
        i = min(len(edges) - 1, max(0, bisect.bisect_right(cum, min(total, k * step)) - 1))
        out.append(edges[i])
    return "".join(out)


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
    rc_rep = road_edges(det, len(cum) - 1)            # partie répétée sur piste cyclable (aller-retour, OUTBACK_OK)
    rep_cycle = sum(cum[i + 1] - cum[i] for i, k in enumerate(keys) if seen[k] > 1 and rc_rep[i] == "c")
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
        # pistes cyclables + voies vertes (chemin piéton ou sentier où le vélo est autorisé ou réservé, ex. Parc
        # Fluvial del Besòs) : ce que les profils loop_calm / loop_sport préfèrent depuis O-15
        "dedicated_cycleway": (sum(m for (rc, ba), m in joint_meters(det.get("road_class"), det.get("bike_road_access"),
                                                                      cum).items()
                                   if rc == "cycleway" or (rc in ("footway", "path") and ba in ("yes", "designated")))
                               / total),
        "bike_network": 1.0 - frac(net, ["missing"]) if net else 0.0,
        "main_roads": frac(rclass, ["primary", "trunk", "secondary"]),
    }
    edges = unpaved_edges(det, len(cum) - 1)
    probable = sum(cum[i + 1] - cum[i] for i, e in enumerate(edges) if e == "p") / total
    shares["unpaved"] = frac(surf, list(UNPAVED)) + probable          # noté + probable : seuil MAX_UNPAVED et note
    # « ville » pour la note : seulement là où roulent des voitures (30/09/2026, Gràcia : le front de mer et le Parc Fluvial
    # del Besòs comptaient comme ville à cause des immeubles ; ce qui gêne en ville, c'est la circulation). La durée garde
    # le ralentissement sur toute la ville (freinages, piétons, croisements aussi sur les pistes cyclables).
    n_e = len(cum) - 1
    rc_e, ba_e, ud_e = (per_edge(det.get(k), n_e) for k in ("road_class", "bike_road_access", "urban_density"))
    car = [not (rc_e[i] == "cycleway" or (rc_e[i] in ("footway", "path", "pedestrian") and ba_e[i] in ("yes", "designated")))
           for i in range(n_e)]
    shares["urban_car"] = {u: sum(cum[i + 1] - cum[i] for i in range(n_e) if car[i] and ud_e[i] == u) / total
                           for u in ("city", "residential")}
    # grandes routes par densité urbaine (diagnostic O-12 ; n'entre pas dans score() ni dans les fichiers publiés)
    joint = joint_meters(det.get("road_class"), det.get("urban_density"), cum)
    shares["main_roads_by_urban"] = {
        u: sum(m for (rc, ud), m in joint.items() if rc in ("primary", "trunk", "secondary") and ud == u) / total
        for u in ("city", "residential", "rural")}
    watts = level_watts(level, duration_h)
    ds, prof = elevation_profile(coords, cum)
    ascend, descend = gain_loss(prof)
    n_signals = SIGNALS.count_along(coords, cum) if SIGNALS is not None else None
    n_stops = STOPS.count_along(coords, cum) if STOPS is not None else None
    surface_mix = {
        "asphalt": frac(surf, list(ASPHALT)), "cobbles": frac(surf, list(COBBLES)),
        "unpaved": frac(surf, list(UNPAVED)), "unpaved_probable": probable,
    }
    surface_mix["unknown"] = max(0.0, 1.0 - sum(surface_mix.values()))
    terrain = slope_stats(ds, prof)
    u_turns = true_uturns(coords) if UTURN_LACETS_OK else count_uturns(coords)
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
        repeat_cycle_share=(rep_cycle / repeated) if repeated > 0 else 0.0,
        scenery=scenery,
    )
    loop.cells = {(round(c[0] / 0.006), round(c[1] / 0.005)) for c in coords}   # cellules ~500 m
    loop.remarkable = remarkable_passed(coords, cum)
    loop.views_passed = views_passed(coords, cum)
    loop.spurs = unjustified_spurs(coords) if SPUR_FIX else []
    loop.surface_seq = surface_seq(edges, cum)
    loop.road_seq = surface_seq(road_edges(det, len(cum) - 1), cum)
    loop.doubt_ways = doubt_ways(det, coords, cum)
    loop.wind_bins = {k: [round(x, 2) for x in v] for k, v in bins.items()}
    loop.score = score(loop)
    return loop


def score_parts(l: Loop) -> tuple[dict, dict]:
    """Sous-scores (0 à 1) et poids utilisés par score() : exposés pour les diagnostics (nature_check.py)."""
    s = l.shares
    parts = {
        "calm": 1.0 - (s.get("urban_car", s["urban"])["city"] + 0.4 * s.get("urban_car", s["urban"])["residential"]),
        "axes": 1.0 - min(1.0, 2.0 * s["main_roads"]),
        "infra": min(1.0, 2.0 * s["dedicated_cycleway"]),
        "flow": 1.0 - min(1.0, FLOW_OVERLAP_FACTOR * l.overlap + 0.15 * l.u_turns),
    }
    weights = dict(WEIGHTS)
    if l.signals is not None:
        per_km = l.signals / max(l.distance_m / 1000.0, 0.1)
        parts["lights"] = 1.0 - min(2.0, per_km / LIGHTS_PER_KM_ZERO_SCORE)   # jusqu'à −1 à 5 feux/km (28/09/2026 :
        # au-delà de 2,5 feux/km, toutes les boucles avaient la même note ; en dessous, notes inchangées)
    else:
        weights.pop("lights")
    if l.scenery is not None:
        parts["scenery"] = l.scenery["score"] / 100.0
    else:
        weights.pop("scenery")
    return parts, weights


def score(l: Loop) -> float:
    return score_from(l, RELIEF_WEIGHTS.get(l.level))


def score_from(l: Loop, relief_weight: float | None = None, lights_weight: float | None = None,
               ind_penalty: float | None = None, ind_max: float | None = None) -> float:
    """Note de la boucle ; relief_weight : poids de la partie « relief » (None ou 0 = pas de bonus). Séparée de score()
    pour que la sonde « relief » compare plusieurs poids sans modifier le réglage global."""
    parts, weights = score_parts(l)
    if lights_weight is not None and "lights" in weights:   # sonde « pénalités » (05/10/2026) : poids des feux essayé
        weights["lights"] = lights_weight
    if relief_weight:
        parts["relief"] = min(1.0, l.dplus_per_km / RELIEF_FULL_M_PER_KM)
        weights["relief"] = relief_weight
    total = sum(weights[k] * parts[k] for k in weights) / sum(weights.values())
    # terre : dès le premier km, 3 points par km (Gràcia 2 h, 30/09/2026 : 1,9 km de piste en terre évitable ne coûtaient
    # que 4 points, la pénalité ne comptant qu'au-delà de 3 % du parcours) ; la part au-delà de 3 % reste pénalisée
    dirt_km = l.shares["unpaved"] * l.distance_m / 1000.0
    total -= min(0.3, max(UNPAVED_PENALTY_PER_KM * dirt_km, max(0.0, l.shares["unpaved"] - 0.03) * 2.0))
    total += min(REMARKABLE_MAX_BONUS, REMARKABLE_BONUS * len(l.remarkable))   # Tibidabo, belvédères connus…
    total += min(VIEW_MAX_BONUS, VIEW_BONUS * l.views_passed)                 # tout belvédère devant lequel on passe
    if l.spurs:                                           # éperons injustifiés (SPUR_FIX)
        total -= min(SPUR_MAX_PENALTY, sum(SPUR_PENALTY + SPUR_PENALTY_PER_100M * s["m"] / 100.0 for s in l.spurs))
    if l.scenery is not None:                             # zones industrielles et portuaires (entrepôts, camions)
        total -= min(INDUSTRIAL_MAX_PENALTY if ind_max is None else ind_max,
                     (INDUSTRIAL_PENALTY if ind_penalty is None else ind_penalty) * l.scenery.get("industrial", 0.0))
    return round(100 * max(0.0, total), 1)


# --------------------------------------------------------------------------- génération
def overlap_ok(l) -> bool:
    """Part répétée acceptable : sous MAX_OVERLAP, ou aller-retour sur piste cyclable (OUTBACK_OK)."""
    return l.overlap <= MAX_OVERLAP or (OUTBACK_OK and l.overlap <= OUTBACK_MAX_OVERLAP
                                        and l.repeat_cycle_share >= OUTBACK_MIN_CYCLE)


def outback_candidates(gh, st, level, profile, duration_h, log, detail=None) -> list:
    """Allers-retours au bord de l'eau (OUTBACK_DRAWS) : on vise un point d'une rivière ou de la mer à la bonne distance,
    dans deux directions bien distinctes, et GraphHopper revient par le chemin qu'il préfère (souvent le même : la piste)."""
    if LANDSCAPE is None:
        return []
    import shapely
    lon, lat = st["lon"], st["lat"]
    target_s = duration_h * 3600.0
    flat_ms = speed_from_power(level_watts(level, duration_h), 0.0) * REAL_WORLD_FACTOR
    loop_km = 0.8 * flat_ms * target_s / 1000.0
    pt = shapely.Point(lon, lat)
    found = []
    for cat, key in (("riviere", "river"), ("mer", "sea")):
        tree = LANDSCAPE.trees.get(key)
        if tree is None:
            continue
        if OUTBACK_NEAR_KM is not None and nearest_attraction(cat, lon, lat, OUTBACK_NEAR_KM) is None:
            continue                                        # eau trop loin : pas d'aller-retour (économie de calcul)
        samples = []                                        # points de l'eau entre 15 % et 50 % de la longueur de boucle
        for i in tree.query(pt.buffer(0.5 * loop_km / 90.0)):
            gm = tree.geometries[int(i)]
            line = gm.boundary if gm.geom_type in ("Polygon", "MultiPolygon") else gm
            parts = list(getattr(line, "geoms", [line]))      # MultiLineString : interpolate ne marche que par morceau
            for part in parts:
                n = max(2, int(part.length / 0.004))
                for k in range(n + 1):
                    q = part.interpolate(k / n, normalized=True)
                    d = haversine(lon, lat, q.x, q.y) / 1000.0
                    if 0.15 * loop_km <= d <= 0.5 * loop_km:
                        samples.append((q.x, q.y, d, bearing(lon, lat, q.x, q.y)))
        if detail is not None:
            detail.append(f"  {cat} : {len(samples)} point(s) au bord de l'eau entre {0.15 * loop_km:.1f} et "
                          f"{0.5 * loop_km:.1f} km (boucle visée ~{loop_km:.1f} km)")
        if not samples:
            continue
        dirs = []                                           # deux directions à plus de 60° l'une de l'autre
        for s in sorted(samples, key=lambda s: abs(s[2] - 0.3 * loop_km)):
            if all(abs((s[3] - b + 180) % 360 - 180) > 60 for b in dirs):
                dirs.append(s[3])
            if len(dirs) >= 2:
                break
        for b0 in dirs:
            side = [s for s in samples if abs((s[3] - b0 + 180) % 360 - 180) <= 30]
            want, loop = 0.3 * loop_km, None
            for _ in range(3):                              # ajustement de la distance visée sur la durée
                s = min(side, key=lambda s: abs(s[2] - want))
                # par l'eau à l'aller ET au retour (diagnostic Sant Adrià : sans ça, le calcul revenait par les rues) :
                # entrée au plus près, un point au bord de l'eau à mi-chemin, le point visé, puis les mêmes en sens inverse
                e = nearest_attraction(cat, lon, lat, 0.5 * loop_km)
                m = nearest_attraction(cat, (e[0] + s[0]) / 2.0, (e[1] + s[1]) / 2.0, 0.5 * loop_km) if e else None
                go = [list(p[:2]) for p in (e, m) if p]
                path = gh.via([[lon, lat]] + go + [[s[0], s[1]]] + go[::-1] + [[lon, lat]], profile)
                loop = analyse(path, level, profile, duration_h, 950, None) if path else None
                if loop is None:
                    break
                ratio = loop.time_s / target_s
                if abs(ratio - 1.0) <= 0.05:
                    break
                want = s[2] / ratio
            ok = (loop is not None and abs(loop.time_s / target_s - 1.0) <= TIME_TOLERANCE and overlap_ok(loop)
                  and loop.u_turns <= MAX_UTURNS and loop.shares["unpaved"] <= MAX_UNPAVED)
            if detail is not None:
                detail.append(f"  aller-retour {cat} cap {b0:.0f}° -> " + ("pas de boucle" if loop is None else
                              f"note {loop.score:.1f}, {loop.distance_m / 1000:.1f} km, {loop.time_s / 60:.0f} min, répété "
                              f"{loop.overlap:.0%} (piste {loop.repeat_cycle_share:.0%}) : " + ("GARDÉ" if ok else "rejeté")))
            if ok:
                found.append(loop)
    log(f"    allers-retours au bord de l'eau : {len(found)} valide(s)")
    return found


def fit_and_sample(gh: GraphHopper, start, level: str, profile: str, duration_h: float, candidates, log,
                   fallback: list | None = None):
    """Retourne (candidats valides, compteur des raisons de rejet). fallback : reçoit les candidats rejetés pour la
    seule terre mais sous les limites du repli (FALLBACK_UNPAVED_*)."""
    lon, lat = start["lon"], start["lat"]
    watts = level_watts(level, duration_h)
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
        if not overlap_ok(cand):
            rejects["tronçons répétés"] += 1
            continue
        if cand.u_turns > MAX_UTURNS:
            rejects["demi-tours"] += 1
            continue
        if cand.shares["unpaved"] > MAX_UNPAVED:
            rejects["non goudronné"] += 1
            if (fallback is not None and cand.shares["unpaved"] <= FALLBACK_UNPAVED_SHARE
                    and cand.shares["unpaved"] * cand.distance_m / 1000.0 <= FALLBACK_UNPAVED_KM):
                cand.unpaved_fallback = True
                fallback.append(cand)
            continue
        pool.append(cand)
    return pool, rejects


class _ClimbGH:
    """GraphHopper vu par fit_and_sample, avec le modèle « montée » ajouté à chaque requête."""

    def __init__(self, gh):
        self.gh = gh

    @property
    def last_error(self):
        return self.gh.last_error

    def round_trip(self, lon, lat, profile, dist_m, seed, heading=None):
        return self.gh.round_trip(lon, lat, profile, dist_m, seed, heading, custom_model=CLIMB_MODEL)


def destination(lon, lat, bearing_deg, km):
    b = math.radians(bearing_deg)
    return (lon + km * math.sin(b) / (111.32 * math.cos(math.radians(lat))), lat + km * math.cos(b) / 110.54)


def remarkable_passed(coords, cum):
    """Lieux remarquables (sommets, belvédères avec un article Wikipédia) à moins de REMARKABLE_NEAR_DEG du tracé :
    [{"n": nom, "km": km de passage}] (une fois chacun)."""
    if not POIS or "lieu" not in POIS or len(coords) < 2:
        return []
    import numpy as np
    import shapely
    xy = sample_points(coords, cum, 100.0)
    tree, items = POIS["lieu"]
    hit = tree.query(shapely.points(np.array(xy)), predicate="dwithin", distance=REMARKABLE_NEAR_DEG)
    seen, out = set(), []
    for p_i, l_i in sorted(zip(hit[0].tolist(), hit[1].tolist())):
        name = items[l_i][1]
        if name not in seen:
            seen.add(name)
            out.append({"n": name, "km": round(p_i * 100.0 / 1000.0 * (cum[-1] / max(cum[-1], 1.0)), 1)})
    return out


def views_passed(coords, cum) -> int:
    """Belvédères OSM (tourism=viewpoint, connus ou non) à moins de VIEW_NEAR_DEG du tracé, hors lieux remarquables
    (déjà comptés) ; deux points de vue à moins de ~100 m l'un de l'autre comptent pour un."""
    if LANDSCAPE is None or LANDSCAPE.trees.get("view") is None or len(coords) < 2:
        return 0
    import numpy as np
    import shapely
    pts = shapely.points(np.array(sample_points(coords, cum, 100.0)))
    tree = LANDSCAPE.trees["view"]
    idx = np.unique(tree.query(pts, predicate="dwithin", distance=VIEW_NEAR_DEG)[1])
    kept = []
    rem = POIS["lieu"][0] if POIS and "lieu" in POIS else None
    for i in idx.tolist():
        v = tree.geometries[i]
        if rem is not None and len(rem.query(v, predicate="dwithin", distance=VIEW_NEAR_DEG)):
            continue                                      # lieu remarquable : bonus REMARKABLE seulement
        if all(v.distance(k) > VIEW_NEAR_DEG for k in kept):
            kept.append(v)
    return len(kept)


def attraction_points(lon, lat, max_km):
    """Point le plus proche de chaque lieu attrayant (mer, grande rivière, grand espace vert) entre TARGET_MIN_KM et
    max_km : {catégorie: (lon, lat, km)}."""
    if LANDSCAPE is None:
        return {}
    import shapely
    from shapely.ops import nearest_points
    pt = shapely.Point(lon, lat)
    out = {}
    if POIS and "lieu" in POIS:                                # lieu remarquable (sommet, belvédère) : le point lui-même
        tree, items = POIS["lieu"]
        near = [items[int(i)] for i in tree.query(pt.buffer(max_km / 90.0))]
        near = [(q.x, q.y, haversine(lon, lat, q.x, q.y) / 1000.0, n) for q, n in near]
        near = [x for x in near if TARGET_MIN_KM <= x[2] <= max_km]
        if near and REMARKABLE_FAME:                           # les 2 plus célèbres (à célébrité égale, le plus proche)
            near.sort(key=lambda x: (-LIEU_FAME.get(x[3], 0), x[2]))
            out["lieu"] = near[0][:3]
            if len(near) > 1:
                out["lieu2"] = near[1][:3]
        elif near:
            out["lieu"] = min(near, key=lambda x: x[2])[:3]
    for cat, keys in (("mer", ("sea",)), ("riviere", ("river",)), ("vert", ("forest", "protected"))):
        best = None
        for key in keys:
            tree = LANDSCAPE.trees.get(key)
            if tree is None:
                continue
            for i in tree.query(pt.buffer(max_km / 90.0)):
                gm = tree.geometries[int(i)]
                if cat == "vert" and (gm.geom_type not in ("Polygon", "MultiPolygon") or gm.area < TARGET_GREEN_MIN_DEG2):
                    continue
                q = pt if (cat == "vert" and gm.contains(pt)) else nearest_points(gm.boundary if cat == "vert" else gm, pt)[0]
                km = haversine(lon, lat, q.x, q.y) / 1000.0
                if TARGET_MIN_KM <= km <= max_km and (best is None or km < best[2]):
                    best = (q.x, q.y, km)
        if best:
            out[cat] = best
    return out


def nearest_attraction(cat: str, lon: float, lat: float, max_km: float):
    """Point le plus proche d'un lieu attrayant d'une catégorie (riviere, mer, vert, lieu) : (lon, lat, km) ou None."""
    import shapely
    from shapely.ops import nearest_points
    pt = shapely.Point(lon, lat)
    if cat == "lieu":
        if not POIS or "lieu" not in POIS:
            return None
        tree, items = POIS["lieu"]
        near = [(q.x, q.y, haversine(lon, lat, q.x, q.y) / 1000.0) for q, _ in
                (items[int(i)] for i in tree.query(pt.buffer(max_km / 90.0)))]
        near = [x for x in near if x[2] <= max_km]
        return min(near, key=lambda x: x[2]) if near else None
    if LANDSCAPE is None:
        return None
    best = None
    for key in {"mer": ("sea",), "riviere": ("river",), "vert": ("forest", "protected")}[cat]:
        tree = LANDSCAPE.trees.get(key)
        if tree is None:
            continue
        for i in tree.query(pt.buffer(max_km / 90.0)):
            gm = tree.geometries[int(i)]
            if cat == "vert" and (gm.geom_type not in ("Polygon", "MultiPolygon") or gm.area < TARGET_GREEN_MIN_DEG2):
                continue
            q = nearest_points(gm.boundary if cat == "vert" else gm, pt)[0]
            km = haversine(lon, lat, q.x, q.y) / 1000.0
            if km <= max_km and (best is None or km < best[2]):
                best = (q.x, q.y, km)
    return best


def true_uturns(coords) -> int:
    """Demi-tours qui reprennent la même rue à l'envers (voir UTURN_SAME_STREET_M) : les lacets ne comptent pas."""
    return sum(1 for u in uturn_points(coords) if u[2] < UTURN_SAME_STREET_M)


def loop_anchors(l, with_idx=False):
    """Points de passage d'une boucle : ses points à RETOUCH_ANCHORS de la distance, décalés de RETOUCH_ANCHOR_SHIFT_M
    plus loin s'ils tombent à moins de 150 m d'un vrai demi-tour de la boucle (impasse : le calcul y referait demi-tour)."""
    c = l.coords
    cum = [0.0]
    for a, b in zip(c, c[1:]):
        cum.append(cum[-1] + haversine(a[0], a[1], b[0], b[1]))
    bad = [u for u in uturn_points(c) if u[2] < UTURN_SAME_STREET_M]
    out, idx, j = [], [], 0
    for f in RETOUCH_ANCHORS:
        while j < len(cum) - 1 and cum[j] < f * cum[-1]:
            j += 1
        k = j
        if any(haversine(c[k][0], c[k][1], u[0], u[1]) < 150.0 for u in bad):
            while k < len(cum) - 1 and cum[k] < cum[j] + RETOUCH_ANCHOR_SHIFT_M:
                k += 1
        out.append([c[k][0], c[k][1]])
        idx.append(k)
    return (out, idx) if with_idx else out


def leg_lights_per_km(l, idx) -> list:
    """Feux par km de chaque tronçon de la boucle entre ses points de passage (idx : indices des points de passage)."""
    c, cuts, out = l.coords, [0] + list(idx) + [len(l.coords) - 1], []
    for a, b in zip(cuts, cuts[1:]):
        sub = c[a:b + 1]
        cum = [0.0]
        for p, q in zip(sub, sub[1:]):
            cum.append(cum[-1] + haversine(p[0], p[1], q[0], q[1]))
        n = SIGNALS.count_along(sub, cum) if SIGNALS is not None and len(sub) > 1 else 0
        out.append(n / max(cum[-1] / 1000.0, 0.3))
    return out


def retouch_candidates(gh, st, level, duration_h, pool, log, fallback=None, detail=None, stats=None) -> list:
    """Variantes des meilleures boucles du pool (voir RETOUCH) : seulement celles qui ont une meilleure note. detail :
    liste qui reçoit une ligne par essai (diagnostic)."""
    import shapely
    if not pool:
        return []
    bases = []
    for l in sorted(pool, key=lambda x: x.score, reverse=True):
        if all(similarity(l, b) < 0.6 for b in bases):
            bases.append(l)
        if len(bases) >= RETOUCH_TOP:
            break
    target_s = duration_h * 3600.0
    home = [st["lon"], st["lat"]]
    found, n_try = [], 0
    for bi, base in enumerate(bases):
        line = shapely.LineString([p[:2] for p in base.coords])
        on_route = lambda p: line.distance(shapely.Point(p[0], p[1])) < RETOUCH_ON_ROUTE_DEG  # noqa: E731
        reach = max(RETOUCH_REACH_MIN_KM, RETOUCH_REACH * base.distance_m / 1000.0)
        pts, idx = loop_anchors(base, with_idx=True)
        anchors = [home] + pts + [home]
        legbad = leg_lights_per_km(base, idx) if RETOUCH_WORST_LEG_FIRST else [0.0] * (len(anchors) - 1)
        trials = []
        for i in range(len(anchors) - 1):
            P, Q = anchors[i], anchors[i + 1]
            last = i + 1 == len(anchors) - 1
            # essais qui sautent le point suivant d'abord (priorité 0) : un détour ajouté sans rien retirer dépasse
            # souvent la durée (diagnostic la Plata, 02/10/2026)
            def add(desc, mid, kind):
                trials.append((1, f"{desc} {i}-{i + 1}", anchors[:i + 1] + mid + anchors[i + 1:], kind + "/garde",
                               legbad[i]))
                if not last:
                    trials.append((0, f"{desc} {i}-{i + 2} (saute {i + 1})", anchors[:i + 1] + mid + anchors[i + 2:],
                                   kind + "/saute", max(legbad[i], legbad[i + 1])))
            for cat in ("riviere", "mer"):                    # suivre l'eau entre P et Q (puis Q, ou directement la suite)
                e = nearest_attraction(cat, P[0], P[1], reach)
                if not e:
                    continue
                if not on_route(e):                           # entrée seule : on rejoint ensuite librement la suite (le
                    add(f"{cat} entrée seule {e[1]:.4f},{e[0]:.4f}", [list(e[:2])], f"{cat}/seule")   # (le long de l'eau
                    # si c'est le plus direct)
                Q2 = anchors[i + 2] if not last else Q
                exits = []                                    # sorties : près de Q, près du point d'après, à mi-chemin
                for tag, R in (("Q", Q), ("Q2", Q2), ("mi-Q2", [(e[0] + Q2[0]) / 2.0, (e[1] + Q2[1]) / 2.0]),
                               ("mi-Q", [(e[0] + Q[0]) / 2.0, (e[1] + Q[1]) / 2.0])):
                    x = nearest_attraction(cat, R[0], R[1], reach)
                    if (x and haversine(e[0], e[1], x[0], x[1]) >= RETOUCH_MIN_RUN_KM * 1000.0
                            and not (on_route(e) and on_route(x))
                            and all(haversine(x[0], x[1], y[0], y[1]) > 500.0 for _, y in exits)):
                        exits.append((tag, x))
                for tag, x in exits:
                    add(f"{cat} entrée {e[1]:.4f},{e[0]:.4f} sortie {x[1]:.4f},{x[0]:.4f}", [list(e[:2]), list(x[:2])],
                        f"{cat}/sortie-{tag}")
            M = [(P[0] + Q[0]) / 2.0, (P[1] + Q[1]) / 2.0]
            for cat in ("vert", "lieu"):                      # passer par un espace vert ou un lieu remarquable proche
                p = nearest_attraction(cat, M[0], M[1], reach)
                if p and not on_route(p):
                    add(f"{cat} {p[1]:.4f},{p[0]:.4f}", [list(p[:2])], cat)
        rank = {k: n for n, k in enumerate(RETOUCH_KIND_ORDER)}   # plus rentables d'abord ; tri stable : ordre des tronçons
        trials = [t[1:4] for t in sorted(trials, key=lambda t: ((-round(t[4]) if RETOUCH_WORST_LEG_FIRST else 0),
                                                                 rank.get(t[3], len(rank))))]
        if RETOUCH_FAMILY_CAP:                               # chaque famille a sa chance (la Plata sportif, 03/10/2026 :
            fam, first, rest = {}, [], []                    # 7 essais « mer » sur 10, l'espace vert du Besòs jamais essayé)
            for t in trials:
                f = t[2].split("/")[0]
                fam[f] = fam.get(f, 0) + 1
                (first if fam[f] <= RETOUCH_FAMILY_CAP else rest).append(t)
            trials = first + rest
        if detail is not None:
            detail.append(f"boucle de départ {bi + 1} : note {base.score:.1f}, {base.distance_m / 1000:.1f} km, "
                          f"{base.time_s / 60:.0f} min ; points de passage (lat,lon) "
                          + " ; ".join(f"{i}: {a[1]:.4f},{a[0]:.4f}" for i, a in enumerate(anchors[1:-1], start=1))
                          + f" ; {len(trials)} essai(s) possibles, {min(len(trials), RETOUCH_MAX_TRIALS)} faits"
                          + (" ; feux/km par tronçon " + " ".join(f"{x:.1f}" for x in legbad) if RETOUCH_WORST_LEG_FIRST else ""))
        def poly_km(pts):
            return sum(haversine(a[0], a[1], b[0], b[1]) for a, b in zip(pts, pts[1:])) / 1000.0
        base_poly, seen, kept = max(poly_km(anchors), 0.1), set(), []
        for desc, way, kind in trials:
            key = tuple((round(p[0], 4), round(p[1], 4)) for p in way)
            if RETOUCH_DEDUPE and key in seen:
                continue
            seen.add(key)
            pred = base.time_s / target_s * poly_km(way) / base_poly       # durée prévue / durée visée
            if RETOUCH_PREFILTER is not None and abs(pred - 1.0) > RETOUCH_PREFILTER:
                continue
            kept.append((desc, way, kind, pred))
        for k, (desc, way, kind, pred) in enumerate(kept[:RETOUCH_MAX_TRIALS]):
            n_try += 1
            path = gh.via(way, base.profile, pass_through=True)
            loop = analyse(path, level, base.profile, duration_h, 1000 + 100 * bi + k, None) if path else None
            why = None
            if loop is None:
                why = f"pas d'itinéraire ({gh.last_error})" if path is None else "boucle invalide"
            elif abs(loop.time_s / target_s - 1.0) > TIME_TOLERANCE:
                why = "durée"
            elif not overlap_ok(loop):
                why = f"tronçons répétés ({loop.overlap:.0%})"
            elif loop.u_turns > MAX_UTURNS:
                why = f"demi-tours ({loop.u_turns})"
            elif loop.shares["unpaved"] > MAX_UNPAVED:
                why = "terre"
            elif loop.score <= base.score:
                why = "note plus basse"
            else:
                found.append(loop)
            if stats is not None:                            # diagnostic : quels essais rapportent
                stats.append([kind, bi, k, why or "gardée", None if loop is None else round(loop.score - base.score, 1),
                              round(pred, 3), None if loop is None else round(loop.time_s / target_s, 3)])
            if detail is not None:
                detail.append(f"  {desc} [durée prévue {pred:.2f}] -> " + ("" if loop is None else
                              f"note {loop.score:.1f}, {loop.distance_m / 1000:.1f} km, {loop.time_s / 60:.0f} min, eau "
                              f"{((loop.scenery or {}).get('landcover') or {}).get('water', 0):.0%}, industriel "
                              f"{(loop.scenery or {}).get('industrial', 0):.0%} : ") + (why or "GARDÉE")
                              + ("" if loop is None or not loop.u_turns else " ; demi-tours (lat,lon,écart m) "
                                 + " ".join(f"{u[1]:.5f},{u[0]:.5f},{u[2]:g}" for u in uturn_points(loop.coords))))
    log(f"    retouche : {len(found)} variante(s) meilleure(s) sur {n_try} essai(s)")
    return found


def target_candidates(gh, st, level, profile, duration_h, log, fallback=None):
    """Boucles par les lieux attrayants proches : triangle vers un lieu (décalé de ±TARGET_OFFSET_DEG), ou enchaînement
    de deux lieux ; taille ajustée sur la durée (jamais plus près que le lieu lui-même). Mêmes filtres que fit_and_sample."""
    lon, lat = st["lon"], st["lat"]
    target_s = duration_h * 3600.0
    flat_ms = speed_from_power(level_watts(level, duration_h), 0.0) * REAL_WORLD_FACTOR
    loop_km = 0.8 * flat_ms * target_s / 1000.0
    pts = attraction_points(lon, lat, TARGET_MAX_SHARE * loop_km)
    plans = []                                               # liste de [(bearing, km)] : points à viser, dans l'ordre
    cats = sorted(pts, key=lambda c: (c != "lieu", pts[c][2]))   # lieu remarquable d'abord, puis le plus proche
    for i in range(len(cats)):                               # deux lieux enchaînés (le plus proche d'abord) : prioritaires
        for j in range(i + 1, len(cats)):
            a, b = pts[cats[i]], pts[cats[j]]
            plans.append((cats[i] + "+" + cats[j], [(bearing(lon, lat, a[0], a[1]), a[2]),
                                                    (bearing(lon, lat, b[0], b[1]), b[2])]))
    if REMARKABLE_FAME:                                      # monter au lieu et redescendre (Sarrià 1 h : le Tibidabo par
        for cat in [c for c in cats if c.startswith("lieu")][::-1]:   # une route, retour par une autre : 51 contre 35)
            x, y, km = pts[cat]
            plans.insert(0, (cat + "-ar", [(bearing(lon, lat, x, y), km)]))
    for off in (TARGET_OFFSET_DEG, -TARGET_OFFSET_DEG):       # puis un lieu seul, triangle décalé d'un côté puis de l'autre
        for cat in cats:
            x, y, km = pts[cat]
            th = bearing(lon, lat, x, y)
            plans.append((cat, [(th, km), (th + off, km)]))
    plans = plans[:TARGET_MAX_PLANS + (2 if "lieu" in pts else 0) + (2 if "lieu2" in pts else 0)]   # ~2,5 requêtes / tirage
    found, n_ok = [], 0
    aims = {(bearing(lon, lat, pts[c][0], pts[c][1]), pts[c][2]): list(pts[c][:2]) for c in pts if c.startswith("lieu")}
    for k, (name, plan) in enumerate(plans):
        s, loop = max(1.0, loop_km / 4.1 / max(p[1] for p in plan)), None
        for _ in range(3):                                   # ajustement de la taille sur la durée visée
            way = [[lon, lat]] + [aims[(b, d)] if (b, d) in aims else list(destination(lon, lat, b, d * s))
                                  for b, d in plan] + [[lon, lat]]    # les lieux remarquables sont visés exactement
            path = gh.via(way, profile)
            loop = analyse(path, level, profile, duration_h, 900 + k, None) if path else None
            if loop is None:
                break
            ratio = loop.time_s / target_s
            if abs(ratio - 1.0) <= 0.05 or (s <= 1.0 and ratio > 1.0) or all(q in aims for q in plan):
                break                                        # (tous les points visés exactement : rien à ajuster)
            s = max(1.0, s / ratio)
        if loop is None or abs(loop.time_s / target_s - 1.0) > TIME_TOLERANCE:
            continue
        if not overlap_ok(loop) or loop.u_turns > MAX_UTURNS:
            continue
        if loop.shares["unpaved"] > MAX_UNPAVED:
            if (fallback is not None and loop.shares["unpaved"] <= FALLBACK_UNPAVED_SHARE
                    and loop.shares["unpaved"] * loop.distance_m / 1000.0 <= FALLBACK_UNPAVED_KM):
                loop.unpaved_fallback = True
                fallback.append(loop)
            continue
        found.append(loop)
        n_ok += 1
    if plans:
        log(f"    tirages ciblés ({', '.join(f'{c} {v[2]:.1f} km' for c, v in pts.items())}) : {n_ok}/{len(plans)} valides")
    return found


class _SoftGH:
    """GraphHopper vu par fit_and_sample, avec le profil « souple » (sans évitement fort de la terre) : repli v10."""

    def __init__(self, gh):
        self.gh = gh

    @property
    def last_error(self):
        return self.gh.last_error

    def round_trip(self, lon, lat, profile, dist_m, seed, heading=None):
        return self.gh.round_trip(lon, lat, profile + "_souple", dist_m, seed, heading)


def route_waypoints(coords, step_m=ROUTE_WAYPOINT_M, skip=()) -> list:
    """Points de passage d'une boucle existante, tous les step_m m, sans ceux des intervalles d'indices skip (éperons) ;
    le début et la fin de chaque intervalle sauté sont gardés."""
    out, acc, cuts = [[coords[0][0], coords[0][1]]], 0.0, set()
    for a, b in skip:
        cuts.add(a)
        cuts.add(b)
    inside = lambda i: any(a < i < b for a, b in skip)  # noqa: E731
    for i in range(1, len(coords) - 1):
        acc += haversine(coords[i - 1][0], coords[i - 1][1], coords[i][0], coords[i][1])
        if inside(i):
            continue
        if i in cuts or acc >= step_m:
            out.append([coords[i][0], coords[i][1]])
            acc = 0.0
    out.append([coords[-1][0], coords[-1][1]])
    return out


def spur_list(coords) -> list:
    """Éperons : demi-tour (cap inversé >= 150°) suivi d'un retour dans le couloir SPUR_CORRIDOR_M de l'aller.
    [{"a": indice d'entrée, "t": indice du demi-tour, "b": indice de sortie, "m": longueur de l'aller}]."""
    idx = [0]
    for i in range(1, len(coords)):
        if haversine(coords[idx[-1]][0], coords[idx[-1]][1], coords[i][0], coords[i][1]) >= 8.0:
            idx.append(i)
    if len(idx) < 3:
        return []
    k_ = [coords[i] for i in idx]
    cum = [0.0]
    for a, b in zip(k_, k_[1:]):
        cum.append(cum[-1] + haversine(a[0], a[1], b[0], b[1]))
    near = lambda p, q: haversine(p[0], p[1], q[0], q[1]) <= SPUR_CORRIDOR_M  # noqa: E731
    out, i = [], 1
    while i < len(k_) - 1:
        d = abs((bearing(k_[i][0], k_[i][1], k_[i + 1][0], k_[i + 1][1]) -
                 bearing(k_[i - 1][0], k_[i - 1][1], k_[i][0], k_[i][1]) + 180) % 360 - 180)
        if d < 150:
            i += 1
            continue
        a, b = i - 1, i + 1
        while a > 0 and b < len(k_) - 1 and (near(k_[a - 1], k_[b + 1]) or
                                              any(near(k_[a - 1], k_[x]) for x in range(b, min(len(k_), b + 4)))):
            a -= 1
            b += 1
        out.append({"a": idx[a], "t": idx[i], "b": idx[b], "m": cum[i] - cum[a],
                    "start_m": cum[a], "end_m": cum[-1] - cum[b]})
        i = b
    return out


def unjustified_spurs(coords) -> list:
    """Éperons d'au moins SPUR_MIN_M, hors départ et arrivée, qui n'apportent rien (voir SPUR_FIX)."""
    import numpy as np
    import shapely
    out = []
    for s in spur_list(coords):
        if s["m"] < SPUR_MIN_M or s["start_m"] < SPUR_END_M or s["end_m"] < SPUR_END_M:
            continue
        seg = coords[s["a"]:s["t"] + 1]
        if len(seg[0]) > 2 and max(c[2] for c in seg) - seg[0][2] >= SPUR_JUSTIFIED_DPLUS_M:
            continue                                        # monte vraiment : ajoute du D+
        tip = shapely.Point(coords[s["t"]][0], coords[s["t"]][1])
        if POIS and "lieu" in POIS and len(POIS["lieu"][0].query(tip, predicate="dwithin", distance=REMARKABLE_NEAR_DEG)):
            continue                                        # mène à un lieu remarquable
        if LANDSCAPE is not None:
            tv = LANDSCAPE.trees.get("view")
            if tv is not None and len(tv.query(tip, predicate="dwithin", distance=VIEW_NEAR_DEG)):
                continue                                    # mène à un belvédère
            pts = shapely.points(np.array([(c[0], c[1]) for c in seg[::max(1, len(seg) // 20)]]))
            hit = np.zeros(len(pts), dtype=bool)
            for key in ("water", "river", "sea", "forest", "protected"):
                tr = LANDSCAPE.trees.get(key)
                if tr is not None:
                    hit[np.unique(tr.query(pts, predicate="dwithin", distance=0.0006)[0])] = True
            if hit.mean() >= SPUR_PLEASANT_SHARE:
                continue                                    # au bord de l'eau, en forêt ou dans un parc
        out.append({"km": None, "m": round(s["m"]), "a": s["a"], "b": s["b"],
                    "lon": round(coords[s["t"]][0], 5), "lat": round(coords[s["t"]][1], 5)})
    return out


def trim_spurs(gh, loop, level, duration_h):
    """La même boucle, recalculée sans ses éperons injustifiés (points de passage pris de part et d'autre)."""
    if not loop.spurs:
        return None
    way = route_waypoints(loop.coords, skip=[(s["a"], s["b"]) for s in loop.spurs])
    path = gh.via(way, loop.profile, pass_through=True)
    new = analyse(path, level, loop.profile, duration_h, loop.seed, loop.heading) if path else None
    if (new is None or abs(new.time_s / (duration_h * 3600.0) - 1.0) > TIME_TOLERANCE or not overlap_ok(new)
            or new.u_turns > MAX_UTURNS or new.shares["unpaved"] > MAX_UNPAVED or len(new.spurs) >= len(loop.spurs)):
        return None
    return new


def previous_candidates(gh, st, level, duration_h) -> list:
    """Boucles publiées de la version précédente pour ce départ, cette allure et cette durée (KEEP_PREVIOUS), recalculées
    et notées avec les règles actuelles : une nouvelle version ne perd plus une bonne boucle par malchance."""
    out = []
    for profile, coords in (st.get("previous") or {}).get((level, round(duration_h * 60)), []):
        path = gh.via(route_waypoints(coords), profile, pass_through=True)
        loop = analyse(path, level, profile, duration_h, 2000, None) if path else None
        if (loop is None or abs(loop.time_s / (duration_h * 3600.0) - 1.0) > TIME_TOLERANCE or not overlap_ok(loop)
                or loop.u_turns > MAX_UTURNS or loop.shares["unpaved"] > MAX_UNPAVED):
            continue
        out.append(loop)
    return out


def level_pool(gh, st, level: str, duration: float, candidates, log) -> list:
    """Tous les candidats valides d'un départ pour une durée et un niveau : tirages ordinaires de chaque profil, plus
    les tirages « montée » en niveau sportif. Utilisé par la génération ET par le diagnostic (nature_check --probe)."""
    pool: list = []
    runs = [(profile, gh, candidates, profile) for profile in LEVELS[level]["profiles"]]
    if level in CLIMB_LEVELS:
        runs.append(("sport", _ClimbGH(gh), CLIMB_CANDIDATES, "sport + montée"))
    spare: list = []
    for profile, client, cands, label in runs:
        log(f"  {duration:g} h / {level} / {label}")
        found, rejects = fit_and_sample(client, st, level, profile, duration, cands, log, fallback=spare)
        why = ", ".join(f"{k} {v}" for k, v in rejects.items() if v)
        log(f"    {len(found)} candidats valides" + (f" (rejetés : {why})" if why else ""))
        pool.extend(found)
    pool.extend(target_candidates(gh, st, level, LEVELS[level]["profiles"][0], duration, log, fallback=spare))  # ciblés
    if OUTBACK_DRAWS:
        pool.extend(outback_candidates(gh, st, level, LEVELS[level]["profiles"][0], duration, log))
    if KEEP_PREVIOUS and st.get("previous"):
        pool.extend(previous_candidates(gh, st, level, duration))
    if RETOUCH:
        pool.extend(retouch_candidates(gh, st, level, duration, pool, log))
    if SPUR_FIX:                                             # couper les éperons des meilleures candidates
        for l in sorted([x for x in pool if x.spurs], key=lambda x: x.score, reverse=True)[:SPUR_TRIM_TOP]:
            t = trim_spurs(gh, l, level, duration)
            if t is not None:
                pool.append(t)
    if pool:
        return pool
    # repli (choix B, 28/09/2026) : aucune boucle sous MAX_UNPAVED. Le test v10 a montré que l'évitement fort de la terre
    # empêche souvent GraphHopper de boucler (candidats rejetés pour la durée ou les tronçons répétés) : on retente avec
    # les profils « souples », puis on garde les candidats les moins terreux sous les limites du repli, signalés.
    for profile in LEVELS[level]["profiles"]:
        log(f"  {duration:g} h / {level} / {profile} souple (repli)")
        found, rejects = fit_and_sample(_SoftGH(gh), st, level, profile, duration, candidates, log, fallback=spare)
        why = ", ".join(f"{k} {v}" for k, v in rejects.items() if v)
        log(f"    {len(found)} candidats valides" + (f" (rejetés : {why})" if why else ""))
        pool.extend(found)
    if pool:
        return pool
    if spare:
        log(f"    repli : {len(spare)} candidat(s) avec un peu de terre (au plus {FALLBACK_UNPAVED_KM:g} km / "
            f"{FALLBACK_UNPAVED_SHARE:.0%}), signalé(s)")
    return spare


def similarity(a: Loop, b: Loop) -> float:
    inter = len(a.cells & b.cells)
    return inter / max(1, min(len(a.cells), len(b.cells)))


def same_route(a: Loop, b: Loop) -> bool:
    """Doublon strict (O-18) : chacune des deux boucles recouvre l'autre à au moins LEVEL_DUP_SIM."""
    return len(a.cells & b.cells) / max(1, len(a.cells), len(b.cells)) >= LEVEL_DUP_SIM


def secondary_ok(l: Loop, best: Loop) -> bool:
    """Option secondaire acceptable face à la recommandée (SECONDARY_GUARD)."""
    if not SECONDARY_GUARD:
        return True
    if l.scenery is not None and l.scenery.get("industrial", 0.0) >= SECONDARY_MAX_INDUSTRIAL:
        return False
    if l.signals is None or best.signals is None:
        return True
    a = l.signals / max(l.distance_m / 1000.0, 0.1)
    b = best.signals / max(best.distance_m / 1000.0, 0.1)
    return not (a >= SECONDARY_MAX_LIGHTS_RATIO * b and a - b >= SECONDARY_MAX_LIGHTS_GAP)


def pick_options(pool: list[Loop], avoid: list | None = None) -> list[tuple[str, Loop]]:
    """avoid : boucles déjà proposées aux allures inférieures pour la même durée ; les options secondaires qui les
    répètent (same_route) sont écartées (O-18 A)."""
    if not pool:
        return []
    avoid = avoid or []
    pool = sorted(pool, key=lambda l: l.score, reverse=True)
    best = pool[0]
    chosen = [("equilibre", best)]
    rest = [l for l in pool[1:] if l.score >= 0.8 * best.score and similarity(l, best) < 0.6
            and not any(same_route(l, a) for a in avoid) and secondary_ok(l, best)]
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
        for l in pool[1:]:                      # note d'au moins 70 % de la boucle recommandée (comme l'appli, 30/09/2026)
            if (l.score >= VARIANT_MIN_SCORE_RATIO * best.score and similarity(l, best) < 0.6
                    and not any(same_route(l, a) for a in avoid) and secondary_ok(l, best)):
                chosen.append(("variante", l))
                break
    return chosen


def choose_options(gh, st: dict, level: str, duration_h: float, pool: list, prior: list, log) -> list:
    """Options d'un niveau (O-18) : pick_options sans doublon d'allure. Si une option secondaire n'a AUCUNE boucle
    différente de l'allure inférieure, on garde quand même la boucle qu'elle aurait eue, marquée « même parcours qu'à
    l'allure inférieure » (same_as : allure et km en plus), plutôt que de retirer l'option (choix de Florent, 26/09/2026 :
    une prolongation dans la durée ne gagne que 3 à 5 km, c'est la même boucle). gh, st : inutilisés (signature stable)."""
    picks = pick_options(pool, prior)
    full = pick_options(pool)
    if not prior or not picks or len(picks) >= len(full):
        return picks
    used = {lab for lab, _ in picks}
    for lab, lost in full[1:]:
        if lab in used:
            continue
        base = next((a for a in prior if same_route(lost, a)), None)
        if base is None:
            continue
        lost.same_as = {"level": base.level, "km_more": round((lost.distance_m - base.distance_m) / 1000.0, 1)}
        log(f"    {lab} : même parcours qu'en {base.level} (+{lost.same_as['km_more']:g} km), gardé faute d'autre boucle")
        picks.append((lab, lost))
        used.add(lab)
    return picks


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
        **({"surface_seq": l.surface_seq} if any(c in l.surface_seq for c in "upn") else {}),
        **({"road_seq": l.road_seq} if "m" in l.road_seq or "c" in l.road_seq else {}),
        **({"unpaved_fallback": True} if l.unpaved_fallback else {}),
        **({"targeted": True} if 900 <= l.seed < 1000 else {}),   # tirage ciblé (lieu attrayant), pour les diagnostics
        **({"retouched": True} if 1000 <= l.seed < 2000 else {}), # retouche d'une meilleure boucle (RETOUCH)
        **({"kept": True} if l.seed >= 2000 else {}),             # boucle de la version précédente (KEEP_PREVIOUS)
        **({"spurs": len(l.spurs)} if l.spurs else {}),           # éperons injustifiés restants (SPUR_FIX)
        **({"remarkable": l.remarkable} if l.remarkable else {}),  # lieux remarquables traversés (appli, GPX)
        **({"views_passed": l.views_passed} if l.views_passed else {}),   # belvédères devant lesquels on passe
        "terrain": {"max_grade_pct": l.terrain.get("max_grade_pct"), "slope_bands": l.terrain.get("bands"),
                    "n_climbs": l.terrain.get("n_climbs"), "climbs": l.terrain.get("climbs"),
                    "avg_climb_grade_pct": l.terrain.get("avg_climb_grade_pct")},
        "u_turns": l.u_turns,
        "longest_repeat_km": round(l.longest_repeat_m / 1000.0, 2),
        **({"out_and_back": True} if l.overlap > MAX_OVERLAP else {}),   # aller-retour sur piste (OUTBACK_OK)
        "scenery": (None if l.scenery is None else {
            "forest": round(l.scenery["forest"], 3), "water": round(l.scenery["water"], 3),
            "protected": round(l.scenery["protected"], 3), "viewpoints": l.scenery["viewpoints"],
            "industrial": round(l.scenery.get("industrial", 0.0), 3), "score": l.scenery["score"],
            **({"industrial_seq": "".join("i" if x else "-" for x in l.scenery["industrial_mask"])}
               if any(l.scenery.get("industrial_mask") or []) else {}),
            **({"protected_seq": "".join("p" if x else "-" for x in l.scenery["landcover"]["park"])}
               if l.scenery.get("landcover") and "park" in l.scenery["landcover"] else {}),
            "landcover": ({k: v for k, v in l.scenery["landcover"].items() if k not in ("cls", "citymask", "park")}
                          if l.scenery.get("landcover") else None),
            **({"landcover_seq": landcover_seq(l.scenery["landcover"]["cls"])} if l.scenery.get("landcover") else {})}),
        "exit_dense_km": None if l.exit_dense_m is None else round(l.exit_dense_m / 1000.0, 1),
        **({"exit_city_km": exit_city_km([0 if c else 3 for c in l.scenery["landcover"]["citymask"]],
                                         round(l.distance_m / 1000.0, 1))}   # sortie de ville : bâti seul
           if l.scenery and l.scenery.get("landcover") else {}),
        "score": l.score,
        **({"same_as": l.same_as} if getattr(l, "same_as", None) else {}),   # même parcours qu'à l'allure inférieure
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


GENERATOR_VERSION = "15"  # 15 : lieux remarquables les plus célèbres visés (aller-retour au lieu compris) et points
#                           d'accès relus (monastère de Montserrat, Coll de Pal…), relief sportif 0,30, vitesse 0,93, allers-retours sur piste au bord de l'eau (03/10/2026). 14 : retouche sans doublons, préfiltre de durée (±25 %), tronçon le plus chargé en feux
#                           d'abord (la Plata sportif 2 h : retour par le Besòs, 03/10/2026). 13 : retouche des meilleures boucles (rivière, mer, espace vert, lieu remarquable ;
#                           10 essais triés par rentabilité) et lacets non comptés comme demi-tours (02/10/2026). 12 : ville −25 % (2e sortie de Florent) ; montées à la manière des compteurs (≥ 500 m, ≥ 3 %,
#                           partie qui monte vraiment, catégories HC à 4) et raideur selon le km le plus raide (01/10/2026). 11 : ville comptée hors pistes sans voitures, tirages ciblés, terre au km, variante ≥ 70 %.
#                           10 : revêtement complété par la base topographique ICGC (scripts/icgc_tag.py), terre évitée.
#                           9 : pistes sans revêtement noté et sentiers hors ville évités, comptés non goudronnés ;
#                           surface_seq et road_seq pour le surlignage (28/09/2026). 8 : allures définies par la FTP


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
            "urban_share": round(u["city"] + u["residential"], 2),
            **({"exit_city_km": o["exit_city_km"]} if "exit_city_km" in o else {})}   # sortie mesurée par le bâti
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
CARRY = None                    # {"src", "by_key"} si --carry-over-from est fourni (départs publiés, toutes versions)


def params_hash(config_dir: str = "config") -> str:
    """Empreinte de tout ce qui influence les boucles : constantes du modèle + fichiers de configuration GraphHopper.
    Deux générations avec la même empreinte produisent les mêmes boucles pour un même départ (aux données OSM près)."""
    import hashlib
    consts = {"version": GENERATOR_VERSION, "levels": LEVELS, "candidates": CANDIDATES, "weights": WEIGHTS,
              "flow_overlap": FLOW_OVERLAP_FACTOR, "climb": [CLIMB_LEVELS, CLIMB_CANDIDATES, CLIMB_MODEL],
              "tol": TIME_TOLERANCE, "overlap": MAX_OVERLAP, "unpaved": MAX_UNPAVED, "uturns": MAX_UTURNS,
              "signal": [SIGNAL_DELAY_S, SIGNAL_RADIUS_M, SIGNAL_CLUSTER_M, LIGHTS_PER_KM_ZERO_SCORE],
              "physics": [TOTAL_MASS_KG, CDA, CRR, DRIVETRAIN_EFF, REAL_WORLD_FACTOR, DESCENT_CAP_MS,
                          RIDER_KG, LONG_RIDE_H, LONG_RIDE_DROP],
              "profile": [PROFILE_STEP_M, SMOOTH_WINDOW, ASCENT_THRESHOLD_M],
              **({"relief": [RELIEF_WEIGHTS, RELIEF_FULL_M_PER_KM]} if RELIEF_WEIGHTS else {}),
              "level_dup": [LEVEL_DUP_SIM, "same_as"],
              "retouch": [RETOUCH, RETOUCH_TOP, RETOUCH_ANCHORS, RETOUCH_REACH, RETOUCH_REACH_MIN_KM, RETOUCH_MIN_RUN_KM,
                          RETOUCH_ON_ROUTE_DEG, RETOUCH_MAX_TRIALS, RETOUCH_ANCHOR_SHIFT_M, RETOUCH_KIND_ORDER],
              "uturns": [UTURN_LACETS_OK, UTURN_SAME_STREET_M],
              "retouch14": [RETOUCH_DEDUPE, RETOUCH_PREFILTER, RETOUCH_WORST_LEG_FIRST, RETOUCH_FAMILY_CAP],
              "v15": [REMARKABLE_FAME, REMARKABLE_POINTS, OUTBACK_OK, OUTBACK_DRAWS, OUTBACK_MAX_OVERLAP, OUTBACK_MIN_CYCLE]}
    h = hashlib.sha1(json.dumps(consts, sort_keys=True, default=str).encode())
    rp = Path(__file__).parent / "remarkable_places.json"   # liste des lieux (points d'accès) : change les boucles aussi
    if rp.exists():
        h.update(rp.read_bytes())
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
    if old.get("stale"):                                  # gardé tel quel lors d'un run interrompu : à recalculer
        return None
    if not set(levels) <= set(REUSE["index"].get("levels", {})):
        return None
    try:
        age = (time.time() - time.mktime(time.strptime(old["computed_at"], "%Y-%m-%dT%H:%M:%SZ"))) / 86400.0
    except (KeyError, ValueError):
        return None
    return old if age <= REUSE["max_age_days"] else None


def load_carry(src: str):
    """Départs publiés (toutes versions du générateur), pour garder la version en ligne d'un départ que le budget de
    temps n'a pas permis de recalculer : le site reste complet même quand un recalcul est interrompu."""
    try:
        idx = _read_json(src, "web/data/index.json")
    except Exception as e:  # noqa: BLE001
        print(f"! reprise des départs publiés impossible ({type(e).__name__}: {e})", file=sys.stderr)
        return None
    return {"src": src, "by_key": {e["key"]: e for e in idx.get("starts", []) if e.get("key")}}


def carry_over(st: dict, sid: str, out: Path, log):
    """Recopie la version publiée d'un départ non recalculé, marquée "stale" (jamais réutilisée comme à jour)."""
    old = CARRY["by_key"].get(st_key(st)) if CARRY else None
    if old is None:
        return None
    try:
        prev = _read_json(CARRY["src"], f"web/data/starts/{old['id']}.json")
    except Exception as e:  # noqa: BLE001
        log(f"  version publiée illisible ({type(e).__name__})")
        return None
    (out / "starts" / f"{sid}.json").write_text(json.dumps(prev, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log(f"- {st['name']} : budget de temps épuisé, version publiée gardée (à recalculer)")
    return {**old, "id": sid, "stale": True}


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
        kept = carry_over(st, sid, out, log)
        if kept is not None:
            return kept, buf, 0.0
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
            # durées déjà essayées, AVEC ou SANS boucle (01/10/2026 : run #159, 275 départs recalculaient à chaque relance
            # les durées sans aucune boucle, alors que le résultat, à paramètres égaux, reste vide)
            have = {float(d) for d in old.get("durations_h", [])} | {float(d) for d in old.get("durations_tried", [])}
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
                             "options_by_duration": by_dur, "compare": compare_block(options), "reused": True,
                             "durations_tried": sorted(have | {float(d) for d in durations})}
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
    if KEEP_PREVIOUS and CARRY is not None:                  # boucles publiées de la version précédente (candidates)
        try:
            e_prev = CARRY["by_key"].get(key0)
            if e_prev is not None:
                prev_opts = _read_json(CARRY["src"], f"web/data/starts/{e_prev['id']}.json")["options"]
                st["previous"] = {}
                for o in prev_opts:
                    st["previous"].setdefault((o["level"], round(o["duration_target_min"])), []).append(
                        (o.get("profile") or LEVELS[o["level"]]["profiles"][0], o["coords"]))
        except Exception as e:  # noqa: BLE001 : sans elles, on calcule comme avant
            log(f"  boucles précédentes indisponibles ({type(e).__name__})")
    if reused_entry is None:
        log(f"- {st['name']}")
    dur_seconds: dict[str, float] = {}
    for duration in missing_durations:
        t_dur = time.time()
        prior: list = []                                  # options des allures inférieures (O-18 A)
        for level in sorted(levels, key=list(LEVELS).index):
            pool = level_pool(gh, st, level, duration, candidates, log)
            picks = choose_options(gh, st, level, duration, pool, prior, log)
            prior += [l for _, l in picks]
            for i, (label, loop) in enumerate(picks, start=1):
                options.append(enrich_option(to_json(loop, label, sid, i), POIS))
                record_ways(sid, loop)
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
             "durations_tried": sorted({float(d) for d in durations} | ({float(d) for d in reused_entry.get("durations_tried", [])}
                                                                       if reused_entry else set())),
             "compare": compare_block(options), "computed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "compute_seconds": round(time.time() - t0, 1), "compute_seconds_by_duration": dur_seconds}
    log(f"  {len(options)} options, durées disponibles : {', '.join(by_dur)} h ({time.time() - t0:.0f} s ; requêtes "
        f"GraphHopper {gh.calls}, dont {gh.hits} déjà en mémoire)")
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
    ap.add_argument("--carry-over-from", default=None, help="adresse du site publié : si le budget de temps s'épuise, un départ "
                                                             "non recalculé garde sa version publiée, marquée à recalculer (stale)")
    ap.add_argument("--pilot-names", default="Barcelona;Badalona;Montcada i Reixac;Manresa;Vic",
                    help="départs toujours inclus par --per-zone (noms séparés par ; ; ceux qui n'existent pas sont ignorés)")
    ap.add_argument("--pilot-rural", type=int, default=0, help="pilote : nombre total de départs ruraux (au moins --per-zone)")
    ap.add_argument("--pilot-box", default=None, help="pilote : « lon0,lat0,lon1,lat1:N » = N départs ruraux imposés dans cette emprise (relief)")
    ap.add_argument("--time-budget-min", type=float, default=0.0, help="ne démarre plus de nouveau départ après ce nombre de minutes de calcul "
                                                                        "(0 = illimité) ; le site est alors publié incomplet et signalé")
    ap.add_argument("--ways-out", default="", help="CSV des voies OSM au revêtement incertain (à vérifier)")
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

    global SIGNALS, STOPS, LANDSCAPE, POIS
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
    try:
        POIS = None if args.no_landscape else load_pois(pbf_path, places_path.parent)
    except Exception as e:  # noqa: BLE001 : facultatif
        print(f"! points d'intérêt ignorés ({type(e).__name__}: {e})", file=sys.stderr)
        POIS = None
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
    global REUSE, PARAMS_HASH, BUDGET_END, CARRY
    PARAMS_HASH = params_hash()
    BUDGET_END = time.time() + 60.0 * args.time_budget_min if args.time_budget_min else None
    REUSE = load_reuse(args.reuse_from, args.reuse_max_age_days) if args.reuse_from else None
    CARRY = load_carry(args.carry_over_from) if args.carry_over_from else None
    used: dict[str, int] = {}
    ids = []
    reserved = {}
    for st in starts:                                   # un départ réutilisé garde l'identifiant de la génération précédente
        old = reuse_candidate(st_key(st), levels) or (CARRY["by_key"].get(st_key(st)) if CARRY else None)
        if old is not None:                             # (ou version publiée gardée si le budget s'épuise)
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
    if args.ways_out:
        print(f"Voies au revêtement incertain empruntées : {write_ways(args.ways_out)} -> {args.ways_out}", flush=True)
    carried = sum(1 for e in index if e.get("stale"))
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
        "attribution": "© contributeurs OpenStreetMap (ODbL) ; revêtement © ICGC (CC BY 4.0) ; calculs GraphHopper (Apache 2.0)",
        "params_hash": PARAMS_HASH,
        "stats": {"starts_requested": len(starts), "starts_generated": len(index),
                  "starts_reused": sum(1 for e in index if e.get("reused")),
                  "seconds_per_computed_start_by_zone": by_zone_seconds(index),
                  "seconds_per_start_and_duration": by_duration_seconds(index),
                  "starts_skipped": len(skipped), "skipped_by_reason": reasons, "skipped_examples": skipped[:30],
                  "starts_carried_over": carried,
                  "incomplete": bool(reasons.get("budget de temps épuisé")) or carried > 0,
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
    if carried:
        print(f"::warning::GÉNÉRATION INCOMPLÈTE : {carried} départs non recalculés (budget de temps épuisé), version publiée "
              f"gardée. Relancer avec « reuse » coché pour les recalculer.", flush=True)
    if reasons.get("budget de temps épuisé"):
        print(f"::warning::GÉNÉRATION INCOMPLÈTE : {reasons['budget de temps épuisé']} départs non démarrés (budget de temps de "
              f"{args.time_budget_min:g} min épuisé). Relancer avec « reuse » coché pour terminer.", flush=True)
    print(f"Terminé : {len(index)} départs sur {len(starts)} en {elapsed:.0f} s "
          f"({elapsed / max(1, len(starts)):.0f} s par départ) -> {out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
