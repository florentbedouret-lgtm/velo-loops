#!/usr/bin/env python3
"""Zones agréables pour GraphHopper (essai v19, 14/10/2026) : bandes le long du littoral et des rivières, grands parcs.

Florent (Eixample 2 h et 3 h) : les boucles passent par le couloir intérieur plutôt que par la piste du bord de mer de la
Barceloneta ou le parc fluvial du Besòs, alors qu'OSM y autorise le vélo. Cause : GraphHopper ne connaît pas le paysage,
seule la note d'Oyan le compte, après coup. Ce script écrit un modèle personnalisé GraphHopper (`loop_scenic.json`) avec
une zone « scenic » et un bonus de préférence pour les voies calmes qui s'y trouvent (pas pour les grands axes).

    python scripts/scenic_areas.py --pbf data/region.osm.pbf --workdir data --out <dossier>/loop_scenic.json
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

COAST_BUFFER_M = 150.0      # bande le long du littoral
RIVER_BUFFER_M = 120.0      # bande le long des rivières (waterway=river, pas les ruisseaux)
PARK_MIN_HA = 5.0           # grands parcs seulement
PARK_BUFFER_M = 20.0
SIMPLIFY_M = 25.0
BONUS = 1.3


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pbf", required=True)
    ap.add_argument("--workdir", default="data")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    import shapely
    import shapely.affinity
    from shapely.geometry import shape, mapping
    from shapely.ops import unary_union

    wd = Path(a.workdir)
    filt, gj = wd / "scenic.osm.pbf", wd / "scenic.geojsonseq"
    subprocess.run(["osmium", "tags-filter", a.pbf, "w/natural=coastline", "w/waterway=river", "wr/leisure=park",
                    "wr/leisure=nature_reserve", "-o", str(filt), "--overwrite"], check=True, capture_output=True)
    subprocess.run(["osmium", "export", str(filt), "-f", "geojsonseq", "-o", str(gj), "--overwrite"], check=True,
                   capture_output=True)
    k_lat = 1.0 / 110540.0                                  # mètres -> degrés de latitude
    sx = 1.0 / 0.7476                                       # 1 / cos(lat0) : les longitudes sont « plus courtes »
    lines_c, lines_r, parks = [], [], []
    with gj.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip("\x1e\n ")
            if not line:
                continue
            try:
                f = json.loads(line)
                g = shape(f["geometry"])
            except (ValueError, KeyError):
                continue
            p = f.get("properties", {})
            if p.get("natural") == "coastline" and g.geom_type in ("LineString", "MultiLineString"):
                lines_c.append(g)
            elif p.get("waterway") == "river" and g.geom_type in ("LineString", "MultiLineString"):
                lines_r.append(g)
            elif g.geom_type in ("Polygon", "MultiPolygon"):
                ha = g.area * 110540.0 * (110540.0 / sx) / 10000.0   # aire approchée en hectares
                if ha >= PARK_MIN_HA:
                    parks.append(g)

    def buffer_m(geoms, m):
        # mise à l'échelle (x * cos(lat0)) pour un tampon rond en mètres, puis retour
        out = []
        for g in geoms:
            g2 = shapely.affinity.scale(g, xfact=1.0 / sx, yfact=1.0, origin=(0, 0))
            b = g2.buffer(m * k_lat, resolution=4)
            out.append(shapely.affinity.scale(b, xfact=sx, yfact=1.0, origin=(0, 0)))
        return out

    parts = buffer_m(lines_c, COAST_BUFFER_M) + buffer_m(lines_r, RIVER_BUFFER_M) + buffer_m(parks, PARK_BUFFER_M)
    zone = unary_union(parts).simplify(SIMPLIFY_M * k_lat, preserve_topology=True)
    model = {
        "areas": {"type": "FeatureCollection", "features": [
            {"type": "Feature", "id": "scenic", "properties": {}, "geometry": mapping(zone)}]},
        "priority": [
            {"if": "in_scenic && road_class != PRIMARY && road_class != SECONDARY && road_class != TERTIARY "
                   "&& road_class != TRUNK && road_class != MOTORWAY", "multiply_by": str(BONUS)}],
    }
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(model), encoding="utf-8")
    print(f"zones agréables : {len(lines_c)} côtes, {len(lines_r)} rivières, {len(parks)} parcs ; "
          f"{Path(a.out).stat().st_size / 1e6:.1f} Mo")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
