from typing import Any, Dict, Optional, Tuple


def _extract_level(entry: Any) -> Tuple[Optional[float], Optional[float]]:
    if isinstance(entry, dict):
        return _to_float(entry.get("price")), _to_float(entry.get("size"))
    if isinstance(entry, (list, tuple)) and entry:
        price = _to_float(entry[0])
        size = _to_float(entry[1]) if len(entry) > 1 else None
        return price, size
    return None, None


def _to_float(val: Any) -> Optional[float]:
    try:
        return float(val)
    except Exception:
        return None


def best_bid_ask(book: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    best_bid = None
    for entry in bids:
        price, _ = _extract_level(entry)
        if price is None:
            continue
        if best_bid is None or price > best_bid:
            best_bid = price
    best_ask = None
    for entry in asks:
        price, _ = _extract_level(entry)
        if price is None:
            continue
        if best_ask is None or price < best_ask:
            best_ask = price
    return best_bid, best_ask


def price_at_level(book: Dict[str, Any], side: str, level: int) -> Optional[float]:
    if level <= 0:
        return None
    levels = book.get("bids") if side == "bid" else book.get("asks")
    if not levels:
        return None
    idx = min(level - 1, len(levels) - 1)
    price, _ = _extract_level(levels[idx])
    return price


def format_price(price: float) -> str:
    text = f"{price:.6f}".rstrip("0").rstrip(".")
    return text or "0"


def price_diff_bps(a: float, b: float) -> float:
    if b == 0:
        return 0.0
    return abs(a - b) / b * 10000.0
