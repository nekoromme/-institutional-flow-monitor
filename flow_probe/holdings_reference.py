"""今回、原提出書類で確認した分母。別の日・別の株数へ自動で流用しない。"""

UAVS_Q1_SOURCE = {
    "source_url": "https://www.sec.gov/Archives/edgar/data/8504/000143774926017229/uavs20260331_10q.htm",
    "sha256": "06e6c48a0644a3e66be9820c468ffa322120810c0ee9d244fba6ba1a8ca65464",
    "accession": "0001437749-26-017229", "filed": "2026-05-15",
    "location": "Condensed consolidated balance sheets; common stock issued and outstanding",
}
QMCO_Q3_SOURCE = {
    "source_url": "https://www.sec.gov/Archives/edgar/data/709283/000162828026008558/qtm-20251231.htm",
    "sha256": "fa7f86760c243ac1682fb3d5a4fdfed068ac128513556dd74de76e6e4360845b",
    "accession": "0001628280-26-008558", "filed": "2026-02-17",
}

# 2026年初の対象選び用。2025年秋に公表された原書類の普通株数を確認。
# 2026年公表の分母を過去へ流用せず、この提出番号・株数の組だけを許可する。
DENOMINATOR_REVIEWS = (
    {"symbol": "UAVS", "end": "2025-09-30", "shares": 36734690, "taxonomy": "us-gaap",
     "accession": "0001437749-25-035221", "filed": "2025-11-14",
     "source_url": "https://www.sec.gov/Archives/edgar/data/8504/000143774925035221/uavs20250930_10q.htm",
     "sha256": "f146d4f1c33eb822eeeb925bcce65ac6a46870160f0252db87f1f5b2f3003dc1",
     "location": "Balance sheets; common stock issued and outstanding; not 200,000,000 authorized shares"},
    {"symbol": "QMCO", "end": "2025-09-30", "shares": 13333000, "taxonomy": "us-gaap",
     "accession": "0000709283-25-000085", "filed": "2025-11-13",
     "source_url": "https://www.sec.gov/Archives/edgar/data/709283/000070928325000085/qtm-20250930.htm",
     "sha256": "54591b895b10c532ced4c6211328ef20395c5564c61cf4749cf85ad54e0280ff",
     "location": "Balance sheets in thousands; 13,333 thousand common shares, not common stock value 133"},
    {"symbol": "UAVS", "end": "2025-12-31", "shares": 43613800, "taxonomy": "us-gaap", **UAVS_Q1_SOURCE},
    {"symbol": "UAVS", "end": "2026-03-31", "shares": 57346783, "taxonomy": "us-gaap", **UAVS_Q1_SOURCE},
    {"symbol": "QMCO", "end": "2025-12-31", "shares": 14135000, "taxonomy": "us-gaap",
     "location": "Balance sheets; 14,135 thousand common shares; not the common stock dollar amount",
     **QMCO_Q3_SOURCE},
    {"symbol": "QMCO", "end": "2026-02-12", "shares": 14638029, "taxonomy": "dei",
     "location": "Cover; common shares at close of business on February 12, 2026", **QMCO_Q3_SOURCE},
)


def reviewed_denominator(symbol, denominator):
    for review in DENOMINATOR_REVIEWS:
        keys = ("end", "shares", "taxonomy", "accession", "filed")
        if review["symbol"] == symbol and all(denominator.get(k) == review[k] for k in keys):
            return {**denominator, "security_class_verified": True,
                    "filing_review": {k: review[k] for k in ("source_url", "sha256", "location")}}
    return denominator
