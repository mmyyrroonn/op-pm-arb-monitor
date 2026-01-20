import json
from typing import Any, Dict, Iterable, List, Optional


def _to_float(val: Any) -> Optional[float]:
    try:
        return float(val)
    except Exception:
        return None


def _complement_price(val: Any) -> Optional[float]:
    price = _to_float(val)
    if price is None:
        return None
    return 1.0 - price


def _extract_price(entry: Dict[str, Any]) -> Optional[float]:
    for key in ("yesMarketPrice", "yesBuyPrice", "yesSellPrice"):
        price = _to_float(entry.get(key))
        if price is not None:
            return price
    for key in ("noMarketPrice", "noBuyPrice", "noSellPrice"):
        price = _complement_price(entry.get(key))
        if price is not None:
            return price
    return None


def _iter_markets(topics: List[Dict[str, Any]]) -> Iterable[Dict[str, Any]]:
    for item in topics:
        child_list = item.get("childList") or []
        if child_list:
            parent_title = item.get("title")
            for child in child_list:
                topic_id = child.get("topicId")
                if topic_id is None:
                    continue
                yield {
                    "topicId": str(topic_id),
                    "title": child.get("title"),
                    "parentTitle": parent_title,
                    "questionId": child.get("questionId"),
                    "yes_token_id": child.get("yesPos"),
                    "no_token_id": child.get("noPos"),
                    "volume": _to_float(child.get("volume")),
                    "price": _extract_price(child),
                }
        else:
            topic_id = item.get("topicId")
            if topic_id is None:
                continue
            yield {
                "topicId": str(topic_id),
                "title": item.get("title"),
                "parentTitle": None,
                "questionId": item.get("questionId"),
                "yes_token_id": item.get("yesPos"),
                "no_token_id": item.get("noPos"),
                "volume": _to_float(item.get("volume")),
                "price": _extract_price(item),
            }


def _load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_json(path: str, data: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=True, indent=2)
        f.write("\n")


def _build_pairs_map(pairs: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for item in pairs:
        mtype = item.get("type")
        if mtype == "binary":
            op = item.get("opinion") or {}
            market_id = op.get("market_id")
            if not market_id:
                continue
            out[str(market_id)] = {
                "market_type": "binary",
                "market_name": item.get("name"),
                "candidate": "YES/NO",
                "yes_token_id": op.get("yes_token_id"),
                "no_token_id": op.get("no_token_id"),
                "polymarket": item.get("polymarket") or {},
            }
        elif mtype == "categorical":
            parent_name = item.get("name")
            for pair in item.get("pairs") or []:
                op = pair.get("opinion") or {}
                market_id = op.get("market_id")
                if not market_id:
                    continue
                out[str(market_id)] = {
                    "market_type": "categorical",
                    "market_name": parent_name,
                    "candidate": pair.get("candidate"),
                    "yes_token_id": op.get("yes_token_id"),
                    "no_token_id": op.get("no_token_id"),
                    "polymarket": pair.get("polymarket") or {},
                }
    return out


def select_markets(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    selector_cfg = config.get("market_selector", {})
    source_file = selector_cfg.get("source_file", "opinion_topics_merged.json")

    price_target = float(selector_cfg.get("price_target", 0.5))
    price_tolerance = float(selector_cfg.get("price_tolerance", 0.05))
    pct_min = float(selector_cfg.get("volume_percentile_min", 0.1))
    pct_max = float(selector_cfg.get("volume_percentile_max", 0.3))
    max_markets = int(selector_cfg.get("max_markets", 12))

    require_pairs = bool(selector_cfg.get("require_market_pairs", False))
    pairs_file = selector_cfg.get("market_pairs_file") or ""
    pairs_map: Dict[str, Dict[str, Any]] = {}
    if pairs_file:
        pairs_map = _build_pairs_map(_load_json(pairs_file))

    topics = _load_json(source_file)
    entries = [e for e in _iter_markets(topics) if e.get("volume") is not None]
    entries.sort(key=lambda e: e["volume"])

    filtered: List[Dict[str, Any]] = []
    for idx, entry in enumerate(entries):
        entry = dict(entry)
        entry["volume_rank"] = idx + 1
        entry["volume_percentile"] = idx / (len(entries) - 1) if len(entries) > 1 else 0.0
        price = entry.get("price")
        if price is None:
            continue
        if not (pct_min <= entry["volume_percentile"] <= pct_max):
            continue
        if abs(price - price_target) > price_tolerance:
            continue
        pair_info = pairs_map.get(entry["topicId"])
        if require_pairs and not pair_info:
            continue
        if pair_info:
            entry["market_type"] = pair_info.get("market_type")
            entry["market_name"] = pair_info.get("market_name")
            entry["candidate"] = pair_info.get("candidate")
            entry["yes_token_id"] = pair_info.get("yes_token_id")
            entry["no_token_id"] = pair_info.get("no_token_id")
            entry["polymarket"] = pair_info.get("polymarket")
        filtered.append(entry)

    filtered.sort(key=lambda e: (abs(e["price"] - price_target), e["volume"]))
    if max_markets > 0:
        filtered = filtered[:max_markets]
    return filtered


def select_and_write(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    selected = select_markets(config)
    selector_cfg = config.get("market_selector", {})
    output_file = selector_cfg.get("output_file", "selected_markets.json")
    _save_json(output_file, selected)
    return selected
