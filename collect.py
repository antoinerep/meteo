#!/usr/bin/env python3
"""Collecte météo locale autour de Monistrol-sur-Loire.

Trois flux indépendants, tous gratuits et sans clé d'API :

  obs       Observations horaires réelles des stations Météo-France
            (data.gouv.fr / BASE/HOR + BASE/HOR_COMP). Fichiers départementaux
            rafraîchis une fois par jour vers 05h45 UTC. C'est la vérité
            terrain : température et pluie vraiment mesurées.

  forecast  Prévisions Open-Meteo pour chaque point de config.json, un
            enregistrement par (run, modèle, échéance). On garde le run_ts pour
            pouvoir plus tard mesurer la qualité des prévisions.

  grid      Archive horaire haute résolution (AROME France HD, 1.3 km) pour
            chaque point. Comble le trou entre les stations : les stations sont
            à 450 m ou 900 m d'altitude, les points eux sont là où on va.

Chaque flux écrit des JSON-lines gzippés, un fichier par mois (obs, grid) ou
par jour (forecast). Les relances sont idempotentes : une ligne déjà présente
est remplacée, pas dupliquée.

Usage:
    python collect.py                      # obs + forecast + grid (incrémental)
    python collect.py --only obs
    python collect.py --backfill 400       # 400 jours d'historique stations
    python collect.py --refresh-stations   # redécouvre les stations du rayon
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import http.client
import io
import json
import math
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent
DATA = ROOT / "data"
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))

MF_BASE = "https://meteofrance.s3.sbg.io.cloud.ovh.net/data/synchro_ftp/BASE"
OM_FORECAST = "https://api.open-meteo.com/v1/forecast"
OM_ARCHIVE = "https://historical-forecast-api.open-meteo.com/v1/forecast"

# Variables horaires demandées à Open-Meteo.
#
# AROME HD est le modèle fin (1,3 km) mais il ne sort ni bilan hydrique ni
# température de sol ; `best_match` les fournit, à une résolution plus grossière.
# On interroge donc les deux et on recolle : la pluie et l'air viennent du
# modèle fin, le sol du modèle qui sait le calculer.
OM_AIR = [
    "temperature_2m",
    "relative_humidity_2m",
    "dew_point_2m",
    "precipitation",
    "wind_speed_10m",
]
OM_SOIL = [
    "et0_fao_evapotranspiration",
    "soil_temperature_6cm",
    "soil_temperature_18cm",
    "soil_moisture_3_9cm",
    "soil_moisture_9_27cm",
    "soil_moisture_27_81cm",
]
OM_HOURLY = OM_AIR + OM_SOIL

HI_RES_MODEL = "meteofrance_arome_france_hd"
SOIL_MODEL = "best_match"

USER_AGENT = "meteo-monistrol/1.0 (collecte perso, https://github.com/)"


# --------------------------------------------------------------------------- #
# utilitaires
# --------------------------------------------------------------------------- #

def log(msg: str) -> None:
    print(f"[{dt.datetime.now(dt.timezone.utc):%H:%M:%S}] {msg}", flush=True)


def fetch(url: str, *, tries: int = 4, timeout: int = 180) -> bytes:
    """GET avec retry exponentiel. Lève la dernière exception si tout échoue."""
    last: Exception | None = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        # IncompleteRead et consorts descendent de HTTPException, pas de OSError :
        # sans elle, une réponse tronquée en plein backfill tue tout le run.
        except (urllib.error.URLError, http.client.HTTPException,
                TimeoutError, OSError) as exc:
            last = exc
            wait = 2 ** attempt
            log(f"  echec ({exc}) — nouvel essai dans {wait}s")
            time.sleep(wait)
    raise RuntimeError(f"abandon apres {tries} essais: {url}") from last


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def read_jsonl_gz(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def write_jsonl_gz(path: pathlib.Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    # mtime=0 : le gzip est reproductible, donc git ne voit un diff que si le
    # contenu change vraiment.
    with gzip.GzipFile(tmp, "wb", compresslevel=9, mtime=0) as gz:
        with io.TextIOWrapper(gz, encoding="utf-8") as out:
            for row in rows:
                out.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    tmp.replace(path)


def merge_into(path: pathlib.Path, new_rows: list[dict], key) -> int:
    """Fusionne new_rows dans le fichier, en écrasant les lignes de même clé.

    Retourne le nombre de lignes ajoutées ou modifiées.
    """
    existing = {key(r): r for r in read_jsonl_gz(path)}
    changed = 0
    for row in new_rows:
        k = key(row)
        if existing.get(k) != row:
            existing[k] = row
            changed += 1
    if changed:
        write_jsonl_gz(path, [existing[k] for k in sorted(existing)])
    return changed


def date_chunks(start: dt.date, end: dt.date, size: int):
    """Découpe [start, end] en tranches contiguës d'au plus `size` jours."""
    cursor = start
    while cursor <= end:
        stop = min(cursor + dt.timedelta(days=size - 1), end)
        yield cursor, stop
        cursor = stop + dt.timedelta(days=1)


def group_by_month(rows: list[dict], ts_field: str) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for row in rows:
        out.setdefault(row[ts_field][:7], []).append(row)
    return out


# --------------------------------------------------------------------------- #
# flux 1 — observations Météo-France
# --------------------------------------------------------------------------- #

# Colonnes retenues du CSV Météo-France. Le fichier en compte ~200, on n'en
# garde que ce qui sert : pluie, température, humidité, vent, rayonnement.
MF_FIELDS = {
    "RR1": "rr",        # cumul de pluie de l'heure, mm
    "T": "t",           # température instantanée, °C
    "TN": "tn",         # minimale de l'heure
    "TX": "tx",         # maximale de l'heure
    "U": "rh",          # humidité relative, %
    "FF": "wind",       # vent moyen 10 min, m/s
    "GLO": "rad",       # rayonnement global, J/cm²
    "TNSOL": "tsol",    # minimale à 10 cm au-dessus du sol
    "T10": "t10",       # température du sol à 10 cm
    "T20": "t20",
    "T50": "t50",
}


def mf_station_cache() -> pathlib.Path:
    return ROOT / "stations.json"


def _mf_urls(dept: str) -> list[tuple[str, str]]:
    """(libellé, url) des fichiers horaires récents d'un département.

    HOR    = postes principaux (pluie + température + reste)
    HORCOMP = postes complémentaires, souvent de simples pluviomètres, mais
              beaucoup plus denses sur le terrain.
    """
    return [
        ("HOR", f"{MF_BASE}/HOR/H_{dept}_latest-2025-2026.csv.gz"),
        ("HORCOMP", f"{MF_BASE}/HOR_COMP/H-COMP_{dept}_latest-2025-2026.csv.gz"),
    ]


def _mf_urls_previous(dept: str) -> list[tuple[str, str]]:
    return [
        ("HOR", f"{MF_BASE}/HOR/H_{dept}_previous-2020-2024.csv.gz"),
        ("HORCOMP", f"{MF_BASE}/HOR_COMP/H-COMP_{dept}_previous-2020-2024.csv.gz"),
    ]


def parse_mf_csv(blob: bytes, keep_ids: set[str] | None, since: str | None):
    """Génère (station_meta, observation) pour les lignes retenues.

    since : horodatage AAAAMMJJHH minimum, pour éviter de reconstruire des
    années d'historique à chaque run.
    """
    text = gzip.decompress(blob).decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    for rec in reader:
        num = rec.get("NUM_POSTE")
        if not num:
            continue
        if keep_ids is not None and num not in keep_ids:
            continue
        stamp = rec.get("AAAAMMJJHH") or ""
        if len(stamp) != 10:
            continue
        if since and stamp < since:
            continue

        meta = {
            "id": num,
            "name": (rec.get("NOM_USUEL") or "").strip(),
            "lat": float(rec["LAT"]),
            "lon": float(rec["LON"]),
            "alt": int(float(rec["ALTI"])) if rec.get("ALTI") else None,
        }

        obs = {
            "station": num,
            # L'horodatage Météo-France est en UTC, heure de fin de cumul.
            "ts": f"{stamp[0:4]}-{stamp[4:6]}-{stamp[6:8]}T{stamp[8:10]}:00Z",
        }
        has_value = False
        for col, short in MF_FIELDS.items():
            raw = (rec.get(col) or "").strip()
            if raw:
                try:
                    obs[short] = float(raw)
                    has_value = True
                except ValueError:
                    pass
        if has_value:
            yield meta, obs


def discover_stations(force: bool = False) -> dict[str, dict]:
    """Liste les stations dans le rayon configuré, et met en cache."""
    cache = mf_station_cache()
    if cache.exists() and not force:
        return json.loads(cache.read_text(encoding="utf-8"))

    center = CONFIG["center"]
    radius = CONFIG["radius_km"]
    found: dict[str, dict] = {}

    for dept in CONFIG["departements"]:
        for kind, url in _mf_urls(dept):
            log(f"decouverte {kind} dept {dept}")
            blob = fetch(url)
            for meta, _ in parse_mf_csv(blob, keep_ids=None, since="2026010100"):
                if meta["id"] in found:
                    continue
                d = haversine_km(center["lat"], center["lon"], meta["lat"], meta["lon"])
                if d <= radius:
                    meta["dist_km"] = round(d, 2)
                    meta["dept"] = dept
                    meta["kind"] = kind
                    found[meta["id"]] = meta

    ordered = dict(sorted(found.items(), key=lambda kv: kv[1]["dist_km"]))
    cache.write_text(
        json.dumps(ordered, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    log(f"{len(ordered)} stations dans un rayon de {radius} km")
    for meta in ordered.values():
        log(f"   {meta['dist_km']:5.1f} km  {meta['alt']:>5} m  {meta['name']}")
    return ordered


def collect_obs(days: int) -> int:
    """Observations des N derniers jours pour toutes les stations du rayon."""
    stations = discover_stations()
    keep = set(stations)
    since_dt = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    since = since_dt.strftime("%Y%m%d%H")

    rows: list[dict] = []
    seen_hours: set[str] = set()
    for dept in CONFIG["departements"]:
        urls = list(_mf_urls(dept))
        # Le fichier "latest" couvre 2025-2026 ; au-delà il faut le précédent.
        if since < "2025010100":
            urls += _mf_urls_previous(dept)
        for kind, url in urls:
            log(f"obs {kind} dept {dept} depuis {since}")
            blob = fetch(url)
            for _, obs in parse_mf_csv(blob, keep_ids=keep, since=since):
                rows.append(obs)
                seen_hours.add(obs["ts"])

    if not rows:
        raise RuntimeError(
            "aucune observation renvoyée — fichier Météo-France vide ou format changé"
        )

    changed = 0
    for month, batch in group_by_month(rows, "ts").items():
        path = DATA / "obs" / f"{month}.jsonl.gz"
        changed += merge_into(path, batch, key=lambda r: (r["station"], r["ts"]))

    log(f"obs: {len(rows)} lignes lues, {changed} nouvelles/modifiees, "
        f"derniere heure {max(seen_hours)}")
    return changed


# --------------------------------------------------------------------------- #
# flux 2 — prévisions Open-Meteo
# --------------------------------------------------------------------------- #

def om_request(base: str, params: dict) -> dict:
    url = base + "?" + urllib.parse.urlencode(params, doseq=True)
    payload = json.loads(fetch(url, timeout=90))
    if payload.get("error"):
        raise RuntimeError(f"Open-Meteo: {payload.get('reason')}")
    return payload


def _unpack_hourly(payload: dict, models: list[str]) -> dict[str, dict[str, dict]]:
    """Réorganise la réponse Open-Meteo en {modèle: {horodatage: {var: valeur}}}.

    Quand on demande plusieurs modèles, Open-Meteo suffixe chaque série du nom
    du modèle (`temperature_2m_meteofrance_arome_france_hd`), sauf s'il n'y en
    a qu'un. On remet tout à plat.
    """
    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    out: dict[str, dict[str, dict]] = {m: {} for m in models}

    for column, values in hourly.items():
        if column == "time":
            continue
        model = next((m for m in models if column.endswith("_" + m)), None)
        if model is None:
            if len(models) != 1:
                continue
            model, var = models[0], column
        else:
            var = column[: -len("_" + model)]
        for ts, value in zip(times, values):
            if value is not None:
                out[model].setdefault(ts, {})[var] = value
    return out


def collect_forecast() -> int:
    run_ts = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    models = CONFIG["forecast_models"]
    rows: list[dict] = []

    for point in CONFIG["points"]:
        payload = om_request(OM_FORECAST, {
            "latitude": point["lat"],
            "longitude": point["lon"],
            "hourly": ",".join(OM_HOURLY),
            "models": ",".join(models),
            "forecast_days": 14,
            "past_days": 2,
            "timezone": CONFIG["timezone"],
        })
        unpacked = _unpack_hourly(payload, models)
        for model, series in unpacked.items():
            for ts, values in series.items():
                rows.append({
                    "run_ts": run_ts,
                    "point": point["id"],
                    "model": model,
                    "ts": ts,
                    **values,
                })
        log(f"prevision {point['id']}: "
            + ", ".join(f"{m}={len(s)}h" for m, s in unpacked.items()))

    if not rows:
        raise RuntimeError("Open-Meteo n'a renvoyé aucune prévision")

    day = run_ts[:10]
    path = DATA / "forecast" / f"{day}.jsonl.gz"
    changed = merge_into(
        path, rows, key=lambda r: (r["run_ts"], r["point"], r["model"], r["ts"])
    )
    log(f"forecast: {len(rows)} lignes, {changed} nouvelles")
    return changed


# --------------------------------------------------------------------------- #
# flux 3 — archive haute résolution par point
# --------------------------------------------------------------------------- #

def collect_grid(days: int) -> int:
    """Historique horaire par point : air depuis AROME HD, sol depuis best_match.

    AROME HD est archivé depuis 2024 environ ; au-delà la requête renvoie des
    nulls, qu'on ignore silencieusement.
    """
    end = dt.date.today() - dt.timedelta(days=1)
    start = end - dt.timedelta(days=days)
    rows: list[dict] = []

    for point in CONFIG["points"]:
        counts: dict[str, int] = {}
        for model, variables in ((HI_RES_MODEL, OM_AIR), (SOIL_MODEL, OM_SOIL)):
            # Au-delà de quelques mois, Open-Meteo tronque parfois la réponse
            # en plein vol. On découpe donc en tranches de 120 jours.
            for chunk_start, chunk_end in date_chunks(start, end, 120):
                payload = om_request(OM_ARCHIVE, {
                    "latitude": point["lat"],
                    "longitude": point["lon"],
                    "hourly": ",".join(variables),
                    "models": model,
                    "start_date": chunk_start.isoformat(),
                    "end_date": chunk_end.isoformat(),
                    "timezone": CONFIG["timezone"],
                })
                series = _unpack_hourly(payload, [model])[model]
                for ts, values in series.items():
                    rows.append({"point": point["id"], "model": model,
                                 "ts": ts, **values})
                counts[model] = counts.get(model, 0) + len(series)
        log(f"archive {point['id']}: "
            + " ".join(f"{m.split('_')[-1]}={n}h" for m, n in counts.items()))

    if not rows:
        raise RuntimeError("archive Open-Meteo vide")

    changed = 0
    for month, batch in group_by_month(rows, "ts").items():
        path = DATA / "grid" / f"{month}.jsonl.gz"
        changed += merge_into(
            path, batch, key=lambda r: (r["point"], r["model"], r["ts"])
        )
    log(f"grid: {len(rows)} lignes, {changed} nouvelles/modifiees")
    return changed


# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", choices=["obs", "forecast", "grid"], action="append",
                    help="ne lancer que ce(s) flux (répétable)")
    ap.add_argument("--backfill", type=int, metavar="JOURS",
                    help="profondeur d'historique à (re)charger pour obs et grid")
    ap.add_argument("--refresh-stations", action="store_true",
                    help="redécouvrir les stations du rayon puis sortir")
    args = ap.parse_args()

    if args.refresh_stations:
        discover_stations(force=True)
        return 0

    flows = args.only or ["obs", "forecast", "grid"]
    # En routine on ne relit que quelques jours : les fichiers Météo-France
    # corrigent parfois a posteriori les dernières heures.
    days = args.backfill or 10
    grid_days = args.backfill or 7

    failures: list[str] = []
    if "obs" in flows:
        try:
            collect_obs(days)
        except Exception as exc:              # noqa: BLE001 — on veut continuer
            failures.append(f"obs: {exc}")
            log(f"ERREUR obs: {exc}")
    if "forecast" in flows:
        try:
            collect_forecast()
        except Exception as exc:              # noqa: BLE001
            failures.append(f"forecast: {exc}")
            log(f"ERREUR forecast: {exc}")
    if "grid" in flows:
        try:
            collect_grid(grid_days)
        except Exception as exc:              # noqa: BLE001
            failures.append(f"grid: {exc}")
            log(f"ERREUR grid: {exc}")

    if failures:
        print("\n".join("ECHEC " + f for f in failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
