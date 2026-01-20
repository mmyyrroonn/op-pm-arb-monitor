import time
from typing import Any, Dict, Optional

import requests

from opinion_frontend_fetch import fetch_market_depth


class RateLimiter:
    def __init__(self, min_interval: float) -> None:
        self.min_interval = max(0.0, float(min_interval))
        self._next = 0.0

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        now = time.monotonic()
        if now < self._next:
            time.sleep(self._next - now)
        self._next = time.monotonic() + self.min_interval


def _unwrap_book(book: Dict[str, Any]) -> Dict[str, Any]:
    if "result" in book and isinstance(book["result"], dict):
        inner = book["result"].get("data") or book["result"]
        if isinstance(inner, dict):
            return inner
    return book


def _normalize_book(book: Dict[str, Any]) -> Dict[str, Any]:
    inner = _unwrap_book(book)
    if not isinstance(inner, dict):
        return {"bids": [], "asks": []}
    bids = inner.get("bids") or []
    asks = inner.get("asks") or []
    return {"bids": bids, "asks": asks}


class OpinionOpenApiClient:
    def __init__(self, host: str, api_key: str, min_interval: float, timeout: float) -> None:
        self.host = host.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.limiter = RateLimiter(min_interval)
        self.session = requests.Session()

    def fetch_orderbook(self, token_id: str) -> Dict[str, Any]:
        if not self.api_key:
            raise RuntimeError("Missing OPINION API key for openapi data fetch.")
        self.limiter.wait()
        url = f"{self.host}/token/orderbook"
        headers = {"apikey": self.api_key, "Accept": "application/json"}
        resp = self.session.get(url, headers=headers, params={"token_id": token_id}, timeout=self.timeout)
        if resp.status_code != 200:
            raise RuntimeError(f"Opinion openapi orderbook HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        if isinstance(data, dict) and data.get("errno") not in (0, None):
            raise RuntimeError(f"Opinion openapi orderbook errno={data.get('errno')}: {data}")
        return _normalize_book(data)


class OpinionFrontendClient:
    def __init__(
        self,
        auth_token: str,
        device_fingerprint: str,
        waf_token: str = "",
    ) -> None:
        self.auth_token = auth_token
        self.device_fingerprint = device_fingerprint
        self.waf_token = waf_token

    def fetch_orderbook(
        self,
        question_id: str,
        symbol: str,
        symbol_types: int,
    ) -> Dict[str, Any]:
        data = fetch_market_depth(
            question_id=question_id,
            symbol=symbol,
            symbol_types=symbol_types,
            auth_token=self.auth_token,
            device_fingerprint=self.device_fingerprint,
            waf_token=self.waf_token,
        )
        return _normalize_book(data)
