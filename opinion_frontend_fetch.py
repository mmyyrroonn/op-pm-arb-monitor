#!/usr/bin/env python3
import argparse
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, Optional, Tuple, List, Iterable

import requests

TOPIC_API_URL = "https://proxy.opinion.trade:8443/api/bsc/api/v2/topic"
DEPTH_API_URL = "https://proxy.opinion.trade:8443/api/bsc/api/v2/order/market/depth"

DEFAULT_AUTH_TOKEN = (
    "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1c2VyX2lkIjoxMDIwMjQ0LCJ3YWxsZXRfYWRkcmVzcyI6IjB4NzgxZWU1OWVjNGI2MjNlYTViODE0ZDhjOWMzZGIwMzkwMWFjNjUyMyIsIndhbGxldF91c2VyIjp7IjU2IjoiMHhhNzAwZjI0OTQwZGIwNTM1YzIwMTFjNTQ0Yzk4NDQ3YTQ4N2RhNDIwIn0sImlzcyI6InByZWRpY3Rpb25fbWFya2V0IiwiZXhwIjoxNzY4MzgxMTU2LCJpYXQiOjE3NjgyOTQ3NTZ9.NbuTQUI0g5DYladxag9B9UApmadlKgT4w7q3HTvaZ-s"
)
DEFAULT_DEVICE_FINGERPRINT = "1891d28b29ed0df165ecae3b7094474a"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/143.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT = (6, 20)
DEFAULT_OUTPUT = "opinion_topics_cache.json"
DEFAULT_MERGED_OUTPUT = "opinion_topics_merged.json"
DEFAULT_DEPTH_OUTPUT = "opinion_depth_cache.json"

ITEM_KEYS = ("list", "records", "items", "topics", "rows", "data")
TOTAL_KEYS = (
    "total",
    "totalCount",
    "total_count",
    "count",
    "totalElements",
    "total_elements",
)


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
        auth_token or DEFAULT_AUTH_TOKEN,
        device_fingerprint or DEFAULT_DEVICE_FINGERPRINT,
        waf_token or "",
        user_agent or DEFAULT_USER_AGENT,
    )
    resp = requests.get(TOPIC_API_URL, params=params, headers=headers, timeout=DEFAULT_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


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


def fetch_all_topics(
    start_page: int = 1,
    limit: int = 12,
    max_pages: Optional[int] = None,
    auth_token: Optional[str] = None,
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
        auth_token or DEFAULT_AUTH_TOKEN,
        device_fingerprint or DEFAULT_DEVICE_FINGERPRINT,
        waf_token or "",
        user_agent or DEFAULT_USER_AGENT,
    )
    resp = requests.get(DEPTH_API_URL, params=params, headers=headers, timeout=DEFAULT_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


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


def fetch_all_depths(
    topics_raw: Any,
    auth_token: Optional[str] = None,
    device_fingerprint: Optional[str] = None,
    waf_token: Optional[str] = None,
    user_agent: Optional[str] = None,
    sleep_s: float = 0.0,
    max_requests: Optional[int] = None,
    max_workers: int = 1,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    items = merge_cached_items(topics_raw)
    tasks: List[Dict[str, Any]] = []

    for market in _iter_markets(items):
        question_id = _as_str(market.get("questionId"))
        yes_pos = _as_str(market.get("yesPos"))
        no_pos = _as_str(market.get("noPos"))
        topic_id = market.get("topicId")
        title = market.get("title")

        for symbol_types, symbol, side in (
            (0, yes_pos, "yes"),
            (1, no_pos, "no"),
        ):
            if not question_id or not symbol:
                continue
            tasks.append(
                {
                    "topicId": topic_id,
                    "title": title,
                    "questionId": question_id,
                    "symbol": symbol,
                    "symbol_types": symbol_types,
                    "side": side,
                }
            )

    if max_requests is not None:
        tasks = tasks[:max_requests]

    results: List[Optional[Dict[str, Any]]] = [None] * len(tasks)
    stats = {"ok": 0, "failed": 0}

    def _run(idx: int, task: Dict[str, Any]) -> Dict[str, Any]:
        payload = fetch_market_depth(
            question_id=task["questionId"],
            symbol=task["symbol"],
            symbol_types=task["symbol_types"],
            auth_token=auth_token,
            device_fingerprint=device_fingerprint,
            waf_token=waf_token,
            user_agent=user_agent,
        )
        if sleep_s > 0:
            time.sleep(sleep_s)
        return {
            "topicId": task["topicId"],
            "title": task["title"],
            "questionId": task["questionId"],
            "symbol": task["symbol"],
            "symbol_types": task["symbol_types"],
            "side": task["side"],
            "response": payload,
        }

    workers = max(1, int(max_workers))
    if tasks:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(_run, i, t): i for i, t in enumerate(tasks)}
            for fut in as_completed(futures):
                idx = futures[fut]
                try:
                    results[idx] = fut.result()
                    stats["ok"] += 1
                except Exception as exc:
                    task = tasks[idx]
                    results[idx] = {
                        "topicId": task["topicId"],
                        "title": task["title"],
                        "questionId": task["questionId"],
                        "symbol": task["symbol"],
                        "symbol_types": task["symbol_types"],
                        "side": task["side"],
                        "error": str(exc),
                    }
                    stats["failed"] += 1

    return [r for r in results if r is not None], stats


def merge_cached_items(raw: Any) -> List[Any]:
    items: List[Any] = []

    if isinstance(raw, list):
        if raw and all(not isinstance(x, dict) for x in raw):
            return raw
        for page in raw:
            if isinstance(page, list):
                items.extend(page)
                continue
            if isinstance(page, (dict, list)):
                batch, _ = _extract_items(page)
                if batch:
                    items.extend(batch)
        if not items and all(isinstance(x, dict) for x in raw):
            return raw
        return items

    if isinstance(raw, dict):
        batch, _ = _extract_items(raw)
        return batch

    return items


def main() -> int:
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
    ap.add_argument("--depth-sleep", type=float, default=0.0)
    ap.add_argument("--depth-max-requests", type=int, default=None)
    ap.add_argument("--depth-workers", type=int, default=8)
    ap.add_argument("--refresh", action="store_true", help="Ignore cached file and re-fetch.")
    ap.add_argument("--auth", default=os.getenv("OPINION_FRONTEND_AUTH", "").strip() or None)
    ap.add_argument(
        "--device-fingerprint",
        default=os.getenv("OPINION_DEVICE_FINGERPRINT", "").strip() or None,
    )
    ap.add_argument("--waf-token", default=os.getenv("OPINION_WAF_TOKEN", "").strip() or None)
    ap.add_argument("--user-agent", default=os.getenv("OPINION_USER_AGENT", "").strip() or None)
    args = ap.parse_args()

    auth = args.auth or DEFAULT_AUTH_TOKEN
    if auth and not auth.startswith("Bearer "):
        auth = f"Bearer {auth}"

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
        t0 = time.monotonic()
        results, stats = fetch_all_depths(
            topics_raw,
            auth_token=auth,
            device_fingerprint=args.device_fingerprint,
            waf_token=args.waf_token,
            user_agent=args.user_agent,
            sleep_s=args.depth_sleep,
            max_requests=args.depth_max_requests,
            max_workers=args.depth_workers,
        )
        elapsed = time.monotonic() - t0
        print(
            json.dumps(
                {
                    "count": len(results),
                    "ok": stats["ok"],
                    "failed": stats["failed"],
                    "elapsed_seconds": round(elapsed, 3),
                    "output": args.depth_output,
                },
                ensure_ascii=True,
                indent=2,
            )
        )
        save_json(args.depth_output, results)
        return 0

    if not args.refresh:
        cached = load_cached(args.output)
        if cached is not None:
            print(json.dumps(cached, ensure_ascii=True, indent=2))
            return 0

    if args.single_page:
        data: Any = fetch_topic_page(
            page=args.page,
            limit=args.limit,
            auth_token=auth,
            device_fingerprint=args.device_fingerprint,
            waf_token=args.waf_token,
            user_agent=args.user_agent,
        )
    else:
        data = fetch_all_topics(
            start_page=args.page,
            limit=args.limit,
            max_pages=args.max_pages,
            auth_token=auth,
            device_fingerprint=args.device_fingerprint,
            waf_token=args.waf_token,
            user_agent=args.user_agent,
        )
    save_json(args.output, data)
    print(json.dumps(data, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
