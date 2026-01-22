import json
from typing import Any, Dict, List, Optional, Union

from opinion_clob_sdk import Client, CHAIN_ID_BNB_MAINNET
from opinion_clob_sdk.chain.py_order_utils.model.order import PlaceOrderDataInput
from opinion_clob_sdk.chain.py_order_utils.model.order_type import LIMIT_ORDER
from opinion_clob_sdk.chain.py_order_utils.model.sides import BUY, SELL, OrderSide


class OpinionOrderExecutor:
    def __init__(
        self,
        *,
        host: str,
        api_key: str,
        chain_id: int,
        rpc_url: str,
        private_key: str,
        multi_sig_addr: str,
    ) -> None:
        chain_id = chain_id or CHAIN_ID_BNB_MAINNET
        self.client = Client(
            host=host,
            apikey=api_key,
            chain_id=chain_id,
            rpc_url=rpc_url,
            private_key=private_key,
            multi_sig_addr=multi_sig_addr,
        )

    def place_limit_order(
        self,
        *,
        market_id: int,
        token_id: str,
        side: Union[OrderSide, str],
        price: str,
        size: float,
    ) -> Optional[str]:
        if isinstance(side, str):
            side = BUY if side.lower() == "buy" else SELL
        payload = PlaceOrderDataInput(
            marketId=int(market_id),
            tokenId=str(token_id),
            makerAmountInBaseToken=str(size),
            price=str(price),
            side=side,
            orderType=LIMIT_ORDER,
        )
        result = self.client.place_order(payload, check_approval=False)
        order_id = _extract_order_id(result)
        if order_id is None:
            raise RuntimeError(f"place_order missing order_id result={_summarize_result(result)}")
        return order_id

    def cancel_order(self, order_id: str) -> Any:
        return self.client.cancel_order(order_id)

    def cancel_all_orders(self) -> Dict[str, Any]:
        return self.client.cancel_all_orders()

    def fetch_open_orders(
        self,
        *,
        market_id: int,
        status: str = "1",
        limit: int = 20,
        max_pages: int = 5,
    ) -> List[Any]:
        all_orders: List[Any] = []
        page = 1
        max_pages = max(1, int(max_pages))
        limit = max(1, int(limit))
        while page <= max_pages:
            result = self.client.get_my_orders(
                market_id=market_id,
                status=status,
                limit=limit,
                page=page,
            )
            orders = self.client._parse_list_response(result, f"get open orders page {page}")
            if not orders:
                break
            all_orders.extend(list(orders))
            if len(orders) < limit:
                break
            page += 1
        return all_orders


def _extract_order_id(result: Any) -> Optional[str]:
    if result is None:
        return None
    order_id = None
    order_id = _extract_order_id_from_obj(result)
    if order_id is None and hasattr(result, "result"):
        order_id = _extract_order_id_from_obj(getattr(result, "result"))

    if order_id is None and isinstance(result, dict):
        order_data = result.get("orderData") or result.get("order_data")
        if isinstance(order_data, dict):
            order_id = order_data.get("orderId") or order_data.get("order_id")
        if order_id is None:
            order_id = result.get("orderId") or result.get("order_id")
        if order_id is None and isinstance(result.get("result"), dict):
            inner = result.get("result") or {}
            order_data = inner.get("orderData") or inner.get("order_data")
            if isinstance(order_data, dict):
                order_id = order_data.get("orderId") or order_data.get("order_id")
            if order_id is None:
                order_id = inner.get("orderId") or inner.get("order_id")

    if order_id is None:
        return None
    return str(order_id)


def _extract_order_id_from_obj(obj: Any) -> Optional[str]:
    if obj is None:
        return None
    if hasattr(obj, "order_data"):
        order_data = getattr(obj, "order_data")
        if hasattr(order_data, "order_id"):
            return getattr(order_data, "order_id")
    if hasattr(obj, "orderData"):
        order_data = getattr(obj, "orderData")
        if hasattr(order_data, "orderId"):
            return getattr(order_data, "orderId")
    if hasattr(obj, "order_id"):
        return getattr(obj, "order_id")
    if hasattr(obj, "orderId"):
        return getattr(obj, "orderId")
    return None


def _summarize_result(result: Any, limit: int = 800) -> str:
    try:
        if isinstance(result, dict):
            text = json.dumps(result, ensure_ascii=True, default=str)
        else:
            text = repr(result)
    except Exception:
        text = repr(result)
    if len(text) > limit:
        return text[:limit] + "...(truncated)"
    return text


def _extract_field(obj: Any, keys: List[str]) -> Any:
    if isinstance(obj, dict):
        for key in keys:
            if key in obj:
                return obj.get(key)
        return None
    for key in keys:
        if hasattr(obj, key):
            return getattr(obj, key)
    return None


def _to_float(val: Any) -> Optional[float]:
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _normalize_side(val: Any) -> Optional[str]:
    if val is None:
        return None
    if isinstance(val, str):
        text = val.strip().lower()
        if text in ("buy", "bid", "b"):
            return "buy"
        if text in ("sell", "ask", "s"):
            return "sell"
        if text.isdigit():
            return _normalize_side(int(text))
        return None
    try:
        num = int(val)
    except (TypeError, ValueError):
        return None
    if num == 0:
        return "buy"
    if num == 1:
        return "sell"
    if num == 2:
        return "sell"
    return None


def normalize_order(order: Any) -> Dict[str, Any]:
    side = _normalize_side(_extract_field(order, ["side", "orderSide", "order_side"]))
    price = _to_float(_extract_field(order, ["price", "orderPrice", "order_price"]))
    size = _to_float(
        _extract_field(
            order,
            [
                "makerAmountInBaseToken",
                "maker_amount_in_base_token",
                "makerAmount",
                "maker_amount",
                "amount",
                "size",
            ],
        )
    )
    token_id = _extract_field(order, ["tokenId", "token_id", "tokenID"])
    outcome = _extract_field(order, ["outcome", "outcomeSide", "outcome_side"])
    return {
        "order_id": _extract_order_id(order),
        "side": side,
        "price": price,
        "size": size,
        "token_id": token_id,
        "outcome": outcome,
    }


def side_from_string(val: str) -> OrderSide:
    if val.lower() == "sell":
        return SELL
    return BUY
