#!/usr/bin/env python3
"""
Rapport de fin de génération : les critères de réussite de l'UX / produit réunis dans un fichier lisible.

Critères : départs générés / ignorés (et pourquoi) ; durée réelle du job ; temps par zone × durée ; tailles
(index.json, site publié, fichiers de départ) ; combinaisons durée × niveau absentes. Chaque critère porte son seuil et un
verdict OK / DÉPASSÉ. Écrit un fichier .md (aussi ajouté au résumé de l'exécution GitHub) et un fichier .json.
"""
from __future__ import annotations

import argparse
import json
import statistics as st
import sys
from pathlib import Path

LEVELS = ("facile", "modere", "soutenu")
THRESHOLDS = {"index_mb": 1.2, "site_mb": 150.0, "missing_pct": 3.0, "job_hours": 5.8}


def sd(v):
    return st.stdev(v) if len(v) > 1 else 0.0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", default="web/data")
    ap.add_argument("--timings", default="data/timings.json")
    ap.add_argument("--skipped", default="data/skipped.json")
    ap.add_argument("--plan", default="data/starts_plan.json", help="plan de départs (nombre attendu)")
    ap.add_argument("--site", default=None, help="dossier du site assemblé (taille publiée)")
    ap.add_argument("--job-seconds", type=float, default=None, help="durée écoulée depuis le début du job")
    ap.add_argument("--out-md", required=True)
    ap.add_argument("--out-json", required=True)
    args = ap.parse_args()

    data = Path(args.data)
    index_path = data / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    starts = index["starts"]
    stats = index.get("stats", {})
    durations = [f"{d:g}" for d in index.get("durations_h", [])]
    levels = [lv for lv in LEVELS if lv in index.get("levels", {})] or list(LEVELS)
    plan_n = None
    if Path(args.plan).exists():
        plan_n = len(json.loads(Path(args.plan).read_text(encoding="utf-8")))
    skipped = json.loads(Path(args.skipped).read_text(encoding="utf-8")) if Path(args.skipped).exists() else []
    timings = json.loads(Path(args.timings).read_text(encoding="utf-8")) if Path(args.timings).exists() else []

    # ---- 1. départs
    by_reason: dict = {}
    for s in skipped:
        by_reason[s["reason"]] = by_reason.get(s["reason"], 0) + 1
    zones = {z: sum(1 for s in starts if s.get("zone") == z) for z in ("dense", "peri", "rural")}
    departs = {"plan": plan_n, "requested": stats.get("starts_requested"), "generated": len(starts),
               "reused": stats.get("starts_reused", 0), "skipped": len(skipped), "skipped_by_reason": by_reason,
               "skipped_list": skipped[:60], "generated_by_zone": zones, "incomplete": bool(stats.get("incomplete"))}

    # ---- 2. durée et temps par zone × durée (départs calculés, hors réutilisés)
    computed = [t for t in timings if t.get("seconds") is not None and not t.get("reused")]
    gen_s = stats.get("generation_seconds")
    total_s = sum(t["seconds"] for t in computed)
    duree = {"job_seconds": args.job_seconds, "generation_seconds": gen_s, "workers": stats.get("workers"),
             "starts_computed": len(computed), "sum_of_start_seconds": round(total_s),
             "average_concurrency": round(total_s / gen_s, 2) if gen_s else None,
             "throughput_seconds_per_start": round(gen_s / max(1, len(computed)), 1) if gen_s else None}
    by_zone: dict = {}
    for z in ("dense", "peri", "rural"):
        sel = [t for t in computed if t.get("zone") == z]
        if not sel:
            continue
        v = [t["seconds"] for t in sel]
        row = {"starts": len(sel), "mean_s": round(st.mean(v), 1), "sd_s": round(sd(v), 1), "max_s": round(max(v), 1), "by_duration": {}}
        for d in durations:
            w = [t["seconds_by_duration"][d] for t in sel if d in (t.get("seconds_by_duration") or {})]
            if w:
                row["by_duration"][d] = {"mean_s": round(st.mean(w), 1), "sd_s": round(sd(w), 1), "max_s": round(max(w), 1)}
        by_zone[z] = row

    # ---- 3. tailles
    idx_kb = index_path.stat().st_size / 1024
    files = list((data / "starts").glob("*.json"))
    sizes = [f.stat().st_size / 1024 for f in files]
    site_mb = None
    if args.site and Path(args.site).exists():
        site_mb = sum(f.stat().st_size for f in Path(args.site).rglob("*") if f.is_file()) / 1024 / 1024
    tailles = {"index_kb": round(idx_kb, 1), "index_mb": round(idx_kb / 1024, 3),
               "index_bytes_per_start": round(index_path.stat().st_size / max(1, len(starts))),
               "start_files": len(files), "start_file_mean_kb": round(st.mean(sizes), 1) if sizes else None,
               "start_file_min_kb": round(min(sizes), 1) if sizes else None, "start_file_max_kb": round(max(sizes), 1) if sizes else None,
               "site_mb": round(site_mb, 1) if site_mb is not None else None}

    # ---- 4. combinaisons durée × niveau absentes
    expected = len(starts) * len(durations) * len(levels)
    missing, per_start, per_combo = 0, {}, {}
    for s in starts:
        comp = s.get("compare", {})
        for d in durations:
            for lv in levels:
                if d not in comp or lv not in comp[d]:
                    missing += 1
                    per_start[s["name"]] = per_start.get(s["name"], 0) + 1
                    per_combo[f"{d} h / {lv}"] = per_combo.get(f"{d} h / {lv}", 0) + 1
    miss_pct = 100.0 * missing / expected if expected else 0.0
    absentes = {"expected": expected, "missing": missing, "missing_pct": round(miss_pct, 2),
                "by_combo": dict(sorted(per_combo.items(), key=lambda x: -x[1])),
                "starts_most_affected": dict(sorted(per_start.items(), key=lambda x: -x[1])[:15])}

    # ---- verdicts
    def verdict(ok):
        return "OK" if ok else "DÉPASSÉ"
    v = {"departs_generes_egal_plan": ("non mesuré" if plan_n is None else verdict(len(starts) == plan_n and not skipped)),
         "index_sous_1_2_mo": verdict(idx_kb / 1024 <= THRESHOLDS["index_mb"]),
         "site_sous_150_mo": ("non mesuré" if site_mb is None else verdict(site_mb <= THRESHOLDS["site_mb"])),
         "absentes_sous_3_pct": verdict(miss_pct <= THRESHOLDS["missing_pct"]),
         "job_sous_5_8_h": ("non mesuré" if args.job_seconds is None else verdict(args.job_seconds / 3600 <= THRESHOLDS["job_hours"]))}
    report = {"generated_at": index.get("generated_at"), "params_hash": index.get("params_hash"), "thresholds": THRESHOLDS,
              "verdicts": v, "departs": departs, "duree": duree, "temps_par_zone": by_zone, "tailles": tailles,
              "combinaisons_absentes": absentes}
    Path(args.out_json).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    # ---- version lisible
    L = []
    L.append("# Rapport de génération")
    L.append(f"Généré le {index.get('generated_at')} · empreinte des paramètres {index.get('params_hash')}"
             + (" · **GÉNÉRATION INCOMPLÈTE**" if departs["incomplete"] else ""))
    L.append("\n## Verdict")
    L.append("| Critère | Seuil | Mesuré | Verdict |\n|---|---|---|---|")
    L.append(f"| Départs générés = plan | {plan_n} | {len(starts)} (ignorés : {len(skipped)}) | {v['departs_generes_egal_plan']} |")
    L.append(f"| `index.json` | 1,2 Mo | {idx_kb / 1024:.3f} Mo ({idx_kb:.0f} Ko) | {v['index_sous_1_2_mo']} |")
    L.append(f"| Site publié | 150 Mo | {'—' if site_mb is None else f'{site_mb:.1f} Mo'} | {v['site_sous_150_mo']} |")
    L.append(f"| Combinaisons durée × niveau absentes | 3 % | {miss_pct:.2f} % ({missing} sur {expected}) | {v['absentes_sous_3_pct']} |")
    jh = "—" if args.job_seconds is None else f"{args.job_seconds / 3600:.2f} h"
    L.append(f"| Durée du job (jusqu'à ce rapport) | 5,8 h | {jh} | {v['job_sous_5_8_h']} |")
    L.append("\n## 1. Départs")
    L.append(f"- Plan : {plan_n} · demandés : {departs['requested']} · **générés : {len(starts)}** (dont réutilisés : {departs['reused']}) · **ignorés : {len(skipped)}**")
    L.append(f"- Générés par zone : {zones}")
    if by_reason:
        L.append(f"- Ignorés par motif : {by_reason}")
        for s in skipped[:40]:
            L.append(f"  - {s['name']} ({s.get('zone')}, {s.get('kind')}) : {s['reason']}")
    L.append("\n## 2. Durée et temps de calcul")
    L.append(f"- Durée du calcul des boucles : {gen_s} s ({'—' if not gen_s else f'{gen_s / 3600:.2f} h'}), {stats.get('workers')} départs en parallèle, "
             f"débit {duree['throughput_seconds_per_start']} s par départ, {duree['average_concurrency']} départs en cours en moyenne")
    L.append(f"- Départs calculés (hors réutilisés) : {len(computed)} · somme des temps de départ : {round(total_s)} s")
    L.append("\n| Zone | Départs | Total par départ (moy ; écart-type ; max, en s) | " + " | ".join(f"{d} h" for d in durations) + " |")
    L.append("|---|---|---|" + "---|" * len(durations))
    for z, r in by_zone.items():
        cells = [f"{r['by_duration'][d]['mean_s']} ; {r['by_duration'][d]['sd_s']} ; {r['by_duration'][d]['max_s']}" if d in r["by_duration"] else "—" for d in durations]
        L.append(f"| {z} | {r['starts']} | {r['mean_s']} ; {r['sd_s']} ; {r['max_s']} | " + " | ".join(cells) + " |")
    L.append("\n## 3. Tailles")
    L.append(f"- `index.json` : {idx_kb:.0f} Ko ({tailles['index_bytes_per_start']} octets par départ)")
    L.append(f"- Fichiers de départ : {len(files)} · moyenne {tailles['start_file_mean_kb']} Ko · min {tailles['start_file_min_kb']} Ko · max {tailles['start_file_max_kb']} Ko")
    L.append(f"- Site publié : {'—' if site_mb is None else f'{site_mb:.1f} Mo'}")
    L.append("\n## 4. Combinaisons durée × niveau absentes")
    L.append(f"- **{missing} sur {expected} ({miss_pct:.2f} %)** ; pilote : 1,4 %")
    if per_combo:
        L.append(f"- Par combinaison : {absentes['by_combo']}")
        L.append(f"- Départs les plus touchés : {absentes['starts_most_affected']}")
    Path(args.out_md).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))
    return 0


if __name__ == "__main__":
    sys.exit(main())
