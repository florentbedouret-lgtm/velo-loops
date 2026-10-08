# -*- coding: utf-8 -*-
"""Liaisons de train directes vers les départs « gare » d'Oyan (O-42, 08/10/2026).

Lit les horaires ouverts (GTFS) de Rodalies (Renfe, CC BY 4.0) et de FGC, et écrit web/data/train_links.json : pour
chaque gare de la zone, les départs « gare » d'Oyan joignables SANS correspondance un samedi matin (départ entre 7 h et
12 h), avec le temps de train le plus court et le nombre de trains par heure. L'app choisit elle-même la gare proche de
l'adresse de l'utilisateur (la position ne quitte pas le téléphone). Aucune boucle n'est recalculée.

  python scripts/build_train_links.py --index _site/web/data/index.json --out _site/web/data/train_links.json
"""
import argparse
import collections
import csv
import datetime
import io
import json
import math
import urllib.request
import zipfile

FEEDS = {"renfe": "https://ssl.renfe.com/ftransit/Fichero_CER_FOMENTO/fomento_transit.zip",
         "fgc": "https://www.fgc.cat/google/google_transit.zip"}
DEST_M = 400          # un départ « gare » d'Oyan correspond à un arrêt à moins de 400 m
WINDOW = (7 * 3600, 12 * 3600)   # départs du samedi matin
MIN_PER_HOUR = 0.5    # au moins un train toutes les deux heures dans la fenêtre


def hav_m(lon1, lat1, lon2, lat2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6371000.0 * math.asin(math.sqrt(h))


def rows(z, name):
    """Lignes d'un fichier GTFS, clés et valeurs sans espaces (le fichier Renfe complète ses lignes avec des blancs)."""
    for r in csv.DictReader(io.TextIOWrapper(z.open(name), encoding="utf-8-sig")):
        yield {k.strip(): (v or "").strip() for k, v in r.items() if k}


def tsec(t):
    h, m, s = (int(x) for x in t.split(":"))
    return h * 3600 + m * 60 + s


def next_saturday(z):
    """Prochain samedi couvert par le calendrier du flux (à partir d'aujourd'hui)."""
    d = datetime.date.today()
    return d + datetime.timedelta(days=(5 - d.weekday()) % 7)


def active_services(z, day):
    ds, act = day.strftime("%Y%m%d"), set()
    if "calendar.txt" in z.namelist():
        for r in rows(z, "calendar.txt"):
            if r.get("saturday") == "1" and r["start_date"] <= ds <= r["end_date"]:
                act.add(r["service_id"])
    if "calendar_dates.txt" in z.namelist():
        for r in rows(z, "calendar_dates.txt"):
            if r["date"] == ds:
                (act.add if r["exception_type"] == "1" else act.discard)(r["service_id"])
    return act


def feed_links(op, z, bbox, dest_starts, log):
    stops = {}
    for r in rows(z, "stops.txt"):
        if r.get("location_type") not in ("", "0", None):
            continue
        lon, lat = float(r["stop_lon"]), float(r["stop_lat"])
        if bbox[0] <= lon <= bbox[2] and bbox[1] <= lat <= bbox[3]:
            stops[r["stop_id"]] = (r["stop_name"], lon, lat)
    dest = {}
    for sid, (_, lon, lat) in stops.items():
        best = min(((hav_m(lon, lat, s["lon"], s["lat"]), s["id"]) for s in dest_starts), default=None)
        if best and best[0] <= DEST_M:
            dest[sid] = best[1]
    day = next_saturday(z)
    act = active_services(z, day)
    trips = {r["trip_id"] for r in rows(z, "trips.txt") if r["service_id"] in act}
    by_trip = collections.defaultdict(list)
    for r in rows(z, "stop_times.txt"):
        if r["trip_id"] in trips and r["stop_id"] in stops:
            t = r["departure_time"] or r["arrival_time"]
            if t:
                by_trip[r["trip_id"]].append((int(r["stop_sequence"]), r["stop_id"], tsec(t)))
    pairs = collections.defaultdict(list)          # (gare d'origine, départ Oyan) -> [(heure de départ, durée)]
    for seq in by_trip.values():
        seq.sort()
        for i, (_, a, ta) in enumerate(seq):
            if not WINDOW[0] <= ta <= WINDOW[1]:
                continue
            for _, b, tb in seq[i + 1:]:
                if b in dest and tb > ta:
                    pairs[(stops[a][0], dest[b])].append((ta, tb - ta))
    # une gare = un nom par opérateur (plusieurs quais possibles) ; position moyenne
    pos = collections.defaultdict(list)
    for name, lon, lat in stops.values():
        pos[name].append((lon, lat))
    out = collections.defaultdict(list)
    hours = (WINDOW[1] - WINDOW[0]) / 3600.0
    for (name, sid), v in pairs.items():
        per_h = len({t for t, _ in v}) / hours
        if per_h < MIN_PER_HOUR:
            continue
        out[name].append([sid, round(min(d for _, d in v) / 60), round(per_h, 1)])
    log(f"{op} : samedi {day}, {len(trips)} trains, {len(dest)} arrêts près d'un départ Oyan, {len(out)} gares avec liaison")
    res = []
    for name, links in out.items():
        lon = sum(p[0] for p in pos[name]) / len(pos[name])
        lat = sum(p[1] for p in pos[name]) / len(pos[name])
        res.append({"n": name, "op": op, "lon": round(lon, 5), "lat": round(lat, 5),
                    "to": sorted(links, key=lambda x: x[1])})
    return res, day


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", required=True, help="index.json publié (départs « gare » et zone couverte)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--feed", action="append", default=[], help="op=chemin.zip (essai local sans téléchargement)")
    args = ap.parse_args()
    idx = json.load(open(args.index, encoding="utf-8"))
    bbox = idx.get("coverage_bbox") or [1.30, 41.20, 2.75, 42.35]
    dest_starts = [s for s in idx["starts"] if s.get("kind") == "station"]
    local = dict(x.split("=", 1) for x in args.feed)
    stations, days = [], {}
    for op, url in FEEDS.items():
        try:
            if op in local:
                z = zipfile.ZipFile(local[op])
            else:
                with urllib.request.urlopen(url, timeout=120) as r:
                    z = zipfile.ZipFile(io.BytesIO(r.read()))
            res, day = feed_links(op, z, bbox, dest_starts, print)
            stations += res
            days[op] = str(day)
        except Exception as ex:  # noqa: BLE001 : un flux indisponible ne bloque pas la publication du site
            print(f"{op} : horaires indisponibles ({type(ex).__name__}: {ex})")
    data = {"generated": datetime.date.today().isoformat(), "saturday": days, "window_h": [7, 12],
            "attribution": "Horaires : Renfe (CC BY 4.0), FGC (dades obertes)", "stations": stations}
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    print(f"{len(stations)} gares, {sum(len(s['to']) for s in stations)} liaisons directes -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
