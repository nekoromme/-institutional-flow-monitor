"""構造化されていない届出を原文で点検した記録。

会社名だけで例外を適用しない。提出番号・発行会社番号・全文の指紋が
すべて一致した原書類に限る。別の届出や修正版には引き継がない。
ここで確認するのは届出の対象となる証券の種類だけで、廃止日ではない。
"""

HTML_REMOVAL_REVIEWS = {
    '0001140361-25-035705': {
        'issuer_cik': '0001082324',
        'sha256': '6a7b2f715b93d97a3b6a30da8503c788170d395932591871d0be28dd3b4a41e0',
        'security_class': 'Common stock, par value $0.0001 per share',
        'reviewed_on': '2026-09-10',
    },
    '0001104659-25-121109': {
        'issuer_cik': '0001485003',
        'sha256': '9342dd64a9c72a98bc0e7888fd009f2b684af87bbd84d889c5c712de8ac6ecfc',
        'security_class': 'Common Stock, $0.001 par value per share',
        'reviewed_on': '2026-09-10',
    },
}
