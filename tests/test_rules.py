# -*- coding: utf-8 -*-
"""Tests des règles qui décident quelles boucles sont gardées (audit de code du 08/10/2026, O-43).

Lancés sur GitHub à chaque envoi qui touche scripts/ ou tests/ (workflow tests.yml) ; en local : python -m pytest tests
(Python 3.10 ou plus). Aucun appel réseau, aucun GraphHopper : seulement les fonctions de calcul.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import generate_loops as g  # noqa: E402
import build_start_pages as sp  # noqa: E402


# ----------------------------------------------------------------------------- réglages de production
def test_reglages_v17():
    """v17 + v18 : règles retenues allumées (docs/v17_proposition.md), essais non retenus éteints, version 18."""
    assert g.GENERATOR_VERSION == "19"
    assert g.DURATION_BIN_MARGIN == 0.05
    assert g.RELIEF_LIMITS is g.RELIEF_LIMITS_V17_FLAT
    assert g.DIRT_RULES is g.DIRT_RULES_V17
    assert g.DETOUR_FIX and g.CORRIDOR_OVERLAP and g.BACKTRACK_RULES and g.CROSSOVER and g.CROSS_NEIGHBOURS
    assert g.TARGETED_GATE is None
    assert g.RETOUCH_ROUND_ROBIN is False
    assert g.RETOUCH_SHORTEN is False
    assert g.DETOUR_SCORE is True                      # v18
    assert g.CROSS_SPATIAL is True                     # départs voisins, en essai
    assert g.CROSS_SEGMENTS is True                    # tronçon par tronçon, en essai
    assert g.SCENIC_AREAS is False                     # zones agréables, en essai
    assert g.TIME_V19 is True                          # modèle de temps v19, en essai
    assert g.LIGHTS_LATE is True                       # feux en fin de boucle, en essai
    assert g.SCENIC_RETOUCH is True                    # voies agréables, en essai
    assert g.WATER_CITY_NEAR is True                   # bord d'eau à 80 m en ville, en essai
    assert g.FALLBACK_BEST is True                     # repli « mieux notée parmi les quasi-plus-plates », en essai
    assert g.SEAFRONT_BONUS is True                    # front de mer, en essai
    assert g.SEG_MULTI is True                         # tronçon par tronçon à plusieurs boucles, en essai
    assert g.SPATIAL_GRAFT is True                     # départs voisins greffés, en essai
    assert g.BACKTRACK_LONG is True                    # longs allers-retours annoncés, en essai
    assert g.BACKTRACK_MAX_SHARE is None               # plafond des allers-retours utiles, en essai
    assert g.BACKTRACK_DEST is False                   # allers-retours vers une destination, en essai
    assert g.DETOUR_DEST is False                      # détours excusés seulement par une destination, en essai
    assert g.DEFECT_STRICT is False                    # défauts presque éliminatoires, en essai
    assert g.FORCED_DETOURS is False                   # lacets et passages obligés, en essai
    assert g.MOTORWAY_V2 is False                      # « nature » le long des voies rapides, en essai
    assert g.SPUR_TURN_WINDOW is False                 # demi-tours par un rond-point, en essai
    assert g.REPEAT_PLAIN is False                     # répétition sur routes ordinaires, en essai
    assert g.CLEAN_PICKS is False                      # nettoyage des boucles publiées, en essai
    assert g.CITY_BACKTRACK is False                   # allers-retours en ville dense, en essai
    assert g.NESTED_BACKTRACK is False                 # allers-retours emboîtés, en essai
    assert g.AXES_OVER is True                         # grands axes au-delà d'un seuil (v19)
    assert g.CORRIDOR_SPURS is True                    # allers-retours dans un couloir (v19)
    assert g.FALSE_START is True                       # faux départs (v19)
    assert g.TURN_RULES is True                        # demi-tours au bon endroit (v19)
    assert g.APPENDIX_RULE is True                     # petites boucles accrochées, en essai
    assert g.SPARE_RELIEF is True                      # repli de dernier recours et relief, en essai
    assert g.FOOT_BACKTRACK is True                    # allers-retours à pied, en essai
    assert g.CONTIGUOUS_BINS is True                   # tranches de durée sans trou, en essai
    assert g.FATIGUE_V19 is True                       # fatigue au fil de la sortie, en essai
    assert g.AXIS_BYPASS is True                       # contourner les grands axes, en essai
    assert g.PREVIOUS_ANY_DURATION is True             # reclassement des boucles publiées, en essai
    assert g.PLUS_RELIEF_RULES is True                 # « plus de relief » du Modéré, en essai
    assert g.PREVIOUS_WAYPOINT_M == 500.0              # boucles publiées à 2 km près (v18), essai à 300 m
    assert g.MOTORWAY_NUISANCE is True                 # voies rapides, en essai


def test_profil_plat_dans_la_configuration():
    """Le relief Tranquille tire avec le profil calm_flat : il doit exister dans la configuration de production."""
    cfg = (Path(__file__).resolve().parents[1] / "config" / "graphhopper.yml").read_text(encoding="utf-8")
    assert "name: calm_flat" in cfg
    assert (Path(__file__).resolve().parents[1] / "config" / "custom_models" / "loop_flat.json").exists()


def test_empreinte_stable():
    """Même code, mêmes réglages : même empreinte (sinon le site croirait tous les départs périmés)."""
    h1 = g.params_hash(str(Path(__file__).resolve().parents[1] / "config"))
    h2 = g.params_hash(str(Path(__file__).resolve().parents[1] / "config"))
    assert h1 == h2 and len(h1) == 12


# ----------------------------------------------------------------------------- durées
@pytest.fixture
def plages(monkeypatch):
    monkeypatch.setattr(g, "DURATION_BINS", True)
    monkeypatch.setattr(g, "DURATION_BIN_MARGIN", None)
    monkeypatch.setattr(g, "TIME_TOLERANCE", 0.15)


def test_plages_strictes(plages):
    h = 3600.0
    assert g.time_ok(5.0 * h, 5.0)
    assert g.time_ok(4.6 * h, 5.0)                 # « 5 h » commence à 4 h 30
    assert not g.time_ok(4.4 * h, 5.0)             # 4 h 24 : hors plage (Sant Celoni, 07/10/2026)
    assert not g.time_ok(5.6 * h, 5.0)             # 5 h 36 : appartient à « 6 h »
    assert g.time_ok(5.6 * h, 6.0)


def test_plages_avec_marge(plages, monkeypatch):
    monkeypatch.setattr(g, "DURATION_BIN_MARGIN", 0.05)
    h = 3600.0
    assert g.time_ok(4.4 * h, 5.0)                 # 4 h 24 : repris avec 5 % de marge
    assert not g.time_ok(4.2 * h, 5.0)             # 4 h 12 : toujours hors plage
    assert not g.time_ok(5.0 * h * 1.2, 5.0)       # jamais au-delà de la tolérance de ±15 %
    assert not g.time_ok(5.6 * h, 5.0)             # marge vers le bas seulement : 5 h 36 reste à « 6 h »


def test_sans_plages(plages, monkeypatch):
    monkeypatch.setattr(g, "DURATION_BINS", False)
    assert g.time_ok(4.4 * 3600, 5.0)


# ----------------------------------------------------------------------------- ordre des durées (marge)
def _opt(level, dur_min, est_min, key, idx=1):
    return {"id": f"x-{dur_min / 60:g}h-{level}-calm-{idx}", "level": level, "duration_target_min": dur_min,
            "time_est_min": est_min, "route_key": key}


def _loop(minutes, coords):
    return SimpleNamespace(time_s=minutes * 60.0, coords=coords)


COORDS_A = [[2.10, 41.40], [2.12, 41.41], [2.14, 41.40], [2.10, 41.40]]
COORDS_B = [[2.10, 41.40], [2.11, 41.43], [2.15, 41.42], [2.10, 41.40]]


def test_ordre_inactif_sans_marge(monkeypatch):
    monkeypatch.setattr(g, "DURATION_BIN_MARGIN", None)
    pool = [_loop(200, COORDS_A)]
    assert g.keep_order(pool, [_opt("facile", 240, 260, "k")], "facile", 5.0) == pool


def test_ordre_des_durees(monkeypatch):
    monkeypatch.setattr(g, "DURATION_BIN_MARGIN", 0.05)
    options = [_opt("facile", 240, 260, "k4"), _opt("facile", 360, 330, "k6")]      # 4 h : 4 h 20 ; 6 h : 5 h 30
    pool = [_loop(262, COORDS_A), _loop(270, COORDS_B), _loop(326, COORDS_B), _loop(329, COORDS_A)]
    kept = [round(l.time_s / 60) for l in g.keep_order(pool, options, "facile", 5.0)]
    assert kept == [270, 326]                      # 3 min d'écart avec la 4 h (263 min) et la 6 h (327 min)


def test_ordre_seulement_sur_les_recommandees(monkeypatch):
    monkeypatch.setattr(g, "DURATION_BIN_MARGIN", 0.05)
    # une option secondaire longue à 4 h (4 h 40) ne bloque plus la durée 5 h (Argelaguer, 07/10/2026)
    options = [_opt("facile", 240, 250, "k4"), _opt("facile", 240, 280, "k4b", idx=2)]
    assert len(g.keep_order([_loop(275, COORDS_A)], options, "facile", 5.0)) == 1


def test_pas_la_meme_boucle_a_deux_durees(monkeypatch):
    monkeypatch.setattr(g, "DURATION_BIN_MARGIN", 0.05)
    key = g.route_key(g.simplify(COORDS_A, 10.0))
    options = [_opt("modere", 300, 290, key)]       # déjà proposée en « 5 h » (Pallejà, 07/10/2026)
    assert g.keep_order([_loop(341, COORDS_A)], options, "modere", 6.0) == []
    assert len(g.keep_order([_loop(341, COORDS_B)], options, "modere", 6.0)) == 1


# ----------------------------------------------------------------------------- terre
def test_terre(monkeypatch):
    monkeypatch.setattr(g, "DIRT_MAX_KM", 4.0)
    assert g.dirt_ok(SimpleNamespace(shares={"unpaved": 0.05}, distance_m=60000))      # 3 km
    assert not g.dirt_ok(SimpleNamespace(shares={"unpaved": 0.08}, distance_m=60000))  # 4,8 km
    assert not g.dirt_ok(SimpleNamespace(shares={"unpaved": 0.20}, distance_m=10000))  # part au-delà de MAX_UNPAVED


# ----------------------------------------------------------------------------- relief par allure (v17)
def _relief(level, dpk, cats=(), grade=8.0):
    return SimpleNamespace(level=level, dplus_per_km=dpk, terrain={"climbs": [{"category": c} for c in cats],
                                                                  "max_grade_pct": grade})


def test_relief_inactif_en_production(monkeypatch):
    monkeypatch.setattr(g, "RELIEF_LIMITS", None)
    assert g.relief_ok(_relief("facile", 25.0, ["HC"], 25.0))


def test_relief_tranquille(monkeypatch):
    monkeypatch.setattr(g, "RELIEF_LIMITS", g.RELIEF_LIMITS_V17)
    assert g.relief_ok(_relief("facile", 9.0, ["3", "4", "nc"]))
    assert not g.relief_ok(_relief("facile", 10.5))                 # au-dessus de 10 m/km
    assert not g.relief_ok(_relief("facile", 8.0, ["2"]))           # montée de catégorie 2
    assert not g.relief_ok(_relief("facile", 8.0, grade=21.0))      # rampe très raide
    assert g.relief_ok(_relief("modere", 17.0, ["1"], 22.0))        # Modéré : pénalité seulement, pas d'interdiction


def test_relief_repli(monkeypatch):
    monkeypatch.setattr(g, "RELIEF_LIMITS", g.RELIEF_LIMITS_V17)
    monkeypatch.setattr(g, "FALLBACK_BEST", False)       # repli v17 : la moins vallonnée (v19 : la mieux notée)
    monkeypatch.setattr(g, "SPARE_RELIEF", False)
    pool = [_relief("facile", 14.0), _relief("facile", 12.0)]
    kept = g.relief_filter(pool, "facile", lambda *_: None)
    assert len(kept) == 1 and kept[0].dplus_per_km == 12.0 and kept[0].relief_fallback
    ok = [_relief("facile", 14.0), _relief("facile", 8.0), _relief("facile", 9.5)]
    assert [l.dplus_per_km for l in g.relief_filter(ok, "facile", lambda *_: None)] == [8.0, 9.5]


# ----------------------------------------------------------------------------- identifiants
def test_slugify():
    assert g.slugify("Sant Martí Sesgueioles") == "sant-marti-sesgueioles"
    assert g.slugify("L'Hospitalet de Llobregat") == "l-hospitalet-de-llobregat"
    assert g.slugify("<script>") == "script"
    assert g.slugify("???") == "x"


# ----------------------------------------------------------------------------- pages par départ
def test_formats_pages():
    assert sp.hm(48) == "48 min"
    assert sp.hm(130) == "2 h 10"
    assert sp.dur_label(0.75) == "45 min"
    assert sp.dur_label(1.5) == "1 h 30"
    assert sp.dur_label(6.0) == "6 h"
    assert sp.dec(1.25, "fr") == "1,2" or sp.dec(1.25, "fr") == "1,3"
    assert sp.dec(1.5, "en") == "1.5"


def test_noms_des_departs():
    gare = {"name": "Gare Montcada-Ripollet", "municipality": "Montcada i Reixac", "kind": "station"}
    assert sp.start_name(gare, "fr") == "Gare Montcada-Ripollet (Montcada i Reixac)"
    assert sp.start_name(gare, "ca", phrase=True) == "l'estació Montcada-Ripollet (Montcada i Reixac)"
    assert sp.start_name({"name": "Gràcia", "municipality": "Barcelona", "kind": "place"}, "fr") == "Gràcia (Barcelona)"
    assert sp.start_name({"name": "Vic", "municipality": "Vic", "kind": "place"}, "fr") == "Vic"
    assert sp.start_name({"name": "Canyet (sud)", "municipality": "Badalona", "kind": "fill"}, "es") == "Canyet (sur) (Badalona)"


def test_page_echappee():
    """Un nom venu d'OpenStreetMap (modifiable par tous) ne peut pas injecter de HTML dans la page."""
    s = {"id": "x", "name": "<img src=x onerror=alert(1)>", "kind": "place", "lat": 41.4, "lon": 2.1}
    o = {"id": "x-2h-facile-calm-1", "level": "facile", "duration_target_min": 120, "distance_km": 30.0, "ascend_m": 200,
         "time_est_min": 118, "exit_city_km": 2.0, "traffic_lights_per_km": 0.4, "shares": {"unpaved": 0.0},
         "scenery": {"landcover": {"forest": 0.3, "water": 0.1}}}
    html_ = sp.page(s, {"options": [o]}, "fr", [])
    assert "<img src=x" not in html_ and "&lt;img" in html_
    assert "</script>" not in html_.split("application/ld+json")[1].split("</script>")[0]   # pas de sortie du JSON-LD


def test_mots_du_relief():
    """Mêmes seuils que l'app et relief_category() : Tranquille vise « peu vallonné », Modéré « vallonné »."""
    w = lambda km, d: sp.relief_word({"distance_km": km, "ascend_m": d}, "fr")  # noqa: E731
    assert w(40, 150) == "plat"                      # 3,8 m/km
    assert w(40, 300) == "peu vallonné"              # 7,5 m/km
    assert w(40, 500) == "vallonné"                  # 12,5 m/km
    assert w(39, 676) == "très vallonné"             # 17,3 m/km : Gràcia 3 h tranquille (v16)
    assert g.relief_category(1000) == "vallonné" and g.relief_category(999) == "peu vallonné"


# ----------------------------------------------------------------------------- liaisons de train (O-42)
def _gtfs(files):
    import io as _io
    import zipfile as _zf
    buf = _io.BytesIO()
    with _zf.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    buf.seek(0)
    return _zf.ZipFile(buf)


def test_liaisons_directes(monkeypatch):
    import datetime as _dt
    import build_train_links as tl
    monkeypatch.setattr(tl, "next_saturday", lambda z: _dt.date(2026, 10, 10))
    # A (ville) -> B (départ Oyan « gare ») en direct, 2 trains le samedi matin ; C n'est desservie qu'après midi.
    # Lignes complétées par des espaces, comme le fichier Renfe.
    z = _gtfs({
        "stops.txt": "stop_id,stop_name,stop_lat,stop_lon   \nA,Ciutat,41.40,2.15   \nB,Poble,41.50,2.00   \nC,Lluny,41.60,1.90   \n",
        "calendar.txt": "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
                        "S,0,0,0,0,0,1,0,20261001,20261031\nW,1,1,1,1,1,0,0,20261001,20261031\n",
        "trips.txt": "route_id,service_id,trip_id\nR,S,t1\nR,S,t2\nR,W,t3\nR,S,t4\n",
        "stop_times.txt": "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                          "t1,08:00:00,08:00:00,A,1\nt1,08:25:00,08:25:00,B,2\n"
                          "t2,09:00:00,09:00:00,A,1\nt2,09:30:00,09:30:00,B,2\n"
                          "t3,08:00:00,08:00:00,A,1\nt3,08:10:00,08:10:00,B,2\n"
                          "t4,13:00:00,13:00:00,A,1\nt4,13:40:00,13:40:00,C,2\n"})
    starts = [{"id": "gare-poble", "lon": 2.0005, "lat": 41.5002}, {"id": "gare-lluny", "lon": 1.90, "lat": 41.60}]
    res, _ = tl.feed_links("x", z, [1.0, 41.0, 3.0, 42.0], starts, lambda *_: None)
    assert res == []      # A -> B : 2 trains en 5 h (< 0,5 par heure) ; t3 roule en semaine ; C seulement l'après-midi


def test_liaisons_frequence(monkeypatch):
    import datetime as _dt
    import build_train_links as tl
    monkeypatch.setattr(tl, "next_saturday", lambda z: _dt.date(2026, 10, 10))
    st = "".join(f"t{i},{7 + i // 2:02d}:{(i % 2) * 30:02d}:00,{7 + i // 2:02d}:{(i % 2) * 30:02d}:00,A,1\n"
                 f"t{i},{7 + i // 2:02d}:{(i % 2) * 30 + 20:02d}:00,{7 + i // 2:02d}:{(i % 2) * 30 + 20:02d}:00,B,2\n"
                 for i in range(10))
    z = _gtfs({"stops.txt": "stop_id,stop_name,stop_lat,stop_lon\nA,Ciutat,41.40,2.15\nB,Poble,41.50,2.00\n",
               "calendar_dates.txt": "service_id,date,exception_type\nS,20261010,1\n",
               "trips.txt": "route_id,service_id,trip_id\n" + "".join(f"R,S,t{i}\n" for i in range(10)),
               "stop_times.txt": "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n" + st})
    res, _ = tl.feed_links("x", z, [1.0, 41.0, 3.0, 42.0], [{"id": "gare-poble", "lon": 2.0, "lat": 41.5}], lambda *_: None)
    assert res[0]["to"] == [["gare-poble", 20, 2.0]]               # 10 trains de 7 h à 11 h 30 : 2 par heure, 20 min


def test_repli_relief_publie(monkeypatch):
    """Le repli de relief part dans les données publiées (l'app l'affiche) ; absent sinon."""
    monkeypatch.setattr(g, "RELIEF_LIMITS", g.RELIEF_LIMITS_V17)
    monkeypatch.setattr(g, "FALLBACK_BEST", False)
    monkeypatch.setattr(g, "SPARE_RELIEF", False)
    flat = g.relief_filter([_relief("facile", 13.0)], "facile", lambda *_: None)[0]
    assert getattr(flat, "relief_fallback", False)
    src = Path(g.__file__).read_text(encoding="utf-8")
    assert '"relief_fallback": True} if getattr(l, "relief_fallback", False)' in src


# ----------------------------------------------------------------------------- terre v17
def _dirt(km, dist_km=40.0):
    return SimpleNamespace(shares={"unpaved": km / dist_km}, distance_m=dist_km * 1000.0, unpaved_fallback=False)


def test_terre_v17_plafond_et_repli(monkeypatch):
    monkeypatch.setattr(g, "DIRT_RULES", g.DIRT_RULES_V17)
    monkeypatch.setattr(g, "FALLBACK_BEST", False)       # repli v17 : la moins terreuse (v19 : la mieux notée)
    kept = g.dirt_filter([_dirt(0.4), _dirt(1.6), _dirt(0.9)], "facile", lambda *_: None)
    assert sorted(round(l.shares["unpaved"] * 40, 1) for l in kept) == [0.4, 0.9]       # plafond 1 km
    only = g.dirt_filter([_dirt(2.5), _dirt(1.8)], "modere", lambda *_: None)
    assert len(only) == 1 and round(only[0].shares["unpaved"] * 40, 1) == 1.8 and only[0].unpaved_fallback   # repli annoncé


def test_terre_v17_inactive(monkeypatch):
    monkeypatch.setattr(g, "DIRT_RULES", None)
    pool = [_dirt(3.0)]
    assert g.dirt_filter(pool, "facile", lambda *_: None) == pool


# ----------------------------------------------------------------------------- filet de la marge (09/10/2026)
def test_marge_ne_vide_jamais_une_duree(monkeypatch):
    """Durée vidée par l'ordre des durées (Torrelles 1 h Modéré) : recalculée avec les plages strictes."""
    monkeypatch.setattr(g, "DURATION_BIN_MARGIN", 0.05)
    seen = []

    def fake_pool(gh, st, level, duration, candidates, log):
        seen.append(g.bin_margin())
        return [_loop(53, COORDS_A)]
    monkeypatch.setattr(g, "level_pool", fake_pool)
    options = [_opt("modere", 45, 52, "k45")]                    # boucle de 45 min : 52 min (écart de 3 min exigé)
    pool = g.ordered_pool(None, {}, "modere", 1.0, [], lambda *_: None, options)
    assert len(pool) == 1 and seen == [0.05, 0.0]                # 1er calcul avec la marge, 2e en plages strictes
    assert g.bin_margin() == 0.05                                # réglage rétabli


# ----------------------------------------------------------------------------- tracé (v17, 08/10/2026)
def _line(points):
    """Points (x m, y m, z m) autour de Barcelone -> coordonnées [lon, lat, z]."""
    return [[2.15 + x / 83800.0, 41.39 + y / 111000.0, z] for x, y, z in points]


def _cum(c):
    out = [0.0]
    for a, b in zip(c, c[1:]):
        out.append(out[-1] + g.haversine(a[0], a[1], b[0], b[1]))
    return out


def test_couloir_compte_la_voie_parallele():
    """Aller puis retour sur une voie parallèle à 15 m (autre chaussée) : répété, ce que la mesure v16 ne voyait pas."""
    c = _line([(x, 0, 10) for x in range(0, 1001, 50)] + [(x, 15, 10) for x in range(1000, -1, -50)])
    rep = sum(g.corridor_repeats(c, _cum(c))) / _cum(c)[-1]
    assert rep > 0.8


def test_couloir_ignore_les_lacets():
    """Lacets superposés sur la carte mais 30 m plus haut : pas une répétition."""
    c = _line([(x, 0, 10 + x * 0.03) for x in range(0, 1001, 50)] + [(x, 15, 40 + (1000 - x) * 0.03) for x in range(1000, -1, -50)])
    rep = sum(g.corridor_repeats(c, _cum(c))) / _cum(c)[-1]
    assert rep < 0.3


def test_detour_pate_de_maisons():
    """Tour d'un pâté de maisons au milieu d'une ligne droite : détecté (Paral·lel -> Calàbria -> retour)."""
    pts = [(x, 0, 10) for x in range(0, 1001, 20)]
    pts += [(1000, y, 10) for y in range(20, 201, 20)] + [(x, 200, 10) for x in range(980, 879, -20)]
    pts += [(880, y, 10) for y in range(180, 59, -20)] + [(x, 60, 10) for x in range(900, 1101, 20)]
    pts += [(x, 0, 10) for x in range(1120, 2001, 20)]
    found = g.block_detours(_line(pts))
    assert len(found) == 1 and found[0]["m"] > 500
    assert g.block_detours(_line([(x, 0, 10) for x in range(0, 2001, 20)])) == []     # ligne droite : rien


# ----------------------------------------------------------------------------- retours sur ses pas (09/10/2026)
def test_retour_utile_relief_selon_allure(monkeypatch):
    """Monter 50 m puis redescendre : utile en Modéré et Sportif, pas en Tranquille (sans rien d'autre au bout)."""
    monkeypatch.setattr(g, "POIS", None)
    monkeypatch.setattr(g, "LANDSCAPE", None)
    c = _line([(x, 0, 10 + x * 0.05) for x in range(0, 1001, 50)] + [(x, 5, 60 - (1000 - x) * 0.05) for x in range(1000, -1, -50)])
    n = len(c) - 1
    assert g.backtrack_useful(c, 0, n, "modere") == "relief"
    assert g.backtrack_useful(c, 0, n, "soutenu") == "relief"
    assert g.backtrack_useful(c, 0, n, "facile") is None


def test_retour_utile_piste_et_lieu(monkeypatch):
    """Aller-retour plat : utile s'il est sur piste cyclable, ou s'il passe par un lieu remarquable (Montjuïc)."""
    import shapely
    monkeypatch.setattr(g, "LANDSCAPE", None)
    monkeypatch.setattr(g, "POIS", None)
    c = _line([(x, 0, 10) for x in range(0, 1001, 50)] + [(x, 5, 10) for x in range(1000, -1, -50)])
    n = len(c) - 1
    assert g.backtrack_useful(c, 0, n, "facile") is None
    assert g.backtrack_useful(c, 0, n, "facile", ["c"] * n) == "piste"
    court = _line([(x, 0, 10) for x in range(0, 201, 50)] + [(x, 5, 10) for x in range(200, -1, -50)])   # 400 m
    assert g.backtrack_useful(court, 0, len(court) - 1, "facile", ["c"] * (len(court) - 1)) is None
    tip = c[20]
    monkeypatch.setattr(g, "POIS", {"lieu": (shapely.STRtree([shapely.Point(tip[0], tip[1])]), None)})
    assert g.backtrack_useful(c, 0, n, "facile") == "lieu"


def test_retour_jamais_sur_la_terre(monkeypatch):
    """Aller-retour sur piste cyclable mais avec 300 m de terre au bout : inutile (relecture de Florent, 09/10/2026)."""
    monkeypatch.setattr(g, "POIS", None)
    monkeypatch.setattr(g, "LANDSCAPE", None)
    c = _line([(x, 0, 10) for x in range(0, 1001, 50)] + [(x, 5, 10) for x in range(1000, -1, -50)])
    n = len(c) - 1
    unp = ["u" if 14 <= i < 26 else "-" for i in range(n)]                  # autour du demi-tour
    assert g.backtrack_useful(c, 0, n, "facile", ["c"] * n, ["-"] * n) == "piste"
    assert g.backtrack_useful(c, 0, n, "facile", ["c"] * n, unp) is None


def test_demi_tour_a_un_endroit_naturel(monkeypatch):
    """Monter 60 m puis faire demi-tour : utile (Modéré) seulement à un sommet ou un col nommé, sinon « pourquoi ici ? »."""
    import shapely
    monkeypatch.setattr(g, "LANDSCAPE", None)
    up = [(x, 0, 10 + x * 0.06) for x in range(0, 1001, 50)]                  # 10 -> 70 m
    c = _line(up + [(x, 5, 10 + x * 0.06) for x in range(1000, -1, -50)])
    n, tip = len(c) - 1, c[20]
    far = shapely.Point(tip[0] + 0.05, tip[1])                              # un sommet à 4 km : sans rapport
    monkeypatch.setattr(g, "POIS", {"peak": (shapely.STRtree([far]), None)})
    assert g.backtrack_useful(c, 0, n, "modere", t=20) is None
    monkeypatch.setattr(g, "POIS", {"peak": (shapely.STRtree([shapely.Point(tip[0], tip[1])]), None)})
    assert g.backtrack_useful(c, 0, n, "modere", t=20) == "relief"


def test_couloir_jamais_plus_strict_que_v16():
    """Boucle de montagne à 30 % de répétition par couloir mais 20 % en v16, sans autre boucle possible : gardée
    (mesure combinée du 10/10/2026, 14 durées vidées) ; la note, elle, compte le couloir."""
    l = SimpleNamespace(overlap=0.30, repeat_cycle_share=0.0, overlap_v16=0.20, repeat_cycle_share_v16=0.0)
    assert g.overlap_ok(l)
    l.overlap_v16 = 0.30
    assert not g.overlap_ok(l)
    assert g.overlap_ok(SimpleNamespace(overlap=0.20, repeat_cycle_share=0.0))      # sans mesure v16 : comme avant


def test_params_hash_unchanged_while_v19_off():
    """Les règles v19 sont éteintes : l'empreinte ne les compte pas (le calcul v18 en cours ne voit aucun changement) ;
    en allumer une change l'empreinte."""
    h0 = g.params_hash()
    old = g.TIME_V19
    g.TIME_V19 = not old
    try:
        assert g.params_hash() != h0
    finally:
        g.TIME_V19 = old
    assert g.params_hash() == h0


def test_time_v19_climbs_faster_descents_slower():
    """Modèle de temps v19 (sorties de Florent) : plus rapide qu'avant en montée (puissance +50 % à 6 %), plus lent en
    descente (plafond de l'allure), et 15 s par feu."""
    flat = [0.0] * 11
    up = [i * 6.0 for i in range(11)]                    # 1 km à 6 %
    down = [60.0 - i * 6.0 for i in range(11)]            # 1 km à −6 %
    w = g.level_watts("modere", 2.0)
    old = g.DENSITY
    g.DENSITY = SimpleNamespace()                         # time_v19() exige une densité chargée
    g._TL.time_v19 = True
    try:
        t = {k: g.estimate_time_s(100.0, p, w, 0.0, 0.0, 0, "modere", [0] * 11) for k, p in
             (("flat", flat), ("up", up), ("down", down))}
        lights = g.estimate_time_s(100.0, flat, w, 0.0, 0.0, 4, "modere", [0] * 11) - t["flat"]
    finally:
        del g._TL.time_v19
        g.DENSITY = old
    t18 = {k: g.estimate_time_s(100.0, p, w, 0.0, 0.0, 0) for k, p in (("up", up), ("down", down))}
    assert t["up"] < t18["up"]
    assert t["down"] > t18["down"]
    assert t["down"] >= 1000.0 / (g.DESCENT_CAP_KMH["modere"] / 3.6) - 1e-6
    assert abs(lights - 4 * g.SIGNAL_DELAY_V19_S) < 1e-6


def test_fallback_best_keeps_best_of_flattest():
    """Repli de relief (FALLBACK_BEST) : la mieux notée parmi les boucles à 1 m/km de la moins vallonnée (Sant Esteve
    de Llémena 4 h Tranquille, 14/10/2026), et non la moins vallonnée quelle que soit sa note."""
    class L:
        pass

    def mk(dpk, sc):
        l = L()
        l.dplus_per_km, l.score, l.level = dpk, sc, "facile"
        l.terrain = {"climbs": [], "max_grade_pct": 0.0}
        return l
    a, b, c = mk(12.0, 70.8), mk(11.9, 48.4), mk(14.0, 90.0)   # aucune sous 10 m/km
    g._TL.fallback_best = False
    try:
        assert g.relief_filter([a, b, c], "facile", lambda *_: None) == [b]      # v17 : la moins vallonnée
    finally:
        del g._TL.fallback_best
    g._TL.fallback_best = True
    try:
        assert g.relief_filter([a, b, c], "facile", lambda *_: None) == [a]      # 12,0 m/km à moins de 1 de 11,9
    finally:
        del g._TL.fallback_best


def test_contiguous_bins_close_the_gaps():
    """Tranches sans trou (CONTIGUOUS_BINS) : une boucle de 72 min trouve une durée, la tolérance borne les extrêmes."""
    g._TL.contiguous_bins = False                                             # le trou de la v18
    assert not g.time_ok(72 * 60, 1.0) and not g.time_ok(72 * 60, 1.5)
    g._TL.contiguous_bins = True
    try:
        assert g.time_ok(72 * 60, 1.0) or g.time_ok(72 * 60, 1.5)
        assert g.time_ok(145 * 60, 2.0) or g.time_ok(145 * 60, 3.0)
        assert not g.time_ok(36 * 60, 0.75)                                   # sous 45 min : tolérance de 15 %
        assert not g.time_ok(7 * 3600, 6.0)                                   # au-delà de 6 h : tolérance de 15 %
        assert not g.time_ok(100 * 60, 1.0)                                   # une « 1 h » ne prend pas 1 h 40
        assert not g.time_ok(53 * 60, 0.75) and not g.time_ok(200 * 60, 4.0)  # ailleurs, plages inchangées
        assert 72 <= g.bin_minutes(1.0)[1] <= 74 and abs(g.bin_minutes(1.5)[0] - g.bin_minutes(1.0)[1]) < 1e-6
    finally:
        del g._TL.contiguous_bins


def test_best_disjoint_takes_the_best_segment_of_each_loop():
    """Tronçon par tronçon à plusieurs boucles (SEG_MULTI) : de 1 à 2 le tronçon de B, de 2 à 3 celui de C ; un tronçon
    de D qui chevauche les deux et rapporte moins n'est pas pris."""
    B, C, D = [[0, 0]], [[1, 1]], [[2, 2]]
    b12 = (10, 20, 0, 5, 0.0, 0.05, B)
    c23 = (20, 30, 0, 5, 0.0, 0.04, C)
    d13 = (12, 28, 0, 5, 0.0, 0.07, D)
    chosen = g.best_disjoint([d13, c23, b12])
    assert [x[6] for x in sorted(chosen, key=lambda x: x[0])] == [B, C]
    assert g.best_disjoint([]) == []


def test_find_appendices_sant_fost_shape():
    """Petite boucle accrochée (APPENDIX_RULE) : route d'arrivée, détour d'environ 2 km qui revient au même carrefour,
    puis départ par la route d'arrivée ; un « 8 » (deux grands lobes) n'en est pas une."""
    import math
    def path(pts, step=10.0):                       # points tous les ~10 m le long des sommets (en mètres)
        out = []
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            n = max(1, int(math.hypot(x1 - x0, y1 - y0) // step))
            out += [(x0 + (x1 - x0) * k / n, y0 + (y1 - y0) * k / n) for k in range(n)]
        out.append(pts[-1])
        kx = 111320.0 * math.cos(math.radians(41.5))
        return [[2.2 + x / kx, 41.5 + y / 110540.0] for x, y in out]
    def cum(c):
        r = [0.0]
        for a, b in zip(c, c[1:]):
            r.append(r[-1] + g.haversine(a[0], a[1], b[0], b[1]))
        return r
    # grande boucle de ~20 km avec, au km ~5, un aller sur 1 km, un carré de 2 km, retour par le même km
    big = [(0, 0), (0, 4000), (0, 5000), (500, 5000), (500, 5500), (0, 5500), (0, 5000), (0, 4000),
           (4000, 4000), (4000, 0), (0, 0)]
    c = path(big)
    found = g.find_appendices(c, cum(c))
    assert len(found) == 1
    # un « 8 » : deux lobes de 8 km
    eight = [(0, 0), (2000, 2000), (0, 4000), (-2000, 2000), (0, 0), (2000, -2000), (0, -4000), (-2000, -2000), (0, 0)]
    c8 = path(eight)
    assert g.find_appendices(c8, cum(c8)) == []


def test_false_start_eixample_shape():
    """Faux départ (FALSE_START) : 2 km vers la mer, retour par une rue parallèle à 120 m, repassage au départ, puis la
    vraie boucle à l'opposé ; une boucle ordinaire n'en a pas."""
    import math
    kx = 111320.0 * math.cos(math.radians(41.39))
    def path(pts, step=20.0):
        out = []
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            n = max(1, int(math.hypot(x1 - x0, y1 - y0) // step))
            out += [(x0 + (x1 - x0) * k / n, y0 + (y1 - y0) * k / n) for k in range(n)]
        out.append(pts[-1])
        return [[2.16 + x / kx, 41.39 + y / 110540.0] for x, y in out]
    def cum(c):
        r = [0.0]
        for a, b in zip(c, c[1:]):
            r.append(r[-1] + g.haversine(a[0], a[1], b[0], b[1]))
        return r
    bad = path([(0, 0), (1500, -1500), (1580, -1420), (80, 80), (-3000, 3000), (-6000, 3000), (-6000, 0), (0, 0)])
    found = g.false_starts(bad, cum(bad))
    assert found and found[0][0] == 0
    good = path([(0, 0), (-3000, 3000), (-6000, 3000), (-6000, 0), (0, 0)])
    assert g.false_starts(good, cum(good)) == []


def test_corridor_spur_dual_carriageway():
    """Aller-retour dans un couloir (CORRIDOR_SPURS) : 1 km sur une chaussée, retour par l'autre à 25 m ; une boucle
    ordinaire n'en a pas."""
    import math
    kx = 111320.0 * math.cos(math.radians(41.54))
    def path(pts, step=10.0):
        out = []
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            n = max(1, int(math.hypot(x1 - x0, y1 - y0) // step))
            out += [(x0 + (x1 - x0) * k / n, y0 + (y1 - y0) * k / n) for k in range(n)]
        out.append(pts[-1])
        return [[2.4 + x / kx, 41.54 + y / 110540.0] for x, y in out]
    def cum(c):
        r = [0.0]
        for a, b in zip(c, c[1:]):
            r.append(r[-1] + g.haversine(a[0], a[1], b[0], b[1]))
        return r
    loop = path([(0, 0), (3000, 0), (3000, 3000), (4000, 3000), (4000, 3025), (3000, 3025), (0, 3025), (0, 0)])
    found = g.corridor_spurs(loop, cum(loop))
    assert len(found) == 1
    plain = path([(0, 0), (3000, 0), (3000, 3000), (0, 3000), (0, 0)])
    assert g.corridor_spurs(plain, cum(plain)) == []


def test_defect_windows_merged():
    """Nettoyage (CLEAN_PICKS) : détours, éperons et boucles accrochées fusionnés quand ils se chevauchent."""
    class L:
        detours = [{"a": 10, "b": 20}]
        spurs = [{"a": 15, "b": 30}, {"a": 50, "b": 60}]
        appendices = [{"a": 70, "b": 80}]

    class C:
        detours, spurs, appendices = [], [], []
    assert g.defect_windows(L()) == [(10, 30), (50, 60), (70, 80)]
    assert g.defect_windows(C()) == []


def test_loopify_way_pier():
    """Aller-retour transformé en petite boucle (CLEAN_PICKS, le Moll d'Espanya) : entrée, un point à 120 m d'un côté du
    milieu de l'aller, le demi-tour, le point symétrique de l'autre côté."""
    import math
    kx = 111320.0 * math.cos(math.radians(41.38))
    pts = [(0, -2000), (0, 0), (0, 1000), (0, 0), (2000, 0), (2000, -2000), (0, -2000)]
    coords = []
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        n = max(1, int(math.hypot(x1 - x0, y1 - y0) // 20))
        coords += [[2.18 + (x0 + (x1 - x0) * k / n) / kx, 41.38 + (y0 + (y1 - y0) * k / n) / 110540.0] for k in range(n)]
    coords.append([2.18, 41.38 - 2000 / 110540.0])
    a = next(i for i, c in enumerate(coords) if abs(c[1] - 41.38) < 1e-9)
    t = max(range(len(coords) // 2), key=lambda i: coords[i][1])
    b = next(i for i in range(t + 1, len(coords)) if abs(coords[i][1] - 41.38) < 1e-9 and abs(coords[i][0] - 2.18) < 1e-9)
    way = g.loopify_way(coords, [(a, b)], [(a, b)])
    k = next(i for i, p in enumerate(way) if p[0] == coords[a][0] and p[1] == coords[a][1])
    left, tip, right = way[k + 1], way[k + 2], way[k + 3]
    assert abs(tip[1] - coords[t][1]) < 1e-9
    assert abs((left[0] - 2.18) * kx - 120.0) < 5 and abs((right[0] - 2.18) * kx + 120.0) < 5


def test_splice_path_details():
    """Nettoyage recousu (CLEAN_PICKS, 4e essai) : les points a..b remplacés, les détails décalés, le reste intact."""
    pts = [[0.0, float(i), 10.0 + i] for i in range(10)]
    path = {"points": {"coordinates": pts}, "ascend": 9.0,
            "details": {"road_class": [[0, 3, "primary"], [3, 9, "residential"]], "surface": [[0, 9, "asphalt"]]}}
    sub = {"points": {"coordinates": [[0.0, 3.0, 13.0], [0.5, 4.0, 13.0], [0.0, 6.0, 16.0]]}, "ascend": 3.0,
           "details": {"road_class": [[0, 2, "cycleway"]], "surface": [[0, 2, "asphalt"]]}}
    new = g.splice_path(path, 3, 6, sub)
    c = new["points"]["coordinates"]
    assert len(c) == 10 - 4 + 3 and c[:3] == pts[:3] and c[-3:] == pts[7:]
    rc = g._edges_raw(new["details"]["road_class"], len(c) - 1)
    assert rc == ["primary"] * 3 + ["cycleway"] * 2 + ["residential"] * 3
    assert new["details"]["surface"] == [[0, 8, "asphalt"]]
    assert abs(new["ascend"] - (9.0 - 3.0 + 3.0)) < 1e-9


def test_spur_turn_window_roundabout():
    """Demi-tour par un rond-point (SPUR_TURN_WINDOW, Olot) : 350 m d'aller, tour d'un rond-point de 15 m de rayon, retour
    par la même route ; vu seulement avec la règle."""
    import math
    kx = 111320.0 * math.cos(math.radians(42.18))
    pts = [(-1500.0, 0.0)] + [(x, 0.0) for x in range(-1000, 351, 10)]
    pts += [(350 + 15 * math.sin(a / 10.0 * 2 * math.pi), 15 - 15 * math.cos(a / 10.0 * 2 * math.pi)) for a in range(1, 10)]
    pts += [(x, 2.0) for x in range(340, -1001, -10)] + [(-1500.0, 2.0), (-1500.0, 1500.0)]
    coords = [[2.48 + x / kx, 42.18 + y / 110540.0, 400.0] for x, y in pts]
    old = g.SPUR_TURN_WINDOW
    try:
        g.SPUR_TURN_WINDOW = False
        before = [s for s in g.spur_list(coords) if s["m"] >= 300]
        g.SPUR_TURN_WINDOW = True
        after = [s for s in g.spur_list(coords) if s["m"] >= 300]
    finally:
        g.SPUR_TURN_WINDOW = old
    assert before == [] and len(after) == 1

