#!/usr/bin/env python3
"""Rueckblick: erklaert Residuallast ueberhaupt den Strompreis — und wo liegen die Schwellen?

Die Baender in detector.py sind geraten. Die laufende Validierung
(band_validation.py) beantwortet das sauber, braucht aber Wochen. Dieser
Rueckblick beantwortet es sofort, weil Ist-Preise und Ist-Erzeugung fuer die
Vergangenheit vorliegen.

Zwei getrennte Fragen, bewusst nacheinander:

  Stufe 1 — Taugt das KONZEPT?
    Gemessene Residuallast (energy-charts liefert die Reihe direkt) gegen den
    tatsaechlichen Boersenpreis. Kein Modell, kein Wetter, kein Kalibrierfaktor.
    Trennt hier nichts, ist der ganze Ansatz tot, egal wie gut das Modell waere.

  Stufe 2 — Was kostet das MODELL?
    Dieselbe Rechnung mit der aus rueckblickendem Wetter modellierten
    Residuallast. Die Differenz zu Stufe 1 ist der Modellfehler.

Stufe 2 kalibriert pro Tag gegen den tatsaechlichen EE-Anteil. Das ist
GROSSZUEGIGER als die Realitaet — live steht nur eine 24-h-Prognose als Anker
zur Verfuegung, die auf 5 Tage angewendet wird. Stufe 2 ist damit eine
Obergrenze fuer die Modellguete, keine Vorhersage der Live-Leistung.

    hindcast.py [--days 45]
"""
import argparse
import collections
import datetime as dt
import importlib.util
import json
import pathlib
import statistics
import urllib.parse
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
BERLIN = dt.timezone(dt.timedelta(hours=2))


def load_detector():
    spec = importlib.util.spec_from_file_location("det", HERE / "detector.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def get(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "residuallast-fruehwarnung-hindcast/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def day_of(ts):
    return dt.datetime.fromtimestamp(ts, BERLIN).strftime("%Y-%m-%d")


def measured(start, end):
    """Gemessene Residuallast, Last und EE-Anteil pro Stunde + Preise pro Tag."""
    pp = get(f"https://api.energy-charts.info/public_power?country=de&start={start}&end={end}")
    series = {p["name"]: p["data"] for p in pp.get("production_types", [])}
    ts = pp["unix_seconds"]

    resid_day, load_day, ren_day = (collections.defaultdict(list) for _ in range(3))
    for i, t in enumerate(ts):
        d = day_of(t)
        for name, bucket in (("Residual load", resid_day), ("Load", load_day),
                             ("Renewable share of load", ren_day)):
            v = series.get(name, [None] * len(ts))[i]
            if v is not None:
                bucket[d].append(v)

    pr = get(f"https://api.energy-charts.info/price?bzn=DE-LU&start={start}&end={end}")
    price_day = collections.defaultdict(list)
    for t, p in zip(pr.get("unix_seconds", []), pr.get("price", [])):
        if p is not None:
            price_day[day_of(t)].append(p)

    out = {}
    for d in sorted(resid_day):
        if len(resid_day[d]) < 90 or len(price_day.get(d, [])) < 90:
            continue  # angebrochener Tag
        out[d] = {
            "peak_resid": max(resid_day[d]) / 1000.0,   # MW -> GW
            "mean_resid": statistics.mean(resid_day[d]) / 1000.0,
            "ren_share": statistics.mean(ren_day[d]) if ren_day.get(d) else None,
            "price_max": max(price_day[d]),
            "price_mean": statistics.mean(price_day[d]),
        }
    return out


def modelled(det, days_back):
    """Modellierte Residuallast pro Stunde aus rueckblickendem Wetter."""
    hours = {}
    for name, lat, lon, ws, ww in det.POINTS:
        # Ortszeit wie im Detektor — sonst misst der Rueckblick ein anderes
        # Modell als das, das live laeuft (Lastprofil-Phase, Tagesgrenzen).
        q = urllib.parse.urlencode({
            "latitude": lat, "longitude": lon,
            "hourly": "shortwave_radiation,wind_speed_100m",
            "past_days": min(days_back + 2, 92), "forecast_days": 1,
            "timezone": "Europe/Berlin"})
        d = get("https://api.open-meteo.com/v1/forecast?" + q)["hourly"]
        for i, t in enumerate(d["time"]):
            rad = d["shortwave_radiation"][i] or 0.0
            wnd = (d["wind_speed_100m"][i] or 0.0) / 3.6
            e = hours.setdefault(t, {"s": 0., "sw": 0., "w": 0., "ww": 0.})
            e["s"] += min(rad / 850., 1.0) * ws
            e["sw"] += ws
            e["w"] += det.wind_cf(wnd) * ww
            e["ww"] += ww

    raw = collections.defaultdict(list)
    for t in sorted(hours):
        e = hours[t]
        # Zeitstempel sind bereits deutsche Ortszeit (siehe timezone-Parameter).
        when = dt.datetime.fromisoformat(t)
        scf = e["s"] / e["sw"] if e["sw"] else 0.
        wcf = e["w"] / e["ww"] if e["ww"] else 0.
        # Formeln NICHT duplizieren, sondern die Konstanten des Detektors
        # benutzen — sonst misst der Rueckblick ein Modell, das es nicht gibt.
        solar = det.PV_GW * scf * det.SOLAR_PR
        wind = (det.WIND_ON_GW + det.WIND_OFF_GW) * wcf * det.WIND_FLEET
        raw[when.strftime("%Y-%m-%d")].append((solar, wind, det.load_at(when)))
    return raw


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=45)
    args = ap.parse_args()
    det = load_detector()

    end = dt.date.today()
    start = end - dt.timedelta(days=args.days)
    print(f"Zeitraum {start} bis {end}\n")

    meas = measured(start, end)
    print(f"Gemessene Tage mit vollstaendigen Preis- und Erzeugungsdaten: {len(meas)}")

    def pearson(xs, ys):
        n = len(xs)
        mx, my = sum(xs) / n, sum(ys) / n
        cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        sx = sum((x - mx) ** 2 for x in xs) ** .5
        sy = sum((y - my) ** 2 for y in ys) ** .5
        return cov / (sx * sy) if sx and sy else float("nan")

    days = sorted(meas)
    pr = [meas[d]["peak_resid"] for d in days]
    pmax = [meas[d]["price_max"] for d in days]
    pmean = [meas[d]["price_mean"] for d in days]

    print("\n=== STUFE 1 — taugt das Konzept? (gemessene Residuallast) ===")
    print(f"  Spitzen-Residuallast vs. Tages-Höchstpreis : r = {pearson(pr, pmax):+.2f}")
    print(f"  Spitzen-Residuallast vs. Tages-Mittelpreis : r = {pearson(pr, pmean):+.2f}")
    mr = [meas[d]["mean_resid"] for d in days]
    print(f"  Mittlere Residuallast vs. Mittelpreis      : r = {pearson(mr, pmean):+.2f}")
    print(f"  Residuallast-Spanne: {min(pr):.1f}–{max(pr):.1f} GW, "
          f"Preis-Spanne: {min(pmax):.0f}–{max(pmax):.0f} EUR/MWh")

    print("\n  Was die aktuellen Bänder rückblickend gesehen hätten:")
    by_band = collections.defaultdict(list)
    for d in days:
        by_band[det.band(meas[d]["peak_resid"])].append(meas[d]["price_max"])
    print(f"    {'Band':10}{'Tage':>5}{'Median':>9}{'Spanne':>20}")
    order = [b for b in ("günstig", "normal", "erhöht", "hoch") if by_band.get(b)]
    for b in order:
        v = by_band[b]
        print(f"    {b:10}{len(v):>5}{statistics.median(v):>9.0f}{min(v):>12.0f}–{max(v):<7.0f}")
    meds = [statistics.median(by_band[b]) for b in order]
    print(f"    -> Mediane {'steigen monoton' if meds == sorted(meds) else 'steigen NICHT monoton'}")

    # Welche Schwelle trennt teuer/billig am besten?
    print("\n  Beste einzelne Schwelle (maximiert Trennung der Tages-Höchstpreise):")
    best = None
    for cut in [x / 2 for x in range(int(min(pr) * 2) + 1, int(max(pr) * 2))]:
        lo = [meas[d]["price_max"] for d in days if meas[d]["peak_resid"] < cut]
        hi = [meas[d]["price_max"] for d in days if meas[d]["peak_resid"] >= cut]
        if len(lo) < 5 or len(hi) < 5:
            continue
        sep = statistics.median(hi) - statistics.median(lo)
        if best is None or sep > best[1]:
            best = (cut, sep, len(lo), len(hi), statistics.median(lo), statistics.median(hi))
    if best:
        cut, sep, nlo, nhi, mlo, mhi = best
        print(f"    {cut:.1f} GW -> unter: {nlo} Tage, Median {mlo:.0f} | "
              f"ab: {nhi} Tage, Median {mhi:.0f} | Abstand {sep:.0f} EUR/MWh")

    print("\n=== STUFE 2 — was kostet das Modell? (rueckblickendes Wetter) ===")
    mod = modelled(det, args.days)
    rows = []
    for d in days:
        if d not in mod or d not in meas:
            continue
        vals = mod[d]
        if len(vals) < 20:
            continue
        # Kalibrierung pro Tag gegen den tatsaechlichen EE-Anteil (grosszuegig).
        # Dieselbe Funktion, die live laeuft — nicht nachgebaut.
        k = det.solve_k(
            statistics.mean(100 * (s + w) / l for s, w, l in vals if l),
            statistics.mean(100 * det.BASE_REN_GW / l for s, w, l in vals if l),
            meas[d]["ren_share"])
        peak = max(l - ((s + w) * k + det.BASE_REN_GW) for s, w, l in vals)
        rows.append((d, peak, meas[d]["peak_resid"], meas[d]["price_max"]))

    if len(rows) >= 10:
        mp = [r[1] for r in rows]
        gp = [r[2] for r in rows]
        px = [r[3] for r in rows]
        err = [abs(a - b) for a, b in zip(mp, gp)]
        print(f"  Tage: {len(rows)}")
        print(f"  Modell vs. gemessene Residuallast : r = {pearson(mp, gp):+.2f}, "
              f"MAE {statistics.mean(err):.1f} GW, max {max(err):.1f} GW")
        print(f"  Modell-Residuallast vs. Preis     : r = {pearson(mp, px):+.2f}"
              f"   (gemessen: {pearson(gp, px):+.2f})")
        bias = statistics.mean(a - b for a, b in zip(mp, gp))
        print(f"  Systematischer Versatz Modell-Ist : {bias:+.1f} GW")
    else:
        print(f"  Nur {len(rows)} Tage mit Modelldaten — zu wenig.")


if __name__ == "__main__":
    main()
