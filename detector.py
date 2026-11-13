#!/usr/bin/env python3
# Dunkelflaute-/Preisspitzen-Fruehwarnung DE (4-Tage-Vorlauf) — v1 advisory.
# Metrik: Residuallast = Last - Erneuerbare (kalibriert an energy-charts 24h ren_share).
# Ausgaben: report.json + HA-Sensoren (REST). Den Alarmtext verschickt ein nachgelagerter Dienst.
import json, os, statistics, urllib.request, urllib.parse, datetime, zoneinfo

HERE=os.path.dirname(os.path.abspath(__file__))
STATE_FILE=os.path.join(HERE,"state.json"); REPORT=os.path.join(HERE,"report.json")

# Installierte Leistung nach api.energy-charts.info/installed_power (Fraunhofer ISE),
# Stand 08.2026. PV als DC-Leistung (AC waeren 116,5) — der 0.85-Faktor unten deckt
# Performance und WR-Kappung mit ab. Bei Gelegenheit nachziehen, die Werte altern.
PV_GW,WIND_ON_GW,WIND_OFF_GW=128.0,70.6,10.9
# Flottenfaktoren, gemessen 16.08.2026 gegen 30 Tage public_power.
#
# Ohne sie erreicht das Modell Obergrenzen von rund dem DOPPELTEN der real je
# gemessenen Spitze: Solar 108,8 gegen 58,5 GW (1,86x), Wind 81,5 gegen 35,6 GW
# (2,29x). Zwei Gruende:
#   Solar — min(rad/850,1) erreicht CF=1,0, sobald die Globalstrahlung 850 W/m²
#           erreicht, was im Sommer regelmaessig passiert. Eine reale Anlage
#           liefert dann nie ihre DC-Nennleistung (Modultemperatur, Anstellwinkel,
#           Verschmutzung, WR-Kappung). Gemessene Spitze / installierte DC-Leistung
#           = 58,5/128,0 = 0,46.
#   Wind  — sechs Stuetzpunkte auf CF 1,0 bedeuten im Modell Volllast fuer die
#           ganze deutsche Flotte. Real glaettet die raeumliche Verteilung stark;
#           gemessene Spitze / installierte Leistung = 35,6/81,5 = 0,44.
#
# Warum das lange unentdeckt blieb: Das alte Lastprofil war um 8-10 GW zu hoch,
# beide Fehler hoben sich im VERHAELTNIS weitgehend auf. Erst mit gemessener Last
# lief k in seinen Klemmwert 0,50 — der Beleg, dass hier etwas Grundlegendes
# schiefliegt. Und k kann das ohnehin nie sauber richten: es skaliert Solar und
# Wind GEMEINSAM, ein falsches Mischungsverhaeltnis bleibt.
SOLAR_PR=0.46      # ersetzt den fruehreren pauschalen 0,85er-Faktor
WIND_FLEET=0.44
# Sockel = gemessene mittlere ERZEUGUNG aus Biomasse+Laufwasser+Speicherwasser+Muell
# (ohne Pumpspeicher), nicht installierte Leistung: 6,3 GW Sommer / 6,8 Fruehjahr /
# 7,1 GW Winter (public_power, je 14 Tage 2026). Geht ungewichtet in die Summe ein,
# wird also NICHT von k skaliert — siehe Kommentar bei der Kalibrierung.
BASE_REN_GW=6.5
# Notnagel-Lastprofil. Wird nur benutzt, wenn die gemessene Kurve nicht abrufbar
# ist — es ueberschaetzt die reale Last nachweislich um 8-10 GW (gemessen
# 16.08.2026 gegen energy-charts: werktags -7,9, am Wochenende -9,8 GW im Mittel,
# stundenabhaengig von -2,3 nachts bis -13,4 GW abends).
LOAD_FALLBACK=[48,46,45,45,46,49,55,62,66,68,69,70,69,68,67,66,66,68,70,69,66,62,57,52]; WEEKEND_F=0.88
# Gemessenes Lastprofil, rollierend aus den letzten 30 Tagen. Passt sich damit
# der Jahreszeit von selbst an, statt ein Sommerprofil festzuverdrahten, das im
# Winter wieder falsch waere.
PROFILE_FILE=os.path.join(HERE,"load-profile.json")
PROFILE_WINDOW_DAYS=30
PROFILE_MAX_AGE_H=72
TZ=zoneinfo.ZoneInfo("Europe/Berlin")
POINTS=[("Nord/Kueste",53.9,8.1,0.10,0.32),("Ostsee",54.3,13.1,0.05,0.16),
        ("Hannover",52.4,9.7,0.15,0.20),("NRW",51.4,7.0,0.15,0.14),
        ("Frankfurt",50.1,8.7,0.20,0.10),("Muenchen",48.1,11.6,0.35,0.08)]
# Schwellen am 16.08.2026 erstmals GEFITTET statt geschaetzt — gegen 45 Tage
# Ist-Boersenpreise, auf der Ausgabe DIESES Modells (nicht auf der gemessenen
# Residuallast; das Modell hat einen konstanten Versatz, der so herausfaellt).
#
# Die Warnschwelle 42,0 GW sticht klar heraus:
#     Schwelle  Warntage  Quote  Median warn  Median ruhig  Differenz
#       38,0       20      44%      217           169          47
#       40,0       14      31%      217           177          39
#       42,0        6      13%      322           188         134   <-
#       43,0        3       7%      340           191         149  (zu duenn)
# Unterhalb 42 bricht die Trennschaerfe auf 39-48 EUR/MWh ein, oberhalb bleiben
# zu wenige Tage fuer eine belastbare Aussage. 13 % Warntage ist ausserdem eine
# brauchbare Alarmquote — die alten Schwellen haetten auf der neuen Skala an
# 71 % der Tage gewarnt.
#
# ACHTUNG: nur Sommertage. Die Modell-Spitze lag im Zeitraum bei max. 45,0 GW,
# oberhalb davon ist "hoch" unbelegt. Die laufende Validierung sammelt Winter.
BANDS=[(33,"günstig"),(42,"normal"),(46,"erhöht"),(999,"hoch")]

# Zweite Kenngroesse, ergaenzt am 16.08.2026 nach dem 45-Tage-Rueckblick
# (hindcast.py). Befund: die TAGES-SPITZE ist die schlechtere Groesse. Gegen den
# tatsaechlichen Boersenpreis korreliert
#     Spitze  vs. Tages-Hoechstpreis  r = +0,56
#     Spitze  vs. Tages-Mittelpreis   r = +0,79
#     MITTEL  vs. Tages-Mittelpreis   r = +0,86   <- deutlich am besten
# (getrimmt +0,80, also nicht ausreisergetrieben; Regression Preis ~ 39 + 3,40*GW)
#
# Schwellen aus den Quartilen der gemessenen mittleren Residuallast (02.07.–16.08.,
# 45 Tage): Q25 17,2 / Q50 19,8 / Q75 25,6 GW. Die zugehoerigen Median-Preise
# steigen sauber monoton: 76 / 100 / 120 / 131 EUR/MWh.
#
# ACHTUNG 1: nur SOMMER-Daten. Im Zeitraum gab es keine Dunkelflaute, oberhalb
# ~35 GW Mittel ist nichts belegt — ausgerechnet der Zielfall.
#
# ACHTUNG 2, wichtiger: Das Mittel ist zwar der bessere PROGNOSTIKER, aber die
# empfindlichere GROESSE. Gemessen am 16.08.:
#     Spitze: Modell 21,9–51,5 vs. gemessen 19,2–52,2 GW, Versatz +0,6, MAE 3,1
#     Mittel: Modell  1,8–29,8 vs. gemessen  8,3–34,6 GW, Versatz -4,4, MAE 4,4
# Und der LIVE-Lauf liefert Mittelwerte von 39 GW — ueber allem je Gemessenen.
# Grund: die Tagesspitze faellt abends, wenn Solar ohnehin nahe null ist, dort
# schlaegt ein falsches k kaum durch. Der Mittelwert enthaelt die Mittagsstunden,
# in denen Solar dominiert — dort wirkt k voll. Das Mittel erbt damit die
# Anker-Schwaeche (k aus 24 h auf 5 Tage) fast ungedaempft.
# Konkret am 16.08.: band_mean stufte 17./18.08. als "hoch" ein, waehrend die
# Preisprognose dort 16–18 ct sah und den 20.08. mit 27 ct als teuersten Tag.
#
# Deshalb loest der Alarm bewusst WEITERHIN ueber die Spitze aus; band_mean wird
# vorerst nur mitberichtet und von band_validation.py mitgeschrieben. Umstellen
# erst, wenn die Kalibrierung repariert ist UND Winterdaten vorliegen.
# Nachgezogen 16.08.2026 auf die korrigierte Modellskala, gefittet gegen den
# Tages-MITTELPREIS (nicht den Hoechstpreis) — das ist die Groesse, mit der das
# Mittel korreliert. Auf der neuen Skala sogar besser als die gemessene
# Residuallast selbst: r = +0,89 gegen +0,86.
#
# Der Versatz gegen die Messung betraegt -6,4 GW (Modell 2,1-26,4 vs. gemessen
# 8,3-34,6 GW), faellt aber heraus, weil auf der MODELLAUSGABE gefittet wird.
#
# Aufteilung 7,5 / 18,5 / 20,5 GW, monoton ueber alle vier Baender mit je >=6
# Tagen (45-Tage-Basis, Median Tages-Mittelpreis gesamt 113 EUR/MWh):
#     günstig   7 Tage   Median  71 EUR/MWh
#     normal   26 Tage   Median 108
#     erhöht    6 Tage   Median 124
#     hoch      6 Tage   Median 139
# Warnquote 27 %. Die Spreizung (68 EUR/MWh) wirkt kleiner als beim Peak-Band
# (134), weil der Tages-MITTELpreis eine geringere Dynamik hat als der
# Hoechstpreis — nicht, weil die Trennung schlechter waere.
#
# WICHTIGER VORBEHALT, gemessen beim ersten Live-Lauf danach: Die Prognosewerte
# liegen mit 19,1-36,7 GW deutlich UEBER dem Bereich, auf dem gefittet wurde
# (Modell 2,1-26,4 GW). Folge: 4 von 5 Tagen landen auf "hoch". Das ist kein
# Fehler der Schwellen, sondern eine Luecke im Fit-Sample — die 45 Sommertage
# enthielten kaum Tage mit ~30 % EE-Anteil, die Prognose sieht aber gerade
# welche. Oberhalb 26 GW ist schlicht nichts belegt.
# Solange band_mean nichts ausloest, ist das unschaedlich; die laufende
# Validierung sammelt genau diese Faelle. Vor einer Umstellung auf das Mittel
# muss mit Daten aus dem echten Wertebereich neu gefittet werden.
BANDS_MEAN_FIT_RANGE=(2.1,26.4)   # Bereich, auf dem die Schwellen belegt sind
BANDS_MEAN=[(7.5,"günstig"),(18.5,"normal"),(20.5,"erhöht"),(999,"hoch")]

def band(pr):
    for lim,name in BANDS:
        if pr<lim: return name
    return "hoch"

def band_mean(mr):
    for lim,name in BANDS_MEAN:
        if mr<lim: return name
    return "hoch"

def http_json(url):
    req=urllib.request.Request(url,headers={"User-Agent":"residuallast-fruehwarnung/2.1"})
    with urllib.request.urlopen(req,timeout=30) as r: return json.load(r)

def wind_cf(v):
    if v<3 or v>25: return 0.0
    if v>=12: return 1.0
    return (v**3-27)/(1728-27)

def fetch():
    hours={}
    for name,lat,lon,ws,ww in POINTS:
        q=urllib.parse.urlencode({"latitude":lat,"longitude":lon,
            # Ortszeit statt UTC (korrigiert 16.08.2026): die Zeitstempel gehen
            # direkt in load_at() und in die Tagesbildung. Mit UTC lief das
            # deutsche Lastprofil im Sommer 2 h phasenverschoben, und die
            # "Tage" begannen um 02:00 Ortszeit. open-meteo rechnet die
            # Sommerzeit selbst mit — besser als eigene Zeitzonenarithmetik.
            "hourly":"shortwave_radiation,wind_speed_100m","forecast_days":5,
            "timezone":"Europe/Berlin"})
        d=http_json("https://api.open-meteo.com/v1/forecast?"+q)["hourly"]
        for i,t in enumerate(d["time"]):
            rad=d["shortwave_radiation"][i] or 0.0; wnd=(d["wind_speed_100m"][i] or 0.0)/3.6
            e=hours.setdefault(t,{"s":0.,"sw":0.,"w":0.,"ww":0.})
            e["s"]+=min(rad/850.,1.0)*ws; e["sw"]+=ws; e["w"]+=wind_cf(wnd)*ww; e["ww"]+=ww
    return hours

ENSEMBLE_API="https://ensemble-api.open-meteo.com/v1/ensemble"
ENS_MODEL="icon_seamless"
# Ab dieser Wahrscheinlichkeit wird gewarnt. Bewusst hoch: Die alte Ampel warnte
# bei jedem Ueberschreiten einer Schwelle, deren Abstand kleiner war als der
# eigene Fehler — Ergebnis waren 43 % Uebereinstimmung mit der Analyse schon bei
# einem Tag Vorlauf. Lieber seltener warnen und dann verlaesslich.
ALERT_P=70
# Das Ensemble liefert Wind nur in 10 m und 80 m, 100 m kommt leer zurueck.
# v100/v80 am 16.08.2026 an der deterministischen API gemessen statt per
# Potenzgesetz angenommen: 1,026 (Kueste) bis 1,163 (Binnenland) gegenueber
# 1,032 nach Faustformel — deutlich daneben und ortsabhaengig.
WIND_80_TO_100={"Nord/Kueste":1.031,"Ostsee":1.111,"Hannover":1.163,
                "NRW":1.026,"Frankfurt":1.156,"Muenchen":1.132}


def quantile(sorted_vals,q):
    if not sorted_vals: return None
    i=q*(len(sorted_vals)-1); lo=int(i); hi=min(lo+1,len(sorted_vals)-1)
    return sorted_vals[lo]+(sorted_vals[hi]-sorted_vals[lo])*(i-lo)


def fetch_ensemble(days=6):
    """{Tag -> {Mitglied -> [(solar, wind, load)]}} aus ICON-EPS, 40 Mitglieder.

    Mitglieder werden ueber die Stuetzpunkte NACH INDEX kombiniert: Mitglied 7
    an der Kueste und Mitglied 7 in Muenchen stammen aus derselben Stoerung und
    gehoeren zum selben Szenario. Erst raeumlich mitteln und dann Szenarien
    bilden wuerde genau das Muster zerstoeren, das eine Dunkelflaute ausmacht.
    """
    per_point=[]
    for name,lat,lon,ws,ww in POINTS:
        q=urllib.parse.urlencode({"latitude":lat,"longitude":lon,
            "hourly":"shortwave_radiation,wind_speed_80m","models":ENS_MODEL,
            "forecast_days":days,"timezone":"Europe/Berlin"})
        d=http_json(ENSEMBLE_API+"?"+q)["hourly"]
        rad=sorted(k for k in d if k.startswith("shortwave_radiation"))
        wnd=sorted(k for k in d if k.startswith("wind_speed_80m"))
        per_point.append((d["time"],[d[k] for k in rad],[d[k] for k in wnd],
                          ws,ww,WIND_80_TO_100.get(name,1.103)))
    n=min(len(p[1]) for p in per_point); times=per_point[0][0]
    out={}
    for m in range(n):
        for i,t in enumerate(times):
            s=sw=w=wwsum=0.0; ok=True
            for _,rads,wnds,ws,ww,h80 in per_point:
                r,v=rads[m][i],wnds[m][i]
                if r is None or v is None: ok=False; break
                s+=min(r/850.,1.0)*ws; sw+=ws
                w+=wind_cf(v*h80/3.6)*ww; wwsum+=ww
            if not ok: continue
            when=datetime.datetime.fromisoformat(t)
            out.setdefault(when.strftime("%Y-%m-%d"),{}).setdefault(m,[]).append((
                PV_GW*(s/sw if sw else 0)*SOLAR_PR,
                (WIND_ON_GW+WIND_OFF_GW)*(w/wwsum if wwsum else 0)*WIND_FLEET,
                load_at(when)))
    return out,n


def ensemble_stats(target):
    """{Tag -> {p10, median, p90, spread, p_warn, unklar}} oder None.

    Faellt die Quelle aus, gibt es None und der Aufrufer bleibt beim
    deterministischen Band — eine fehlende Zweitmeinung darf die Warnung nicht
    komplett ausfallen lassen.
    """
    try:
        days,n=fetch_ensemble()
    except Exception as e:
        print("Ensemble nicht abrufbar (%s) — bleibe beim deterministischen Band"%e)
        return None
    cut=BANDS[1][0]
    stats={}
    for day,members in days.items():
        peaks=[]
        for vals in members.values():
            if len(vals)<20: continue
            k=solve_k(statistics.mean(100*(s+w)/l for s,w,l in vals if l),
                      statistics.mean(100*BASE_REN_GW/l for s,w,l in vals if l),target)
            peaks.append(max(l-((s+w)*k+BASE_REN_GW) for s,w,l in vals))
        if len(peaks)<5: continue
        peaks.sort()
        p10,p90=quantile(peaks,0.10),quantile(peaks,0.90)
        pw=round(100*sum(1 for p in peaks if p>=cut)/len(peaks))
        stats[day]={"members":len(peaks),"p10":round(p10,1),
                    "median":round(quantile(peaks,0.50),1),"p90":round(p90,1),
                    "spread":round(peaks[-1]-peaks[0],1),
                    "p_warn":pw,
                    # "Unentscheidbar" heisst: die Mitgliederschaft spaltet sich
                    # wirklich. Frueher stand hier p10<=Schwelle<=p90 — das war zu
                    # grob: Am 20.08. lag P10 nur 0,6 GW unter der Schwelle, 88 %
                    # der Szenarien darueber, und der Tag hiess trotzdem
                    # "unsicher", waehrend der Alarm ausloeste. Zwei Ansichten,
                    # zwei Aussagen. Jetzt haengt beides an derselben Zahl.
                    "unentscheidbar":25<=pw<ALERT_P}
    return stats


def solve_k(sw_share, base_share, target):
    """Exakte Loesung von: ziel = k * anteil(solar+wind) + anteil(sockel).

    Bewusst eine eigene Funktion, damit hindcast.py exakt dieselbe Regel misst,
    die live laeuft. Eine duplizierte Kopie waere frueher oder spaeter eine
    andere Regel — genau das ist hier schon zweimal passiert.
    """
    if not target or sw_share<=0: return 1.0
    return max(0.5,min(2.0,(target-base_share)/sw_share))


def build_load_profile():
    """Stundenmittel der gemessenen Last, getrennt nach Werktag/Wochenende.

    Ersetzt den Wochenend-Skalar durch zwei echte Kurven — das Wochenende ist
    nicht der Werktag mal 0,88, es hat einen anderen Tagesgang.
    """
    end=datetime.date.today(); start=end-datetime.timedelta(days=PROFILE_WINDOW_DAYS)
    d=http_json("https://api.energy-charts.info/public_power?country=de"
                "&start=%s&end=%s"%(start,end))
    load=next((p["data"] for p in d.get("production_types",[]) if p["name"]=="Load"),None)
    if not load: raise ValueError("energy-charts liefert keine Load-Reihe")
    buckets={}
    for ts,v in zip(d["unix_seconds"],load):
        if v is None: continue
        loc=datetime.datetime.fromtimestamp(ts,TZ)
        buckets.setdefault(("we" if loc.weekday()>=5 else "wd",loc.hour),[]).append(v/1000.0)
    prof={}
    for kind in ("wd","we"):
        vals=[]
        for h in range(24):
            b=buckets.get((kind,h))
            # Lieber gar kein Profil als ein loechriges: ein einzelner duenn
            # belegter Stundeneimer wuerde eine Delle in die Kurve reissen.
            if not b or len(b)<8: raise ValueError("Stunde %s/%d nur %d Werte"%(kind,h,len(b or [])))
            vals.append(round(sum(b)/len(b),2))
        prof[kind]=vals
    prof["updated"]=datetime.datetime.utcnow().isoformat()+"Z"
    prof["window_days"]=PROFILE_WINDOW_DAYS
    return prof

_PROFILE=[]   # leere Liste = noch nicht ermittelt; None = bewusst kein Profil

def get_load_profile():
    global _PROFILE
    if _PROFILE!=[]: return _PROFILE
    cached=None
    try: cached=json.load(open(PROFILE_FILE))
    except Exception: pass
    stale=True
    if cached:
        try:
            age=(datetime.datetime.utcnow()
                 -datetime.datetime.fromisoformat(cached["updated"].rstrip("Z"))).total_seconds()/3600
            stale=age>PROFILE_MAX_AGE_H
        except Exception: pass
    if stale:
        try:
            fresh=build_load_profile()
            json.dump(fresh,open(PROFILE_FILE,"w"),indent=1)
            cached=fresh
            print("Lastprofil erneuert (%d Tage): Werktag %.1f-%.1f GW, Wochenende %.1f-%.1f GW"%(
                fresh["window_days"],min(fresh["wd"]),max(fresh["wd"]),
                min(fresh["we"]),max(fresh["we"])))
        except Exception as e:
            print("Lastprofil-Update fehlgeschlagen (%s) — nutze %s"%(
                e,"vorhandenen Cache" if cached else "Notnagel-Profil"))
    _PROFILE=cached
    return _PROFILE

def load_at(dt):
    """Last in GW. `dt` MUSS deutsche Ortszeit sein (open-meteo liefert sie so)."""
    p=get_load_profile()
    if p: return p["we" if dt.weekday()>=5 else "wd"][dt.hour]
    b=LOAD_FALLBACK[dt.hour]; return b*WEEKEND_F if dt.weekday()>=5 else b

def main():
    hours=fetch()
    raw=[]
    for t in sorted(hours):
        e=hours[t]; dt=datetime.datetime.fromisoformat(t)
        scf=e["s"]/e["sw"] if e["sw"] else 0.; wcf=e["w"]/e["ww"] if e["ww"] else 0.
        raw.append({"dt":dt,"t":t,"solar":PV_GW*scf*SOLAR_PR,
                    "wind":(WIND_ON_GW+WIND_OFF_GW)*wcf*WIND_FLEET,"L":load_at(dt)})
    # Kalibrierung an energy-charts.
    #
    # ACHTUNG, offener Punkt: k wird aus einer Summe INKLUSIVE BASE_REN_GW
    # bestimmt, unten aber nur auf (solar+wind) angewendet. Heute trifft das
    # Ergebnis sein Ziel auf 0,1 pp — aber weil sich zwei Unsauberkeiten
    # zufaellig aufheben, nicht by design. Je kleiner das EE-Dargebot, desto
    # staerker schlaegt der starre Sockel durch: bei halbem Dargebot +3,0 pp,
    # in einer Winter-Dunkelflaute +6,4 pp zu hoch. Das Modell ueberschaetzt die
    # Erneuerbaren also ausgerechnet in der Dunkelflaute und warnt zu spaet.
    # Bewusst noch nicht geaendert — die richtige Korrektur ist die Frage an die
    # Akkudoktor-Community (Faktor auf die Gesamtsumme? getrennte Faktoren?).
    try:
        ec=http_json("https://api.energy-charts.info/ren_share_forecast?country=de")
        ren=[x for x in ec.get("ren_share",[]) if x is not None]
        target=sum(ren)/len(ren) if ren else None
        anchor={"min":round(min(ren),1),"avg":round(target,1)} if ren else {"err":"leer"}
    except Exception as ex: target=None; anchor={"err":str(ex)}
    # k wird jetzt exakt geloest statt geschaetzt (repariert 16.08.2026).
    #
    # Vorher: ms war ein ENERGIEGEWICHTETER Anteil (Summe ren / Summe Last),
    # verglichen wurde er aber gegen `target` — und energy-charts liefert dort
    # das ARITHMETISCHE Mittel der Stundenanteile. Zwei verschiedene Metriken.
    # Zusaetzlich wurde k aus einer Summe inklusive Sockel bestimmt, dann aber
    # nur auf (solar+wind) angewendet. Dass das Ergebnis sein Ziel trotzdem traf,
    # lag daran, dass sich beide Fehler zufaellig aufhoben — nicht by design.
    #
    # Jetzt: dieselbe Metrik wie das Ziel, und k so geloest, dass der kalibrierte
    # arithmetische Mittelwert das Ziel EXAKT trifft, bei unskaliertem Sockel:
    #     ziel = k * anteil(solar+wind) + anteil(sockel)
    # Der Sockel bleibt bewusst unskaliert: er ist gemessen und gut bekannt
    # (6,3–7,1 GW ueber die Jahreszeiten), die Unsicherheit steckt im
    # wetterabhaengigen Teil. Eine gut bekannte Konstante mit einem
    # wetterabhaengigen Faktor zu skalieren waere falsch.
    #
    # Gegen die Alternative "k auf die Gesamtsumme" ueber 45 Tage und alle
    # Vorlaufweiten getestet — die ist messbar schlechter:
    #     Regel                  Peak-Bias  Peak-MAE  Mittel-MAE
    #     bisher (zufaellig ok)      +0,8       3,2        5,0
    #     Gesamtsumme                +1,7       3,6        5,3
    #     diese hier                 +0,7       3,2        5,2
    f=raw[:24]
    k=solve_k(statistics.mean(100*(s["solar"]+s["wind"])/s["L"] for s in f if s["L"]),
              statistics.mean(100*BASE_REN_GW/s["L"] for s in f if s["L"]),
              target)
    series=[]
    for s in raw:
        rn=(s["solar"]+s["wind"])*k+BASE_REN_GW
        series.append({"t":s["t"],"dt":s["dt"],"L":round(s["L"],1),"ren":round(rn,1),
                       "resid":round(s["L"]-rn,1),"share":round(100*rn/s["L"],1)})
    days={}
    for s in series: days.setdefault(s["dt"].date().isoformat(),[]).append(s)
    rows=[]
    for d in sorted(days):
        xs=days[d]; peak=max(x["resid"] for x in xs)
        mean=sum(x["resid"] for x in xs)/len(xs)
        rows.append({"day":d,"peak_resid":round(peak,1),"band":band(peak),
                     "mean_resid":round(mean,1),"band_mean":band_mean(mean),
                     # Sichtbar machen, wenn band_mean ausserhalb seines
                     # Belegbereichs raet — sonst liest sich "hoch" wie ein Befund.
                     "band_mean_extrapoliert":not (BANDS_MEAN_FIT_RANGE[0]<=mean<=BANDS_MEAN_FIT_RANGE[1]),
                     "min_share":round(min(x["share"] for x in xs),1),
                     "avg_share":round(sum(x["share"] for x in xs)/len(xs),1)})
    # Ensemble dazu: dieselbe Rechnung ueber 40 ICON-EPS-Mitglieder.
    ens=ensemble_stats(target)
    if ens:
        for r in rows:
            e=ens.get(r["day"])
            if e: r["ensemble"]=e

    # Ausgeloest wird jetzt ueber die WAHRSCHEINLICHKEIT, nicht ueber das Band.
    #
    # Grund (forecast_skill.py, 45 Tage): Die Bandzuordnung stimmte schon bei
    # einem Tag Vorlauf nur zu 43 % mit der Analyse ueberein, weil der MAE von
    # 5-6 GW groesser ist als die Bandbreite von 4 GW. Die Schwelle entschied
    # also ueber Rauschen. Das Ensemble misst diese Unsicherheit, statt sie
    # wegzurunden — ein Tag wird gemeldet, wenn genug Szenarien ihn teuer sehen.
    #
    # Faellt das Ensemble aus, gilt wieder das Band: lieber die alte,
    # unzuverlaessige Warnung als gar keine.
    if ens:
        flagged=[r for r in rows
                 if r.get("ensemble",{}).get("p_warn",0)>=ALERT_P]
    else:
        flagged=[r for r in rows if r["band"] in ("erhöht","hoch")]
    # State-Diff ZUERST: welche Tage sind wirklich neu?
    #
    # BUG bis 18.08.2026: Gespeichert wurde die komplette Liste `cur` als
    # "gemeldet", die Nachricht nannte aber nur den SCHLIMMSTEN Tag. Rutschte ein
    # neuer Tag herein, feuerte der Alarm, der Text sprach jedoch von einem
    # anderen — und der neue Tag galt trotzdem als erledigt. Folge: Der 21.08.
    # wurde nie in einer Nachricht genannt und danach dauerhaft verschwiegen,
    # der 17.08. dafuer zweimal gemeldet.
    #
    # Jetzt nennt die Nachricht ALLE neu hinzugekommenen Tage, und nur wer
    # genannt wurde, gilt anschliessend als gemeldet.
    prev=[]
    try: prev=json.load(open(STATE_FILE)).get("alerted_days",[])
    except Exception: pass
    cur=[r["day"] for r in flagged]
    new_rows=sorted((r for r in flagged if r["day"] not in prev), key=lambda r:r["day"])

    # Entwarnung: ein frueher gemeldeter Tag ist nicht mehr geflaggt.
    #
    # Entscheidend ist die Einschraenkung auf Tage, die NOCH IM PROGNOSEFENSTER
    # liegen. Ohne sie waere jeder vergangene Warntag eine Entwarnung — der Tag
    # faellt ja taeglich hinten aus dem Fenster, ganz ohne Lagewechsel. Gemeldet
    # werden soll nur: "die Warnung von neulich gilt nicht mehr".
    by_day={r["day"]:r for r in rows}
    cleared=sorted((by_day[d] for d in prev if d not in cur and d in by_day),
                   key=lambda r:r["day"])
    alert_needed=bool(new_rows or cleared)

    # Empfehlungstext — beschreibt genau die neuen Tage.
    WD=["Mo","Di","Mi","Do","Fr","Sa","So"]
    def teile(r):
        """(Tagesangabe, Befund) — getrennt, damit ein einzelner Tag als Satz
        und mehrere als Aufzaehlung sauber lesbar sind."""
        dd=datetime.date.fromisoformat(r["day"])
        tag="*%s %s*"%(WD[dd.weekday()],dd.strftime("%d.%m."))
        e=r.get("ensemble")
        if e:
            sicher=("ziemlich sicher" if e["p_warn"]>=90 else
                    "wahrscheinlich" if e["p_warn"]>=75 else "eher wahrscheinlich")
            return tag,("teure Preise %s (%d%% der Wettermodelle, "
                        "Residuallast %.0f–%.0f GW)"%(sicher,e["p_warn"],e["p10"],e["p90"]))
        adj={"erhöht":"erhöhte","hoch":"hohe"}.get(r["band"],r["band"])
        return tag,("%s Preise zu erwarten (Residuallast ~%.0f GW, "
                    "Erneuerbare im Tief %.0f%%)"%(adj,r["peak_resid"],r["min_share"]))

    EMPFEHLUNG=("\n\n*Empfehlung:* ID.3 vorher auf 100% laden (bevorzugt "
        "PV-Überschuss/günstige Stunden) und E3DC voll halten → ~20 kWh Puffer + "
        "volles Auto, um die teuren Stunden zu überbrücken. EOS optimiert die "
        "genaue Stunde am Vortag.")
    def entwarnung_zeile(r):
        dd=datetime.date.fromisoformat(r["day"])
        tag="*%s %s*"%(WD[dd.weekday()],dd.strftime("%d.%m."))
        e=r.get("ensemble")
        if e:
            return tag,("nur noch %d%% der Wettermodelle, Residuallast %.0f–%.0f GW"
                        %(e["p_warn"],e["p10"],e["p90"]))
        return tag,("Residuallast nur noch ~%.0f GW (%s)"%(r["peak_resid"],r["band"]))

    teile_text=[]
    if new_rows:
        if len(new_rows)==1:
            tag,befund=teile(new_rows[0])
            teile_text.append("⚠️ *Strompreis-Warnung* — für %s sind %s."%(tag,befund)+EMPFEHLUNG)
        else:
            teile_text.append("⚠️ *Strompreis-Warnung* — mehrere Tage:\n"
                              +"\n".join("• %s: %s"%teile(r) for r in new_rows)+EMPFEHLUNG)
    if cleared:
        # Kurz halten und ohne Handlungsempfehlung: Es ist nichts zu tun, das ist
        # ja gerade die Nachricht. Wer eine Warnung im Kopf hat, soll sie
        # streichen koennen — mehr nicht.
        if len(cleared)==1:
            tag,befund=entwarnung_zeile(cleared[0])
            teile_text.append("✅ *Entwarnung* — für %s ist keine Preisspitze mehr "
                              "zu erwarten (%s)."%(tag,befund))
        else:
            teile_text.append("✅ *Entwarnung* — keine Preisspitze mehr erwartet:\n"
                              +"\n".join("• %s: %s"%entwarnung_zeile(r) for r in cleared))
    alert_text="\n\n".join(teile_text) if teile_text else None

    # Nur genannte Tage gelten als gemeldet. Bereits frueher gemeldete Tage
    # bleiben gemerkt, solange sie noch geflaggt sind — sonst warnt derselbe Tag
    # bei jedem Lauf erneut.
    json.dump({"alerted_days":[d for d in cur if d in prev]+[r["day"] for r in new_rows],
               "updated":datetime.datetime.utcnow().isoformat()+"Z"},open(STATE_FILE,"w"))
    worst_band=max([r["band"] for r in rows],key=lambda b:["günstig","normal","erhöht","hoch"].index(b))
    report={"generated":datetime.datetime.utcnow().isoformat()+"Z","k":round(k,2),"anchor":anchor,
            "days":rows,"flagged":flagged,"alert_needed":alert_needed,"alert_text":alert_text,
            "worst_band":worst_band,"series":[{"t":s["t"],"resid":s["resid"],"ren":s["ren"],"L":s["L"]} for s in series]}
    json.dump(report,open(REPORT,"w"),ensure_ascii=False,indent=2)
    # HA push
    push_ha(rows,worst_band,flagged,alert_text,anchor,k)
    # Konsole
    print("k=%.2f  anchor=%s  worst_band=%s  alert_needed=%s"%(k,anchor,worst_band,alert_needed))
    for r in rows:
        e=r.get("ensemble")
        ens=(" | Ensemble %5.1f–%-5.1f GW  P(Warnung) %3d%%%s"%(
                e["p10"],e["p90"],e["p_warn"]," ?" if e["unentscheidbar"] else "")) if e else ""
        print("  %s  Peak %5.1f GW %-8s | Ø-EE %5.1f%%%s"%(
            r["day"],r["peak_resid"],r["band"],r["avg_share"],ens))
    print("ALERT_TEXT:",(alert_text or "(keiner)"))

def hass_token():
    """Token aus der Umgebung, sonst aus der Datei, auf die HASS_TOKEN_FILE zeigt.

    HASS_TOKEN_FILE ist fuer Umgebungen gedacht, die Zugangsdaten nur als
    read-only gemountete Datei bereitstellen.
    """
    tok=os.environ.get("HASS_TOKEN")
    if tok: return tok
    path=os.environ.get("HASS_TOKEN_FILE")
    if not path: return None
    try:
        with open(path) as fh: return fh.read().strip() or None
    except OSError as ex:
        print("HA: HASS_TOKEN_FILE nicht lesbar (%s): %s"%(path,ex)); return None

def push_ha(rows,worst_band,flagged,alert_text,anchor,k):
    url=os.environ.get("HASS_URL"); tok=hass_token()
    if not url or not tok: print("HA: kein HASS_URL/TOKEN — uebersprungen"); return
    def put(ent,state,attrs):
        body=json.dumps({"state":state,"attributes":attrs}).encode()
        req=urllib.request.Request(url.rstrip("/")+"/api/states/"+ent,data=body,method="POST",
            headers={"Authorization":"Bearer "+tok,"Content-Type":"application/json"})
        try:
            with urllib.request.urlopen(req,timeout=15) as r: return r.status
        except Exception as ex: print("HA push %s Fehler: %s"%(ent,ex)); return None
    put("sensor.dunkelflaute_status",worst_band,{
        "friendly_name":"Dunkelflaute/Preis-Status","icon":"mdi:weather-cloudy-alert",
        "flagged_days":[r["day"] for r in flagged],
        "recommendation":alert_text or "keine erhoehten Preise in den naechsten ~4 Tagen",
        "kalibrierung_k":k,"energy_charts_ren_share_avg":anchor.get("avg"),
        "prognose":rows})
    peak=max((r["peak_resid"] for r in rows),default=0)
    put("sensor.residuallast_prognose_max",peak,{
        "friendly_name":"Residuallast Prognose (Peak 5 Tage)","unit_of_measurement":"GW","icon":"mdi:transmission-tower"})
    print("HA: Sensoren aktualisiert (worst_band=%s, peak=%.1f GW)"%(worst_band,peak))

if __name__=="__main__": main()
