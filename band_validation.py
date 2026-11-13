#!/usr/bin/env python3
"""Sammelt Belege dafuer, ob die Residuallast-Baender ueberhaupt Preise treffen.

Die Baender in detector.py sind geraten, nicht gefittet. Ein erster Abgleich am
16.08.2026 gegen eine echte Preisprognose sah beim Extremtag sehr gut aus
(r=0,96 ueber 4 Tage), im Mittelfeld dagegen gar nicht: 51,3 und 52,3 GW
("erhoeht") und 49,4 GW ("normal") lagen preislich praktisch gleichauf. Vier
Tage sind aber kein Beleg — also mitschreiben, bis es einer ist.

Geloggt wird pro Lauf fuer jeden Prognosetag:
  - eigene Spitzen-Residuallast + Band (aus report.json des Detektors)
  - Preisprognose von EpexPredictor (Open Source, 5 Tage, ohne Key)
  - spaeter der tatsaechlich eingetretene Boersenpreis (energy-charts)

EpexPredictor und energy-charts sind im bekannten Bereich dieselben Daten
(geprueft: max. 0,05 EUR/MWh Abweichung), also direkt vergleichbar.

    band_validation.py            # einen Schnappschuss anhaengen
    band_validation.py --report   # auswerten, was bisher zusammenkam
    band_validation.py --report --drop   # Auswertung als Markdown-Datei ablegen
"""
import argparse
import collections
import datetime as dt
import json
import os
import pathlib
import statistics
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
REPORT = HERE / "report.json"
FORECASTS = HERE / "validation-forecasts.jsonl"
ACTUALS = HERE / "validation-actuals.json"
# Zielordner fuer --drop; ueber die Umgebung einstellbar.
DROP_DIR = pathlib.Path(os.environ.get("RESIDUALLAST_DROP_DIR", HERE / "drop"))

EPEX = "https://epexpredictor.batzill.com/prices?region=DE&hours=120&unit=EUR_PER_KWH"
ENERGY_CHARTS = "https://api.energy-charts.info/price?bzn=DE-LU&start={start}&end={end}"
BERLIN = dt.timezone(dt.timedelta(hours=2))  # CEST; nur fuer die Tageszuordnung


def get(url, timeout=40):
    req = urllib.request.Request(url, headers={"User-Agent": "residuallast-fruehwarnung-validation/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def epex_by_day():
    """{Tag -> {max, mean, known}} aus der EpexPredictor-Prognose."""
    d = get(EPEX)
    known_until = d.get("knownUntil") or ""
    days = collections.defaultdict(list)
    for x in d.get("prices", []):
        # total kommt in EUR/kWh -> EUR/MWh, damit alles eine Einheit hat
        days[x["startsAt"][:10]].append((x["total"] * 1000.0, x["startsAt"] <= known_until))
    out = {}
    for day, vals in days.items():
        prices = [p for p, _ in vals]
        out[day] = {
            "max": round(max(prices), 2),
            "mean": round(statistics.mean(prices), 2),
            # Ein Tag, der schon Ist-Daten enthaelt, ist keine echte Prognose mehr.
            "known": all(k for _, k in vals),
        }
    return out


def actual_by_day(days_back=45):
    """{Tag -> {max, mean, p90}} tatsaechlicher Boersenpreise, EUR/MWh."""
    end = dt.date.today()
    start = end - dt.timedelta(days=days_back)
    d = get(ENERGY_CHARTS.format(start=start, end=end))
    days = collections.defaultdict(list)
    for ts, price in zip(d.get("unix_seconds", []), d.get("price", [])):
        if price is None:
            continue
        day = dt.datetime.fromtimestamp(ts, BERLIN).strftime("%Y-%m-%d")
        days[day].append(price)
    out = {}
    for day, prices in days.items():
        if len(prices) < 90:  # unvollstaendiger Tag (mind. ~24 h im 15-min-Raster)
            continue
        s = sorted(prices)
        out[day] = {
            "max": round(max(prices), 2),
            "mean": round(statistics.mean(prices), 2),
            "p90": round(s[int(0.9 * (len(s) - 1))], 2),
        }
    return out


def snapshot():
    if not REPORT.exists():
        raise SystemExit(f"{REPORT} fehlt — laeuft der Detektor?")
    rep = json.loads(REPORT.read_text())
    epex = epex_by_day()
    today = dt.date.today()
    run = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

    rows = []
    for r in rep.get("days", []):
        day = r["day"]
        e = epex.get(day) or {}
        lead = (dt.date.fromisoformat(day) - today).days
        rows.append({
            "run": run, "day": day, "lead_days": lead,
            "peak_resid": r["peak_resid"], "band": r["band"],
            # Zweite Kenngroesse ab 16.08. — im Rueckblick der bessere
            # Prognostiker, live aber anfaellig fuer die Anker-Schwaeche.
            # Genau diesen Widerspruch soll die Sammlung aufloesen.
            "mean_resid": r.get("mean_resid"), "band_mean": r.get("band_mean"),
            "band_mean_extrapoliert": r.get("band_mean_extrapoliert"),
            # Ensemble: das ist ab jetzt die Groesse, die den Alarm ausloest —
            # ohne sie im Log kann spaeter niemand pruefen, ob sie besser trifft
            # als das deterministische Band.
            "ens_p10": (r.get("ensemble") or {}).get("p10"),
            "ens_median": (r.get("ensemble") or {}).get("median"),
            "ens_p90": (r.get("ensemble") or {}).get("p90"),
            "ens_p_warn": (r.get("ensemble") or {}).get("p_warn"),
            "ens_unentscheidbar": (r.get("ensemble") or {}).get("unentscheidbar"),
            # Relative Einstufung, Ausloeser seit 13.11.2026.
            "rel_cut": (r.get("rel") or {}).get("cut"),
            "rel_warn": (r.get("rel") or {}).get("warn"),
            "min_share": r.get("min_share"), "avg_share": r.get("avg_share"),
            "epex_max": e.get("max"), "epex_mean": e.get("mean"),
            "epex_is_forecast": (not e.get("known")) if e else None,
        })

    with FORECASTS.open("a") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    # Ist-Preise komplett neu schreiben — klein, abgeleitet, damit selbstheilend.
    ACTUALS.write_text(json.dumps(actual_by_day(), indent=1, sort_keys=True))
    print(f"{len(rows)} Prognosezeilen angehaengt, {len(json.loads(ACTUALS.read_text()))} Ist-Tage gespeichert")


def load_pairs():
    """[(lead, peak_resid, band, epex_max, actual_max, actual_mean)] je Tag+Lead."""
    if not FORECASTS.exists():
        return []
    actuals = json.loads(ACTUALS.read_text()) if ACTUALS.exists() else {}
    # Pro (Prognosetag, Lead) den letzten Lauf gewinnen lassen.
    best = {}
    for line in FORECASTS.read_text().splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("lead_days", 0) < 1:
            continue  # nur echte Vorhersagen, keine Rueckschau
        best[(r["day"], r["lead_days"])] = r
    out = []
    for (day, lead), r in sorted(best.items()):
        a = actuals.get(day)
        if not a:
            continue
        out.append((lead, r["peak_resid"], r["band"], r.get("epex_max"), a["max"], a["mean"]))
    return out


def pearson(xs, ys):
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sx = sum((x - mx) ** 2 for x in xs) ** 0.5
    sy = sum((y - my) ** 2 for y in ys) ** 0.5
    return cov / (sx * sy) if sx and sy else float("nan")


def report(as_drop=False):
    pairs = load_pairs()
    lines = []
    if len(pairs) < 8:
        lines.append(f"Nur {len(pairs)} aufgeloeste Prognose/Ist-Paare — zu duenn fuer eine Aussage.")
        lines.append("Es wird weiter gesammelt; ab ~30 Paaren wird die Auswertung belastbar.")
    else:
        resid = [p[1] for p in pairs]
        amax = [p[4] for p in pairs]
        lines.append(f"Paare: {len(pairs)} ueber {len({p[0] for p in pairs})} Vorlaufweiten")
        lines.append(f"Korrelation Residuallast vs. Ist-Tagesmaximum: r = {pearson(resid, amax):.2f}")
        epx = [(p[3], p[4]) for p in pairs if p[3] is not None]
        if len(epx) >= 8:
            lines.append(f"Zum Vergleich EpexPredictor vs. Ist:        r = "
                         f"{pearson([e for e, _ in epx], [a for _, a in epx]):.2f}")
            mae = statistics.mean(abs(e - a) for e, a in epx)
            lines.append(f"EpexPredictor MAE auf Tagesmaximum: {mae:.1f} EUR/MWh")
        lines.append("")
        lines.append("Was die Baender tatsaechlich an Preisen gesehen haben:")
        by_band = collections.defaultdict(list)
        for _, _, band, _, am, _ in pairs:
            by_band[band].append(am)
        lines.append(f"  {'Band':10}{'n':>4}{'Median':>9}{'Spanne':>18}")
        for band in ("günstig", "normal", "erhöht", "hoch"):
            v = by_band.get(band)
            if not v:
                continue
            lines.append(f"  {band:10}{len(v):>4}{statistics.median(v):>9.1f}"
                         f"{min(v):>10.1f}–{max(v):<7.1f}")
        # Trennen die Baender ueberhaupt?
        ordered = [b for b in ("günstig", "normal", "erhöht", "hoch") if by_band.get(b)]
        meds = [statistics.median(by_band[b]) for b in ordered]
        if len(meds) >= 3 and meds != sorted(meds):
            lines.append("")
            lines.append("  ACHTUNG: die Band-Mediane steigen NICHT monoton — die Schwellen "
                         "trennen die Preise derzeit nicht sauber.")
    body = "\n".join(lines)
    if not as_drop:
        print(body)
        return
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    DROP_DIR.mkdir(parents=True, exist_ok=True)
    (DROP_DIR / f"dunkelflaute-validierung-{stamp}.md").write_text(
        f"""# Dunkelflaute-Bänder — Validierung {stamp}

Routineauswertung: treffen die geratenen Residuallast-Bänder die echten
Börsenpreise? **Kein Alarm** — ein Bericht.

```
{body}
```

Der Bericht ändert nichts an den Schwellen. Ist die Datenlage noch zu dünn,
steht genau das oben — das ist ein Ergebnis, kein Problem.
""", encoding="utf-8")
    print(f"Drop geschrieben ({len(pairs)} Paare)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--drop", action="store_true")
    a = ap.parse_args()
    if a.report:
        report(as_drop=a.drop)
    else:
        snapshot()
