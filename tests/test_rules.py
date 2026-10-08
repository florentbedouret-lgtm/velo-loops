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
def test_essais_desactives_en_production():
    """Les essais de la v17 restent éteints tant qu'ils ne sont pas ajoutés à l'empreinte et recalculés."""
    assert g.DURATION_BIN_MARGIN is None
    assert g.RELIEF_LIMITS is None
    assert g.TARGETED_GATE is None


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
