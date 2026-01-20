import random
import time
from typing import Any, Dict, List, Optional, Tuple

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


def _split_csv(raw: str) -> List[str]:
    if not raw:
        return []
    cleaned = raw.replace(",", " ")
    return [item for item in cleaned.split() if item]


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
        auth_mode: str = "random",
    ) -> None:
        self.auth_tokens = _split_csv(auth_token)
        self.device_fingerprints = _split_csv(device_fingerprint)
        self.waf_token = waf_token
        self.auth_mode = (auth_mode or "random").lower()
        self._rr_index = 0
        self._warned_fp_mismatch = False

    def fetch_orderbook(
        self,
        question_id: str,
        symbol: str,
        symbol_types: int,
        worker_idx: Optional[int] = None,
    ) -> Dict[str, Any]:
        auth_token, device_fingerprint = self._pick_auth(worker_idx)
        data = fetch_market_depth(
            question_id=question_id,
            symbol=symbol,
            symbol_types=symbol_types,
            auth_token=auth_token,
            device_fingerprint=device_fingerprint,
            waf_token=self.waf_token,
        )
        return _normalize_book(data)

    def _pick_auth(self, worker_idx: Optional[int]) -> Tuple[str, str]:
        if not self.auth_tokens:
            return "", self.device_fingerprints[0] if self.device_fingerprints else ""

        idx = 0
        if len(self.auth_tokens) > 1:
            mode = self.auth_mode
            if mode in ("worker", "worker_round_robin", "worker_rr"):
                if worker_idx is not None:
                    idx = worker_idx % len(self.auth_tokens)
                else:
                    idx = self._rr_index % len(self.auth_tokens)
                    self._rr_index += 1
            elif mode in ("round_robin", "rr"):
                idx = self._rr_index % len(self.auth_tokens)
                self._rr_index += 1
            else:
                idx = random.randrange(len(self.auth_tokens))

        token = self.auth_tokens[idx]
        fp = ""
        if self.device_fingerprints:
            if len(self.device_fingerprints) == 1:
                fp = self.device_fingerprints[0]
            elif len(self.device_fingerprints) == len(self.auth_tokens):
                fp = self.device_fingerprints[idx]
            else:
                fp = self.device_fingerprints[0]
                if not self._warned_fp_mismatch:
                    print("[WARN] frontend device fingerprints count mismatch; using first fingerprint")
                    self._warned_fp_mismatch = True
        return token, fp
