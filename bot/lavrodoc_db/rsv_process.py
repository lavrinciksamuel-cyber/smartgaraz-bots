"""Stream CZ RSV open-data CSV from stdin -> aggregated, PII-free outputs.

Outputs in out/:
  header.txt, columns.json, sample_rows.csv, progress.txt, status_dist.json
  rsv_agg_all.csv.gz   – M1/N1, year>=1995, ALL statuses (legacy-compatible)
  rsv_agg.csv.gz       – same but Status == PROVOZOVANÉ only (active)
  rsv_vin.csv.gz       – VIN prefixes from active only
  active_by_engine.csv – active M1 make/obch/engine/fuel/kw/ccm counts
  stats_active_m1.json – summary stats for active M1
"""
import csv,sys,gzip,collections,io,re,json,signal,time
csv.field_size_limit(10**8)
inp=io.TextIOWrapper(sys.stdin.buffer,encoding='utf-8',errors='replace',newline='')
r=csv.reader(inp)
h=next(r); h=[c.strip().lstrip('\ufeff') for c in h]
open('out/header.txt','w').write('\n'.join(h))
def col(*keys,excl=()):
    for i,c in enumerate(h):
        l=c.lower()
        if all(k in l for k in keys) and not any(e in l for e in excl): return i
    return None
C={'make':col('tovární','značka') or col('značka'),'obch':col('obchodní'),'typ':col('typ',excl=('motor','karos','pal','vozidla','druh','status','hmot','převod','barv')),
   'kat':col('kategorie'),'fuel':col('palivo') if col('palivo') is not None else col('paliv'),'kw':col('výkon'),'ccm':col('zdvih') if col('zdvih') is not None else col('objem'),
   'eng':col('typ','motor') if col('typ','motor') is not None else col('motor',excl=('výrob','výkon')),'engmfr':col('výrob','motor'),
   'reg':col('1.','registrace',excl=('čr',)),'regcz':col('registrace','čr'),'vin':col('vin'),'yr':col('rok','výroby'),
   'status': next((i for i,c in enumerate(h) if c.strip().lower()=='status'), None)}
json.dump({k:(h[v] if v is not None else None) for k,v in C.items()},open('out/columns.json','w'),ensure_ascii=False,indent=1)
samp=open('out/sample_rows.csv','w',newline=''); sw=csv.writer(samp); sw.writerow(h)
g=lambda row,k: (row[C[k]].strip() if C[k] is not None and C[k]<len(row) else '')
A_all=collections.Counter(); A_act=collections.Counter(); V=collections.Counter()
ST=collections.Counter(); ST_m1=collections.Counter()
ENG=collections.Counter()  # active M1: make,obch,engine,fuel,kw,ccm
n=0; kept_all=0; kept_act=0; m1_act=0
ACTIVE={'PROVOZOVANÉ','PROVOZOVANE'}  # normalize accents below

def norm_status(s):
    s=(s or '').strip().upper()
    # fold common accentless variants
    return (s.replace('Á','A').replace('É','E').replace('Í','I').replace('Ó','O').replace('Ú','U').replace('Ý','Y')
             .replace('Č','C').replace('Ř','R').replace('Š','S').replace('Ž','Z').replace('Ď','D').replace('Ť','T').replace('Ň','N'))

def is_active(st):
    n=norm_status(st)
    return n in ('PROVOZOVANE',) or n.startswith('PROVOZOVAN')

def dump(tag=''):
    with gzip.open('out/rsv_agg_all.csv.gz','wt',encoding='utf-8',newline='') as f:
        w=csv.writer(f); w.writerow(['make','obch','typ','kat','fuel','kw','ccm','engine','year','n'])
        for k,c in list(A_all.items()): w.writerow(list(k)+[c])
    with gzip.open('out/rsv_agg.csv.gz','wt',encoding='utf-8',newline='') as f:
        w=csv.writer(f); w.writerow(['make','obch','typ','kat','fuel','kw','ccm','engine','year','n'])
        for k,c in list(A_act.items()): w.writerow(list(k)+[c])
    with gzip.open('out/rsv_vin.csv.gz','wt',encoding='utf-8',newline='') as f:
        w=csv.writer(f); w.writerow(['vin8','my','make','obch','fuel','kw','ccm','engine','n'])
        for k,c in list(V.items()):
            if c>=2: w.writerow(list(k)+[c])
    with open('out/active_by_engine.csv','w',encoding='utf-8',newline='') as f:
        w=csv.writer(f); w.writerow(['make','obch','engine','fuel','kw','ccm','n'])
        for k,c in ENG.most_common():
            w.writerow(list(k)+[c])
    json.dump({'all':ST,'m1':ST_m1,'active_norm':'PROVOZOVANÉ (and PROVOZOVANE*)'},
              open('out/status_dist.json','w'),ensure_ascii=False,indent=1)
    open('out/progress.txt','w').write(
        f'{tag} rows {n} kept_all {kept_all} kept_act {kept_act} m1_act {m1_act} '
        f'agg_all {len(A_all)} agg_act {len(A_act)} vin {len(V)} eng {len(ENG)}\n')

def onsig(*a):
    dump('PARTIAL(signal)'); print('PARTIAL rows',n,flush=True); sys.exit(0)
signal.signal(signal.SIGTERM,onsig); signal.signal(signal.SIGINT,onsig)
T0=time.time(); last=T0
for row in r:
    n+=1
    if n<=30: sw.writerow([x if not ('vin' in h[i].lower() or 'karoserie' in h[i].lower() and 'číslo' in h[i].lower() or 'motoru' in h[i].lower() and 'číslo' in h[i].lower() or h[i] in ('Číslo TP','Číslo ORV','PČV')) else x[:11]+'******' for i,x in enumerate(row)])
    st=g(row,'status'); ST[st or '(empty)']+=1
    kat=g(row,'kat').upper()
    if kat.startswith('M1'): ST_m1[st or '(empty)']+=1
    if kat and not (kat.startswith('M1') or kat.startswith('N1')): continue
    reg=g(row,'reg') or g(row,'regcz') or g(row,'yr'); y=reg[:4] if re.match(r'\d{4}',reg) else ''
    if y and y<'1995': continue
    kw=g(row,'kw'); m=re.match(r'\s*([\d.,]+)',kw); kw=m.group(1).replace(',','.') if m else ''
    make=g(row,'make').upper(); obch=g(row,'obch').upper(); typ=g(row,'typ').upper()
    fuel=g(row,'fuel').upper(); ccm=g(row,'ccm'); eng=g(row,'eng').upper()
    key=(make,obch,typ,kat[:2],fuel,kw,ccm,eng,y)
    A_all[key]+=1; kept_all+=1
    act=is_active(st)
    if act:
        A_act[key]+=1; kept_act+=1
        vin=g(row,'vin').upper()
        if len(vin)==17 and re.match(r'^[A-HJ-NPR-Z0-9]{17}$',vin):
            V[(vin[:8],vin[9],make,obch,fuel,kw,ccm,eng)]+=1
        if kat.startswith('M1'):
            m1_act+=1
            ENG[(make,obch,eng,fuel,kw,ccm)]+=1
    if n%500000==0:
        print('rows',n,'kept_all',kept_all,'kept_act',kept_act,'m1_act',m1_act,'agg_act',len(A_act),int(time.time()-T0),'s',flush=True)
        if time.time()-last>1200: dump('PARTIAL'); last=time.time()
samp.close()
dump('DONE')
# compact active M1 summary for artifact
def family(obch):
    o=(obch or '').strip().upper()
    # strip body suffixes
    for suf in (' COMBI',' KOMBI',' SW',' VARIANT',' TOURER',' SPORTSTOURER',' SPORTBACK',' LIFTBACK',' HATCHBACK',' COUPE',' COUPÉ',' CABRIO',' CABRIOLET',' ROADSTER',' ALLROAD',' CROSS',' SCOUT',' RS',' GTI',' GTD',' R LINE'):
        if o.endswith(suf): o=o[: -len(suf)].strip()
    # FABIA COMBI -> FABIA already handled; also "OCTAVIA II" style keep
    return o or '(empty)'
makes=collections.Counter(); models=collections.Counter(); fams=collections.Counter(); by_year=collections.Counter()
for (make,obch,typ,kat,fuel,kw,ccm,eng,y),c in A_act.items():
    if kat!='M1': continue
    makes[make]+=c; models[(make,obch)]+=c; fams[(make,family(obch))]+=c
    if y.isdigit() and int(y)>=2015: by_year[(make,family(obch),y)]+=c
combo=collections.Counter()
for (make,obch,eng,fuel,kw,ccm),c in ENG.items():
    combo[(make,family(obch),eng,fuel,kw,ccm)]+=c
stats={
  'dataset_note':'streamed from stdin; filter Status PROVOZOVANÉ',
  'rows_total':n,'kept_all':kept_all,'kept_act':kept_act,'m1_act':m1_act,
  'status_all':ST.most_common(),'status_m1':ST_m1.most_common(),
  'top10_makes':makes.most_common(10),
  'top20_models_raw':models.most_common(20),
  'top20_model_families':fams.most_common(20),
  'top15_families_2015plus':by_year.most_common(15),
  'top30_make_model_engine':[(list(k)+[c]) for k,c in combo.most_common(30)],
}
# top engines per top-20 family
top_fams=[f for f,_ in fams.most_common(20)]
eng_per={}
for fam in top_fams:
    ec=collections.Counter()
    for (make,obch,eng,fuel,kw,ccm),c in ENG.items():
        if (make,family(obch))==fam:
            ec[(eng,fuel,kw,ccm)]+=c
    eng_per[f'{fam[0]}|{fam[1]}']=[[list(k),c] for k,c in ec.most_common(8)]
stats['top_engines_per_top20_family']=eng_per
json.dump(stats,open('out/stats_active_m1.json','w'),ensure_ascii=False,indent=1)
print('DONE rows',n,'kept_all',kept_all,'kept_act',kept_act,'m1_act',m1_act,'agg_act',len(A_act),'status',ST.most_common(10))
