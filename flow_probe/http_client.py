"""外部サービスとの通信を一か所に集め、秘密情報を出力しない。"""

from __future__ import annotations

import json
import socket
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


class ProbeError(Exception):
    """公開してよい固定の分類だけを持つエラー。応答本文は保存しない。"""

    def __init__(self, category: str, status: int | None = None):
        self.category = category
        self.status = status
        super().__init__(category)

    def summary(self) -> dict:
        return {"category": self.category, "http_status": self.status}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # 認証ヘッダーが別の宛先へ引き継がれることを防ぐ。
        return None


def error_category(status: int, body: bytes) -> str:
    """提供元の自由文をそのまま公開せず、既知の原因だけ分類する。"""
    lower = body.lower()
    if b"subscription does not permit" in lower or b"not subscribed" in lower:
        return "subscription_restriction"
    return {
        400: "invalid_request", 401: "authentication_failed",
        403: "forbidden_check_auth_and_entitlement", 404: "not_found",
        429: "rate_limited",
    }.get(status, "server_error" if status >= 500 else "http_error")


class SafeHttp:
    """GET専用。再試行・間隔・時間・容量を制限して読み取りだけ行う。"""

    def __init__(self, key: str = "", secret: str = "", *, opener=None,
                 interval: float = 0.5, timeout: float = 15,
                 max_requests: int = 180, max_seconds: float = 480):
        self._auth = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
        self._opener = opener or build_opener(NoRedirect())
        self.interval = interval
        self.timeout = timeout
        self.max_requests = max_requests
        self.max_seconds = max_seconds
        self.started = time.monotonic()
        self.last_request = 0.0
        self.calls = 0
        self.total_bytes = 0
        self.retry_count = 0

    def read(self, url: str, params: dict | None = None,
             *, max_bytes: int = 16_000_000) -> bytes:
        parsed = urlparse(url)
        host = parsed.hostname
        allowed = {"data.alpaca.markets", "paper-api.alpaca.markets",
                   "data.sec.gov", "www.sec.gov"}
        if parsed.scheme != "https" or host not in allowed or parsed.username:
            raise ProbeError("destination_not_allowed")
        if host == "paper-api.alpaca.markets" and not (
            parsed.path == "/v2/calendar" or parsed.path.startswith("/v2/assets/")
        ):
            # 口座残高・注文などにはアクセスできないよう、経路も限定する。
            raise ProbeError("endpoint_not_allowed")
        if host == "data.alpaca.markets" and parsed.path not in {
            "/v2/stocks/bars", "/v2/stocks/trades", "/v2/stocks/quotes"
        }:
            raise ProbeError("endpoint_not_allowed")
        headers = {
            "User-Agent": "institutional-flow-monitor/0.1 "
                          "(research; https://github.com/nekoromme/-institutional-flow-monitor)",
            "Accept": "application/json, application/xml, text/xml",
        }
        if host in {"data.alpaca.markets", "paper-api.alpaca.markets"}:
            headers.update(self._auth)
        full_url = url + ("?" + urlencode(params) if params else "")
        for attempt in range(3):
            if self.calls >= self.max_requests or time.monotonic() - self.started > self.max_seconds:
                raise ProbeError("request_or_time_budget_exceeded")
            time.sleep(max(0, self.interval - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            self.calls += 1
            try:
                # 必ずGET。公開レポートにURL全体やヘッダーを流さない。
                request = Request(full_url, headers=headers, method="GET")
                with self._opener.open(request, timeout=self.timeout) as response:
                    body = response.read(max_bytes + 1)
                    self.total_bytes += len(body)
                    if len(body) > max_bytes:
                        raise ProbeError("response_too_large")
                    return body
            except HTTPError as exc:
                body = exc.read(4096)
                self.total_bytes += len(body)
                category = error_category(exc.code, body)
                if exc.code in {429, 500, 502, 503, 504} and attempt < 2:
                    # 無限再試行しない。指定待ち時間が長い時は今回は保留する。
                    retry_after = exc.headers.get("Retry-After", "") if exc.headers else ""
                    if retry_after.isdigit() and int(retry_after) > 20:
                        raise ProbeError("rate_limit_wait_exceeds_probe_budget", exc.code) from None
                    delay = int(retry_after) if retry_after.isdigit() else 2 ** (attempt + 1)
                    self.retry_count += 1
                    time.sleep(delay)
                    continue
                raise ProbeError(category, exc.code) from None
            except (URLError, TimeoutError, socket.timeout, ConnectionError):
                if attempt < 2:
                    self.retry_count += 1
                    time.sleep(2 ** attempt)
                    continue
                raise ProbeError("network_or_timeout") from None
        raise ProbeError("retry_exhausted")

    def json(self, url: str, params: dict | None = None) -> object:
        try:
            return json.loads(self.read(url, params))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ProbeError("invalid_json_response") from None

    def metrics(self) -> dict:
        return {"http_requests": self.calls, "received_bytes": self.total_bytes,
                "retries": self.retry_count,
                "elapsed_seconds": round(time.monotonic() - self.started, 3)}
