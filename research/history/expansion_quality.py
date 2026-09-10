"""取得成功と、検証に使える価格が揃ったことを分けて報告する。"""
from collections import Counter
import json
from .collect import ROOT,write_json


def summarize(report):
    years=[]
    for chunk in report['chunks']:
        year=chunk['year'];issues=[];raw=chunk['quality']['raw']
        for symbol,q in raw.items():
            reasons=[]
            if not q['records']:reasons.append('no_bars_returned')
            if q['missing_sessions']:reasons.append('missing_sessions_require_event_review')
            if q['invalid_rows'] or q['zero_volume_rows']:reasons.append('invalid_or_zero_volume_rows')
            if q['duplicate_dates'] or q['non_session_dates'] or not q['ordered']:reasons.append('calendar_or_order_error')
            if symbol in chunk['adjustment_date_mismatch_symbols']:reasons.append('adjustment_date_mismatch')
            if reasons:issues.append({'symbol':symbol,'reasons':reasons,'records':q['records'],
                'first_date':q['first_date'],'last_date':q['last_date'],'missing_dates':q['missing_dates']})
        years.append({'year':year,'requested_symbols':len(chunk['symbols']),
            'symbols_with_any_bars':sum(q['records']>0 for q in raw.values()),
            'padded_records':sum(q['records'] for q in raw.values()),
            'missing_padded_symbol_sessions':sum(q['missing_sessions'] for q in raw.values()),
            'reasons':dict(Counter(reason for row in issues for reason in row['reasons'])),
            'issues':issues})
    # 価格そのものを開かずに作る報告なので、中心期間の実レコード数はここでは推測しない。
    return {'collection_complete':report['collection_complete'],'years':years,'returns_computed':False,
            'missing_periods_not_classified_as_no_trading':True,'backtest_admission_complete':False}


def main():
    report=json.loads((ROOT/'diagnostics/history/expansion/summary.json').read_text())
    out=summarize(report)
    write_json(ROOT/'diagnostics/history/expansion/quality-overview.json',out)
    print(json.dumps([{k:v for k,v in y.items() if k!='issues'} for y in out['years']]))

if __name__=='__main__':main()
