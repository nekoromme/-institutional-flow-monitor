"""保存済み暗号データを読み、分割補正の変化だけを検査する。損益は計算しない。"""
import json
import os
from flow_probe.alpaca import split_adjustment_audit
from flow_probe.bulk13f import digest
from .collect import ROOT,decrypt_bytes,write_json

DEVELOPMENT_YEARS=(2023,2024)


def load_development(root,year,secret):
    # 引数を間違えても保留中の2025年を開かない。
    if year not in DEVELOPMENT_YEARS:raise ValueError('reserved_year_must_not_be_opened')
    expected=json.loads((root/'research/history/evidence/identity/collection-result.json').read_text())
    checksum=next(r['encrypted_sha256'] for r in expected['market_chunks'] if r['year']==year)
    path=root/f'data/history/encrypted/additional-{year}.enc'
    if digest(path)!=checksum:raise ValueError('encrypted_history_changed')
    raw=decrypt_bytes(path.read_bytes(),secret)
    import hashlib
    plain=next(r['plaintext_sha256'] for r in expected['market_chunks'] if r['year']==year)
    if hashlib.sha256(raw).hexdigest()!=plain:raise ValueError('decrypted_history_changed')
    payload=json.loads(raw)
    if payload['year']!=year:raise ValueError('history_year_mismatch')
    return payload


def main():
    secret=os.environ.get('ALPACA_SECRET_KEY','');reports=[]
    for year in DEVELOPMENT_YEARS:
        payload=load_development(ROOT,year,secret)
        audit=split_adjustment_audit(payload['prices']['raw'],payload['prices']['split'],complete=True)
        rows=[]
        for symbol,a in audit.items():
            rows.append({'symbol':symbol,**{k:a[k] for k in ('status','coverage_complete','invalid_pairs',
                'price_mismatch_days','volume_mismatch_days','unresolved_days','provider_adjustment_segments')}})
        report={'year':year,'rows':rows,'returns_computed':False,'source_sha256':digest(ROOT/f'data/history/encrypted/additional-{year}.enc')}
        write_json(ROOT/f'diagnostics/history/validation/adjustments-{year}.json',report);reports.append(report)
        print(json.dumps({'year':year,'symbols':len(rows),'multiple_factor_segments':[r['symbol'] for r in rows if len(r['provider_adjustment_segments'])>1],
                          'pairing_issues':[r['symbol'] for r in rows if r['status']!='comparable_sample']}),flush=True)
    write_json(ROOT/'diagnostics/history/validation/scan-summary.json',{'years':reports,'reserved_year_opened':False})

if __name__=='__main__':main()
