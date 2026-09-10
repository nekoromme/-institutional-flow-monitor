"""機関流入を含まない独立な乱数でも、既存条件がどれほど当たるか。

これは現実の誤検知率の推定ではない。分布が変わらず、日ごとに独立で、
同じ値が出ないという単純な仮定の下で、条件そのものの性質を点検する。
本体の閾値・反復ルールをそのまま呼び、最適な設定を検索しない。
"""
import hashlib
import json
import math
from pathlib import Path
import random
import sys

# リポジトリのルートを明示し、研究用スクリプトから本体の判定を呼ぶ。
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from flow_probe.pilot import classify

SEED = 614329
PATHS = 10000
HORIZONS = (20, 40, 61)


def run():
    generator = random.Random(SEED)
    counts = {h:{'any_abnormal':0, 'any_repeated':0} for h in HORIZONS}
    abnormal_days = 0
    for _ in range(PATHS):
        # 最初の60日が基準作成、次の4日が反復の準備。その後61日を評価。
        values = [1+generator.random() for _ in range(64+max(HORIZONS))]
        judgments = []
        repeated = []
        for i in range(60, len(values)):
            row = {'status':'prepared', 'raw_volume':values[i],
                   'baseline_volumes_in_decision_day_shares':values[i-60:i]}
            flag = classify(row)['abnormal']
            judgments.append(flag)
            if i >= 64:
                repeated.append(flag and sum(judgments[-5:]) >= 2)
        evaluated = judgments[4:]
        abnormal_days += sum(evaluated)
        for horizon in HORIZONS:
            counts[horizon]['any_abnormal'] += any(evaluated[:horizon])
            counts[horizon]['any_repeated'] += any(repeated[:horizon])
    results = {}
    for horizon, items in counts.items():
        results[str(horizon)] = {}
        for name, n in items.items():
            p = n/PATHS
            results[str(horizon)][name] = {'paths':n,'fraction':p,
                'simulation_standard_error_percentage_points':100*math.sqrt(p*(1-p)/PATHS)}
    return {'version':'null-volume-check-0.1','seed':SEED,'independent_simulated_paths':PATHS,
            'assumption':'iid_continuous_positive_volumes_no_institutional_component_no_short_sessions',
            # 60個の57番目を超える新しい値の順位は、61個中58〜61番目の4通り。
            # 最近傍順位による95%点の定義が誤りなのではなく、5%検定とは異なる。
            'exact_marginal_abnormal_probability_under_assumption':4/61,
            'simulated_abnormal_day_fraction':abnormal_days/(PATHS*61),
            'horizons_sessions':results,'production_threshold_changed':False,
            'real_market_false_positive_rate':None,
            'script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'classifier_sha256':hashlib.sha256((ROOT/'flow_probe/pilot.py').read_bytes()).hexdigest()}


if __name__=='__main__':
    result=run()
    destination=ROOT/'docs/evidence/null-volume-check-2026-09-10.json'
    destination.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False,indent=2))
