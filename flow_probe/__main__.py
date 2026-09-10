"""実行結果を日本語の要約と、秘密を含まない診断データにする。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .alpaca import run_market
from .http_client import ProbeError, SafeHttp
from .sec import run_sec


def describe_status(probe: dict) -> str:
    if probe["status"] == "ok":
        return "取得完了" if probe["records"] else "応答成功・データ0件"
    if probe["status"] == "partial_page_limit":
        return "ページ上限で途中終了"
    return probe.get("error", {}).get("category", probe["status"])


def markdown(report: dict) -> str:
    lines = ["# 第1段階：データ取得試験", "", f"実行時刻：{report['started_at']}",
             f"プログラム版：{report['version']}／変更番号：{report['commit']}", "",
             "これは接続とデータ品質の試験です。機関流入や投資の優位性を判定した結果ではありません。", ""]
    market = report["market"]
    if "error" in market:
        lines.extend(["市場データ：" + market["error"]["category"], ""])
    if market.get("session"):
        lines += ["直近の試験対象取引日：" + market["session"]["date"], ""]
    lines += ["| 試験 | 結果 | 件数 | ページ | 秒 |", "| --- | --- | ---: | ---: | ---: |"]
    for p in market.get("probes", []):
        lines.append(f"| {p['name']} | {describe_status(p)} | {p['records']} | {p['pages']} | {p['elapsed_seconds']} |")
    lines += ["", "配信区分は要求時に明示しています。全米市場（sip）から単一取引所（iex）への自動切り替えはしません。", "",
              "| 銘柄 | 1分足の通常取引時間内の件数 | 存在しない分の数 | 形式不整合 |",
              "| --- | ---: | ---: | ---: |"]
    for p in market.get("probes", []):
        if p["name"] == "sip_minutes_20sessions":
            for symbol, q in p["quality"].items():
                missing = q.get("missing_regular_minutes")
                lines.append(f"| {symbol} | {q.get('observed_regular_minutes', 0)} / {q.get('expected_regular_minutes', 0)} | {missing if missing is not None else '未判定'} | {q['invalid_records']} |")
    lines += ["", "1分足が存在しない理由には、売買がなかった場合や集計対象条件もあります。取得障害と即断せず、ゼロで埋めません。", "",
              "保有情報の試験：" + report["sec"].get("status", "unknown"),
              "会社名・銘柄コード・証券番号の一般的な対応表と、全機関の重複整理は次の段階で整備が必要です。", "",
              "公開するのは診断情報だけです。キー、認証ヘッダー、生の価格・約定・気配は出力していません。", "",
              "詳細な診断情報は同じ実行のログ内の STAGE1_DIAGNOSTICS を確認できます。", ""]
    return "\n".join(lines)


def assert_no_secrets(text: str, values: list[str]) -> None:
    # マスキング頼みではなく、公開する直前にも実際の秘密値との一致を検査。
    if any(value and value in text for value in values):
        raise ProbeError("secret_detected_output_suppressed")


def main() -> int:
    parser = argparse.ArgumentParser(description="Alpacaと公開保有資料の読み取り試験")
    parser.add_argument("--output-dir", default="diagnostics")
    parser.add_argument("--sec-only", action="store_true", help="市場のキーを使わず公的資料だけ試す")
    args = parser.parse_args()
    now = datetime.now(timezone.utc)
    key = os.environ.get("ALPACA_API_KEY", "").strip()
    secret = os.environ.get("ALPACA_SECRET_KEY", "").strip()
    report = {"schema_version": 1, "version": __version__, "started_at": now.isoformat(),
              "commit": os.environ.get("GITHUB_SHA", "local"),
              "credentials_present": {"key": bool(key), "secret": bool(secret)},
              "market": {}, "sec": {}}
    market_client = SafeHttp(key, secret)
    if args.sec_only:
        report["market"] = {"status": "not_requested"}
    elif not key or not secret:
        report["market"] = {"status": "blocked", "error": {"category": "missing_github_secrets"}}
    else:
        print("市場データの読み取り試験を開始します。")
        try:
            report["market"] = run_market(market_client, now)
        except ProbeError as exc:
            report["market"] = {"status": "blocked", "error": exc.summary()}
        except Exception as exc:
            # 未知の例外に含まれ得る秘密値や応答本文を、そのまま表示しない。
            report["market"] = {"status": "error", "error": {"category": "unexpected_" + type(exc).__name__}}
    report["market_http"] = market_client.metrics()
    print("公的な保有資料の読み取り試験を開始します。")
    sec_client = SafeHttp(interval=0.6, timeout=12, max_requests=40, max_seconds=180)
    try:
        report["sec"] = run_sec(sec_client, now.date().isoformat())
    except ProbeError as exc:
        report["sec"] = {"status": "blocked", "error": exc.summary()}
    except Exception as exc:
        report["sec"] = {"status": "error", "error": {"category": "unexpected_" + type(exc).__name__}}
    report["sec_http"] = sec_client.metrics()
    report["completed_at"] = datetime.now(timezone.utc).isoformat()
    encoded = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    summary = markdown(report)
    assert_no_secrets(encoded + summary, [key, secret])
    destination = Path(args.output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "summary.json").write_text(encoded + "\n", encoding="utf-8")
    (destination / "summary.md").write_text(summary, encoding="utf-8")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
            handle.write(summary)
    print(summary)
    print("STAGE1_DIAGNOSTICS_BEGIN")
    print(encoded)
    print("STAGE1_DIAGNOSTICS_END")
    # SECの全機関集計はまだ未完成。市場の必須試験に失敗した実行を緑にしない。
    return 0 if args.sec_only or report["market"].get("required_market_probe_ok") else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ProbeError as exc:
        print("試験を停止しました：" + exc.category)
        sys.exit(2)
