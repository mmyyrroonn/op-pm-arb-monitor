import json
import time
from typing import Any, Dict, List, Optional, Tuple

from .config import resolve_secret
from .market_data import OpinionFrontendClient, OpinionOpenApiClient
from .market_selector import select_and_write
from .orders import OpinionOrderExecutor
from .quote import best_bid_ask, format_price, price_at_level, price_diff_bps
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


class MarketMaker:
    def __init__(self, config: Dict[str, Any]) -> None:
        self.config = config
        self.state_path = (config.get("state") or {}).get("file", "mm_state.json")
        self.state = load_state(self.state_path)

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
            self.data_client = OpinionFrontendClient(auth, device_fp, waf)
        else:
            min_interval = float(op_cfg.get("http_min_interval", 0.2))
            timeout = float(op_cfg.get("http_timeout_seconds", 15))
            self.data_client = OpinionOpenApiClient(
                op_cfg.get("host", ""),
                api_key,
                min_interval,
                timeout,
            )

        self.markets: List[Dict[str, Any]] = []
        self._load_or_select_markets()

        switch_cfg = config.get("market_switch", {})
        self.switch_enabled = bool(switch_cfg.get("enabled", False))
        self.switch_interval = int(switch_cfg.get("interval_seconds", 3600))
        self.switch_cancel_all = bool(switch_cfg.get("cancel_all_on_switch", True))
        self.switch_run_selector = bool(switch_cfg.get("run_selector_on_switch", True))
        self.next_switch_ts = time.time() + self.switch_interval if self.switch_enabled else 0

    def _load_or_select_markets(self) -> None:
        selector_cfg = self.config.get("market_selector", {})
        output_file = selector_cfg.get("output_file", "selected_markets.json")
        try:
            self.markets = _load_json(output_file)
        except FileNotFoundError:
            self.markets = select_and_write(self.config)

    def _maybe_switch_markets(self) -> None:
        if not self.switch_enabled:
            return
        now = time.time()
        if now < self.next_switch_ts:
            return

        if self.switch_run_selector:
            select_and_write(self.config)

        selector_cfg = self.config.get("market_selector", {})
        output_file = selector_cfg.get("output_file", "selected_markets.json")
        new_markets = _load_json(output_file)

        old_ids = {str(m.get("topicId")) for m in self.markets}
        new_ids = {str(m.get("topicId")) for m in new_markets}
        if old_ids != new_ids and self.switch_cancel_all:
            try:
                self.executor.cancel_all_orders()
            except Exception as exc:
                print(f"[WARN] cancel_all_orders failed: {exc}")
            self.state["orders"] = {}

        self.markets = new_markets
        self.next_switch_ts = now + self.switch_interval

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
                try:
                    self.executor.cancel_order(existing["order_id"])
                except Exception as exc:
                    print(f"[WARN] cancel failed {existing['order_id']}: {exc}")
                self.state["orders"].pop(key, None)
                return
            if price_diff_bps(existing_price, desired_price) < replace_bps:
                return
            try:
                self.executor.cancel_order(existing["order_id"])
            except Exception as exc:
                print(f"[WARN] cancel failed {existing['order_id']}: {exc}")
            self.state["orders"].pop(key, None)

        try:
            order_id = self.executor.place_limit_order(
                market_id=market_id,
                token_id=token_id,
                side="BUY" if side == "buy" else "SELL",
                price=format_price(desired_price),
                size=size,
            )
        except Exception as exc:
            print(f"[WARN] place failed {market_id} {token_id} {side}: {exc}")
            order_id = None
        if order_id:
            self.state["orders"][key] = {
                "order_id": order_id,
                "price": desired_price,
                "size": size,
            }

    def _select_token(self, market: Dict[str, Any]) -> Tuple[str, str, int]:
        quote_cfg = self.config.get("quote", {})
        token_side = (quote_cfg.get("token_side") or "yes").lower()
        if token_side == "no":
            return str(market.get("no_token_id")), "no", 1
        return str(market.get("yes_token_id")), "yes", 0

    def run_once(self) -> None:
        self._maybe_switch_markets()
        quote_cfg = self.config.get("quote", {})
        risk_cfg = self.config.get("risk", {})

        level = int(quote_cfg.get("orderbook_level", 5))
        size = float(quote_cfg.get("size_per_side", 10.0))
        min_size = float(quote_cfg.get("min_size", 1.0))
        replace_bps = float(quote_cfg.get("replace_bps", 5.0))
        cancel_on_proximity = bool(risk_cfg.get("cancel_on_price_proximity", True))
        proximity_bps = float(risk_cfg.get("proximity_bps", 5.0))
        reference = str(risk_cfg.get("reference_price", "mid")).lower()

        if size < min_size:
            return

        for market in self.markets:
            market_id = int(market.get("topicId"))
            token_id, _side_label, symbol_types = self._select_token(market)
            if not token_id:
                continue

            try:
                if isinstance(self.data_client, OpinionFrontendClient):
                    question_id = market.get("questionId")
                    if not question_id:
                        continue
                    book = self.data_client.fetch_orderbook(
                        question_id=str(question_id),
                        symbol=str(token_id),
                        symbol_types=symbol_types,
                    )
                else:
                    book = self.data_client.fetch_orderbook(str(token_id))
            except Exception as exc:
                print(f"[WARN] orderbook failed {market_id} {token_id}: {exc}")
                continue

            best_bid, best_ask = best_bid_ask(book)
            desired_bid = price_at_level(book, "bid", level) or best_bid
            desired_ask = price_at_level(book, "ask", level) or best_ask

            if desired_bid is None or desired_ask is None:
                continue
            if desired_bid <= 0 or desired_ask <= 0 or desired_bid >= desired_ask:
                continue

            ref_price = _reference_price(reference, best_bid, best_ask)

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

        save_state(self.state_path, self.state)

    def run_forever(self) -> None:
        interval = int((self.config.get("runtime") or {}).get("loop_interval_seconds", 3))
        while True:
            self.run_once()
            time.sleep(max(0.1, interval))
