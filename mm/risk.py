from typing import Optional


def should_cancel_on_proximity(order_price: float, reference_price: Optional[float], bps: float) -> bool:
    if reference_price is None or reference_price <= 0:
        return False
    threshold = (bps / 10000.0) * reference_price
    return abs(order_price - reference_price) <= threshold
