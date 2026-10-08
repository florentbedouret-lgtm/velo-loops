#!/usr/bin/env python3
"""Diagnostic des revêtements « compactés » (Florent, 08/10/2026 : la piste au bord de la plage du Bogatell, 874 m,
surface=compacted, smoothness=good, est évitée comme un chemin de terre ; voir UNPAVED dans generate_loops.py et
config/custom_models/road_surface.json).

Mesure, sur la carte de la région, les voies utilisables à vélo notées surface=compacted : kilomètres par état du
revêtement (smoothness), par type de voie et par zone (celle du départ publié le plus proche), puis les plus longues par
nom, pour que Florent juge sur des lieux qu'il connaît. Rien n'est modifié.

    python scripts/surface_check.py --pbf data/region.osm.pbf --index <site>/web/data/index.json --out-md data/surface_check.md
"""
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path

import requests

NOT_BIKE = {"motorway", "motorway_link", "trunk", "trunk_link", "steps", "construction", "proposed", "abandoned",
            "platform", "corridor", "elevator", "bus_stop", "raceway"}
SMOOTH_ORDER = ["excellent", "good", "intermediate", "bad", "very_bad", "horrible", "(non noté)", "(autre)"]


def kind_of(hw: str) -> str:
    if hw == "cycleway":
        return "piste cyclable"
    if hw in ("footway", "path", "pedestrian", "bridleway"):
        return "chemin / promenade"
    if hw == "track":
        return "piste agricole ou forestière"
    return "rue ou route"


def bike_ok(t) -> bool:
    hw = t.get("highway")
    if not hw or hw in NOT_BIKE or t.get("bicycle") in ("no", "dismount", "private") or t.get("access") in ("no", "private"):
        return False
    if hw in ("footway", "pedestrian"):                       # trottoirs et places : seulement si le vélo y est permis
        return t.get("bicycle") in ("yes", "designated", "permissive")
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pbf", required=True)
    ap.add_argument("--index", required=True, help="index.json publié (zones des départs)")
    ap.add_argument("--out-md", default="data/surface_check.md")
    ap.add_argument("--out", default="data/surface_check.json")
    args = ap.parse_args()
    import osmium

    starts = requests.get(args.index, timeout=60).json()["starts"]
    grid = defaultdict(list)                                  # départs par case de ~11 km
    for s in starts:
        grid[(round(s["lon"] * 10), round(s["lat"] * 10))].append(s)

    def zone(lon, lat):
        gx, gy = round(lon * 10), round(lat * 10)
        near = [s for dx in (-1, 0, 1) for dy in (-1, 0, 1) for s in grid.get((gx + dx, gy + dy), [])]
        if not near:
            return "(loin des départs)"
        return min(near, key=lambda s: (s["lon"] - lon) ** 2 + (s["lat"] - lat) ** 2).get("zone") or "?"

    km = defaultdict(float)                                   # (smoothness, type, zone) -> km
    by_name = defaultdict(lambda: {"km": 0.0, "ways": 0, "smooth": defaultdict(float), "at": None, "zone": None})
    all_surf = defaultdict(float)                             # compacted : notée ou non, pour situer la part

    class H(osmium.SimpleHandler):
        def way(self, w):
            t = w.tags
            if t.get("surface") != "compacted" or not bike_ok(t):
                return
            try:
                pts = [(n.lon, n.lat) for n in w.nodes]
            except osmium.InvalidLocationError:
                return
            m = sum(2 * 6371000 * math.asin(math.sqrt(
                math.sin(math.radians(b[1] - a[1]) / 2) ** 2 + math.cos(math.radians(a[1])) * math.cos(math.radians(b[1]))
                * math.sin(math.radians(b[0] - a[0]) / 2) ** 2)) for a, b in zip(pts, pts[1:]))
            mid = pts[len(pts) // 2]
            sm = t.get("smoothness") or "(non noté)"
            if sm not in SMOOTH_ORDER:
                sm = "(autre)"
            z = zone(*mid)
            k = kind_of(t.get("highway"))
            km[(sm, k, z)] += m / 1000.0
            all_surf[sm] += m / 1000.0
            name = t.get("name") or f"(sans nom) {k}"
            key = (name, z) if t.get("name") else (name, f"{mid[1]:.2f},{mid[0]:.2f}")
            e = by_name[key]
            e["km"] += m / 1000.0
            e["ways"] += 1
            e["smooth"][sm] += m / 1000.0
            if e["at"] is None or m > e.get("_best", 0):
                e["at"], e["_best"], e["zone"], e["kind"] = [round(mid[1], 5), round(mid[0], 5)], m, z, k

    H().apply_file(args.pbf, locations=True)

    tot = sum(km.values())
    L = [f"# Voies cyclables notées « compacted » ({tot:.0f} km)", "",
         "Aujourd'hui toutes traitées comme de la terre : GraphHopper ×0,1 (road_surface.json) et comptées « terre » "
         "dans la note (UNPAVED).", "",
         "## Par état du revêtement (smoothness)", "", "| état | km | part |", "|---|---|---|"]
    for sm in SMOOTH_ORDER:
        if all_surf.get(sm):
            L.append(f"| {sm} | {all_surf[sm]:.0f} | {all_surf[sm] / tot:.0%} |")
    zones = sorted({z for _, _, z in km})
    kinds = sorted({k for _, k, _ in km})
    L += ["", "## Par type de voie et par zone (km ; entre parenthèses : dont état bon ou excellent)", "",
          "| type | " + " | ".join(zones) + " |", "|---" * (len(zones) + 1) + "|"]
    for k in kinds:
        cells = []
        for z in zones:
            a = sum(v for (sm, kk, zz), v in km.items() if kk == k and zz == z)
            g = sum(v for (sm, kk, zz), v in km.items() if kk == k and zz == z and sm in ("excellent", "good"))
            cells.append(f"{a:.0f} ({g:.0f})")
        L.append(f"| {k} | " + " | ".join(cells) + " |")
    top = sorted(by_name.items(), key=lambda kv: -kv[1]["km"])[:60]
    L += ["", "## Les 60 plus longues (même nom, même zone)", "",
          "| nom | type | zone | km | état (km) | lat,lon |", "|---|---|---|---|---|---|"]
    for (name, _), e in top:
        st = ", ".join(f"{s} {v:.1f}" for s, v in sorted(e["smooth"].items(), key=lambda x: -x[1]))
        L.append(f"| {name} | {e['kind']} | {e['zone']} | {e['km']:.1f} | {st} | {e['at'][0]},{e['at'][1]} |")
    Path(args.out_md).write_text("\n".join(L) + "\n", encoding="utf-8")
    Path(args.out).write_text(json.dumps({
        "km": [[sm, k, z, round(v, 2)] for (sm, k, z), v in sorted(km.items())],
        "top": [{"name": n, "km": round(e["km"], 2), "zone": e["zone"], "kind": e["kind"], "at": e["at"],
                 "smooth": {s: round(v, 2) for s, v in e["smooth"].items()}} for (n, _), e in top]},
        ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n".join(L))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
