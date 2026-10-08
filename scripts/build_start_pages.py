# -*- coding: utf-8 -*-
"""Pages HTML statiques, une par départ et par langue (référencement, O-44, 08/10/2026).

Lit les données déjà publiées (index.json et starts/<id>.json : rien n'est recalculé) et écrit, pour chaque départ :
  fr/depart/<id>.html · ca/sortida/<id>.html · es/salida/<id>.html · en/start/<id>.html
plus sitemap.xml (pages fixes + pages des départs). Lisibles sans JavaScript, liées entre elles (hreflang) et vers l'app.

  python scripts/build_start_pages.py --data <dossier ou URL de web/data> --out <dossier du site> [--only id1,id2]
"""
import argparse
import html
import json
import math
import urllib.request
from pathlib import Path

BASE = "https://florentbedouret-lgtm.github.io/velo-loops/"
LANGS = ["ca", "es", "fr", "en"]
DIRS = {"fr": "fr/depart", "ca": "ca/sortida", "es": "es/salida", "en": "en/start"}
HOW = {"fr": "comment-ca-marche.html", "ca": "com-funciona.html", "es": "como-funciona.html", "en": "how-it-works.html"}
LEVELS = ["facile", "modere", "soutenu"]
FIXED_PAGES = [("", "1.0"), ("comment-ca-marche.html", "0.6"), ("com-funciona.html", "0.6"), ("como-funciona.html", "0.6"),
               ("how-it-works.html", "0.6"), ("mentions.html", "0.2")]

T = {
 "fr": {"lvl": {"facile": "Tranquille", "modere": "Modéré", "soutenu": "Sportif"}, "station": "Gare", "station_phrase": "la gare {r}",
        "dirs": {"nord": "nord", "sud": "sud", "est": "est", "ouest": "ouest", "nord-est": "nord-est", "nord-ouest": "nord-ouest",
                 "sud-est": "sud-est", "sud-ouest": "sud-ouest"},
        "title": "Boucles vélo au départ de {name} · Oyan",
        "desc": "{n} boucles à vélo depuis {name}, de {dmin} à {dmax} : distance, D+, sortie de ville et feux. Trace GPX gratuite.",
        "h1": "Boucles à vélo au départ de {name}",
        "lead": "Depuis {name}, Oyan propose une boucle par durée, de {dmin} à {dmax}, et par allure. Chaque boucle revient au point de départ ; la trace GPX se télécharge gratuitement dans l'app.",
        "best_nature": "La plus nature : {dur} en {lvl}, {p} en forêt ou au bord de l'eau.",
        "best_exit": "Sortie de ville la plus rapide : {dur} en {lvl}, nature après {km} km.",
        "h2": "Boucle de {dur}", "dplus": "D+ {m} m", "time": "≈ {t}", "forest": "{p} forêt ou eau",
        "exit": "nature après {km} km", "exit0": "nature dès le départ", "stays": "reste en ville",
        "lights": "{l} feux/km", "lights1": "{l} feu/km", "nolights": "aucun feu", "dirt": "{km} km de terre", "see": "Voir et télécharger",
        "near": "Départs voisins", "how": "Comment sont faites les boucles ?", "legal": "Mentions légales & confidentialité",
        "app": "← Ouvrir l'app", "langs": "Langues", "note": "Durées en mouvement, sans pauses. Parcours indicatifs, à vérifier avant de partir.",
        "data": "Données : © contributeurs OpenStreetMap (ODbL), revêtement © ICGC (CC BY 4.0), calculs GraphHopper (Apache 2.0)."},
 "ca": {"lvl": {"facile": "Tranquil", "modere": "Moderat", "soutenu": "Esportiu"}, "station": "Estació", "station_phrase": "l'estació {r}",
        "dirs": {"nord": "nord", "sud": "sud", "est": "est", "ouest": "oest", "nord-est": "nord-est", "nord-ouest": "nord-oest",
                 "sud-est": "sud-est", "sud-ouest": "sud-oest"},
        "title": "Rutes en bici des de {name} · Oyan",
        "desc": "{n} rutes circulars en bici des de {name}, de {dmin} a {dmax}: distància, desnivell, sortida de la ciutat i semàfors. Traça GPX gratuïta.",
        "h1": "Rutes en bici des de {name}",
        "lead": "Des de {name}, Oyan proposa una ruta circular per durada, de {dmin} a {dmax}, i per ritme. Cada ruta torna al punt de sortida; la traça GPX es descarrega gratis a l'app.",
        "best_nature": "La més natural: {dur} a ritme {lvl}, {p} al bosc o vora l'aigua.",
        "best_exit": "Sortida de la ciutat més ràpida: {dur} a ritme {lvl}, natura després de {km} km.",
        "h2": "Ruta de {dur}", "dplus": "D+ {m} m", "time": "≈ {t}", "forest": "{p} bosc o aigua",
        "exit": "natura després de {km} km", "exit0": "natura des de la sortida", "stays": "es queda a la ciutat",
        "lights": "{l} semàfors/km", "nolights": "cap semàfor", "dirt": "{km} km de terra", "see": "Veure i descarregar",
        "near": "Sortides properes", "how": "Com es fan les rutes?", "legal": "Avís legal i privadesa",
        "app": "← Obre l'app", "langs": "Idiomes", "note": "Durades en moviment, sense pauses. Rutes orientatives: comprova-les abans de sortir.",
        "data": "Dades: © col·laboradors d'OpenStreetMap (ODbL), ferm © ICGC (CC BY 4.0), càlcul GraphHopper (Apache 2.0)."},
 "es": {"lvl": {"facile": "Tranquilo", "modere": "Moderado", "soutenu": "Deportivo"}, "station": "Estación", "station_phrase": "la estación {r}",
        "dirs": {"nord": "norte", "sud": "sur", "est": "este", "ouest": "oeste", "nord-est": "noreste", "nord-ouest": "noroeste",
                 "sud-est": "sureste", "sud-ouest": "suroeste"},
        "title": "Rutas en bici desde {name} · Oyan",
        "desc": "{n} rutas circulares en bici desde {name}, de {dmin} a {dmax}: distancia, desnivel, salida de la ciudad y semáforos. Track GPX gratis.",
        "h1": "Rutas en bici desde {name}",
        "lead": "Desde {name}, Oyan propone una ruta circular por duración, de {dmin} a {dmax}, y por ritmo. Cada ruta vuelve al punto de salida; el track GPX se descarga gratis en la app.",
        "best_nature": "La más natural: {dur} a ritmo {lvl}, {p} por bosque o junto al agua.",
        "best_exit": "Salida de la ciudad más rápida: {dur} a ritmo {lvl}, naturaleza tras {km} km.",
        "h2": "Ruta de {dur}", "dplus": "D+ {m} m", "time": "≈ {t}", "forest": "{p} bosque o agua",
        "exit": "naturaleza tras {km} km", "exit0": "naturaleza desde la salida", "stays": "se queda en la ciudad",
        "lights": "{l} semáforos/km", "nolights": "ningún semáforo", "dirt": "{km} km de tierra", "see": "Ver y descargar",
        "near": "Salidas cercanas", "how": "¿Cómo se hacen las rutas?", "legal": "Aviso legal y privacidad",
        "app": "← Abrir la app", "langs": "Idiomas", "note": "Tiempos en movimiento, sin pausas. Rutas orientativas: compruébalas antes de salir.",
        "data": "Datos: © colaboradores de OpenStreetMap (ODbL), firme © ICGC (CC BY 4.0), cálculo GraphHopper (Apache 2.0)."},
 "en": {"lvl": {"facile": "Relaxed", "modere": "Moderate", "soutenu": "Sporty"}, "station": "Station", "station_phrase": "{r} station",
        "dirs": {"nord": "north", "sud": "south", "est": "east", "ouest": "west", "nord-est": "north-east", "nord-ouest": "north-west",
                 "sud-est": "south-east", "sud-ouest": "south-west"},
        "title": "Bike loops from {name} · Oyan",
        "desc": "{n} bike loops from {name}, from {dmin} to {dmax}: distance, climbing, way out of town and traffic lights. Free GPX download.",
        "h1": "Bike loops from {name}",
        "lead": "From {name}, Oyan suggests one loop per duration, from {dmin} to {dmax}, and per pace. Every loop ends where it starts; the GPX file is free to download in the app.",
        "best_nature": "Most nature: {dur} at {lvl} pace, {p} in forest or by the water.",
        "best_exit": "Quickest way out of town: {dur} at {lvl} pace, nature after {km} km.",
        "h2": "{dur} loop", "dplus": "↑ {m} m", "time": "≈ {t}", "forest": "{p} forest or water",
        "exit": "nature after {km} km", "exit0": "nature from the start", "stays": "stays in town",
        "lights": "{l} lights/km", "nolights": "no traffic lights", "dirt": "{km} km unpaved", "see": "See and download",
        "near": "Nearby starts", "how": "How are the loops made?", "legal": "Legal notice & privacy",
        "app": "← Open the app", "langs": "Languages", "note": "Moving times, without breaks. Routes are indicative: check them before you ride.",
        "data": "Data: © OpenStreetMap contributors (ODbL), surface © ICGC (CC BY 4.0), routing by GraphHopper (Apache 2.0)."},
}
RELIEF = {'fr': ['plat', 'peu vallonné', 'vallonné', 'très vallonné'], 'ca': ['pla', 'poc ondulat', 'ondulat', 'molt ondulat'], 'es': ['llano', 'poco ondulado', 'ondulado', 'muy ondulado'], 'en': ['flat', 'gently rolling', 'hilly', 'very hilly']}   # mêmes seuils que relief_category() et l'app (D+ pour 100 km)
e = html.escape


def relief_word(o: dict, lang: str) -> str:
    per100 = 100.0 * o["ascend_m"] / max(o["distance_km"], 0.1)
    return RELIEF[lang][0 if per100 < 500 else 1 if per100 < 1000 else 2 if per100 < 1600 else 3]


def dec(x: float, lang: str, nd: int = 1) -> str:
    s = f"{x:.{nd}f}"
    return s if lang == "en" else s.replace(".", ",")


def pct(p: float, lang: str) -> str:
    return f"{round(p * 100)}%" if lang == "en" else f"{round(p * 100)} %"


def hm(minutes: float) -> str:
    m = round(minutes)
    return f"{m} min" if m < 60 else f"{m // 60} h {m % 60:02d}"


def dur_label(h: float) -> str:
    m = round(h * 60)
    return f"{m} min" if m < 60 else f"{m // 60} h" + (f" {m % 60:02d}" if m % 60 else "")


def start_name(s: dict, lang: str, phrase: bool = False) -> str:
    """Comme startLabel() de l'app : « Commune · lieu », gares « Commune · Gare … », suffixes de direction traduits."""
    name, m = s["name"], s.get("municipality")
    for k, v in sorted(T[lang]["dirs"].items(), key=lambda kv: -len(kv[0])):
        name = name.replace(f"({k})", f"({v})")
    if s.get("kind") == "station":
        rest = name[5:] if name.startswith("Gare ") else name
        if m and rest.lower().startswith(m.lower()):
            rest = rest[len(m):].lstrip(" -–|")
        if phrase and rest:                                  # dans une phrase : « au départ de la gare … »
            return T[lang]["station_phrase"].format(r=rest) + (f" ({m})" if m else "")
        return T[lang]["station"] + (" " + rest if rest else "") + (f" ({m})" if m else "")
    if m and (name == m or m.lower() in name.lower()):
        return name
    return f"{name} ({m})" if m else name


def crow_km(a: dict, b: dict) -> float:
    p1, p2 = math.radians(a["lat"]), math.radians(b["lat"])
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(b["lon"] - a["lon"]) / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def load(src: str, rel: str):
    if src.startswith("http"):
        with urllib.request.urlopen(src.rstrip("/") + "/" + rel, timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))
    return json.loads((Path(src) / rel).read_text(encoding="utf-8"))


def recommended(options: list) -> dict:
    """{(durée en h, allure): boucle recommandée} (identifiant en -1, comme to_json)."""
    out = {}
    for o in options:
        if str(o.get("id", "")).endswith("-1"):
            out[(round(o["duration_target_min"]) / 60.0, o["level"])] = o
    return out


def nature_share(o: dict):
    lc = (o.get("scenery") or {}).get("landcover")
    return None if not lc else lc.get("forest", 0) + lc.get("water", 0)


def loop_line(o: dict, s: dict, lang: str, rel: str) -> str:
    d = T[lang]
    ex, li = o.get("exit_city_km"), o.get("traffic_lights_per_km") or 0
    dirt = (o.get("shares") or {}).get("unpaved", 0) * o["distance_km"]
    facts = [d["exit0"] if ex == 0 else d["exit"].format(km=dec(ex, lang)) if ex is not None else d["stays"]]
    ns = nature_share(o)
    if ns is not None and ns >= 0.05:
        facts.append(d["forest"].format(p=pct(ns, lang)))
    facts.append(d["nolights"] if li < 0.05 else d.get("lights1" if li < 2 else "lights", d["lights"]).format(l=dec(li, lang)))
    if dirt >= 0.5:
        facts.append(d["dirt"].format(km=dec(dirt, lang)))
    q = f"?s={s['id']}&d={round(o['duration_target_min']) / 60:g}&l={o['level']}"
    return (f'<li><span class="lvl">{e(d["lvl"][o["level"]])}</span> '
            f'<span class="fig">{dec(o["distance_km"], lang)} km · {e(d["dplus"].format(m=round(o["ascend_m"])))} ({e(relief_word(o, lang))}) · '
            f'{e(d["time"].format(t=hm(o["time_est_min"])))}</span>'
            f'<span class="facts">{e(" · ".join(facts))}</span>'
            f'<a href="{rel}index.html{e(q)}">{e(d["see"])}</a></li>')


def page(s: dict, data: dict, lang: str, near: list):
    d, rel = T[lang], "../../"
    rec = recommended(data["options"])
    if not rec:
        return None
    name = start_name(s, lang, phrase=True)
    durs = sorted({k[0] for k in rec})
    head = {"name": name, "n": len(rec), "dmin": dur_label(durs[0]), "dmax": dur_label(durs[-1])}
    # deux faits saillants : la plus « nature » et la sortie de ville la plus rapide (en km, à partir de 1 h 30)
    nat = [(nature_share(o), k) for k, o in rec.items() if nature_share(o) is not None]
    exits = [(o["exit_city_km"], k) for k, o in rec.items() if o.get("exit_city_km") is not None and k[0] >= 1.5]
    best = []
    if nat:
        p, k = max(nat)
        if p >= 0.2:
            best.append(d["best_nature"].format(dur=dur_label(k[0]), lvl=d["lvl"][k[1]].lower(), p=pct(p, lang)))
    if exits:
        km, k = min(exits)
        best.append(d["best_exit"].format(dur=dur_label(k[0]), lvl=d["lvl"][k[1]].lower(), km=dec(km, lang)))
    blocks = []
    for h in durs:
        items = [loop_line(rec[(h, lv)], s, lang, rel) for lv in LEVELS if (h, lv) in rec]
        blocks.append(f'  <h2>{e(d["h2"].format(dur=dur_label(h)))}</h2>\n  <ul class="loops">{"".join(items)}</ul>')
    alts = "\n".join(f'  <link rel="alternate" hreflang="{x}" href="{BASE}{DIRS[x]}/{s["id"]}.html">' for x in LANGS) + \
        f'\n  <link rel="alternate" hreflang="x-default" href="{BASE}{DIRS["fr"]}/{s["id"]}.html">'
    langs = " ".join(f'<a href="{rel}{DIRS[x]}/{s["id"]}.html" hreflang="{x}" lang="{x}"' +
                     (' aria-current="page"' if x == lang else "") + f'>{x.upper()}</a>' for x in LANGS)
    nears = "".join(f'<li><a href="{n["id"]}.html">{e(start_name(n, lang))}</a></li>' for n in near)
    ld = {"@context": "https://schema.org", "@type": "Place", "name": name,
          "geo": {"@type": "GeoCoordinates", "latitude": s["lat"], "longitude": s["lon"]},
          **({"containedInPlace": {"@type": "Place", "name": s["municipality"]}} if s.get("municipality") else {}),
          "url": f'{BASE}{DIRS[lang]}/{s["id"]}.html'}
    title = d["title"].format(**head)
    ld_json = json.dumps(ld, ensure_ascii=False).replace("<", "\\u003c")   # un nom OSM ne sort pas du script
    return f'''<!DOCTYPE html>
<html lang="{lang}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{e(title)}</title>
  <meta name="description" content="{e(d["desc"].format(**head))}">
  <link rel="canonical" href="{BASE}{DIRS[lang]}/{s["id"]}.html">
{alts}
  <meta property="og:type" content="website">
  <meta property="og:site_name" content="Oyan">
  <meta property="og:title" content="{e(title)}">
  <meta property="og:description" content="{e(d["desc"].format(**head))}">
  <meta property="og:image" content="{BASE}brand/png/oyan-icone-512.png">
  <meta name="twitter:card" content="summary">
  <meta name="theme-color" content="#F6F4F0">
  <link rel="icon" href="{rel}brand/svg/favicon.svg" type="image/svg+xml">
  <link href="{rel}brand/oyan-tokens.css" rel="stylesheet">
  <link href="{rel}fonts/oyan-fonts.css" rel="stylesheet">
  <script type="application/ld+json">{ld_json}</script>
  <style>
    body {{ max-width: 640px; margin: 0 auto; padding: 16px; font-family: var(--font-sans); line-height: 1.5;
           background: var(--color-bg); color: var(--color-text); -webkit-text-size-adjust: 100%; }}
    h1 {{ font-size: var(--fs-h1); font-weight: var(--fw-regular); margin: 8px 0 12px; text-wrap: balance; }}
    h2 {{ font-size: var(--fs-h2); font-weight: var(--fw-medium); margin: 28px 0 4px; }}
    p {{ margin: 0 0 8px; text-wrap: pretty; }}
    a {{ color: var(--color-text); text-underline-offset: 3px; }}
    :focus-visible {{ outline: 2px solid var(--color-text); outline-offset: 2px; }}
    .top {{ display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; min-height: 44px; }}
    .top nav a {{ font-size: 13px; font-weight: var(--fw-medium); margin-left: 12px; color: var(--color-text-2); }}
    .top nav a[aria-current] {{ color: var(--color-text); text-decoration: none; }}
    .best {{ color: var(--color-text-2); }}
    ul {{ list-style: none; margin: 0; padding: 0; }}
    .loops li {{ display: flex; flex-direction: column; gap: 2px; padding: 12px 0; border-bottom: 1px solid var(--color-border); }}
    .loops .lvl {{ font-weight: var(--fw-medium); }}
    .loops .fig {{ font-variant-numeric: tabular-nums; }}
    .loops .facts {{ font-size: 14px; color: var(--color-text-2); }}
    .loops a {{ align-self: flex-start; display: inline-flex; align-items: center; min-height: 36px; font-size: 14px; color: var(--color-accent); }}
    .near {{ display: flex; flex-wrap: wrap; gap: 8px 16px; }}
    .near a {{ display: inline-block; padding: 6px 0; }}
    footer {{ margin-top: 32px; padding-top: 12px; border-top: 1px solid var(--color-border); font-size: 13px; color: var(--color-text-2); }}
    footer a {{ color: var(--color-text-2); }}
  </style>
</head>
<body>
  <div class="top"><a href="{rel}index.html{e("?s=" + s["id"])}">{e(d["app"])}</a>
    <nav aria-label="{e(d["langs"])}">{langs}</nav></div>
  <main>
  <h1>{e(d["h1"].format(**head))}</h1>
  <p>{e(d["lead"].format(**head))}</p>
{"".join(f'  <p class="best">{e(b)}</p>' + chr(10) for b in best)}{chr(10).join(blocks)}
  <h2>{e(d["near"])}</h2>
  <ul class="near">{nears}</ul>
  </main>
  <footer>
    <p>{e(d["note"])}</p>
    <p>{e(d["data"])}</p>
    <p><a href="{rel}{HOW[lang]}">{e(d["how"])}</a> · <a href="{rel}mentions.html">{e(d["legal"])}</a></p>
  </footer>
</body>
</html>
'''


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, help="dossier web/data ou son URL")
    ap.add_argument("--out", required=True, help="dossier du site (les pages y sont écrites)")
    ap.add_argument("--only", default="", help="identifiants de départs séparés par des virgules (essai)")
    ap.add_argument("--near", type=int, default=6, help="départs voisins listés")
    args = ap.parse_args()
    idx = load(args.data, "index.json")
    starts = [s for s in idx["starts"] if not args.only or s["id"] in args.only.split(",")]
    out, done, lastmod = Path(args.out), [], {}
    for s in starts:
        try:
            data = load(args.data, f"starts/{s['id']}.json")
        except Exception as ex:  # noqa: BLE001 : un départ manquant ne bloque pas les autres
            print(f"  {s['id']} : données absentes ({type(ex).__name__})")
            continue
        near = sorted((n for n in idx["starts"] if n["id"] != s["id"]), key=lambda n: crow_km(s, n))[:args.near]
        wrote = False
        for lang in LANGS:
            html_ = page(s, data, lang, near)
            if html_ is None:
                continue
            p = out / DIRS[lang] / f"{s['id']}.html"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(html_, encoding="utf-8")
            wrote = True
        if wrote:
            done.append(s["id"])
            lastmod[s["id"]] = (s.get("computed_at") or idx.get("generated_at") or "")[:10]
    # sitemap : pages fixes + pages des départs (avec leurs équivalents dans les autres langues)
    today = (idx.get("generated_at") or "")[:10]
    rows = [f"  <url><loc>{BASE}{p}</loc>{f'<lastmod>{today}</lastmod>' if today else ''}<priority>{pr}</priority></url>"
            for p, pr in FIXED_PAGES]
    for sid in done:
        alt = "".join(f'<xhtml:link rel="alternate" hreflang="{x}" href="{BASE}{DIRS[x]}/{sid}.html"/>' for x in LANGS)
        for lang in LANGS:
            lm = f"<lastmod>{lastmod[sid]}</lastmod>" if lastmod[sid] else ""
            rows.append(f"  <url><loc>{BASE}{DIRS[lang]}/{sid}.html</loc>{lm}<priority>0.5</priority>{alt}</url>")
    (out / "sitemap.xml").write_text('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
                                     'xmlns:xhtml="http://www.w3.org/1999/xhtml">\n' + "\n".join(rows) + "\n</urlset>\n",
                                     encoding="utf-8")
    print(f"{len(done)} départs, {len(done) * len(LANGS)} pages, sitemap de {len(rows)} adresses")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
