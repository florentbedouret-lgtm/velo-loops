# -*- coding: utf-8 -*-
"""Pistes « grade2 » empruntées par les boucles publiées (Florent, 25/10/2026 : le Camí de la Pleta, noté béton mais
grade2 dans OSM, est en gravier). Liste les pistes (highway=track, tracktype=grade2, au moins 150 m) qu'une boucle
publiée longe sur au moins 60 % de leur longueur, pour vérification sur le terrain.

Usage : python scripts/track_probe.py data/region.osm.pbf data https://.../web/data > data/nature_compare_tracks.json
"""
import json
import math
import subprocess
import sys
import urllib.request
from pathlib import Path


def hav(a, b):
    t = math.pi / 180
    x = (math.sin((b[1] - a[1]) * t / 2) ** 2
         + math.cos(a[1] * t) * math.cos(b[1] * t) * math.sin((b[0] - a[0]) * t / 2) ** 2)
    return 2 * 6371000 * math.asin(math.sqrt(x))


def main():
    pbf, wd, site = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3].rstrip("/")
    filt, out = wd / "trk.osm.pbf", wd / "trk.geojsonseq"
    subprocess.run(["osmium", "tags-filter", str(pbf), "w/highway=track", "-o", str(filt), "--overwrite"], check=True,
                   capture_output=True)
    subprocess.run(["osmium", "export", str(filt), "-f", "geojsonseq", "-o", str(out), "--overwrite"], check=True,
                   capture_output=True)
    ways = []
    for line in out.read_text(encoding="utf-8").splitlines():
        line = line.strip("\x1e ")
        if not line:
            continue
        f = json.loads(line)
        p, g = f.get("properties") or {}, f.get("geometry") or {}
        if p.get("tracktype") != "grade2" or g.get("type") != "LineString":
            continue
        c = g["coordinates"]
        length = sum(hav(a, b) for a, b in zip(c, c[1:]))
        if length >= 150:
            ways.append((p, c, length))
    idx = json.loads(urllib.request.urlopen(site + "/index.json", timeout=60).read())
    cell = 0.002
    found = {}
    for e in idx["starts"]:
        try:
            js = json.loads(urllib.request.urlopen(f"{site}/starts/{e['id']}.json", timeout=60).read())
        except Exception:  # noqa: BLE001
            continue
        grid: dict = {}
        for o in js["options"]:
            for c in o["coords"]:
                grid.setdefault((int(c[0] / cell), int(c[1] / cell)), []).append((c[0], c[1], o["id"]))
        if not grid:
            continue
        for p, c, length in ways:
            hits = []
            for v in c:
                gx, gy = int(v[0] / cell), int(v[1] / cell)
                hits.append([q for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                             for q in grid.get((gx + dx, gy + dy), []) if hav(v, q) < 40])
            if sum(1 for h in hits if h) >= max(3, 0.6 * len(c)):
                key = p.get("@id") or "%s" % (c[len(c) // 2],)
                ids = sorted({q[2] for h in hits for q in h})
                rec = found.setdefault(str(key), {"name": p.get("name"), "surface": p.get("surface"),
                                                 "long_m": round(length), "lat": round(c[len(c) // 2][1], 5),
                                                 "lon": round(c[len(c) // 2][0], 5), "boucles": []})
                rec["boucles"] += ids
    json.dump(found, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
