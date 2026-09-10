"""結果を見る前に固定した比較条件を、各処理で共用する。"""
import json
from .bulk13f import digest
PROTOCOL_PATH = 'docs/evidence/batch-comparison-protocol-2026-09-10.json'
PROTOCOL_SHA = 'ebb17d254790110735dd1de7952b88773f3139b462c3742dbaa2f853b66c3a92'


def load_protocol(root):
    if digest(root / PROTOCOL_PATH) != PROTOCOL_SHA:
        raise ValueError('batch_protocol_changed_requires_new_version')
    return json.loads((root / PROTOCOL_PATH).read_text())


def observed_change(a, b):
    """報告合計の増減と、両期に観測された運用者だけの増減を分ける。

一方の期の行がない場合、その期に本当に保有ゼロだったとは言わない。
いずれも日単位の実際の機関売買を表す正解ラベルではない。
    """
    by_manager = lambda r: {x['cik']: x['shares'] for x in r['ledger'] if x['shares'] > 0}
    x, y = by_manager(a), by_manager(b)
    both = x.keys() & y.keys()
    delta = (b['reported_share_sum_before_overlap_resolution'] - a['reported_share_sum_before_overlap_resolution']) if x and y else None
    matched = sum(y[k] - x[k] for k in both) if both else None
    return {'observed_reported_share_change': delta,
            'observed_reported_increase': delta > 0 if delta is not None else None,
            'matched_manager_share_change': matched,
            'matched_manager_increase': matched > 0 if matched is not None else None,
            'matched_managers': len(both), 'previous_only_managers': len(x.keys() - y.keys()),
            'current_only_managers': len(y.keys() - x.keys()), 'actual_institutional_net_buy': None}
