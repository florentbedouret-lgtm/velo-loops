#!/usr/bin/env python3
"""
Extraction des voies de la base topographique ICGC (Referencial topogràfic territorial, CC BY 4.0) pour la province de
Barcelone, À FAIRE SUR UN PC (28/09/2026) : le serveur de l'ICGC ne répond pas aux machines GitHub (run #133 : délai
dépassé, même avec curl). Le fichier produit est déposé dans une « release » du dépôt, que le workflow télécharge.

Bibliothèque standard seulement (Python 3.6+) : le GeoPackage est une base SQLite ; géométries GPKG/WKB décodées ici ;
coordonnées ETRS89 / UTM 31N (EPSG:25831) converties en longitude / latitude (formules de Krüger, précision < 1 mm).

Usage : python scripts/icgc_extract.py --gpkg D:/Claude/icgc/topografia-territorial-v1r0-2024.gpkg \
            --out D:/Claude/icgc/icgc_vials_barcelona_2024.geojsonseq.gz
Sortie : GeoJSONSeq compressé (gzip), une ligne par voie : {"type":"Feature","properties":{"tipus":...},"geometry":...}
"""
import argparse
import gzip
import json
import math
import sqlite3
import struct
import sys
import time
from collections import Counter

LAYER = "_35_transports_l"
KEEP = {"aut", "vcd", "vcu", "vnc", "vpd", "vpu", "bir", "vca", "cor", "bin"}   # voies (axes), revêtues ou non
BBOX = (1.35, 41.18, 2.80, 42.33)                                               # province de Barcelone (lon / lat)

# ---------------------------------------------------------------- UTM 31N (ETRS89 = GRS80) -> lon / lat
_A, _F = 6378137.0, 1 / 298.257222101
_N = _F / (2 - _F)
_AA = _A / (1 + _N) * (1 + _N ** 2 / 4 + _N ** 4 / 64)
_BETA = (_N / 2 - 2 * _N ** 2 / 3 + 37 * _N ** 3 / 96,
         _N ** 2 / 48 + _N ** 3 / 15,
         17 * _N ** 3 / 480)
_DELTA = (2 * _N - 2 * _N ** 2 / 3 - 2 * _N ** 3,
          7 * _N ** 2 / 3 - 8 * _N ** 3 / 5,
          56 * _N ** 3 / 15)
_K0, _E0, _LON0 = 0.9996, 500000.0, math.radians(3.0)


def utm31_to_lonlat(x, y):
    xi = y / (_K0 * _AA)
    eta = (x - _E0) / (_K0 * _AA)
    xi_p, eta_p = xi, eta
    for j, b in enumerate(_BETA, start=1):
        xi_p -= b * math.sin(2 * j * xi) * math.cosh(2 * j * eta)
        eta_p -= b * math.cos(2 * j * xi) * math.sinh(2 * j * eta)
    chi = math.asin(math.sin(xi_p) / math.cosh(eta_p))
    lat = chi
    for j, d in enumerate(_DELTA, start=1):
        lat += d * math.sin(2 * j * chi)
    lon = _LON0 + math.atan2(math.sinh(eta_p), math.cos(xi_p))
    return math.degrees(lon), math.degrees(lat)


# ---------------------------------------------------------------- géométrie GeoPackage (en-tête GP + WKB)
_ENV = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}


def gpkg_lines(blob):
    """Liste de lignes [(x, y), ...] d'une géométrie GPKG (LineString ou MultiLineString, 2D/Z/M/ZM)."""
    if not blob or blob[:2] != b"GP":
        return []
    flags = blob[3]
    off = 8 + _ENV.get((flags >> 1) & 7, 0)
    lines = []

    def read(o):
        bo = "<" if blob[o] == 1 else ">"
        t = struct.unpack_from(bo + "I", blob, o + 1)[0]
        o += 5
        base, dims = t % 1000, t // 1000
        if t & 0x80000000 or t & 0x40000000:          # EWKB (rare en GPKG)
            base, dims = t & 0xFF, (1 if t & 0x80000000 else 0) + (2 if t & 0x40000000 else 0)
        nd = 2 + (1 if dims in (1, 3) else 0) + (1 if dims in (2, 3) else 0)
        if base == 2:
            n = struct.unpack_from(bo + "I", blob, o)[0]
            o += 4
            vals = struct.unpack_from(bo + "d" * (n * nd), blob, o)
            lines.append([(vals[i * nd], vals[i * nd + 1]) for i in range(n)])
            return o + 8 * n * nd
        if base == 5:
            n = struct.unpack_from(bo + "I", blob, o)[0]
            o += 4
            for _ in range(n):
                o = read(o)
            return o
        raise ValueError(f"type WKB inattendu {t}")
    read(off)
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gpkg", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    t0 = time.time()
    db = sqlite3.connect(f"file:{args.gpkg}?mode=ro", uri=True)
    srs = db.execute("SELECT srs_id FROM gpkg_geometry_columns WHERE table_name = ?", (LAYER,)).fetchone()
    print(f"Couche {LAYER}, système de coordonnées {srs}", flush=True)
    if not srs or srs[0] != 25831:
        sys.exit("système de coordonnées inattendu (UTM 31N, EPSG:25831, attendu)")
    seen, kept, npts = Counter(), Counter(), 0
    w, s, e, n = BBOX
    with gzip.open(args.out, "wt", encoding="utf-8", compresslevel=9) as out:
        for k, (blob, tipus) in enumerate(db.execute(f'SELECT geom, tipus FROM "{LAYER}"')):
            seen[tipus] += 1
            if k % 500000 == 0:
                print(f"  {k} voies lues ({time.time() - t0:.0f} s)", flush=True)
            if tipus not in KEEP:
                continue
            for line in gpkg_lines(blob):
                if len(line) < 2:
                    continue
                cx, cy = utm31_to_lonlat(*line[len(line) // 2])
                if not (w - 0.05 <= cx <= e + 0.05 and s - 0.05 <= cy <= n + 0.05):
                    continue
                coords = [[round(v, 6) for v in utm31_to_lonlat(x, y)] for x, y in line]
                out.write(json.dumps({"type": "Feature", "properties": {"tipus": tipus},
                                      "geometry": {"type": "LineString", "coordinates": coords}},
                                     separators=(",", ":")) + "\n")
                kept[tipus] += 1
                npts += len(coords)
    print("Types lus (tous) : " + ", ".join(f"{k} {v}" for k, v in seen.most_common()), flush=True)
    print("Voies gardées (province) : " + ", ".join(f"{k} {v}" for k, v in kept.most_common())
          + f" ; {npts} points ; {time.time() - t0:.0f} s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
