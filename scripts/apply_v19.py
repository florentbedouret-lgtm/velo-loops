# -*- coding: utf-8 -*-
"""Activation de la v19 (préparée le 15/10/2026, à appliquer après la fin de la v18 et l'accord de Florent) :
14 règles allumées (fatigue comprise), version 19, tests, textes de l'app (docs/v19_proposition.md).
Usage, depuis la racine du dépôt : python scripts/apply_v19.py . ; ne pas lancer deux fois (vérifié)."""
import io
import os
import sys

ROOT = sys.argv[1] if len(sys.argv) > 1 else '.'
if 'GENERATOR_VERSION = "19"' in io.open(os.path.join(ROOT, 'scripts/generate_loops.py'), encoding='utf-8').read():
    sys.exit('déjà en v19 : rien à faire')


def patch(rel, pairs):
    p = os.path.join(ROOT, rel)
    s = io.open(p, encoding='utf-8').read()
    for a, b in pairs:
        n = s.count(a)
        assert n >= 1, (rel, a[:70])
        s = s.replace(a, b)
    io.open(p, 'w', encoding='utf-8', newline='').write(s)


FLAGS = ["TIME_V19", "FALLBACK_BEST", "CROSS_SEGMENTS", "LIGHTS_LATE", "SCENIC_RETOUCH", "SCENIC_DENSE", "WATER_CITY_NEAR",
         "SEAFRONT_BONUS", "PREVIOUS_ANY_DURATION", "MOTORWAY_NUISANCE", "PLUS_RELIEF_RULES", "AXIS_BYPASS",
         "FATIGUE_V19", "CROSS_SPATIAL", "SPATIAL_GRAFT", "SEG_MULTI",
         "CONTIGUOUS_BINS",                         # tranches de durée sans trou (16/10/2026)
         "BACKTRACK_LONG", "SPARE_RELIEF",
         "FOOT_BACKTRACK"]                          # allers-retours par des escaliers jamais utiles (16/10/2026)          # longs allers-retours annoncés ; repli et relief (16/10/2026)   # départs voisins greffés (16/10/2026)
gen = [("\n%s = False" % f, "\n%s = True                    # v19" % f) for f in FLAGS]
gen += [("\nPREVIOUS_WAYPOINT_M = None\n", "\nPREVIOUS_WAYPOINT_M = 500.0                # v19\n"),
        ('GENERATOR_VERSION = "18"  # 18 :',
         'GENERATOR_VERSION = "19"  # 19 : modèle de temps recalé sur les sorties de Florent (+30 % en montée, descente\n'
         '#                           28 km/h en Modéré, densité de carrefours, 15 s par feu) ; repli corrigé ; croisement par\n'
         '#                           tronçon ; feux en fin de boucle ; voies agréables ; bord d\'eau à 80 m en ville ; front\n'
         '#                           de mer ; boucles publiées retrouvées (500 m) et reclassées ; voies rapides ; « plus de\n'
         '#                           relief » du Modéré ; grands axes contournés (docs/v19_proposition.md).\n'
         '#                      18 :')]
patch('scripts/generate_loops.py', gen)

ttext = io.open(os.path.join(ROOT, 'tests/test_rules.py'), encoding='utf-8').read()
tests = [("assert g.%s is False" % f, "assert g.%s is True " % f) for f in FLAGS if "assert g.%s is False" % f in ttext]
tests.append(("assert g.PREVIOUS_WAYPOINT_M is None ", "assert g.PREVIOUS_WAYPOINT_M == 500.0"))
tests.append(("    g.TIME_V19 = True\n", "    g.TIME_V19 = not old\n"))     # l'empreinte change quand on bascule la règle
tests.append(('    assert g.relief_filter([a, b, c], "facile", lambda *_: None) == [b]          # v17 : la moins vallonnée',
              '    g._TL.fallback_best = False\n'
              '    try:\n'
              '        assert g.relief_filter([a, b, c], "facile", lambda *_: None) == [b]      # v17 : la moins vallonnée\n'
              '    finally:\n'
              '        del g._TL.fallback_best'))
patch('tests/test_rules.py', tests)

patch('i18n.js', [
("notes: ['Durées estimées en mouvement, sans pauses. Dénivelé : estimation.', 'Moving times, excluding breaks. Climbing is an estimate.',\n"
 "    'Tiempos en movimiento, sin pausas. Desnivel: estimación.', 'Temps en moviment, sense pauses. Desnivell: estimació.'],",
 "notes: ['Durées estimées sans les pauses, arrêts aux feux compris. Dénivelé : estimation.',\n"
 "    'Estimated times without breaks, stops at traffic lights included. Climbing is an estimate.',\n"
 "    'Tiempos estimados sin pausas, paradas en semáforos incluidas. Desnivel: estimación.',\n"
 "    'Temps estimats sense pauses, aturades als semàfors incloses. Desnivell: estimació.'],"),
("La durée est estimée pour ton allure (tranquille, modéré, sportif), relief, ville et feux compris (≈ 10 s par feu), sans les pauses.",
 "La durée est estimée pour ton allure (tranquille, modéré, sportif) : un peu plus d'effort dans les montées, des descentes prudentes, un ralentissement dans les quartiers denses, et environ 15 s par feu (arrêt, freinage et relance). Les pauses ne sont pas comptées. Ce calcul a été recalé sur des sorties réelles."),
("The time is estimated for your pace (easy, moderate, sporty), including hills, town and traffic lights (≈ 10 s per light), without breaks.",
 "The time is estimated for your pace (easy, moderate, sporty): a little more effort on climbs, careful descents, slower riding in dense neighbourhoods, and about 15 s per traffic light (stopping, braking and getting going again). Breaks are not counted. The estimate was calibrated on real rides."),
("La duración se estima para tu ritmo (tranquilo, moderado, deportivo), con relieve, ciudad y semáforos (≈ 10 s por semáforo), sin pausas.",
 "La duración se estima para tu ritmo (tranquilo, moderado, deportivo): algo más de esfuerzo en las subidas, bajadas prudentes, más lento en los barrios densos y unos 15 s por semáforo (parada, frenada y arranque). Las pausas no se cuentan. El cálculo se ha ajustado con salidas reales."),
("La durada es calcula per al teu ritme (tranquil, moderat, esportiu), amb relleu, ciutat i semàfors (≈ 10 s per semàfor), sense pauses.",
 "La durada es calcula per al teu ritme (tranquil, moderat, esportiu): una mica més d'esforç a les pujades, baixades prudents, més lent als barris densos i uns 15 s per semàfor (aturada, frenada i arrencada). Les pauses no es compten. El càlcul s'ha ajustat amb sortides reals."),
])

PAGES = {
    'comment-ca-marche.html': ("La durée est calculée pour ton allure, relief, ville et feux compris (environ 10 secondes par feu), sans les pauses.",
                               "La durée est calculée pour ton allure : un peu plus d'effort dans les montées, des descentes prudentes, un ralentissement dans les quartiers denses, et environ 15 secondes par feu (arrêt, freinage et relance). Les pauses ne sont pas comptées. Ce calcul a été recalé sur des sorties réelles."),
    'how-it-works.html': ("The time is calculated for your pace, including hills, town and traffic lights (about 10 seconds per light), without breaks.",
                          "The time is calculated for your pace: a little more effort on climbs, careful descents, slower riding in dense neighbourhoods, and about 15 seconds per traffic light (stopping, braking and getting going again). Breaks are not counted. The estimate was calibrated on real rides."),
    'como-funciona.html': ("La duración se calcula para tu ritmo, con relieve, ciudad y semáforos (unos 10 segundos por semáforo), sin pausas.",
                           "La duración se calcula para tu ritmo: algo más de esfuerzo en las subidas, bajadas prudentes, más lento en los barrios densos y unos 15 segundos por semáforo (parada, frenada y arranque). Las pausas no se cuentan. El cálculo se ha ajustado con salidas reales."),
    'com-funciona.html': ("La durada es calcula per al teu ritme, amb relleu, ciutat i semàfors (uns 10 segons per semàfor), sense pauses.",
                          "La durada es calcula per al teu ritme: una mica més d'esforç a les pujades, baixades prudents, més lent als barris densos i uns 15 segons per semàfor (aturada, frenada i arrencada). Les pauses no es compten. El càlcul s'ha ajustat amb sortides reals."),
}
for f, (a, b) in PAGES.items():
    pairs = [(a, b)]
    if "'" in a:
        pairs.append((a.replace("'", "&#x27;"), b.replace("'", "&#x27;")))
    p = os.path.join(ROOT, f)
    s = io.open(p, encoding='utf-8').read()
    n = 0
    for x, y in pairs:
        n += s.count(x)
        s = s.replace(x, y)
    assert n >= 2, (f, n)
    io.open(p, 'w', encoding='utf-8', newline='').write(s)
print('v19 appliquée')
