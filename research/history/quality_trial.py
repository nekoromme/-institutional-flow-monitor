"""企業の選定条件を当時の公表情報で比較する研究用ランナー。

APIキーは暗号化保存にだけ利用し、SECへの通信には送らない。
財務が取れない場合を良好とはせず、判定可能な範囲を対照にも適用する。
"""
import hashlib
import html
import json
import os
import re
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from statistics import mean
from flow_probe.http_client import SafeHttp, ProbeError
from flow_probe.bulk13f import digest
from .collect import ROOT, write_json, encrypt_bytes
from .exit_search import load_inputs, simulate_exit

GATES = ('event_clear','screened_clear','operating_positive','equity_positive','liquidity_positive')
TAG_OCF='NetCashProvidedByUsedInOperatingActivities'


def age(a,b):
    return (date.fromisoformat(a)-date.fromisoformat(b)).days


def eligible_facts(facts,tag,asof,annual=False):
    """公表日を先に絞る。決算期末だけで絞ると後日訂正が混ざる。"""
    rows=facts.get('facts',{}).get('us-gaap',{}).get(tag,{}).get('units',{}).get('USD',[])
    good=[]
    for r in rows:
        if not r.get('filed') or not r.get('end') or not r.get('accn'):continue
        if r['filed']>=asof or r['end']>=asof:continue
        if r.get('form') not in ('10-K','10-K/A','10-Q','10-Q/A'):continue
        if not isinstance(r.get('val'),(int,float)):continue
        if annual:
            if r['form'] not in ('10-K','10-K/A') or not r.get('start'):continue
            if not 330<=age(r['end'],r['start'])<=380 or age(asof,r['end'])>550:continue
        elif r.get('start') or age(asof,r['end'])>200:continue
        good.append(r)
    return good


def choose(rows):
    """最新期末→その時点の最新公表。矛盾した値は勝手に選ばない。"""
    if not rows:return None
    end=max(r['end'] for r in rows); rows=[r for r in rows if r['end']==end]
    filed=max(r['filed'] for r in rows);rows=[r for r in rows if r['filed']==filed]
    if len({r['val'] for r in rows})!=1:return None
    r=sorted(rows,key=lambda r:r['accn'])[0]
    return {k:r[k] for k in ('val','end','filed','accn','form','start') if k in r}


def financial_snapshot(facts,asof):
    ocf=choose(eligible_facts(facts,TAG_OCF,asof,True))
    equity=choose(eligible_facts(facts,'StockholdersEquity',asof))
    if equity is None:equity=choose(eligible_facts(facts,'StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest',asof))
    assets=eligible_facts(facts,'AssetsCurrent',asof); liabilities=eligible_facts(facts,'LiabilitiesCurrent',asof)
    pairs=[]
    for a in assets:
        for b in liabilities:
            if (a['end'],a['filed'],a['accn'])==(b['end'],b['filed'],b['accn']):
                pairs.append({**a,'val':a['val']-b['val']})
    working=choose(pairs)
    return {'operating_cash':ocf,'equity':equity,'working_capital':working}


def submission_rows(block):
    """SECの列形式を行へ。公表済み資料の索引で現在のティッカーは使わない。"""
    n=len(block.get('accessionNumber',[]))
    return [{k:(v[i] if i<len(v) else '') for k,v in block.items() if isinstance(v,list)} for i in range(n)]


def going_concern_scan(raw):
    """疑義の文言は要確認として拾う。一般論や否定文だけでは確定しない。"""
    text=html.unescape(re.sub('<[^>]+>',' ',raw.decode('utf-8',errors='replace')))
    text=re.sub(r'\s+',' ',text)
    hits=[]
    for m in re.finditer(r'substantial doubt',text,re.I):
        excerpt=text[max(0,m.start()-100):m.end()+350]
        if re.search(r'going concern',excerpt,re.I):
            # 「疑義なし」「疑義の解消」等も含めて原文を残すが、肯定扱いしない。
            negative=bool(re.search(r'(?:no|not|without)\s+(?:a\s+)?substantial doubt|(?:alleviat|mitigat)\w*.{0,90}substantial doubt',excerpt,re.I))
            hits.append({'text':excerpt[:500],'negative_or_mitigated':negative})
    active=[h for h in hits if not h['negative_or_mitigated']]
    return {'status':'review_required' if active else 'no_positive_phrase_detected','hits':hits[:8],
            'characters':len(text),'guarantees_solvency':False}


def issuer_map(root):
    out=defaultdict(set)
    for name in ('price-request.json','expansion-request.json'):
        doc=json.loads((root/'research/history'/name).read_text())
        for year in ('2023','2024'):
            for r in doc['years'][year]['additional_candidates']:
                if r['cusip_issuer_verified']:
                    out[(year,r['ticker_in_reviewed_filing'])].add(r['issuer_candidate_cik'])
    return {k:next(iter(v)) for k,v in out.items() if len(v)==1}


def fetch_cached(client,folder,key,url,source_log):
    """取得ごとにチェックポイント。再実行時は保存済みの同じ資料を再利用する。"""
    path=folder/key;meta=path.with_suffix(path.suffix+'.source.json')
    if path.exists() and meta.exists():
        m=json.loads(meta.read_text())
        if m['url']!=url or digest(path)!=m['sha256']:raise ValueError('source_cache_changed')
        raw=path.read_bytes()
    else:
        raw=client.read(url,max_bytes=35_000_000)
        path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
        m={'url':url,'sha256':digest(path),'bytes':len(raw)};write_json(meta,m)
    source_log.append(m)
    return raw


def collect_issuer(client,folder,cik,needed_days,source_log):
    """企業ごとに財務・提出履歴・必要な年次報告だけ収集する。"""
    result={'cik':cik,'facts':{},'filings':[],'annual_scans':{},'errors':[],'history_complete':False}
    try:
        facts=json.loads(fetch_cached(client,folder,cik+'-facts.json','https://data.sec.gov/api/xbrl/companyfacts/CIK'+cik+'.json',source_log))
        if int(facts['cik'])!=int(cik):raise ValueError('issuer_mismatch')
        # 2025以降の値は選定にも保存用事実一覧にも渡さない。
        facts={'cik':facts['cik'],'facts':{'us-gaap':{tag:{'units':{'USD':[r for r in value.get('units',{}).get('USD',[]) if r.get('filed','9999')<'2025-01-01']}} for tag,value in facts.get('facts',{}).get('us-gaap',{}).items()}}}
        result['facts']=facts
    except (ProbeError,ValueError,KeyError) as e:result['errors'].append({'kind':'facts','error':str(e)})
    try:
        sub=json.loads(fetch_cached(client,folder,cik+'-sub.json','https://data.sec.gov/submissions/CIK'+cik+'.json',source_log))
        if int(sub['cik'])!=int(cik):raise ValueError('issuer_mismatch')
        rows=submission_rows(sub['filings']['recent'])
        for old in sub['filings'].get('files',[]):
            if old['filingTo']>='2022-01-01' and old['filingFrom']<'2025-01-01':
                block=json.loads(fetch_cached(client,folder,old['name'],'https://data.sec.gov/submissions/'+old['name'],source_log));rows+=submission_rows(block)
        seen={r['accessionNumber']:r for r in rows if '2022-01-01'<=r.get('filingDate','')<'2025-01-01'}
        result['filings']=sorted(seen.values(),key=lambda r:(r['filingDate'],r['accessionNumber']))
        result['history_complete']=True
        annuals=[r for r in result['filings'] if r['form']=='10-K']
        needed={}
        for day in needed_days:
            prior=[r for r in annuals if r['filingDate']<day]
            if prior:
                latest=max(prior,key=lambda r:r['filingDate']);needed[latest['accessionNumber']]=latest
        for acc,r in needed.items():
            url=f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-','')}/{r['primaryDocument']}"
            try:
                raw=fetch_cached(client,folder,acc+'.html',url,source_log)
                scan=going_concern_scan(raw)
                if scan['characters']<10000:raise ValueError('annual_text_too_short')
                result['annual_scans'][acc]={'url':url,'sha256':hashlib.sha256(raw).hexdigest(),'filed':r['filingDate'],'report_date':r.get('reportDate'),**scan}
            except (ProbeError,ValueError) as e:result['annual_scans'][acc]={'status':'unavailable','error':str(e),'url':url,'filed':r['filingDate']}
    except (ProbeError,ValueError,KeyError) as e:result['errors'].append({'kind':'filings','error':str(e)})
    return result


def gate_snapshot(issuer,day):
    """False＝条件外、None＝資料不足。欠損と危機情報を分ける。"""
    f=financial_snapshot(issuer.get('facts',{}),day)
    events=[]
    if issuer.get('history_complete'):
        for r in issuer['filings']:
            if r['form'] in ('8-K','8-K/A') and 0<age(day,r['filingDate'])<=365:
                codes=set(re.findall(r'\d\.\d\d',r.get('items','')))&{'1.03','2.04','3.01'}
                if codes:events.append({'filed':r['filingDate'],'accession':r['accessionNumber'],'items':sorted(codes)})
        event_clear=not events
    else:event_clear=None
    annuals=[(a,r) for a,r in issuer.get('annual_scans',{}).items() if r['filed']<day]
    annual=max(annuals,key=lambda p:p[1]['filed']) if annuals else None
    scan_known=bool(annual and annual[1]['status']!='unavailable' and annual[1].get('report_date') and age(day,annual[1]['report_date'])<=550)
    screened=(event_clear and annual[1]['status']=='no_positive_phrase_detected') if event_clear is not None and scan_known else None
    def combine(parent,metric):
        return None if parent is None or metric is None else bool(parent and metric['val']>0)
    operating=combine(screened,f['operating_cash']); equity=combine(operating,f['equity'])
    liquid=None if equity is None or f['working_capital'] is None else bool(equity and f['working_capital']['val']>=0)
    return {'gates':dict(zip(GATES,(event_clear,screened,operating,equity,liquid))),
            'financials':f,'events':events,'annual_accession':annual[0] if annual else None,
            'annual_status':annual[1]['status'] if annual else 'missing'}


def risk_details(record,books,days):
    """終値での深い含み損と、口座が直前ピークを下回った期間を補助集計する。"""
    sells={t['id']:t for t in record['closed']};adverse=[];losses=[]
    for b in record['fills']:
        if b['event']!='buy':continue
        t=sells.get(b['id']);end=t['exit_date'] if t else days[-1];start=b['date']
        closes=[books[b['asset']][d]['split']['c'] for d in days if start<=d<=end and not(t and t['exit_when']=='open' and d==end)]
        entry=books[b['asset']][start]['split']['o']
        adverse.append(min([0]+[v/entry-1 for v in closes]))
        if t:losses.append(t['trade_net_return'])
    peak=1.;duration=longest=0
    for r in record['daily']:
        if r['equity']>=peak-1e-12:peak=max(peak,r['equity']);duration=0
        else:duration+=1;longest=max(longest,duration)
    return {'purchases_with_close_loss20':sum(v<=-.2 for v in adverse),'worst_trade_close_excursion':min(adverse) if adverse else None,
            'closed_loss20_count':sum(v<=-.2 for v in losses),'longest_underwater_sessions':longest,'still_underwater_at_end':duration>0,
            'note':'unrecovered duration is right-censored; excursion excludes exit-day close for open sales'}


def run(root,secret):
    # 保存済み延長資料のハッシュは既存の公開結果から設定する。
    previous=json.loads((root/'research/history/evidence/exit-extension-final-results.json').read_text())
    write_json(root/'diagnostics/history/exit-search/extension.json',previous['extension_metadata'])
    os.environ['EXIT_EXTENSION']='1'
    scored,books,days,overlap=load_inputs(root,secret)
    mapping=issuer_map(root);need=defaultdict(set)
    for r in scored:
        if r.get('depressed_control'):
            cik=mapping.get((r['date'][:4],r['symbol']))
            if cik:need[cik].add(r['date'])
    folder=root/'data/history/quality-source-cache';sources=[];issuers={}
    client=SafeHttp(interval=.3,timeout=25,max_requests=1600,max_seconds=1000)
    for i,(cik,needed_days) in enumerate(sorted(need.items())):
        issuers[cik]=collect_issuer(client,folder,cik,needed_days,sources)
        if i%10==0:print(json.dumps({'progress':'financial_sources','done':i+1,'issuers':len(need),'requests':client.calls}),flush=True)
    snapshots={};audit=[]
    for r in scored:
        if not r.get('depressed_control'):continue
        cik=mapping.get((r['date'][:4],r['symbol']))
        key=(cik,r['date'])
        if key not in snapshots:snapshots[key]=gate_snapshot(issuers.get(cik,{}),r['date'])
        snap=snapshots[key]
        audit.append({'symbol':r['symbol'],'date':r['date'],'cik':cik,'signal':bool(r.get('depressed')),**snap})
        for gate in GATES:
            v=snap['gates'][gate]
            r['quality_'+gate]=bool(r.get('depressed') and v is True)
            r['coverage_'+gate]=bool(r.get('depressed') and v is not None)
            r['control_'+gate]=v is True
    out={'code_commit':os.environ.get('GITHUB_SHA'),'plan_sha256':digest(root/'research/history/QUALITY_PLAN.md'),
         'reserved_2025_opened':False,'scope':'seen_2023_2024_quality_exploration','new_market_requests':0,
         'sec_metrics':client.metrics(),'source_count':len(sources),'issuer_count':len(issuers),'overlap_checked':overlap,
         'issuer_errors':{c:v['errors'] for c,v in issuers.items() if v['errors']},'results':[],
         'annual_scan_counts':dict(Counter(r['status'] for v in issuers.values() for r in v['annual_scans'].values())),
         'review_required_sources':[{'cik':c,'accession':a,**r} for c,v in issuers.items() for a,r in v['annual_scans'].items() if r['status']=='review_required'],
         'coverage':{g:dict(Counter('pass' if a['gates'][g] is True else 'fail' if a['gates'][g] is False else 'unknown' for a in audit if a['signal'])) for g in GATES},
         'base_signal_rows':sum(a['signal'] for a in audit),'base_signal_symbols':len({a['symbol'] for a in audit if a['signal']}),
         'missing_identity_symbols':sorted({a['symbol'] for a in audit if not a['cik']}),
         'limitations':['annual_text_scan_not_manual_certification','no_full_quarterly_text_scan','recent_listing_notice_not_unresolved_status','standard_USD_US_GAAP_only','no_out_of_sample_test']}
    private={}
    fields=['depressed','depressed_control']+[p+g for g in GATES for p in ('quality_','coverage_','control_')]
    for ticket in (.2,.1):
        for field in fields:
            ident=f'{field}_a{ticket}'
            try:
                result,record=simulate_exit(scored,books,days,field,20,'profit20',ticket=ticket,cost=.0025)
                out['results'].append({'id':ident,'field':field,'ticket':ticket,'status':'ok',**result,'risk_details':risk_details(record,books,days)})
                private[ident]=record
            except ValueError as e:out['results'].append({'id':ident,'status':'failed','error':str(e)})
    for ticket in (.2,.1):
        actual=next(r for r in out['results'] if r['id']==f'depressed_a{ticket}')
        old=next(r for r in previous['results'] if r['id']==f'depressed_h20_profit20_a{ticket}_c0.0025')
        if actual.get('summary')!=old['summary'] or actual.get('years')!=old['years']:raise ValueError('baseline_did_not_reproduce')
    out['baseline_reproduced']=True
    out['configuration_count']=len(out['results'])
    # 公開原資料の全文も非公開の暗号化証跡へ。市場データと同じ保存方式。
    payload={'records':private,'selection_audit':audit,'issuers':issuers,'sources':sources}
    path=root/'data/history/encrypted/quality-trial.enc';path.write_bytes(encrypt_bytes(json.dumps(payload,sort_keys=True,allow_nan=False).encode(),secret))
    out['encrypted_sha256']=digest(path)
    write_json(root/'diagnostics/history/quality/results.json',out)
    write_json(root/'diagnostics/history/quality/sources.json',sources)
    # 財務数値・提出日の監査表には市場価格を含めない。
    write_json(root/'diagnostics/history/quality/selection-audit.json',audit)
    print(json.dumps({'completed':True,'configurations':len(out['results']),'failed':sum(r['status']=='failed' for r in out['results'])}),flush=True)


if __name__=='__main__':run(ROOT,os.environ.get('ALPACA_SECRET_KEY',''))
