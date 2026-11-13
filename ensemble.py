#!/usr/bin/env python3
"""Ensemble-Ansicht auf der Kommandozeile.

Duenner Aufsatz auf detector.py — die Rechnung liegt dort (`fetch_ensemble`,
`ensemble_stats`), damit sie nur einmal existiert. Ein Nachbau hier waere die
dritte Kopie derselben Formeln; die ersten beiden sind heute schon
auseinandergelaufen und haben ein Modell gemessen, das es nicht mehr gab.

Hintergrund und Begruendung der Methode stehen im Kopf von detector.py bei
`fetch_ensemble` und `ALERT_P`.

    ensemble.py [--json]
"""
import argparse
import importlib.util
import json
import pathlib

HERE = pathlib.Path(__file__).resolve().parent


def load_detector():
    spec = importlib.util.spec_from_file_location("det", HERE / "detector.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    det = load_detector()

    anchor = det.http_json("https://api.energy-charts.info/ren_share_forecast?country=de")
    ren = [x for x in anchor.get("ren_share", []) if x is not None]
    stats = det.ensemble_stats(sum(ren) / len(ren) if ren else None)
    if not stats:
        raise SystemExit("Ensemble nicht abrufbar")

    if args.json:
        print(json.dumps(stats, ensure_ascii=False, indent=1))
        return

    cut = det.BANDS[1][0]
    print(f"ICON-EPS | Warnschwelle {cut} GW | Alarm ab {det.ALERT_P} %\n")
    print(f"{'Tag':12}{'P10':>7}{'Median':>8}{'P90':>7}{'Spanne':>8}{'P(Warnung)':>12}   Einordnung")
    for day in sorted(stats):
        e = stats[day]
        if e["p_warn"] >= det.ALERT_P:
            txt = "warnen"
        elif e["unentscheidbar"]:
            txt = "unsicher — Szenarien spalten sich"
        elif e["p_warn"] > 10:
            txt = "eher ruhig"
        else:
            txt = "ruhig"
        print(f"{day:12}{e['p10']:>7.1f}{e['median']:>8.1f}{e['p90']:>7.1f}"
              f"{e['spread']:>8.1f}{e['p_warn']:>11} %   {txt}")
    print("\nP(Warnung) = Anteil der Szenarien über der Schwelle.")


if __name__ == "__main__":
    main()
