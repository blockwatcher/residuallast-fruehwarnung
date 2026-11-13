#!/usr/bin/env python3
"""Wie gut ist die Warnung wirklich — aufgeschluesselt nach Vorlaufweite?

hindcast.py misst den MODELLfehler: es fuettert das Modell mit rueckblickender
Ist-Einstrahlung. Damit faellt der Wetterprognosefehler heraus. Fuer eine
5-Tage-Warnung ist aber genau der entscheidend — und er waechst mit dem Vorlauf.

Dieses Skript schliesst die Luecke. Open-Meteo archiviert seine frueheren
Prognoselaeufe unter previous-runs-api.open-meteo.com: `..._previous_day3`
liefert fuer jede Stunde, was die Prognose DREI TAGE VORHER gesagt hat. Damit
laesst sich rekonstruieren, was der Detektor an Tag D-3 ausgegeben haette.

Die Kalibrierung wird ueber alle Vorlaufweiten bewusst GLEICH gehalten (pro Tag
gegen den tatsaechlichen EE-Anteil). Damit ist der einzige Unterschied zwischen
den Zeilen der Wetterprognosefehler — das ist die Groesse, die gemessen werden
soll. Die Absolutwerte sind dadurch optimistisch, der VERLAUF ueber den Vorlauf
ist es nicht.

    forecast_skill.py [--days 45] [--max-lead 5]
"""
import argparse
import collections
import datetime as dt
import pathlib
import statistics
import urllib.parse

import hindcast as h

PREV_RUNS = "https://previous-runs-api.open-meteo.com/v1/forecast"


CACHE_DIR = pathlib.Path(__file__).resolve().parent / ".skill-cache"


def get_slow(url, tries=3):
    """Das Archiv der Prognoselaeufe antwortet deutlich langsamer als die
    normale API (gemessen 5-38 s je Abruf) und laeuft gelegentlich in einen
    Timeout. Zwei Konsequenzen:

    - Wiederholen statt aufgeben: ein einzelner Aussetzer soll nicht den ganzen
      Durchlauf kosten.
    - Auf Platte zwischenspeichern: ein Durchlauf sind 30 Abrufe und damit gut
      10 Minuten. Ohne Cache kostet jeder Abbruch und jeder erneute Lauf alles
      noch einmal. Der Schluessel enthaelt das Datum, veraltet also von selbst.
    """
    import hashlib
    import json
    import time
    CACHE_DIR.mkdir(exist_ok=True)
    key = hashlib.sha256(f"{dt.date.today()}|{url}".encode()).hexdigest()[:20]
    cached = CACHE_DIR / f"{key}.json"
    if cached.exists():
        try:
            return json.loads(cached.read_text())
        except Exception:
            cached.unlink(missing_ok=True)
    for attempt in range(tries):
        try:
            data = h.get(url, timeout=180)
            cached.write_text(json.dumps(data))
            return data
        except Exception as e:
            if attempt == tries - 1:
                raise
            print(f"    Wiederholung nach {type(e).__name__} "
                  f"({attempt+1}/{tries-1})", flush=True)
            time.sleep(5 * (attempt + 1))


def modelled_at_lead(det, lead, days_back):
    """{Tag -> [(solar, wind, load)]} aus dem Prognoselauf von vor `lead` Tagen.

    lead=0 ist der beste verfuegbare Stand (Analyse), also das, was hindcast.py
    benutzt — die Obergrenze dessen, was das Modell koennte.
    """
    suffix = "" if lead == 0 else f"_previous_day{lead}"
    rad, wind = f"shortwave_radiation{suffix}", f"wind_speed_100m{suffix}"
    hours = {}
    for idx, (name, lat, lon, ws, ww) in enumerate(det.POINTS, 1):
        print(f"    {idx}/{len(det.POINTS)} {name}", end="\r", flush=True)
        q = urllib.parse.urlencode({
            "latitude": lat, "longitude": lon, "hourly": f"{rad},{wind}",
            "past_days": min(days_back + 2, 92), "forecast_days": 1,
            "timezone": "Europe/Berlin"})
        d = get_slow(f"{PREV_RUNS}?{q}")["hourly"]
        for i, t in enumerate(d["time"]):
            r, w = d[rad][i], d[wind][i]
            if r is None or w is None:
                continue
            e = hours.setdefault(t, {"s": 0., "sw": 0., "w": 0., "ww": 0.})
            e["s"] += min(r / 850., 1.0) * ws
            e["sw"] += ws
            e["w"] += det.wind_cf(w / 3.6) * ww
            e["ww"] += ww
    out = collections.defaultdict(list)
    for t in sorted(hours):
        e = hours[t]
        when = dt.datetime.fromisoformat(t)
        scf = e["s"] / e["sw"] if e["sw"] else 0.
        wcf = e["w"] / e["ww"] if e["ww"] else 0.
        out[when.strftime("%Y-%m-%d")].append(
            (det.PV_GW * scf * det.SOLAR_PR,
             (det.WIND_ON_GW + det.WIND_OFF_GW) * wcf * det.WIND_FLEET,
             det.load_at(when)))
    return out


def pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return float("nan")
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sx = sum((x - mx) ** 2 for x in xs) ** .5
    sy = sum((y - my) ** 2 for y in ys) ** .5
    return cov / (sx * sy) if sx and sy else float("nan")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=45)
    ap.add_argument("--max-lead", type=int, default=5)
    args = ap.parse_args()
    det = h.load_detector()

    end = dt.date.today()
    start = end - dt.timedelta(days=args.days)
    meas = h.measured(start, end)
    print(f"Zeitraum {start} bis {end} — {len(meas)} Tage mit vollstaendigen Ist-Daten\n")

    warn_days_actual = None
    rows_out = []
    for lead in range(0, args.max_lead + 1):
        print(f"  Vorlauf {lead} ...", flush=True)
        mod = modelled_at_lead(det, lead, args.days)
        pk, gm, price, bands = [], [], [], []
        for d in sorted(meas):
            vals = mod.get(d)
            if not vals or len(vals) < 20:
                continue
            # Kalibrierung ueber alle Leads identisch -> Differenz = Wetterfehler.
            k = det.solve_k(
                statistics.mean(100 * (s + w) / l for s, w, l in vals if l),
                statistics.mean(100 * det.BASE_REN_GW / l for s, w, l in vals if l),
                meas[d]["ren_share"])
            peak = max(l - ((s + w) * k + det.BASE_REN_GW) for s, w, l in vals)
            pk.append(peak)
            gm.append(meas[d]["peak_resid"])
            price.append(meas[d]["price_max"])
            bands.append(det.band(peak))
        if len(pk) < 10:
            print(f"Lead {lead}: nur {len(pk)} Tage — uebersprungen")
            continue
        warn = {d for d, b in zip(sorted(meas), bands) if b in ("erhöht", "hoch")}
        if lead == 0:
            warn_days_actual = warn
        mae = statistics.mean(abs(a - b) for a, b in zip(pk, gm))
        agree = (len(warn & warn_days_actual) / max(1, len(warn | warn_days_actual))
                 if warn_days_actual is not None else 1.0)
        rows_out.append((lead, len(pk), pearson(pk, gm), mae, pearson(pk, price),
                         len(warn), agree))

    print(f"{'Vorlauf':>8}{'Tage':>6}{'r vs. Ist-Resid':>17}{'MAE GW':>9}"
          f"{'r vs. Preis':>13}{'Warntage':>10}{'Deckung L0':>12}")
    for lead, n, r_res, mae, r_pr, nwarn, agree in rows_out:
        lbl = "Analyse" if lead == 0 else f"{lead} Tage"
        print(f"{lbl:>8}{n:>6}{r_res:>17.2f}{mae:>9.1f}{r_pr:>13.2f}"
              f"{nwarn:>10}{100*agree:>11.0f}%")
    print("\nAnalyse = bester verfuegbarer Stand (kein Prognosefehler) = Obergrenze.")
    print("Deckung L0 = Jaccard-Ueberschneidung der Warntage mit der Analyse-Zeile:")
    print("  wie oft haette die Warnung bei diesem Vorlauf dieselben Tage getroffen.")


if __name__ == "__main__":
    main()
