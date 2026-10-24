# -*- coding: utf-8 -*-
"""Diagnostic des villages « particulièrement jolis » (O-92) : pour chaque village et ville de la région, ce que les
données ouvertes disent de son centre (monuments et sites historiques, patrimoine protégé, curiosités touristiques,
article Wikipédia). Sert à trouver un critère qui place en tête les exemples de Florent (24/10/2026).

Usage : python scripts/village_probe.py data/region.osm.pbf data > data/nature_compare_villages.json
"""
import json
import math
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

EXAMPLES = ["Rupit", "Mura", "Cardona", "Caldes de Montbui", "Sitges", "Sant Pol de Mar", "Garraf",
            "Castellar de n'Hug", "Bagà", "Tavertet", "Talamanca", "Gironella", "Súria", "Moià", "Pujalt", "Castellet",
            "Montseny", "Pontons", "Canet de Mar", "Colònia Güell"]
RADII_M = (400, 800)


def export(pbf: Path, wd: Path, name: str, filters: list) -> list:
    filt, out = wd / f"{name}.osm.pbf", wd / f"{name}.geojsonseq"
    subprocess.run(["osmium", "tags-filter", str(pbf), *filters, "-o", str(filt), "--overwrite"], check=True,
                   capture_output=True)
    subprocess.run(["osmium", "export", str(filt), "-f", "geojsonseq", "-o", str(out), "--overwrite"], check=True,
                   capture_output=True)
    feats = []
    for line in out.read_text(encoding="utf-8").splitlines():
        line = line.strip("\x1e ")
        if not line:
            continue
        try:
            f = json.loads(line)
        except ValueError:
            continue
        g = f.get("geometry") or {}
        c = g.get("coordinates")
        if not c:
            continue
        while isinstance(c[0], list):                       # premier point d'une ligne ou d'un polygone
            c = c[0]
        feats.append((c[0], c[1], f.get("properties") or {}))
    return feats


UA = {"User-Agent": "velo-loops diagnostic (github.com/florentbedouret-lgtm/velo-loops)"}


def _get(url: str):
    """JSON d'une API publique de Wikimedia, avec un rythme modéré et deux nouvelles tentatives (429, 5xx)."""
    for k in range(5):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                time.sleep(0.5)
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            print(f"! {e.code} : nouvel essai dans {5 * 2 ** k} s", file=sys.stderr)
            time.sleep(5.0 * 2 ** k)
        except Exception as e:  # noqa: BLE001 : réseau
            print(f"! {type(e).__name__} : nouvel essai", file=sys.stderr)
            time.sleep(5.0 * 2 ** k)
    return None


def ca_titles(qids: list) -> dict:
    """Titre de l'article catalan et nombre de langues (sitelinks) de chaque identifiant Wikidata."""
    out = {}
    for i in range(0, len(qids), 50):
        j = _get("https://www.wikidata.org/w/api.php?action=wbgetentities&props=sitelinks&format=json&ids="
                 + "|".join(qids[i:i + 50])) or {}
        for q, e in (j.get("entities") or {}).items():
            sl = e.get("sitelinks") or {}
            out[q] = ((sl.get("cawiki") or {}).get("title"), len(sl))
    return out


def pageviews(lang: str, title: str):
    """Lectures sur 12 mois d'un article Wikipédia (API publique de Wikimedia) ; None si indisponible."""
    if not title:
        return None
    j = _get("https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/%s.wikipedia/all-access/user/%s/monthly/"
             "20250901/20260831" % (lang, urllib.parse.quote(title.replace(" ", "_"), safe="")))
    return None if j is None else sum(i.get("views", 0) for i in j.get("items", []))


def main():
    pbf, wd = Path(sys.argv[1]), Path(sys.argv[2])
    places = [p for p in export(pbf, wd, "vil_places", ["n/place=village,town,hamlet"]) if p[2].get("name")]
    feats = export(pbf, wd, "vil_feats", ["nwr/historic", "nwr/heritage", "nwr/tourism=attraction,museum",
                                          "nwr/amenity=place_of_worship",
                                          "nwr/tourism=hotel,guest_house,hostel,apartment,chalet,information,viewpoint",
                                          "nwr/amenity=restaurant,cafe,bar"])
    nature = export(pbf, wd, "vil_nature", ["nwr/landuse=forest", "nwr/natural=wood", "nwr/natural=water",
                                            "w/waterway=river", "n/natural=peak", "nwr/natural=cliff",
                                            "nwr/leisure=nature_reserve", "nwr/boundary=protected_area"])
    cell = 0.01
    grid: dict = {}
    for x, y, p in feats:
        grid.setdefault((int(x / cell), int(y / cell)), []).append((x, y, p))
    ngrid: dict = {}
    for x, y, p in nature:
        ngrid.setdefault((int(x / cell), int(y / cell)), []).append((x, y, p))
    rows = []
    for x0, y0, p0 in places:
        kx = 111320.0 * math.cos(math.radians(y0))
        row = {"name": p0["name"], "place": p0.get("place"), "lon": round(x0, 5), "lat": round(y0, 5),
               "population": p0.get("population"), "wikidata": bool(p0.get("wikidata")),
               "wikipedia": p0.get("wikipedia"), "wikidata_id": p0.get("wikidata")}
        near = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for x, y, p in grid.get((int(x0 / cell) + dx, int(y0 / cell) + dy), []):
                    d = math.hypot((x - x0) * kx, (y - y0) * 110540.0)
                    if d <= max(RADII_M):
                        near.append((d, p))
        for r in RADII_M:
            sel = [p for d, p in near if d <= r]
            row[f"historique_{r}"] = sum(1 for p in sel if p.get("historic"))
            row[f"patrimoine_{r}"] = sum(1 for p in sel if p.get("heritage"))
            row[f"patrimoine_national_{r}"] = sum(1 for p in sel if str(p.get("heritage")) in ("1", "2"))
            row[f"curiosites_{r}"] = sum(1 for p in sel if p.get("tourism") in ("attraction", "museum"))
            row[f"hebergements_{r}"] = sum(1 for p in sel if p.get("tourism") in ("hotel", "guest_house", "hostel",
                                                                                     "apartment", "chalet"))
            row[f"accueil_{r}"] = sum(1 for p in sel if p.get("tourism") in ("information", "viewpoint"))
            row[f"restauration_{r}"] = sum(1 for p in sel if p.get("amenity") in ("restaurant", "cafe", "bar"))
            row[f"avec_wikidata_{r}"] = sum(1 for p in sel if p.get("wikidata") and (p.get("historic") or p.get("heritage")))
        nn = []                                               # cadre : nature à moins de 1 km (premier point de l'objet)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for x, y, p in ngrid.get((int(x0 / cell) + dx, int(y0 / cell) + dy), []):
                    if math.hypot((x - x0) * kx, (y - y0) * 110540.0) <= 1000:
                        nn.append(p)
        row["foret_1km"] = sum(1 for p in nn if p.get("landuse") == "forest" or p.get("natural") == "wood")
        row["eau_1km"] = sum(1 for p in nn if p.get("natural") == "water" or p.get("waterway") == "river")
        row["sommets_1km"] = sum(1 for p in nn if p.get("natural") == "peak")
        row["falaises_1km"] = sum(1 for p in nn if p.get("natural") == "cliff")
        row["protege_1km"] = sum(1 for p in nn if p.get("leisure") == "nature_reserve" or p.get("boundary") == "protected_area")
        try:
            row["altitude"] = float(str(p0.get("ele", "")).split()[0])
        except (ValueError, IndexError):
            row["altitude"] = None
        low = row["name"].lower()
        row["exemple"] = any(e.lower() == low or low.startswith(e.lower() + " ") or low.startswith(e.lower() + ",")
                             for e in EXAMPLES)
        rows.append(row)
    titles = ca_titles(sorted({r["wikidata_id"] for r in rows if r.get("wikidata_id")}))
    for r in rows:
        t, n = titles.get(r.get("wikidata_id") or "", (None, 0))
        if not t and r.get("wikipedia") and str(r["wikipedia"]).startswith("ca:"):
            t = str(r["wikipedia"])[3:]
        r["langues_wikipedia"] = n
        r["lectures_wikipedia"] = pageviews("ca", t)
    json.dump({"examples": EXAMPLES, "places": rows}, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
