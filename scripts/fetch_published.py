#!/usr/bin/env python3
"""
Récupère les données déjà publiées sur le site (web/data/index.json, web/data/starts/*.json et
web/approach_model.json) pour republier le site sans rien recalculer (mode « site » du workflow :
nouvelle version de la page web, mêmes données).

Sans ce script, un fichier committé dans le dépôt mais jamais republié depuis (ex. web/approach_model.json
édité à la main) resterait périmé indéfiniment : le mode « site » se déclenche automatiquement à chaque
modification du front et republierait alors la version périmée du dépôt à la place de celle réellement en
ligne. En le retéléchargeant à chaque fois depuis le site publié, ce script rend le mode « site » sans effet
de bord sur les données, quel que soit l'état du dépôt.

Usage : python scripts/fetch_published.py --site https://<compte>.github.io/<dépôt> --out web/data
Échoue (code 1) si un fichier manque : mieux vaut ne rien publier qu'un site incomplet.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def get(url: str, retries: int = 4) -> bytes:
    last = None
    for k in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "velo-loops-site-mode", "Accept-Encoding": "identity"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = e
            time.sleep(1.5 * (k + 1))
    raise RuntimeError(f"{url} : {last}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--site", required=True)
    ap.add_argument("--out", default="web/data")
    ap.add_argument("--workers", type=int, default=16)
    args = ap.parse_args()

    site = args.site.rstrip("/")
    base = site + "/web/data"
    out = Path(args.out)
    (out / "starts").mkdir(parents=True, exist_ok=True)

    model_raw = get(site + "/web/approach_model.json")
    model_path = out.parent / "approach_model.json"
    model_path.write_bytes(model_raw)
    print(f"approach_model.json récupéré ({model_path})", flush=True)

    raw = get(base + "/index.json")
    index = json.loads(raw)
    if not index.get("coverage_bbox") and index.get("starts"):        # champ additif : boîte englobante des départs
        xs = [s["lon"] for s in index["starts"]]
        ys = [s["lat"] for s in index["starts"]]
        index["coverage_bbox"] = [round(min(xs), 4), round(min(ys), 4), round(max(xs), 4), round(max(ys), 4)]
        raw = json.dumps(index, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        print(f"coverage_bbox ajouté à index.json : {index['coverage_bbox']}", flush=True)
    (out / "index.json").write_bytes(raw)
    ids = [s["id"] for s in index["starts"]]
    print(f"index.json : {len(ids)} départs, généré le {index.get('generated_at')}", flush=True)

    def one(i: str):
        try:
            data = get(f"{base}/starts/{i}.json")
            json.loads(data)                               # vérifie que le fichier est complet
            (out / "starts" / f"{i}.json").write_bytes(data)
            return i, len(data), None
        except Exception as e:                             # noqa: BLE001
            return i, 0, str(e)

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        results = list(ex.map(one, ids))
    bad = [(i, e) for i, n, e in results if e]
    total = sum(n for _, n, _ in results)
    print(f"{len(ids) - len(bad)} fichiers récupérés ({total / 1024 / 1024:.1f} Mo) en {time.time() - t0:.0f} s", flush=True)
    if bad:
        for i, e in bad[:10]:
            print(f"! {i} : {e}", file=sys.stderr)
        print(f"ERREUR : {len(bad)} fichier(s) manquant(s) : rien ne sera publié.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
