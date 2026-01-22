from typing import Any, Dict, List, Optional, Tuple


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


def _sorted_levels(book: Dict[str, Any], side: str) -> List[Tuple[float, Optional[float]]]:
    levels = book.get("bids") if side == "bid" else book.get("asks")
    if not levels:
        return []
    parsed: List[Tuple[float, Optional[float]]] = []
    for entry in levels:
        price, size = _extract_level(entry)
        if price is None:
            continue
        parsed.append((price, size))
    parsed.sort(key=lambda item: item[0], reverse=(side == "bid"))
    return parsed


def best_bid_ask(book: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    bids = _sorted_levels(book, "bid")
    asks = _sorted_levels(book, "ask")
    best_bid = bids[0][0] if bids else None
    best_ask = asks[0][0] if asks else None
    return best_bid, best_ask


def price_at_level(book: Dict[str, Any], side: str, level: int) -> Optional[float]:
    if level <= 0:
        return None
    levels = _sorted_levels(book, side)
    if not levels:
        return None
    idx = min(level - 1, len(levels) - 1)
    return levels[idx][0]


def depth_at_levels(book: Dict[str, Any], side: str, level: int) -> Optional[float]:
    if level <= 0:
        return None
    levels = _sorted_levels(book, side)
    if not levels:
        return None
    total = 0.0
    saw_size = False
    for idx in range(min(level, len(levels))):
        _, size = levels[idx]
        qty = _to_float(size)
        if qty is None:
            continue
        saw_size = True
        total += qty
    if not saw_size:
        return None
    return total


def notional_depth_at_levels(book: Dict[str, Any], side: str, level: int) -> Optional[float]:
    if level <= 0:
        return None
    levels = _sorted_levels(book, side)
    if not levels:
        return None
    total = 0.0
    saw_size = False
    for idx in range(min(level, len(levels))):
        price, size = levels[idx]
        qty = _to_float(size)
        if qty is None:
            continue
        saw_size = True
        total += price * qty
    if not saw_size:
        return None
    return total


def format_price(price: float) -> str:
    text = f"{price:.6f}".rstrip("0").rstrip(".")
    return text or "0"


def price_diff_bps(a: float, b: float) -> float:
    if b == 0:
        return 0.0
    return abs(a - b) / b * 10000.0
