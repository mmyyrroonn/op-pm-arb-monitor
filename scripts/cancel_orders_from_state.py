#!/usr/bin/env python3
import argparse
import json
import os
import sys
from typing import Any, Dict, List, Set, Tuple

from dotenv import load_dotenv

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from mm.config import load_config, resolve_secret
from mm.orders import OpinionOrderExecutor, normalize_order
from mm.state import load_state, save_state


def _build_executor(config: Dict[str, Any]) -> OpinionOrderExecutor:
    op_cfg = config.get("opinion", {})
    api_key = resolve_secret(op_cfg, "api_key", op_cfg.get("api_key_env", ""))
    rpc_url = resolve_secret(op_cfg, "rpc_url", op_cfg.get("rpc_url_env", ""))
    private_key = resolve_secret(op_cfg, "private_key", op_cfg.get("private_key_env", ""))
    multi_sig_addr = resolve_secret(op_cfg, "multi_sig_addr", op_cfg.get("multi_sig_addr_env", ""))
    return OpinionOrderExecutor(
        host=op_cfg.get("host", ""),
        api_key=api_key,
        chain_id=int(op_cfg.get("chain_id", 56)),
        rpc_url=rpc_url,
        private_key=private_key,
        multi_sig_addr=multi_sig_addr,
    )


def _collect_order_ids(state: Dict[str, Any]) -> List[str]:
    orders = state.get("orders", {}) if isinstance(state, dict) else {}
    if not isinstance(orders, dict):
        return []
    seen = set()
    ordered: List[str] = []
    for key in sorted(orders.keys()):
        info = orders.get(key) or {}
        order_id = info.get("order_id")
        if order_id is None:
            continue
        order_id = str(order_id).strip()
        if not order_id or order_id in seen:
            continue
        seen.add(order_id)
        ordered.append(order_id)
    return ordered


def _collect_market_ids_from_state(state: Dict[str, Any]) -> Set[int]:
    orders = state.get("orders", {}) if isinstance(state, dict) else {}
    if not isinstance(orders, dict):
        return set()
    market_ids: Set[int] = set()
    for key in orders.keys():
        parts = str(key).split(":")
        if not parts:
            continue
        try:
            market_ids.add(int(parts[0]))
        except (TypeError, ValueError):
            continue
    return market_ids


def _load_selected_markets(path: str) -> List[Dict[str, Any]]:
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def _collect_market_ids_from_selected(path: str) -> Set[int]:
    market_ids: Set[int] = set()
    for item in _load_selected_markets(path):
        topic_id = item.get("topicId")
        if topic_id is None:
            continue
        try:
            market_ids.add(int(topic_id))
        except (TypeError, ValueError):
            continue
    return market_ids


def _collect_order_ids_from_open_orders(
    executor: OpinionOrderExecutor,
    status: str,
    limit: int,
    max_pages: int,
) -> Tuple[List[str], List[Tuple[int, str]]]:
    order_ids: List[str] = []
    failures: List[Tuple[int, str]] = []
    seen: Set[str] = set()
    try:
        orders = executor.fetch_open_orders(
            market_id=0,
            status=status,
            limit=limit,
            max_pages=max_pages,
        )
    except Exception as exc:
        failures.append((0, str(exc)))
        return order_ids, failures
    for order in orders:
        info = normalize_order(order)
        order_id = info.get("order_id")
        if order_id is None:
            continue
        order_id = str(order_id).strip()
        if not order_id or order_id in seen:
            continue
        seen.add(order_id)
        order_ids.append(order_id)
    return order_ids, failures


def _cancel_all(executor: OpinionOrderExecutor, order_ids: List[str]) -> Tuple[int, List[Tuple[str, str]]]:
    failures: List[Tuple[str, str]] = []
    for order_id in order_ids:
        try:
            executor.cancel_order(order_id)
            print(f"cancel ok order_id={order_id}")
        except Exception as exc:
            failures.append((order_id, str(exc)))
            print(f"cancel failed order_id={order_id} err={exc}")
    return len(order_ids), failures


def main() -> None:
    load_dotenv()
    ap = argparse.ArgumentParser(description="Cancel all orders from mm_state.json.")
    ap.add_argument("--config", default="mm_config.json", help="Config JSON path.")
    ap.add_argument("--state", default="mm_state.json", help="State JSON path.")
    ap.add_argument("--clear-state", action="store_true", help="Clear state file if all cancels succeed.")
    ap.add_argument("--dry-run", action="store_true", help="Print order IDs without cancelling.")
    ap.add_argument("--no-open-orders", action="store_true", help="Skip fetching open orders from API.")
    ap.add_argument("--status", default="", help="Open order status for API fetch (default from config).")
    ap.add_argument("--limit", type=int, default=0, help="Open order page size (default from config).")
    ap.add_argument("--max-pages", type=int, default=0, help="Open order max pages (default from config).")
    args = ap.parse_args()

    config = load_config(args.config)
    state = load_state(args.state)
    order_ids = _collect_order_ids(state)
    sources: Dict[str, Set[str]] = {order_id: {"state"} for order_id in order_ids}

    order_sync_cfg = config.get("order_sync", {}) if isinstance(config, dict) else {}
    status = args.status or str(order_sync_cfg.get("status", "1"))
    limit = int(args.limit or order_sync_cfg.get("limit", 20))
    max_pages = int(args.max_pages or order_sync_cfg.get("max_pages", 5))

    open_orders_failures: List[Tuple[int, str]] = []
    if not args.no_open_orders:
        executor = _build_executor(config)
        open_order_ids, open_orders_failures = _collect_order_ids_from_open_orders(
            executor,
            status,
            limit,
            max_pages,
        )
        for order_id in open_order_ids:
            sources.setdefault(order_id, set()).add("open_orders")

    if not sources:
        print(f"no orders found in {args.state} and open orders fetch")
        return

    if args.dry_run:
        for order_id in sorted(sources.keys()):
            src = ",".join(sorted(sources.get(order_id, set())))
            print(f"cancel planned order_id={order_id} source={src}")
        if open_orders_failures:
            for market_id, err in open_orders_failures:
                print(f"open orders fetch failed market_id={market_id} err={err}")
        return

    executor = _build_executor(config)
    cancel_list = sorted(sources.keys())
    total, failures = _cancel_all(executor, cancel_list)

    if failures or open_orders_failures:
        print(f"cancel summary total={total} failed={len(failures)}")
        for order_id, err in failures:
            print(f"cancel error order_id={order_id} err={err}")
        for market_id, err in open_orders_failures:
            print(f"open orders fetch failed market_id={market_id} err={err}")
        return

    print(f"cancel summary total={total} failed=0")
    if args.clear_state:
        state["orders"] = {}
        save_state(args.state, state)
        print(f"state cleared path={args.state}")


if __name__ == "__main__":
    main()
