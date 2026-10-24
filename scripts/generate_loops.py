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
import threading
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
# Relief par allure (Florent, 08/10/2026 : « peut-on vraiment considérer 38,7 km et 798 m de D+ comme tranquille ? ») :
# en v16, Tranquille (13,5 m/km en médiane) et Modéré (14,0) avaient le même relief. Cibles : Tranquille 5-10 m/km,
# Modéré 10-15 m/km. Tranquille : règles OBLIGATOIRES pour toutes ses options, « Plus de relief » compris (D+ par km au
# plus max_dpk, aucune montée de catégorie 2 / 1 / HC, aucune rampe ≥ max_grade %, même seuil que l'alerte « rampe très
# raide » de l'app) ; sans boucle conforme, la moins vallonnée est gardée (repli signalé). Au-dessus de pen_above, la
# note perd RELIEF_OVER_PENALTY par m/km (RELIEF_OVER_MAX au plus). None : désactivé (production v16).
RELIEF_LIMITS = None
RELIEF_LIMITS_V17 = {"facile": {"max_dpk": 10.0, "banned_cats": ("2", "1", "HC"), "max_grade": 20.0, "pen_above": 10.0},
                     "modere": {"pen_above": 15.0}}
# 2e essai (08/10/2026, test 1 : repli dans 50 % des cas en Tranquille, Modéré presque inchangé) : tirages avec le profil
# GraphHopper « calm_flat » (loop_calm + pente moyenne fortement pénalisée, scripts/diag_models/loop_flat.json ; à mettre
# dans config/ à l'activation) et Modéré pénalisé de 4 points par m/km au-dessus de 15
RELIEF_LIMITS_V17_FLAT = {"facile": {**RELIEF_LIMITS_V17["facile"], "flat_profile": "calm_flat"},
                          "modere": {"pen_above": 15.0, "pen_per": 0.04}}
RELIEF_LIMITS = RELIEF_LIMITS_V17_FLAT   # v17 (choix A de Florent, 09/10/2026) ; profil calm_flat dans config/
# v18 (O-55, 10/10/2026 : le profil plat seul fait passer les détours de 76 à 96 et les demi-tours de 32 à 53 en
# Tranquille) : profil adouci (scripts/diag_models/loop_flat_soft.json : ×0,3 / ×0,6 / ×0,85 au lieu de ×0,1 / ×0,3 /
# ×0,6), essai seulement (diagnostic g1 / g2 sur une copie de la configuration)
RELIEF_LIMITS_V18_SOFT = {"facile": {**RELIEF_LIMITS_V17["facile"], "flat_profile": "calm_flat_soft"},
                          "modere": RELIEF_LIMITS_V17_FLAT["modere"]}
# Repli de dernier recours et relief (revue de cohérence du 15/10/2026 : 122 boucles Tranquille trop vallonnées, avec une
# montée de catégorie 2 ou plus ou une rampe de 20 % ou plus, toutes en repli « terre » sans repli « relief » ; Espinalbet
# 1 h 30 : montée de catégorie 1, plus de 50 % sur un chemin de terre). Les candidates « un peu de terre » gardées en
# dernier recours (spare, level_pool) étaient publiées sans passer par relief_filter. SPARE_RELIEF : (A) elles y passent
# aussi (la moins vallonnée, annoncée) ; (B, accord de Florent) une allure ne propose rien quand même sa boucle de repli
# relief dépasse RELIEF_HARD (Tranquille : montée de catégorie 1 ou HC, ou rampe de 25 % ou plus : on pousse le vélo).
# False : réserve publiée telle quelle (production v18).
SPARE_RELIEF = True                    # v19
# 1re mesure (16/10/2026) : 10 durées Tranquille retirées, 18 replis annoncés, Modéré et Sportif inchangés ; des replis
# restaient très durs (el Coll 1 h : 54 m/km) : plafond de relief moyen à 25 m/km (choix de Florent).
RELIEF_HARD = {"facile": {"banned_cats": ("1", "HC"), "max_grade": 25.0, "max_dpk": 25.0}}
RELIEF_OVER_PENALTY = 0.02       # 2 points de note par m/km au-dessus de la cible (pen_per dans RELIEF_LIMITS pour changer)
RELIEF_OVER_MAX = 0.20
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
# Audit du 06/10/2026 (60 départs tirés au hasard) : une boucle « 6 h » de 5 h 25 à côté d'une « 5 h » de 5 h 31. Avec
# DURATION_BINS, chaque durée garde sa plage jusqu'à mi-chemin des durées voisines (« 5 h » de 4 h 30 à 5 h 30), dans la
# limite de TIME_TOLERANCE. Essai : désactivé en production.
DURATION_BINS = True
DURATION_SET = (0.75, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 6.0)
# Diagnostic du 07/10/2026 (123 recommandées v15 perdant plus de 10 points) : 64 boucles écartées à 1-6 % de la limite de
# leur plage (Sant Celoni 5 h : 4 h 24, Breda 6 h : 5 h 22). DURATION_BIN_MARGIN élargit chaque plage de cette part de la
# durée visée (0,05 : « 5 h » de 4 h 15 à 5 h 45) ; l'ordre reste garanti : à une allure donnée, toute boucle d'une durée
# est plus longue que celles de la durée inférieure et plus courte que celles de la durée supérieure (order_bounds).
# None : désactivé (production v16). À l'activation, l'ajouter à l'empreinte (params_hash, "v16").
DURATION_BIN_MARGIN = 0.05        # v17 (test v4 : aucune durée perdue grâce au filet ordered_pool)
ORDER_GAP_S = 180.0              # avec la marge : une durée plus longue d'au moins 3 min que la recommandée de la durée inférieure
# Tranches de durée sans trou (revue de cohérence du 15/10/2026, accord de Florent) : avec TIME_TOLERANCE (15 %) en plus
# des plages, aucune durée ne prend une boucle de 69 à 76 min (entre 1 h et 1 h 30) ni de 138 à 153 min (entre 2 h et
# 3 h) : ces boucles sont perdues (Castelldefels 1 h 30 Sportif, traçage c4) et des durées restent vides. Avec
# CONTIGUOUS_BINS, seul un trou entre deux durées voisines est comblé : chacune s'étend jusqu'au milieu du trou (« 1 h »
# jusqu'à 73 min, « 1 h 30 » dès 73 min ; « 2 h » jusqu'à 2 h 26, « 3 h » dès 2 h 26) ; ailleurs, plages et tolérance
# inchangées. 1re mesure (15/10/2026, 30 départs) : en élargissant toutes les plages jusqu'aux milieux des durées, la
# durée courte prenait des boucles plus longues et poussait la suivante (Sant Feliu 1 h Sportif : 54 -> 75 min, note
# 63 -> 46) : 12 recommandées moins bonnes.
# False : plages bornées par TIME_TOLERANCE (production v18).
CONTIGUOUS_BINS = True                    # v19
_TL = threading.local()          # réglage par fil d'exécution (diagnostics qui comparent deux réglages en parallèle)
# Modèle de temps v19 (14/10/2026), recalé sur les 3 sorties de Florent ensemble (Gràcia 28/09 et 01/10, Eixample 13/10 ;
# traces gardées hors du dépôt), tranches de 100 m, ajustement commun (aucun réglage par sortie). Constats :
# - en montée on pousse plus fort que sur le plat : +50 % de puissance à 6 % et plus (proportionnel en dessous). Avec ce
#   bonus, la puissance sur le plat retrouvée est celle de l'allure Modéré (162 / 158 / 148 W pour 157 W) ;
# - en descente, Florent ne dépasse pas ~25 km/h de moyenne (sinueux, prudence) quand la physique donnait ~43 km/h ;
# - en ville, le ralentissement dépend de la densité de carrefours (passages piétons, feux, stops, cédez-le-passage dans un
#   carré de 700 m), pas du fait d'être sur une piste cyclable : 0 dans le delta, jusqu'à 19 % au-delà de 420 carrefours ;
#   l'ancienne règle (25 % sur toute la « ville » de GraphHopper, rayon 1,5 km) comptait Vallvidrera comme l'Eixample ;
# - feux : 12,5 s d'arrêt + 2,5 s perdues en roulant (freinage avant, relance après, cales) = 15 s par feu franchi.
#   Feux et densité ajustés ensemble (sans coût en roulant, la densité monte à 37 % : pas de double compte).
# Écart à la durée réelle (hors pause) : −1 %, −2 %, −3 % (modèle actuel : jusqu'à +12 % sur l'Eixample).
# Plafond de descente : 25 km/h mesuré sur Florent, qui se dit plus prudent que la moyenne ; 28 km/h retenu en Modéré
# (choix de Florent, 14/10/2026 : ne pas calquer sa prudence, rester prudent). Tranquille et Sportif : estimations, sans
# sortie pour les confirmer. Effet : Eixample 2 h « plus de relief » 106 min à 25 km/h, 102 à 30. False : modèle v18.
TIME_V19 = True                    # v19
CLIMB_POWER_BONUS = 0.3          # puissance × 1,3 à CLIMB_POWER_FULL_GRADE et au-delà, soit ~90 % de la FTP (choix de
#                                 Florent, 15/10/2026) : +50 % au 1er réglage (presque la FTP, intenable sur les longues
#                                 montées de montagne : boucles estimées trop courtes, qui sortaient de leur durée) ; avec
#                                 +30 % et la descente à 28 km/h, écart sur les 3 sorties −1 %, −1 %, 0 %
CLIMB_POWER_FULL_GRADE = 0.06
DESCENT_CAP_KMH = {"facile": 22.0, "modere": 28.0, "soutenu": 32.0}   # sous −2 %
DENSITY_SLOWDOWN = 0.19          # ralentissement maximal en roulant, atteint à DENSITY_FULL carrefours dans le carré
DENSITY_FULL = 420.0
SIGNAL_DELAY_V19_S = 15.0
# Fatigue au fil de la sortie (Florent, 15/10/2026 : « un même cycliste ne montera pas à la même vitesse les 300 m de D+
# d'une boucle d'1 h 30 que les 1 200 d'une boucle de 6 h ») : à la place de la marche à 3 h (LONG_RIDE_DROP, toute la
# boucle), la puissance de base baisse de FATIGUE_PER_H par heure au-delà de FATIGUE_FROM_H (temps déjà roulé à cet endroit)
# et le bonus en montée s'épuise avec le D+ déjà grimpé (exp(-D+ / FATIGUE_CLIMB_M) : moitié vers 1 000 m, un quart vers
# 2 000 m). Ordres de grandeur de l'endurance des amateurs, à caler sur une sortie longue de Florent ; sur ses 3 sorties
# de 2 h, écart −0,5 %, −0,6 %, +1,0 %. False : bonus constant et marche à 3 h.
FATIGUE_V19 = True                    # v19
FATIGUE_FROM_H = 2.0
FATIGUE_PER_H = 0.02
FATIGUE_CLIMB_M = 1440.0
DENSITY = None                   # JunctionDensity, chargé dans main()
# Feux en fin de boucle (Florent, 14/10/2026 : « plus agréable d'avoir les feux sur la fin du parcours qu'au début, moins
# frustrant »). Une boucle retenue dont les feux sont surtout dans la première moitié (position moyenne avant
# LIGHTS_LATE_MAX_MEAN) est recalculée dans l'autre sens ; le sens inverse est gardé s'il est valide, note au moins aussi
# bien à LIGHTS_LATE_TOL points près, et met ses feux plus tard. Le sens change aussi les montées (la Carretera de
# Vallvidrera montée ou descendue) : la note tranche. False : désactivé.
LIGHTS_LATE = True                    # v19
LIGHTS_LATE_MIN = 8              # au moins 8 feux sur la boucle
LIGHTS_LATE_MAX_MEAN = 0.45      # position moyenne des feux (part de la boucle)
LIGHTS_LATE_TOL = 1.0             # choix de Florent (14/10/2026) ; 0,5 au 1er essai (e1 / e2) : 11 boucles inversées sur 188
# Retouche « voies agréables » (Florent, 14/10/2026 ; diagnostic cmp5 : l'Eixample 2 h par la piste du front de mer de la
# Barceloneta note 54,2, la boucle publiée par le couloir de la B-10 53,6, le meilleur candidat tiré aujourd'hui 50,2 :
# aucun tirage ne passe par le front de mer). Voies agréables : pistes cyclables et voies vertes goudronnées à moins de
# SCENIC_SEA_M de la côte ou SCENIC_RIVER_M d'une rivière. Si l'une des meilleures boucles les longe à moins de
# SCENIC_NEAR_M pendant au moins SCENIC_RUN_M sans les emprunter, on essaie d'y passer (trois points de passage pris sur
# la voie, au quart, à la moitié et aux trois quarts du tronçon longé) ; la note tranche. False : désactivé.
SCENIC_RETOUCH = True                    # v19
SCENIC_SEA_M = 60.0
SCENIC_RIVER_M = 100.0
SCENIC_NEAR_M = 600.0           # 400 m au 1er essai (y1 / y2) : l'Eixample 2 h longe la plage de la Barceloneta à 450 m
SCENIC_ON_M = 30.0               # plus près : la boucle est déjà sur la voie
SCENIC_RUN_M = 1000.0
SCENIC_MIN_WAY_M = 500.0         # entre le premier et le dernier point de passage sur la voie
SCENIC_TOP = 2                   # meilleures boucles essayées par durée et allure
SCENIC_MAX_RUNS = 2              # tronçons longés essayés par boucle
SCENIC_WAYS = None               # STRtree des voies agréables, chargé dans main()
# Traçage m7 (14/10/2026) : sur l'Eixample 2 h, le tronçon longé de 6,4 km (Barceloneta - Poblenou) est bien détecté, mais
# avec trois points posés sur la voie et sans sens, la variante note 35,3 (allers-retours, morceaux à contresens).
# SCENIC_DENSE : un point tous les SCENIC_PICK_M le long du tronçon, avec le sens de circulation de la boucle. False : trois
# points sans sens (1er essai).
SCENIC_DENSE = True                    # v19
SCENIC_PICK_M = 500.0
# Traçage m9 (15/10/2026) : avec les points serrés, la variante par tout le front de mer (6,4 km d'un bloc) note 49,2 contre
# 55,8 : remplacer un si long tronçon coûte ailleurs ; le tracé de Florent ne changeait que la Barceloneta (54,2 contre 53,6).
# SCENIC_WINDOWS : en plus du tronçon entier, des portions successives d'environ SCENIC_WINDOW_M. False : tronçon entier seul.
SCENIC_WINDOWS = False
# Contourner les grands axes (Florent, 15/10/2026 : Gràcia 2 h « plus de relief » roule 1 km sur un grand axe là où la
# boucle de l'Eixample passe par le Passeig de la Muntanya, 3,3 km de rues calmes, 0 % de grand axe ; aucune recherche
# n'essayait d'éviter un tronçon de grand axe). AXIS_BYPASS : pour les AXIS_TOP meilleures boucles, chaque tronçon de grand
# axe (primary, trunk, secondary) d'au moins AXIS_RUN_M en ville est remplacé par un passage décalé de AXIS_OFFSET_M de
# chaque côté de l'axe (rues voisines) ; la variante est gardée si elle note mieux. False : désactivé.
AXIS_BYPASS = True                    # v19
AXIS_RUN_M = 500.0
AXIS_OFFSET_M = 200.0
AXIS_TOP = 2
AXIS_MAX_RUNS = 2
# Découpage (traçage a6, 15/10/2026 : à Gràcia, le grand axe court 5,1 km d'affilée de Vallcarca vers Collserola ; un seul
# point décalé au milieu ne reproduit pas un détour local comme le Passeig de la Muntanya). AXIS_CHUNKS : les tronçons de
# plus de 1,5 × AXIS_CHUNK_M sont essayés morceau par morceau (AXIS_MAX_TRIES essais au plus par boucle). False : entier.
AXIS_CHUNKS = False
AXIS_CHUNK_M = 1000.0
AXIS_MAX_TRIES = 6
SCENIC_WINDOW_M = 2000.0
# Bord d'eau en ville (même diagnostic : le couloir de la B-10, à 150-200 m de la mer derrière les immeubles, comptait 49 %
# de bord d'eau, la piste du front de mer 52 %). WATER_CITY_NEAR : en ville dense (bâti de la barre de terrain), un point
# ne compte comme bord d'eau qu'à moins de WATER_CITY_DEG (~80 m) de l'eau ; hors ville, inchangé (~100 m ; une corniche
# voit la mer de plus loin). False : désactivé.
WATER_CITY_NEAR = True                    # v19
WATER_CITY_DEG = 0.0008
# Repli « la mieux notée parmi les quasi-moins vallonnées » (Florent, 14/10/2026 ; diagnostic s1 / s2 : sans boucle
# conforme en Tranquille, le repli gardait LA moins vallonnée quelle que soit sa note : Sant Esteve de Llémena 4 h,
# 12,0 m/km note 70,8 remplacée par 11,9 m/km note 48,4 dès qu'une candidate de plus est apparue ; 5 des 7 baisses du
# test). FALLBACK_BEST : parmi les boucles à FALLBACK_RELIEF_TOL m/km (relief) ou FALLBACK_DIRT_TOL_KM km (terre) de la
# moins vallonnée (la moins terreuse), la mieux notée. False : la moins vallonnée seule (v17).
FALLBACK_BEST = True                    # v19
# Option « plus de relief » du Modéré (Florent, 14/10/2026 : la boucle de l'Eixample qu'il a roulée et aimée, 17,5 m/km,
# perdait 10 points de pénalité de relief, l'option proposée aujourd'hui 16 ; la pénalité au-dessus de 15 m/km, voulue pour
# garder le Modéré modéré, écartait les boucles vallonnées de l'option faite pour elles). PLUS_RELIEF_RULES : pour CHOISIR
# cette option (seuil de 80 % de la recommandée, la plus vallonnée), pas de pénalité jusqu'à PLUS_RELIEF_PEN_ABOVE m/km
# (choix de Florent : 20), la pénalité habituelle au-delà ; interdits (parcours vraiment trop dur) : montée de catégorie
# PLUS_RELIEF_BANNED_CATS, rampe de PLUS_RELIEF_MAX_GRADE % ou plus (pente maximale lissée sur 500 m). La recommandée et
# « moins de relief » gardent la règle du Modéré. False : désactivé.
PLUS_RELIEF_RULES = True                    # v19
PLUS_RELIEF_PEN_ABOVE = 20.0
PLUS_RELIEF_BANNED_CATS = ("1", "HC")
PLUS_RELIEF_MAX_GRADE = 15.0
# Boucles publiées retrouvées à l'identique (14/10/2026, traçage m1 : la boucle publiée de Sant Martí 3 h Tranquille, 72,2,
# retouchée, n'était pas retrouvée par KEEP_PREVIOUS, recalculée d'un point de passage tous les 2 km : GraphHopper prend
# un autre chemin entre deux points). PREVIOUS_WAYPOINT_M : points de passage tous les N m (avec le sens de circulation)
# pour recalculer les boucles publiées. None : ROUTE_WAYPOINT_M (2 km, v18).
PREVIOUS_WAYPOINT_M = 500.0                # v19
# Reclassement des boucles publiées (15/10/2026, mesure combinée v19 : 41 durées perdent plus de 10 points, et dans 40 cas
# la très bonne boucle d'avant n'est plus nulle part : avec le nouveau modèle de temps, elle est estimée plus courte ou plus
# longue, sort de la plage de sa durée, et n'est jamais proposée à la durée où elle tombe maintenant).
# PREVIOUS_ANY_DURATION : les boucles publiées de l'allure, toutes durées confondues, sont candidates à chaque durée ; leur
# durée estimée décide où elles tombent (time_ok). Les requêtes GraphHopper sont les mêmes d'une durée à l'autre (mémoire
# GH_MEMO). False : seulement celles de la même durée (v18).
PREVIOUS_ANY_DURATION = True                    # v19
# Front de mer (Florent, 14/10/2026 ; diagnostics cmp6 / y3 : la piste du passeig Marítim de la Barceloneta et le couloir
# intérieur notent presque pareil, 53,1 contre 52,8 : la note ne voit ni la vue ni l'espace d'une promenade). SEAFRONT_BONUS :
# SEAFRONT_PER_KM point par km roulé sans voitures (piste, voie verte) à moins de SEAFRONT_DEG (~80 m) de la côte ou d'une
# plage, SEAFRONT_MAX points au plus. False : désactivé.
SEAFRONT_BONUS = True                    # v19
SEAFRONT_DEG = 0.0008
SEAFRONT_PER_KM = 0.01
SEAFRONT_MAX = 0.05
# Voies rapides (Florent, 14/10/2026, à tester) : longer une autoroute ou une voie express (bruit, gaz d'échappement) est
# désagréable même sur une piste séparée. MOTORWAY_NUISANCE : MOTORWAY_PER_KM point par km roulé à moins de MOTORWAY_DEG
# (~30 m) d'une autoroute ou voie express à l'air libre (tunnels et sections couvertes exclus), hors trajet SUR la voie
# elle-même (déjà compté dans les grands axes), MOTORWAY_MAX points au plus. False : désactivé.
MOTORWAY_NUISANCE = True                    # v19
MOTORWAY_DEG = 0.00035
MOTORWAY_PER_KM = 0.01
MOTORWAY_MAX = 0.05
MOTORWAY_TRUNK = False           # v3 (i1 / i2 v2 : 73 % des boucles touchées ; les « voies express » OSM comprennent la
#                                 Meridiana, la Gran Via, déjà comptées comme grands axes) : autoroutes seules
MOTORWAY_RUN_M = 300.0           # v2 (i1 / i2 du 14/10 : 95 % des boucles touchées, chaque passage dessus ou dessous
#                                 comptait) : seulement les tronçons LONGÉS sur au moins 300 m d'affilée
MOTORWAYS = None                 # STRtree des autoroutes et voies express à l'air libre, chargé dans main()
# 2e version (revue à l'aveugle de Florent, 21/10/2026, Esplugues 1 h Modéré : « la partie censée être agréable, dans
# la nature, longe la B-23 puis la B-20 : beaucoup de trafic, des voitures qui vont vite, de la pollution »). La forêt,
# le parc ou l'eau comptaient comme agréables à côté d'une voie rapide, et la nuisance ne coûtait qu'un point par km à
# moins de 30 m. MOTORWAY_V2 : (1) les voies express rapides comptent aussi (trunk avec motorroad=yes ou limitées à
# MOTORWAY_V2_TRUNK_KMH et plus : la Ronda de Dalt, pas la Gran Via ni la Meridiana à 50) ; (2) bande de bruit de
# MOTORWAY_V2_DEG (~100 m) ; (3) dans cette bande, ni forêt, ni parc, ni eau ne comptent comme agréables (note de
# paysage, aller-retour « nature », répétition sur route agréable) ; (4) MOTORWAY_V2_PER_KM par km longé, au plus
# MOTORWAY_V2_MAX. False : comme la v19. Hors de l'empreinte tant qu'elle est éteinte.
MOTORWAY_V2 = False
# 2e essai (mesure combinée du 21/10 : km longés 973 -> 5 170 sur 1 093 boucles ; les voies à 80 km/h comprenaient les
# routes principales ordinaires, presque toutes les boucles autour de Barcelone étaient pénalisées) : autoroutes et voies
# express (motorroad=yes) seulement, bande de ~60 m, pénalité inchangée (1 point par km) ; l'essentiel est le retrait de
# la « nature » dans la bande.
MOTORWAY_V2_DEG = 0.0007
MOTORWAY_V2_PER_KM = 0.01
MOTORWAY_V2_MAX = 0.05
MOTORWAY_V2_TRUNK_KMH = 999
MOTORWAYS_FAST = None            # autoroutes et voies express rapides à l'air libre (MOTORWAY_V2), chargé avec MOTORWAYS


def motorway_v2() -> bool:
    """« Nature » le long des voies rapides (MOTORWAY_V2), réglable par fil d'exécution."""
    return bool(getattr(_TL, "motorway_v2", MOTORWAY_V2)) and MOTORWAYS_FAST is not None


def noisy_mask(pts):
    """Par point : à moins de MOTORWAY_V2_DEG d'une autoroute ou voie express rapide (MOTORWAY_V2) ; None si éteint."""
    if not motorway_v2():
        return None
    import numpy as np
    m = np.zeros(len(pts), dtype=bool)
    m[np.unique(MOTORWAYS_FAST.query(pts, predicate="dwithin", distance=MOTORWAY_V2_DEG)[0])] = True
    return m
FALLBACK_RELIEF_TOL = 1.0
FALLBACK_DIRT_TOL_KM = 0.5


def contiguous_bins() -> bool:
    """Tranches de durée sans trou (CONTIGUOUS_BINS), réglable par fil d'exécution."""
    return bool(getattr(_TL, "contiguous_bins", CONTIGUOUS_BINS))


def bin_margin():
    return getattr(_TL, "bin_margin", DURATION_BIN_MARGIN)


def fatigue_v19() -> bool:
    """Fatigue au fil de la sortie (FATIGUE_V19), réglable par fil d'exécution."""
    return bool(getattr(_TL, "fatigue", FATIGUE_V19))


def time_v19() -> bool:
    """Modèle de temps v19 (TIME_V19), réglable par fil d'exécution pour les diagnostics."""
    return bool(getattr(_TL, "time_v19", TIME_V19)) and DENSITY is not None


def axis_bypass() -> bool:
    """Contourner les grands axes (AXIS_BYPASS), réglable par fil d'exécution."""
    return bool(getattr(_TL, "axis_bypass", AXIS_BYPASS))


def axis_candidates(gh, st, level, duration_h, pool, log, detail=None) -> list:
    """Variantes des meilleures boucles qui contournent leurs tronçons de grand axe en ville (AXIS_BYPASS)."""
    if detail is None:
        detail = getattr(_TL, "trace_detail", None)
    bases = []
    for l in sorted(pool, key=lambda x: x.score, reverse=True):
        if all(similarity(l, b) < 0.6 for b in bases):
            bases.append(l)
        if len(bases) >= AXIS_TOP:
            break
    found = []
    for bi, base in enumerate(bases):
        seq = base.road_seq or ""
        # « en ville » : densité urbaine de GraphHopper, ville ou résidentiel (15/10/2026 : la barre de terrain classait
        # Vallcarca en espace ouvert, le raccourci de Gràcia n'était pas essayé) ; jamais la campagne (BV-1468)
        city = (getattr(base, "urban_seq", "") or "").replace("r", "v") or "v" * len(seq)
        runs, k = [], 0                                      # tronçons de grand axe (tranches de 100 m) en ville
        while k < len(seq):
            if seq[k] != "m":
                k += 1
                continue
            a = k
            while k < len(seq) and seq[k] == "m":
                k += 1
            if (k - a) * 100.0 >= AXIS_RUN_M and city[a:k].count("v") >= 0.5 * (k - a):
                runs.append((a, k))
        runs.sort(key=lambda r: r[0] - r[1])
        if getattr(_TL, "axis_chunks", AXIS_CHUNKS):         # morceaux d'environ AXIS_CHUNK_M (AXIS_CHUNKS)
            step = max(1, int(round(AXIS_CHUNK_M / 100.0)))
            pieces = []
            for a, b in runs[:AXIS_MAX_RUNS]:
                if (b - a) * 100.0 > 1.5 * AXIS_CHUNK_M:
                    pieces += [(x, min(b, x + step)) for x in range(a, b, step) if min(b, x + step) - x >= 5]
                else:
                    pieces.append((a, b))
            runs = pieces[:AXIS_MAX_TRIES]
        c, cum = base.coords, _cum_cache(base)
        if detail is not None:
            detail.append(f"  grands axes : boucle {bi + 1} ({base.score:.1f}) : {seq.count('m') / 10:.1f} km de grands axes, "
                          f"{len(runs)} tronçon(s) d'au moins {AXIS_RUN_M:.0f} m en ville")
        for n, (a, b) in enumerate(runs if getattr(_TL, "axis_chunks", AXIS_CHUNKS) else runs[:AXIS_MAX_RUNS]):
            ia = min(range(len(cum)), key=lambda i: abs(cum[i] - a * 100.0))
            ib = min(range(len(cum)), key=lambda i: abs(cum[i] - b * 100.0))
            im = min(range(len(cum)), key=lambda i: abs(cum[i] - (a + b) * 50.0))
            if ib <= ia + 1:
                continue
            kx = 111320.0 * math.cos(math.radians(c[im][1]))
            dx, dy = (c[ib][0] - c[ia][0]) * kx, (c[ib][1] - c[ia][1]) * 110540.0
            norm = math.hypot(dx, dy) or 1.0
            for side in (1, -1):                             # perpendiculaire à l'axe, d'un côté puis de l'autre
                ox, oy = -dy / norm * AXIS_OFFSET_M * side, dx / norm * AXIS_OFFSET_M * side
                mid = [c[im][0] + ox / kx, c[im][1] + oy / 110540.0]
                way = route_waypoints(c, skip=[(ia, ib)])
                pos = next((m for m, p in enumerate(way) if abs(p[0] - c[ia][0]) < 1e-9 and abs(p[1] - c[ia][1]) < 1e-9),
                           None)
                if pos is None:
                    continue
                way = way[:pos + 1] + [mid] + way[pos + 1:]
                path = gh.via(way, base.profile, pass_through=True)
                loop = analyse(path, level, base.profile, duration_h, 4200 + 10 * bi + 2 * n + (side > 0), None) if path else None
                ok = (loop is not None and time_ok(loop.time_s, duration_h) and overlap_ok(loop)
                      and loop.u_turns <= MAX_UTURNS and dirt_ok(loop) and loop.score > base.score)
                if detail is not None:
                    detail.append(f"  grands axes : boucle {bi + 1}, tronçon de {(b - a) / 10:.1f} km à {a / 10:.1f} km, "
                                  f"côté {'gauche' if side > 0 else 'droit'} -> "
                                  + ("pas d'itinéraire" if loop is None else
                                     f"note {loop.score:.1f}, grands axes {loop.shares['main_roads']:.0%}, {loop.time_s / 60:.0f} min")
                                  + (" : gardée" if ok else " : rejetée"))
                if ok:
                    loop.axis_bypassed = True
                    found.append(loop)
    log(f"    grands axes contournés : {len(found)} variante(s) gardée(s)")
    return found


def scenic_windows() -> bool:
    """Voies agréables par portions (SCENIC_WINDOWS), réglable par fil d'exécution."""
    return bool(getattr(_TL, "scenic_windows", SCENIC_WINDOWS))


def scenic_dense() -> bool:
    """Voies agréables : points serrés et orientés (SCENIC_DENSE), réglable par fil d'exécution."""
    return bool(getattr(_TL, "scenic_dense", SCENIC_DENSE))


def spatial_graft() -> bool:
    """Départs voisins greffés (SPATIAL_GRAFT), réglable par fil d'exécution."""
    return bool(getattr(_TL, "spatial_graft", SPATIAL_GRAFT))


def graft_way(home, coords, time_min: float, target_s: float):
    """Points de passage d'une boucle voisine greffée à ce départ (SPATIAL_GRAFT), ou None si la durée estimée est trop
    loin de la durée visée : (points, durée estimée en s, m d'approche)."""
    cum = [0.0]
    for a, b in zip(coords, coords[1:]):
        cum.append(cum[-1] + haversine(a[0], a[1], b[0], b[1]))
    tot = max(cum[-1], 1.0)
    dist = [haversine(home[0], home[1], c[0], c[1]) for c in coords]
    first = [i for i in range(len(coords)) if cum[i] <= 0.5 * tot]
    second = [i for i in range(len(coords)) if cum[i] > 0.5 * tot]
    if not first or not second:
        return None
    i = min(first, key=lambda k: dist[k])
    j = min(second, key=lambda k: dist[k])
    if j - i < 2:
        return None
    speed = tot / max(time_min * 60.0, 1.0)                  # m/s de la boucle voisine
    approach = dist[i] + dist[j]
    est = time_min * 60.0 * (cum[j] - cum[i]) / tot + 1.3 * approach / speed
    if abs(est / target_s - 1.0) > GRAFT_TIME_TOL:
        return None
    return [home] + route_waypoints(coords[i:j + 1]) + [home], est, approach


def spatial_shorter() -> bool:
    """Départs voisins : durée inférieure aussi (SPATIAL_SHORTER), réglable par fil d'exécution."""
    return bool(getattr(_TL, "spatial_shorter", SPATIAL_SHORTER))


def scenic_retouch() -> bool:
    """Retouche « voies agréables » (SCENIC_RETOUCH), réglable par fil d'exécution."""
    return bool(getattr(_TL, "scenic_retouch", SCENIC_RETOUCH)) and SCENIC_WAYS is not None


def water_city_near() -> bool:
    """Bord d'eau à ~80 m en ville (WATER_CITY_NEAR), réglable par fil d'exécution."""
    return bool(getattr(_TL, "water_city_near", WATER_CITY_NEAR))


def fallback_best() -> bool:
    """Repli « la mieux notée parmi les quasi-moins vallonnées » (FALLBACK_BEST), réglable par fil d'exécution."""
    return bool(getattr(_TL, "fallback_best", FALLBACK_BEST))


def seafront_bonus() -> bool:
    """Bonus « front de mer » (SEAFRONT_BONUS), réglable par fil d'exécution."""
    return bool(getattr(_TL, "seafront", SEAFRONT_BONUS))


def motorway_nuisance() -> bool:
    """Pénalité « voie rapide » (MOTORWAY_NUISANCE), réglable par fil d'exécution."""
    return bool(getattr(_TL, "motorway", MOTORWAY_NUISANCE)) and MOTORWAYS is not None


def side_km(coords, cum, car_e, rc_e) -> tuple[float, float]:
    """(km sans voitures en front de mer, km à côté d'une voie rapide), mesurés tous les 100 m."""
    import numpy as np
    import shapely
    if LANDSCAPE is None or cum[-1] < 100.0:
        return 0.0, 0.0
    pos = np.arange(0.0, cum[-1], 100.0)
    idx = np.clip(np.searchsorted(cum, pos, side="right") - 1, 0, len(car_e) - 1)
    pts = shapely.points(np.array(sample_points(coords, cum, 100.0)[:len(pos)]))
    sea_km = mw_km = 0.0
    sea = LANDSCAPE.trees.get("sea")
    if seafront_bonus() and sea is not None:
        near = np.zeros(len(pos), dtype=bool)
        near[np.unique(sea.query(pts, predicate="dwithin", distance=SEAFRONT_DEG)[0])] = True
        sea_km = 0.1 * float(sum(1 for k in range(len(pos)) if near[k] and not car_e[idx[k]]))
    if motorway_nuisance():
        near = np.zeros(len(pos), dtype=bool)
        tree_, deg_ = (MOTORWAYS_FAST, MOTORWAY_V2_DEG) if motorway_v2() else (MOTORWAYS, MOTORWAY_DEG)
        near[np.unique(tree_.query(pts, predicate="dwithin", distance=deg_)[0])] = True
        hit = [bool(near[k]) and rc_e[idx[k]] not in ("motorway", "trunk") for k in range(len(pos))]
        run, n_run = 0, max(1, int(round(MOTORWAY_RUN_M / 100.0)))
        for h in hit + [False]:                              # tronçons longés seulement (MOTORWAY_RUN_M d'affilée)
            if h:
                run += 1
                continue
            if run >= n_run:
                mw_km += 0.1 * run
            run = 0
    return sea_km, mw_km


def load_motorways(pbf: Path, workdir: Path):
    """Autoroutes et voies express à l'air libre (MOTORWAY_NUISANCE) : STRtree de leurs lignes, ou None."""
    import shutil
    import subprocess
    from shapely.geometry import shape
    from shapely.strtree import STRtree
    if not pbf.exists() or not shutil.which("osmium"):
        return None
    filt, out = workdir / "motorways.osm.pbf", workdir / "motorways.geojson"
    try:
        subprocess.run(["osmium", "tags-filter", str(pbf), "w/highway=motorway,trunk",
                        "-o", str(filt), "--overwrite"],
                       check=True, capture_output=True)
        subprocess.run(["osmium", "export", str(filt), "-f", "geojson", "-o", str(out), "--overwrite"],
                       check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        print(f"! voies rapides : extraction échouée ({e.stderr.decode()[:200]})", file=sys.stderr)
        return None
    global MOTORWAYS_FAST
    lines, fast = [], []
    for f in json.loads(out.read_text(encoding="utf-8")).get("features", []):
        p, g = f.get("properties", {}), f.get("geometry", {})
        if g.get("type") != "LineString" or p.get("tunnel") in ("yes", "building_passage") or p.get("covered") == "yes":
            continue
        try:
            kmh = int(str(p.get("maxspeed", "")).split()[0])
        except (ValueError, IndexError):
            kmh = 0
        quick = p.get("highway") == "motorway" or p.get("motorroad") == "yes" or kmh >= MOTORWAY_V2_TRUNK_KMH
        if quick:
            fast.append(shape(g))                        # autoroutes et voies express rapides (MOTORWAY_V2)
        if p.get("highway") == "motorway" or MOTORWAY_TRUNK:
            lines.append(shape(g))
    MOTORWAYS_FAST = STRtree(fast) if fast else None
    print(f"voies rapides à l'air libre : {len(lines)} tronçons ; avec les voies express rapides : {len(fast)}")
    return STRtree(lines) if lines else None


def previous_step() -> float:
    """Pas des points de passage des boucles publiées remises en jeu (PREVIOUS_WAYPOINT_M), réglable par fil d'exécution."""
    return getattr(_TL, "previous_step", PREVIOUS_WAYPOINT_M) or ROUTE_WAYPOINT_M


def plus_relief_rules() -> bool:
    """Option « plus de relief » du Modéré (PLUS_RELIEF_RULES), réglable par fil d'exécution."""
    return bool(getattr(_TL, "plus_relief", PLUS_RELIEF_RULES))


def plus_relief_score(l) -> float:
    """Note de la boucle pour choisir l'option « plus de relief » : la pénalité de relief au-dessus de la cible de l'allure
    est remplacée par celle au-dessus de PLUS_RELIEF_PEN_ABOVE (Modéré seulement)."""
    lim = relief_limits().get(l.level) or {}
    if l.level != "modere" or lim.get("pen_above") is None:
        return l.score
    per = lim.get("pen_per", RELIEF_OVER_PENALTY)
    pen = lambda above: min(RELIEF_OVER_MAX, per * max(0.0, l.dplus_per_km - above))  # noqa: E731
    return l.score + 100.0 * (pen(lim["pen_above"]) - pen(PLUS_RELIEF_PEN_ABOVE))


def plus_relief_ok(l) -> bool:
    """Option « plus de relief » du Modéré : pas de montée trop dure ni de rampe trop raide (PLUS_RELIEF_RULES)."""
    if l.level != "modere":
        return True
    if any(c.get("category") in PLUS_RELIEF_BANNED_CATS for c in (l.terrain or {}).get("climbs", [])):
        return False
    return ((l.terrain or {}).get("max_grade_pct") or 0.0) < PLUS_RELIEF_MAX_GRADE


def previous_any_duration() -> bool:
    """Reclassement des boucles publiées (PREVIOUS_ANY_DURATION), réglable par fil d'exécution."""
    return bool(getattr(_TL, "previous_any", PREVIOUS_ANY_DURATION))


def lights_late() -> bool:
    """Feux en fin de boucle (LIGHTS_LATE), réglable par fil d'exécution."""
    return bool(getattr(_TL, "lights_late", LIGHTS_LATE))


def dirt_rules() -> dict:
    """Règles de terre v17 (DIRT_RULES), réglables par fil d'exécution pour les diagnostics."""
    return getattr(_TL, "dirt_rules", DIRT_RULES) or {}


def backtrack_rules() -> bool:
    """Règle unique des retours sur ses pas (BACKTRACK_RULES), réglable par fil d'exécution pour les diagnostics."""
    return bool(getattr(_TL, "backtrack_rules", BACKTRACK_RULES))


def backtrack_long() -> bool:
    """Allers-retours longs annoncés et pénalisés (BACKTRACK_LONG), réglable par fil d'exécution."""
    return bool(getattr(_TL, "backtrack_long", BACKTRACK_LONG))


def long_backtrack(l):
    """Le plus long aller-retour utile de la boucle s'il dépasse BACKTRACK_LONG_SHARE du parcours : (part, aller-retour),
    sinon None."""
    bts = getattr(l, "backtracks", None) or []
    if not bts or not l.distance_m:
        return None
    b = max(bts, key=lambda x: x["m"])
    share = b["m"] / l.distance_m
    return (share, b) if share > BACKTRACK_LONG_SHARE else None


def backtrack_max_share():
    """Plafond des allers-retours utiles (BACKTRACK_MAX_SHARE), réglable par fil d'exécution."""
    return getattr(_TL, "backtrack_max_share", BACKTRACK_MAX_SHARE)


def turn_rules() -> bool:
    """Demi-tours au bon endroit (TURN_RULES), réglable par fil d'exécution."""
    return bool(getattr(_TL, "turn_rules", TURN_RULES))


def destination_tip(p) -> bool:
    """Demi-tour à une destination (TURN_RULES, volet 5) : lieu remarquable, belvédère, sommet ou col, bord de mer ou de
    lac (pas le long d'une rivière). Sans données chargées : pas de jugement (vrai)."""
    import shapely
    if not POIS and LANDSCAPE is None:
        return True
    pt = shapely.Point(p[0], p[1])
    if POIS:
        if "lieu" in POIS and len(POIS["lieu"][0].query(pt, predicate="dwithin", distance=REMARKABLE_NEAR_DEG)):
            return True
        if dest_heritage() and len(POIS["patrimoine"][0].query(pt, predicate="dwithin", distance=REMARKABLE_NEAR_DEG)):
            return True                                  # monastère, château, bien patrimonial (DEST_HERITAGE)
        for k in ("peak", "pass"):
            if k in POIS and len(POIS[k][0].query(pt, predicate="dwithin", distance=REMARKABLE_NEAR_DEG)):
                return True
    if LANDSCAPE is not None:
        tv = LANDSCAPE.trees.get("view")
        if tv is not None and len(tv.query(pt, predicate="dwithin", distance=VIEW_NEAR_DEG)):
            return True
        near = lambda k, d: (LANDSCAPE.trees.get(k) is not None  # noqa: E731
                             and len(LANDSCAPE.trees[k].query(pt, predicate="dwithin", distance=d)) > 0)
        if near("sea", 0.0006):
            return True                                  # bord de mer
        if near("water", 0.0006) and not near("river", 0.0015):
            return True                                  # lac, réservoir (pas une rivière : la couche « eau » les mêle)
    return False


def extend_to_place(gh, l, level, duration_h) -> list:
    """La même boucle, ses allers-retours prolongés jusqu'au lieu remarquable tout proche qu'ils n'atteignent pas
    (TURN_RULES, cas de Montserrat)."""
    if not (POIS and "lieu" in POIS) or not getattr(l, "backtracks", None):
        return []
    import shapely
    heritage = dest_heritage()
    trees = [POIS["lieu"]] + ([POIS["patrimoine"]] if heritage else [])
    seen = {r["n"] for r in (l.remarkable or [])}
    out = []
    for bt in l.backtracks:
        tip = shapely.Point(bt["lon"], bt["lat"])
        near = [(haversine(bt["lon"], bt["lat"], tr.geometries[k].x, tr.geometries[k].y), tr.geometries[k], it[k][1], n_)
                for n_, (tr, it) in enumerate(trees)
                for k in tr.query(tip, predicate="dwithin",
                                  distance=(DEST_HERITAGE_EXTEND_M if heritage else TURN_EXTEND_M) / 111000.0)]
        if heritage:                                         # le plus proche du demi-tour (DEST_HERITAGE)
            near.sort(key=lambda z: z[0])
        for _, g_, name, n_ in [z for z in near if z[2] not in seen][:1]:   # un lieu par aller-retour (comme avant)
            way = route_waypoints(l.coords)
            j = min(range(len(way)), key=lambda i: haversine(way[i][0], way[i][1], bt["lon"], bt["lat"]))
            way = way[:j + 1] + [[g_.x, g_.y]] + way[j:]    # du demi-tour au lieu, puis retour au même point
            path = gh.via(way, l.profile)
            t = analyse(path, level, l.profile, duration_h, l.seed, l.heading) if path else None
            reached = (name in {r["n"] for r in (t.remarkable or [])} if t is not None and n_ == 0 else
                       t is not None and min(haversine(c[0], c[1], g_.x, g_.y) for c in t.coords) <= 150.0)
            if (t is not None and time_ok(t.time_s, duration_h) and overlap_ok(t) and t.u_turns <= MAX_UTURNS
                    and dirt_ok(t) and reached):
                out.append(t)
    return out


def appendix_rule() -> bool:
    """Petites boucles accrochées sans but (APPENDIX_RULE), réglable par fil d'exécution."""
    return bool(getattr(_TL, "appendix_rule", APPENDIX_RULE))


def find_appendices(coords, cum) -> list:
    """Petites boucles accrochées (voir APPENDIX_RULE) : [(a, b)] indices de départ et de retour, sans chevauchement,
    les plus longues d'abord."""
    import bisect
    total = cum[-1]
    if total < 3 * APPENDIX_MIN_M or len(coords) < 10:
        return []
    kx = 111320.0 * math.cos(math.radians(coords[0][1]))
    xy = [(p[0] * kx, p[1] * 110540.0) for p in coords]

    def at(m):
        m = min(max(m, 0.0), total)
        k = min(len(cum) - 1, max(1, bisect.bisect_left(cum, m)))
        a, b = xy[k - 1], xy[k]
        f = (m - cum[k - 1]) / max(cum[k] - cum[k - 1], 1e-9)
        return (a[0] + f * (b[0] - a[0]), a[1] + f * (b[1] - a[1]))
    d = lambda p, q: math.hypot(p[0] - q[0], p[1] - q[1])  # noqa: E731
    samp, last = [], -1e9
    for i, m in enumerate(cum):
        if m - last >= 25.0:
            samp.append(i)
            last = m
    near = APPENDIX_NEAR_M
    grid: dict = {}
    for i in samp:
        grid.setdefault((int(xy[i][0] // near), int(xy[i][1] // near)), []).append(i)
    hi = min(APPENDIX_MAX_M, APPENDIX_MAX_SHARE * total)
    cands = []
    for i in samp:
        if cum[i] < APPENDIX_STEM_M:
            continue
        gx, gy = int(xy[i][0] // near), int(xy[i][1] // near)
        best = None
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for j in grid.get((gx + dx, gy + dy), []):
                    span = cum[j] - cum[i]
                    if not APPENDIX_MIN_M <= span <= hi or total - cum[j] < APPENDIX_STEM_M or d(xy[i], xy[j]) > near:
                        continue
                    if d(at(cum[i] - APPENDIX_STEM_M), at(cum[j] + APPENDIX_STEM_M)) > near:
                        continue                         # on ne repart pas par la route d'arrivée
                    if d(at(cum[i] + 150.0), at(cum[j] - 150.0)) < 2 * near:
                        continue                         # aller-retour : vérifié à part
                    if best is None or span > cum[best] - cum[i]:
                        best = j
        if best is not None:
            cands.append((cum[best] - cum[i], i, best))
    out = []
    for _, a, b in sorted(cands, reverse=True):          # les plus longues, sans chevauchement
        if all(b < a2 or a > b2 for a2, b2 in out):
            out.append((a, b))
    return out


# FORCED_DETOURS (mesure combinée du 21/10, vérifié sur la carte : le « détour » repéré 17 fois près de Pedralbes est un
# lacet de la Carretera de Vallvidrera, BV-1462 ; l'altitude lissée ne montre pas la montée) : quand le nettoyage refait
# le morceau par le chemin le plus direct (profil « sport ») et retrouve le détour au même endroit, c'est la route
# elle-même (lacet, échangeur, passage sous une voie) : l'endroit est retenu (FORCED_SPOTS, ~50 m) et ses détours ne
# comptent plus, ni comme défaut ni dans la note. False : comme avant.
FORCED_DETOURS = False
FORCED_SPOTS: set = set()
# Comparaison à l'aveugle de Florent (22/10/2026, 20 cas) : nettoyage 4 mieux / 2 pareil / 1 moins bien, allers-retours
# 4 / 3 / 3. Restaient : (1) DETOUR_DEST : un détour en pâté de maisons était excusé s'il passait par une piste, de l'eau
# ou de la nature (Granollers 6 h, vers Caldes de Montbui : vus, jamais comptés) ; seuls un lieu remarquable, un
# belvédère ou un sommet l'excusent désormais, comme pour les allers-retours (D82). (2) DEFECT_STRICT : les défauts
# restants, faute de version propre valide (Sant Martí carrer del Rosari, Horta carrer de Ramon Rocafull : « ce genre de
# bug décrédibiliserait l'application »), coûtent DEFECT_STRICT_PEN chacun (au plus DEFECT_STRICT_MAX) en plus : une
# boucle sans défaut de note proche passe devant. False : comme avant.
DETOUR_DEST = False
DEFECT_STRICT = False
DEFECT_STRICT_PEN = 0.08
DEFECT_STRICT_MAX = 0.20
# HAIRPINS (24/10/2026, options regrettées par Florent : Folgueroles 6 h, Monistrol 2 h) : leurs « détours » étaient des
# lacets (épingle du Pont del Llop sur la C-154, BP-1121), rendus presque éliminatoires par DEFECT_STRICT. Un détour en
# pâté de maisons tourne à angle vif à 3 carrefours au moins ; un lacet ne fait qu'un virage (2 pour un double lacet) :
# un détour à moins de 3 virages vifs (tracé complet de GraphHopper) n'est plus compté. False : comme avant.
HAIRPINS = False
HAIRPIN_STEP_M = 15.0
HAIRPIN_SHARP_DEG = 60.0


def hairpins() -> bool:
    """Lacets non comptés comme détours (HAIRPINS), réglable par fil d'exécution."""
    return bool(getattr(_TL, "hairpins", HAIRPINS))


def smooth_window(coords, a: int, b: int) -> bool:
    """Le tracé coords[a..b] est-il un lacet plutôt qu'un tour de pâté de maisons ? Virages vifs (au moins
    HAIRPIN_SHARP_DEG entre le cap des HAIRPIN_STEP_M avant et celui des HAIRPIN_STEP_M après un point), regroupés à
    moins de 40 m : un pâté de maisons en a au moins 3 (les carrefours), un lacet 1, un double lacet 2."""
    cum = [0.0]
    for p, q in zip(coords[a:b], coords[a + 1:b + 1]):
        cum.append(cum[-1] + haversine(p[0], p[1], q[0], q[1]))
    seg = coords[a:b + 1]
    zones, last = 0, -1e9
    for i in range(1, len(seg) - 1):
        j = i - 1
        while j > 0 and cum[i] - cum[j] < HAIRPIN_STEP_M:
            j -= 1
        k = i + 1
        while k < len(seg) - 1 and cum[k] - cum[i] < HAIRPIN_STEP_M:
            k += 1
        turn = abs((bearing(seg[i][0], seg[i][1], seg[k][0], seg[k][1])
                    - bearing(seg[j][0], seg[j][1], seg[i][0], seg[i][1]) + 180) % 360 - 180)
        if turn >= HAIRPIN_SHARP_DEG:
            if cum[i] - last > 40.0:
                zones += 1
            last = cum[i]
    return zones < 3


# Les 43 jugements de Florent (revue et comparaison à l'aveugle, 21-22/10/2026). (1) Entre deux versions d'une même sortie,
# il choisit 11 fois sur 12 celle qui repasse le moins ; la note n'en tenait presque pas compte (25 % répétés : 4,5
# points). FLOW_V2 : répétition pesée FLOW_V2_FACTOR fois au lieu de FLOW_OVERLAP_FACTOR. (2) Florent : « l'attractivité
# des petites routes de campagne / forêt tant qu'elles sont goudronnées ». SMALL_ROADS : bonus SMALL_ROADS_W par part
# du parcours sur de petites routes goudronnées (classes SMALL_ROADS_CLASSES) hors de la ville. False : comme avant.
FLOW_V2 = False
FLOW_V2_FACTOR = 3.0
SMALL_ROADS = False
SMALL_ROADS_W = 0.10
SMALL_ROADS_CLASSES = ("unclassified", "tertiary", "track", "living_street")


def flow_v2() -> bool:
    """Répétition pesée davantage (FLOW_V2), réglable par fil d'exécution."""
    return bool(getattr(_TL, "flow_v2", FLOW_V2))


def small_roads() -> bool:
    """Bonus des petites routes goudronnées hors de la ville (SMALL_ROADS), réglable par fil d'exécution."""
    return bool(getattr(_TL, "small_roads", SMALL_ROADS))


def detour_dest() -> bool:
    """Détours excusés seulement par une destination (DETOUR_DEST), réglable par fil d'exécution."""
    return bool(getattr(_TL, "detour_dest", DETOUR_DEST))


def defect_strict() -> bool:
    """Défauts presque éliminatoires (DEFECT_STRICT), réglable par fil d'exécution."""
    return bool(getattr(_TL, "defect_strict", DEFECT_STRICT))


def forced_detours() -> bool:
    """Lacets et passages obligés (FORCED_DETOURS), réglable par fil d'exécution."""
    return bool(getattr(_TL, "forced_detours", FORCED_DETOURS))


def spot_key(p) -> tuple:
    """Case d'environ 50 m d'un point (FORCED_SPOTS)."""
    return (round(p[1] * 2000), round(p[0] * 2000))


def clean_picks() -> bool:
    """Nettoyage des boucles publiées (CLEAN_PICKS), réglable par fil d'exécution."""
    return bool(getattr(_TL, "clean_picks", CLEAN_PICKS))


def city_backtrack() -> bool:
    """Allers-retours en ville dense (CITY_BACKTRACK), réglable par fil d'exécution."""
    return bool(getattr(_TL, "city_backtrack", CITY_BACKTRACK))


def backtrack_dest() -> bool:
    """Allers-retours vers une destination (BACKTRACK_DEST), réglable par fil d'exécution."""
    return bool(getattr(_TL, "backtrack_dest", BACKTRACK_DEST))


def repeat_plain() -> bool:
    """Répétition sur routes ordinaires (REPEAT_PLAIN), réglable par fil d'exécution."""
    return bool(getattr(_TL, "repeat_plain", REPEAT_PLAIN))


def spur_turn_window() -> bool:
    """Demi-tours par un rond-point (SPUR_TURN_WINDOW), réglable par fil d'exécution."""
    return bool(getattr(_TL, "spur_turn_window", SPUR_TURN_WINDOW))


def nested_backtrack() -> bool:
    """Allers-retours emboîtés (NESTED_BACKTRACK), réglable par fil d'exécution."""
    return bool(getattr(_TL, "nested_backtrack", NESTED_BACKTRACK))


def defect_windows(l) -> list:
    """Tronçons à retirer d'une boucle (CLEAN_PICKS) : détours, éperons injustifiés et boucles accrochées, fusionnés."""
    win = sorted([(d["a"], d["b"]) for d in (getattr(l, "detours", None) or [])]
                 + [(s["a"], s["b"]) for s in (getattr(l, "spurs", None) or [])]
                 + [(s["a"], s["b"]) for s in (getattr(l, "appendices", None) or [])])
    out: list = []
    for a, b in win:
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def loop_targets(l) -> list:
    """Allers-retours à transformer en boucle avant de les retirer (CLEAN_PICKS) : éperons injustifiés et allers-retours
    dans un couloir d'au moins LOOPIFY_MIN_M à l'aller. [(a, b)]."""
    out = [(s["a"], s["b"]) for s in (getattr(l, "spurs", None) or []) if (s.get("m") or 0) >= LOOPIFY_MIN_M]
    out += [(s["a"], s["b"]) for s in (getattr(l, "appendices", None) or []) if s.get("corridor")]
    return sorted(out)


def loop_extras(coords, a, b) -> list:
    """Points qui font d'un aller-retour coords[a..b] une petite boucle (Florent, 17/10/2026, le Moll d'Espanya :
    descendre le quai par un côté, remonter par l'autre) : un point à LOOPIFY_OFFSET_M d'un côté du milieu de l'aller,
    le demi-tour, puis le point symétrique de l'autre côté."""
    kx = 111320.0 * math.cos(math.radians(coords[0][1]))
    t = max(range(a, b + 1), key=lambda i: haversine(coords[a][0], coords[a][1], coords[i][0], coords[i][1]))
    half, acc, m = haversine(coords[a][0], coords[a][1], coords[t][0], coords[t][1]) / 2.0, 0.0, a
    while m < t and acc < half:
        acc += haversine(coords[m][0], coords[m][1], coords[m + 1][0], coords[m + 1][1])
        m += 1
    h = math.radians(bearing(coords[a][0], coords[a][1], coords[t][0], coords[t][1]))
    dx, dy = math.cos(h) * LOOPIFY_OFFSET_M, -math.sin(h) * LOOPIFY_OFFSET_M     # perpendiculaire (est, nord)
    side = lambda s: [coords[m][0] + s * dx / kx, coords[m][1] + s * dy / 110540.0]  # noqa: E731
    return [side(1), [coords[t][0], coords[t][1]], side(-1)]


def loopify_way(coords, win, targets) -> list:
    """Points de passage de la boucle sans les tronçons win, où chaque aller-retour de targets devient une petite boucle
    (loop_extras), insérée après l'entrée du tronçon qui le contient."""
    way = route_waypoints(coords, skip=win)
    for w0, w1 in win:
        extra = [p for a, b in targets if w0 <= a and b <= w1 for p in loop_extras(coords, a, b)]
        if not extra:
            continue
        k = next((i for i, p in enumerate(way) if p[0] == coords[w0][0] and p[1] == coords[w0][1]), None)
        if k is not None:
            way[k + 1:k + 1] = extra
    return way


def _edges_raw(det, n) -> list:
    """Valeur brute d'un détail GraphHopper pour chacun des n tronçons (None si absente)."""
    arr = [None] * n
    for a, b, val in det or []:
        for i in range(max(0, a), min(b, n)):
            arr[i] = val
    return arr


def _intervals(arr) -> list:
    """Inverse de _edges_raw : [[début, fin, valeur]] (les tronçons sans valeur sont omis)."""
    out, i = [], 0
    while i < len(arr):
        j = i
        while j + 1 < len(arr) and arr[j + 1] == arr[i]:
            j += 1
        if arr[i] is not None:
            out.append([i, j + 1, arr[i]])
        i = j + 1
    return out


def splice_path(path: dict, a: int, b: int, sub: dict) -> dict:
    """Itinéraire path où les points a..b sont remplacés par l'itinéraire sub (CLEAN_PICKS, 4e essai), détails compris :
    le reste de la boucle est gardé tel quel (aucun nouveau point de passage, donc aucun nouveau détour ailleurs)."""
    pts, sp = path["points"]["coordinates"], sub["points"]["coordinates"]
    n_old, n_sub = len(pts) - 1, len(sp) - 1
    det, sd = path.get("details") or {}, sub.get("details") or {}
    new_det = {}
    for k in set(det) | set(sd):
        old = _edges_raw(det.get(k), n_old)                  # pts[a-1] -> sp[0] : tronçon a-1 ; sp[-1] -> pts[b+1] : b
        new_det[k] = _intervals(old[:a] + _edges_raw(sd.get(k), n_sub) + old[b:])
    up = sum(max(0.0, pts[i + 1][2] - pts[i][2]) for i in range(a, b)) if len(pts[0]) > 2 else 0.0
    return {"points": {"coordinates": pts[:a] + sp + pts[b + 1:]}, "details": new_det,
            "ascend": max(0.0, float(path.get("ascend", 0.0)) - up + float(sub.get("ascend", 0.0)))}


def clean_loop(gh, l, level, duration_h, bounds=None):
    """La même boucle sans ses détours, éperons et boucles accrochées (CLEAN_PICKS), ou None. 4e essai (mesure du
    18/10/2026 : recalculer toute la boucle par des points tous les 2 km rajoutait des détours ailleurs, 315 essais
    sur 363) : seul le morceau autour de chaque défaut (CLEAN_MARGIN_M avant et après) est recalculé et recousu dans la
    boucle ; les allers-retours sont d'abord transformés en petite boucle (loop_extras), puis retirés. La meilleure
    version valide est gardée."""
    win = defect_windows(l)
    aw = list(getattr(l, "aimless_windows", None) or [])    # allers-retours « nature » sans destination (BACKTRACK_DEST)
    det0 = getattr(l, "gh_details", None)
    if not (win or aw) or det0 is None:
        return None
    targets = sorted(loop_targets(l) + aw)
    c = l.coords
    cum = [0.0]
    for p, q in zip(c, c[1:]):
        cum.append(cum[-1] + haversine(p[0], p[1], q[0], q[1]))

    def widen(ws):                                           # tronçons élargis de CLEAN_MARGIN_M, fusionnés
        out: list = []
        for a, b in sorted(ws):
            a2, b2 = a, b
            while a2 > 1 and cum[a] - cum[a2 - 1] <= CLEAN_MARGIN_M:
                a2 -= 1
            while b2 < len(c) - 2 and cum[b2 + 1] - cum[b] <= CLEAN_MARGIN_M:
                b2 += 1
            if out and a2 <= out[-1][1]:
                out[-1] = (out[-1][0], max(out[-1][1], b2))
            else:
                out.append((a2, b2))
        return out
    stats = getattr(_TL, "clean_stats", None)
    best = None
    variants = (([("boucle", True, widen(win + aw), l.profile)] if targets else [])
                + ([("retrait", False, widen(win), l.profile)] if win else [])
                # 5e essai (mesure du 21/10 : encore 293 retraits sur 373 refont le détour) : les profils « calm » préfèrent
                # faire le tour d'un pâté de maisons par une piste cyclable plutôt que 100 m de rue ; un cycliste du coin
                # va tout droit. Le morceau est aussi recalculé avec le profil « sport », sans attrait pour les pistes.
                + ([("direct", False, widen(win), "sport")] if win and l.profile != "sport" else []))
    for kind, loopify, wide, prof in variants:
        path = {"points": {"coordinates": c}, "details": det0, "ascend": l.ascend_gh_m}
        for a2, b2 in reversed(wide):                        # de la fin vers le début : les indices restent justes
            pts = [[c[a2][0], c[a2][1]]]
            if loopify:
                pts += [p for a, b in targets if a2 <= a and b <= b2 for p in loop_extras(c, a, b)]
            pts.append([c[b2][0], c[b2][1]])
            sub = gh.via(pts, prof, pass_through=len(pts) > 2)
            if sub is None or len(sub["points"]["coordinates"]) < 2:
                path = None
                break
            path = splice_path(path, a2, b2, sub)
        t = analyse(path, level, l.profile, duration_h, l.seed, l.heading) if path else None
        why = ("sans itinéraire" if t is None else "durée" if not time_ok(t.time_s, duration_h)
               else "répétition" if not overlap_ok(t) else "demi-tours" if t.u_turns > MAX_UTURNS
               else "terre" if not dirt_ok(t) else "relief" if not relief_ok(t)
               else "défauts" if (len(defect_windows(t)) >= len(win) if win else len(defect_windows(t)) > 0)
               else "note" if (t.score < l.score - 100 * CLEAN_PICKS_SLACK if win else t.score <= l.score)
               else "grands axes" if t.shares["main_roads"] > l.shares["main_roads"] + 0.03
               else "ordre" if bounds is not None and not bounds[0] <= t.time_s <= bounds[1] else "ok")
        if stats is not None:
            stats[f"{kind} {why}"] = stats.get(f"{kind} {why}", 0) + 1
            if why == "défauts" and len(stats.setdefault("exemples", [])) < 40:   # où le défaut reste (diagnostic)
                where = lambda w: [round(t.coords[w[0]][1], 5), round(t.coords[w[0]][0], 5)]  # noqa: E731
                stats["exemples"].append({"essai": kind, "km": round(l.distance_m / 1000.0, 1),
                                          "avant": [[round(c[a][1], 5), round(c[a][0], 5)] for a, _ in win],
                                          "après": [where(w) for w in defect_windows(t)]})
        if (forced_detours() and t is not None and why == "défauts" and (kind == "direct" or prof == "sport")
                and getattr(l, "detours", None)):        # même détour au même endroit par le plus direct : la route
            again = {spot_key(t.coords[d["a"]]) for d in (t.detours or [])}
            for d in l.detours:
                if spot_key(c[d["a"]]) in again:
                    FORCED_SPOTS.add(spot_key(c[d["a"]]))
        if why == "ok" and (best is None or t.score > best.score):
            best = t
            best.cleaned_kind = kind                         # pour les mesures : quelle version propre (22/10/2026)
    if forced_detours() and getattr(l, "detours", None):   # lacets et passages obligés : plus comptés (FORCED_DETOURS)
        kept = [d for d in l.detours if spot_key(c[d["a"]]) not in FORCED_SPOTS]
        if len(kept) < len(l.detours):
            l.detours = kept
            l.score = score(l)
    if best is not None:
        best.cleaned_from = getattr(l, "cleaned_from", None) or route_key(simplify(l.coords, 10.0))[:6]
        for attr in ("relief_fallback", "unpaved_fallback"):
            if getattr(l, attr, False):
                setattr(best, attr, True)
    return best


def axes_over() -> bool:
    """Grands axes au-delà d'un seuil (AXES_OVER), réglable par fil d'exécution."""
    return bool(getattr(_TL, "axes_over", AXES_OVER))


def corridor_spurs_on() -> bool:
    """Allers-retours dans un couloir (CORRIDOR_SPURS), réglable par fil d'exécution."""
    return bool(getattr(_TL, "corridor_spurs", CORRIDOR_SPURS))


def corridor_spurs(coords, cum) -> list:
    """Allers-retours dans un couloir (voir CORRIDOR_SPURS) : [(a, b, t)] indices de l'entrée, de la sortie et du
    demi-tour. Le retour longe l'aller « en miroir » : plus on avance à l'aller, plus le point de retour le plus tardif
    à moins de CORRIDOR_NEAR_M est proche du demi-tour."""
    total = cum[-1]
    if len(coords) < 10 or total < 3000.0:
        return []
    kx = 111320.0 * math.cos(math.radians(coords[0][1]))
    samp, last = [], -1e9
    for i, m in enumerate(cum):
        if m - last >= 25.0:
            samp.append(i)
            last = m
    xy = [(coords[i][0] * kx, coords[i][1] * 110540.0) for i in samp]
    near = CORRIDOR_NEAR_M
    grid: dict = {}
    for p, (x, y) in enumerate(xy):
        grid.setdefault((int(x // near), int(y // near)), []).append(p)
    match = [None] * len(samp)
    for p, (x, y) in enumerate(xy):                       # retour le plus tardif à moins de near (dans les 10 km)
        best = None
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for q in grid.get((int(x // near) + dx, int(y // near) + dy), []):
                    gap = cum[samp[q]] - cum[samp[p]]
                    if 60.0 <= gap <= 10000.0 and math.hypot(xy[q][0] - x, xy[q][1] - y) <= near:
                        if best is None or q > best:
                            best = q
        match[p] = best
    out, p = [], 0
    while p < len(samp):
        if match[p] is None or match[p] <= p + 1:
            p += 1
            continue
        p0, q0 = p, match[p]
        while p + 1 < len(samp) and match[p + 1] is not None and p + 1 < match[p + 1] <= match[p]:
            p += 1                                           # l'aller avance, le retour recule : miroir
        a, b, t = samp[p0], samp[q0], samp[p]
        if (cum[t] - cum[a] >= CORRIDOR_MIN_M and cum[a] >= 300.0 and total - cum[b] >= 300.0
                and (cum[b] - cum[a]) <= 0.5 * total):
            out.append((a, b, t))
        p = max(p + 1, p0 + 1)
    return out


def false_start() -> bool:
    """Faux départs et faux retours (FALSE_START), réglable par fil d'exécution."""
    return bool(getattr(_TL, "false_start", FALSE_START))


def false_starts(coords, cum) -> list:
    """Faux départ / faux retour (voir FALSE_START) : [(a, b)] indices du tronçon (début ou fin de boucle) qui s'éloigne
    du départ puis y repasse avant de partir ailleurs."""
    if len(coords) < 10 or cum[-1] < 3000.0:
        return []
    s0, total = coords[0], cum[-1]
    d0 = [haversine(s0[0], s0[1], c[0], c[1]) for c in coords]
    out = []
    head = [j for j in range(len(coords)) if 800.0 <= cum[j] <= FALSE_START_SHARE * total and d0[j] <= FALSE_START_NEAR_M]
    if head:
        j = head[-1]
        if max(d0[:j + 1]) >= FALSE_START_AWAY_M:
            out.append((0, j))
    tail = [i for i in range(len(coords)) if 800.0 <= total - cum[i] <= FALSE_START_SHARE * total
            and d0[i] <= FALSE_START_NEAR_M]
    if tail:
        i = tail[0]
        if max(d0[i:]) >= FALSE_START_AWAY_M and (not out or i > out[0][1]):
            out.append((i, len(coords) - 1))
    return out


def appendix_spurs(loop, coords, cum, level, windows=None) -> list:
    """Petites boucles accrochées sans but de la boucle (ou les tronçons « windows » donnés : faux départs), au format
    des éperons injustifiés (retirées par trim_spurs)."""
    out = []
    lc = ""
    if loop.scenery and (loop.scenery.get("landcover") or {}).get("cls") is not None:
        lc = landcover_seq(loop.scenery["landcover"]["cls"])
    road = loop.road_seq or ""
    for a, b in (find_appendices(coords, cum) if windows is None else windows):
        m0, m1 = cum[a], cum[b]
        if any(r.get("km") is not None and m0 / 1000.0 - 0.1 <= r["km"] <= m1 / 1000.0 + 0.1 for r in loop.remarkable or []):
            continue                                     # passe par un lieu remarquable
        tv = LANDSCAPE.trees.get("view") if LANDSCAPE is not None else None
        if tv is not None:
            import numpy as np
            import shapely
            pts = shapely.points(np.array([(c[0], c[1]) for c in coords[a:b + 1:max(1, (b - a) // 30)]]))
            if len(tv.query(pts, predicate="dwithin", distance=VIEW_NEAR_DEG)[0]):
                continue                                 # passe devant un belvédère
        k0, k1 = int(m0 // 100), max(int(m0 // 100) + 1, int(m1 // 100))
        seg = range(k0, min(k1, len(road)))
        green = sum(1 for k in seg if (road[k] == "c" and (k >= len(lc) or lc[k] != "v")) or (k < len(lc) and lc[k] == "e"))
        if seg and green >= APPENDIX_GREEN * len(seg):
            continue                                     # piste ou voie verte hors de la ville, ou bord de l'eau
        if level == "soutenu" and len(coords[0]) > 2:
            up = sum(max(0.0, coords[k + 1][2] - coords[k][2]) for k in range(a, b))
            if up >= APPENDIX_CLIMB_M:
                continue                                 # une vraie montée, en Sportif
        out.append({"km": round(m0 / 1000.0, 1), "m": round(m1 - m0), "a": a, "b": b,
                    "lon": round(coords[a][0], 5), "lat": round(coords[a][1], 5), "appendix": True})
    return out


def foot_backtrack() -> bool:
    """Allers-retours à pied jamais utiles (FOOT_BACKTRACK), réglable par fil d'exécution."""
    return bool(getattr(_TL, "foot_backtrack", FOOT_BACKTRACK))


def foot_edges(det, n) -> list:
    """Par tronçon : True si on ne peut pas y rouler. 3e version (16/10/2026) : les escaliers seulement ; un chemin
    piéton d'OpenStreetMap se roule souvent en vrai (montée au château de Montjuïc, où Florent roule jusqu'à
    l'esplanade : 2e version encore 33 recommandées moins bonnes pour 8 meilleures)."""
    rc = per_edge(det.get("road_class"), n)
    return [rc[i] == "steps" for i in range(n)]


def corridor_overlap() -> bool:
    """Répétition mesurée par couloir (CORRIDOR_OVERLAP), réglable par fil d'exécution pour les diagnostics."""
    return bool(getattr(_TL, "corridor_overlap", CORRIDOR_OVERLAP))


def cross_neighbours() -> bool:
    """Parents des durées voisines pour le croisement (CROSS_NEIGHBOURS), réglable par fil d'exécution."""
    return bool(getattr(_TL, "cross_neighbours", CROSS_NEIGHBOURS))


def scenic_areas() -> bool:
    """Zones agréables (SCENIC_AREAS), réglables par fil d'exécution pour les diagnostics."""
    return bool(getattr(_TL, "scenic", SCENIC_AREAS))


def cross_segments() -> bool:
    """Croisement tronçon par tronçon (CROSS_SEGMENTS), réglable par fil d'exécution."""
    return bool(getattr(_TL, "cross_segments", CROSS_SEGMENTS))


def segment_quality(l, m0: float, m1: float) -> float:
    """Note (0 à 1, même esprit que score_parts) du morceau de la boucle l entre m0 et m1 mètres : feux, grands axes,
    pistes, ville, nature, terre et relief selon l'allure."""
    n = max(1, int((m1 - m0) // 100))
    i0, i1 = int(m0 // 100), int(m0 // 100) + n
    road = (l.road_seq or "")[i0:i1]
    surf = (l.surface_seq or "")[i0:i1]
    lc = ""
    if l.scenery and (l.scenery.get("landcover") or {}).get("cls") is not None:
        lc = landcover_seq(l.scenery["landcover"]["cls"])[i0:i1]
    share = lambda s, ch: (sum(1 for c in s if c in ch) / len(s)) if s else 0.0  # noqa: E731
    km = max((m1 - m0) / 1000.0, 0.1)
    lights = sum(1 for p in getattr(l, "signal_m", []) if m0 <= p <= m1) / km
    q = (WEIGHTS["lights"] * (1.0 - min(2.0, lights / LIGHTS_PER_KM_ZERO_SCORE))
         + WEIGHTS["axes"] * (1.0 - min(1.0, 2.0 * share(road, "m")))
         + WEIGHTS["infra"] * min(1.0, 2.0 * share(road, "c"))
         + WEIGHTS["calm"] * (1.0 - share(lc, "v"))
         + WEIGHTS["scenery"] * share(lc, "ef")
         - 0.5 * share(surf, "up"))
    c = l.coords
    seg = [p for p, d in zip(c, _cum_cache(l)) if m0 <= d <= m1]
    if len(seg) > 1 and len(seg[0]) > 2:
        up = sum(max(0.0, b[2] - a[2]) for a, b in zip(seg, seg[1:])) / km
        lim = (relief_limits().get(l.level) or {})
        target = lim.get("max_dpk") or lim.get("pen_above")
        if target:
            q -= 0.02 * max(0.0, up - target)
    return q


def _cum_cache(l) -> list:
    if getattr(l, "_cum", None) is None:
        cum = [0.0]
        for a, b in zip(l.coords, l.coords[1:]):
            cum.append(cum[-1] + haversine(a[0], a[1], b[0], b[1]))
        l._cum = cum
    return l._cum


def seg_swaps(A, B) -> list:
    """Tronçons où A s'écarte de B puis la rejoint (dans le même sens) et où le tronçon de B note nettement mieux
    (CROSS_SEG_MARGIN) : [(a0, a1, b0, b1, écart de durée en s, gain, coords de B)] (indices dans A et dans B)."""
    import numpy as np
    ca, cb = _cum_cache(A), _cum_cache(B)

    def pick(cum):                                           # points tous les ~50 m
        out, last = [], -1e9
        for i, d in enumerate(cum):
            if d - last >= 50.0:
                out.append(i)
                last = d
        return out
    pa, pb = pick(ca), pick(cb)
    kx = 111320.0 * np.cos(np.radians(A.coords[0][1]))
    xa = np.array([[A.coords[i][0] * kx, A.coords[i][1] * 110540.0] for i in pa])
    xb = np.array([[B.coords[i][0] * kx, B.coords[i][1] * 110540.0] for i in pb])
    d = np.hypot(xa[:, None, 0] - xb[None, :, 0], xa[:, None, 1] - xb[None, :, 1])
    near = d.argmin(axis=1)
    matched = d[np.arange(len(pa)), near] < CROSS_NEAR_M
    if matched.sum() < 4 or matched.all():
        return []
    swaps, k = [], 0
    while k < len(pa):                                       # entre deux points communs consécutifs
        if matched[k]:
            k += 1
            continue
        a0 = k - 1
        while k < len(pa) and not matched[k]:
            k += 1
        if a0 < 0 or k >= len(pa):
            continue
        b0, b1 = int(near[a0]), int(near[k])
        if b1 <= b0 + 1:
            continue                                         # B ne fait pas le même chemin dans le même sens
        ma0, ma1 = ca[pa[a0]], ca[pa[k]]
        mb0, mb1 = cb[pb[b0]], cb[pb[b1]]
        qa, qb = segment_quality(A, ma0, ma1), segment_quality(B, mb0, mb1)
        if qb >= qa + CROSS_SEG_MARGIN:
            dt = B.time_s * (mb1 - mb0) / max(cb[-1], 1.0) - A.time_s * (ma1 - ma0) / max(ca[-1], 1.0)
            swaps.append((pa[a0], pa[k], pb[b0], pb[b1], dt, qb - qa, B.coords))
    return swaps


def fit_swaps(A, swaps: list, target_s: float):
    """Échanges gardés pour tenir la durée (les moins utiles retirés d'abord) : (échanges triés, durée prévue) ou None."""
    est = (A.time_s + sum(x[4] for x in swaps)) / target_s
    if abs(est - 1.0) > CROSS_SEG_TIME:                      # trop long ou trop court : les meilleurs échanges seuls
        swaps = sorted(swaps, key=lambda x: -x[5])
        while swaps and abs((A.time_s + sum(x[4] for x in swaps)) / target_s - 1.0) > CROSS_SEG_TIME:
            swaps.pop()
        if not swaps:
            return None
        est = (A.time_s + sum(x[4] for x in swaps)) / target_s
    return sorted(swaps, key=lambda x: x[0]), est


def best_disjoint(cands: list) -> list:
    """Échanges sans chevauchement sur la boucle de base, de gain total maximal (ordonnancement pondéré)."""
    cands = sorted(cands, key=lambda x: x[1])
    best = [(0.0, [])]                                       # best[k] : meilleur choix parmi les k premiers
    for k, c in enumerate(cands):
        j = k
        while j > 0 and cands[j - 1][1] > c[0]:              # dernier échange qui finit avant le début de c
            j -= 1
        take = (best[j][0] + c[5], best[j][1] + [c])
        best.append(max(best[k], take, key=lambda t: t[0]))
    return best[-1][1]


def seg_multi() -> bool:
    """Tronçon par tronçon à plusieurs boucles (SEG_MULTI), réglable par fil d'exécution."""
    return bool(getattr(_TL, "seg_multi", SEG_MULTI))


def segment_crossover(gh, level: str, duration_h: float, parents: list, log, detail=None) -> list:
    """Boucles assemblées tronçon par tronçon (voir CROSS_SEGMENTS ; à plusieurs boucles avec SEG_MULTI), à partir des
    boucles parents (celles du départ, des durées voisines et des départs voisins)."""
    target_s = duration_h * 3600.0
    plans = []
    for ai, A in enumerate(parents):                         # deux boucles : la mieux classée sert de base
        for B in parents[ai + 1:]:
            fit = fit_swaps(A, seg_swaps(A, B), target_s)
            if fit is not None:
                plans.append((sum(x[5] for x in fit[0]), A, 1, fit[0], fit[1]))
    plans.sort(key=lambda p: -p[0])
    plans = plans[:CROSS_SEG_MAX]
    if seg_multi():                                          # plusieurs boucles : chaque tronçon, le meilleur de toutes
        bases = []
        for l in sorted(parents, key=lambda x: x.score, reverse=True):
            if all(similarity(l, b) < 0.8 for b in bases):
                bases.append(l)
            if len(bases) >= SEG_MULTI_BASES:
                break
        for A in bases:
            cands = [x for B in parents if B is not A for x in seg_swaps(A, B)]
            chosen = best_disjoint(cands)
            if len({id(x[6]) for x in chosen}) < 2:
                continue                                     # un seul donneur : déjà essayé avec les paires
            fit = fit_swaps(A, chosen, target_s)
            if fit is not None and len({id(x[6]) for x in fit[0]}) >= 2:
                plans.append((sum(x[5] for x in fit[0]), A, len({id(x[6]) for x in fit[0]}), fit[0], fit[1]))
    found = []
    for n, (gain, A, donors, swaps, est) in enumerate(plans):
        coords, cur = [], 0
        for a0, a1, b0, b1, _, _, bc in swaps:
            coords += A.coords[cur:a0] + bc[b0:b1 + 1]
            cur = a1 + 1
        coords += A.coords[cur:]
        way = route_waypoints(coords)
        way[0] = way[-1] = [A.coords[0][0], A.coords[0][1]]
        path = gh.via(way, A.profile, pass_through=True)
        loop = analyse(path, level, A.profile, duration_h, 3600 + n, None) if path else None
        ok = (loop is not None and time_ok(loop.time_s, duration_h) and overlap_ok(loop)
              and loop.u_turns <= MAX_UTURNS and dirt_ok(loop))
        if ok:
            found.append(loop)
        if detail is not None:
            detail.append(f"  tronçons {A.score:.1f} + {donors} boucle(s) ({len(swaps)} échange(s), gain {gain:.2f}, "
                          f"durée prévue {est:.2f}) -> "
                          + ("pas d'itinéraire" if loop is None else f"note {loop.score:.1f}, {loop.time_s / 60:.0f} min")
                          + (" : valide" if ok else " : rejeté"))
    log(f"    croisements tronçon par tronçon : {len(found)} valide(s) sur {len(plans)} essai(s)")
    return found


def cross_spatial() -> bool:
    """Boucles des départs voisins pour le croisement (CROSS_SPATIAL), réglable par fil d'exécution."""
    return bool(getattr(_TL, "cross_spatial", CROSS_SPATIAL)) and bool(NEIGHBOUR_SRC)


def neighbour_loops(st, level: str, duration_h: float) -> list:
    """Boucles publiées des départs voisins (voir CROSS_SPATIAL) : [(profil, coords)], la plus proche d'abord."""
    with _NEIGHBOUR_LOCK:
        if "index" not in _NEIGHBOUR_CACHE:
            try:
                _NEIGHBOUR_CACHE["index"] = _read_json(NEIGHBOUR_SRC, "web/data/index.json").get("starts", [])
            except Exception:  # noqa: BLE001 : sans index, pas de voisins
                _NEIGHBOUR_CACHE["index"] = []
        idx = _NEIGHBOUR_CACHE["index"]
    near = sorted((haversine(st["lon"], st["lat"], e["lon"], e["lat"]), e["id"]) for e in idx)
    near = [sid for d, sid in near if 50.0 < d <= CROSS_SPATIAL_KM * 1000.0][:CROSS_SPATIAL_STARTS]
    out = []
    for sid in near:
        with _NEIGHBOUR_LOCK:
            data = _NEIGHBOUR_CACHE.get(sid)
        if data is None:
            try:
                data = _read_json(NEIGHBOUR_SRC, f"web/data/starts/{sid}.json")
            except Exception:  # noqa: BLE001
                data = {"options": []}
            with _NEIGHBOUR_LOCK:
                _NEIGHBOUR_CACHE[sid] = data
        wanted = [round(duration_h * 60)]
        if spatial_shorter() and duration_h in DURATION_SET and DURATION_SET.index(duration_h) > 0:
            wanted.append(round(DURATION_SET[DURATION_SET.index(duration_h) - 1] * 60))   # l'approche compense
        if spatial_graft() and duration_h in DURATION_SET:   # greffe : durées voisines aussi (la durée estimée tranche)
            i_d = DURATION_SET.index(duration_h)
            wanted += [round(DURATION_SET[j] * 60) for j in (i_d - 1, i_d + 1)
                       if 0 <= j < len(DURATION_SET) and round(DURATION_SET[j] * 60) not in wanted]
        for dm in wanted:
            opts = [o for o in data.get("options", []) if o.get("level") == level
                    and round(o.get("duration_target_min", 0)) == dm and o.get("coords")]
            for o in opts[:CROSS_SPATIAL_LOOPS]:
                out.append((o.get("profile") or LEVELS[level]["profiles"][0], o["coords"], sid,
                            f"{o.get('label', '')}, {dm} min", o.get("time_est_min") or dm))
    return out


def crossover() -> bool:
    """Croisement des meilleures boucles (CROSSOVER), réglable par fil d'exécution pour les diagnostics."""
    return bool(getattr(_TL, "crossover", CROSSOVER))


def retouch_shorten() -> bool:
    """Retouche trop longue raccourcie (RETOUCH_SHORTEN), réglable par fil d'exécution pour les diagnostics."""
    return bool(getattr(_TL, "retouch_shorten", RETOUCH_SHORTEN))


def retouch_round_robin() -> bool:
    """Familles d'essais de retouche à tour de rôle (RETOUCH_ROUND_ROBIN), réglable par fil d'exécution."""
    return bool(getattr(_TL, "retouch_rr", RETOUCH_ROUND_ROBIN))


def detour_score() -> bool:
    """Détours restants comptés dans la note (DETOUR_SCORE), réglable par fil d'exécution pour les diagnostics."""
    return bool(getattr(_TL, "detour_score", DETOUR_SCORE))


def detour_fix() -> bool:
    """Détours en pâté de maisons (DETOUR_FIX), réglable par fil d'exécution pour les diagnostics."""
    return bool(getattr(_TL, "detour_fix", DETOUR_FIX))


def relief_limits() -> dict:
    """Règles de relief de l'allure (RELIEF_LIMITS), réglables par fil d'exécution pour les diagnostics."""
    return getattr(_TL, "relief_limits", RELIEF_LIMITS) or {}
# Même audit : 12 % des boucles avaient plus de 3 km de terre (8,5 km à la Molina en 5 h : la limite était une PART du
# parcours) ; les pentes de plus de 20 % étaient presque toutes sur des chemins de terre. Essai : désactivé.
DIRT_MAX_KM = 4.0               # plafond absolu de terre (notée ou probable) ; v16 : 4 km
STEEP_DIRT_GRADE = 0.12         # terre raide : pénalité de la terre doublée sur ces tronçons (STEEP_DIRT_PENALTY)
STEEP_DIRT_PENALTY = True
MAX_OVERLAP = 0.25            # part max de tronçons empruntés 2 fois
MAX_UNPAVED = 0.12
# Aller-retour (Florent, 03/10/2026 : Sant Adrià 1 h, l'aller-retour sur la piste du Besòs note 59 contre 42) : accepté
# au-delà de MAX_OVERLAP si la partie répétée est surtout piste cyclable ou voie verte. Essai : désactivé en production.
OUTBACK_OK = True
OUTBACK_MAX_OVERLAP = 1.0        # un vrai aller-retour est répété à ~100 % (Sant Adrià 1 h : 72 %, note 65 contre 46)
OUTBACK_MIN_CYCLE = 0.6          # part de piste cyclable / voie verte dans la partie répétée
OUTBACK_NEAR_KM = 2.0            # essai (O-38) : allers-retours seulement si l'eau est à moins de N km du départ
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
# Les demandes de Florent après la 2e comparaison à l'aveugle (24/10/2026, « prends-les en compte dès maintenant ») :
# (1) SMALL_MODEL : de petites routes (7 commentaires : Monistrol, Collserola, Folgueroles, la colonia Montserrat…) ; le
#     bonus ajouté à la note n'avait presque pas d'effet : la préférence passe dans le profil de GraphHopper (modèle
#     personnalisé ajouté à chaque requête) : petites routes goudronnées hors de la ville favorisées, grandes routes
#     pénalisées. (2) WATER_HUG : longer la mer, les rivières, les lacs au plus près (Ciutat Vella, Barceloneta, El
#     Masnou, le Besòs, le Llobregat, le Fluvià) : bonus par km à moins de ~50 m de l'eau, et un aller-retour le long de
#     l'eau est accepté comme sur une piste (la promenade de la Barceloneta n'est pas une piste : l'aller-retour de Ciutat
#     Vella 45 min était rejeté). (3) DEST_HERITAGE : faire demi-tour à un endroit naturel (Monistrol : « au Monestir de
#     Sant Benet ou à la colonia Puig ») : les monastères, châteaux et biens patrimoniaux nommés deviennent des
#     destinations, et un aller-retour tout proche est prolongé jusqu'à eux. (5) TOWNS_PEN : éviter les agglomérations
#     et zones industrielles (Martorell, Sant Andreu de la Barca, Montcada - Granollers) : pénalité par km en ville au
#     milieu de la boucle. (6) LOOP_SHAPE : une vraie boucle, claire : pénalité si le début et la fin se croisent (Sant
#     Andreu 2 h) et si la boucle tourne plus que la moyenne (à sortie égale, 8 choix sur 12 pour la moins tournante).
#     (4, détours restants) : le nettoyage et DETOUR_DEST / DEFECT_STRICT. False : comme avant ; hors de l'empreinte.
SMALL_MODEL = False
# Ordre de Florent (24/10/2026) : 1 petite route dans la nature, 2 grosse route dans la nature, 3 route en ville.
# Profil « calm » : ville 0,45, grande route 0,35 (une grande route de montagne passait sous une rue de ville) ; avec ce
# modèle : petite route hors ville 1,5, grande route hors ville 0,35 x 1,6 = 0,56, rue de ville 0,45. Profil « sport »
# (ville 0,5, grande route 0,6) : 1,5 / 0,96 / 0,5. Grande route en ville inchangée (la moins bien placée).
SMALL_MODEL_PRIORITY = [
    {"if": "urban_density == RURAL && (road_class == UNCLASSIFIED || road_class == TERTIARY || "
           "(road_class == TRACK && surface == ASPHALT))", "multiply_by": "1.5"},
    {"else_if": "urban_density == RURAL && road_class == PRIMARY", "multiply_by": "1.6"},
]
WATER_HUG = False
WATER_HUG_DEG = 0.0006          # ~50 m de la mer, d'une rivière ou d'un lac
WATER_HUG_PER_KM = 0.01
WATER_HUG_MAX = 0.10
DEST_HERITAGE = False
DEST_HERITAGE_EXTEND_M = 2500.0
TOWNS_PEN = False
TOWNS_PEN_EDGE = 0.2            # le premier et le dernier cinquième (sortie et retour en ville) ne comptent pas
TOWNS_PEN_PER_KM = 0.005
TOWNS_PEN_MAX = 0.08
LOOP_SHAPE = False
LOOP_SHAPE_CROSS = 0.03         # début et fin qui se croisent (hors 150 m autour du départ)
LOOP_SHAPE_END_M = 2000.0
LOOP_SHAPE_TURNS_FREE = 1.5     # virages marqués (60° et plus, tous les 50 m) par km au-delà desquels on pénalise
LOOP_SHAPE_TURNS_PER = 0.01
LOOP_SHAPE_TURNS_MAX = 0.05


def _flag(name, default):
    return bool(getattr(_TL, name, default))


def small_model() -> bool:
    return _flag("small_model", SMALL_MODEL)


def water_hug() -> bool:
    return _flag("water_hug", WATER_HUG)


def dest_heritage() -> bool:
    return _flag("dest_heritage", DEST_HERITAGE) and bool(POIS) and "patrimoine" in POIS


def towns_pen() -> bool:
    return _flag("towns_pen", TOWNS_PEN)


def loop_shape() -> bool:
    return _flag("loop_shape", LOOP_SHAPE)


def shape_measures(coords, cum) -> tuple:
    """(début et fin se croisent, virages marqués par km) : LOOP_SHAPE."""
    from shapely.geometry import LineString, Point
    total = cum[-1]
    head = [c[:2] for c, m in zip(coords, cum) if m <= LOOP_SHAPE_END_M]
    tail = [c[:2] for c, m in zip(coords, cum) if total - m <= LOOP_SHAPE_END_M]
    cross = False
    if len(head) > 2 and len(tail) > 2:
        home = Point(coords[0][0], coords[0][1]).buffer(150.0 / 111000.0)
        hit = LineString(head).intersection(LineString(tail)).difference(home)
        cross = not hit.is_empty
    pts = [coords[0]]
    for p in coords[1:]:
        if haversine(pts[-1][0], pts[-1][1], p[0], p[1]) >= 50.0:
            pts.append(p)
    turns = sum(1 for a, b, c in zip(pts, pts[1:], pts[2:])
                if abs((bearing(b[0], b[1], c[0], c[1]) - bearing(a[0], a[1], b[0], b[1]) + 180) % 360 - 180) >= 60)
    return cross, turns / max(total / 1000.0, 0.1)
# v20, 1er essai (docs/v20_proposition.md, accord de Florent le 22/10/2026) : GOAL_RETURN. Pour chaque but à la bonne
# distance (lieu remarquable, col ou sommet, mer, rivière, forêt : attraction_points), l'aller est calculé seul, puis le
# retour en demandant à GraphHopper d'éviter le couloir de l'aller (zone de GOAL_AVOID_DEG autour de l'aller, hors des
# GOAL_FREE_M autour du départ et du but, priorité multipliée par GOAL_AVOID_PRIORITY : pénalisée, pas interdite ; si
# le seul retour agréable est le même chemin, il est repris, comme le veut Florent). False : comme avant.
GOAL_RETURN = False
GOAL_AVOID_DEG = 0.0009
GOAL_FREE_M = 300.0
GOAL_AVOID_PRIORITY = 0.15
GOAL_MIN_SHARE, GOAL_MAX_SHARE = 0.2, 0.45     # but à 20-45 % de la longueur de la boucle, à vol d'oiseau
GOAL_VILLAGES = 0                # villages visés au plus (les plus proches de 1/3 de la boucle, directions différentes).
#                                 0 (Florent, 22/10/2026) : un village quelconque n'est pas un but, seulement s'il est
#                                 particulièrement joli ; critère à trouver (v20). Les lieux remarquables restent visés.
VILLAGES = None                  # villages OSM (place=village), chargés dans main() si GOAL_RETURN


def goal_return() -> bool:
    """Aller vers un but, retour qui évite l'aller (GOAL_RETURN), réglable par fil d'exécution."""
    return bool(getattr(_TL, "goal_return", GOAL_RETURN))


def concat_paths(p1: dict, p2: dict) -> dict:
    """Itinéraire p1 puis p2 (p2 part du point d'arrivée de p1), détails compris."""
    a, b = p1["points"]["coordinates"], p2["points"]["coordinates"]
    det = {}
    for k in set(p1.get("details") or {}) | set(p2.get("details") or {}):
        e1 = _edges_raw((p1.get("details") or {}).get(k), len(a) - 1)
        e2 = _edges_raw((p2.get("details") or {}).get(k), len(b) - 1)
        det[k] = _intervals(e1 + e2)
    return {"points": {"coordinates": a + b[1:]}, "details": det,
            "ascend": float(p1.get("ascend", 0.0)) + float(p2.get("ascend", 0.0))}


def goal_candidates(gh, st, level, profile, duration_h, log) -> list:
    """Boucles « aller vers un but, retour par un autre chemin » (GOAL_RETURN)."""
    import shapely
    from shapely.geometry import LineString, mapping
    lon, lat = st["lon"], st["lat"]
    flat_ms = speed_from_power(level_watts(level, duration_h), 0.0) * REAL_WORLD_FACTOR
    loop_km = 0.8 * flat_ms * duration_h * 3600.0 / 1000.0
    goals = [(cat, x, y) for cat, (x, y, km) in attraction_points(lon, lat, GOAL_MAX_SHARE * loop_km).items()
             if km >= GOAL_MIN_SHARE * loop_km]
    if POIS:                                                 # cols et sommets nommés à la bonne distance
        pt = shapely.Point(lon, lat)
        for k in ("pass", "peak"):
            if k not in POIS:
                continue
            tree, items = POIS[k]
            near = [(items[int(i)][0], haversine(lon, lat, items[int(i)][0].x, items[int(i)][0].y) / 1000.0)
                    for i in tree.query(pt.buffer(GOAL_MAX_SHARE * loop_km / 90.0))]
            near = [(q, km) for q, km in near if GOAL_MIN_SHARE * loop_km <= km <= GOAL_MAX_SHARE * loop_km]
            if near:
                q = min(near, key=lambda z: z[1])[0]
                goals.append((k, q.x, q.y))
    if GOAL_VILLAGES and VILLAGES is not None and VILLAGES.points:   # villages à la bonne distance
        ideal = loop_km / 3.0
        cand = sorted(((abs(haversine(lon, lat, x, y) / 1000.0 - ideal), x, y) for x, y in VILLAGES.points
                       if GOAL_MIN_SHARE * loop_km <= haversine(lon, lat, x, y) / 1000.0 <= GOAL_MAX_SHARE * loop_km))
        picked = []
        for _, x, y in cand:
            b = bearing(lon, lat, x, y)
            if all(abs((b - b2 + 180) % 360 - 180) >= 60 for b2 in picked):
                goals.append(("village", x, y))
                picked.append(b)
            if len(picked) >= GOAL_VILLAGES:
                break
    kx = 111320.0 * math.cos(math.radians(lat))
    found, tried = [], 0
    for cat, gx, gy in goals:
        tried += 1
        out = gh.via([[lon, lat], [gx, gy]], profile)
        if out is None or len(out["points"]["coordinates"]) < 3:
            continue
        line = LineString([(c[0], c[1]) for c in out["points"]["coordinates"]]).simplify(0.0002)
        zone = line.buffer(GOAL_AVOID_DEG)
        free = GOAL_FREE_M / kx
        zone = zone.difference(shapely.Point(lon, lat).buffer(free)).difference(shapely.Point(gx, gy).buffer(free))
        if zone.is_empty:
            continue
        cm = {"priority": [{"if": "in_aller", "multiply_by": str(GOAL_AVOID_PRIORITY)}],
              "areas": {"type": "FeatureCollection",
                        "features": [{"type": "Feature", "id": "aller", "properties": {},
                                      "geometry": mapping(zone.simplify(0.0002))}]}}
        back = gh.via([[gx, gy], [lon, lat]], profile, custom_model=cm)
        if back is None or len(back["points"]["coordinates"]) < 3:
            continue
        loop = analyse(concat_paths(out, back), level, profile, duration_h, 6000 + tried, None)
        if (loop is None or not time_ok(loop.time_s, duration_h) or not overlap_ok(loop)
                or loop.u_turns > MAX_UTURNS or not dirt_ok(loop)):
            continue
        loop.goal = cat
        found.append(loop)
    log(f"    buts : {len(found)} boucle(s) valide(s) sur {tried} but(s)")
    return found
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
# Familles à tour de rôle (09/10/2026, la Plata Modéré 1 h 30 : le retour de Florent par le Besòs note 58,5 contre 51,8,
# mais les 10 essais de la boucle publiée sont pris par « lieu », « mer » et « vert » ; la rivière n'est jamais essayée) :
# un essai de chaque famille, dans l'ordre de rentabilité, puis on recommence. False : désactivé (production v16).
# À l'activation, l'ajouter à l'empreinte (params_hash).
RETOUCH_ROUND_ROBIN = False
# Retouche trop longue raccourcie (09/10/2026, la Plata Modéré 1 h 30, 44 essais sans préfiltre : le retour par le Besòs
# note 63,5 contre 51,8 mais dure 1 h 45 pour une limite de 1 h 43, et il est jeté ; la retouche ajoute un passage sans
# rien raccourcir ailleurs). RETOUCH_SHORTEN : un essai qui gagne au moins RETOUCH_SHORTEN_GAIN de note mais dépasse la
# durée est recalculé une fois sans le point de passage d'origine dont le retrait rapproche le plus la durée prévue de
# RETOUCH_SHORTEN_AIM ; gardé s'il passe tous les filtres et note mieux que la boucle de départ. False : désactivé
# (production v16). À l'activation, l'ajouter à l'empreinte (params_hash).
RETOUCH_SHORTEN = False
# Croisement des meilleures boucles (09/10/2026 : la boucle de Florent à la Plata 1 h 30, notée 58,5 contre 51,8, est le
# début de la boucle Oyan de 1 h 30 et la fin de celle de 2 h ; les deux morceaux existaient, rien ne les assemblait).
# CROSSOVER : les CROSS_TOP meilleures boucles du pool, deux à deux ; là où elles passent à moins de CROSS_NEAR_M l'une
# de l'autre (entre 20 et 80 % du parcours), début de l'une + fin de l'autre, au point dont la durée prévue est la plus
# proche de la cible ; CROSS_MAX_TRIALS essais au plus. False : désactivé (production v16). À l'activation, l'ajouter à
# l'empreinte (params_hash).
CROSSOVER = True                  # v17
CROSS_TOP = 4
CROSS_NEAR_M = 150.0
CROSS_MAX_TRIALS = 12
# Parents des durées voisines (09/10/2026, la Plata 1 h 30 : la boucle de Florent assemble la boucle publiée de 1 h 30 et
# celle de 2 h ; le croisement seul ne combine que des boucles de la même durée) : CROSS_NEIGHBOURS ajoute comme parents
# les CROSS_NEIGHBOUR_LOOPS premières boucles publiées (KEEP_PREVIOUS) des durées voisines, recalculées. False : désactivé.
CROSS_NEIGHBOURS = True           # v17
CROSS_NEIGHBOUR_LOOPS = 2
# Boucles des départs voisins (Florent, 14/10/2026, Gràcia Modéré 2 h « plus de relief » : l'Eixample, à 1,5 km, passe
# par le coll del Portell, Gràcia non ; chaque départ est calculé seul et ne voit pas les boucles de ses voisins).
# CROSS_SPATIAL : les boucles publiées (même durée, même allure) des CROSS_SPATIAL_STARTS départs les plus proches à moins
# de CROSS_SPATIAL_KM, adaptées au départ (du départ au point le plus proche de la boucle voisine, la boucle, retour),
# deviennent des candidates et des parents du croisement. Source : site publié (NEIGHBOUR_SRC). False : désactivé.
CROSS_SPATIAL = True                    # v19
# Croisement tronçon par tronçon (Florent, 14/10/2026 : « si A est meilleure sur un tronçon 1 et B sur un tronçon 2,
# la boucle doit prendre le tronçon 1 de A et le tronçon 2 de B ») : là où deux boucles parents se séparent puis se
# rejoignent, chaque tronçon est noté (feux, grands axes, pistes, ville, nature, terre, relief selon l'allure) et le
# meilleur des deux est pris, si la durée prévue reste à CROSS_SEG_TIME près ; CROSS_SEG_MAX essais au plus. La note de la
# boucle entière tranche ensuite. False : désactivé.
CROSS_SEGMENTS = True                    # v19
# Zones agréables pour GraphHopper (Florent, 14/10/2026 : l'Eixample 2 h passe par le couloir de la B-10 plutôt que par la
# piste du bord de mer de la Barceloneta, autorisée dans OSM ; GraphHopper ne connaît pas le paysage) : profils
# « <profil>_scenic » = profil + scripts/scenic_areas.py (bandes le long du littoral et des rivières, grands parcs, bonus
# ×1,3 sur les voies calmes). SCENIC_AREAS : tous les itinéraires demandés avec ces profils. False : désactivé (essai :
# copie de configuration, diagnostic z1 / z2).
SCENIC_AREAS = False
SCENIC_PROFILES = ("calm", "sport", "calm_flat")
CROSS_SEG_MAX = 8
# Tronçon par tronçon à plusieurs boucles (Florent, 15/10/2026 : A et B se croisent en 1, 2 et 3, B est meilleure de 1 à 2,
# A de 2 à 3 : A doit prendre le tronçon 1-2 de B et garder le reste ; et si une 3e boucle C est meilleure de 2 à 3, la
# prendre aussi). Le croisement par paires ne combinait que deux boucles. SEG_MULTI : pour chacune des SEG_MULTI_BASES
# meilleures boucles (de ce départ ou d'un départ voisin), chaque tronçon où elle s'écarte d'une autre boucle est remplacé
# par le meilleur tronçon parmi toutes les autres (échanges sans chevauchement, gain total maximal), si la durée tient.
# False : par paires seulement.
SEG_MULTI = True                    # v19
SEG_MULTI_BASES = 3
CROSS_SEG_MARGIN = 0.03          # un tronçon remplace l'autre s'il note au moins 3 points de mieux (sur 100)
CROSS_SEG_TIME = 0.12
CROSS_SPATIAL_KM = 3.0
CROSS_SPATIAL_STARTS = 3
CROSS_SPATIAL_LOOPS = 2
# Traçage m8 (14/10/2026) : la boucle 1 h 30 de Horta, adaptée à Gràcia, note 64,0 (Gràcia seule : 59,7) mais dure 118 min
# avec l'approche. SPATIAL_SHORTER : les boucles des départs voisins de la durée inférieure aussi (l'approche compense).
SPATIAL_SHORTER = False
# Greffe (approfondissement demandé par Florent, 15/10/2026) : la boucle voisine était reprise ENTIÈRE, plus l'aller et le
# retour jusqu'à elle ; elle gardait donc sa propre sortie de ville (le tronçon proche de SON départ) en plus de la nôtre :
# trop longue (Horta 1 h 30 adaptée à Gràcia : 118 min) ou répétée. SPATIAL_GRAFT : on rejoint la boucle voisine à son
# point le plus proche de notre départ dans sa première moitié, on la quitte à son point le plus proche dans sa seconde
# moitié (sa sortie de ville est remplacée par la nôtre) ; boucles voisines de la durée inférieure, de la même et de la
# supérieure, gardées si la durée estimée est à GRAFT_TIME_TOL près. False : boucle voisine entière seulement.
SPATIAL_GRAFT = True                    # v19
GRAFT_TIME_TOL = 0.25
NEIGHBOUR_SRC = None             # adresse du site publié (ou dossier contenant web/data/), fixée au lancement
_NEIGHBOUR_CACHE: dict = {}
_NEIGHBOUR_LOCK = threading.Lock()
RETOUCH_SHORTEN_GAIN = 0.03
RETOUCH_SHORTEN_AIM = 0.97
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
# Terre v17 (Florent, 08/10/2026 : « limiter la terre au maximum » ; en v16, 13 % des boucles recommandées ont plus de
# 1 km de terre) : plafond max_km avec REPLI (sans boucle sous le plafond, la moins terreuse est gardée et annoncée,
# unpaved_fallback : plus aucun départ retiré) et pen_per_km par km. None : désactivé (production v16).
DIRT_RULES = None
DIRT_RULES_V17 = {"max_km": 1.0, "pen_per_km": 0.06}
DIRT_RULES = DIRT_RULES_V17       # v17
FALLBACK_UNPAVED_SHARE = 0.20
MAX_UTURNS = 2                # demi-tours acceptés (impasses parcourues aller-retour)
WEIGHTS = {"calm": 0.22, "lights": 0.30, "axes": 0.14, "infra": 0.12, "flow": 0.18, "scenery": 0.12}   # feux 0,22 -> 0,30 (v16)
# « ville » de la répartition du terrain (O-9.4) : au moins LANDCOVER_BLD_MIN bâtiments OSM à moins de ~75 m du point.
# Les zones bâties « landuse » ne marchent pas à Barcelone (îlots dessinés sans les rues) ; calage : diagnostic
# landcover_check n°2 du 26/09/2026 (Gràcia 1 h : 89 % ; Collserola 23-33 %). Partagé avec scripts/landcover.py.
LANDCOVER_BLD_DEG = 0.0009
LANDCOVER_BLD_MIN = 5
FLOW_OVERLAP_FACTOR = 1.0     # pénalité des tronçons répétés (2.0 jusqu'à la v4 ; D21 : diagnostic nature_check v5)
LIGHTS_PER_KM_ZERO_SCORE = 2.5  # à 2,5 feux/km, le sous-score "feux" tombe à 0 ; −1 à 5 feux/km
# zones industrielles et portuaires (OSM landuse=industrial, port) : sortie de Florent, zone franche et port de Barcelone
# « au milieu des entrepôts, travaux et camions : pas agréable, un peu dangereux, pollué » ; 14 % du parcours -> −7 points
INDUSTRIAL_PENALTY = 1.0          # v16 (0,5 avant) : sonde « pénalités » du 05/10/2026
INDUSTRIAL_MAX_PENALTY = 0.30      # v16 (0,15 avant)
# Options secondaires (Florent, 05/10/2026 : « Plus de pistes » par le port revenue en v15) : jamais nettement pire que la
# recommandée sur les feux (+25 % et au moins +0,4 / km) ni à 10 % de zone industrielle ; une autre candidate est cherchée
# (même règle que le filtre de l'appli). Essai : désactivé en production.
SECONDARY_GUARD = True
SECONDARY_MAX_LIGHTS_RATIO = 1.25
SECONDARY_MAX_LIGHTS_GAP = 0.4
SECONDARY_MAX_INDUSTRIAL = 0.10
# Éperons (Florent, 05/10/2026 : allers-retours « sans raison » de quelques centaines de mètres, 62 % des boucles
# publiées en ont au moins un de 100 m ou plus). Un éperon = la boucle repart en sens inverse dans un couloir de
# SPUR_CORRIDOR_M ; il est justifié s'il monte d'au moins SPUR_JUSTIFIED_DPLUS_M, mène à un lieu remarquable ou un
# belvédère, ou longe surtout l'eau, la forêt ou un parc. Les autres sont pénalisés et, pour les meilleures candidates,
# coupés (boucle recalculée sans l'éperon). Essai : désactivé en production.
SPUR_FIX = True
SPUR_CORRIDOR_M = 40.0
SPUR_MIN_M = 150.0              # éperon pris en compte à partir de cette longueur (aller seul)
SPUR_END_M = 300.0              # au départ ou à l'arrivée (rue en cul-de-sac du départ) : ignoré
SPUR_JUSTIFIED_DPLUS_M = 40.0
SPUR_PLEASANT_SHARE = 0.6
SPUR_PENALTY = 0.04             # 4 points par éperon injustifié…
SPUR_PENALTY_PER_100M = 0.01    # … plus 1 point par 100 m
SPUR_MAX_PENALTY = 0.20
# Détours en pâté de maisons (Florent, 08/10/2026, Sants-Montjuïc 1 h 30 Tranquille : Paral·lel -> Calàbria -> retour,
# 810 m pour revenir à 118 m). Cause : un point de passage repris d'une boucle (retouche, boucle précédente, éperon coupé)
# accroché à la chaussée d'en face d'une avenue ; sans demi-tour permis (pass_through), GraphHopper fait le tour du pâté
# de maisons. DETOUR_FIX : (1) le sens de circulation de la boucle d'origine est donné pour chacun de ces points
# (headings, bonne chaussée) ; (2) détour restant (retour à moins de DETOUR_GAP_M après DETOUR_MIN_M à DETOUR_MAX_M, sans
# monter ni repasser par la même rue, hors départ et arrivée) : les meilleures candidates sont recalculées sans lui, et
# la version sans détour remplace l'originale si sa note ne baisse pas de plus de DETOUR_SCORE_SLACK. Pas de pénalité :
# la détection compte aussi des ronds-points et des échangeurs (échantillon v16 : la moitié des boucles), le recalcul
# les laisse en place et ils restent donc sans effet.
# False : désactivé (production v16). À l'activation, l'ajouter à l'empreinte (params_hash).
DETOUR_FIX = True                 # v17
# Répétition mesurée par couloir (Florent, 08/10/2026, Gràcia Modéré 1 h « plus de relief » : trois passages sur la
# carretera de Sant Cugat, 4 % de répétition mesurée). La mesure v16 compte un tronçon répété seulement s'il passe par les
# mêmes points de la carte : l'autre chaussée, une voie parallèle ou un triangle de carrefour ne comptent pas (Gràcia :
# 46 % par couloir ; 1 229 recommandées v16 : 23 au-delà de 25 %, 76 par couloir). CORRIDOR_OVERLAP : un passage à moins
# de CORRIDOR_R_M d'un passage antérieur (au moins CORRIDOR_GAP_M de chemin plus tôt, à moins de CORRIDOR_DZ_M
# d'altitude : les lacets d'une côte, superposés sur la carte mais à des altitudes différentes, ne comptent pas) est
# répété, les deux passages comptant. Sert à MAX_OVERLAP, à la note « fluidité » et aux allers-retours (OUTBACK_OK).
# False : désactivé (production v16). À l'activation, l'ajouter à l'empreinte (params_hash).
CORRIDOR_OVERLAP = True           # v17
# Retours sur ses pas utiles ou inutiles (Florent, 09/10/2026 : « bien différencier les demi-tours utiles — prolonger une
# route agréable, avoir du D+, atteindre un lieu remarquable ou une piste cyclable — des demi-tours inutiles »). En v16,
# chaque mécanisme a ses propres exceptions (éperons : montée, lieu, belvédère, nature ; répétition : allers-retours sur
# piste ; demi-tours : aucune, le demi-tour au château de Montjuïc coûte ~3 points). BACKTRACK_RULES : un passage où la
# boucle revient sur ses pas (demi-tour, éperon, tronçon répété, détour) est utile s'il passe par un lieu remarquable ou
# un belvédère, s'il monte d'au moins BACKTRACK_CLIMB_M en Modéré ou Sportif (pas en Tranquille), ou s'il est surtout
# (BACKTRACK_SHARE) au bord de l'eau, en forêt, dans un parc, ou sur piste cyclable / voie verte ; utile, il ne coûte
# rien (ni demi-tour, ni répétition, ni éperon, ni détour). False : désactivé (production v16). À l'activation,
# l'ajouter à l'empreinte (params_hash).
BACKTRACK_RULES = True            # v17
BACKTRACK_CLIMB_M = 40.0
# Allers-retours à pied (Florent, 15/10/2026, el Gornal 3 h Modéré : la boucle quitte la place du Tibidabo pour monter au
# temple du Sacré-Cœur par des chemins piétons et des escaliers, puis redescend ; la place est déjà à moins de 130 m du
# sommet, le lieu est donc vu sans ce crochet). FOOT_BACKTRACK : un retour sur ses pas qui emprunte plus de
# FOOT_BACKTRACK_MAX_M de chemin piéton, de zone piétonne ou d'escalier sans accès vélo n'est jamais utile (lieu compris) :
# il est pénalisé et retiré comme tout éperon injustifié ; le lieu reste compté par le passage le plus proche
# (remarkable_passed, à moins de 130 m du tracé). False : un lieu justifie l'aller-retour quel que soit le chemin.
# 2e version (1re mesure du 16/10/2026 : 40 recommandées moins bonnes, 7 meilleures ; le château de Montjuïc, où Florent
# roule jusqu'à l'esplanade, perdait tout son aller-retour) : les zones piétonnes (places, esplanades) restent roulables,
# et seule la fin à pied est retirée quand le reste de l'aller-retour est utile (le lieu à moins de 130 m de son bout).
FOOT_BACKTRACK = True                    # v19
# Petites boucles accrochées (Florent, 16/10/2026, Espronceda 3 h Modéré : à Sant Fost, la boucle quitte la B-500, fait
# 2,7 km dans des rues résidentielles et revient au même carrefour pour repartir par où elle est arrivée ; revue de la
# v18 : 1 539 boucles). Une petite boucle accrochée revient au même point et repart par la route d'arrivée (empruntée
# dans les deux sens sur APPENDIX_STEM_M), sans être un aller-retour, et pèse au plus APPENDIX_MAX_SHARE du parcours et
# APPENDIX_MAX_M (au-delà, c'est un « 8 » ou une raquette, choix de Florent). APPENDIX_RULE : elle n'est gardée que si
# elle apporte quelque chose de nommable : un lieu remarquable ou un belvédère ; au moins APPENDIX_GREEN de son parcours
# sur piste cyclable ou voie verte HORS de la ville, ou au bord de l'eau ; en Sportif, au moins APPENDIX_CLIMB_M de
# montée. Sinon elle est pénalisée et retirée comme un éperon injustifié (trim_spurs). False : non vérifiée.
# 2e version (1re mesure du 16/10/2026 : 6 meilleures, 16 moins bonnes ; quand la boucle sans le détour ne tient plus
# dans la durée, la même boucle gardait la lourde pénalité d'un éperon : Tordera 6 h 69 -> 41 sans rien changer) :
# pénalité FIXE de APPENDIX_PENALTY (3 points : à note proche, une autre boucle passe devant, choix de Florent) et, pour
# les APPENDIX_TRIM_TOP meilleures candidates (boucles publiées comprises), la même boucle sans le détour est essayée.
APPENDIX_RULE = True                    # v19
# Demi-tours au bon endroit (Florent, 17/10/2026 : l'Eixample 3 h Modéré monte la Carretera del Tibidabo et fait demi-tour
# au milieu de la route, « excusé » par un point de vue sans nom croisé à 245 m ; l'Eixample 6 h Sportif monte vers le
# monastère de Montserrat et fait demi-tour 830 m avant, aller-retour « nature »). TURN_RULES : (1) un aller-retour
# « belvédère » n'est utile que si son demi-tour est au point de vue ; (2) pour les TURN_EXTEND_TOP meilleures candidates,
# un aller-retour qui fait demi-tour à moins de TURN_EXTEND_M d'un lieu remarquable sans l'atteindre est prolongé
# jusqu'au lieu (gardé si la durée tient ; la note tranche) ; (3) deux allers-retours vers le même endroit (demi-tours à
# moins de 300 m) : seul le premier est utile, le second est un crochet inutile. False : comme la v19. Hors de l'empreinte (V19_PARAMS) tant
# qu'elle est éteinte : la v19 en cours de calcul ne doit pas voir son empreinte changer ; l'y ajouter à l'activation.
TURN_RULES = True                    # v19 (17/10/2026, mesures s3 à s0 puis c5 à c7)
# Faux départs (Florent, 17/10/2026, l'Eixample 5 h Sportif « plus de relief » : la boucle part vers la mer, revient par
# une rue parallèle (sens uniques), repasse par le départ et part à l'opposé pour tout le reste ; « le plus bizarre est de
# partir dans un sens pour finalement repasser par le point de départ »). FALSE_START : dans le premier (ou le dernier)
# FALSE_START_SHARE du parcours, un tronçon qui s'éloigne d'au moins FALSE_START_AWAY_M du départ puis y repasse à moins de
# FALSE_START_NEAR_M est traité comme une boucle accrochée (but nommable sinon 3 points, et la même boucle sans lui essayée).
# Le retour par une rue parallèle compte (aucune exigence de même rue). Hors de l'empreinte tant qu'elle est éteinte.
FALSE_START = True                    # v19 (17/10/2026, mesures s3 à s0 puis c5 à c7)
# Allers-retours dans un couloir (Florent, 17/10/2026 : à Mataró, l'Eixample 5 h Sportif file 1 km sur une avenue à deux
# chaussées, fait demi-tour et revient par l'autre chaussée ; dans l'Eixample, les sens uniques font revenir par une rue
# parallèle). Les allers-retours n'étaient reconnus que sur les mêmes points. CORRIDOR_SPURS : un tronçon qui s'éloigne
# puis revient le long de son aller, à CORRIDOR_NEAR_M au plus (autre chaussée, rue parallèle), sur au moins
# CORRIDOR_MIN_M à l'aller, suit la règle des allers-retours (utile : gardé ; sinon éperon injustifié, pénalisé et
# retiré). Hors de l'empreinte tant qu'elle est éteinte.
CORRIDOR_SPURS = True                    # v19 (17/10/2026, mesures s3 à s0 puis c5 à c7)
# Grands axes au-delà d'un seuil (Florent, 17/10/2026, Sarrià 3 h Modéré : la boucle choisie passe 45 % sur de grands axes
# et note 60,5 contre 59,9 pour la boucle v18 à 2 % ; la note pesait trop peu les grands axes face à un peu de relief).
# AXES_OVER : au-delà de AXES_OVER_SHARE du parcours sur de grandes routes, AXES_OVER_PER point par point de part en plus
# (au plus AXES_OVER_MAX) : 30 % -> 5 points, 45 % -> 12,5. En dessous, rien ne change. Hors de l'empreinte tant
# qu'elle est éteinte.
AXES_OVER = True                    # v19 (17/10/2026, mesures s3 à s0 puis c5 à c7)
AXES_OVER_SHARE = 0.20
AXES_OVER_PER = 0.5
AXES_OVER_MAX = 0.15
# Relecture de l'Eixample par Florent (17/10/2026, 10 captures : « beaucoup de boucles problématiques »). Trois causes :
# (1) CLEAN_PICKS : les détours en pâté de maisons, les éperons et les boucles accrochées étaient bien repérés (22 boucles
# de l'Eixample sur 51 gardaient un détour) mais réparés seulement sur les 3 meilleures candidates de chaque tirage ; les
# options publiées (« moins de relief », « plus de relief ») venaient souvent d'ailleurs et n'étaient que pénalisées
# (Diagonal - Villarroel, Diagonal Mar, Vila Olímpica, carrer Transversal, place d'Espagne, Sant Cugat). Chaque boucle
# CHOISIE qui en garde est recalculée sans eux (points de passage de part et d'autre), puis le choix est refait
# (CLEAN_PICKS_ROUNDS fois au plus) ; la version propre doit rester valide (durée, ordre des durées, répétition, terre,
# relief) et noter au moins la boucle d'origine moins CLEAN_PICKS_SLACK.
# (2) CITY_BACKTRACK : en ville dense (au moins CITY_BACKTRACK_SHARE de l'aller-retour en densité « ville »), un
# aller-retour n'est utile que vers un lieu remarquable, un belvédère ou un sommet ; « nature », « piste » ou « montée »
# (eau du port, parc) ne suffisent plus (le Moll d'Espanya, aller-retour « nature » sur le quai). Sinon : éperon injustifié.
# (3) NESTED_BACKTRACK : un aller-retour utile qui en contient un autre (on remonte la même rue pour aller plus loin)
# n'est plus utile, seul le plus court reste (l'Eixample 2 h Sportif, deux fois la Carretera de la Gran Vista).
# False : comme avant. Hors de l'empreinte tant qu'elles sont éteintes.
CLEAN_PICKS = False
CLEAN_PICKS_ROUNDS = 2
CLEAN_PICKS_SLACK = 0.03
# 2e essai (Florent, 17/10/2026, tracé à l'appui : « pour le moll il faudrait simplement faire ce trajet ») : un
# aller-retour sans intérêt est d'abord transformé en petite boucle (aller par un côté, retour par l'autre, points à
# LOOPIFY_OFFSET_M de part et d'autre du milieu de l'aller), puis retiré si aucune boucle propre n'existe ; la mieux
# notée des deux versions valides est gardée.
LOOPIFY_MIN_M = 300.0
LOOPIFY_OFFSET_M = 120.0
# 3e essai (mesure du 17/10/2026 : 274 boucles à détour sur 673, encore 268 après) : les points gardés de part et d'autre
# d'un détour sont à moins de 120 m l'un de l'autre, souvent sur une chaussée ou un sens unique qui y oblige ; le chemin
# est donc repris CLEAN_MARGIN_M avant et après chaque défaut.
# 4e essai (mesure du 18/10/2026 : encore 390 boucles à détour sur 1 083 ; 315 essais de retrait sur 363 ajoutaient
# autant de défauts ailleurs, car toute la boucle était recalculée par des points tous les 2 km, chacun pouvant tomber du
# mauvais côté d'une avenue) : seul le morceau autour du défaut est recalculé et recousu (splice_path).
CLEAN_MARGIN_M = 250.0
CITY_BACKTRACK = False
CITY_BACKTRACK_SHARE = 0.6
NESTED_BACKTRACK = False
# Revue à l'aveugle de Florent (21/10/2026, 23 boucles v19 : 6 « je la ferais », 12 « bof », 5 « non » ; la note
# d'Oyan ne prédit pas son jugement). Les allers-retours sont le 1er reproche (10 boucles) : « pourquoi là ? ».
# BACKTRACK_DEST (remplace D80, accord de Florent) : un aller-retour « piste » ou « montée » n'est utile que s'il fait
# demi-tour à une destination (destination_tip) ; sinon éperon injustifié, retiré ou bouclé par le nettoyage. Un
# aller-retour « nature » sans destination reste permis (le long de la mer ou d'une rivière, mieux qu'un retour par une
# route sans intérêt, Florent) mais coûte BACKTRACK_DEST_NATURE_PEN (au plus BACKTRACK_DEST_NATURE_MAX), et le
# nettoyage essaie d'en faire une boucle, gardée seulement si elle note mieux sans ajouter de grands axes.
# REPEAT_PLAIN : les 6 boucles qu'il ferait repassent sur moins de 5 % du parcours, aucune au-delà n'est « je la
# ferais ». Seule la répétition sur des routes ordinaires compte (pas au bord de l'eau, en forêt, ni sur piste cyclable
# hors de la ville : « je préfère revenir le long de la mer plutôt que par une route différente mais pas agréable ») :
# au-delà de REPEAT_PLAIN_FREE, REPEAT_PLAIN_PER par point de part en plus (au plus REPEAT_PLAIN_MAX).
# False : comme avant. Hors de l'empreinte tant qu'elles sont éteintes.
BACKTRACK_DEST = False
BACKTRACK_DEST_NATURE_PEN = 0.05
BACKTRACK_DEST_NATURE_MAX = 0.10
REPEAT_PLAIN = False
REPEAT_PLAIN_FREE = 0.05
REPEAT_PLAIN_PER = 0.6
REPEAT_PLAIN_MAX = 0.15
# SPUR_TURN_WINDOW (même revue : Olot 45 min Sportif vers la C-152, Collbató 1 h 30 Tranquille carrer Ramon Casas) : un
# demi-tour par un rond-point ou une petite boucle au bout tourne de 150° en plusieurs points, et l'aller-retour n'était
# pas vu (ni jugé, ni pénalisé, ni retiré). Il est reconnu sur SPUR_TURN_WINDOW_M au plus, si le retour repasse à moins
# de SPUR_CORRIDOR_M de l'aller à la même altitude (CORRIDOR_DZ_M : pas les lacets d'une montée). False : comme avant.
SPUR_TURN_WINDOW = False
SPUR_TURN_WINDOW_M = 150.0
CORRIDOR_NEAR_M = 100.0         # 1re mesure (17/10/2026) à 150 m : 12 meilleures, 38 moins bonnes (couloir trop large)
CORRIDOR_MIN_M = 400.0
FALSE_START_SHARE = 0.33
FALSE_START_AWAY_M = 600.0
FALSE_START_NEAR_M = 250.0
TURN_EXTEND_M = 1500.0
TURN_EXTEND_TOP = 4
# (4) un aller-retour « nature » ou « piste » qui monte d'au moins TURN_CLIMB_M n'est utile que s'il fait demi-tour à un
# sommet, un col ou au bord de l'eau (natural_tip ; Florent, 17/10/2026, l'Eixample 1 h 30 Sportif : demi-tour à 192 m
# en pleine montée vers Sant Pere Màrtir ; Montserrat : demi-tour 830 m avant le monastère).
TURN_CLIMB_M = 20.0
# 2e version (mesure du 17/10/2026 : rendu « inutile », l'aller-retour faisait dépasser la limite de répétition et la boucle
# était éliminée : Esparreguera 6 h Tranquille 64 -> 29) : il reste utile (« montée ») mais coûte TURN_CLIMB_PEN.
TURN_CLIMB_PEN = 0.04
TURN_CLIMB_MAX = 0.08
# (5) un aller-retour « nature » ou « piste » sans destination (demi-tour ni à un lieu remarquable, ni à un belvédère, ni
# à un sommet ou un col, ni au bord de la mer ou d'un lac) reste permis mais coûte TURN_AIMLESS_PEN (au plus
# TURN_AIMLESS_MAX) : à note proche, une boucle ou un demi-tour plus naturel passe devant (Florent, 17/10/2026 : « pas les
# interdire, plutôt les pénaliser peu » ; le Besòs depuis l'Eixample 2 h Tranquille).
TURN_AIMLESS_PEN = 0.02
TURN_AIMLESS_MAX = 0.04
APPENDIX_PENALTY = 0.03
APPENDIX_TRIM_TOP = 4
APPENDIX_MIN_M = 800.0
APPENDIX_MAX_M = 5000.0
APPENDIX_MAX_SHARE = 0.10
APPENDIX_STEM_M = 200.0
APPENDIX_NEAR_M = 40.0
APPENDIX_GREEN = 0.5
APPENDIX_CLIMB_M = 60.0
FOOT_BACKTRACK_MAX_M = 20.0     # 3e version : escaliers seulement, 20 m suffisent (on porte le vélo)
BACKTRACK_SHARE = 0.6
# audit du 09/10/2026 : 429 des 661 allers-retours « nature » et 33 des 53 « piste » faisaient moins de 500 m (demi-tour
# au bout d'un chemin forestier ou d'une bretelle) : « prolonger une route agréable » demande au moins ce trajet
BACKTRACK_PLEASANT_MIN_M = 1000.0
# Revue de cohérence du 15/10/2026 : 320 allers-retours « utiles » font plus de 25 % du parcours, jusqu'à 47 km (Sant Martí
# 1 h Tranquille refait 10 km sur 16 pour la « nature ») : la boucle devient un aller-retour. BACKTRACK_MAX_SHARE : au-delà
# de cette part du parcours (aller et retour comptés), un retour sur ses pas n'est jamais utile (pénalisé, retiré comme
# un éperon). None : pas de plafond (production v18).
BACKTRACK_MAX_SHARE = None
BACKTRACK_MAX_SHARE_TRIAL = 0.20
# Mesure du plafond (16/10/2026, 16 départs) : 30 -> 0 longs allers-retours, mais 37 recommandées moins bonnes et 2 durées
# perdues (Sant Martí 1 h Tranquille 55 -> 39, Bescanó 45 min 87 -> 57) : ces allers-retours longent souvent de beaux
# endroits ; le vrai défaut est qu'ils ne sont pas annoncés (choix de Florent : annoncer et pénaliser, pas interdire).
# BACKTRACK_LONG : un aller-retour utile de plus de BACKTRACK_LONG_SHARE du parcours est annoncé dans l'app
# (long_backtrack) et coûte BACKTRACK_LONG_PEN + BACKTRACK_LONG_PEN_PER par part au-delà (au plus BACKTRACK_LONG_MAX) :
# à qualité proche une vraie boucle passe devant, une boucle bien plus belle reste. False : gratuit et non annoncé.
BACKTRACK_LONG = True                    # v19
BACKTRACK_LONG_SHARE = 0.25
BACKTRACK_LONG_PEN = 0.03
BACKTRACK_LONG_PEN_PER = 0.20
BACKTRACK_LONG_MAX = 0.12
# relecture de Florent (09/10/2026, 29 cas : lieux 6/6 utiles, belvédères 5/6, relief 1 utile et 4 « demi-tour soudain »,
# nature 1/6, piste 3/6) : (1) jamais sur un chemin de terre ou de gravier (« galérer sur un chemin de terre pour qu'on
# me dise de revenir sur mes pas »), sauf pour atteindre un lieu remarquable ; (2) un demi-tour se fait à un endroit
# naturel (natural_tip : sommet ou col nommé, rivière ou plan d'eau), sinon « pourquoi ici ? » ; (3) une piste
# cyclable seule ne suffit pas (piste entre deux routes et des feux) : elle doit mener dans un endroit au moins un peu
# agréable (BACKTRACK_PISTE_GREEN de bord de l'eau, forêt ou parc)
BACKTRACK_DIRT_MAX_M = 50.0
BACKTRACK_PISTE_GREEN = 0.3
# mesure combinée du 10/10/2026 : un aller-retour utile ne comptant plus du tout comme répétition, les boucles à long
# aller-retour gagnaient (répétées à plus de 25 % : 6 -> 30 en Tranquille) ; il compte désormais pour cette part
BACKTRACK_REPEAT_WEIGHT = 0.5
BACKTRACK_RELIEF_LEVELS = ("modere", "soutenu")
CORRIDOR_R_M = 25.0
CORRIDOR_GAP_M = 200.0
CORRIDOR_DZ_M = 6.0
CORRIDOR_STEP_M = 10.0
DETOUR_MIN_M, DETOUR_MAX_M = 200.0, 1500.0
DETOUR_GAP_M = 120.0             # retour à moins de 120 m…
DETOUR_RATIO = 0.25              # … et à moins du quart du chemin parcouru
DETOUR_CLIMB_M = 15.0            # monte d'au moins 15 m : lacets d'une côte, pas un détour
DETOUR_END_M = 300.0             # près du départ ou de l'arrivée : ignoré (rue du départ)
DETOUR_SCORE_SLACK = 0.01        # version sans détour gardée jusqu'à 1 point de note en moins
# v18 (O-55) : un détour inutile qui reste après la réparation compte dans la note (une boucle plate mais tordue battait
# une boucle propre un peu moins plate) ; les détours jugés utiles (BACKTRACK_RULES) restent gratuits. False : désactivé.
DETOUR_SCORE = True               # v18 (Florent, 10/10/2026 : activée en interrompant le calcul de la v17)
DETOUR_PENALTY = 0.02            # 2 points par détour…
DETOUR_PENALTY_PER_100M = 0.01   # … plus 1 point par 100 m en trop
DETOUR_MAX_PENALTY = 0.10
DETOUR_TRIM_TOP = 3              # meilleures candidates dont on essaie d'enlever les détours
SPUR_TRIM_TOP = 3               # meilleures candidates dont on essaie de couper les éperons (6 : sonde 18 -> 30 min)
TARGETED_GATE = None            # essai (O-38) : tirages ciblés seulement si la meilleure boucle ordinaire note moins que ça
AR_DRAWS = True                 # essai : aller-retour vers le bord de mer, une rivière ou un espace vert (ar_candidates)
LIEU_TOUR = True                # essai : monter à un lieu remarquable et en faire le tour (lieu_tour_candidates)
# Mémoire des bonnes boucles (Florent, 05/10/2026 : « ne jamais régresser ») : les boucles publiées de la version
# précédente sont recalculées et ajoutées aux candidates, notées avec les nouvelles règles. Essai : désactivé.
KEEP_PREVIOUS = True
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

    def count_along(self, coords, cum, out=None) -> int:
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
                if out is not None:                          # un point par carrefour à feux (m depuis le départ)
                    out.append(pos)
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


class JunctionDensity:
    """Carrefours (passages piétons, feux, stops, cédez-le-passage) dans un carré de (2 * R + 1) cellules de 100 m autour
    d'un point (modèle de temps v19) : grille de comptes cumulés, lecture en temps constant."""

    CELL_M = 100.0
    R = 3

    def __init__(self, points):
        import numpy as np
        a = np.array(points, dtype=float)
        lat_m = float(a[:, 1].mean())
        self.cx = self.CELL_M / (111320.0 * math.cos(math.radians(lat_m)))
        self.cy = self.CELL_M / 110540.0
        self.lon0, self.lat0 = float(a[:, 0].min()), float(a[:, 1].min())
        ix = ((a[:, 0] - self.lon0) / self.cx).astype(int)
        iy = ((a[:, 1] - self.lat0) / self.cy).astype(int)
        h = np.zeros((ix.max() + 1, iy.max() + 1), dtype=np.int32)
        np.add.at(h, (ix, iy), 1)
        self.s = np.pad(h.cumsum(0).cumsum(1), ((1, 0), (1, 0)), "constant")
        self.nx, self.ny = h.shape

    def at(self, lon: float, lat: float) -> int:
        i, j = int((lon - self.lon0) // self.cx), int((lat - self.lat0) // self.cy)
        i0, i1 = max(0, i - self.R), min(self.nx - 1, i + self.R)
        j0, j1 = max(0, j - self.R), min(self.ny - 1, j + self.R)
        if i0 > i1 or j0 > j1:
            return 0
        s = self.s
        return int(s[i1 + 1, j1 + 1] - s[i0, j1 + 1] - s[i1 + 1, j0] + s[i0, j0])

    def along(self, coords, cum, n: int, ds: float) -> list:
        """Densité aux points du profil d'altitude (un point tous les ds m, n + 1 points)."""
        out = []
        for k in range(n + 1):
            d = k * ds
            i = min(max(1, bisect.bisect_right(cum, d)), len(cum) - 1)
            s0, s1 = cum[i - 1], cum[i]
            t = 0.0 if s1 == s0 else min(1.0, (d - s0) / (s1 - s0))
            lon = coords[i - 1][0] + t * (coords[i][0] - coords[i - 1][0])
            lat = coords[i - 1][1] + t * (coords[i][1] - coords[i - 1][1])
            out.append(self.at(lon, lat))
        return out


def load_scenic_ways(pbf: Path, workdir: Path, landscape):
    """Voies agréables (SCENIC_RETOUCH) : pistes cyclables et voies vertes goudronnées au bord de la mer ou d'une rivière.
    Retourne un STRtree de leurs lignes, ou None."""
    import shutil
    import subprocess
    from shapely.geometry import shape
    from shapely.strtree import STRtree
    if landscape is None or not pbf.exists() or not shutil.which("osmium"):
        return None
    filt, out = workdir / "scenic_ways.osm.pbf", workdir / "scenic_ways.geojson"
    try:
        subprocess.run(["osmium", "tags-filter", str(pbf), "w/highway=cycleway,path,footway,pedestrian", "-o", str(filt),
                        "--overwrite"], check=True, capture_output=True)
        subprocess.run(["osmium", "export", str(filt), "-f", "geojson", "-o", str(out), "--overwrite"],
                       check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        print(f"! voies agréables : extraction échouée ({e.stderr.decode()[:200]})", file=sys.stderr)
        return None
    sea, river = landscape.trees.get("sea"), landscape.trees.get("river")
    ways = []
    for f in json.loads(out.read_text(encoding="utf-8")).get("features", []):
        p, g = f.get("properties", {}), f.get("geometry", {})
        if g.get("type") != "LineString":
            continue
        hw = p.get("highway")
        if hw != "cycleway" and p.get("bicycle") not in ("yes", "designated"):
            continue
        if p.get("surface") in UNPAVED or (p.get("surface") is None and hw == "path"):
            continue                                       # goudronnées seulement (vélo de route)
        line = shape(g)
        mid = line.interpolate(0.5, normalized=True)
        if ((sea is not None and len(sea.query(mid, predicate="dwithin", distance=SCENIC_SEA_M / 85000.0)))
                or (river is not None and len(river.query(mid, predicate="dwithin", distance=SCENIC_RIVER_M / 85000.0)))):
            ways.append(line)
    print(f"voies agréables : {len(ways)} tronçons")
    return STRtree(ways) if ways else None


def scenic_candidates(gh, st, level, duration_h, pool, log, detail=None) -> list:
    """Retouche « voies agréables » (SCENIC_RETOUCH) des meilleures boucles du pool : seulement les variantes valides qui
    notent mieux que leur boucle de départ."""
    import shapely
    if detail is None:                                       # traçage d'un diagnostic (nature_check --trace)
        detail = getattr(_TL, "trace_detail", None)
    from shapely.ops import nearest_points
    bases = []
    for l in sorted(pool, key=lambda x: x.score, reverse=True):
        if all(similarity(l, b) < 0.6 for b in bases):
            bases.append(l)
        if len(bases) >= SCENIC_TOP:
            break
    found = []
    for bi, base in enumerate(bases):
        c, cum = base.coords, _cum_cache(base)
        samples, last = [], -1e9                           # un point tous les ~100 m : (indice, voie la plus proche, m)
        for i, d in enumerate(cum):
            if d - last >= 100.0:
                pt = shapely.Point(c[i][0], c[i][1])
                j = SCENIC_WAYS.nearest(pt)
                way = SCENIC_WAYS.geometries[int(j)]
                q = nearest_points(pt, way)[1]
                samples.append((i, way, haversine(c[i][0], c[i][1], q.x, q.y), q))
                last = d
        runs, k = [], 0
        while k < len(samples):
            if not (SCENIC_ON_M < samples[k][2] <= SCENIC_NEAR_M):
                k += 1
                continue
            a = k
            while k < len(samples) and SCENIC_ON_M < samples[k][2] <= SCENIC_NEAR_M:
                k += 1
            if cum[samples[k - 1][0]] - cum[samples[a][0]] >= SCENIC_RUN_M:
                runs.append(samples[a:k])
        runs.sort(key=lambda r: -(cum[r[-1][0]] - cum[r[0][0]]))
        if detail is not None:                               # ce que la détection voit (diagnostic)
            near = [s_ for s_ in samples if s_[2] <= SCENIC_NEAR_M]
            on = sum(1 for s_ in samples if s_[2] <= SCENIC_ON_M)
            detail.append(f"  voies agréables : boucle {bi + 1} ({base.score:.1f}, {base.distance_m / 1000:.1f} km) : "
                          f"{len(near) / 10:.1f} km à moins de {SCENIC_NEAR_M:.0f} m d'une voie agréable, {on / 10:.1f} km "
                          f"dessus, {len(runs)} tronçon(s) longé(s) d'au moins {SCENIC_RUN_M / 1000:g} km")
        trials = []
        for run in runs[:SCENIC_MAX_RUNS]:
            trials.append(run)
            if scenic_windows() and cum[run[-1][0]] - cum[run[0][0]] >= 1.5 * SCENIC_WINDOW_M:
                k = 0                                        # portions successives d'environ SCENIC_WINDOW_M
                while k < len(run) - 1 and len(trials) < 10:
                    j = k
                    while j < len(run) - 1 and cum[run[j][0]] - cum[run[k][0]] < SCENIC_WINDOW_M:
                        j += 1
                    if cum[run[j][0]] - cum[run[k][0]] >= SCENIC_MIN_WAY_M:
                        trials.append(run[k:j + 1])
                    k = j + 1
        for n, run in enumerate(trials):
            if scenic_dense():                               # un point tous les SCENIC_PICK_M, sens de la boucle
                chosen, last_m = [], -1e9
                for s_ in run:
                    if cum[s_[0]] - last_m >= SCENIC_PICK_M:
                        chosen.append(s_)
                        last_m = cum[s_[0]]
                picks = [s_[3] for s_ in chosen]
                heads = [travel_heading(c, s_[0]) for s_ in chosen]
            else:
                picks = [run[int(f * (len(run) - 1))][3] for f in (0.25, 0.5, 0.75)]
                heads = [None] * len(picks)
            if haversine(picks[0].x, picks[0].y, picks[-1].x, picks[-1].y) < SCENIC_MIN_WAY_M:
                continue
            ia, ib = run[0][0], run[-1][0]
            way = route_waypoints(c, skip=[(ia, ib)])
            pos = next((m for m, p in enumerate(way) if abs(p[0] - c[ia][0]) < 1e-9 and abs(p[1] - c[ia][1]) < 1e-9), None)
            if pos is None:
                continue
            way = way[:pos + 1] + [[p.x, p.y] + ([h] if h is not None else []) for p, h in zip(picks, heads)] + way[pos + 1:]
            path = gh.via(way, base.profile, pass_through=True)
            loop = analyse(path, level, base.profile, duration_h, 4100 + 10 * bi + n, None) if path else None
            ok = (loop is not None and time_ok(loop.time_s, duration_h) and overlap_ok(loop)
                  and loop.u_turns <= MAX_UTURNS and dirt_ok(loop) and loop.score > base.score)
            if detail is not None:
                detail.append(f"  voies agréables : boucle {bi + 1} ({base.score:.1f}), tronçon longé de "
                              f"{(cum[ib] - cum[ia]) / 1000:.1f} km vers {picks[len(picks) // 2].y:.4f},"
                              f"{picks[len(picks) // 2].x:.4f} ({len(picks)} points) -> "
                              + ("pas d'itinéraire" if loop is None else f"note {loop.score:.1f}, {loop.time_s / 60:.0f} min")
                              + (" : gardée" if ok else " : rejetée"))
            if ok:
                loop.scenic_retouched = True
                found.append(loop)
    log(f"    voies agréables : {len(found)} variante(s) gardée(s)")
    return found


def load_density(pbf: Path, workdir: Path):
    idx = load_points(pbf, workdir, ["n/highway=crossing,traffic_signals,stop,give_way", "n/crossing=traffic_signals"],
                      "junctions")
    return JunctionDensity(idx.points) if idx is not None else None


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
        noisy = noisy_mask(pts)                          # bande de bruit des voies rapides (MOTORWAY_V2)
        quiet = set(range(len(xy))) if noisy is None else {k for k in range(len(xy)) if not noisy[k]}

        def share(key, dist):
            tree = self.trees[key]
            if tree is None:
                return 0.0
            hit = tree.query(pts, predicate="dwithin", distance=dist)[0]
            return len(set(hit.tolist()) & quiet) / len(xy)

        views = 0
        if self.trees["view"] is not None:
            views = len(set(self.trees["view"].query(pts, predicate="dwithin", distance=SCENERY_VIEW_DEG)[1].tolist()))
        forest = share("forest", SCENERY_FOREST_DEG)
        water = share("water", SCENERY_WATER_DEG)
        if water_city_near() and self.trees["water"] is not None and self.trees["buildings"] is not None and len(xy):
            hit_w = np.zeros(len(xy), dtype=bool)          # en ville dense : bord d'eau seulement à ~80 m (WATER_CITY_NEAR)
            hit_w[np.unique(self.trees["water"].query(pts, predicate="dwithin", distance=SCENERY_WATER_DEG)[0])] = True
            close = np.zeros(len(xy), dtype=bool)
            close[np.unique(self.trees["water"].query(pts, predicate="dwithin", distance=WATER_CITY_DEG)[0])] = True
            hit_b = self.trees["buildings"].query(pts, predicate="dwithin", distance=LANDCOVER_BLD_DEG)[0]
            dense = np.bincount(hit_b, minlength=len(xy)) >= LANDCOVER_BLD_MIN
            water = float((hit_w & (~dense | close) & (True if noisy is None else ~noisy)).mean())
        protected = share("protected", SCENERY_PROTECTED_DEG)
        ind_mask = industrial_mask(self.trees, pts)       # par point (tous les 100 m) : surlignage dans l'appli
        industrial = float(ind_mask.mean()) if len(xy) else 0.0
        index = min(1.0, 0.8 * forest + 1.5 * water + 0.6 * protected)   # belvédères : bonus à part (VIEW_BONUS)
        return {"forest": forest, "water": water, "protected": protected, "viewpoints": views,
                "industrial": industrial, "industrial_mask": ind_mask.tolist(), "score": round(100 * index),
                **({"noisy": noisy.tolist()} if noisy is not None else {}),
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
# Sortie durable (Florent, 16/10/2026 : les Corts 2 h Tranquille annoncée « tu pars directement hors de la ville » alors
# que la boucle reste en ville jusqu'au km 12 : les 1,5 premiers km longent le campus et les terrains de sport de la Zona
# Universitària, sans bâti). La sortie n'est retenue que si, sur les EXIT_CITY_WIN_KM suivants, la ville pèse au plus
# EXIT_CITY_MAX_SHARE : un parc ou un campus traversé en ville n'est pas une sortie.
EXIT_CITY_WIN_KM = 5.0
EXIT_CITY_MAX_SHARE = 0.4           # 0,25 : routes de périphérie entre villages jugées « en ville » ; 0,5 : les Corts au km 5,7


def landcover_seq(cls) -> str:
    """Catégorie de terrain de chaque point (0 ville, 1 eau, 2 forêt, 3 espaces ouverts) -> « vvvvooeeff… » (bande de
    terrain sous le profil, O-17)."""
    return "".join(LANDCOVER_CODES[int(c)] for c in cls)


def exit_city_km(cls, dist_km: float):
    """Km où l'on sort de la ville, mesuré par le bâti comme la barre de terrain : début du premier tronçon d'au moins
    1 km sans « ville » (un parc de 200 m n'est pas une sortie) après lequel la ville pèse au plus EXIT_CITY_MAX_SHARE
    des EXIT_CITY_WIN_KM suivants (un campus traversé en ville n'est pas une sortie). 0 = départ hors de la ville ;
    None = reste en ville. Les points sont recalés sur la distance officielle (comme le profil)."""
    step = dist_km / max(len(cls), 1)
    win = max(EXIT_CITY_RUN, round(EXIT_CITY_WIN_KM / max(step, 1e-6)))
    run = 0
    for i, c in enumerate(cls):
        run = run + 1 if int(c) != 0 else 0
        if run >= EXIT_CITY_RUN:
            a = i - run + 1
            seg = cls[a:a + win]
            if sum(1 for x in seg if int(x) == 0) <= EXIT_CITY_MAX_SHARE * len(seg):
                return round(a * step, 1)
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
                    "n/natural=peak", "nw/tourism=viewpoint", "nw/historic=monastery", "nw/historic=castle",
                    "nw/amenity=monastery", "nw/heritage=1", "nw/heritage=2", "-o", str(filt), "--overwrite"],
                   check=True, capture_output=True)
    subprocess.run(["osmium", "export", str(filt), "-f", "geojsonseq", "-o", str(out), "--overwrite"], check=True,
                   capture_output=True)
    kinds = {"w": [], "c": [], "g": [], "pass": [], "peak": [], "lieu": [], "patrimoine": []}
    # lieux remarquables : scripts/remarkable_places.json (Wikidata, au moins 6 articles Wikipédia ; sonde #163 : le simple
    # « a un article Wikipédia » retenait 63 petits turons de Catalogne, 239 boucles sur 459, le Tibidabo 2 fois seulement)
    try:
        remarkable_all = json.loads((Path(__file__).parent / "remarkable_places.json").read_text(encoding="utf-8"))["places"]
        remarkable = {q for q, e in remarkable_all.items() if not e.get("exclude")}   # écartés à la main : ni visés ni comptés
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
            if name and (p.get("historic") in ("monastery", "castle") or p.get("amenity") == "monastery"
                         or p.get("heritage") in ("1", "2")):
                kinds["patrimoine"].append((pt, name))     # monastère, château, bien patrimonial (DEST_HERITAGE)
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
                    n_signals: int | None = None, level: str | None = None, dens: list | None = None) -> float:
    """Temps estimé : vitesse déduite de la puissance sur le profil lissé (un point tous les ~100 m),
    + attente moyenne par feu tricolore (SIGNAL_DELAY_S). Sans données de feux, on garde une majoration
    forfaitaire plus forte en ville. Avec dens (densité de carrefours aux points du profil) et le modèle v19 : voir
    TIME_V19."""
    if dens is not None and n_signals is not None and time_v19():
        cap = DESCENT_CAP_KMH.get(level, DESCENT_CAP_KMH["modere"]) / 3.6
        fat = fatigue_v19() and level in LEVELS
        if fat:                                              # puissance de départ, sans la marche à 3 h
            watts = LEVELS[level]["ftp_wkg"] * RIDER_KG * LEVELS[level]["intensity"]
        t, climbed = 0.0, 0.0
        for k in range(1, len(profile)):
            grade = max(-0.20, min(0.20, (profile[k] - profile[k - 1]) / ds))
            base, bonus = watts, CLIMB_POWER_BONUS
            if fat:                                          # fatigue : temps déjà roulé, D+ déjà grimpé
                base = watts * max(0.5, 1.0 - FATIGUE_PER_H * max(0.0, t / 3600.0 - FATIGUE_FROM_H))
                bonus = CLIMB_POWER_BONUS * math.exp(-climbed / FATIGUE_CLIMB_M)
                climbed += max(0.0, profile[k] - profile[k - 1])
            p = base * (1.0 + bonus * min(1.0, max(0.0, grade) / CLIMB_POWER_FULL_GRADE))
            v = speed_from_power(p, grade) * REAL_WORLD_FACTOR
            if grade < -0.02:
                v = min(v, cap)
            d = 0.5 * (dens[k - 1] + dens[k])
            t += ds / v * (1.0 + DENSITY_SLOWDOWN * min(1.0, d / DENSITY_FULL))
        return t + n_signals * SIGNAL_DELAY_V19_S
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
        self.heading_retries = 0                            # sens de circulation refusé : requête refaite sans
        self.heading_error = ""                             # message du premier refus (diagnostic)

    def _route(self, body):
        import copy
        if scenic_areas() and body.get("profile") in SCENIC_PROFILES:   # zones agréables (SCENIC_AREAS)
            body = {**body, "profile": body["profile"] + "_scenic"}
        if small_model():                                    # petites routes (SMALL_MODEL)
            cm = dict(body.get("custom_model") or {})
            cm["priority"] = list(cm.get("priority") or []) + SMALL_MODEL_PRIORITY
            body = {**body, "custom_model": cm, "ch.disable": True}
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

    def via(self, points, profile, pass_through=False, custom_model=None):
        """Itinéraire passant par des points imposés (tirages ciblés), mêmes détails que round_trip. pass_through : pas de
        demi-tour aux points de passage (retouche : points pris sur une boucle, parfois du mauvais côté d'une avenue)."""
        heads = [p[2] if len(p) > 2 else None for p in points]   # sens de circulation (DETOUR_FIX), en degrés
        body = {"points": [[p[0], p[1]] for p in points], "profile": profile, "ch.disable": True,
                "points_encoded": False, "elevation": True, "instructions": False, "details": DETAILS,
                **({"pass_through": True} if pass_through else {}),
                **({"custom_model": custom_model} if custom_model else {})}
        if detour_fix() and any(h is not None for h in heads):
            # point sans sens imposé : "NaN" en texte (09/10/2026 : la valeur NaN brute, hors JSON, était refusée par
            # GraphHopper, 1 971 fois sur le diagnostic des détours ; le texte "NaN" est lu comme un nombre)
            body["headings"] = ["NaN" if h is None else round(h, 1) % 360.0 for h in heads]   # 360,0 refusé (09/10)
            path = self._route(body)
            if path is not None or not self.last_error.startswith("HTTP 400"):
                return path
            self.heading_retries += 1                       # refusé : même requête sans sens imposé
            self.heading_error = self.heading_error or self.last_error
            del body["headings"]
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
    steep_dirt_km: float = 0.0
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


def corridor_repeats(coords, cum) -> list:
    """Longueur répétée de chaque tronçon, mesurée par couloir (voir CORRIDOR_OVERLAP) : tronçons découpés en morceaux de
    CORRIDOR_STEP_M, un morceau est répété s'il passe à moins de CORRIDOR_R_M (et CORRIDOR_DZ_M d'altitude) d'un morceau
    franchi au moins CORRIDOR_GAP_M plus tôt ; ce morceau antérieur l'est aussi."""
    kx = 111320.0 * math.cos(math.radians(coords[0][1]))
    ky = 110540.0
    pts = []                                                 # (x, y, z, position sur la boucle, tronçon, longueur)
    for i in range(len(coords) - 1):
        a, b = coords[i], coords[i + 1]
        L = cum[i + 1] - cum[i]
        n = max(1, int(math.ceil(L / CORRIDOR_STEP_M)))
        za, zb = (a[2], b[2]) if len(a) > 2 and len(b) > 2 else (0.0, 0.0)
        for k in range(n):
            f = (k + 0.5) / n
            pts.append(((a[0] + (b[0] - a[0]) * f) * kx, (a[1] + (b[1] - a[1]) * f) * ky, za + (zb - za) * f,
                        cum[i] + f * L, i, L / n))
    rep = [False] * len(pts)
    grid: dict = {}
    r, r2 = CORRIDOR_R_M, CORRIDOR_R_M ** 2
    for p, (x, y, z, s, _, _) in enumerate(pts):
        gx, gy = int(x // r), int(y // r)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for q in grid.get((gx + dx, gy + dy), ()):
                    qx, qy, qz, qs, _, _ = pts[q]
                    if qs < s - CORRIDOR_GAP_M and (qx - x) ** 2 + (qy - y) ** 2 < r2 and abs(qz - z) < CORRIDOR_DZ_M:
                        rep[p] = rep[q] = True
        grid.setdefault((gx, gy), []).append(p)
    out = [0.0] * (len(coords) - 1)
    for p, pt in enumerate(pts):
        if rep[p]:
            out[pt[4]] += pt[5]
    return out


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
    if corridor_overlap():                           # par couloir (CORRIDOR_OVERLAP) : longueur répétée de chaque tronçon
        rep_m = corridor_repeats(coords, cum)
    else:                                            # mêmes points de la carte (v16)
        rep_m = [cum[i + 1] - cum[i] if seen[k] > 1 else 0.0 for i, k in enumerate(keys)]
    rc_rep = road_edges(det, len(cum) - 1)            # partie répétée sur piste cyclable (aller-retour, OUTBACK_OK)
    unp_e = unpaved_edges(det, len(cum) - 1) if backtrack_rules() else None
    foot_e = foot_edges(det, len(cum) - 1) if foot_backtrack() else None
    useful = useful_windows(coords, level, rc_rep, unp_e, foot_e) if backtrack_rules() else []
    dup_windows = []
    if turn_rules() and len(useful) > 1:                 # même destination deux fois : seul le 1er aller-retour compte
        keep = []                                        # (Florent, 17/10/2026 : l'Eixample 4 h Sportif monte deux fois
        for w in useful:                                 # au même point de vue du Turó de Valldaura)
            if any(haversine(coords[w[2]][0], coords[w[2]][1], coords[k[2]][0], coords[k[2]][1]) < 300.0 for k in keep):
                dup_windows.append(w)
            else:
                keep.append(w)
        useful = keep
    demoted = []                                         # utiles d'après les règles d'avant, plus maintenant
    if city_backtrack() and useful:                      # en ville dense : seulement vers une vraie destination
        ud_ = per_edge(det.get("urban_density"), len(cum) - 1)
        keep = []
        for w in useful:
            span = [(cum[i + 1] - cum[i], str(ud_[i]).lower() == "city") for i in range(w[0], min(w[1], len(ud_)))]
            tot = sum(m for m, _ in span)
            if (w[3] in ("nature", "piste", "montée") and tot > 0
                    and sum(m for m, c in span if c) >= CITY_BACKTRACK_SHARE * tot):
                demoted.append((w, "city"))
            else:
                keep.append(w)
        useful = keep
    if nested_backtrack() and len(useful) > 1:          # aller-retour qui en contient un autre : la même rue remontée
        keep = [w for w in useful if not any(k is not w and w[0] <= k[0] and k[1] <= w[1] and k[1] - k[0] < w[1] - w[0]
                                             for k in useful)]
        demoted += [(w, "nested") for w in useful if w not in keep]
        useful = keep
    if backtrack_dest() and useful:                      # « piste » ou « montée » : seulement vers une destination
        keep = []
        for w in useful:
            if w[3] in ("piste", "montée") and not destination_tip(coords[w[2]]):
                demoted.append((w, "aimless"))
            else:
                keep.append(w)
        useful = keep
    for a_, b_, _, _ in useful:                      # aller-retour utile (BACKTRACK_RULES) : répétition comptée pour moitié
        for i in range(a_, min(b_, len(rep_m))):
            rep_m[i] *= BACKTRACK_REPEAT_WEIGHT
    repeated = sum(rep_m)
    rep_cycle = sum(m for i, m in enumerate(rep_m) if rc_rep[i] == "c")
    # mesure v16 gardée pour le filtre (10/10/2026, mesure combinée : 14 durées vidées en montagne, boucles à 24-54 % par
    # couloir mais 14-24 % en v16, sans autre boucle possible) : la mesure par couloir compte dans la note, jamais plus
    # stricte que la v16 comme filtre (voir overlap_ok)
    rep_v16 = [cum[i + 1] - cum[i] if seen[k] > 1 else 0.0 for i, k in enumerate(keys)]
    ov16 = sum(rep_v16) / total
    cyc16 = (sum(m for i, m in enumerate(rep_v16) if rc_rep[i] == "c") / sum(rep_v16)) if sum(rep_v16) > 0 else 0.0
    longest_repeat, run = 0.0, 0.0
    for i, m in enumerate(rep_m):                    # plus long tronçon consécutif emprunté deux fois
        run = run + m if m > 0.5 * (cum[i + 1] - cum[i]) else 0.0
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
    sf_e = per_edge(det.get("surface"), n_e)                 # petites routes goudronnées hors de la ville (SMALL_ROADS)
    small_m = sum(cum[i + 1] - cum[i] for i in range(n_e) if rc_e[i] in SMALL_ROADS_CLASSES
                  and sf_e[i] not in UNPAVED and str(ud_e[i]).lower() != "city"
                  and (rc_e[i] != "track" or sf_e[i] in ("asphalt", "paved", "concrete")))
    steps_m = sum(cum[i + 1] - cum[i] for i in range(n_e) if rc_e[i] == "steps")   # publié pour la revue (16/10/2026)
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
    signal_m: list = []
    n_signals = SIGNALS.count_along(coords, cum, signal_m) if SIGNALS is not None else None
    n_stops = STOPS.count_along(coords, cum) if STOPS is not None else None
    surface_mix = {
        "asphalt": frac(surf, list(ASPHALT)), "cobbles": frac(surf, list(COBBLES)),
        "unpaved": frac(surf, list(UNPAVED)), "unpaved_probable": probable,
    }
    surface_mix["unknown"] = max(0.0, 1.0 - sum(surface_mix.values()))
    terrain = slope_stats(ds, prof)
    u_turns = true_uturns(coords) if UTURN_LACETS_OK else count_uturns(coords)
    if useful and UTURN_LACETS_OK:                   # demi-tour au bout d'un aller-retour utile (BACKTRACK_RULES) : gratuit
        u_turns = sum(1 for u in uturn_points(coords) if u[2] < UTURN_SAME_STREET_M and not any(
            haversine(u[0], u[1], coords[t][0], coords[t][1]) < 40.0 for _, _, t, _ in useful))
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
        time_s=estimate_time_s(ds, prof, watts, city, resid, n_signals, level,
                               DENSITY.along(coords, cum, len(prof) - 1, ds) if time_v19() else None),
        shares=shares, overlap=repeated / total, signals=n_signals, exit_dense_m=exit_dense,
        stops=n_stops, surface_mix=surface_mix, terrain=terrain, u_turns=u_turns, longest_repeat_m=longest_repeat,
        repeat_cycle_share=(rep_cycle / repeated) if repeated > 0 else 0.0,
        scenery=scenery,
    )
    loop.overlap_v16, loop.repeat_cycle_share_v16 = ov16, cyc16
    loop.steps_m = steps_m
    loop.cells = {(round(c[0] / 0.006), round(c[1] / 0.005)) for c in coords}   # cellules ~500 m
    loop.remarkable = remarkable_passed(coords, cum)
    loop.views_passed = views_passed(coords, cum)
    loop.spurs = unjustified_spurs(coords, level, rc_rep, unp_e, foot_e) if SPUR_FIX else []
    for a_, b_, t_, _ in dup_windows:                    # aller-retour vers une destination déjà vue : crochet inutile
        loop.spurs = [s_ for s_ in loop.spurs if s_["b"] < a_ or s_["a"] > b_]
        loop.spurs.append({"km": round(cum[a_] / 1000.0, 1), "m": round(cum[b_] - cum[a_]), "a": a_, "b": b_,
                           "lon": round(coords[t_][0], 5), "lat": round(coords[t_][1], 5), "same_place": True})
    for (a_, b_, t_, _), why_ in demoted:                # en ville sans destination, ou emboîté : éperon injustifié
        loop.spurs = [s_ for s_ in loop.spurs if s_["b"] < a_ or s_["a"] > b_]
        loop.spurs.append({"km": round(cum[a_] / 1000.0, 1), "m": round(cum[t_] - cum[a_]), "a": a_, "b": b_,
                           "lon": round(coords[t_][0], 5), "lat": round(coords[t_][1], 5), why_: True})
    corridor_windows = []
    if SPUR_FIX and corridor_spurs_on() and backtrack_rules():   # allers-retours dans un couloir (CORRIDOR_SPURS)
        taken = [(s["a"], s["b"]) for s in loop.spurs] + [(a_, b_) for a_, b_, _, _ in useful]
        for a_, b_, t_ in corridor_spurs(coords, cum):
            if any(not (b_ < x or a_ > y) for x, y in taken):
                continue                                     # déjà vu comme aller-retour ordinaire
            if backtrack_useful(coords, a_, b_, level, rc_rep, unp_e, t_, foot_e) is None:
                # 2e version : comme une boucle accrochée (3 points, la boucle sans lui essayée), pas comme un éperon
                corridor_windows.append({"km": round(cum[a_] / 1000.0, 1), "m": round(cum[b_] - cum[a_]), "a": a_,
                                         "b": b_, "lon": round(coords[t_][0], 5), "lat": round(coords[t_][1], 5),
                                         "corridor": True})
                taken.append((a_, b_))
    loop.backtracks = [{"why": w, "m": round(cum[min(b_, len(cum) - 1)] - cum[a_]), "lat": round(coords[t][1], 5),
                        "lon": round(coords[t][0], 5), "km": round(cum[t] / 1000.0, 1),
                        "from_km": round(cum[a_] / 1000.0, 2), "to_km": round(cum[min(b_, len(cum) - 1)] / 1000.0, 2)}
                       for a_, b_, t, w in useful]
    loop.aimless_bt = (sum(1 for b_ in loop.backtracks if b_["why"] in ("nature", "piste")
                           and not destination_tip([b_["lon"], b_["lat"]])) if turn_rules() else 0)
    loop.climb_mid_bt = sum(1 for b_ in loop.backtracks if b_["why"] == "montée")
    loop.aimless_windows = ([(a_, b_) for a_, b_, t_, w_ in useful if w_ == "nature" and not destination_tip(coords[t_])]
                            if backtrack_dest() else [])     # allers-retours « nature » sans destination (BACKTRACK_DEST)
    loop.detours = block_detours(coords) if detour_fix() else []
    if loop.detours and hairpins():                      # lacets : la route elle-même (HAIRPINS)
        loop.detours = [d for d in loop.detours if not smooth_window(coords, d["a"], d["b"])]
    if loop.detours and backtrack_rules():           # détour utile (lieu, eau, piste…) : gardé
        loop.detours = [d for d in loop.detours
                        if (backtrack_useful(coords, d["a"], d["b"], level, rc_rep, unp_e, None, foot_e) or "")
                        not in (("lieu", "belvédère", "relief") if detour_dest() else
                                ("lieu", "belvédère", "relief", "nature", "piste", "montée"))]
    if loop.detours and forced_detours() and FORCED_SPOTS:   # lacet ou passage obligé déjà reconnu (FORCED_DETOURS)
        loop.detours = [d for d in loop.detours if spot_key(coords[d["a"]]) not in FORCED_SPOTS]
    loop.surface_seq = surface_seq(edges, cum)
    if STEEP_DIRT_PENALTY:                                # terre (notée ou probable) sur une pente d'au moins 12 %
        n_ = min(len(loop.surface_seq), len(prof) - 1)
        loop.steep_dirt_km = sum(1 for k in range(n_) if loop.surface_seq[k] in "up"
                                 and abs(prof[k + 1] - prof[k]) / ds >= STEEP_DIRT_GRADE) * ds / 1000.0
    loop.road_seq = surface_seq(road_edges(det, len(cum) - 1), cum)
    # densité urbaine de GraphHopper tous les 100 m (v ville, r résidentiel, o campagne) : grands axes à contourner
    loop.urban_seq = surface_seq([{"city": "v", "residential": "r"}.get(str(u).lower(), "o") for u in ud_e], cum)
    loop.signal_m = signal_m                                 # positions des feux (croisement tronçon par tronçon)
    loop.doubt_ways = doubt_ways(det, coords, cum)
    loop.wind_bins = {k: [round(x, 2) for x in v] for k, v in bins.items()}
    loop.seafront_km, loop.motorway_km = (side_km(coords, cum, car, rc_e) if (seafront_bonus() or motorway_nuisance())
                                          else (0.0, 0.0))
    loop.appendices = appendix_spurs(loop, coords, cum, level) if appendix_rule() else []   # boucles accrochées sans but
    loop.appendices = loop.appendices + corridor_windows     # allers-retours dans un couloir (CORRIDOR_SPURS)
    if false_start():                                        # faux départs et faux retours (FALSE_START)
        loop.appendices = loop.appendices + appendix_spurs(loop, coords, cum, level, false_starts(coords, cum))
    loop.small_roads = small_m / total                       # part sur petites routes goudronnées (SMALL_ROADS)
    loop.gh_details = det                                    # détails GraphHopper, pour recoudre un morceau (CLEAN_PICKS)
    if water_hug() and LANDSCAPE is not None:              # au plus près de l'eau (WATER_HUG)
        import numpy as np
        import shapely
        xy = sample_points(coords, cum, 100.0)
        wpts = shapely.points(np.array(xy))
        close = np.zeros(len(xy), dtype=bool)
        for key_ in ("sea", "river", "water"):
            tr_ = LANDSCAPE.trees.get(key_)
            if tr_ is not None:
                close[np.unique(tr_.query(wpts, predicate="dwithin", distance=WATER_HUG_DEG)[0])] = True
        loop.water_km = 0.1 * float(close.sum())
        if repeated > 0:                                     # aller-retour le long de l'eau : accepté comme sur piste
            rep_water = sum(m for i, m in enumerate(rep_m) if rc_rep[i] != "c" and m > 0
                            and int(cum[i] // 100) < len(close) and close[int(cum[i] // 100)])
            loop.repeat_cycle_share = (rep_cycle + rep_water) / repeated
    if towns_pen() and loop.scenery and (loop.scenery.get("landcover") or {}).get("citymask") is not None:
        cm_ = loop.scenery["landcover"]["citymask"]          # bâti dense, tous les 100 m (VILLES : TOWNS_PEN)
        n_ = len(cm_)
        lo_, hi_ = int(TOWNS_PEN_EDGE * n_), int((1.0 - TOWNS_PEN_EDGE) * n_)
        loop.towns_km = 0.1 * sum(1 for k in range(lo_, hi_) if cm_[k])
    if loop_shape():                                         # vraie boucle, claire (LOOP_SHAPE)
        loop.ends_cross, loop.turns_km = shape_measures(coords, cum)
    loop.rep_plain = 0.0                                     # répétition sur routes ordinaires (REPEAT_PLAIN)
    if repeat_plain() or flow_v2():                          # FLOW_V2 aussi : seule la répétition ordinaire est pesée 3 fois
        lc_ = ""
        if loop.scenery and (loop.scenery.get("landcover") or {}).get("cls") is not None:
            lc_ = landcover_seq(loop.scenery["landcover"]["cls"])
        road_ = loop.road_seq or ""
        plain = 0.0
        for i, m in enumerate(rep_m):
            if m <= 0:
                continue
            k = int(cum[i] // 100)
            nz_ = (loop.scenery or {}).get("noisy")
            nice = (((k < len(lc_) and lc_[k] in "ef")
                     or (k < len(road_) and road_[k] == "c" and (k >= len(lc_) or lc_[k] != "v")))
                    and not (nz_ and k < len(nz_) and nz_[k]))
            if not nice:
                plain += m
        loop.rep_plain = plain / total
    loop.score = score(loop)
    return loop


def score_parts(l: Loop) -> tuple[dict, dict]:
    """Sous-scores (0 à 1) et poids utilisés par score() : exposés pour les diagnostics (nature_check.py)."""
    s = l.shares
    parts = {
        "calm": 1.0 - (s.get("urban_car", s["urban"])["city"] + 0.4 * s.get("urban_car", s["urban"])["residential"]),
        "axes": 1.0 - min(1.0, 2.0 * s["main_roads"]),
        "infra": min(1.0, 2.0 * s["dedicated_cycleway"]),
        # FLOW_V2, 2e version (mesure du 24/10/2026 : bord de mer -15 %, nature en baisse ; Florent préfère revenir le
        # long de la mer plutôt que par une route désagréable) : seule la répétition sur routes ordinaires (rep_plain :
        # ni bord de l'eau, ni forêt, ni piste hors de la ville) est pesée FLOW_V2_FACTOR fois ; l'autre comme avant.
        "flow": 1.0 - min(1.0, FLOW_OVERLAP_FACTOR * l.overlap
                          + ((FLOW_V2_FACTOR - FLOW_OVERLAP_FACTOR) * getattr(l, "rep_plain", 0.0) if flow_v2() else 0.0)
                          + 0.15 * l.u_turns),
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
    dirt_km = l.shares["unpaved"] * l.distance_m / 1000.0 + l.steep_dirt_km   # terre raide comptée deux fois
    per_km = dirt_rules().get("pen_per_km", UNPAVED_PENALTY_PER_KM)
    total -= min(0.3, max(per_km * dirt_km, max(0.0, l.shares["unpaved"] - 0.03) * 2.0))
    total += min(REMARKABLE_MAX_BONUS, REMARKABLE_BONUS * len(l.remarkable))   # Tibidabo, belvédères connus…
    total += min(VIEW_MAX_BONUS, VIEW_BONUS * l.views_passed)                 # tout belvédère devant lequel on passe
    if seafront_bonus():                                  # front de mer sans voitures (SEAFRONT_BONUS)
        total += min(SEAFRONT_MAX, SEAFRONT_PER_KM * getattr(l, "seafront_km", 0.0))
    if axes_over():                                       # grands axes au-delà du seuil (AXES_OVER)
        over = l.shares.get("main_roads", 0.0) - AXES_OVER_SHARE
        if over > 0:
            total -= min(AXES_OVER_MAX, AXES_OVER_PER * over)
    if motorway_nuisance():                               # à côté d'une voie rapide (MOTORWAY_NUISANCE)
        if motorway_v2():                                 # 2e version (MOTORWAY_V2)
            total -= min(MOTORWAY_V2_MAX, MOTORWAY_V2_PER_KM * getattr(l, "motorway_km", 0.0))
        else:
            total -= min(MOTORWAY_MAX, MOTORWAY_PER_KM * getattr(l, "motorway_km", 0.0))
    if getattr(l, "climb_mid_bt", 0):                     # demi-tour en pleine montée : pénalisé (TURN_RULES, 2e version)
        total -= min(TURN_CLIMB_MAX, TURN_CLIMB_PEN * l.climb_mid_bt)
    if getattr(l, "aimless_bt", 0):                       # aller-retour sans destination : un peu pénalisé (TURN_RULES)
        if backtrack_dest():                              # remplace D80 : davantage (BACKTRACK_DEST)
            total -= min(BACKTRACK_DEST_NATURE_MAX, BACKTRACK_DEST_NATURE_PEN * l.aimless_bt)
        else:
            total -= min(TURN_AIMLESS_MAX, TURN_AIMLESS_PEN * l.aimless_bt)
    if repeat_plain() and getattr(l, "rep_plain", 0.0) > REPEAT_PLAIN_FREE:   # répétition sur routes ordinaires
        total -= min(REPEAT_PLAIN_MAX, REPEAT_PLAIN_PER * (l.rep_plain - REPEAT_PLAIN_FREE))
    if backtrack_long():                                  # long aller-retour annoncé, plus gratuit (BACKTRACK_LONG)
        lb = long_backtrack(l)
        if lb is not None:
            total -= min(BACKTRACK_LONG_MAX, BACKTRACK_LONG_PEN + BACKTRACK_LONG_PEN_PER * (lb[0] - BACKTRACK_LONG_SHARE))
    if getattr(l, "appendices", None):                    # petite boucle accrochée sans but : 3 points, une seule fois
        total -= APPENDIX_PENALTY
    if l.spurs:                                           # éperons injustifiés (SPUR_FIX)
        total -= min(SPUR_MAX_PENALTY, sum(SPUR_PENALTY + SPUR_PENALTY_PER_100M * s["m"] / 100.0 for s in l.spurs))
    if water_hug():                                       # au plus près de la mer, des rivières, des lacs (WATER_HUG)
        total += min(WATER_HUG_MAX, WATER_HUG_PER_KM * getattr(l, "water_km", 0.0))
    if towns_pen():                                       # traversées de villes au milieu de la boucle (TOWNS_PEN)
        total -= min(TOWNS_PEN_MAX, TOWNS_PEN_PER_KM * getattr(l, "towns_km", 0.0))
    if loop_shape():                                      # vraie boucle, claire (LOOP_SHAPE)
        if getattr(l, "ends_cross", False):
            total -= LOOP_SHAPE_CROSS
        total -= min(LOOP_SHAPE_TURNS_MAX, LOOP_SHAPE_TURNS_PER * max(0.0, getattr(l, "turns_km", 0.0)
                                                                        - LOOP_SHAPE_TURNS_FREE))
    if small_roads():                                     # petites routes goudronnées hors de la ville (SMALL_ROADS)
        total += SMALL_ROADS_W * getattr(l, "small_roads", 0.0)
    if defect_strict():                                    # défauts restants presque éliminatoires (DEFECT_STRICT)
        nd = len(defect_windows(l))
        if nd:
            total -= min(DEFECT_STRICT_MAX, DEFECT_STRICT_PEN * nd)
    if detour_score() and getattr(l, "detours", None):    # détours inutiles restants (DETOUR_SCORE, v18)
        total -= min(DETOUR_MAX_PENALTY, sum(DETOUR_PENALTY + DETOUR_PENALTY_PER_100M * (d["m"] - d["gap"]) / 100.0
                                             for d in l.detours))
    if l.scenery is not None:                             # zones industrielles et portuaires (entrepôts, camions)
        total -= min(INDUSTRIAL_MAX_PENALTY if ind_max is None else ind_max,
                     (INDUSTRIAL_PENALTY if ind_penalty is None else ind_penalty) * l.scenery.get("industrial", 0.0))
    lim = relief_limits().get(l.level)                    # relief au-dessus de la cible de l'allure (RELIEF_LIMITS)
    if lim and lim.get("pen_above") is not None and l.dplus_per_km > lim["pen_above"]:
        total -= min(RELIEF_OVER_MAX, lim.get("pen_per", RELIEF_OVER_PENALTY) * (l.dplus_per_km - lim["pen_above"]))
    return round(100 * max(0.0, total), 1)


def relief_ok(l) -> bool:
    """Boucle conforme aux règles obligatoires de relief de son allure (RELIEF_LIMITS ; toujours vraie si désactivé)."""
    lim = relief_limits().get(l.level)
    if not lim:
        return True
    if lim.get("max_dpk") is not None and l.dplus_per_km > lim["max_dpk"]:
        return False
    if lim.get("banned_cats") and any(c.get("category") in lim["banned_cats"] for c in l.terrain.get("climbs", [])):
        return False
    if lim.get("max_grade") is not None and (l.terrain.get("max_grade_pct") or 0.0) >= lim["max_grade"]:
        return False
    return True


def spare_relief() -> bool:
    """Repli de dernier recours contrôlé pour le relief (SPARE_RELIEF), réglable par fil d'exécution."""
    return bool(getattr(_TL, "spare_relief", SPARE_RELIEF))


def relief_hard_ok(l) -> bool:
    """Boucle de repli encore faisable à son allure (RELIEF_HARD ; toujours vraie sans SPARE_RELIEF)."""
    hard = RELIEF_HARD.get(l.level) if spare_relief() else None
    if not hard:
        return True
    if any(c.get("category") in hard["banned_cats"] for c in l.terrain.get("climbs", [])):
        return False
    if hard.get("max_dpk") is not None and l.dplus_per_km > hard["max_dpk"]:
        return False
    return (l.terrain.get("max_grade_pct") or 0.0) < hard["max_grade"]


def dirt_filter(pool: list, level: str, log) -> list:
    """Terre v17 : pool réduit aux boucles sous le plafond ; sans aucune, la moins terreuse seule, marquée
    unpaved_fallback (l'app l'annonce). Désactivé si DIRT_RULES est vide."""
    cap = dirt_rules().get("max_km")
    if not pool or cap is None:
        return pool
    km = lambda l: l.shares["unpaved"] * l.distance_m / 1000.0  # noqa: E731
    ok = [l for l in pool if km(l) <= cap]
    if ok:
        return ok
    least = min(pool, key=km)
    if fallback_best():                                      # la mieux notée parmi les quasi-moins terreuses
        least = max((l for l in pool if km(l) <= km(least) + FALLBACK_DIRT_TOL_KM), key=lambda l: l.score)
    least.unpaved_fallback = True
    log(f"    terre : aucune boucle sous {cap:g} km ({level}), la moins terreuse gardée ({km(least):.1f} km)")
    return [least]


def relief_filter(pool: list, level: str, log) -> list:
    """Pool réduit aux boucles conformes (relief_ok) ; sans aucune, la moins vallonnée seule, marquée relief_fallback."""
    lim = relief_limits().get(level)
    if not pool or not lim or not any(k in lim for k in ("max_dpk", "banned_cats", "max_grade")):
        return pool
    ok = [l for l in pool if relief_ok(l)]
    if ok:
        return ok
    flat = min(pool, key=lambda l: l.dplus_per_km)
    if fallback_best():                                      # la mieux notée parmi les quasi-moins vallonnées
        flat = max((l for l in pool if l.dplus_per_km <= flat.dplus_per_km + FALLBACK_RELIEF_TOL), key=lambda l: l.score)
    if not relief_hard_ok(flat):                            # même le repli n'est pas faisable à cette allure (B)
        log(f"    relief : aucune boucle faisable ({level}) : la moins vallonnée ({flat.dplus_per_km:.1f} m/km) a une "
            f"montée trop dure, une rampe de {flat.terrain.get('max_grade_pct') or 0:.0f} % ou un relief trop fort, "
            "durée non proposée")
        return []
    flat.relief_fallback = True
    log(f"    relief : aucune boucle conforme ({level}), la moins vallonnée gardée ({flat.dplus_per_km:.1f} m/km)")
    return [flat]


# --------------------------------------------------------------------------- génération
def time_ok(time_s: float, duration_h: float) -> bool:
    """Durée acceptable pour la durée visée (TIME_TOLERANCE, et plages sans chevauchement si DURATION_BINS)."""
    r = time_s / (duration_h * 3600.0)
    if DURATION_BINS and contiguous_bins() and duration_h in DURATION_SET:
        lo_m, hi_m = bin_minutes(duration_h)                 # plage sans trou (CONTIGUOUS_BINS)
        return lo_m <= time_s / 60.0 <= hi_m
    if abs(r - 1.0) > TIME_TOLERANCE:
        return False
    if not DURATION_BINS or duration_h not in DURATION_SET:
        return True
    i = DURATION_SET.index(duration_h)
    lo = (DURATION_SET[i - 1] + duration_h) / 2.0 / duration_h if i > 0 else 0.0
    hi = (DURATION_SET[i + 1] + duration_h) / 2.0 / duration_h if i + 1 < len(DURATION_SET) else 9.0
    m = bin_margin() or 0.0
    # marge vers le bas seulement (08/10/2026, 2e test : une durée qui prenait une boucle un peu trop longue vidait parfois
    # la durée suivante, calculée après elle : Argelaguer 4 h, Sant Celoni 6 h) ; une boucle un peu courte reste reprise
    return lo - m <= r <= hi


def _bin_strict(i: int) -> tuple:
    """Plage d'une durée en minutes, comme time_ok sans CONTIGUOUS_BINS (plages, marge vers le bas, TIME_TOLERANCE)."""
    d = DURATION_SET[i]
    lo = (DURATION_SET[i - 1] + d) / 2.0 / d if i > 0 else 0.0
    hi = (DURATION_SET[i + 1] + d) / 2.0 / d if i + 1 < len(DURATION_SET) else 9.0
    lo, hi = max(lo - (bin_margin() or 0.0), 1.0 - TIME_TOLERANCE), min(hi, 1.0 + TIME_TOLERANCE)
    return lo * d * 60.0, hi * d * 60.0


def bin_minutes(duration_h: float) -> tuple:
    """Plage sans trou (CONTIGUOUS_BINS) : la plage habituelle, étendue jusqu'au milieu d'un trou avec une durée voisine."""
    i = DURATION_SET.index(duration_h)
    lo, hi = _bin_strict(i)
    if i > 0:
        below = _bin_strict(i - 1)[1]
        if below < lo:
            lo = (below + lo) / 2.0
    if i + 1 < len(DURATION_SET):
        above = _bin_strict(i + 1)[0]
        if above > hi:
            hi = (hi + above) / 2.0
    return lo, hi


def order_bounds(options: list, level: str, duration_h: float) -> tuple:
    """Bornes de durée (s) d'une nouvelle boucle pour garder l'ordre des durées à cette allure (DURATION_BIN_MARGIN) :
    plus longue que la boucle RECOMMANDÉE des durées inférieures, plus courte que celle des durées supérieures.
    08/10/2026 : seulement les recommandées (une secondaire un peu longue bloquait toute la durée suivante : Argelaguer
    4 h, Castellfollit 5 h, Sant Celoni et Calella 6 h perdus) et un écart d'au moins ORDER_GAP_S."""
    lo, hi, dm = 0.0, float("inf"), round(duration_h * 60)
    for o in options:
        if o["level"] != level or not str(o.get("id", "")).endswith("-1"):   # -1 : boucle recommandée (to_json)
            continue
        if o["duration_target_min"] < dm:
            lo = max(lo, o["time_est_min"] * 60.0 + ORDER_GAP_S)
        elif o["duration_target_min"] > dm:
            hi = min(hi, o["time_est_min"] * 60.0 - ORDER_GAP_S)
    return lo, hi


def keep_order(pool: list, options: list, level: str, duration_h: float) -> list:
    """Pool restreint aux boucles qui respectent l'ordre des durées et ne sont pas déjà proposées à une autre durée de
    cette allure (08/10/2026 : à Pallejà, la même boucle de 5 h 41 en « 5 h » et en « 6 h ») ; seulement si
    DURATION_BIN_MARGIN est actif."""
    if not bin_margin():
        return pool
    lo, hi = order_bounds(options, level, duration_h)
    dm = round(duration_h * 60)
    taken = {o.get("route_key") for o in options if o["level"] == level and o["duration_target_min"] != dm}
    return [l for l in pool if lo <= l.time_s <= hi and route_key(simplify(l.coords, 10.0)) not in taken]   # comme to_json


def ordered_pool(gh, st, level: str, duration: float, candidates, log, options: list) -> list:
    """Pool d'une durée avec la marge et l'ordre des durées (keep_order). Filet (09/10/2026, 3e test de la marge : 65
    recommandées meilleures, mais 4 durées vidées sur 1 416, dont Torrelles 1 h Modéré : 53 min, pas assez plus longue que
    la boucle de 45 min, 52 min) : si la durée est vide, elle est recalculée avec les plages strictes (v16). La marge ne
    peut donc plus faire perdre une boucle."""
    pool = keep_order(level_pool(gh, st, level, duration, candidates, log), options, level, duration)
    if pool or not bin_margin():
        return pool
    had = hasattr(_TL, "bin_margin")
    old = getattr(_TL, "bin_margin", None)
    _TL.bin_margin = 0.0                                     # plages strictes, sans contrainte d'ordre (v16)
    try:
        pool = level_pool(gh, st, level, duration, candidates, log)
    finally:
        if had:
            _TL.bin_margin = old
        else:
            del _TL.bin_margin
    if pool:
        log(f"    marge : aucune boucle en gardant l'ordre des durées ; plages strictes reprises ({len(pool)} candidats)")
    return pool


def dirt_ok(l) -> bool:
    """Terre acceptable : part sous MAX_UNPAVED et, si DIRT_MAX_KM, pas plus de DIRT_MAX_KM km."""
    return l.shares["unpaved"] <= MAX_UNPAVED and (DIRT_MAX_KM is None or
                                                   l.shares["unpaved"] * l.distance_m / 1000.0 <= DIRT_MAX_KM)


def overlap_ok(l) -> bool:
    """Part répétée acceptable : sous MAX_OVERLAP, ou aller-retour sur piste cyclable (OUTBACK_OK). Avec
    CORRIDOR_OVERLAP, acceptable aussi selon la mesure v16 : le couloir pèse dans la note, sans vider de durée."""
    def ok(ov, cyc):
        return ov <= MAX_OVERLAP or (OUTBACK_OK and ov <= OUTBACK_MAX_OVERLAP and cyc >= OUTBACK_MIN_CYCLE)
    return ok(l.overlap, l.repeat_cycle_share) or (
        hasattr(l, "overlap_v16") and ok(l.overlap_v16, l.repeat_cycle_share_v16))


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
            ok = (loop is not None and time_ok(loop.time_s, duration_h) and overlap_ok(loop)
                  and loop.u_turns <= MAX_UTURNS and dirt_ok(loop))
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
        if not time_ok(cand.time_s, duration_h):       # une seule retouche de la distance par candidat
            path = gh.round_trip(lon, lat, profile, min(250_000.0, max(3_000.0, dist / ratio)), seed, heading)
            cand = analyse(path, level, profile, duration_h, seed, heading) if path else None
            if cand is None or not time_ok(cand.time_s, duration_h):
                rejects["durée"] += 1
                continue
        if not overlap_ok(cand):
            rejects["tronçons répétés"] += 1
            continue
        if cand.u_turns > MAX_UTURNS:
            rejects["demi-tours"] += 1
            continue
        if not dirt_ok(cand):
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


def travel_heading(coords, k, back=True, ahead=True) -> float:
    """Cap de la boucle au point k (sens de circulation), mesuré sur ~20 m avant et/ou après."""
    i = j = k
    while back and i > 0 and haversine(coords[i][0], coords[i][1], coords[k][0], coords[k][1]) < 20.0:
        i -= 1
    while ahead and j < len(coords) - 1 and haversine(coords[j][0], coords[j][1], coords[k][0], coords[k][1]) < 20.0:
        j += 1
    if i == j:
        i, j = max(0, k - 1), min(len(coords) - 1, k + 1)
    return bearing(coords[i][0], coords[i][1], coords[j][0], coords[j][1])


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
        out.append([c[k][0], c[k][1], travel_heading(c, k)])   # sens de circulation (utilisé si DETOUR_FIX)
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
        if retouch_round_robin():                            # une famille après l'autre (mer, lieu, rivière, vert…)
            fams: dict = {}
            for t in trials:
                fams.setdefault(t[2].split("/")[0], []).append(t)
            queues = list(fams.values())
            trials = [q[i] for i in range(max(len(q) for q in queues)) for q in queues if i < len(q)] if queues else []
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
                if detail is not None:
                    detail.append(f"  (écarté sans calcul : durée prévue {pred:.2f}) {desc}")
                continue
            kept.append((desc, way, kind, pred))
        if detail is not None:
            for desc, way, kind, pred in kept[RETOUCH_MAX_TRIALS:]:
                detail.append(f"  (non essayé, au-delà de {RETOUCH_MAX_TRIALS} essais : durée prévue {pred:.2f}) {desc}")
        for k, (desc, way, kind, pred) in enumerate(kept[:RETOUCH_MAX_TRIALS]):
            n_try += 1
            path = gh.via(way, base.profile, pass_through=True)
            loop = analyse(path, level, base.profile, duration_h, 1000 + 100 * bi + k, None) if path else None
            why = None
            if loop is None:
                why = f"pas d'itinéraire ({gh.last_error})" if path is None else "boucle invalide"
            elif not time_ok(loop.time_s, duration_h):
                why = "durée"
            elif not overlap_ok(loop):
                why = f"tronçons répétés ({loop.overlap:.0%})"
            elif loop.u_turns > MAX_UTURNS:
                why = f"demi-tours ({loop.u_turns})"
            elif not dirt_ok(loop):
                why = "terre"
            elif loop.score <= base.score:
                why = "note plus basse"
            else:
                found.append(loop)
            if (why == "durée" and retouch_shorten() and loop.time_s > target_s
                    and loop.score >= base.score + RETOUCH_SHORTEN_GAIN):   # trop longue mais meilleure : raccourcir
                extra = [p for p in way[1:-1] if p not in anchors]          # points ajoutés par l'essai : gardés
                r0, L0 = loop.time_s / target_s, max(poly_km(way), 0.1)
                cands = [(abs(r0 * poly_km(way[:j] + way[j + 1:]) / L0 - RETOUCH_SHORTEN_AIM), j)
                         for j in range(1, len(way) - 1) if way[j] not in extra]
                if cands:
                    j = min(cands)[1]
                    w2 = way[:j] + way[j + 1:]
                    n_try += 1
                    p2 = gh.via(w2, base.profile, pass_through=True)
                    l2 = analyse(p2, level, base.profile, duration_h, 1050 + 100 * bi + k, None) if p2 else None
                    ok2 = (l2 is not None and time_ok(l2.time_s, duration_h) and overlap_ok(l2)
                           and l2.u_turns <= MAX_UTURNS and dirt_ok(l2) and l2.score > base.score)
                    if detail is not None:
                        detail.append(f"    raccourcie sans le point {j} -> " + ("pas d'itinéraire" if l2 is None else
                                      f"note {l2.score:.1f}, {l2.distance_m / 1000:.1f} km, {l2.time_s / 60:.0f} min")
                                      + (" : GARDÉE" if ok2 else " : rejetée"))
                    if ok2:
                        l2.shortened = True
                        found.append(l2)
                        why = "durée, raccourcie gardée"
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
        if loop is None or not time_ok(loop.time_s, duration_h):
            continue
        if not overlap_ok(loop) or loop.u_turns > MAX_UTURNS:
            continue
        if not dirt_ok(loop):
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
    le début et la fin de chaque intervalle sauté sont gardés. Chaque point porte le sens de circulation de la boucle
    (3e valeur, utilisée si DETOUR_FIX) : à l'entrée d'un intervalle sauté celui d'avant, à la sortie celui d'après."""
    out, acc, cuts = [[coords[0][0], coords[0][1]]], 0.0, set()
    starts, ends = {a for a, _ in skip}, {b for _, b in skip}
    for a, b in skip:
        cuts.add(a)
        cuts.add(b)
    inside = lambda i: any(a < i < b for a, b in skip)  # noqa: E731
    for i in range(1, len(coords) - 1):
        acc += haversine(coords[i - 1][0], coords[i - 1][1], coords[i][0], coords[i][1])
        if inside(i):
            continue
        if i in cuts or acc >= step_m:
            out.append([coords[i][0], coords[i][1], travel_heading(coords, i, back=i not in ends, ahead=i not in starts)])
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
        a, b, t = i - 1, i + 1, i
        if d < 150:
            j = None
            if spur_turn_window():                          # demi-tour par un rond-point ou une petite boucle au bout
                h0 = bearing(k_[i - 1][0], k_[i - 1][1], k_[i][0], k_[i][1])
                for x in range(i + 1, len(k_) - 1):
                    if cum[x] - cum[i] > SPUR_TURN_WINDOW_M:
                        break
                    dx = abs((bearing(k_[x][0], k_[x][1], k_[x + 1][0], k_[x + 1][1]) - h0 + 180) % 360 - 180)
                    if (dx >= 150 and near(k_[i - 1], k_[x + 1])
                            and (len(k_[i - 1]) < 3 or abs(k_[i - 1][2] - k_[x + 1][2]) <= CORRIDOR_DZ_M)):
                        j = x
                        break
            if j is None:
                i += 1
                continue
            a, b = i - 1, j + 1
            t = max(range(i, j + 1), key=lambda y: haversine(k_[a][0], k_[a][1], k_[y][0], k_[y][1]))
        while a > 0 and b < len(k_) - 1 and (near(k_[a - 1], k_[b + 1]) or
                                              any(near(k_[a - 1], k_[x]) for x in range(b, min(len(k_), b + 4)))):
            a -= 1
            b += 1
        out.append({"a": idx[a], "t": idx[t], "b": idx[b], "m": cum[t] - cum[a],
                    "start_m": cum[a], "end_m": cum[-1] - cum[b]})
        i = b
    return out


def block_detours(coords) -> list:
    """Détours en pâté de maisons (voir DETOUR_FIX) : la boucle revient à moins de DETOUR_GAP_M (et du quart du chemin)
    après DETOUR_MIN_M à DETOUR_MAX_M, sans monter de DETOUR_CLIMB_M ni repasser par la même rue (éperon), hors départ et
    arrivée. [{"a": indice d'entrée, "b": indice de sortie, "m": chemin parcouru, "gap": distance entrée-sortie}]."""
    import numpy as np
    idx = [0]
    for i in range(1, len(coords)):
        if haversine(coords[idx[-1]][0], coords[idx[-1]][1], coords[i][0], coords[i][1]) >= 15.0:
            idx.append(i)
    n = len(idx)
    if n < 4:
        return []
    lat0 = math.radians(coords[0][1])
    x = np.array([coords[i][0] for i in idx]) * 111320.0 * math.cos(lat0)
    y = np.array([coords[i][1] for i in idx]) * 110540.0
    acc = np.concatenate([[0.0], np.cumsum(np.hypot(np.diff(x), np.diff(y)))])
    total = acc[-1]
    best = np.full(n, -1)
    for k in range(1, n):                                    # pour chaque entrée i : la sortie valide la plus lointaine
        L = acc[k:] - acc[:-k]
        if L.min() > DETOUR_MAX_M:
            break
        gap = np.hypot(x[k:] - x[:-k], y[k:] - y[:-k])
        ok = (L >= DETOUR_MIN_M) & (L <= DETOUR_MAX_M) & (gap <= DETOUR_GAP_M) & (gap < DETOUR_RATIO * L)
        ok &= (acc[:-k] >= DETOUR_END_M) & (total - acc[k:] >= DETOUR_END_M)
        best[:-k][ok] = np.arange(k, n)[ok]
    out, i = [], 0
    while i < n:
        j = int(best[i])
        if j < 0:
            i += 1
            continue
        seg = [coords[q] for q in idx[i:j + 1]]
        z = [c[2] for c in seg if len(c) > 2]
        h = len(seg) // 2
        same = sum(any(haversine(p[0], p[1], q[0], q[1]) < 25.0 for q in seg[h:]) for p in seg[:h]) / max(1, h)
        if (not z or max(z) - min(z) < DETOUR_CLIMB_M) and same <= 0.5:
            out.append({"a": idx[i], "b": idx[j], "m": round(float(acc[j] - acc[i])),
                        "gap": round(float(math.hypot(x[j] - x[i], y[j] - y[i])))})
        i = j
    return out


def trim_detours(gh, loop, level, duration_h):
    """La même boucle, recalculée sans ses détours en pâté de maisons (points de passage pris de part et d'autre, avec le
    sens de circulation)."""
    if not loop.detours:
        return None
    way = route_waypoints(loop.coords, skip=[(d["a"], d["b"]) for d in loop.detours])
    path = gh.via(way, loop.profile, pass_through=True)
    new = analyse(path, level, loop.profile, duration_h, loop.seed, loop.heading) if path else None
    if (new is None or not time_ok(new.time_s, duration_h) or not overlap_ok(new)
            or new.u_turns > MAX_UTURNS or not dirt_ok(new) or len(new.detours) >= len(loop.detours)):
        return None
    return new


def natural_tip(p) -> bool:
    """Demi-tour à un endroit naturel (relecture de Florent, 09/10/2026 : sinon « pourquoi ici ? ») : sommet ou col nommé
    (OSM) à ~150 m, ou rivière ou plan d'eau à ~60 m (un creux, un pont). Sans données chargées : pas de jugement (vrai)."""
    import shapely
    if not POIS and LANDSCAPE is None:
        return True
    pt = shapely.Point(p[0], p[1])
    for k in ("peak", "pass"):
        if POIS and k in POIS and len(POIS[k][0].query(pt, predicate="dwithin", distance=REMARKABLE_NEAR_DEG)):
            return True
    if LANDSCAPE is not None:
        for k in ("water", "river"):
            tr = LANDSCAPE.trees.get(k)
            if tr is not None and len(tr.query(pt, predicate="dwithin", distance=0.0006)):
                return True
    return False


def backtrack_useful(coords, a: int, b: int, level: str, cyc=None, unp=None, t=None, foot=None):
    """Pourquoi le passage coords[a..b], où la boucle revient sur ses pas, est utile (voir BACKTRACK_RULES) : "lieu",
    "belvédère", "relief", "nature" ou "piste" ; None s'il n'apporte rien. cyc : lettre de chaque tronçon (road_edges) ;
    unp : lettre de chaque tronçon (unpaved_edges) ; t : indice du demi-tour (sinon le point le plus éloigné de l'entrée)."""
    import numpy as np
    import shapely
    seg = coords[a:b + 1]
    if len(seg) < 2:
        return None
    if foot is not None and foot_backtrack():               # jamais à pied, même pour un lieu (FOOT_BACKTRACK)
        walk = sum(haversine(coords[i][0], coords[i][1], coords[i + 1][0], coords[i + 1][1])
                   for i in range(a, min(b, len(foot))) if foot[i])
        if walk > FOOT_BACKTRACK_MAX_M:
            return None
    cap = backtrack_max_share()
    if cap is not None:                                      # trop long : la boucle devient un aller-retour
        part = sum(haversine(seg[i][0], seg[i][1], seg[i + 1][0], seg[i + 1][1]) for i in range(len(seg) - 1))
        whole = part + sum(haversine(coords[i][0], coords[i][1], coords[i + 1][0], coords[i + 1][1])
                           for i in list(range(0, a)) + list(range(b, len(coords) - 1)))
        if part > cap * whole:
            return None
    pts = shapely.points(np.array([(c[0], c[1]) for c in seg[::max(1, len(seg) // 30)]]))
    if (POIS and "lieu" in POIS and len(POIS["lieu"][0].query(pts, predicate="dwithin", distance=REMARKABLE_NEAR_DEG)[0])
            or dest_heritage() and len(POIS["patrimoine"][0].query(
                shapely.points(np.array([[coords[t if t is not None else b][0], coords[t if t is not None else b][1]]])),
                predicate="dwithin", distance=REMARKABLE_NEAR_DEG)[0])):
        return "lieu"                                        # un lieu remarquable vaut le détour, même sur la fin en terre
    if unp is not None:                                      # jamais sur la terre ou le gravier pour revenir sur ses pas
        dirt = sum(haversine(coords[i][0], coords[i][1], coords[i + 1][0], coords[i + 1][1])
                   for i in range(a, min(b, len(unp))) if unp[i] in ("u", "p"))
        if dirt > BACKTRACK_DIRT_MAX_M:
            return None
    tv = LANDSCAPE.trees.get("view") if LANDSCAPE is not None else None
    if t is None:                                            # demi-tour : le point le plus éloigné de l'entrée
        t = max(range(a, b + 1), key=lambda i: haversine(coords[a][0], coords[a][1], coords[i][0], coords[i][1]))
    if tv is not None:
        if turn_rules():                                     # le demi-tour doit être au point de vue (TURN_RULES)
            seen_view = len(tv.query(shapely.points(np.array([[coords[t][0], coords[t][1]]])), predicate="dwithin",
                                     distance=VIEW_NEAR_DEG)[0])
        else:
            seen_view = len(tv.query(pts, predicate="dwithin", distance=VIEW_NEAR_DEG)[0])
        if seen_view:
            return "belvédère"
    if (level in BACKTRACK_RELIEF_LEVELS and len(seg[0]) > 2
            and max(c[2] for c in seg) - seg[0][2] >= BACKTRACK_CLIMB_M and natural_tip(coords[t])):
        return "relief"                                      # monte, et fait demi-tour à un sommet, un col ou au bord de l'eau
    long_ = sum(haversine(seg[i][0], seg[i][1], seg[i + 1][0], seg[i + 1][1]) for i in range(len(seg) - 1))
    if long_ < BACKTRACK_PLEASANT_MIN_M:                     # trop court pour « prolonger une route agréable »
        return None
    climb_mid = (turn_rules() and len(seg[0]) > 2 and coords[t][2] - seg[0][2] >= TURN_CLIMB_M
                 and not natural_tip(coords[t]))           # monte puis fait demi-tour en pleine montée (TURN_RULES)
    green = None
    if LANDSCAPE is not None:
        hit = np.zeros(len(pts), dtype=bool)
        for key in ("water", "river", "sea", "forest", "protected"):
            tr = LANDSCAPE.trees.get(key)
            if tr is not None:
                hit[np.unique(tr.query(pts, predicate="dwithin", distance=0.0006)[0])] = True
        nz = noisy_mask(pts)                                 # pas de nature le long d'une voie rapide (MOTORWAY_V2)
        if nz is not None:
            hit &= ~nz
        green = float(hit.mean())
        if green >= BACKTRACK_SHARE:
            return "montée" if climb_mid else "nature"
    if cyc is not None and (green is None or green >= BACKTRACK_PISTE_GREEN):
        m = [(haversine(coords[i][0], coords[i][1], coords[i + 1][0], coords[i + 1][1]), cyc[i] == "c")
             for i in range(a, min(b, len(cyc)))]
        tot = sum(x for x, _ in m)
        if tot > 0 and sum(x for x, c in m if c) / tot >= BACKTRACK_SHARE:
            return "montée" if climb_mid else "piste"
    return None


def useful_windows(coords, level: str, cyc=None, unp=None, foot=None) -> list:
    """Allers-retours utiles de la boucle (voir BACKTRACK_RULES) : [(a, b, t, raison)] (entrée, sortie, demi-tour)."""
    out = []
    for s in spur_list(coords):
        why = backtrack_useful(coords, s["a"], s["b"], level, cyc, unp, s["t"], foot)
        if why:
            out.append((s["a"], s["b"], s["t"], why))
    return out


def unjustified_spurs(coords, level: str | None = None, cyc=None, unp=None, foot=None) -> list:
    """Éperons d'au moins SPUR_MIN_M, hors départ et arrivée, qui n'apportent rien (voir SPUR_FIX ; avec
    BACKTRACK_RULES, jugés par backtrack_useful)."""
    import numpy as np
    import shapely
    out = []
    rules = backtrack_rules() and level is not None
    for s in spur_list(coords):
        if s["m"] < SPUR_MIN_M or s["start_m"] < SPUR_END_M or s["end_m"] < SPUR_END_M:
            continue
        if rules:
            if backtrack_useful(coords, s["a"], s["b"], level, cyc, unp, s["t"], foot) is None:
                a, b, m = s["a"], s["b"], s["m"]
                if foot is not None and foot_backtrack():   # seule la fin à pied est retirée si le reste est utile
                    walk = [i for i in range(a, min(b, len(foot))) if foot[i]]
                    w_in, w_out = [i for i in walk if i < s["t"]], [i for i in walk if i >= s["t"]]
                    if (w_in and w_out and w_in[0] > a + 1 and w_out[-1] + 2 < b
                            and backtrack_useful(coords, a, w_in[0], level, cyc, unp, w_in[0], foot) is not None):
                        a, b = w_in[0], w_out[-1] + 1
                        m = sum(haversine(coords[i][0], coords[i][1], coords[i + 1][0], coords[i + 1][1])
                                for i in range(a, b))
                out.append({"km": None, "m": round(m), "a": a, "b": b,
                            "lon": round(coords[s["t"]][0], 5), "lat": round(coords[s["t"]][1], 5)})
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
    if (new is None or not time_ok(new.time_s, duration_h) or not overlap_ok(new)
            or new.u_turns > MAX_UTURNS or not dirt_ok(new) or len(new.spurs) >= len(loop.spurs)):
        return None
    return new


def previous_candidates(gh, st, level, duration_h) -> list:
    """Boucles publiées de la version précédente pour ce départ, cette allure et cette durée (KEEP_PREVIOUS), recalculées
    et notées avec les règles actuelles : une nouvelle version ne perd plus une bonne boucle par malchance."""
    out = []
    prev = st.get("previous") or {}
    if previous_any_duration():                              # toutes les durées de l'allure (reclassement)
        items = [pc for (lv, _), lst in sorted(prev.items()) if lv == level for pc in lst]
    else:
        items = prev.get((level, round(duration_h * 60)), [])
    for profile, coords in items:
        way = route_waypoints(coords, step_m=previous_step())
        way[0] = way[-1] = [st["lon"], st["lat"]]          # départ actuel (il a pu être déplacé : start_moves.json)
        path = gh.via(way, profile, pass_through=True)
        loop = analyse(path, level, profile, duration_h, 2000, None) if path else None
        if (loop is None or not time_ok(loop.time_s, duration_h) or not overlap_ok(loop)
                or loop.u_turns > MAX_UTURNS or not dirt_ok(loop)):
            continue
        out.append(loop)
    return out


def _fit_via(gh, level, profile, duration_h, make_way, x0, seed, tries=3):
    """Boucle par des points imposés, ajustée sur la durée : make_way(x) donne les points pour un réglage x (distance),
    x est corrigé selon l'écart de durée (2 retouches au plus). Renvoie la dernière boucle valide ou None."""
    target_s, x, loop = duration_h * 3600.0, x0, None
    for _ in range(tries):
        path = gh.via(make_way(x), profile)
        loop = analyse(path, level, profile, duration_h, seed, None) if path else None
        if loop is None:
            return None
        ratio = loop.time_s / target_s
        if abs(ratio - 1.0) <= 0.05:
            break
        x = x / ratio
    if (not time_ok(loop.time_s, duration_h) or not overlap_ok(loop) or loop.u_turns > MAX_UTURNS
            or not dirt_ok(loop)):
        return None
    return loop


def ar_candidates(gh, st, level, profile, duration_h, log) -> list:
    """Aller vers un endroit agréable proche (bord de mer, rivière, grand espace vert) puis rentrer (AR_DRAWS, Florent,
    06/10/2026 : l'Hospitalet 1 h vers le delta, 41 contre 24) : point de l'endroit à la bonne distance, dans la
    direction de son point le plus proche ; le calcul choisit librement l'aller et le retour."""
    if LANDSCAPE is None:
        return []
    import shapely
    lon, lat = st["lon"], st["lat"]
    flat_ms = speed_from_power(level_watts(level, duration_h), 0.0) * REAL_WORLD_FACTOR
    loop_km = 0.8 * flat_ms * duration_h * 3600.0 / 1000.0
    pt = shapely.Point(lon, lat)
    found = []
    for cat, keys in (("mer", ("sea",)), ("riviere", ("river",)), ("vert", ("forest", "protected"))):
        near = nearest_attraction(cat, lon, lat, 0.45 * loop_km)
        if not near:
            continue
        samples = []
        for key in keys:
            tree = LANDSCAPE.trees.get(key)
            if tree is None:
                continue
            for i in tree.query(pt.buffer(0.45 * loop_km / 90.0)):
                gm = tree.geometries[int(i)]
                if cat == "vert" and (gm.geom_type not in ("Polygon", "MultiPolygon") or gm.area < TARGET_GREEN_MIN_DEG2):
                    continue
                line = gm.boundary if gm.geom_type in ("Polygon", "MultiPolygon") else gm
                for part in getattr(line, "geoms", [line]):
                    n = max(2, int(part.length / 0.004))
                    for k in range(n + 1):
                        q = part.interpolate(k / n, normalized=True)
                        d = haversine(lon, lat, q.x, q.y) / 1000.0
                        if 0.15 * loop_km <= d <= 0.45 * loop_km:
                            samples.append((q.x, q.y, d, bearing(lon, lat, q.x, q.y)))
        # deux directions bien distinctes (l'Hospitalet : l'endroit vert le plus proche, Montjuïc, n'est pas le delta)
        dirs = []
        for sm in sorted(samples, key=lambda s: abs(s[2] - 0.32 * loop_km)):
            if all(abs((sm[3] - b + 180) % 360 - 180) > 60 for b in dirs):
                dirs.append(sm[3])
            if len(dirs) >= 2:
                break
        for b0 in dirs:
            side = [sm for sm in samples if abs((sm[3] - b0 + 180) % 360 - 180) <= 30]
            pick = lambda want, side=side: min(side, key=lambda s: abs(s[2] - want))  # noqa: E731
            loop = _fit_via(gh, level, profile, duration_h,
                            lambda w, pick=pick: [[lon, lat], list(pick(w)[:2]), [lon, lat]], 0.32 * loop_km, 960, tries=2)
            if loop is not None:
                found.append(loop)
    log(f"    allers-retours vers un endroit agréable : {len(found)} valide(s)")
    return found


def lieu_tour_candidates(gh, st, level, profile, duration_h, log) -> list:
    """Monter au lieu remarquable puis en faire le tour avant de rentrer (LIEU_TOUR, Florent, 06/10/2026 : l'Eixample
    1 h, Montjuïc seul durait 46 min) : un point à r km du lieu, de chaque côté, r ajusté sur la durée."""
    if not (POIS and "lieu" in POIS):
        return []
    lon, lat = st["lon"], st["lat"]
    flat_ms = speed_from_power(level_watts(level, duration_h), 0.0) * REAL_WORLD_FACTOR
    loop_km = 0.8 * flat_ms * duration_h * 3600.0 / 1000.0
    pts = attraction_points(lon, lat, TARGET_MAX_SHARE * loop_km)
    found = []
    for cat in [c for c in pts if c.startswith("lieu")]:
        x, y, km = pts[cat]
        b = bearing(lon, lat, x, y)
        for side in (90.0, -90.0):
            loop = _fit_via(gh, level, profile, duration_h,
                            lambda r: [[lon, lat], [x, y], list(destination(x, y, b + side, max(0.3, r))), [lon, lat]],
                            max(0.5, 0.12 * loop_km), 970)
            if loop is not None:
                found.append(loop)
    log(f"    tours de lieux remarquables : {len(found)} valide(s)")
    return found


def level_pool(gh, st, level: str, duration: float, candidates, log, gate="defaut") -> list:
    """Tous les candidats valides d'un départ pour une durée et un niveau : tirages ordinaires de chaque profil, plus
    les tirages « montée » en niveau sportif. Utilisé par la génération ET par le diagnostic (nature_check --probe)."""
    pool: list = []
    runs = [(profile, gh, candidates, profile) for profile in LEVELS[level]["profiles"]]
    flat = (relief_limits().get(level) or {}).get("flat_profile")
    if flat:                                                 # relief v17 : tirages qui évitent les côtes (Tranquille)
        runs.append((flat, gh, candidates, flat))
    if level in CLIMB_LEVELS:
        runs.append(("sport", _ClimbGH(gh), CLIMB_CANDIDATES, "sport + montée"))
    spare: list = []
    for profile, client, cands, label in runs:
        log(f"  {duration:g} h / {level} / {label}")
        found, rejects = fit_and_sample(client, st, level, profile, duration, cands, log, fallback=spare)
        why = ", ".join(f"{k} {v}" for k, v in rejects.items() if v)
        log(f"    {len(found)} candidats valides" + (f" (rejetés : {why})" if why else ""))
        pool.extend(found)
    gate = TARGETED_GATE if gate == "defaut" else gate
    targeted = True
    if gate is not None:                                     # tirages ciblés « de rattrapage » (TARGETED_GATE)
        if KEEP_PREVIOUS and st.get("previous"):
            pool.extend(previous_candidates(gh, st, level, duration))
        best = max((l.score for l in pool), default=0.0)
        targeted = best < gate
        if not targeted:
            log(f"    tirages ciblés sautés (meilleure boucle {best:.1f} >= {gate:g})")
    if targeted:
        pool.extend(target_candidates(gh, st, level, LEVELS[level]["profiles"][0], duration, log, fallback=spare))
        if OUTBACK_DRAWS:
            pool.extend(outback_candidates(gh, st, level, LEVELS[level]["profiles"][0], duration, log))
        if AR_DRAWS:
            pool.extend(ar_candidates(gh, st, level, LEVELS[level]["profiles"][0], duration, log))
        if LIEU_TOUR:
            pool.extend(lieu_tour_candidates(gh, st, level, LEVELS[level]["profiles"][0], duration, log))
    if goal_return():                                        # v20 : aller vers un but, retour par un autre chemin
        pool.extend(goal_candidates(gh, st, level, LEVELS[level]["profiles"][0], duration, log))
    if gate is None and KEEP_PREVIOUS and st.get("previous") and getattr(_TL, "keep_previous", True):
        pool.extend(previous_candidates(gh, st, level, duration))
    if RETOUCH:
        pool.extend(retouch_candidates(gh, st, level, duration, pool, log))
    if scenic_retouch() and pool:                            # voies agréables (SCENIC_RETOUCH)
        pool.extend(scenic_candidates(gh, st, level, duration, pool, log))
    if axis_bypass() and pool:                               # contourner les grands axes (AXIS_BYPASS)
        pool.extend(axis_candidates(gh, st, level, duration, pool, log))
    if crossover() and len(pool) >= 2:                       # croisement des meilleures boucles (CROSSOVER, v17)
        pool.extend(crossover_candidates(gh, st, level, duration, pool, log))
    if SPUR_FIX:                                             # couper les éperons des meilleures candidates
        for l in sorted([x for x in pool if x.spurs], key=lambda x: x.score, reverse=True)[:SPUR_TRIM_TOP]:
            t = trim_spurs(gh, l, level, duration)
            if t is not None:
                pool.append(t)
    if turn_rules():                                         # allers-retours prolongés jusqu'au lieu tout proche
        for l in sorted([x for x in pool if getattr(x, "backtracks", None)], key=lambda x: x.score,
                        reverse=True)[:TURN_EXTEND_TOP]:
            pool.extend(extend_to_place(gh, l, level, duration))
    if appendix_rule():                                      # la même boucle sans sa boucle accrochée (APPENDIX_RULE)
        for l in sorted([x for x in pool if getattr(x, "appendices", None)], key=lambda x: x.score,
                        reverse=True)[:APPENDIX_TRIM_TOP]:
            way = route_waypoints(l.coords, skip=[(s["a"], s["b"]) for s in l.appendices])
            path = gh.via(way, l.profile, pass_through=True)
            t = analyse(path, level, l.profile, duration, l.seed, l.heading) if path else None
            if (t is not None and time_ok(t.time_s, duration) and overlap_ok(t) and t.u_turns <= MAX_UTURNS
                    and dirt_ok(t) and len(t.appendices) < len(l.appendices)):
                pool.append(t)
    if detour_fix():                                         # enlever les détours des meilleures candidates
        for l in sorted([x for x in pool if x.detours], key=lambda x: x.score, reverse=True)[:DETOUR_TRIM_TOP]:
            t = trim_detours(gh, l, level, duration)
            if t is not None and t.score >= l.score - DETOUR_SCORE_SLACK:
                pool[next(k for k, x in enumerate(pool) if x is l)] = t
    if pool:
        return relief_filter(dirt_filter(pool, level, log), level, log)
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
        return relief_filter(dirt_filter(pool, level, log), level, log)
    if spare:
        log(f"    repli : {len(spare)} candidat(s) avec un peu de terre (au plus {FALLBACK_UNPAVED_KM:g} km / "
            f"{FALLBACK_UNPAVED_SHARE:.0%}), signalé(s)")
    if spare and spare_relief():                             # le repli passe aussi par les règles de relief (A)
        return relief_filter(spare, level, log)
    return spare


def crossover_candidates(gh, st, level, duration_h, pool, log, detail=None) -> list:
    """Boucles « croisées » (voir CROSSOVER) : début d'une bonne boucle, fin d'une autre, raccordées là où elles se
    croisent ; mêmes filtres que les autres tirages. detail : une ligne par essai (diagnostic)."""
    import numpy as np
    target_s = duration_h * 3600.0
    tops = []
    for l in sorted(pool, key=lambda x: x.score, reverse=True):
        if all(similarity(l, b) < 0.8 for b in tops):
            tops.append(l)
        if len(tops) >= CROSS_TOP:
            break
    parents = list(tops)
    if cross_neighbours() and st.get("previous") and duration_h in DURATION_SET:
        i = DURATION_SET.index(duration_h)                   # boucles publiées des durées voisines, même allure
        for j in (i - 1, i + 1):
            if not 0 <= j < len(DURATION_SET):
                continue
            for profile, coords in (st["previous"].get((level, round(DURATION_SET[j] * 60))) or [])[:CROSS_NEIGHBOUR_LOOPS]:
                way = route_waypoints(coords)
                way[0] = way[-1] = [st["lon"], st["lat"]]
                p = gh.via(way, profile, pass_through=True)
                l = analyse(p, level, profile, duration_h, 2100, None) if p else None
                if l is not None:
                    parents.append(l)
    adopted = []
    if cross_spatial():                                      # boucles des départs voisins, adaptées à ce départ
        home = [st["lon"], st["lat"]]
        det = detail if detail is not None else getattr(_TL, "trace_detail", None)
        tries = []
        for n, (profile, coords, nsid, nlab, ntime) in enumerate(neighbour_loops(st, level, duration_h)):
            if round(ntime) and abs(ntime * 60.0 / target_s - 1.0) <= 0.15 + 1e-9 or not spatial_graft():
                k = min(range(len(coords)), key=lambda i: haversine(home[0], home[1], coords[i][0], coords[i][1]))
                rot = coords[k:] + coords[1:k + 1]           # même boucle, commencée au point le plus proche du départ
                tries.append((profile, [home] + route_waypoints(rot) + [home], nsid, nlab, 3500 + n))
            if spatial_graft():                              # greffe : la sortie de ville du voisin remplacée par la nôtre
                gw = graft_way(home, coords, ntime, target_s)
                if gw is not None:
                    tries.append((profile, gw[0], nsid, f"{nlab}, greffée ({gw[2] / 1000:.1f} km d'approche, "
                                  f"{gw[1] / 60:.0f} min prévues)", 3600 + n))
                elif det is not None:
                    det.append(f"  départ voisin {nsid} ({nlab}) greffé : durée estimée trop loin de la cible")
        for profile, way, nsid, nlab, seed in tries:
            p = gh.via(way, profile, pass_through=True)
            l = analyse(p, level, profile, duration_h, seed, None) if p else None
            if l is not None:
                parents.append(l)
                if (time_ok(l.time_s, duration_h) and overlap_ok(l) and l.u_turns <= MAX_UTURNS and dirt_ok(l)):
                    adopted.append(l)
            if det is not None:                              # chaque boucle voisine : adoptable ou pourquoi pas
                why = ("pas d'itinéraire" if l is None else "durée" if not time_ok(l.time_s, duration_h) else
                       f"répétitions ({l.overlap:.0%})" if not overlap_ok(l) else f"demi-tours ({l.u_turns})"
                       if l.u_turns > MAX_UTURNS else "terre" if not dirt_ok(l) else "adoptable")
                det.append(f"  départ voisin {nsid} ({nlab}) -> " + ("" if l is None else
                           f"note {l.score:.1f}, {l.distance_m / 1000:.1f} km, {l.time_s / 60:.0f} min : ") + why)
        if detail is not None:
            detail.append(f"  départs voisins : {len(adopted)} boucle(s) adoptable(s)")

    def sampled(l):                                          # un point tous les ~50 m : (indices, x m, y m, part du parcours)
        c = l.coords
        cum = [0.0]
        for a, b in zip(c, c[1:]):
            cum.append(cum[-1] + haversine(a[0], a[1], b[0], b[1]))
        idx, last = [], -1e9
        for i, d in enumerate(cum):
            if d - last >= 50.0:
                idx.append(i)
                last = d
        kx = 111320.0 * math.cos(math.radians(c[0][1]))
        return (np.array(idx), np.array([c[i][0] * kx for i in idx]), np.array([c[i][1] * 110540.0 for i in idx]),
                np.array([cum[i] / max(cum[-1], 1.0) for i in idx]))
    samp = {id(l): sampled(l) for l in parents}
    plans = []
    for A in parents:
        for B in parents:
            if A is B:
                continue
            ia, xa, ya, fa = samp[id(A)]
            ib, xb, yb, fb = samp[id(B)]
            ma, mb = (fa > 0.2) & (fa < 0.8), (fb > 0.2) & (fb < 0.8)
            if not ma.any() or not mb.any():
                continue
            d = np.hypot(xa[ma][:, None] - xb[mb][None, :], ya[ma][:, None] - yb[mb][None, :])
            pa, pb = np.nonzero(d < CROSS_NEAR_M)
            if not len(pa):
                continue
            # durée prévue : part de A jusqu'au raccord + part de B après le raccord
            est = (A.time_s * fa[ma][pa] + B.time_s * (1.0 - fb[mb][pb])) / target_s
            k = int(np.argmin(np.abs(est - 1.0)))
            if abs(est[k] - 1.0) > 0.12:
                continue
            plans.append((abs(est[k] - 1.0), A, B, int(ia[ma][pa[k]]), int(ib[mb][pb[k]]), float(est[k])))
    plans.sort(key=lambda p: (p[0], -(p[1].score + p[2].score)))
    found = []
    for n, (_, A, B, i, j, est) in enumerate(plans[:CROSS_MAX_TRIALS]):
        way = route_waypoints(A.coords[:i + 1]) + route_waypoints(B.coords[j:])[1:]
        path = gh.via(way, A.profile, pass_through=True)
        loop = analyse(path, level, A.profile, duration_h, 3000 + n, None) if path else None
        ok = (loop is not None and time_ok(loop.time_s, duration_h) and overlap_ok(loop)
              and loop.u_turns <= MAX_UTURNS and dirt_ok(loop))
        if ok:
            found.append(loop)
        if detail is not None:
            detail.append(f"  croisement {A.score:.1f} x {B.score:.1f} [durée prévue {est:.2f}] -> "
                          + ("pas d'itinéraire" if loop is None else
                             f"note {loop.score:.1f}, {loop.distance_m / 1000:.1f} km, {loop.time_s / 60:.0f} min")
                          + (" : valide" if ok else " : rejeté"))
    log(f"    croisements : {len(found)} valide(s) sur {min(len(plans), CROSS_MAX_TRIALS)} essai(s)"
        + (f" ; boucles de départs voisins adoptables : {len(adopted)}" if adopted else ""))
    if cross_segments():                                     # tronçon par tronçon (Florent, 14/10/2026)
        found += segment_crossover(gh, level, duration_h, parents, log, detail)
    return found + adopted


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
        if plus_relief_rules():                              # « plus de relief » du Modéré (PLUS_RELIEF_RULES)
            used = [l for _, l in chosen]
            rest = [l for l in pool[1:] if plus_relief_score(l) >= 0.8 * best.score and plus_relief_ok(l)
                    and all(similarity(l, u) < 0.6 for u in used) and not any(same_route(l, a) for a in avoid)
                    and secondary_ok(l, best)]
        if rest:
            hilly = max(rest, key=lambda l: l.dplus_per_km)
            if plus_relief_rules():                          # la mieux notée des nettement plus vallonnées (Florent, 15/10)
                hillier = [l for l in rest if l.dplus_per_km >= best.dplus_per_km + 3.0]
                if hillier:
                    hilly = max(hillier, key=plus_relief_score)
            if hilly.dplus_per_km >= best.dplus_per_km + 3.0:
                chosen.append(("plus_de_relief", hilly))
    if len(chosen) == 1 and len(pool) > 1:      # pas de contraste de relief : on propose une variante
        for l in pool[1:]:                      # note d'au moins 70 % de la boucle recommandée (comme l'appli, 30/09/2026)
            if (l.score >= VARIANT_MIN_SCORE_RATIO * best.score and similarity(l, best) < 0.6
                    and not any(same_route(l, a) for a in avoid) and secondary_ok(l, best)):
                chosen.append(("variante", l))
                break
    return chosen


def light_mean(l) -> float | None:
    """Position moyenne des feux franchis (0 : au départ, 1 : à l'arrivée) ; None sans feux connus."""
    pos = getattr(l, "signal_m", None)
    if not pos:
        return None
    return sum(pos) / len(pos) / max(l.distance_m, 1.0)


def lights_late_swap(gh, level: str, duration_h: float, picks: list, log) -> list:
    """Feux en fin de boucle (LIGHTS_LATE) : sens inverse des boucles retenues dont les feux sont surtout au début."""
    out = []
    for n, (lab, l) in enumerate(picks):
        m = light_mean(l)
        if m is None or len(l.signal_m) < LIGHTS_LATE_MIN or m >= LIGHTS_LATE_MAX_MEAN or getattr(l, "same_as", None):
            out.append((lab, l))
            continue
        way = route_waypoints(l.coords[::-1])
        way[0] = way[-1] = [l.coords[0][0], l.coords[0][1]]
        path = gh.via(way, l.profile, pass_through=True)
        r = analyse(path, level, l.profile, duration_h, 4000 + n, None) if path else None
        ok = (r is not None and time_ok(r.time_s, duration_h) and overlap_ok(r) and r.u_turns <= MAX_UTURNS
              and dirt_ok(r) and relief_ok(r) and (light_mean(r) or 0.0) > m and r.score >= l.score - LIGHTS_LATE_TOL)
        log(f"    feux en fin de boucle ({lab}) : feux à {m:.0%} en moyenne, "
            + ("pas d'itinéraire inverse" if r is None else
               f"sens inverse à {(light_mean(r) or 0.0):.0%}, note {l.score:.1f} -> {r.score:.1f}")
            + (" : sens inverse gardé" if ok else " : sens gardé"))
        if ok:
            r.lights_late = True
            for attr in ("relief_fallback", "unpaved_fallback"):
                if getattr(l, attr, False):
                    setattr(r, attr, True)
        out.append((lab, r if ok else l))
    return out


def choose_options(gh, st: dict, level: str, duration_h: float, pool: list, prior: list, log, bounds=None) -> list:
    """Options d'un niveau (O-18) : pick_options sans doublon d'allure. Si une option secondaire n'a AUCUNE boucle
    différente de l'allure inférieure, on garde quand même la boucle qu'elle aurait eue, marquée « même parcours qu'à
    l'allure inférieure » (same_as : allure et km en plus), plutôt que de retirer l'option (choix de Florent, 26/09/2026 :
    une prolongation dans la durée ne gagne que 3 à 5 km, c'est la même boucle). gh, st : inutilisés (signature stable)."""
    if clean_picks() and pool:                               # boucles choisies sans détours ni éperons (CLEAN_PICKS)
        pool, tried = list(pool), set()
        for _ in range(CLEAN_PICKS_ROUNDS):
            dirty = [l for _, l in pick_options(pool, prior)
                     if (defect_windows(l) or getattr(l, "aimless_windows", None)) and id(l) not in tried]
            if not dirty:
                break
            for l in dirty:
                tried.add(id(l))
                t = clean_loop(gh, l, level, duration_h, bounds)
                log(f"    nettoyage : {len(defect_windows(l))} défaut(s), "
                    f"{len(getattr(l, 'aimless_windows', None) or [])} aller(s)-retour(s) sans but, note {l.score:.1f} -> "
                    + ("pas de version propre valide" if t is None else f"{t.score:.1f} ({len(defect_windows(t))} restant)"))
                if t is not None:
                    pool = [t if x is l else x for x in pool]
    picks = pick_options(pool, prior)
    if lights_late() and picks:                              # feux en fin de boucle (LIGHTS_LATE)
        picks = lights_late_swap(gh, level, duration_h, picks, log)
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
        **({"relief_fallback": True} if getattr(l, "relief_fallback", False) else {}),   # v17 : aucune boucle peu vallonnée
        **({"targeted": True} if 900 <= l.seed < 1000 else {}),   # tirage ciblé (lieu attrayant), pour les diagnostics
        **({"retouched": True} if 1000 <= l.seed < 2000 else {}), # retouche d'une meilleure boucle (RETOUCH)
        **({"crossed": True} if 3000 <= l.seed < 4000 else {}),   # croisement de deux boucles (CROSSOVER, v17)
        **({"kept": True} if l.seed >= 2000 else {}),             # boucle de la version précédente (KEEP_PREVIOUS)
        **({"spurs": len(l.spurs)} if l.spurs else {}),
        **({"backtracks": l.backtracks} if getattr(l, "backtracks", None) else {}),   # allers-retours jugés utiles (v17)
        **({"long_backtrack": {"km": round(long_backtrack(l)[1]["m"] / 2000.0, 1), "why": long_backtrack(l)[1]["why"]}}
           if backtrack_long() and long_backtrack(l) is not None else {}),       # annoncé dans l'app (BACKTRACK_LONG)
        **({"shortened": True} if getattr(l, "shortened", False) else {}),   # retouche raccourcie (RETOUCH_SHORTEN, v17)           # éperons injustifiés restants (SPUR_FIX)
        **({"remarkable": l.remarkable} if l.remarkable else {}),  # lieux remarquables traversés (appli, GPX)
        **({"views_passed": l.views_passed} if l.views_passed else {}),   # belvédères devant lesquels on passe
        "terrain": {"max_grade_pct": l.terrain.get("max_grade_pct"), "slope_bands": l.terrain.get("bands"),
                    "n_climbs": l.terrain.get("n_climbs"), "climbs": l.terrain.get("climbs"),
                    "avg_climb_grade_pct": l.terrain.get("avg_climb_grade_pct")},
        "u_turns": l.u_turns,
        **({"steps_m": round(l.steps_m)} if getattr(l, "steps_m", 0) >= 5 else {}),   # escaliers (revue de cohérence)
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


GENERATOR_VERSION = "19"  # 19 : modèle de temps recalé sur les sorties de Florent (+30 % en montée, descente
#                           28 km/h en Modéré, densité de carrefours, 15 s par feu) ; repli corrigé ; croisement par
#                           tronçon ; feux en fin de boucle ; voies agréables ; bord d'eau à 80 m en ville ; front
#                           de mer ; boucles publiées retrouvées (500 m) et reclassées ; voies rapides ; « plus de
#                           relief » du Modéré ; grands axes contournés ; fatigue ; départs voisins greffés et
#                           tronçon par tronçon à plusieurs boucles ; tranches de durée sans trou ; longs allers-
#                           retours annoncés ; repli et relief ; escaliers ; boucles accrochées (docs/v19_proposition.md).
#                      18 : détours en pâté de maisons restants comptés dans la note (diagnostic : détours −25 à
#                           −30 % sur les trois allures, aucune durée perdue) ; v17 interrompue au premier run (10/10/2026).
#                      17 : relief par allure (Tranquille 5-10 m/km et repli annoncé, Modéré pénalisé au-dessus de
#                           15 m/km), terre plafonnée à 1 km, marge des durées, détours en pâté de maisons, répétition par
#                           couloir, retours sur ses pas utiles ou inutiles, croisement des meilleures boucles (durées
#                           voisines comprises) ; départ du Tibidabo retiré, Susqueda, Collserola et Montnegre (10/10/2026).
#                      16 : feux 0,30 et zones industrielles x2 ; options secondaires jamais nettement pires ; éperons
#                           coupés ou pénalisés ; boucles de la version précédente reprises ; allers-retours vers un endroit
#                           agréable et tour des lieux ; plages de durée sans chevauchement ; terre plafonnée à 4 km, terre raide
#                           x2 ; allers-retours au bord de l'eau seulement près de l'eau (05-06/10/2026). 15 : lieux remarquables les plus célèbres visés (aller-retour au lieu compris) et points
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
            # 16/10/2026 : durée totale de la sortie depuis un départ voisin ou une gare (bloc « Autres façons de rouler »)
            "min": o["time_est_min"], "km": o["distance_km"], "dplus": round(o["ascend_m"]),
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
BUDGET_GRACE_S = 1200.0         # au-delà de BUDGET_END + 20 min, un départ en cours s'interrompt entre deux durées
CARRY = None                    # {"src", "by_key"} si --carry-over-from est fourni (départs publiés, toutes versions)


def V19_PARAMS() -> list:  # noqa: N802 : liste de constantes, comme les autres entrées de l'empreinte
    """Règles et réglages de la v19 ; le premier élément liste les interrupteurs."""
    return [[TIME_V19, FALLBACK_BEST, LIGHTS_LATE, SCENIC_RETOUCH, CROSS_SEGMENTS, SEAFRONT_BONUS, MOTORWAY_NUISANCE,
             WATER_CITY_NEAR, PREVIOUS_WAYPOINT_M is not None, PLUS_RELIEF_RULES, PREVIOUS_ANY_DURATION, FATIGUE_V19,
             CONTIGUOUS_BINS, FOOT_BACKTRACK, SPARE_RELIEF, BACKTRACK_MAX_SHARE is not None, SPATIAL_GRAFT,
             SEG_MULTI, BACKTRACK_LONG, CROSS_SPATIAL, APPENDIX_RULE, TURN_RULES, FALSE_START, CORRIDOR_SPURS, AXES_OVER],
            [PREVIOUS_WAYPOINT_M],
            # 17/10/2026 : ajoutées en cours de calcul v19 (demi-tours, faux départs, couloirs, grands axes) : calcul relancé
            [TURN_RULES, TURN_EXTEND_M, TURN_EXTEND_TOP, TURN_CLIMB_M, TURN_CLIMB_PEN, TURN_CLIMB_MAX, TURN_AIMLESS_PEN,
             TURN_AIMLESS_MAX],
            [FALSE_START, FALSE_START_SHARE, FALSE_START_AWAY_M, FALSE_START_NEAR_M],
            [CORRIDOR_SPURS, CORRIDOR_NEAR_M, CORRIDOR_MIN_M],
            [AXES_OVER, AXES_OVER_SHARE, AXES_OVER_PER, AXES_OVER_MAX],
            [FOOT_BACKTRACK, FOOT_BACKTRACK_MAX_M],
            [SPARE_RELIEF, RELIEF_HARD],
            [APPENDIX_RULE, APPENDIX_PENALTY, APPENDIX_TRIM_TOP, APPENDIX_MIN_M, APPENDIX_MAX_M, APPENDIX_MAX_SHARE, APPENDIX_STEM_M, APPENDIX_NEAR_M,
             APPENDIX_GREEN, APPENDIX_CLIMB_M],
            [BACKTRACK_MAX_SHARE],
            [SPATIAL_GRAFT, GRAFT_TIME_TOL],
            [CROSS_SPATIAL, CROSS_SPATIAL_KM, CROSS_SPATIAL_STARTS, CROSS_SPATIAL_LOOPS],
            [SEG_MULTI, SEG_MULTI_BASES],
            [BACKTRACK_LONG, BACKTRACK_LONG_SHARE, BACKTRACK_LONG_PEN, BACKTRACK_LONG_PEN_PER, BACKTRACK_LONG_MAX],
            [FATIGUE_V19, FATIGUE_FROM_H, FATIGUE_PER_H, FATIGUE_CLIMB_M],
            [CLIMB_POWER_BONUS, CLIMB_POWER_FULL_GRADE, DESCENT_CAP_KMH, DENSITY_SLOWDOWN, DENSITY_FULL, SIGNAL_DELAY_V19_S,
             JunctionDensity.CELL_M, JunctionDensity.R],
            [PLUS_RELIEF_RULES, PLUS_RELIEF_PEN_ABOVE, PLUS_RELIEF_BANNED_CATS, PLUS_RELIEF_MAX_GRADE],
            [FALLBACK_RELIEF_TOL, FALLBACK_DIRT_TOL_KM],
            [LIGHTS_LATE_MIN, LIGHTS_LATE_MAX_MEAN, LIGHTS_LATE_TOL],
            [SCENIC_SEA_M, SCENIC_RIVER_M, SCENIC_NEAR_M, SCENIC_ON_M, SCENIC_RUN_M, SCENIC_MIN_WAY_M, SCENIC_TOP,
             SCENIC_MAX_RUNS, SCENIC_DENSE, SCENIC_PICK_M, SCENIC_WINDOWS, SCENIC_WINDOW_M],
            [CROSS_SEG_MAX, CROSS_SEG_MARGIN, CROSS_SEG_TIME],
            [AXIS_CHUNKS, AXIS_CHUNK_M, AXIS_MAX_TRIES],
            [SEAFRONT_DEG, SEAFRONT_PER_KM, SEAFRONT_MAX, MOTORWAY_DEG, MOTORWAY_PER_KM, MOTORWAY_MAX, MOTORWAY_RUN_M,
             MOTORWAY_TRUNK,
             WATER_CITY_DEG]]


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
              "v15": [REMARKABLE_FAME, REMARKABLE_POINTS, OUTBACK_OK, OUTBACK_DRAWS, OUTBACK_MAX_OVERLAP, OUTBACK_MIN_CYCLE],
              "v16": [INDUSTRIAL_PENALTY, INDUSTRIAL_MAX_PENALTY, SECONDARY_GUARD, SECONDARY_MAX_LIGHTS_RATIO,
                      SECONDARY_MAX_LIGHTS_GAP, SECONDARY_MAX_INDUSTRIAL, SPUR_FIX, SPUR_CORRIDOR_M, SPUR_MIN_M, SPUR_END_M,
                      SPUR_JUSTIFIED_DPLUS_M, SPUR_PLEASANT_SHARE, SPUR_PENALTY, SPUR_PENALTY_PER_100M, SPUR_MAX_PENALTY,
                      SPUR_TRIM_TOP, KEEP_PREVIOUS, ROUTE_WAYPOINT_M, AR_DRAWS, LIEU_TOUR, OUTBACK_NEAR_KM, DURATION_BINS,
                      DURATION_SET, DIRT_MAX_KM, STEEP_DIRT_GRADE, STEEP_DIRT_PENALTY],
              "v17": [RELIEF_LIMITS, RELIEF_OVER_PENALTY, RELIEF_OVER_MAX, DIRT_RULES, DURATION_BIN_MARGIN, ORDER_GAP_S,
                      DETOUR_FIX, DETOUR_MIN_M, DETOUR_MAX_M, DETOUR_GAP_M, DETOUR_RATIO, DETOUR_CLIMB_M, DETOUR_END_M,
                      DETOUR_SCORE_SLACK, DETOUR_TRIM_TOP, CORRIDOR_OVERLAP, CORRIDOR_R_M, CORRIDOR_GAP_M, CORRIDOR_DZ_M,
                      CORRIDOR_STEP_M, BACKTRACK_RULES, BACKTRACK_CLIMB_M, BACKTRACK_SHARE, BACKTRACK_RELIEF_LEVELS,
                      BACKTRACK_PLEASANT_MIN_M, BACKTRACK_DIRT_MAX_M, BACKTRACK_PISTE_GREEN, BACKTRACK_REPEAT_WEIGHT,
                      CROSSOVER, CROSS_TOP, CROSS_NEAR_M, CROSS_MAX_TRIALS, CROSS_NEIGHBOURS, CROSS_NEIGHBOUR_LOOPS,
                      RETOUCH_ROUND_ROBIN, RETOUCH_SHORTEN],
              "v18": [DETOUR_SCORE, DETOUR_PENALTY, DETOUR_PENALTY_PER_100M, DETOUR_MAX_PENALTY],
              # v19 (préparée le 14/10/2026, docs/v19_proposition.md) : comptée dès qu'une de ses règles est allumée,
              # pour que l'empreinte ne change pas tant qu'elles sont toutes éteintes (calcul v18 en cours)
              **({"v19": V19_PARAMS()} if any(V19_PARAMS()[0]) else {})}
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
            "by_key": {e["key"]: e for e in idx.get("starts", []) if e.get("key")},
            "no_option": set(idx.get("no_option") or [])}


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
    # 16/10/2026 : la chaîne v18 se relançait sans fin : chaque run passait ~5 h sur 22 départs de montagne sans aucune
    # boucle valide (non publiés, donc jamais réutilisés), puis manquait de budget pour 13 autres. À paramètres égaux
    # (même empreinte), le résultat reste vide : le départ est ignoré tout de suite.
    if REUSE is not None and key0 in REUSE.get("no_option", ()):
        log(f"- {st['name']} : aucune option valide au calcul précédent (mêmes paramètres), départ ignoré")
        return None, buf, 0.0
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
        if BUDGET_END is not None and time.time() > BUDGET_END + BUDGET_GRACE_S:
            # 20/10/2026 : un départ commencé juste avant la fin du budget a duré 49 min de plus ; le run a dépassé la
            # limite de 350 min, coupé avant la publication (chaîne v19 arrêtée). Il s'arrête désormais entre deux durées.
            kept = carry_over(st, sid, out, log)
            log(f"- {st['name']} : budget de temps dépassé de plus de {BUDGET_GRACE_S / 60:g} min, départ interrompu"
                + (", version publiée gardée (à recalculer)" if kept is not None else ""))
            return kept, buf, time.time() - t0
        t_dur = time.time()
        prior: list = []                                  # options des allures inférieures (O-18 A)
        for level in sorted(levels, key=list(LEVELS).index):
            pool = ordered_pool(gh, st, level, duration, candidates, log, options)
            picks = choose_options(gh, st, level, duration, pool, prior, log,
                                   order_bounds(options, level, duration) if bin_margin() else None)
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

    global SIGNALS, STOPS, LANDSCAPE, POIS, DENSITY, SCENIC_WAYS, MOTORWAYS, VILLAGES
    places_path = Path(args.places)
    pbf_path = Path(args.pbf) if args.pbf else places_path.parent / "region.osm.pbf"
    SIGNALS = load_signals(pbf_path, places_path.parent)
    if TIME_V19:                                             # densité de carrefours (modèle de temps v19)
        DENSITY = load_density(pbf_path, places_path.parent)
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
    if MOTORWAY_NUISANCE:                                    # voies rapides à l'air libre
        MOTORWAYS = load_motorways(pbf_path, places_path.parent)
    if GOAL_RETURN:                                          # villages visés (v20)
        VILLAGES = load_points(pbf_path, places_path.parent, ["n/place=village"], "villages")
    if SCENIC_RETOUCH:                                       # voies agréables (retouche)
        SCENIC_WAYS = load_scenic_ways(pbf_path, places_path.parent, LANDSCAPE)
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
    global REUSE, PARAMS_HASH, BUDGET_END, CARRY, NEIGHBOUR_SRC
    PARAMS_HASH = params_hash()
    BUDGET_END = time.time() + 60.0 * args.time_budget_min if args.time_budget_min else None
    REUSE = load_reuse(args.reuse_from, args.reuse_max_age_days) if args.reuse_from else None
    CARRY = load_carry(args.carry_over_from) if args.carry_over_from else None
    # départs voisins (CROSS_SPATIAL) : boucles du site publié (16/10/2026 : la source n'était fixée que par le diagnostic,
    # la règle serait restée inactive sans le dire dans le calcul réel)
    NEIGHBOUR_SRC = NEIGHBOUR_SRC or args.reuse_from or args.carry_over_from
    if CROSS_SPATIAL and not NEIGHBOUR_SRC:
        print("ATTENTION : CROSS_SPATIAL allumé sans site publié (--reuse-from ou --carry-over-from) : départs voisins inactifs")
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
    no_option = sorted(st_key(st) for st, r in zip(starts, results)            # mémorisés pour le calcul suivant
                       if not r[0] and any("aucune option valide" in line for line in r[1]))

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
        "no_option": no_option,                          # départs sans aucune boucle valide (clés), ignorés au calcul suivant
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
