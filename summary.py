#!/usr/bin/env python3
"""Écrit ETAT.md : photo lisible de la météo locale, passé et prévu.

Volontairement neutre : ce fichier décrit le temps qu'il a fait et qu'il va
faire, sans interprétation métier.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent
DATA = ROOT / "data"


def load(name: str) -> dict:
    path = DATA / name
    if not path.exists():
        sys.exit(f"{path} absent — lancer d'abord consolidate.py")
    return json.loads(path.read_text(encoding="utf-8"))


def window_sum(days: dict[str, dict], end: dt.date, n: int, field: str) -> float | None:
    total, found = 0.0, 0
    for i in range(n):
        entry = days.get((end - dt.timedelta(days=i)).isoformat())
        if entry and field in entry:
            total += entry[field]
            found += 1
    return round(total, 1) if found else None


def sparkline(values: list[float | None], vmax: float) -> str:
    blocks = " ▁▂▃▄▅▆▇█"
    out = []
    for v in values:
        if v is None:
            out.append("·")
        elif v <= 0:
            out.append(blocks[0])
        else:
            idx = min(len(blocks) - 1, 1 + int(v / vmax * (len(blocks) - 2)))
            out.append(blocks[idx])
    return "".join(out)


def main() -> int:
    daily = load("daily.json")
    fcst = load("forecast_daily.json")

    today = dt.date.today()
    lines: list[str] = []
    add = lines.append

    add("# État météo — bassin de Monistrol-sur-Loire")
    add("")
    add(f"Généré le {dt.datetime.now():%Y-%m-%d %H:%M} · "
        f"prévisions du run `{fcst.get('run_ts')}`")
    add("")
    add("Journées météo calées 06h–06h UTC, comme les cumuls Météo-France.")
    add("")

    # ---------------------------------------------------------------- stations
    add("## Observations — stations Météo-France")
    add("")
    add("| Station | alt | dist | 24 h | 7 j | 30 j | 90 j | Tmin | Tmax | 30 derniers jours |")
    add("|---|--:|--:|--:|--:|--:|--:|--:|--:|---|")

    stations = {k: v for k, v in daily["sources"].items() if v["kind"] == "station"}
    for src, meta in sorted(stations.items(), key=lambda kv: kv[1]["dist_km"]):
        days = daily["daily"].get(src, {})
        if not days:
            continue
        last = max(days)
        last_date = dt.date.fromisoformat(last)
        recent = days[last]
        spark = sparkline(
            [(days.get((last_date - dt.timedelta(days=29 - i)).isoformat()) or {}).get("rr")
             for i in range(30)], vmax=20)
        add("| {n} | {a} m | {d} km | {h24} | {h7} | {h30} | {h90} | {tn} | {tx} | `{s}` |".format(
            n=meta["name"].title(), a=meta["alt"], d=round(meta["dist_km"]),
            h24=window_sum(days, last_date, 1, "rr"),
            h7=window_sum(days, last_date, 7, "rr"),
            h30=window_sum(days, last_date, 30, "rr"),
            h90=window_sum(days, last_date, 90, "rr"),
            tn=recent.get("tmin", "–"), tx=recent.get("tmax", "–"), s=spark))
    add("")
    add("Cumuls en mm, arrêtés à la dernière journée complète de chaque station.")
    add("")

    # ------------------------------------------------------------------ points
    add("## Analyse par point — AROME France HD (1,3 km)")
    add("")
    add("| Point | alt | 7 j | 30 j | 90 j | ETP 30 j | Bilan 30 j | Humidité sol |")
    add("|---|--:|--:|--:|--:|--:|--:|--:|")
    points = {k: v for k, v in daily["sources"].items() if v["kind"] == "point"}
    for src, meta in sorted(points.items(), key=lambda kv: -kv[1]["alt"]):
        days = daily["daily"].get(src, {})
        if not days:
            continue
        last_date = dt.date.fromisoformat(max(days))
        p30 = window_sum(days, last_date, 30, "rr")
        e30 = window_sum(days, last_date, 30, "et0")
        bilan = round(p30 - e30, 1) if (p30 is not None and e30 is not None) else None
        sm = days[max(days)].get("sm")
        add("| {n} | {a} m | {p7} | {p30} | {p90} | {e} | {b} | {sm} |".format(
            n=meta["name"], a=meta["alt"],
            p7=window_sum(days, last_date, 7, "rr"), p30=p30,
            p90=window_sum(days, last_date, 90, "rr"),
            e=e30, b=bilan, sm=sm if sm is not None else "–"))
    add("")
    add("Bilan = pluie − évapotranspiration de référence, en mm. "
        "Humidité du sol en m³/m³ dans la couche 3–9 cm.")
    add("")

    # --------------------------------------------------------------- prévision
    add("## Prévision — 10 prochains jours")
    add("")
    horizon = [(today + dt.timedelta(days=i)).isoformat() for i in range(10)]
    header = " | ".join(d[8:10] + "/" + d[5:7] for d in horizon)
    add(f"| Point | {header} |")
    add("|---" * (len(horizon) + 1) + "|")
    for pid, days in sorted(fcst.get("points", {}).items()):
        cells = []
        for d in horizon:
            entry = days.get(d)
            if not entry:
                cells.append("·")
            else:
                rr = entry.get("rr", 0)
                tx = entry.get("tmax")
                cells.append(f"{rr:.0f}mm<br>{tx:.0f}°" if tx is not None else f"{rr:.0f}mm")
        add(f"| {pid} | " + " | ".join(cells) + " |")
    add("")
    add("Pluie quotidienne et température maximale. Les 5 premiers jours "
        "viennent d'AROME HD, les suivants d'ARPEGE ou du meilleur modèle "
        "disponible.")
    add("")

    (ROOT / "ETAT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"ETAT.md écrit ({len(lines)} lignes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
