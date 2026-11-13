# Residuallast-Frühwarnung

Frühwarnung vor teuren Strom-Tagen in Deutschland mit bis zu vier Tagen Vorlauf.
Sie schätzt aus Wetterprognosen die **Residuallast** (Last minus Erneuerbare) und
meldet, wenn ein Tag voraussichtlich teuer wird.

Der Zweck: Ein Day-Ahead-Optimierer wie [Akkudoktor-EOS](https://github.com/Akkudoktor-EOS/EOS)
sieht die Börsenpreise erst 24–48 Stunden vorher. Wer Hausspeicher und Auto vor
einer teuren Phase gezielt vollhalten will, braucht die Information früher. Die
Warnung steuert nichts, sie sagt nur „bereite dich vor".

Entstanden aus einem Thread im Akkudoktor-Forum („Residuallast-Frühwarnung:
5 Tage Vorlauf vor teuren Tagen — Methodik-Check erbeten"). Eine deutlich
weiterentwickelte Variante mit gelerntem Wetter- und Preismodell läuft auf
[DVhub Online](https://dvhub.online/v1/residual-load/outlook).

**Stand:** Hobbyprojekt im Dauerbetrieb seit August 2026. Die bekannten
Schwächen stehen unten; wer das nachbaut, sollte sie vorher lesen.

## Wie es rechnet

1. **Wetter:** Open-Meteo, `shortwave_radiation` und `wind_speed_100m` an sechs
   Punkten in Deutschland mit festen Gewichten (Tabelle `POINTS` in
   `detector.py`).
2. **Erzeugung:** Solar linear aus der Globalstrahlung (gedeckelt bei 850 W/m²),
   Wind über eine vereinfachte Leistungskurve (3 / 12 / 25 m/s). Hochgerechnet
   auf die installierte Leistung, mit Flottenfaktoren 0,46 (Solar) und 0,44
   (Wind), plus 6,5 GW Sockel aus Biomasse, Wasser und Müll.
3. **Last:** Stundenprofil aus der gemessenen Last der letzten 30 Tage
   (energy-charts), getrennt nach Werktag und Wochenende.
4. **Kalibrierung:** Ein Faktor `k` auf Solar und Wind, so gewählt, dass der
   Erneuerbaren-Anteil der ersten 24 Stunden zu `ren_share_forecast` von
   energy-charts passt.
5. **Auslöser:** Gewarnt wird, wenn das prognostizierte Tagesmittel der
   Residuallast über dem 80-%-Quantil der Prognosen gleichen Vorlaufs für die
   28 Tage davor liegt. Die Historie kommt aus `validation-forecasts.jsonl`;
   `band_validation.py` muss dafür täglich laufen.
6. **Unsicherheit:** Dieselbe Rechnung für die 40 Mitglieder des
   ICON-EPS-Ensembles, als Streuband im Report. Bis zum 13.11.2026 war das
   Ensemble der Auslöser (mindestens 70 % der Mitglieder über 42 GW
   Tagesspitze); das gilt weiter als Rückfall, wenn die Historie fehlt.

## Dateien

| Datei | Zweck |
|---|---|
| `detector.py` | Prognose und Ensemble rechnen, `report.json` schreiben, optional Sensoren an Home Assistant schicken |
| `ensemble.py` | Ensemble-Ansicht auf der Kommandozeile |
| `band_validation.py` | Täglich Prognose, EpexPredictor und Ist-Preise mitschreiben; `--report` wertet aus |
| `hindcast.py` | Rückblick mit Ist-Wetter: Erklärt die Residuallast den Preis? |
| `forecast_skill.py` | Güte nach Vorlauf, aus archivierten Prognoseläufen |
| `abgleich_relativ.py` | Feste Schwelle gegen relative Einstufung, am Live-Protokoll |
| `validation-forecasts.jsonl` | Live-Protokoll: was das Modell seit dem 16.08.2026 jeweils vorhergesagt hat |
| `ha-lovelace-card.yaml`, `ha_dashboard.cjs` | Darstellung in Home Assistant |

## Benutzen

Python 3.9 oder neuer, keine Fremdbibliotheken.

```
python3 detector.py          # rechnet und schreibt report.json
python3 ensemble.py          # Verteilung je Tag anzeigen
python3 band_validation.py   # einen Schnappschuss ins Protokoll schreiben
```

`detector.py` schickt zwei Sensoren an Home Assistant, wenn `HASS_URL` und
`HASS_TOKEN` (oder `HASS_TOKEN_FILE`) gesetzt sind. Ohne diese Variablen
entfällt der Schritt. Der Alarmtext landet in `report.json` (`alert_needed`,
`alert_text`); verschicken muss ihn ein eigener Dienst. Die Empfehlung im
Alarmtext ist auf eine bestimmte Anlage zugeschnitten und sollte angepasst
werden.

## Datenquellen

- **[Open-Meteo](https://open-meteo.com)**: Prognose, Ensemble
  (`ensemble-api`) und archivierte Prognoseläufe (`previous-runs-api`). Die
  freie API ist nur für nichtkommerzielle Nutzung.
- **[energy-charts](https://api.energy-charts.info)** (Fraunhofer ISE):
  `ren_share_forecast` als Kalibrieranker (reicht nur bis zum Folgetag),
  `public_power` für Last und gemessene Residuallast, `price` für Börsenpreise.
- **[EpexPredictor](https://github.com/b3nn0/EpexPredictor)**: freie
  5-Tage-Preisprognose, dient in der Validierung als Vergleich.

## Was die Auswertungen ergeben haben

**Das Konzept trägt.** Die gemessene Residuallast (Tagesmittel) korreliert von
Juli bis Mitte November 2026 mit r = 0,93 mit dem Tagesmittelpreis. Das
Tagesmittel erklärt den Preis deutlich besser als die Tagesspitze.

**Die Wetterprognose hält über vier Tage.** Der Fehler der Residuallast wächst
mit dem Vorlauf kaum.

**Vergleich der Auslöser** (Vorlauf 2–4 Tage, 15.09.–12.11.2026, 171 Paare;
teuer = Tagesmittelpreis über dem 80-%-Quantil der letzten 28 Tage):

| Auslöser | F1 | Präzision | Warnquote |
|---|---|---|---|
| fest: Spitze ≥ 42 GW | 0,76 | 0,77 | 43 % |
| Ensemble ≥ 70 % über 42 GW (Auslöser bis 13.11.2026) | 0,70 | 0,73 | 39 % |
| relativ: Tagesmittel über P80 der letzten 28 Tage (Auslöser seit 13.11.2026) | 0,79 | 0,90 | 34 % |
| EpexPredictor | 0,83 | 0,83 | 44 % |

In diesem Zeitraum stiegen die Preise durchgehend, 25 von 58 Tagen gelten als
teuer. Die Werte sind deshalb nur untereinander vergleichbar.

## Bekannte Schwächen

- **„Teuer" heißt teuer gegenüber den letzten vier Wochen.** Die relative
  Einstufung warnt zu jeder Jahreszeit an grob einem Fünftel der Tage, auch im
  Sommer bei absolut niedrigen Preisen. Sie ersetzt seit dem 13.11.2026 die
  feste 42-GW-Schwelle, die auf 45 Sommertagen gefittet war und im November an
  71 % der Prognosen überschritten wurde (September: 22 %).
- **Die relative Einstufung ist erst an zwei Herbstmonaten geprüft.** Der
  Vorsprung vor der festen Schwelle ist in diesem Zeitraum nicht gesichert.
- **Das Ensemble war als Auslöser nicht besser als die schlichte Schwelle.** Es
  liefert eine Unsicherheitsangabe, mehr bisher nicht.
- **Der Wind-Flottenfaktor 0,44 ist ein Sommerwert.** Er stammt aus der
  gemessenen Spitze eines 30-Tage-Fensters im Sommer (35,6 GW); im Herbst lag
  die Spitze bei 53,6 GW. Die Kalibrierung fängt das auf (`k` stieg von 0,97
  auf 1,39), ohne den Anker wäre der Wind deutlich zu niedrig.
- **Versatz:** Das Modell liegt bei jedem Vorlauf 6–7 GW unter der gemessenen
  Residuallast. Für Schwellen, die auf der Modellausgabe gefittet sind, ist das
  unkritisch; für den Vergleich mit anderen Modellen nicht.
- **Das Lastprofil kennt weder Temperatur noch Feiertage.**
- **Der Preis hängt nicht nur an der Residuallast.** Bei gleicher Residuallast
  lagen die Preise im Oktober/November rund 40 EUR/MWh über Juli/August.
  Brennstoffpreise, Im- und Export, Must-run und Pumpspeicher sind nicht
  modelliert.
- **Kommentare im Code sind teils überholt.** Sie dokumentieren den Stand vom
  August 2026, etwa die Begründung der Flottenfaktoren.

## Fallen für Nachbauer

- **Obergrenzen gegen gemessene Maxima prüfen.** Ein zu hohes Lastprofil und zu
  hohe Erzeugungsgrenzen können sich im Verhältnis fast aufheben. Ein
  gemeinsamer Kalibrierfaktor kaschiert das und kann ein falsches
  Mischungsverhältnis von Wind und Solar nie korrigieren.
- **Zeitzone:** Open-Meteo mit `timezone=Europe/Berlin` abrufen, wenn die
  Stunden in ein deutsches Lastprofil gehen.
- **Archivierte Vorläufe sind kein Ensemble.** Bei 4–5 Tagen Vorlauf füllt die
  API ältere Läufe mit dem aktuellen auf; eine Abstimmung darüber ist immer
  einstimmig.
- **Die Ensemble-API liefert Wind nur in 10 m und 80 m.** Das Verhältnis zu
  100 m ist ortsabhängig (gemessen 1,03 an der Küste bis 1,16 im Binnenland).
- **energy-charts:** Die Website zeigt mehrere Tage Prognose, die API nur den
  Folgetag.

## Lizenz

MIT, siehe `LICENSE`.
