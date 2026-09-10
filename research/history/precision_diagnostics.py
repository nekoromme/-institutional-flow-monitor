"""残った補正差を項目別に説明する。品質基準や売買条件は変更しない。"""
import json
import math
import os
from decimal import Decimal
from flow_probe.returns_core import by_date
from .adjustment_scan import load_development
from .collect import ROOT,write_json
from .event_returns import prepare_prices,shares_between


def main():
    secret=os.environ.get('ALPACA_SECRET_KEY','')
    # 2023年用ファイルだけを読み、2025年用の封印データを開かない。
    payload=load_development(ROOT,2023,secret)
    events=json.loads((ROOT/'research/history/evidence/validation-events.json').read_text())['events']
    books,_=prepare_prices(payload,events,allow_cent_rounding=True);rows=[]
    for symbol,book in books.items():
        adjusted=by_date(payload['prices']['split'][symbol])
        for day,item in book.items():
            if item['valid']:continue
            raw=item['raw'];other=adjusted.get(day)
            if other is None:continue
            factor=1/shares_between(events,symbol,day,payload['retrieved_at_utc'][:10])
            fields={}
            for k in ('o','h','l','c'):
                observed=Decimal(str(other[k]));expected=Decimal(str(raw[k]))*Decimal(str(factor))
                fields[k]={'passes_original_tolerance':math.isclose(other[k],raw[k]*factor,rel_tol=1e-8,abs_tol=.0001*(factor+1)),
                           'is_cent_grid':observed==observed.quantize(Decimal('.01')),
                           'absolute_error':float(abs(observed-expected)),
                           'within_half_cent':abs(observed-expected)<=Decimal('.005')}
            rows.append({'symbol':symbol,'date':day,'fields':fields,
                         'each_field_passes_original_or_cent_rule':all(x['passes_original_tolerance'] or (x['is_cent_grid'] and x['within_half_cent']) for x in fields.values())})
    out={'year':2023,'rows':rows,'returns_computed':False,'quality_policy_changed':False,
         'interpretation':'Per-field explanation only; retained all-fields cent rule is unchanged.'}
    write_json(ROOT/'diagnostics/history/expansion/precision-residuals.json',out)
    print(json.dumps({'precision_residual_days':len(rows),'explained_by_mixed_precision':sum(r['each_field_passes_original_or_cent_rule'] for r in rows)}))

if __name__=='__main__':main()
