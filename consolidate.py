#!/usr/bin/env python3
"""Agrège les données brutes en séries journalières prêtes à consommer.

C'est le *contrat* du projet météo avec ses consommateurs : quoi qu'il arrive
aux formats bruts, ces deux fichiers gardent la même forme.

    data/daily.json           passé observé + analysé, un enregistrement par
                              (source, jour)
    data/forecast_daily.json  prévision la plus récente, un enregistrement par
                              (point, jour)

Une « source » est soit une station Météo-France (`station:43137003`), soit un
point de config.json alimenté par AROME HD (`point:orcimont`).

Les journées météo sont calées sur 06h–06h UTC, comme les cumuls quotidiens de
Météo-France : une pluie qui tombe dans la nuit est attribuée à la veille, ce
qui correspond à la façon dont on en parle sur le terrain.
"""

from __future__ import annotations

import collections
import datetime as dt
import gzip
import json
import math
import pathlib
import statistics
import sys

ROOT = pathlib.Path(__file__).resolve().parent
DATA = ROOT / "data"
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))

# Décalage appliqué avant de découper en journées. Météo-France clôt la journée
# pluviométrique à 06h UTC.
DAY_CUTOFF_HOUR = 6


def iter_jsonl_gz(paths):
    for path in sorted(paths):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    yield json.loads(line)


def to_day(ts: str) -> str:
    """Horodatage ISO -> jour météo (06h-06h)."""
    stamp = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return (stamp - dt.timedelta(hours=DAY_CUTOFF_HOUR)).date().isoformat()


class DayBucket:
    """Accumule les valeurs horaires d'une journée puis les résume."""

    __slots__ = ("rr", "t", "rh", "et0", "tsol", "sm", "sm_deep", "wind", "hours")

    def __init__(self) -> None:
        self.rr = 0.0
        self.t: list[float] = []
        self.rh: list[float] = []
        self.et0 = 0.0
        self.tsol: list[float] = []
        self.sm: list[float] = []
        self.sm_deep: list[float] = []
        self.wind: list[float] = []
        self.hours = 0

    def add(self, *, rr=None, t=None, rh=None, et0=None, tsol=None, sm=None,
            sm_deep=None, wind=None):
        self.hours += 1
        if rr is not None:
            self.rr += rr
        if et0 is not None:
            self.et0 += et0
        for value, bag in ((t, self.t), (rh, self.rh), (tsol, self.tsol),
                           (sm, self.sm), (sm_deep, self.sm_deep), (wind, self.wind)):
            if value is not None:
                bag.append(value)

    def summary(self) -> dict:
        out: dict[str, float | int] = {"rr": round(self.rr, 1), "hours": self.hours}
        if self.t:
            out["tmin"] = round(min(self.t), 1)
            out["tmax"] = round(max(self.t), 1)
            out["tmoy"] = round(statistics.fmean(self.t), 1)
        if self.rh:
            out["rh"] = round(statistics.fmean(self.rh))
        if self.et0:
            out["et0"] = round(self.et0, 2)
        if self.tsol:
            out["tsol"] = round(statistics.fmean(self.tsol), 1)
        if self.sm:
            out["sm"] = round(statistics.fmean(self.sm), 3)
        if self.sm_deep:
            out["sm_deep"] = round(statistics.fmean(self.sm_deep), 3)
        if self.wind:
            out["wind"] = round(statistics.fmean(self.wind), 1)
        return out


def build_station_daily() -> dict[str, dict[str, dict]]:
    buckets: dict[str, dict[str, DayBucket]] = collections.defaultdict(
        lambda: collections.defaultdict(DayBucket))
    for row in iter_jsonl_gz((DATA / "obs").glob("*.jsonl.gz")):
        src = f"station:{row['station']}"
        buckets[src][to_day(row["ts"])].add(
            rr=row.get("rr"), t=row.get("t"), rh=row.get("rh"),
            tsol=row.get("t10"), wind=row.get("wind"),
        )
    return {s: {d: b.summary() for d, b in days.items()} for s, days in buckets.items()}


def build_point_daily() -> dict[str, dict[str, dict]]:
    """Séries par point, en recollant les deux modèles.

    L'air vient d'AROME HD, le sol de best_match : deux lignes distinctes pour
    la même heure. On les fusionne avant de cumuler, sinon chaque heure serait
    comptée deux fois.
    """
    merged: dict[tuple[str, str], dict] = {}
    for row in iter_jsonl_gz((DATA / "grid").glob("*.jsonl.gz")):
        key = (row["point"], row["ts"])
        slot = merged.setdefault(key, {})
        for field, value in row.items():
            if field not in ("point", "ts", "model") and value is not None:
                slot.setdefault(field, value)

    buckets: dict[str, dict[str, DayBucket]] = collections.defaultdict(
        lambda: collections.defaultdict(DayBucket))
    for (point, ts), values in merged.items():
        buckets[f"point:{point}"][to_day(ts)].add(
            rr=values.get("precipitation"), t=values.get("temperature_2m"),
            rh=values.get("relative_humidity_2m"),
            et0=values.get("et0_fao_evapotranspiration"),
            tsol=values.get("soil_temperature_6cm"),
            sm=values.get("soil_moisture_3_9cm"),
            sm_deep=values.get("soil_moisture_9_27cm"),
            wind=values.get("wind_speed_10m"),
        )
    return {s: {d: b.summary() for d, b in days.items()} for s, days in buckets.items()}


# Du plus fin au plus grossier. AROME HD ne porte que ~5 jours, ARPEGE ~7,
# best_match va jusqu'à 14 et c'est le seul à sortir les variables de sol.
MODEL_PRIORITY = [
    "meteofrance_arome_france_hd",
    "meteofrance_arpege_europe",
    "best_match",
]


def interpoler_stations(daily: dict[str, dict[str, dict]],
                        sources: dict[str, dict]) -> None:
    """Ajoute à chaque point la pluie *mesurée*, interpolée des stations.

    Pourquoi : la pluie d'un point venait jusqu'ici d'AROME HD, qui est un
    modèle. Confronté jour par jour aux pluviomètres sur un millier de jours,
    il donne une corrélation de 0,79 et une erreur absolue moyenne de 1,63 mm.
    Une simple interpolation des quatorze stations réelles, testée en validation
    croisée (chaque station reconstituée depuis les autres), fait bien mieux :
    corrélation 0,93, erreur 0,86 mm. Surtout, sur ce qui compte pour un modèle
    de fructification — « un épisode de 20 mm en 4 jours a-t-il eu lieu » — les
    deux approches sont d'accord avec la mesure 90,8 % du temps pour le modèle,
    95,3 % pour l'interpolation. L'erreur sur le déclencheur est donc divisée
    par deux.

    La pondération est en inverse du carré d'une distance *corrigée du
    dénivelé* : 100 m d'écart d'altitude pénalisent autant qu'un kilomètre de
    distance horizontale. Ce n'est pas arbitraire — dans ce secteur, les cumuls
    suivent le relief plus que la distance, St-Romain-Lachalm (902 m) recevant
    64 % de plus que Bas-en-Basset (446 m) à treize kilomètres de là.

    Le champ `rr` du modèle est conservé tel quel : c'est au consommateur de
    choisir, et la prévision, elle, n'a pas de station.
    """
    stations = {k: v for k, v in sources.items() if v["kind"] == "station"}
    points = {k: v for k, v in sources.items() if v["kind"] == "point"}
    if not stations:
        return

    # Index inverse : pour un jour donné, les stations qui ont mesuré.
    par_jour: dict[str, list[tuple[str, float]]] = {}
    for sid in stations:
        for jour, entry in daily.get(sid, {}).items():
            if "rr" in entry and entry.get("hours", 0) >= 20:
                par_jour.setdefault(jour, []).append((sid, entry["rr"]))

    for pid, pm in points.items():
        poids = {}
        for sid, sm in stations.items():
            d_km = math.hypot((pm["lat"] - sm["lat"]) * 111.0,
                              (pm["lon"] - sm["lon"]) * 78.1)
            penalite = abs((pm["alt"] or 0) - (sm["alt"] or 0)) / 100.0
            poids[sid] = 1.0 / max(d_km + penalite, 0.5) ** 2

        for jour, mesures in par_jour.items():
            if jour not in daily.get(pid, {}):
                continue
            num = den = 0.0
            for sid, rr in mesures:
                w = poids[sid]
                num += w * rr
                den += w
            # Moins de trois pluviomètres : l'interpolation ne vaut pas mieux
            # que le modèle, on ne l'écrit pas.
            if den > 0 and len(mesures) >= 3:
                daily[pid][jour]["rr_stations"] = round(num / den, 2)
                daily[pid][jour]["rr_stations_n"] = len(mesures)


def build_forecast_daily() -> dict:
    """Prévision journalière du dernier run, champ par champ.

    Chaque grandeur est prise au modèle le plus fin qui la fournit ce jour-là :
    la pluie et la température viennent d'AROME HD tant qu'il porte, puis
    d'ARPEGE, puis du modèle global ; les variables de sol viennent toujours de
    best_match, seul à les calculer. `models` garde la trace de l'origine.
    """
    files = sorted((DATA / "forecast").glob("*.jsonl.gz"))
    if not files:
        return {"run_ts": None, "points": {}}

    rows = list(iter_jsonl_gz(files[-2:]))
    if not rows:
        return {"run_ts": None, "points": {}}
    run_ts = max(r["run_ts"] for r in rows)
    rows = [r for r in rows if r["run_ts"] == run_ts]

    buckets: dict[tuple[str, str, str], DayBucket] = collections.defaultdict(DayBucket)
    for row in rows:
        key = (row["point"], row["model"], to_day(row["ts"]))
        buckets[key].add(
            rr=row.get("precipitation"), t=row.get("temperature_2m"),
            rh=row.get("relative_humidity_2m"),
            et0=row.get("et0_fao_evapotranspiration"),
            tsol=row.get("soil_temperature_6cm"),
            sm=row.get("soil_moisture_3_9cm"),
            sm_deep=row.get("soil_moisture_9_27cm"),
            wind=row.get("wind_speed_10m"),
        )

    by_point: dict[str, dict[str, dict]] = collections.defaultdict(dict)
    for model in MODEL_PRIORITY:                       # du plus fin au plus large
        for (point, row_model, day), bucket in buckets.items():
            if row_model != model:
                continue
            # Une journée tronquée (bord de la fenêtre du modèle) fausserait le
            # cumul de pluie : on l'ignore.
            if bucket.hours < 20:
                continue
            entry = by_point[point].setdefault(day, {"models": {}})
            for field, value in bucket.summary().items():
                if field == "hours" or field in entry:
                    continue
                entry[field] = value
                entry["models"][field] = model

    return {"run_ts": run_ts, "points": {p: dict(sorted(d.items()))
                                         for p, d in by_point.items()}}


def main() -> int:
    stations = json.loads((ROOT / "stations.json").read_text(encoding="utf-8")) \
        if (ROOT / "stations.json").exists() else {}

    sources: dict[str, dict] = {}
    for sid, meta in stations.items():
        sources[f"station:{sid}"] = {
            "kind": "station", "name": meta["name"], "lat": meta["lat"],
            "lon": meta["lon"], "alt": meta["alt"], "dist_km": meta["dist_km"],
        }
    for point in CONFIG["points"]:
        sources[f"point:{point['id']}"] = {
            "kind": "point", "name": point["name"], "lat": point["lat"],
            "lon": point["lon"], "alt": point["alt"],
            "model": "meteofrance_arome_france_hd",
        }

    daily = build_station_daily()
    daily.update(build_point_daily())
    interpoler_stations(daily, sources)
    daily = {k: dict(sorted(v.items())) for k, v in sorted(daily.items())}

    payload = {
        "generated": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "day_cutoff_utc_hour": DAY_CUTOFF_HOUR,
        "units": {"rr": "mm", "rr_stations": "mm", "tmin/tmax/tmoy/tsol": "degC",
                  "rh": "%", "et0": "mm", "sm": "m3/m3", "wind": "m/s"},
        "champs": {
            "rr": "pluie du modèle AROME HD interpolé au point",
            "rr_stations": "pluie mesurée, interpolée des stations voisines "
                           "(pondération inverse du carré de la distance "
                           "corrigée du dénivelé) ; absente si moins de trois "
                           "pluviomètres ce jour-là",
            "rr_stations_n": "nombre de stations utilisées",
        },
        "sources": sources,
        "daily": daily,
    }
    (DATA / "daily.json").write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8")

    forecast = build_forecast_daily()
    forecast["generated"] = payload["generated"]
    (DATA / "forecast_daily.json").write_text(
        json.dumps(forecast, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    n_days = sum(len(v) for v in daily.values())
    print(f"daily.json          : {len(daily)} sources, {n_days} jours-source")
    print(f"forecast_daily.json : run {forecast['run_ts']}, "
          f"{len(forecast['points'])} points")
    return 0


if __name__ == "__main__":
    sys.exit(main())
