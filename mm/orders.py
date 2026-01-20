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
        return _extract_order_id(result)

    def cancel_order(self, order_id: str) -> Any:
        return self.client.cancel_order(order_id)

    def cancel_all_orders(self) -> Dict[str, Any]:
        return self.client.cancel_all_orders()


def _extract_order_id(result: Any) -> Optional[str]:
    if result is None:
        return None
    order_id = None

    if hasattr(result, "order_data"):
        order_data = getattr(result, "order_data")
        order_id = getattr(order_data, "order_id", None)
    if order_id is None and hasattr(result, "orderData"):
        order_data = getattr(result, "orderData")
        order_id = getattr(order_data, "orderId", None)

    if order_id is None and isinstance(result, dict):
        order_data = result.get("orderData") or result.get("order_data")
        if isinstance(order_data, dict):
            order_id = order_data.get("orderId") or order_data.get("order_id")
        if order_id is None:
            order_id = result.get("orderId") or result.get("order_id")

    if order_id is None:
        return None
    return str(order_id)


def side_from_string(val: str) -> OrderSide:
    if val.lower() == "sell":
        return SELL
    return BUY
