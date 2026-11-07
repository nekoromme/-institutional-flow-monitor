# 株価分析の共通環境

2026-11-07 初版。既存の機関保有・異常出来高研究を残し、日々の質問に使う共通入口を追加。
コードを保存する場所、データの取得口、実験条件、検証結果を一か所から辿れる。
**ツール整備と予測精度の実証は別。今回の実装で勝率が上がったとは主張しない。**

## できること

| 分野 | 現在の機能 |
| --- | --- |
| 米株・ETF | Alpaca SIP日足、取引日一覧、分割補正、全ページ取得、欠損検査 |
| テクニカル | Wilder RSI14、20/50/200/250日移動平均と乖離、出来高倍率、騰落率 |
| 過去照合 | 急落、小十字、RSI条件を翌営業日始値→1/3/5/10/20日目終値で比較 |
| リスクと反例 | 費用、指数との差、保有中の最大下落・上昇、最安値の日、年度別、最大当たり除外 |
| SEC | 提出履歴とXBRL原データ、既存13F分析への導線 |
| 経済指標 | FREDの当時の改定版を指定取得。キーが別途必要 |
| 外部データ | 出所・通貨・取引日一覧付き価格CSV、時刻と実測/推計の区分付き根拠JSON |
| 米韓・ADR | 同時点・為替方向・預託比率を明示したプレミアム計算関数。非同期終値は拒否 |
| 再現 | SQLiteの取得版・内容ハッシュ・実験条件・コードハッシュで結果を固定 |

22銘柄は現在の研究対象。推奨銘柄一覧でも、過去の全銘柄を網羅した一覧でもない。
韓国本国株、過去オプション、Vanda等の主体別フローは取得済みと見なさない。
細かい実装状況と公式仕様は `config/data-sources.json` を参照。

## 日次更新

`株価分析ハブ・検証とデータ更新` が日本時間の火〜土15:35に動く設定。
米国の時間外も終わったNY前日まで、直近約5年の22銘柄を更新する。
朝の速報は既存の調査タスクを使い、この収集は日足の再現検証用とする。
GitHubの定期実行は遅延・停止する場合がある。成功した最終時刻を確認する。

1. 既存試験と共通環境の試験を実行。
2. 前回成功した暗号化DBを復元。なければ履歴を再取得して開始。
3. SIP日足を取得。配信区分の自動切替なし。途中ページの失敗は失敗として終了。
4. `config/research-request.json` の条件を実行。集計だけ公開し詳細はDBへ。
5. DBを暗号化して復号照合。キャッシュと公開診断を別の成果物として保存。

**キャッシュは90日保持で、永久保管ではない。** 連続実行で引き継ぐ。
中断・artifact削除・キー変更時は復元できない場合がある。必要なら履歴を再取得する。
研究に使用した取得版と実験記録はDB内で保持、使っていない旧価格版だけ整理する。
長期運用で研究が増えた時は容量を計測し、専用の非公開永続保存先へ移す。
今のDBはActions内にあり、ChatGPTは秘密キーを取得できない。
わたしは依頼JSONを更新して実行し、公開された集計を読んで次の分析へ進む。

## 手元での操作（通常は分析担当が行う）

Python 3.12。指標と取得に大型の分析環境は不要。暗号化だけ依存パッケージあり。

```bash
python -m pip install -r requirements-hub.txt
python -m market_hub fetch --symbols SOXL,SOXX --start 2021-11-01 --end 2026-11-06
python -m market_hub inventory
# 次のIDはinventoryに返った実際のIDに置き換える。
python -m market_hub study --dataset SOXL_DATASET_ID --benchmark SOXX_DATASET_ID --signal drop --threshold 0.05 --horizons 1,3,5,10,20 --test-start 2025-01-01
python -m market_hub sec --cik 723125
python -m market_hub macro --series DGS10 --start 2024-01-01 --end 2024-12-31 --vintage 2025-01-02
```

APIキーは環境変数 `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` / 必要時のみ `FRED_API_KEY`。
既存のGitHub Actions秘密設定を使う。チャットやコードに値を書かない。
ローカルDBは `data/hub/market.sqlite3`、詳細レポートは `data/hub/latest-study.json`。
公開集計は `market-hub-public-status` 成果物内の `study-summary.json`。

## CSVと根拠データの持ち込み

価格CSV列：`date,open,high,low,close,volume`。一銘柄一ファイル。
メタデータJSONは `source,symbol,source_url,retrieved_at_utc,currency,exchange_timezone,feed,adjustment,timeframe,session_scope,sessions,start,end` 必須。
`sessions` は価格が存在する日を並べるのではなく、取引所の本来の営業日一覧。
価格のない日は欠損のままにする。補正が未確認なら `adjustment=unknown` とし、検証に使わない。

```bash
python -m market_hub import-prices --csv data/vendor/prices.csv --metadata data/vendor/metadata.json
python -m market_hub import-evidence --json data/vendor/evidence.json
```

根拠JSONは `source,symbol,source_url,published_at,observed_at,retrieved_at_utc,measurement,coverage,value` 必須。
時刻はタイムゾーン付きISO形式。`measurement` は `measured` / `estimated` / `opinion`。
`coverage` には全市場/特定取引所/特定証券会社/日中途中/終日などの範囲を明記。
ニュース・売買主体・予測記事の根拠保管口であり、自動の真偽判定や売買シグナル化は行わない。

## 検証の読み方

- シグナルは確定日足から判定。翌取引日始値で買い、h取引日目の終値で売る仮定。
- 片道費用25bpsは0.25%。実際のスプレッド・金利・税・配当を完全再現しない。
- 重複保有は既定で除外。日付分割を跨ぐ取引も除外。除外件数と理由を出す。
- カレンダーと連続した価格窓を照合。不足した日を飛ばして「5営業日」としない。
- SOXLはSOXL自身の価格。SOXXの騰落を3倍して代用しない。
- 初回RSIは14変化分の平均、以後Wilder平滑化。データ開始直後は初期値依存が残る。
- 小十字は実体/高安差の一般的定義。Shenの条件を再現したと呼ばない。
- `low_session` は買ってから最安値をつけた取引日番号。同じ安値なら最初の日。
- 日足は通常時間限定とは保証しない。プレ→寄りや下髭の約定順序には分足・約定が必要。
- split補正は取得時の版。完全な当時版データとは呼ばない。配当込みリターンでもない。
- 勝率95%区間は独立試行を仮定した近似。相場の連続性や多数の条件探索を補正しない。
- 日付分割だけで未知期間の検証が成立したと呼ばない。結果を見た後の変更は探索扱い。

## 次の拡張の優先順位

1. データ源間の価格照合と企業行動確認、取得時刻・欠損の継続監査。
2. 指数下落率・ボラティリティ・金利など、事前に定めた相場条件ごとの比較。
3. 公表時刻を揃えた決算・FOMC・雇用統計・選挙のイベント入力と比較用ベースライン。
4. KRX・ADRの権利比率・為替・取引時刻の裏取りと接続。
5. オプション等は履歴の利用権と追加価値を確認してから。有料契約は勝手に開始しない。

便利そうなライブラリを大量に並べることを目標にしない。
同じデータ・同じ定義で再計算でき、反例を見つけられる機能を優先する。
