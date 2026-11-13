#!/usr/bin/env python3
"""Abgleich: feste GW-Schwelle gegen relative Einstufung, am Live-Protokoll.

Prueft, welcher Ausloeser "teure Tage" am besten trifft. Teuer ist ein Tag,
dessen Tagesmittelpreis ueber dem 80-%-Quantil der letzten 28 Tage liegt
(Definition aus dem Akkudoktor-Forum uebernommen). Die relative Einstufung
vergleicht jede Prognose mit den Prognosen gleichen Vorlaufs der 28 Tage davor.

Eingaben (vorher holen, Zeitraum passend zum Protokoll):
    curl "https://api.energy-charts.info/price?bzn=DE-LU&start=2026-07-01&end=2026-11-12" -o daten/price.json
    curl "https://api.energy-charts.info/public_power?country=de&start=2026-07-01&end=2026-11-12" -o daten/pp.json

Aufruf:
    abgleich_relativ.py daten validation-forecasts.jsonl

Die Monats- und Fenstergrenzen im Skript sind auf den Lauf vom 13.11.2026
festgeschrieben und muessen fuer andere Zeitraeume angepasst werden.
"""
import json,sys,statistics as st,collections,datetime as dt,random,zoneinfo
D=sys.argv[1]; LOG=sys.argv[2]
TZ=zoneinfo.ZoneInfo("Europe/Berlin")
def daily(ts,vals):
    d=collections.defaultdict(list)
    for t,v in zip(ts,vals):
        if v is None: continue
        d[dt.datetime.fromtimestamp(t,TZ).strftime("%Y-%m-%d")].append(v)
    return {k:v for k,v in d.items() if len(v)>=90}
p=json.load(open(D+"/price.json")); pd=daily(p["unix_seconds"],p["price"])
pmean={k:st.mean(v) for k,v in pd.items()}; pmax={k:max(v) for k,v in pd.items()}
pp=json.load(open(D+"/pp.json")); ser={x["name"]:x["data"] for x in pp["production_types"]}
rd=daily(pp["unix_seconds"],ser["Residual load"]); rmean={k:st.mean(v)/1000 for k,v in rd.items()}; rpeak={k:max(v)/1000 for k,v in rd.items()}
def r(xs,ys):
    mx,my=st.mean(xs),st.mean(ys); num=sum((x-mx)*(y-my) for x,y in zip(xs,ys))
    den=(sum((x-mx)**2 for x in xs)*sum((y-my)**2 for y in ys))**.5
    return num/den if den else float("nan")
print("== gemessene Residuallast (Tagesmittel) vs. Tagesmittelpreis, je Monat")
for m in ["2026-07","2026-08","2026-09","2026-10","2026-11"]:
    ds=[d for d in sorted(pmean) if d.startswith(m) and d in rmean]
    print(m,len(ds),"r_mean=%.2f"%r([rmean[d] for d in ds],[pmean[d] for d in ds]),"r_peak_vs_max=%.2f"%r([rpeak[d] for d in ds],[pmax[d] for d in ds]),
          "Preis Ø %.0f"%st.mean(pmean[d] for d in ds),"Resid Ø %.1f"%st.mean(rmean[d] for d in ds))
for a,b,lab in [("2026-07-02","2026-08-16","Sommerfenster"),("2026-08-17","2026-11-12","seit Logstart"),("2026-07-01","2026-11-12","alles")]:
    ds=[d for d in sorted(pmean) if a<=d<=b and d in rmean]
    print(lab,len(ds),"r_mean=%.2f"%r([rmean[d] for d in ds],[pmean[d] for d in ds]))
# Preisniveau bei gleicher Residuallast
print("== Preis je Residuallast-Klasse (Tagesmittel), Jul/Aug vs Okt/Nov")
for lo,hi in [(0,15),(15,25),(25,35),(35,99)]:
    out=[]
    for ms in (("2026-07","2026-08"),("2026-10","2026-11")):
        ds=[d for d in pmean if d[:7] in ms and d in rmean and lo<=rmean[d]<hi]
        out.append("%s n=%d Ø%.0f"%("/".join(m[5:] for m in ms),len(ds),st.mean(pmean[d] for d in ds) if ds else 0))
    print(lo,hi,out)

rows=[json.loads(l) for l in open(LOG)]
fc={}  # (day,lead)->row, letzter Lauf des Tages gewinnt
for x in rows: fc[(x["day"],x["lead_days"])]=x
days=sorted(pmean)
def q(vals,qq):
    s=sorted(vals); i=qq*(len(s)-1); lo=int(i); hi=min(lo+1,len(s)-1); return s[lo]+(s[hi]-s[lo])*(i-lo)
def prev(d,n): 
    b=dt.date.fromisoformat(d); return [(b-dt.timedelta(days=i)).isoformat() for i in range(1,n+1)]
truth={}
for d in days:
    h=[pmean[x] for x in prev(d,28) if x in pmean]
    if len(h)>=21: truth[d]=pmean[d]>q(h,.8)
def f1(pairs):
    tp=sum(1 for p,t in pairs if p and t); fp=sum(1 for p,t in pairs if p and not t); fn=sum(1 for p,t in pairs if t and not p)
    pr=tp/(tp+fp) if tp+fp else 0; rc=tp/(tp+fn) if tp+fn else 0
    return (2*pr*rc/(pr+rc) if pr+rc else 0),pr,rc,tp+fp
def rel(key,d,L,minhist=14):
    x=fc.get((d,L)); 
    if not x or x.get(key) is None: return None
    h=[fc[(y,L)][key] for y in prev(d,28) if (y,L) in fc and fc[(y,L)].get(key) is not None]
    if len(h)<minhist: return None
    return x[key]>q(h,.8)
def epexrel(d,L):
    x=fc.get((d,L))
    if not x or x.get("epex_mean") is None: return None
    run=(dt.date.fromisoformat(d)-dt.timedelta(days=L)).isoformat()
    h=[pmean[y] for y in prev(run,28) if y in pmean]+[pmean.get(run)] if pmean.get(run) else [pmean[y] for y in prev(run,28) if y in pmean]
    return x["epex_mean"]>q(h,.8)
methods={
 "fest: Spitze >= 42 GW (determ.)": lambda d,L:(fc[(d,L)]["peak_resid"]>=42) if (d,L) in fc else None,
 "Ensemble P(warn) >= 70 % (live)": lambda d,L:(fc[(d,L)]["ens_p_warn"]>=70) if (d,L) in fc and fc[(d,L)].get("ens_p_warn") is not None else None,
 "relativ: Spitze > P80(28 T)": lambda d,L:rel("peak_resid",d,L),
 "relativ: Mittel > P80(28 T)": lambda d,L:rel("mean_resid",d,L),
 "relativ: Ens-Median > P80(28 T)": lambda d,L:rel("ens_median",d,L),
 "EpexPredictor Mittel > P80 Ist(28 T)": epexrel,
}
START="2026-09-15"
for leads,lab in [((2,3,4),"Vorlauf 2-4"),((1,),"Vorlauf 1")]:
    # gemeinsame Menge
    keys=[(d,L) for d in days if d>=START and d in truth for L in leads if all(m(d,L) is not None for m in methods.values())]
    nd=len(set(d for d,_ in keys)); nt=sum(truth[d] for d in set(d for d,_ in keys))
    print("== %s ab %s: %d Paare, %d Tage, davon %d teuer"%(lab,START,len(keys),nd,nt))
    res={}
    for name,m in methods.items():
        pairs=[(m(d,L),truth[d]) for d,L in keys]; res[name]=pairs
        f,pr,rc,nw=f1(pairs); print("  %-38s F1 %.2f  Praez %.2f  Treffer %.2f  Warnquote %2.0f%%"%(name,f,pr,rc,100*nw/len(pairs)))
    # nach Saison
    for name in ["fest: Spitze >= 42 GW (determ.)","Ensemble P(warn) >= 70 % (live)","relativ: Mittel > P80(28 T)"]:
        for a,b in [("2026-09-15","2026-10-14"),("2026-10-15","2026-11-12")]:
            sub=[(methods[name](d,L),truth[d]) for d,L in keys if a<=d<=b]
            f,pr,rc,nw=f1(sub); print("     %s %s..%s: F1 %.2f Treffer %.2f Warnquote %.0f%% (n=%d)"%(name[:22],a[5:],b[5:],f,rc,100*nw/len(sub),len(sub)))
    # Block-Bootstrap (14-Tage-Bloecke) Differenz relativ-Mittel minus fest
    random.seed(1); dl=sorted(set(d for d,_ in keys)); B=14; blocks=[dl[i:i+B] for i in range(0,len(dl),B)]
    for a,b in [("relativ: Mittel > P80(28 T)","fest: Spitze >= 42 GW (determ.)"),("relativ: Mittel > P80(28 T)","Ensemble P(warn) >= 70 % (live)"),("relativ: Mittel > P80(28 T)","EpexPredictor Mittel > P80 Ist(28 T)")]:
        diffs=[]
        for _ in range(2000):
            samp=[d for blk in random.choices(blocks,k=len(blocks)) for d in blk]
            ks=[(d,L) for d in samp for L in leads if (d,L) in set(keys)]
            diffs.append(f1([(methods[a](d,L),truth[d]) for d,L in ks])[0]-f1([(methods[b](d,L),truth[d]) for d,L in ks])[0])
        diffs.sort(); print("  Diff F1 [%s] - [%s]: 95%% %.2f .. %.2f"%(a[:15],b[:15],diffs[50],diffs[1949]))
# Korrelation Modell vs Preis je Vorlauf
print("== Modell (Prognose) vs. Ist-Tagesmittelpreis, ab 2026-08-17")
for L in range(5):
    ks=[d for d in days if (d,L) in fc and fc[(d,L)].get("mean_resid") is not None and d>="2026-08-17"]
    print(" Vorlauf",L,len(ks),"r(Mittel)=%.2f"%r([fc[(d,L)]["mean_resid"] for d in ks],[pmean[d] for d in ks]),
      "r(Spitze,Max)=%.2f"%r([fc[(d,L)]["peak_resid"] for d in ks],[pmax[d] for d in ks]),
      "r(Epex)=%.2f"%r([fc[(d,L)]["epex_mean"] for d in ks if fc[(d,L)].get("epex_mean")],[pmean[d] for d in ks if fc[(d,L)].get("epex_mean")]),
      "MAE Resid-Mittel vs gemessen=%.1f bias=%.1f"%(st.mean(abs(fc[(d,L)]["mean_resid"]-rmean[d]) for d in ks if d in rmean),st.mean(fc[(d,L)]["mean_resid"]-rmean[d] for d in ks if d in rmean)),
      "MAE Spitze=%.1f bias=%.1f"%(st.mean(abs(fc[(d,L)]["peak_resid"]-rpeak[d]) for d in ks if d in rpeak),st.mean(fc[(d,L)]["peak_resid"]-rpeak[d] for d in ks if d in rpeak)))
