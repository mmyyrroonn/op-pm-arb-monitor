import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from .config import resolve_secret
from .market_data import OpinionFrontendClient, OpinionOpenApiClient
from .market_selector import select_and_write
from .orders import OpinionOrderExecutor, normalize_order
from .quote import best_bid_ask, depth_at_levels, format_price, price_at_level, price_diff_bps
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


def _logging_config(config: Dict[str, Any]) -> Dict[str, Any]:
    raw = config.get("logging", {})
    return raw if isinstance(raw, dict) else {}


def _setup_logger(config: Dict[str, Any]) -> logging.Logger:
    log_cfg = _logging_config(config)
    logger = logging.getLogger("mm.engine")
    if getattr(logger, "_configured", False):
        return logger
    level_name = str(log_cfg.get("level", "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)
    logger.setLevel(level)
    handler = logging.StreamHandler()
    fmt = log_cfg.get("format", "%(asctime)s [%(levelname)s] %(message)s")
    datefmt = log_cfg.get("datefmt", "%Y-%m-%d %H:%M:%S")
    handler.setFormatter(logging.Formatter(fmt=fmt, datefmt=datefmt))
    logger.addHandler(handler)
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

        self.state_path = (config.get("state") or {}).get("file", "mm_state.json")
        self.state = load_state(self.state_path)
        self.logger.info(
            "state loaded path=%s orders=%d",
            self.state_path,
            len(self.state.get("orders", {})),
        )

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
        )

        data_cfg = config.get("opinion_data", {})
        source = data_cfg.get("source", "openapi")
        if source == "frontend":
            auth = resolve_secret(data_cfg, "frontend_auth", data_cfg.get("frontend_auth_env", ""))
            device_fp = resolve_secret(data_cfg, "frontend_device_fp", data_cfg.get("frontend_device_fp_env", ""))
            waf = resolve_secret(data_cfg, "frontend_waf", data_cfg.get("frontend_waf_env", ""))
            auth_mode = str(data_cfg.get("frontend_auth_mode", "random"))
            self.data_client = OpinionFrontendClient(auth, device_fp, waf, auth_mode=auth_mode)
        else:
            min_interval = float(op_cfg.get("http_min_interval", 0.2))
            timeout = float(op_cfg.get("http_timeout_seconds", 15))
            self.data_client = OpinionOpenApiClient(
                op_cfg.get("host", ""),
                api_key,
                min_interval,
                timeout,
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
            self.logger.info(
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
        self.logger.info(
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
        self.logger.info(
            "state remove key=%s order_id=%s price=%s size=%s reason=%s",
            key,
            order.get("order_id"),
            price_val,
            size_val,
            reason,
        )

    def _sync_open_orders(
        self,
        *,
        market_id: int,
        token_id: str,
        sync_cfg: Dict[str, Any],
    ) -> None:
        if not sync_cfg.get("enabled", False):
            return
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
                "net fetch_open_orders failed market=%s token=%s err=%s elapsed_ms=%.1f",
                market_id,
                token_id,
                exc,
                _elapsed_ms(t0),
            )
            return
        self.logger.info(
            "net fetch_open_orders market=%s token=%s count=%d elapsed_ms=%.1f",
            market_id,
            token_id,
            len(orders),
            _elapsed_ms(t0),
        )

        normalized = []
        for order in orders:
            info = normalize_order(order)
            if not info.get("order_id"):
                continue
            if info.get("price") is None:
                if self.log_decisions:
                    self.logger.info(
                        "sync skip missing price market=%s token=%s order_id=%s",
                        market_id,
                        token_id,
                        info.get("order_id"),
                    )
                continue
            side = info.get("side")
            if side not in ("buy", "sell"):
                if self.log_decisions:
                    self.logger.info(
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
            side = existing_by_id.get(str(info["order_id"]), info["side"])
            if side not in ("buy", "sell"):
                continue
            info["side"] = side
            key = _order_key(market_id, token_id, side)
            if key in new_orders:
                duplicates += 1
                continue
            new_orders[key] = info
        if duplicates and self.log_decisions:
            self.logger.info(
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
    ) -> None:
        key = _order_key(market_id, token_id, side)
        existing = self.state["orders"].get(key)

        if existing:
            existing_price = float(existing.get("price", 0))
            if cancel_on_proximity and should_cancel_on_proximity(existing_price, reference_price, proximity_bps):
                if self.log_decisions:
                    self.logger.info(
                        "cancel proximity market=%s token=%s side=%s order_id=%s existing_price=%s ref_price=%s proximity_bps=%s",
                        market_id,
                        token_id,
                        side,
                        existing.get("order_id"),
                        _fmt_float(existing_price),
                        _fmt_float(reference_price),
                        proximity_bps,
                    )
                try:
                    t0 = time.perf_counter()
                    self.executor.cancel_order(existing["order_id"])
                    self.logger.info(
                        "net cancel_order market=%s token=%s order_id=%s elapsed_ms=%.1f",
                        market_id,
                        token_id,
                        existing.get("order_id"),
                        _elapsed_ms(t0),
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
                    self.logger.info(
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
                self.logger.info(
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
                self.logger.info(
                    "net cancel_order market=%s token=%s order_id=%s elapsed_ms=%.1f",
                    market_id,
                    token_id,
                    existing.get("order_id"),
                    _elapsed_ms(t0),
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
                self.logger.info(
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
                token_id=token_id,
                side="BUY" if side == "buy" else "SELL",
                price=format_price(desired_price),
                size=size,
            )
            self.logger.info(
                "net place_order market=%s token=%s side=%s order_id=%s elapsed_ms=%.1f",
                market_id,
                token_id,
                side,
                order_id,
                _elapsed_ms(t0),
            )
        except Exception as exc:
            self.logger.warning(
                "place failed market=%s token=%s side=%s err=%s elapsed_ms=%.1f",
                market_id,
                token_id,
                side,
                exc,
                _elapsed_ms(t0),
            )
            order_id = None
        if order_id:
            self.state["orders"][key] = {
                "order_id": order_id,
                "price": desired_price,
                "size": size,
            }
            self._log_state_add(key, self.state["orders"][key])
            if self.log_order_params:
                self.logger.info(
                    "place ok market=%s token=%s side=%s order_id=%s",
                    market_id,
                    token_id,
                    side,
                    order_id,
                )

    def _cancel_order(self, *, market_id: int, token_id: str, side: str, reason: str = "cancel") -> None:
        key = _order_key(market_id, token_id, side)
        existing = self.state["orders"].get(key)
        if not existing:
            if self.log_decisions:
                self.logger.info(
                    "cancel skip market=%s token=%s side=%s reason=missing",
                    market_id,
                    token_id,
                    side,
                )
            return
        if self.log_order_params:
            self.logger.info(
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
            self.logger.info(
                "net cancel_order market=%s token=%s order_id=%s elapsed_ms=%.1f",
                market_id,
                token_id,
                existing.get("order_id"),
                _elapsed_ms(t0),
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
        sync_cfg = self.config.get("order_sync", {})

        level = int(quote_cfg.get("orderbook_level", 5))
        size = float(quote_cfg.get("size_per_side", 10.0))
        min_size = float(quote_cfg.get("min_size", 1.0))
        replace_bps = float(quote_cfg.get("replace_bps", 5.0))
        cancel_on_proximity = bool(risk_cfg.get("cancel_on_price_proximity", True))
        proximity_bps = float(risk_cfg.get("proximity_bps", 5.0))
        reference = str(risk_cfg.get("reference_price", "mid")).lower()

        if size < min_size:
            if self.log_decisions:
                self.logger.info(
                    "skip run size too small size=%s min_size=%s",
                    _fmt_float(size, 4),
                    _fmt_float(min_size, 4),
                )
            return
        self.logger.info(
            "loop start markets=%d level=%d size=%s min_size=%s replace_bps=%s proximity_bps=%s reference=%s",
            len(self.markets),
            level,
            _fmt_float(size, 4),
            _fmt_float(min_size, 4),
            _fmt_float(replace_bps, 2),
            _fmt_float(proximity_bps, 2),
            reference,
        )

        for idx, market in enumerate(self.markets):
            market_id = int(market.get("topicId"))
            token_id, _side_label, symbol_types = self._select_token(market)
            if not token_id:
                if self.log_decisions:
                    self.logger.info("skip market=%s reason=missing_token_id", market_id)
                continue

            self._sync_open_orders(market_id=market_id, token_id=token_id, sync_cfg=sync_cfg)

            t0 = time.perf_counter()
            try:
                if isinstance(self.data_client, OpinionFrontendClient):
                    question_id = market.get("questionId")
                    if not question_id:
                        if self.log_decisions:
                            self.logger.info("skip market=%s token=%s reason=missing_question_id", market_id, token_id)
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
            self.logger.info(
                "net fetch_orderbook market=%s token=%s source=%s elapsed_ms=%.1f",
                market_id,
                token_id,
                source,
                _elapsed_ms(t0),
            )

            best_bid, best_ask = best_bid_ask(book)
            desired_bid = price_at_level(book, "bid", level) or best_bid
            desired_ask = price_at_level(book, "ask", level) or best_ask

            bid_depth = depth_at_levels(book, "bid", level)
            ask_depth = depth_at_levels(book, "ask", level)

            ref_price = _reference_price(reference, best_bid, best_ask)
            if self.log_orderbook:
                self.logger.info(
                    "book market=%s token=%s best_bid=%s best_ask=%s desired_bid=%s desired_ask=%s bid_depth=%s ask_depth=%s ref_price=%s",
                    market_id,
                    token_id,
                    _fmt_float(best_bid),
                    _fmt_float(best_ask),
                    _fmt_float(desired_bid),
                    _fmt_float(desired_ask),
                    _fmt_float(bid_depth, 4),
                    _fmt_float(ask_depth, 4),
                    _fmt_float(ref_price),
                )

            target_side = None
            if ask_depth is None and bid_depth is None:
                target_side = None
            elif ask_depth is None:
                target_side = "buy"
            elif bid_depth is None:
                target_side = "sell"
            elif bid_depth >= ask_depth:
                target_side = "buy"
            else:
                target_side = "sell"

            existing_orders: Dict[str, Dict[str, Any]] = {}
            for side in ("buy", "sell"):
                key = _order_key(market_id, token_id, side)
                existing = self.state.get("orders", {}).get(key)
                if existing:
                    existing_orders[side] = existing

            for side, existing in existing_orders.items():
                existing_price = float(existing.get("price", 0))
                if cancel_on_proximity and should_cancel_on_proximity(existing_price, ref_price, proximity_bps):
                    if self.log_decisions:
                        self.logger.info(
                            "cancel proximity market=%s token=%s side=%s order_id=%s existing_price=%s ref_price=%s proximity_bps=%s",
                            market_id,
                            token_id,
                            side,
                            existing.get("order_id"),
                            _fmt_float(existing_price),
                            _fmt_float(ref_price),
                            proximity_bps,
                        )
                    self._cancel_order(market_id=market_id, token_id=token_id, side=side, reason="proximity")
                    continue
                if target_side is not None and side != target_side:
                    if self.log_decisions:
                        self.logger.info(
                            "cancel depth_reversal market=%s token=%s side=%s order_id=%s target_side=%s",
                            market_id,
                            token_id,
                            side,
                            existing.get("order_id"),
                            target_side,
                        )
                    self._cancel_order(market_id=market_id, token_id=token_id, side=side, reason="depth_reversal")

            remaining_sides = []
            for side in ("buy", "sell"):
                key = _order_key(market_id, token_id, side)
                if self.state.get("orders", {}).get(key):
                    remaining_sides.append(side)
            if remaining_sides:
                if self.log_decisions:
                    self.logger.info(
                        "skip market=%s token=%s reason=existing_order sides=%s",
                        market_id,
                        token_id,
                        ",".join(remaining_sides),
                    )
                continue

            if desired_bid is None or desired_ask is None:
                if self.log_decisions:
                    self.logger.info(
                        "skip market=%s token=%s reason=missing_desired_price best_bid=%s best_ask=%s",
                        market_id,
                        token_id,
                        _fmt_float(best_bid),
                        _fmt_float(best_ask),
                    )
                continue
            if desired_bid <= 0 or desired_ask <= 0 or desired_bid >= desired_ask:
                if self.log_decisions:
                    self.logger.info(
                        "skip market=%s token=%s reason=invalid_spread desired_bid=%s desired_ask=%s",
                        market_id,
                        token_id,
                        _fmt_float(desired_bid),
                        _fmt_float(desired_ask),
                    )
                continue
            if bid_depth is None and ask_depth is None:
                if self.log_decisions:
                    self.logger.info("skip market=%s token=%s reason=missing_depth", market_id, token_id)
                continue

            if target_side == "buy":
                if self.log_decisions:
                    self.logger.info("decision market=%s token=%s action=place_buy", market_id, token_id)
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
                )
            elif target_side == "sell":
                if self.log_decisions:
                    self.logger.info("decision market=%s token=%s action=place_sell", market_id, token_id)
                self._update_order(
                    market_id=market_id,
                    token_id=token_id,
                    side="sell",
                    desired_price=desired_ask,
                    size=size,
                    reference_price=ref_price,
                    replace_bps=replace_bps,
                    proximity_bps=proximity_bps,
                    cancel_on_proximity=cancel_on_proximity,
                )
            else:
                if self.log_decisions:
                    self.logger.info("skip market=%s token=%s reason=missing_target_side", market_id, token_id)

        save_state(self.state_path, self.state)
        if self.log_state_changes:
            self.logger.info(
                "state saved path=%s orders=%d",
                self.state_path,
                len(self.state.get("orders", {})),
            )

    def run_forever(self) -> None:
        interval = int((self.config.get("runtime") or {}).get("loop_interval_seconds", 3))
        while True:
            self.run_once()
            time.sleep(max(0.1, interval))
