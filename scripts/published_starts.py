#!/usr/bin/env python3
"""
Liste des départs DÉJÀ PUBLIÉS, au format de starts_plan.json, pour régénérer les boucles sans refaire le plan
(O-13, handoff.md). Pourquoi : le plan choisit les départs un par un (couverture maximale) ; une petite évolution
d'OpenStreetMap décale les choix en cascade (84 départs intermédiaires déplacés en 2 jours, run #57 du 25/09/2026).
Garder les départs publiés rend les régénérations comparables et la réutilisation (--reuse-from) efficace.

Chaque départ garde sa clé `key` (empreinte de la position du PLAN) : generate_loops.py l'utilise telle quelle pour
retrouver l'entrée publiée, car lon/lat publiés sont la position recalée sur la route, pas celle du plan.

Usage : python scripts/published_starts.py --site https://<compte>.github.io/<dépôt> --out data/published_starts.json
        [--extra scripts/restore_starts.json]   (départs à rétablir, ajoutés s'ils manquent au site : même format)
Échoue (code 1) si l'index publié est introuvable ou vide : mieux vaut s'arrêter que générer un site vide.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import requests

FIELDS = ("name", "lon", "lat", "kind", "zone", "display_name", "municipality", "key")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--site", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--extra", default=None, help="JSON de départs à ajouter s'ils manquent au site (même format, clé `key`)")
    args = ap.parse_args()
    r = requests.get(f"{args.site.rstrip('/')}/web/data/index.json", timeout=60)
    r.raise_for_status()
    starts = r.json().get("starts", [])
    if not starts:
        print("! index publié vide : arrêt", file=sys.stderr)
        return 1
    out = [{k: s[k] for k in FIELDS if s.get(k) is not None} for s in starts]
    if args.extra and Path(args.extra).exists():
        have = {s.get("key") for s in out}
        extra = [{k: s[k] for k in FIELDS if s.get(k) is not None}
                 for s in json.loads(Path(args.extra).read_text(encoding="utf-8")) if s.get("key") not in have]
        out += extra
        print(f"Départs rétablis depuis {args.extra} : {len(extra)}")
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Départs publiés repris : {len(out)} (sans clé : {sum(1 for s in out if 'key' not in s)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
