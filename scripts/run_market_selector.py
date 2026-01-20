#!/usr/bin/env python3
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from mm.config import load_config
from mm.market_selector import select_and_write


def _to_int(val: Any, default: int = 0) -> int:
    try:
        return int(float(val))
    except Exception:
        return default


def _build_cutoff_map(topics: List[Dict[str, Any]]) -> Dict[str, int]:
    cutoff_map: Dict[str, int] = {}
    for item in topics:
        parent_cutoff = _to_int(item.get("cutoffTime")) or _to_int(item.get("resolvedTime"))
        child_list = item.get("childList") or []
        if child_list:
            for child in child_list:
                topic_id = child.get("topicId")
                if topic_id is None:
                    continue
                child_cutoff = _to_int(child.get("cutoffTime")) or _to_int(child.get("resolvedTime"))
                cutoff_map[str(topic_id)] = child_cutoff or parent_cutoff
        else:
            topic_id = item.get("topicId")
            if topic_id is None:
                continue
            cutoff_map[str(topic_id)] = _to_int(item.get("cutoffTime")) or _to_int(item.get("resolvedTime"))
    return cutoff_map


def _format_ts(ts: int) -> str:
    if ts <= 0:
        return "N/A"
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def _format_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "N/A"
    if seconds < 0:
        return "overdue " + _format_duration(-seconds)
    total = int(seconds)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if mins:
        parts.append(f"{mins}m")
    if not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)


def _format_float(val: Any, decimals: int = 6) -> str:
    try:
        num = float(val)
    except Exception:
        return "N/A"
    fmt = f"{{:.{decimals}f}}".format(num).rstrip("0").rstrip(".")
    return fmt or "0"


def _format_volume(val: Any) -> str:
    try:
        num = float(val)
    except Exception:
        return "N/A"
    text = f"{num:,.3f}".rstrip("0").rstrip(".")
    return text


def _print_table(rows: List[Dict[str, str]], headers: List[str]) -> None:
    widths = {h: len(h) for h in headers}
    for row in rows:
        for h in headers:
            widths[h] = max(widths[h], len(row.get(h, "")))
    header_line = " | ".join(h.ljust(widths[h]) for h in headers)
    print(header_line)
    print("-+-".join("-" * widths[h] for h in headers))
    for row in rows:
        print(" | ".join(row.get(h, "").ljust(widths[h]) for h in headers))


def main() -> None:
    ap = argparse.ArgumentParser(description="Select Opinion markets by volume rank + price target.")
    ap.add_argument("--config", default="mm_config.json", help="Config JSON path.")
    args = ap.parse_args()

    cfg = load_config(args.config)
    selected = select_and_write(cfg)
    selector_cfg = cfg.get("market_selector", {})
    source_file = selector_cfg.get("source_file", "opinion_topics_merged.json")

    with open(source_file, "r", encoding="utf-8") as f:
        topics = json.load(f)
    cutoff_map = _build_cutoff_map(topics)

    print(
        f"[OK] selected={len(selected)} "
        f"source={source_file} "
        f"output={selector_cfg.get('output_file', 'selected_markets.json')}"
    )

    now = time.time()
    rows: List[Dict[str, str]] = []
    for entry in selected:
        topic_id = str(entry.get("topicId") or "")
        cutoff = cutoff_map.get(topic_id, 0)
        title = str(entry.get("title") or "")
        parent = entry.get("parentTitle")
        market_name = f"{parent} / {title}" if parent else title
        rows.append(
            {
                "settle_time": _format_ts(cutoff),
                "time_to_settle": _format_duration(cutoff - now if cutoff else None),
                "market_name": market_name,
                "last_price": _format_float(entry.get("price")),
                "volume": _format_volume(entry.get("volume")),
            }
        )
    if rows:
        _print_table(rows, ["settle_time", "time_to_settle", "market_name", "last_price", "volume"])


if __name__ == "__main__":
    main()
