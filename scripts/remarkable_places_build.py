#!/usr/bin/env python3
"""Liste des lieux remarquables (scripts/remarkable_places.json) depuis Wikidata (CC0), à relancer à la main de temps en temps.

Lieux de la province de Barcelone ayant au moins MIN_SITELINKS articles Wikipédia (toutes langues) : sommets, cols et
belvédères (liste du 01/10/2026), plus, depuis le 03/10/2026 (Florent : Montserrat manquait), monastères, abbayes,
sanctuaires, châteaux, barrages, réservoirs et lacs. Les cathédrales et les monuments de la vieille ville sont écartés
(but de sortie à vélo improbable, traversée du centre). Les champs ajoutés à la main dans le fichier existant
(« point », « via », « exclude ») sont gardés.

Usage : python scripts/remarkable_places_build.py   (stdlib seulement)
"""
import json
import urllib.parse
import urllib.request
from pathlib import Path

MIN_SITELINKS = 6
KINDS = {"Q8502": "sommet", "Q133056": "col", "Q1440300": "belvédère", "Q44613": "monastère", "Q160742": "abbaye",
         "Q29553": "sanctuaire", "Q23413": "château", "Q12323": "barrage", "Q131681": "réservoir", "Q23397": "lac"}
EXCLUDE = {"Q17155", "Q2101038", "Q5117140"}   # cathédrale de Barcelone, Sant Pau del Camp, Santa Anna (vieille ville)
OUT = Path(__file__).parent / "remarkable_places.json"


def query():
    q = ("SELECT ?item ?itemLabel ?cls ?n ?coord WHERE { VALUES ?cls { " + " ".join("wd:" + k for k in KINDS) + " } "
         "?item wdt:P31/wdt:P279? ?cls ; wdt:P131* wd:Q81949 ; wikibase:sitelinks ?n ; wdt:P625 ?coord . "
         f"FILTER(?n >= {MIN_SITELINKS}) SERVICE wikibase:label {{ bd:serviceParam wikibase:language \"ca,es,fr,en\". }} }}")
    req = urllib.request.Request("https://query.wikidata.org/sparql?format=json&query=" + urllib.parse.quote(q),
                                 headers={"User-Agent": "oyan-velo-loops/1.0 (github florentbedouret-lgtm/velo-loops)"})
    return json.loads(urllib.request.urlopen(req, timeout=120).read().decode("utf-8"))["results"]["bindings"]


def main():
    old = json.loads(OUT.read_text(encoding="utf-8")).get("places", {}) if OUT.exists() else {}
    places = {}
    for b in query():
        qid = b["item"]["value"].rsplit("/", 1)[1]
        if qid in EXCLUDE:
            continue
        lon, lat = (float(x) for x in b["coord"]["value"][6:-1].split())
        e = places.setdefault(qid, {"label": b["itemLabel"]["value"], "sitelinks": int(b["n"]["value"]), "kind": [],
                                    "coord": [round(lon, 6), round(lat, 6)]})
        k = KINDS[b["cls"]["value"].rsplit("/", 1)[1]]
        if k not in e["kind"]:
            e["kind"].append(k)
    for qid, e in places.items():                     # champs relus à la main : gardés
        for k in ("point", "via", "exclude"):
            if k in old.get(qid, {}):
                e[k] = old[qid][k]
    note = ("Lieux remarquables de la province de Barcelone ayant au moins 6 articles Wikipédia (Wikidata, CC0), construits par "
            "scripts/remarkable_places_build.py. Sommets, cols, belvédères : un élément OSM de ce type portant le tag "
            "wikidata compte comme lieu (tirages ciblés, bonus). « point » [lon, lat] : endroit visé à vélo quand le lieu "
            "lui-même est inaccessible (ex. le monastère pour Montserrat), proposé par le rapport « lieux » puis relu ; "
            "« via » : ce qu'est ce point ; « exclude » : true pour écarter un lieu.")
    OUT.write_text(json.dumps({"_note": note, "min_sitelinks": MIN_SITELINKS,
                               "places": dict(sorted(places.items(), key=lambda kv: -kv[1]["sitelinks"]))},
                              ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{len(places)} lieux -> {OUT}")


if __name__ == "__main__":
    main()
