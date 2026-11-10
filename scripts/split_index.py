# -*- coding: utf-8 -*-
"""Index allégé pour l'app (10/11/2026) : le bloc « compare » de chaque départ fait 87 % de index.json (268 Ko compressés
sur 340) alors que l'accueil n'en a besoin qu'après l'affichage de la carte (départs proposés, autres façons de rouler).
Écrit, à côté de index.json (gardé entier pour les outils) :
  index_app.json : index.json sans les blocs « compare » ;
  compare.json   : {identifiant du départ: bloc compare}, chargé par l'app juste après.
Usage : python scripts/split_index.py <dossier des données, ex. _site/web/data>"""
import json
import sys
from pathlib import Path


def split(index: dict) -> tuple[dict, dict]:
    starts, compare = [], {}
    for s in index.get("starts", []):
        if "compare" in s:
            compare[s["id"]] = s["compare"]
        starts.append({k: v for k, v in s.items() if k != "compare"})
    return {**index, "starts": starts}, compare


def main(data: Path) -> None:
    index = json.loads((data / "index.json").read_text(encoding="utf-8"))
    app, compare = split(index)
    dump = lambda o: json.dumps(o, ensure_ascii=False, separators=(",", ":"))   # noqa: E731
    (data / "index_app.json").write_text(dump(app), encoding="utf-8")
    (data / "compare.json").write_text(dump(compare), encoding="utf-8")
    kb = lambda n: round((data / n).stat().st_size / 1024)                      # noqa: E731
    print(f"index.json {kb('index.json')} Ko -> index_app.json {kb('index_app.json')} Ko + compare.json "
          f"{kb('compare.json')} Ko ({len(compare)} départs)")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
