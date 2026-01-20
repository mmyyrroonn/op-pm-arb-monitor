import json
from typing import Any, Dict, Optional, Union

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


def side_from_string(val: str) -> OrderSide:
    if val.lower() == "sell":
        return SELL
    return BUY
