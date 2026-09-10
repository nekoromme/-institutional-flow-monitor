"""今回確認した公開資料。対象期間を拡張するときは更新が必要。"""

# 旧株数 / 新株数。値動きから推測せず、発行会社の正式資料で確認したもの。
SPLIT_EVENTS = {
    "UAVS": [
        {"trading_day": "2024-02-09", "old_shares": 20, "new_shares": 1,
         "source": "https://www.sec.gov/Archives/edgar/data/8504/000149315224005564/form8-k.htm"},
        {"trading_day": "2024-10-15", "old_shares": 50, "new_shares": 1,
         "source": "https://www.sec.gov/Archives/edgar/data/8504/000149315224041068/form8-k.htm"},
    ],
    "QMCO": [
        {"trading_day": "2024-08-27", "old_shares": 20, "new_shares": 1,
         "source": "https://investors.quantum.com/news-events/press-releases/detail/203/quantum-announces-reverse-stock-split"},
    ],
}

# 今回の二期に限って照合を検証する。新しい報告期へ自動延長しない。
SECURITY_SAMPLES = {
    "UAVS": {"cusip": "00848K309", "valid_from": "2024-10-15",
             "verified_through": "2026-06-30", "source": SPLIT_EVENTS["UAVS"][1]["source"]},
    "QMCO": {"cusip": "747906600", "valid_from": "2024-08-27",
             "verified_through": "2026-06-30", "source": SPLIT_EVENTS["QMCO"][0]["source"]},
}


def documented_price_factor(symbol: str, day: str, through: str) -> float:
    factor = 1.0
    for event in SPLIT_EVENTS.get(symbol, []):
        if day < event["trading_day"] <= through:
            factor *= event["old_shares"] / event["new_shares"]
    return factor
