import json
import logging
import math
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from .config import resolve_secret
from .frontend_auth import FrontendAuthRefresher
from .market_data import OpinionFrontendClient, OpinionOpenApiClient
from .market_selector import select_and_write
from .orders import OpinionOrderExecutor, normalize_order
from .quote import best_bid_ask, format_price, notional_depth_at_levels, price_at_level, price_diff_bps
from .raw_dump import RawNetDumper
from .risk import should_cancel_on_proximity
from .state import load_state, save_state


def _load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _order_key(market_id: int, token_id: str, side: str) -> str:
    return f"{market_id}:{token_id}:{side}"


def _reference_price(reference: str, best_bid: Optional[float], best_ask: Optional[float]) -> Optional[float]:
    if reference == "best_bid":
        return best_bid
    if reference == "best_ask":
        return best_ask
    if best_bid is not None and best_ask is not None:
        return (best_bid + best_ask) / 2.0
    return best_bid or best_ask


def _complement_price(price: Optional[float]) -> Optional[float]:
    if price is None:
        return None
    return 1.0 - price


def _mapped_reference_price(side: str, reference_price: Optional[float]) -> Optional[float]:
    if side == "sell":
        return _complement_price(reference_price)
    return reference_price


def _to_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_float(value: Optional[float], decimals: int = 6) -> str:
    if value is None:
        return "None"
    return f"{value:.{decimals}f}"


def _elapsed_ms(start: float) -> float:
    return (time.perf_counter() - start) * 1000.0


TRACE_LEVEL = 5
PRICE_EPSILON = 1e-6


def _ensure_trace_level() -> None:
    if hasattr(logging, "TRACE"):
        return
    logging.TRACE = TRACE_LEVEL  # type: ignore[attr-defined]
    logging.addLevelName(TRACE_LEVEL, "TRACE")

    def trace(self: logging.Logger, msg: str, *args: Any, **kwargs: Any) -> None:
        if self.isEnabledFor(TRACE_LEVEL):
            self._log(TRACE_LEVEL, msg, args, **kwargs)

    logging.Logger.trace = trace  # type: ignore[assignment]


def _resolve_level(value: Any, default: int = logging.INFO) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        name = value.strip().upper()
        if name == "TRACE":
            return TRACE_LEVEL
        return getattr(logging, name, default)
    return default


def _logging_config(config: Dict[str, Any]) -> Dict[str, Any]:
    raw = config.get("logging", {})
    return raw if isinstance(raw, dict) else {}


def _setup_logger(config: Dict[str, Any]) -> logging.Logger:
    _ensure_trace_level()
    log_cfg = _logging_config(config)
    logger = logging.getLogger("mm.engine")
    if getattr(logger, "_configured", False):
        return logger
    level_name = log_cfg.get("level", "INFO")
    console_level_name = log_cfg.get("console_level", level_name)
    file_level_name = log_cfg.get("file_level", "INFO")
    logger_level = min(
        _resolve_level(level_name),
        _resolve_level(console_level_name),
        _resolve_level(file_level_name),
    )
    logger.setLevel(logger_level)
    handler = logging.StreamHandler()
    fmt = log_cfg.get("format", "%(asctime)s [%(levelname)s] %(message)s")
    datefmt = log_cfg.get("datefmt", "%Y-%m-%d %H:%M:%S")
    handler.setFormatter(logging.Formatter(fmt=fmt, datefmt=datefmt))
    handler.setLevel(_resolve_level(console_level_name, logger_level))
    logger.addHandler(handler)
    file_enabled = bool(log_cfg.get("file_enabled", True))
    file_path = str(log_cfg.get("file", "mm_info.log")) if file_enabled else ""
    if file_enabled and file_path:
        dir_name = os.path.dirname(file_path)
        if dir_name:
            os.makedirs(dir_name, exist_ok=True)
        file_handler = logging.FileHandler(file_path, encoding="utf-8")
        file_handler.setLevel(_resolve_level(file_level_name, logging.INFO))
        file_handler.setFormatter(logging.Formatter(fmt=fmt, datefmt=datefmt))
        logger.addHandler(file_handler)
    logger.propagate = False
    logger.disabled = not bool(log_cfg.get("enabled", True))
    setattr(logger, "_configured", True)
    return logger


class MarketMaker:
    def __init__(self, config: Dict[str, Any]) -> None:
        self.config = config
        log_cfg = _logging_config(config)
        self.logger = _setup_logger(config)
        self.log_orderbook = bool(log_cfg.get("log_orderbook", True))
        self.log_state_changes = bool(log_cfg.get("log_state_changes", True))
        self.log_order_params = bool(log_cfg.get("log_order_params", True))
        self.log_decisions = bool(log_cfg.get("log_decisions", True))
        self.loop_count = 0

        raw_cfg = log_cfg.get("raw_net", {})
        if not isinstance(raw_cfg, dict):
            raw_cfg = {}
        raw_enabled = bool(raw_cfg.get("enabled", log_cfg.get("raw_net_enabled", False)))
        raw_dir = str(raw_cfg.get("dir", log_cfg.get("raw_net_dir", "net_raw")))
        raw_max_chars = int(raw_cfg.get("max_chars", log_cfg.get("raw_net_max_chars", 200000)))
        self.raw_dumper = RawNetDumper(enabled=raw_enabled, directory=raw_dir, max_chars=raw_max_chars)
        if raw_enabled:
            self.logger.info("raw net dump enabled dir=%s", raw_dir)

        self.state_path = (config.get("state") or {}).get("file", "mm_state.json")
        self.state = load_state(self.state_path)
        self.logger.info(
            "state loaded path=%s orders=%d",
            self.state_path,
            len(self.state.get("orders", {})),
        )
        self._frontend_auth_refresher: Optional[FrontendAuthRefresher] = None
        self._order_sync_cfg = config.get("order_sync", {})
        self._order_sync_enabled = bool(self._order_sync_cfg.get("enabled", False))
        try:
            max_interval = int(self._order_sync_cfg.get("interval_loops", 1))
        except (TypeError, ValueError):
            max_interval = 1
        try:
            min_interval = int(self._order_sync_cfg.get("min_interval_loops", 1))
        except (TypeError, ValueError):
            min_interval = 1
        try:
            backoff_factor = float(self._order_sync_cfg.get("backoff_factor", 2.0))
        except (TypeError, ValueError):
            backoff_factor = 2.0
        self._order_sync_max_interval = max(1, max_interval)
        self._order_sync_min_interval = max(1, min_interval)
        if self._order_sync_min_interval > self._order_sync_max_interval:
            self._order_sync_min_interval = self._order_sync_max_interval
        self._order_sync_backoff_factor = max(1.0, backoff_factor)
        self._order_sync_interval = self._order_sync_max_interval
        self._order_sync_next_loop = 0
        self._precheck_on_place = bool(self._order_sync_cfg.get("precheck_on_place", True))
        try:
            self._precheck_cache_seconds = float(self._order_sync_cfg.get("precheck_cache_seconds", 2.0))
        except (TypeError, ValueError):
            self._precheck_cache_seconds = 2.0
        self._last_open_orders: Optional[List[Any]] = None
        self._last_open_orders_ts = 0.0
        self._last_open_orders_loop = -1
        self._last_open_orders_market_id: Optional[int] = None

        op_cfg = config.get("opinion", {})
        api_key = resolve_secret(op_cfg, "api_key", op_cfg.get("api_key_env", ""))
        rpc_url = resolve_secret(op_cfg, "rpc_url", op_cfg.get("rpc_url_env", ""))
        private_key = resolve_secret(op_cfg, "private_key", op_cfg.get("private_key_env", ""))
        multi_sig_addr = resolve_secret(op_cfg, "multi_sig_addr", op_cfg.get("multi_sig_addr_env", ""))

        self.executor = OpinionOrderExecutor(
            host=op_cfg.get("host", ""),
            api_key=api_key,
            chain_id=int(op_cfg.get("chain_id", 56)),
            rpc_url=rpc_url,
            private_key=private_key,
            multi_sig_addr=multi_sig_addr,
            raw_dumper=self.raw_dumper,
        )

        data_cfg = config.get("opinion_data", {})
        source = data_cfg.get("source", "openapi")
        if source == "frontend":
            auth = resolve_secret(data_cfg, "frontend_auth", data_cfg.get("frontend_auth_env", ""))
            device_fp = resolve_secret(data_cfg, "frontend_device_fp", data_cfg.get("frontend_device_fp_env", ""))
            waf = resolve_secret(data_cfg, "frontend_waf", data_cfg.get("frontend_waf_env", ""))
            auth_mode = str(data_cfg.get("frontend_auth_mode", "random"))
            self.data_client = OpinionFrontendClient(
                auth,
                device_fp,
                waf,
                auth_mode=auth_mode,
                raw_dumper=self.raw_dumper,
            )
            refresh_cfg = data_cfg.get("frontend_auth_refresh", {})
            if bool(refresh_cfg.get("enabled", False)):
                try:
                    refresh_interval = refresh_cfg.get("interval_seconds")
                    refresh_interval_s = int(refresh_interval) if refresh_interval else None
                    env_path = refresh_cfg.get("env_file", ".env")
                    persist_env = bool(refresh_cfg.get("persist_env", True))
                    refresh_on_start = bool(refresh_cfg.get("refresh_on_start", True))
                    refresh_before = int(refresh_cfg.get("refresh_before_seconds", 300))
                    min_sleep = int(refresh_cfg.get("min_sleep_seconds", 30))
                    timeout_s = int(refresh_cfg.get("timeout_seconds", 20))
                    chain_id = int(op_cfg.get("chain_id", 56))
                    self._frontend_auth_refresher = FrontendAuthRefresher(
                        update_callback=self.data_client.update_auth,
                        chain_id=chain_id,
                        refresh_before=refresh_before,
                        min_sleep=min_sleep,
                        timeout_s=timeout_s,
                        refresh_interval_s=refresh_interval_s,
                        env_path=env_path,
                        persist_env=persist_env,
                        refresh_on_start=refresh_on_start,
                        logger=self.logger,
                    )
                    self._frontend_auth_refresher.start()
                    self.logger.info(
                        "frontend auth refresh enabled interval=%s persist_env=%s env=%s",
                        refresh_interval_s,
                        persist_env,
                        env_path,
                    )
                except Exception as exc:
                    self.logger.warning("frontend auth refresh disabled err=%s", exc)
        else:
            min_interval = float(op_cfg.get("http_min_interval", 0.2))
            timeout = float(op_cfg.get("http_timeout_seconds", 15))
            self.data_client = OpinionOpenApiClient(
                op_cfg.get("host", ""),
                api_key,
                min_interval,
                timeout,
                raw_dumper=self.raw_dumper,
            )
        self.logger.info("data source=%s", source)

        self.markets: List[Dict[str, Any]] = []
        self._load_or_select_markets()

        switch_cfg = config.get("market_switch", {})
        self.switch_enabled = bool(switch_cfg.get("enabled", False))
        self.switch_interval = int(switch_cfg.get("interval_seconds", 3600))
        self.switch_cancel_all = bool(switch_cfg.get("cancel_all_on_switch", True))
        self.switch_run_selector = bool(switch_cfg.get("run_selector_on_switch", True))
        self.next_switch_ts = time.time() + self.switch_interval if self.switch_enabled else 0
        self.logger.info(
            "market switch enabled=%s interval=%ss cancel_all=%s run_selector=%s",
            self.switch_enabled,
            self.switch_interval,
            self.switch_cancel_all,
            self.switch_run_selector,
        )
        if self._order_sync_enabled:
            self.logger.info(
                "order sync enabled min_loops=%d max_loops=%d backoff_factor=%s",
                self._order_sync_min_interval,
                self._order_sync_max_interval,
                self._order_sync_backoff_factor,
            )
        if self._precheck_on_place:
            self.logger.info(
                "order precheck enabled cache_seconds=%s",
                self._precheck_cache_seconds,
            )

    def _load_or_select_markets(self) -> None:
        selector_cfg = self.config.get("market_selector", {})
        output_file = selector_cfg.get("output_file", "selected_markets.json")
        auto_run = bool(selector_cfg.get("auto_run_on_missing", False))
        try:
            self.markets = _load_json(output_file)
            self.logger.info("markets loaded path=%s count=%d", output_file, len(self.markets))
        except FileNotFoundError:
            if auto_run:
                self.markets = select_and_write(self.config)
                self.logger.info("markets generated via selector count=%d", len(self.markets))
                return
            raise RuntimeError(
                f"Missing {output_file}. Run scripts/run_market_selector.py to generate it."
            )

    def _maybe_switch_markets(self) -> None:
        if not self.switch_enabled:
            return
        now = time.time()
        if now < self.next_switch_ts:
            return

        self.logger.info("market switch triggered")
        if self.switch_run_selector:
            select_and_write(self.config)

        selector_cfg = self.config.get("market_selector", {})
        output_file = selector_cfg.get("output_file", "selected_markets.json")
        new_markets = _load_json(output_file)

        old_ids = {str(m.get("topicId")) for m in self.markets}
        new_ids = {str(m.get("topicId")) for m in new_markets}
        if old_ids != new_ids:
            self.logger.info(
                "market switch change old_count=%d new_count=%d",
                len(old_ids),
                len(new_ids),
            )
        if old_ids != new_ids and self.switch_cancel_all:
            t0 = time.perf_counter()
            try:
                self.executor.cancel_all_orders()
                self.logger.info("net cancel_all_orders elapsed_ms=%.1f", _elapsed_ms(t0))
            except Exception as exc:
                self.logger.warning(
                    "net cancel_all_orders failed err=%s elapsed_ms=%.1f",
                    exc,
                    _elapsed_ms(t0),
                )
            self._clear_state_orders("market_switch")

        self.markets = new_markets
        self.next_switch_ts = now + self.switch_interval

    def _clear_state_orders(self, reason: str) -> None:
        if not self.state.get("orders"):
            return
        if self.log_state_changes:
            self.logger.trace(
                "state clear orders=%d reason=%s",
                len(self.state.get("orders", {})),
                reason,
            )
        self.state["orders"] = {}

    def _log_state_add(self, key: str, order: Dict[str, Any]) -> None:
        if not self.log_state_changes:
            return
        price_val = _fmt_float(_to_float(order.get("price")))
        size_val = _fmt_float(_to_float(order.get("size")), 4)
        self.logger.trace(
            "state add key=%s order_id=%s price=%s size=%s",
            key,
            order.get("order_id"),
            price_val,
            size_val,
        )

    def _log_state_remove(self, key: str, order: Dict[str, Any], reason: str) -> None:
        if not self.log_state_changes:
            return
        price_val = _fmt_float(_to_float(order.get("price")))
        size_val = _fmt_float(_to_float(order.get("size")), 4)
        self.logger.trace(
            "state remove key=%s order_id=%s price=%s size=%s reason=%s",
            key,
            order.get("order_id"),
            price_val,
            size_val,
            reason,
        )

    def _log_order_success(
        self,
        *,
        market_id: int,
        token_id: str,
        side: str,
        order_id: str,
        order_token_id: Optional[str] = None,
        order_side: Optional[str] = None,
    ) -> None:
        if order_token_id and order_token_id != token_id:
            self.logger.info(
                "order ok market=%s token=%s side=%s order_token=%s order_side=%s order_id=%s",
                market_id,
                token_id,
                side,
                order_token_id,
                order_side,
                order_id,
            )
            return
        if order_side and order_side != side:
            self.logger.info(
                "order ok market=%s token=%s side=%s order_token=%s order_side=%s order_id=%s",
                market_id,
                token_id,
                side,
                order_token_id or token_id,
                order_side,
                order_id,
            )
            return
        self.logger.info(
            "order ok market=%s token=%s side=%s order_id=%s",
            market_id,
            token_id,
            side,
            order_id,
        )

    def _log_cancel_success(
        self,
        *,
        market_id: int,
        token_id: str,
        side: str,
        order_id: Optional[str],
        reason: str,
    ) -> None:
        self.logger.info(
            "cancel ok market=%s token=%s side=%s order_id=%s reason=%s",
            market_id,
            token_id,
            side,
            order_id,
            reason,
        )

    def _log_risk_cancel(
        self,
        *,
        market_id: int,
        token_id: str,
        side: str,
        order_id: Optional[str],
        order_price: Optional[float],
        reference_price: Optional[float],
        proximity_bps: float,
    ) -> None:
        self.logger.info(
            "risk cancel proximity market=%s token=%s side=%s order_id=%s price=%s ref_price=%s proximity_bps=%s",
            market_id,
            token_id,
            side,
            order_id,
            _fmt_float(order_price),
            _fmt_float(reference_price),
            _fmt_float(proximity_bps, 2),
        )

    def _log_risk_cancel_depth(
        self,
        *,
        market_id: int,
        token_id: str,
        side: str,
        order_id: Optional[str],
        depth_notional: Optional[float],
        min_depth_usd: float,
    ) -> None:
        self.logger.info(
            "risk cancel depth market=%s token=%s side=%s order_id=%s depth=%s min_depth_usd=%s",
            market_id,
            token_id,
            side,
            order_id,
            _fmt_float(depth_notional, 4),
            _fmt_float(min_depth_usd, 2),
        )

    def _log_risk_cancel_level(
        self,
        *,
        market_id: int,
        token_id: str,
        side: str,
        order_id: Optional[str],
        order_price: Optional[float],
        desired_price: Optional[float],
        level: int,
    ) -> None:
        self.logger.info(
            "risk cancel orderbook_level market=%s token=%s side=%s order_id=%s price=%s desired_price=%s level=%s",
            market_id,
            token_id,
            side,
            order_id,
            _fmt_float(order_price),
            _fmt_float(desired_price),
            level,
        )

    def _snapshot_orders(self, market_id: int, token_id: str) -> Dict[str, Tuple[str, Optional[float], Optional[float]]]:
        snapshot: Dict[str, Tuple[str, Optional[float], Optional[float]]] = {}
        for side in ("buy", "sell"):
            key = _order_key(market_id, token_id, side)
            existing = self.state.get("orders", {}).get(key)
            if not existing:
                continue
            snapshot[key] = (
                str(existing.get("order_id")),
                _to_float(existing.get("price")),
                _to_float(existing.get("size")),
            )
        return snapshot

    def _sync_open_orders(
        self,
        *,
        market_id: int,
        token_id: str,
        no_token_id: Optional[str] = None,
        orders: List[Any],
        sync_cfg: Dict[str, Any],
    ) -> Optional[bool]:
        if not sync_cfg.get("enabled", False):
            return None
        prev_snapshot = self._snapshot_orders(market_id, token_id)

        normalized = []
        no_token_id_val = str(no_token_id) if no_token_id else ""
        for order in orders:
            info = normalize_order(order)
            if not info.get("order_id"):
                continue
            if info.get("price") is None:
                if self.log_decisions:
                    self.logger.trace(
                        "sync skip missing price market=%s token=%s order_id=%s",
                        market_id,
                        token_id,
                        info.get("order_id"),
                    )
                continue
            order_market_id = info.get("market_id")
            if order_market_id not in (None, "", 0, "0"):
                if str(order_market_id) != str(market_id):
                    continue
            token_val = info.get("token_id")
            if token_val is not None:
                token_val = str(token_val)
                info["token_id"] = token_val
            if not token_val:
                outcome = info.get("outcome")
                if outcome == "yes":
                    token_val = token_id
                elif outcome == "no":
                    if no_token_id_val:
                        token_val = no_token_id_val
                    else:
                        if info.get("side") == "buy":
                            info["side"] = "sell"
                        elif info.get("side") == "sell":
                            info["side"] = "buy"
                if token_val:
                    info["token_id"] = token_val
            if token_val and no_token_id_val and token_val == no_token_id_val:
                if info.get("side") == "buy":
                    info["side"] = "sell"
                elif info.get("side") == "sell":
                    info["side"] = "buy"
                else:
                    if self.log_decisions:
                        self.logger.trace(
                            "sync skip unknown no_token side market=%s token=%s order_id=%s side=%s",
                            market_id,
                            token_id,
                            info.get("order_id"),
                            info.get("side"),
                        )
                    continue
            elif token_val and token_val != token_id:
                if self.log_decisions:
                    self.logger.trace(
                        "sync skip unknown token market=%s token=%s order_id=%s order_token=%s",
                        market_id,
                        token_id,
                        info.get("order_id"),
                        token_val,
                    )
                continue
            side = info.get("side")
            if side not in ("buy", "sell"):
                if self.log_decisions:
                    self.logger.trace(
                        "sync skip unknown side market=%s token=%s order_id=%s side=%s",
                        market_id,
                        token_id,
                        info.get("order_id"),
                        side,
                    )
                continue
            normalized.append(info)

        existing_by_id: Dict[str, str] = {}
        for side in ("buy", "sell"):
            key = _order_key(market_id, token_id, side)
            existing = self.state.get("orders", {}).get(key)
            if existing and existing.get("order_id"):
                existing_by_id[str(existing["order_id"])] = side

        new_orders: Dict[str, Dict[str, Any]] = {}
        duplicates = 0
        for info in normalized:
            side = info["side"]
            order_id = str(info["order_id"])
            existing_side = existing_by_id.get(order_id)
            if existing_side and existing_side != side:
                if self.log_decisions:
                    self.logger.trace(
                        "sync side mismatch market=%s token=%s order_id=%s state_side=%s cloud_side=%s",
                        market_id,
                        token_id,
                        order_id,
                        existing_side,
                        side,
                    )
            key = _order_key(market_id, token_id, side)
            if key in new_orders:
                duplicates += 1
                continue
            new_orders[key] = info
        if duplicates and self.log_decisions:
            self.logger.trace(
                "sync duplicate orders market=%s token=%s skipped=%d",
                market_id,
                token_id,
                duplicates,
            )

        for side in ("buy", "sell"):
            key = _order_key(market_id, token_id, side)
            existing = self.state.get("orders", {}).get(key)
            if existing and key not in new_orders:
                self.state["orders"].pop(key, None)
                self._log_state_remove(key, existing, "sync_missing")

        for key, info in new_orders.items():
            existing = self.state.get("orders", {}).get(key)
            if existing and existing.get("order_id") == info["order_id"]:
                existing["price"] = info["price"]
                if info.get("size") is not None:
                    existing["size"] = info["size"]
                continue
            self.state["orders"][key] = {
                "order_id": info["order_id"],
                "price": info["price"],
                "size": info.get("size"),
            }
            self._log_state_add(key, self.state["orders"][key])
        curr_snapshot = self._snapshot_orders(market_id, token_id)
        return prev_snapshot != curr_snapshot

    def _cache_open_orders(self, orders: List[Any], market_id: int) -> None:
        self._last_open_orders = orders
        self._last_open_orders_ts = time.time()
        self._last_open_orders_loop = self.loop_count
        self._last_open_orders_market_id = market_id

    def _get_cached_open_orders(self, market_id: int) -> Optional[List[Any]]:
        if not self._last_open_orders:
            return None
        if self._last_open_orders_market_id not in (0, market_id):
            return None
        if self.loop_count == self._last_open_orders_loop:
            return self._last_open_orders
        if self._precheck_cache_seconds <= 0:
            return None
        if (time.time() - self._last_open_orders_ts) <= self._precheck_cache_seconds:
            return self._last_open_orders
        return None

    def _fetch_open_orders(self, sync_cfg: Dict[str, Any]) -> Optional[List[Any]]:
        if not sync_cfg.get("enabled", False):
            return None
        status = str(sync_cfg.get("status", "1"))
        limit = int(sync_cfg.get("limit", 20))
        max_pages = int(sync_cfg.get("max_pages", 3))
        t0 = time.perf_counter()
        try:
            orders = self.executor.fetch_open_orders(
                market_id=0,
                status=status,
                limit=limit,
                max_pages=max_pages,
            )
        except Exception as exc:
            self.logger.warning(
                "net fetch_open_orders failed market=all token=all err=%s elapsed_ms=%.1f",
                exc,
                _elapsed_ms(t0),
            )
            return None
        self.logger.trace(
            "net fetch_open_orders market=all token=all count=%d elapsed_ms=%.1f",
            len(orders),
            _elapsed_ms(t0),
        )
        self._cache_open_orders(orders, market_id=0)
        return orders

    def _fetch_open_orders_for_market(self, market_id: int, sync_cfg: Dict[str, Any]) -> Optional[List[Any]]:
        status = str(sync_cfg.get("status", "1"))
        limit = int(sync_cfg.get("limit", 20))
        max_pages = int(sync_cfg.get("max_pages", 3))
        t0 = time.perf_counter()
        try:
            orders = self.executor.fetch_open_orders(
                market_id=market_id,
                status=status,
                limit=limit,
                max_pages=max_pages,
            )
        except Exception as exc:
            self.logger.warning(
                "net fetch_open_orders failed market=%s token=all err=%s elapsed_ms=%.1f",
                market_id,
                exc,
                _elapsed_ms(t0),
            )
            return None
        self.logger.trace(
            "net fetch_open_orders market=%s token=all count=%d elapsed_ms=%.1f",
            market_id,
            len(orders),
            _elapsed_ms(t0),
        )
        self._cache_open_orders(orders, market_id=market_id)
        return orders

    def _precheck_open_orders(
        self,
        *,
        market_id: int,
        token_id: str,
        no_token_id: Optional[str],
        side: str,
    ) -> bool:
        if not self._precheck_on_place:
            return False
        key = _order_key(market_id, token_id, side)
        if self.state.get("orders", {}).get(key):
            return True
        orders = self._get_cached_open_orders(market_id)
        if orders is None:
            orders = self._fetch_open_orders_for_market(market_id, self._order_sync_cfg)
        if not orders:
            return False
        self._sync_open_orders(
            market_id=market_id,
            token_id=token_id,
            no_token_id=no_token_id,
            orders=orders,
            sync_cfg={"enabled": True},
        )
        existing = self.state.get("orders", {}).get(key)
        if existing and self.log_decisions:
            self.logger.trace(
                "precheck found order market=%s token=%s side=%s order_id=%s",
                market_id,
                token_id,
                side,
                existing.get("order_id"),
            )
        return bool(existing)

    def _order_sync_due(self) -> bool:
        if not self._order_sync_enabled:
            return False
        return self.loop_count >= self._order_sync_next_loop

    def _schedule_order_sync_next_loop(self) -> None:
        if not self._order_sync_enabled:
            return
        self._order_sync_interval = self._order_sync_min_interval
        next_loop = self.loop_count + 1
        if self._order_sync_next_loop <= self.loop_count:
            self._order_sync_next_loop = next_loop
        else:
            self._order_sync_next_loop = min(self._order_sync_next_loop, next_loop)

    def _apply_order_sync_backoff(self, changed: bool) -> None:
        if not self._order_sync_enabled:
            return
        if changed:
            self._order_sync_interval = self._order_sync_min_interval
        else:
            current = max(self._order_sync_min_interval, self._order_sync_interval)
            next_interval = int(math.ceil(current * self._order_sync_backoff_factor))
            self._order_sync_interval = min(self._order_sync_max_interval, max(self._order_sync_min_interval, next_interval))
        self._order_sync_next_loop = self.loop_count + self._order_sync_interval

    def _update_order(
        self,
        *,
        market_id: int,
        token_id: str,
        side: str,
        desired_price: float,
        size: float,
        reference_price: Optional[float],
        replace_bps: float,
        proximity_bps: float,
        cancel_on_proximity: bool,
        order_token_id: Optional[str] = None,
        order_side: Optional[str] = None,
        no_token_id: Optional[str] = None,
    ) -> None:
        key = _order_key(market_id, token_id, side)
        existing = self.state["orders"].get(key)
        if not existing:
            self._precheck_open_orders(
                market_id=market_id,
                token_id=token_id,
                no_token_id=no_token_id,
                side=side,
            )
            existing = self.state["orders"].get(key)
        place_token_id = order_token_id or token_id
        place_side = order_side if order_side in ("buy", "sell") else side

        if existing:
            existing_price = float(existing.get("price", 0))
            if cancel_on_proximity and should_cancel_on_proximity(existing_price, reference_price, proximity_bps):
                if self.log_decisions:
                    self.logger.trace(
                        "cancel proximity market=%s token=%s side=%s order_id=%s existing_price=%s ref_price=%s proximity_bps=%s",
                        market_id,
                        token_id,
                        side,
                        existing.get("order_id"),
                        _fmt_float(existing_price),
                        _fmt_float(reference_price),
                        proximity_bps,
                    )
                self._log_risk_cancel(
                    market_id=market_id,
                    token_id=token_id,
                    side=side,
                    order_id=existing.get("order_id"),
                    order_price=existing_price,
                    reference_price=reference_price,
                    proximity_bps=proximity_bps,
                )
                try:
                    t0 = time.perf_counter()
                    self.executor.cancel_order(existing["order_id"])
                    self.logger.trace(
                        "net cancel_order market=%s token=%s order_id=%s elapsed_ms=%.1f",
                        market_id,
                        token_id,
                        existing.get("order_id"),
                        _elapsed_ms(t0),
                    )
                    self._log_cancel_success(
                        market_id=market_id,
                        token_id=token_id,
                        side=side,
                        order_id=existing.get("order_id"),
                        reason="proximity",
                    )
                except Exception as exc:
                    self.logger.warning(
                        "net cancel_order failed market=%s token=%s order_id=%s err=%s elapsed_ms=%.1f",
                        market_id,
                        token_id,
                        existing.get("order_id"),
                        exc,
                        _elapsed_ms(t0),
                    )
                self.state["orders"].pop(key, None)
                self._log_state_remove(key, existing, "proximity")
                return
            diff_bps = price_diff_bps(existing_price, desired_price)
            if diff_bps < replace_bps:
                if self.log_decisions:
                    self.logger.trace(
                        "skip replace market=%s token=%s side=%s existing_price=%s desired_price=%s diff_bps=%s replace_bps=%s",
                        market_id,
                        token_id,
                        side,
                        _fmt_float(existing_price),
                        _fmt_float(desired_price),
                        _fmt_float(diff_bps, 2),
                        _fmt_float(replace_bps, 2),
                    )
                return
            if self.log_decisions:
                self.logger.trace(
                    "replace order market=%s token=%s side=%s existing_price=%s desired_price=%s diff_bps=%s replace_bps=%s",
                    market_id,
                    token_id,
                    side,
                    _fmt_float(existing_price),
                    _fmt_float(desired_price),
                    _fmt_float(diff_bps, 2),
                    _fmt_float(replace_bps, 2),
                )
            try:
                t0 = time.perf_counter()
                self.executor.cancel_order(existing["order_id"])
                self.logger.trace(
                    "net cancel_order market=%s token=%s order_id=%s elapsed_ms=%.1f",
                    market_id,
                    token_id,
                    existing.get("order_id"),
                    _elapsed_ms(t0),
                )
                self._log_cancel_success(
                    market_id=market_id,
                    token_id=token_id,
                    side=side,
                    order_id=existing.get("order_id"),
                    reason="replace",
                )
            except Exception as exc:
                self.logger.warning(
                    "net cancel_order failed market=%s token=%s order_id=%s err=%s elapsed_ms=%.1f",
                    market_id,
                    token_id,
                    existing.get("order_id"),
                    exc,
                    _elapsed_ms(t0),
                )
            self.state["orders"].pop(key, None)
            self._log_state_remove(key, existing, "replace")

        try:
            if self.log_order_params:
                if place_token_id != token_id or place_side != side:
                    self.logger.trace(
                        "place order market=%s token=%s side=%s order_token=%s order_side=%s price=%s size=%s",
                        market_id,
                        token_id,
                        side,
                        place_token_id,
                        place_side,
                        _fmt_float(desired_price),
                        _fmt_float(size, 4),
                    )
                else:
                    self.logger.trace(
                        "place order market=%s token=%s side=%s price=%s size=%s",
                        market_id,
                        token_id,
                        side,
                        _fmt_float(desired_price),
                        _fmt_float(size, 4),
                    )
            t0 = time.perf_counter()
            order_id = self.executor.place_limit_order(
                market_id=market_id,
                token_id=place_token_id,
                side="BUY" if place_side == "buy" else "SELL",
                price=format_price(desired_price),
                size=size,
            )
            if place_token_id != token_id or place_side != side:
                self.logger.trace(
                    "net place_order market=%s token=%s side=%s order_token=%s order_side=%s order_id=%s elapsed_ms=%.1f",
                    market_id,
                    token_id,
                    side,
                    place_token_id,
                    place_side,
                    order_id,
                    _elapsed_ms(t0),
                )
            else:
                self.logger.trace(
                    "net place_order market=%s token=%s side=%s order_id=%s elapsed_ms=%.1f",
                    market_id,
                    token_id,
                    side,
                    order_id,
                    _elapsed_ms(t0),
                )
        except Exception as exc:
            if place_token_id != token_id or place_side != side:
                self.logger.warning(
                    "place failed market=%s token=%s side=%s order_token=%s order_side=%s err=%s elapsed_ms=%.1f",
                    market_id,
                    token_id,
                    side,
                    place_token_id,
                    place_side,
                    exc,
                    _elapsed_ms(t0),
                )
            else:
                self.logger.warning(
                    "place failed market=%s token=%s side=%s err=%s elapsed_ms=%.1f",
                    market_id,
                    token_id,
                    side,
                    exc,
                    _elapsed_ms(t0),
                )
            order_id = None
        self._schedule_order_sync_next_loop()
        if order_id:
            self.state["orders"][key] = {
                "order_id": order_id,
                "price": desired_price,
                "size": size,
            }
            self._log_state_add(key, self.state["orders"][key])
            self._log_order_success(
                market_id=market_id,
                token_id=token_id,
                side=side,
                order_id=order_id,
                order_token_id=place_token_id,
                order_side=place_side,
            )

    def _cancel_order(self, *, market_id: int, token_id: str, side: str, reason: str = "cancel") -> None:
        key = _order_key(market_id, token_id, side)
        existing = self.state["orders"].get(key)
        if not existing:
            if self.log_decisions:
                self.logger.trace(
                    "cancel skip market=%s token=%s side=%s reason=missing",
                    market_id,
                    token_id,
                    side,
                )
            return
        if self.log_order_params:
            self.logger.trace(
                "cancel order market=%s token=%s side=%s order_id=%s",
                market_id,
                token_id,
                side,
                existing.get("order_id"),
            )
        t0 = time.perf_counter()
        try:
            self.executor.cancel_order(existing["order_id"])
        except Exception as exc:
            self.logger.warning(
                "net cancel_order failed market=%s token=%s order_id=%s err=%s elapsed_ms=%.1f",
                market_id,
                token_id,
                existing.get("order_id"),
                exc,
                _elapsed_ms(t0),
            )
        else:
            self.logger.trace(
                "net cancel_order market=%s token=%s order_id=%s elapsed_ms=%.1f",
                market_id,
                token_id,
                existing.get("order_id"),
                _elapsed_ms(t0),
            )
            self._log_cancel_success(
                market_id=market_id,
                token_id=token_id,
                side=side,
                order_id=existing.get("order_id"),
                reason=reason,
            )
        self.state["orders"].pop(key, None)
        self._log_state_remove(key, existing, reason)

    def _select_token(self, market: Dict[str, Any]) -> Tuple[str, str, int]:
        token_id = market.get("yes_token_id")
        if token_id is None:
            return "", "yes", 0
        return str(token_id), "yes", 0

    def run_once(self) -> None:
        self._maybe_switch_markets()
        quote_cfg = self.config.get("quote", {})
        risk_cfg = self.config.get("risk", {})
        sync_cfg = self._order_sync_cfg
        do_sync = self._order_sync_due()

        level = int(quote_cfg.get("orderbook_level", 5))
        size = float(quote_cfg.get("size_per_side", 10.0))
        min_size = float(quote_cfg.get("min_size", 1.0))
        replace_bps = float(quote_cfg.get("replace_bps", 5.0))
        cancel_on_proximity = bool(risk_cfg.get("cancel_on_price_proximity", True))
        proximity_bps = float(risk_cfg.get("proximity_bps", 5.0))
        reference = str(risk_cfg.get("reference_price", "mid")).lower()
        min_depth_usd = float(risk_cfg.get("min_depth_usd", quote_cfg.get("min_depth_usd", 2000.0)))

        if size < min_size:
            if self.log_decisions:
                self.logger.trace(
                    "skip run size too small size=%s min_size=%s",
                    _fmt_float(size, 4),
                    _fmt_float(min_size, 4),
                )
            return
        self.logger.trace(
            "loop start markets=%d level=%d size=%s min_size=%s replace_bps=%s proximity_bps=%s reference=%s min_depth_usd=%s",
            len(self.markets),
            level,
            _fmt_float(size, 4),
            _fmt_float(min_size, 4),
            _fmt_float(replace_bps, 2),
            _fmt_float(proximity_bps, 2),
            reference,
            _fmt_float(min_depth_usd, 2),
        )

        sync_changed = False
        sync_success = False
        sync_orders: Optional[List[Any]] = None
        if do_sync:
            sync_orders = self._fetch_open_orders(sync_cfg)
            if sync_orders is None:
                do_sync = False
        for idx, market in enumerate(self.markets):
            market_id = int(market.get("topicId"))
            token_id, _side_label, symbol_types = self._select_token(market)
            if not token_id:
                if self.log_decisions:
                    self.logger.trace("skip market=%s reason=missing_token_id", market_id)
                continue
            no_token_raw = market.get("no_token_id")
            no_token_id = str(no_token_raw) if no_token_raw is not None else ""

            if do_sync and sync_orders is not None:
                result = self._sync_open_orders(
                    market_id=market_id,
                    token_id=token_id,
                    no_token_id=market.get("no_token_id"),
                    orders=sync_orders,
                    sync_cfg=sync_cfg,
                )
                if result is not None:
                    sync_success = True
                    if result:
                        sync_changed = True

            t0 = time.perf_counter()
            try:
                if isinstance(self.data_client, OpinionFrontendClient):
                    question_id = market.get("questionId")
                    if not question_id:
                        if self.log_decisions:
                            self.logger.trace("skip market=%s token=%s reason=missing_question_id", market_id, token_id)
                        continue
                    book = self.data_client.fetch_orderbook(
                        question_id=str(question_id),
                        symbol=str(token_id),
                        symbol_types=symbol_types,
                        worker_idx=idx,
                    )
                else:
                    book = self.data_client.fetch_orderbook(str(token_id))
            except Exception as exc:
                self.logger.warning(
                    "orderbook failed market=%s token=%s err=%s elapsed_ms=%.1f",
                    market_id,
                    token_id,
                    exc,
                    _elapsed_ms(t0),
                )
                continue
            source = "frontend" if isinstance(self.data_client, OpinionFrontendClient) else "openapi"
            self.logger.trace(
                "net fetch_orderbook market=%s token=%s source=%s elapsed_ms=%.1f",
                market_id,
                token_id,
                source,
                _elapsed_ms(t0),
            )

            best_bid, best_ask = best_bid_ask(book)
            desired_bid = price_at_level(book, "bid", level) or best_bid
            desired_ask = price_at_level(book, "ask", level) or best_ask

            bid_notional = notional_depth_at_levels(book, "bid", level)
            ask_notional = notional_depth_at_levels(book, "ask", level)

            ref_price = _reference_price(reference, best_bid, best_ask)
            mapped_ref_price = _complement_price(ref_price)
            mapped_ask_price = _complement_price(desired_ask)

            bid_depth_ok = bid_notional is not None and (min_depth_usd <= 0 or bid_notional >= min_depth_usd)
            ask_depth_ok = ask_notional is not None and (min_depth_usd <= 0 or ask_notional >= min_depth_usd)

            if self.log_orderbook:
                self.logger.trace(
                    "book market=%s token=%s best_bid=%s best_ask=%s desired_bid=%s desired_ask=%s bid_notional=%s ask_notional=%s min_depth_usd=%s ref_price=%s",
                    market_id,
                    token_id,
                    _fmt_float(best_bid),
                    _fmt_float(best_ask),
                    _fmt_float(desired_bid),
                    _fmt_float(desired_ask),
                    _fmt_float(bid_notional, 4),
                    _fmt_float(ask_notional, 4),
                    _fmt_float(min_depth_usd, 2),
                    _fmt_float(ref_price),
                )

            existing_orders: Dict[str, Dict[str, Any]] = {}
            for side in ("buy", "sell"):
                key = _order_key(market_id, token_id, side)
                existing = self.state.get("orders", {}).get(key)
                if existing:
                    existing_orders[side] = existing

            for side, existing in existing_orders.items():
                existing_price = float(existing.get("price", 0))
                side_ref_price = _mapped_reference_price(side, ref_price)
                if cancel_on_proximity and should_cancel_on_proximity(existing_price, side_ref_price, proximity_bps):
                    if self.log_decisions:
                        self.logger.trace(
                            "cancel proximity market=%s token=%s side=%s order_id=%s existing_price=%s ref_price=%s proximity_bps=%s",
                            market_id,
                            token_id,
                            side,
                            existing.get("order_id"),
                            _fmt_float(existing_price),
                            _fmt_float(side_ref_price),
                            proximity_bps,
                        )
                    self._log_risk_cancel(
                        market_id=market_id,
                        token_id=token_id,
                        side=side,
                        order_id=existing.get("order_id"),
                        order_price=existing_price,
                        reference_price=side_ref_price,
                        proximity_bps=proximity_bps,
                    )
                    self._cancel_order(market_id=market_id, token_id=token_id, side=side, reason="proximity")
                    continue
                depth_ok = bid_depth_ok if side == "buy" else ask_depth_ok
                depth_val = bid_notional if side == "buy" else ask_notional
                if not depth_ok:
                    if self.log_decisions:
                        self.logger.trace(
                            "cancel depth_threshold market=%s token=%s side=%s order_id=%s depth=%s min_depth_usd=%s",
                            market_id,
                            token_id,
                            side,
                            existing.get("order_id"),
                            _fmt_float(depth_val, 4),
                            _fmt_float(min_depth_usd, 2),
                        )
                    self._log_risk_cancel_depth(
                        market_id=market_id,
                        token_id=token_id,
                        side=side,
                        order_id=existing.get("order_id"),
                        depth_notional=depth_val,
                        min_depth_usd=min_depth_usd,
                    )
                    self._cancel_order(market_id=market_id, token_id=token_id, side=side, reason="depth_threshold")
                    continue
                desired_price = desired_bid if side == "buy" else mapped_ask_price
                if desired_price is not None and abs(existing_price - desired_price) > PRICE_EPSILON:
                    if self.log_decisions:
                        self.logger.trace(
                            "cancel orderbook_level market=%s token=%s side=%s order_id=%s existing_price=%s desired_price=%s level=%s",
                            market_id,
                            token_id,
                            side,
                            existing.get("order_id"),
                            _fmt_float(existing_price),
                            _fmt_float(desired_price),
                            level,
                        )
                    self._log_risk_cancel_level(
                        market_id=market_id,
                        token_id=token_id,
                        side=side,
                        order_id=existing.get("order_id"),
                        order_price=existing_price,
                        desired_price=desired_price,
                        level=level,
                    )
                    self._cancel_order(market_id=market_id, token_id=token_id, side=side, reason="orderbook_level")

            spread_valid = True
            if desired_bid is not None and desired_ask is not None:
                if desired_bid <= 0 or desired_ask <= 0 or desired_bid >= desired_ask:
                    spread_valid = False
                    if self.log_decisions:
                        self.logger.trace(
                            "skip market=%s token=%s reason=invalid_spread desired_bid=%s desired_ask=%s",
                            market_id,
                            token_id,
                            _fmt_float(desired_bid),
                            _fmt_float(desired_ask),
                        )
            if desired_bid is None and desired_ask is None:
                if self.log_decisions:
                    self.logger.trace(
                        "skip market=%s token=%s reason=missing_desired_price best_bid=%s best_ask=%s",
                        market_id,
                        token_id,
                        _fmt_float(best_bid),
                        _fmt_float(best_ask),
                    )

            if desired_bid is not None and desired_bid > 0 and spread_valid and bid_depth_ok:
                if self.log_decisions:
                    self.logger.trace("decision market=%s token=%s action=place_buy", market_id, token_id)
                self._update_order(
                    market_id=market_id,
                    token_id=token_id,
                    side="buy",
                    desired_price=desired_bid,
                    size=size,
                    reference_price=ref_price,
                    replace_bps=replace_bps,
                    proximity_bps=proximity_bps,
                    cancel_on_proximity=cancel_on_proximity,
                    no_token_id=no_token_id or None,
                )
            elif desired_bid is not None and desired_bid > 0 and spread_valid and not bid_depth_ok:
                if self.log_decisions:
                    self.logger.trace(
                        "skip market=%s token=%s reason=depth_threshold side=buy depth=%s min_depth_usd=%s",
                        market_id,
                        token_id,
                        _fmt_float(bid_notional, 4),
                        _fmt_float(min_depth_usd, 2),
                    )

            if desired_ask is not None and desired_ask > 0 and spread_valid and ask_depth_ok:
                mapped_price = mapped_ask_price
                if not no_token_id:
                    if self.log_decisions:
                        self.logger.trace(
                            "skip market=%s token=%s reason=missing_no_token_id",
                            market_id,
                            token_id,
                        )
                elif mapped_price is None or mapped_price <= 0:
                    if self.log_decisions:
                        self.logger.trace(
                            "skip market=%s token=%s reason=invalid_no_price yes_ask=%s no_price=%s",
                            market_id,
                            token_id,
                            _fmt_float(desired_ask),
                            _fmt_float(mapped_price),
                        )
                else:
                    if self.log_decisions:
                        self.logger.trace("decision market=%s token=%s action=place_sell", market_id, token_id)
                        self.logger.trace(
                            "sell mapped to buy_no market=%s token_yes=%s token_no=%s yes_ask=%s no_price=%s",
                            market_id,
                            token_id,
                            no_token_id,
                            _fmt_float(desired_ask),
                            _fmt_float(mapped_price),
                        )
                    self._update_order(
                        market_id=market_id,
                        token_id=token_id,
                        side="sell",
                        desired_price=mapped_price,
                        size=size,
                        reference_price=mapped_ref_price,
                        replace_bps=replace_bps,
                        proximity_bps=proximity_bps,
                        cancel_on_proximity=cancel_on_proximity,
                        order_token_id=no_token_id,
                        order_side="buy",
                        no_token_id=no_token_id or None,
                    )
            elif desired_ask is not None and desired_ask > 0 and spread_valid and not ask_depth_ok:
                if self.log_decisions:
                    self.logger.trace(
                        "skip market=%s token=%s reason=depth_threshold side=sell depth=%s min_depth_usd=%s",
                        market_id,
                        token_id,
                        _fmt_float(ask_notional, 4),
                        _fmt_float(min_depth_usd, 2),
                    )

        if do_sync and sync_success:
            self._apply_order_sync_backoff(sync_changed)

        save_state(self.state_path, self.state)
        if self.log_state_changes:
            self.logger.trace(
                "state saved path=%s orders=%d",
                self.state_path,
                len(self.state.get("orders", {})),
            )
        self.loop_count += 1

    def run_forever(self) -> None:
        interval = int((self.config.get("runtime") or {}).get("loop_interval_seconds", 3))
        while True:
            self.run_once()
            time.sleep(max(0.1, interval))
