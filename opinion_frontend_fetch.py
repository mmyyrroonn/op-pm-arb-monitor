#!/usr/bin/env python3
import argparse
import copy
import asyncio
import json
import math
import os
import random
import time
import threading
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional, Tuple, List, Iterable, Sequence

import aiohttp
import requests
from requests.adapters import HTTPAdapter
from dotenv import load_dotenv

TOPIC_API_URL = "https://proxy.opinion.trade:8443/api/bsc/api/v2/topic"
DEPTH_API_URL = "https://proxy.opinion.trade:8443/api/bsc/api/v2/order/market/depth"

DEFAULT_AUTH_TOKEN = (
    "Bearer .eyJ1c2VyX2lkIjoxMTgwODc3LCJ3YWxsZXRfYWRkcmVzcyI6IjB4NTVkZmVhZGRhMThmMDJjMWIxNjljOTU1NTJkNThhYjAyMGU5MTA5NiIsIndhbGxldF91c2VyIjp7fSwiaXNzIjoicHJlZGljdGlvbl9tYXJrZXQiLCJleHAiOjE3NjgzOTA5OTEsImlhdCI6MTc2ODMwNDU5MX0.UqIZjjnzfpEweeUYR2rn3UKLrzpuDNpW8uArnM1ZRPQ"
)
DEFAULT_DEVICE_FINGERPRINT = "1891d28b29ed0df165ecae3b7094474a"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/143.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT = (6, 20)
DEFAULT_HTTP_RETRIES = 4
DEFAULT_HTTP_BACKOFF = 0.6
DEFAULT_OUTPUT = "opinion_topics_cache.json"
DEFAULT_MERGED_OUTPUT = "opinion_topics_merged.json"
DEFAULT_DEPTH_OUTPUT = "opinion_depth_cache.json"
DEFAULT_DEPTH_UI_OUTPUT = "opinion_depth_ui.json"

ITEM_KEYS = ("list", "records", "items", "topics", "rows", "data")
TOTAL_KEYS = (
    "total",
    "totalCount",
    "total_count",
    "count",
    "totalElements",
    "total_elements",
)


_tls = threading.local()


def _build_session() -> requests.Session:
    s = requests.Session()
    adapter = HTTPAdapter(
        max_retries=0,
        pool_connections=64,
        pool_maxsize=64,
    )
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def _get_session(name: str) -> requests.Session:
    sess = getattr(_tls, name, None)
    if sess is None:
        sess = _build_session()
        setattr(_tls, name, sess)
    return sess


def _request_with_retry(
    method: str,
    url: str,
    *,
    session: requests.Session,
    headers: Optional[Dict[str, str]] = None,
    params: Optional[Dict[str, Any]] = None,
    timeout=DEFAULT_TIMEOUT,
    tries: int = DEFAULT_HTTP_RETRIES,
) -> requests.Response:
    last_err: Optional[Exception] = None
    retryable = {429, 500, 502, 503, 504}

    for attempt in range(max(1, tries)):
        try:
            resp = session.request(
                method=method,
                url=url,
                headers=headers,
                params=params,
                timeout=timeout,
            )
            if resp.status_code == 200:
                return resp
            if resp.status_code in retryable:
                retry_after = resp.headers.get("Retry-After")
                if retry_after:
                    try:
                        sleep_s = float(retry_after)
                    except Exception:
                        sleep_s = None
                else:
                    sleep_s = None
                if sleep_s is None:
                    sleep_s = (DEFAULT_HTTP_BACKOFF * (2 ** attempt)) + random.random() * 0.2
                time.sleep(sleep_s)
                last_err = RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
                continue
            return resp
        except (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.ChunkedEncodingError,
        ) as exc:
            last_err = exc
            sleep_s = (DEFAULT_HTTP_BACKOFF * (2 ** attempt)) + random.random() * 0.2
            time.sleep(sleep_s)
            continue

    raise RuntimeError(f"request failed after {tries} tries: {method} {url} last={last_err}")


def _request_json(
    method: str,
    url: str,
    *,
    session: requests.Session,
    headers: Optional[Dict[str, str]] = None,
    params: Optional[Dict[str, Any]] = None,
    timeout=DEFAULT_TIMEOUT,
    tries: int = DEFAULT_HTTP_RETRIES,
) -> Any:
    resp = _request_with_retry(
        method,
        url,
        session=session,
        headers=headers,
        params=params,
        timeout=timeout,
        tries=tries,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.json()


def _build_async_session(limit: int) -> aiohttp.ClientSession:
    timeout = aiohttp.ClientTimeout(sock_connect=DEFAULT_TIMEOUT[0], sock_read=DEFAULT_TIMEOUT[1])
    connector = aiohttp.TCPConnector(limit=limit, limit_per_host=limit)
    return aiohttp.ClientSession(timeout=timeout, connector=connector)


async def _async_request_json(
    method: str,
    url: str,
    *,
    session: aiohttp.ClientSession,
    headers: Optional[Dict[str, str]] = None,
    params: Optional[Dict[str, Any]] = None,
    tries: int = DEFAULT_HTTP_RETRIES,
) -> Any:
    last_err: Optional[Exception] = None
    retryable = {429, 500, 502, 503, 504}

    for attempt in range(max(1, tries)):
        try:
            async with session.request(method, url, headers=headers, params=params) as resp:
                if resp.status == 200:
                    return await resp.json()

                if resp.status in retryable:
                    retry_after = resp.headers.get("Retry-After")
                    if retry_after:
                        try:
                            sleep_s = float(retry_after)
                        except Exception:
                            sleep_s = None
                    else:
                        sleep_s = None
                    if sleep_s is None:
                        sleep_s = (DEFAULT_HTTP_BACKOFF * (2 ** attempt)) + random.random() * 0.2
                    await asyncio.sleep(sleep_s)
                    txt = await resp.text()
                    last_err = RuntimeError(f"HTTP {resp.status}: {txt[:200]}")
                    continue

                txt = await resp.text()
                raise RuntimeError(f"HTTP {resp.status}: {txt[:200]}")

        except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as exc:
            last_err = exc
            sleep_s = (DEFAULT_HTTP_BACKOFF * (2 ** attempt)) + random.random() * 0.2
            await asyncio.sleep(sleep_s)
            continue
        except aiohttp.ClientError as exc:
            last_err = exc
            break

    raise RuntimeError(f"request failed after {tries} tries: {method} {url} last={last_err}")


def _build_headers(
    auth_token: str,
    device_fingerprint: str,
    waf_token: str,
    user_agent: str,
) -> Dict[str, str]:
    return {
        "accept": "application/json",
        "accept-language": "zh-CN,zh;q=0.9",
        "authorization": auth_token,
        "origin": "https://app.opinion.trade",
        "referer": "https://app.opinion.trade/",
        "user-agent": user_agent,
        "x-aws-waf-token": waf_token,
        "x-device-fingerprint": device_fingerprint,
        "x-device-kind": "web",
    }


def fetch_topic_page(
    page: int = 5,
    limit: int = 12,
    auth_token: Optional[str] = None,
    auth_tokens: Optional[Sequence[str]] = None,
    device_fingerprint: Optional[str] = None,
    waf_token: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> Dict[str, Any]:
    params = {
        "labelId": "",
        "keywords": "",
        "sortBy": 5,
        "chainId": 56,
        "limit": limit,
        "status": 2,
        "isShow": 1,
        "topicType": 2,
        "page": page,
        "indicatorType": 0,
        "excludePin": 1,
    }
    headers = _build_headers(
        _choose_auth_token(auth_token, auth_tokens),
        device_fingerprint or DEFAULT_DEVICE_FINGERPRINT,
        waf_token or "",
        user_agent or DEFAULT_USER_AGENT,
    )
    session = _get_session("topic")
    return _request_json(
        "GET",
        TOPIC_API_URL,
        session=session,
        params=params,
        headers=headers,
        timeout=DEFAULT_TIMEOUT,
    )


def _extract_items(payload: Any) -> Tuple[list, Dict[str, Any]]:
    def walk(obj: Any) -> Optional[Tuple[list, Dict[str, Any]]]:
        if isinstance(obj, dict):
            for key, val in obj.items():
                if key in ITEM_KEYS and isinstance(val, list):
                    return val, obj
            for val in obj.values():
                found = walk(val)
                if found is not None:
                    return found
        elif isinstance(obj, list):
            if all(isinstance(x, dict) for x in obj):
                return obj, {}
            for item in obj:
                found = walk(item)
                if found is not None:
                    return found
        return None

    found = walk(payload)
    if found is None:
        return [], {}
    return found


def _extract_total(payload: Any) -> Optional[int]:
    def walk(obj: Any) -> Optional[int]:
        if isinstance(obj, dict):
            for key in TOTAL_KEYS:
                if key in obj:
                    try:
                        return int(obj[key])
                    except Exception:
                        return None
            for val in obj.values():
                found = walk(val)
                if found is not None:
                    return found
        elif isinstance(obj, list):
            for item in obj:
                found = walk(item)
                if found is not None:
                    return found
        return None

    return walk(payload)


def _first_csv_value(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None
    parts = [item.strip() for item in raw.split(",") if item.strip()]
    if not parts:
        return None
    return parts[0]


def _split_csv_values(raw: Optional[str]) -> List[str]:
    if not raw:
        return []
    parts = [item.strip() for item in raw.split(",") if item.strip()]
    return parts


def _normalize_auth_tokens(raw: Optional[str]) -> List[str]:
    tokens = _split_csv_values(raw)
    normalized: List[str] = []
    for token in tokens:
        if token.startswith("Bearer "):
            normalized.append(token)
        else:
            normalized.append(f"Bearer {token}")
    return normalized


def _choose_auth_token(
    auth_token: Optional[str],
    auth_tokens: Optional[Sequence[str]],
) -> str:
    if auth_tokens:
        return random.choice(auth_tokens)
    if auth_token:
        return auth_token
    return DEFAULT_AUTH_TOKEN


def _pick_worker_value(values: Optional[Sequence[str]], worker_idx: int) -> Optional[str]:
    if not values:
        return None
    return values[worker_idx % len(values)]


def _pick_worker_auth(
    auth_token: Optional[str],
    auth_tokens: Optional[Sequence[str]],
    worker_idx: int,
) -> str:
    if auth_tokens:
        return auth_tokens[worker_idx % len(auth_tokens)]
    if auth_token:
        return auth_token
    return DEFAULT_AUTH_TOKEN


def fetch_all_topics(
    start_page: int = 1,
    limit: int = 12,
    max_pages: Optional[int] = None,
    auth_token: Optional[str] = None,
    auth_tokens: Optional[Sequence[str]] = None,
    device_fingerprint: Optional[str] = None,
    waf_token: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> List[Any]:
    page = start_page
    total: Optional[int] = None
    pages_fetched = 0
    pages: list = []

    while True:
        payload = fetch_topic_page(
            page=page,
            limit=limit,
            auth_token=auth_token,
            auth_tokens=auth_tokens,
            device_fingerprint=device_fingerprint,
            waf_token=waf_token,
            user_agent=user_agent,
        )
        pages.append(payload)
        batch, _ = _extract_items(payload)
        if total is None:
            total = _extract_total(payload)
        pages_fetched += 1

        if max_pages is not None and pages_fetched >= max_pages:
            break
        if len(batch) < limit:
            break
        if total is not None and (pages_fetched * limit) >= total:
            break

        page += 1

    return pages


def fetch_market_depth(
    question_id: str,
    symbol: str,
    symbol_types: int,
    auth_token: Optional[str] = None,
    auth_tokens: Optional[Sequence[str]] = None,
    device_fingerprint: Optional[str] = None,
    waf_token: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> Dict[str, Any]:
    params = {
        "symbol_types": symbol_types,
        "question_id": question_id,
        "symbol": symbol,
        "chainId": 56,
    }
    headers = _build_headers(
        _choose_auth_token(auth_token, auth_tokens),
        device_fingerprint or DEFAULT_DEVICE_FINGERPRINT,
        waf_token or "",
        user_agent or DEFAULT_USER_AGENT,
    )
    session = _get_session("depth")
    return _request_json(
        "GET",
        DEPTH_API_URL,
        session=session,
        params=params,
        headers=headers,
        timeout=DEFAULT_TIMEOUT,
    )


async def fetch_market_depth_async(
    question_id: str,
    symbol: str,
    symbol_types: int,
    *,
    session: aiohttp.ClientSession,
    auth_token: Optional[str] = None,
    auth_tokens: Optional[Sequence[str]] = None,
    device_fingerprint: Optional[str] = None,
    waf_token: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> Dict[str, Any]:
    params = {
        "symbol_types": symbol_types,
        "question_id": question_id,
        "symbol": symbol,
        "chainId": 56,
    }
    headers = _build_headers(
        _choose_auth_token(auth_token, auth_tokens),
        device_fingerprint or DEFAULT_DEVICE_FINGERPRINT,
        waf_token or "",
        user_agent or DEFAULT_USER_AGENT,
    )
    return await _async_request_json(
        "GET",
        DEPTH_API_URL,
        session=session,
        params=params,
        headers=headers,
    )


def load_cached(path: str) -> Optional[Any]:
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_json(path: str, data: Any) -> None:
    if not path:
        return
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=True, indent=2)
        f.write("\n")


def _iter_markets(items: Iterable[Dict[str, Any]]) -> Iterable[Dict[str, Any]]:
    for item in items:
        if isinstance(item, dict):
            yield item
            for child in item.get("childList") or []:
                if isinstance(child, dict):
                    yield child


def _as_str(val: Any) -> str:
    return str(val).strip() if val is not None else ""


def _as_float(val: Any) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def ffloat(val: Any) -> Optional[float]:
    return _as_float(val)


def _extract_level(entry: Any) -> Tuple[Any, Any]:
    if isinstance(entry, dict):
        return entry.get("price"), entry.get("size")
    if isinstance(entry, (list, tuple)) and entry:
        price = entry[0]
        size = entry[1] if len(entry) > 1 else None
        return price, size
    return None, None


def parse_best_bid_ask(book: Dict[str, Any]) -> Dict[str, Optional[float]]:
    if not isinstance(book, dict):
        return {
            "best_bid": None,
            "best_bid_size": None,
            "best_ask": None,
            "best_ask_size": None,
        }
    # Opinion 可能在 result.data 下
    if "result" in book and isinstance(book["result"], dict):
        inner = book["result"].get("data") or book["result"]
        if isinstance(inner, dict):
            book = inner

    bids = book.get("bids") or []
    asks = book.get("asks") or []

    best_bid = None
    best_bid_size = None
    for x in bids:
        price, size = _extract_level(x)
        p = ffloat(price)
        if p is None:
            continue
        if (best_bid is None) or (p > best_bid):
            best_bid = p
            best_bid_size = ffloat(size)

    best_ask = None
    best_ask_size = None
    for x in asks:
        price, size = _extract_level(x)
        p = ffloat(price)
        if p is None:
            continue
        if (best_ask is None) or (p < best_ask):
            best_ask = p
            best_ask_size = ffloat(size)

    return {
        "best_bid": best_bid,
        "best_bid_size": best_bid_size,
        "best_ask": best_ask,
        "best_ask_size": best_ask_size,
    }


def _ui_market_key(entry: Dict[str, Any]) -> str:
    for key in ("topicId", "questionId", "title"):
        val = entry.get(key)
        if val is not None and str(val).strip():
            return str(val)
    symbol = entry.get("symbol")
    if symbol is not None and str(symbol).strip():
        return str(symbol)
    return "unknown"


def build_depth_ui_results(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    markets: Dict[str, Dict[str, Any]] = {}
    for entry in results:
        if not isinstance(entry, dict):
            continue
        side = entry.get("side")
        if side not in ("yes", "no"):
            continue
        key = _ui_market_key(entry)
        info = markets.setdefault(
            key,
            {
                "title": entry.get("title") or key,
                "topicId": entry.get("topicId"),
                "questionId": entry.get("questionId"),
                "yes": {},
                "no": {},
            },
        )
        info[side] = {
            "best_bid": entry.get("best_bid"),
            "best_bid_size": entry.get("best_bid_size"),
            "best_ask": entry.get("best_ask"),
            "best_ask_size": entry.get("best_ask_size"),
        }
    rows = list(markets.values())
    rows.sort(key=lambda x: str(x.get("title") or ""))
    return rows


def _unwrap_depth_book(book: Dict[str, Any]) -> Dict[str, Any]:
    if "result" in book and isinstance(book["result"], dict):
        inner = book["result"].get("data") or book["result"]
        if isinstance(inner, dict):
            return inner
    return book


def _complement_price(val: Any) -> Optional[float]:
    if val is None:
        return None
    text = str(val).strip()
    if not text:
        return None
    try:
        dec = Decimal(text)
    except InvalidOperation:
        return None
    complement = Decimal("1") - dec
    if "." in text:
        places = len(text.split(".", 1)[1])
        quant = Decimal("1").scaleb(-places)
        complement = complement.quantize(quant)
    return float(complement)


def _mirror_levels(levels: Iterable[Any]) -> List[Any]:
    mirrored: List[Any] = []
    for entry in levels:
        if isinstance(entry, dict):
            price = _complement_price(entry.get("price"))
            if price is None:
                continue
            new_entry = dict(entry)
            new_entry["price"] = price
            mirrored.append(new_entry)
            continue
        if isinstance(entry, (list, tuple)) and entry:
            price = _complement_price(entry[0])
            if price is None:
                continue
            new_entry = list(entry)
            new_entry[0] = price
            mirrored.append(new_entry)
    return mirrored


def _mirror_depth_book(book: Dict[str, Any]) -> Dict[str, Any]:
    mirrored = copy.deepcopy(book)
    inner = _unwrap_depth_book(mirrored)
    if not isinstance(inner, dict):
        return mirrored
    bids = inner.get("bids") or []
    asks = inner.get("asks") or []
    inner["bids"] = _mirror_levels(asks)
    inner["asks"] = _mirror_levels(bids)
    return mirrored


def _parse_epoch_seconds(val: Any) -> Optional[float]:
    raw = _as_float(val)
    if raw is None or raw <= 0:
        return None
    if raw > 1_000_000_000_000:
        return raw / 1000.0
    return raw


def _percentile(sorted_vals: List[float], p: float) -> Optional[float]:
    if not sorted_vals:
        return None
    if p <= 0:
        return sorted_vals[0]
    if p >= 1:
        return sorted_vals[-1]
    idx = int(math.ceil(p * len(sorted_vals))) - 1
    idx = max(0, min(idx, len(sorted_vals) - 1))
    return sorted_vals[idx]


def _summarize_latencies_ms(values: List[float]) -> Dict[str, Any]:
    if not values:
        return {"count": 0}
    vals = sorted(values)
    total = sum(vals)
    n = len(vals)
    buckets = {
        "le_50": 0,
        "le_100": 0,
        "le_200": 0,
        "le_500": 0,
        "le_1000": 0,
        "gt_1000": 0,
    }
    for v in vals:
        if v <= 50:
            buckets["le_50"] += 1
        elif v <= 100:
            buckets["le_100"] += 1
        elif v <= 200:
            buckets["le_200"] += 1
        elif v <= 500:
            buckets["le_500"] += 1
        elif v <= 1000:
            buckets["le_1000"] += 1
        else:
            buckets["gt_1000"] += 1
    return {
        "count": n,
        "min": round(vals[0], 3),
        "max": round(vals[-1], 3),
        "avg": round(total / n, 3),
        "p50": round(_percentile(vals, 0.5) or 0.0, 3),
        "p90": round(_percentile(vals, 0.9) or 0.0, 3),
        "p95": round(_percentile(vals, 0.95) or 0.0, 3),
        "p99": round(_percentile(vals, 0.99) or 0.0, 3),
        "buckets": buckets,
        "slowest": [round(v, 3) for v in vals[-5:][::-1]],
    }


async def fetch_all_depths(
    topics_raw: Any,
    *,
    session: aiohttp.ClientSession,
    auth_token: Optional[str] = None,
    auth_tokens: Optional[Sequence[str]] = None,
    device_fingerprint: Optional[str] = None,
    device_fingerprints: Optional[Sequence[str]] = None,
    waf_token: Optional[str] = None,
    user_agent: Optional[str] = None,
    sleep_s: float = 0.0,
    max_requests: Optional[int] = None,
    max_workers: int = 1,
    cutoff_hours: Optional[float] = 24.0,
    min_volume: Optional[float] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    items = merge_cached_items(topics_raw)
    tasks: List[Dict[str, Any]] = []
    now_s = time.time()
    cutoff_window_s = None
    if cutoff_hours is not None and cutoff_hours > 0:
        cutoff_window_s = cutoff_hours * 3600.0

    skipped_cutoff = 0
    skipped_volume = 0
    for market in _iter_markets(items):
        cutoff_time = _parse_epoch_seconds(market.get("cutoffTime"))
        if cutoff_window_s is not None and cutoff_time is not None:
            if cutoff_time <= now_s + cutoff_window_s:
                skipped_cutoff += 1
                continue
        volume = _as_float(market.get("volume"))
        if min_volume is not None and min_volume > 0:
            if volume is not None and volume < min_volume:
                skipped_volume += 1
                continue
        question_id = _as_str(market.get("questionId"))
        yes_pos = _as_str(market.get("yesPos"))
        no_pos = _as_str(market.get("noPos"))
        topic_id = market.get("topicId")
        title = market.get("title")

        if not question_id:
            continue
        fetch_symbol = None
        fetch_symbol_types = None
        fetch_side = None
        if yes_pos:
            fetch_symbol = yes_pos
            fetch_symbol_types = 0
            fetch_side = "yes"
        elif no_pos:
            fetch_symbol = no_pos
            fetch_symbol_types = 1
            fetch_side = "no"
        if not fetch_symbol:
            continue
        tasks.append(
            {
                "topicId": topic_id,
                "title": title,
                "questionId": question_id,
                "symbol": fetch_symbol,
                "symbol_types": fetch_symbol_types,
                "side": fetch_side,
                "yes_pos": yes_pos,
                "no_pos": no_pos,
            }
        )

    if max_requests is not None:
        if max_requests <= 0:
            tasks = []
        else:
            random.shuffle(tasks)
            tasks = tasks[:max_requests]

    results: List[Dict[str, Any]] = []
    stats = {
        "ok": 0,
        "failed": 0,
        "skipped_cutoff": skipped_cutoff,
        "skipped_volume": skipped_volume,
    }
    latencies_ms: List[float] = []
    latency_samples: List[Dict[str, Any]] = []
    error_samples: List[Dict[str, Any]] = []

    workers = max(1, int(max_workers))

    def _build_result(
        task: Dict[str, Any],
        *,
        side: str,
        symbol: str,
        symbol_types: int,
        response: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
        derived: bool = False,
    ) -> Dict[str, Any]:
        result = {
            "topicId": task["topicId"],
            "title": task["title"],
            "questionId": task["questionId"],
            "symbol": symbol,
            "symbol_types": symbol_types,
            "side": side,
        }
        if derived:
            result["derived"] = True
        if response is not None:
            result["response"] = response
            result.update(parse_best_bid_ask(response))
        if error is not None:
            result["error"] = error
        return result

    async def _worker(worker_idx: int) -> None:
        worker_auth = _pick_worker_auth(auth_token, auth_tokens, worker_idx)
        worker_fp = _pick_worker_value(device_fingerprints, worker_idx) or device_fingerprint
        while True:
            task = await task_queue.get()
            if task is None:
                task_queue.task_done()
                break
            t_start = time.monotonic()
            err: Optional[Exception] = None
            payload: Optional[Dict[str, Any]] = None
            try:
                payload = await fetch_market_depth_async(
                    question_id=task["questionId"],
                    symbol=task["symbol"],
                    symbol_types=task["symbol_types"],
                    session=session,
                    auth_token=worker_auth,
                    auth_tokens=None,
                    device_fingerprint=worker_fp,
                    waf_token=waf_token,
                    user_agent=user_agent,
                )
            except Exception as exc:
                err = exc
            t_end = time.monotonic()
            if sleep_s > 0:
                await asyncio.sleep(sleep_s)

            if err is None and payload is not None:
                primary = _build_result(
                    task,
                    side=task["side"],
                    symbol=task["symbol"],
                    symbol_types=task["symbol_types"],
                    response=payload,
                )
                derived: Optional[Dict[str, Any]] = None
                if task["side"] == "yes" and task.get("no_pos"):
                    mirrored = _mirror_depth_book(payload)
                    derived = _build_result(
                        task,
                        side="no",
                        symbol=task["no_pos"],
                        symbol_types=1,
                        response=mirrored,
                        derived=True,
                    )
                elif task["side"] == "no" and task.get("yes_pos"):
                    mirrored = _mirror_depth_book(payload)
                    derived = _build_result(
                        task,
                        side="yes",
                        symbol=task["yes_pos"],
                        symbol_types=0,
                        response=mirrored,
                        derived=True,
                    )
            else:
                primary = _build_result(
                    task,
                    side=task["side"],
                    symbol=task["symbol"],
                    symbol_types=task["symbol_types"],
                    error=str(err) if err is not None else "unknown error",
                )
                derived = None

            results.append(primary)
            if derived is not None:
                results.append(derived)
            latency_ms = (t_end - t_start) * 1000.0
            latencies_ms.append(latency_ms)
            latency_samples.append(
                {
                    "latency_ms": latency_ms,
                    "topicId": primary.get("topicId"),
                    "symbol": primary.get("symbol"),
                    "side": primary.get("side"),
                }
            )
            if err is None:
                stats["ok"] += 1
            else:
                stats["failed"] += 1
                if len(error_samples) < 50:
                    error_samples.append(
                        {
                            "topicId": primary.get("topicId"),
                            "symbol": primary.get("symbol"),
                            "side": primary.get("side"),
                            "error": primary.get("error") or str(err),
                        }
                    )
            task_queue.task_done()

    if tasks:
        task_queue: asyncio.Queue[Optional[Dict[str, Any]]] = asyncio.Queue()
        for task in tasks:
            task_queue.put_nowait(task)
        for _ in range(workers):
            task_queue.put_nowait(None)

        worker_tasks = [asyncio.create_task(_worker(i)) for i in range(workers)]
        await task_queue.join()
        for wt in worker_tasks:
            await wt

    stats["latency_ms"] = _summarize_latencies_ms(latencies_ms)
    latency_samples.sort(key=lambda x: x["latency_ms"], reverse=True)
    stats["latency_samples"] = latency_samples
    stats["error_samples"] = error_samples

    return [r for r in results if r is not None], stats


def merge_cached_items(raw: Any) -> List[Any]:
    items: List[Any] = []
    found_items = False

    if isinstance(raw, list):
        if raw and all(not isinstance(x, dict) for x in raw):
            return raw
        for page in raw:
            if isinstance(page, list):
                items.extend(page)
                found_items = True
                continue
            if isinstance(page, (dict, list)):
                batch, parent = _extract_items(page)
                if parent or isinstance(page, list):
                    found_items = True
                if batch:
                    items.extend(batch)
        if not items and all(isinstance(x, dict) for x in raw) and not found_items:
            return raw
        return items

    if isinstance(raw, dict):
        batch, _ = _extract_items(raw)
        return batch

    return items


def main() -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(description="Fetch Opinion frontend topic list with a Bearer token.")
    ap.add_argument("--page", type=int, default=1)
    ap.add_argument("--limit", type=int, default=12)
    ap.add_argument("--max-pages", type=int, default=None)
    ap.add_argument("--single-page", action="store_true", help="Only fetch one page.")
    ap.add_argument("--output", default=DEFAULT_OUTPUT)
    ap.add_argument("--merge-cached", action="store_true", help="Merge cached pages into one list.")
    ap.add_argument("--merged-output", default=DEFAULT_MERGED_OUTPUT)
    ap.add_argument("--fetch-depth", action="store_true", help="Fetch market depth for all topics.")
    ap.add_argument("--topics-input", default=DEFAULT_MERGED_OUTPUT)
    ap.add_argument("--depth-output", default=DEFAULT_DEPTH_OUTPUT)
    ap.add_argument("--depth-ui-output", default=DEFAULT_DEPTH_UI_OUTPUT)
    ap.add_argument("--depth-sleep", type=float, default=0.0)
    ap.add_argument("--depth-max-requests", type=int, default=None)
    ap.add_argument("--depth-workers", type=int, default=8)
    ap.add_argument(
        "--depth-print",
        action="store_true",
        help="Print depth payload to stdout each round.",
    )
    ap.add_argument(
        "--depth-latency-samples",
        type=int,
        default=0,
        help="Print slowest N depth requests with topicId/symbol/side.",
    )
    ap.add_argument(
        "--depth-interval",
        type=float,
        default=0.0,
        help="Seconds to sleep between depth rounds.",
    )
    ap.add_argument(
        "--depth-cutoff-hours",
        type=float,
        default=24.0,
        help="Skip topics expiring within this many hours (cutoffTime).",
    )
    ap.add_argument(
        "--depth-min-volume",
        type=float,
        default=0.0,
        help="Skip topics with volume below this threshold.",
    )
    ap.add_argument("--refresh", action="store_true", help="Ignore cached file and re-fetch.")
    ap.add_argument("--auth", default=os.getenv("OPINION_FRONTEND_AUTH", "").strip() or None)
    ap.add_argument(
        "--device-fingerprint",
        default=(
            os.getenv("OPINION_DEVICE_FINGERPRINTS", "").strip()
            or os.getenv("OPINION_DEVICE_FINGERPRINT", "").strip()
            or None
        ),
    )
    ap.add_argument("--waf-token", default=os.getenv("OPINION_WAF_TOKEN", "").strip() or None)
    ap.add_argument("--user-agent", default=os.getenv("OPINION_USER_AGENT", "").strip() or None)
    args = ap.parse_args()

    auth_tokens = _normalize_auth_tokens(args.auth)
    device_fingerprints = _split_csv_values(args.device_fingerprint)
    device_fingerprint = device_fingerprints[0] if device_fingerprints else None

    if args.merge_cached:
        cached = load_cached(args.output)
        if cached is None:
            print(json.dumps({"error": "cached file not found"}, ensure_ascii=True, indent=2))
            return 1
        merged = merge_cached_items(cached)
        save_json(args.merged_output, merged)
        print(json.dumps({"count": len(merged), "output": args.merged_output}, ensure_ascii=True, indent=2))
        return 0

    if args.fetch_depth:
        topics_raw = load_cached(args.topics_input)
        if topics_raw is None:
            print(json.dumps({"error": "topics file not found"}, ensure_ascii=True, indent=2))
            return 1
        async def _run_depth_loop() -> None:
            round_idx = 0
            workers = max(1, int(args.depth_workers))
            async with _build_async_session(workers) as session:
                while True:
                    round_idx += 1
                    t0 = time.monotonic()
                    results, stats = await fetch_all_depths(
                        topics_raw,
                        session=session,
                        auth_tokens=auth_tokens,
                        device_fingerprint=device_fingerprint,
                        device_fingerprints=device_fingerprints,
                        waf_token=args.waf_token,
                        user_agent=args.user_agent,
                        sleep_s=args.depth_sleep,
                        max_requests=args.depth_max_requests,
                        max_workers=workers,
                        cutoff_hours=args.depth_cutoff_hours,
                        min_volume=args.depth_min_volume if args.depth_min_volume > 0 else None,
                    )
                    elapsed = time.monotonic() - t0
                    total = stats["ok"] + stats["failed"]
                    success_rate = round((stats["ok"] / total) if total else 0.0, 4)
                    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
                    payload = {
                        "timestamp": ts,
                        "round": round_idx,
                        "count": len(results),
                        "ok": stats["ok"],
                        "failed": stats["failed"],
                        "success_rate": success_rate,
                        "skipped_cutoff": stats["skipped_cutoff"],
                        "skipped_volume": stats["skipped_volume"],
                        "elapsed_seconds": round(elapsed, 3),
                        "latency_ms": stats.get("latency_ms"),
                        "output": args.depth_output,
                    }
                    if args.depth_latency_samples and args.depth_latency_samples > 0:
                        samples = stats.get("latency_samples") or []
                        payload["latency_samples"] = samples[: int(args.depth_latency_samples)]
                    payload["results"] = results
                    save_json(args.depth_output, payload)
                    ui_rows = build_depth_ui_results(results)
                    ui_payload = {
                        "timestamp": ts,
                        "round": round_idx,
                        "count": len(ui_rows),
                        "ok": stats["ok"],
                        "failed": stats["failed"],
                        "success_rate": success_rate,
                        "skipped_cutoff": stats["skipped_cutoff"],
                        "skipped_volume": stats["skipped_volume"],
                        "elapsed_seconds": round(elapsed, 3),
                        "latency_ms": stats.get("latency_ms"),
                        "results": ui_rows,
                    }
                    error_samples = stats.get("error_samples") or []
                    if error_samples:
                        ui_payload["error_samples"] = error_samples[:10]
                    if args.depth_ui_output:
                        save_json(args.depth_ui_output, ui_payload)
                    if args.depth_print:
                        print(json.dumps(payload, ensure_ascii=True, indent=2))
                    if args.depth_interval and args.depth_interval > 0:
                        await asyncio.sleep(args.depth_interval)

        try:
            asyncio.run(_run_depth_loop())
        except KeyboardInterrupt:
            print(json.dumps({"stopped": True}, ensure_ascii=True, indent=2))
        return 0

    if not args.refresh:
        cached = load_cached(args.output)
        if cached is not None:
            merged = merge_cached_items(cached)
            if merged is not cached:
                save_json(args.output, merged)
            print(json.dumps(merged, ensure_ascii=True, indent=2))
            return 0

    if args.single_page:
        data: Any = fetch_topic_page(
            page=args.page,
            limit=args.limit,
            auth_tokens=auth_tokens,
            device_fingerprint=device_fingerprint,
            waf_token=args.waf_token,
            user_agent=args.user_agent,
        )
    else:
        data = fetch_all_topics(
            start_page=args.page,
            limit=args.limit,
            max_pages=args.max_pages,
            auth_tokens=auth_tokens,
            device_fingerprint=device_fingerprint,
            waf_token=args.waf_token,
            user_agent=args.user_agent,
        )
    merged = merge_cached_items(data)
    save_json(args.output, merged)
    print(json.dumps(merged, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
