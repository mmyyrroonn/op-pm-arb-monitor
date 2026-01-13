#!/usr/bin/env python3
import argparse
import json
import math
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from textual.app import App, ComposeResult
from textual.widgets import DataTable, Static


def _load_json(path: str) -> Optional[Any]:
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _fmt_price(val: Any) -> str:
    try:
        num = float(val)
    except Exception:
        return "--"
    return f"{num:.4f}"


def _fmt_size(val: Any) -> str:
    try:
        num = float(val)
    except Exception:
        return "--"
    if num >= 1000:
        return f"{num:.1f}"
    return f"{num:.2f}"


def _fmt_quote(price: Any, size: Any) -> str:
    price_s = _fmt_price(price)
    size_s = _fmt_size(size)
    if price_s == "--" and size_s == "--":
        return "--"
    return f"{price_s}@{size_s}"


def _truncate(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    return text[: width - 3] + "..."


def _market_key(entry: Dict[str, Any]) -> str:
    for key in ("topicId", "questionId", "title", "symbol"):
        val = entry.get(key)
        if val is not None and str(val).strip():
            return str(val)
    return "unknown"


def _aggregate_rows(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    markets: Dict[str, Dict[str, Any]] = {}
    for entry in results:
        if not isinstance(entry, dict):
            continue
        side = entry.get("side")
        if side not in ("yes", "no"):
            continue
        key = _market_key(entry)
        info = markets.setdefault(
            key,
            {
                "title": entry.get("title") or key,
                "topicId": entry.get("topicId"),
                "questionId": entry.get("questionId"),
                "yes": {},
                "no": {},
            },
        )
        info[side] = {
            "best_bid": entry.get("best_bid"),
            "best_bid_size": entry.get("best_bid_size"),
            "best_ask": entry.get("best_ask"),
            "best_ask_size": entry.get("best_ask_size"),
        }
    rows = list(markets.values())
    rows.sort(key=lambda x: str(x.get("title") or ""))
    return rows


def _normalize_rows(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for entry in results:
        if not isinstance(entry, dict):
            continue
        key = _market_key(entry)
        rows.append(
            {
                "title": entry.get("title") or key,
                "topicId": entry.get("topicId"),
                "questionId": entry.get("questionId"),
                "yes": entry.get("yes") or {},
                "no": entry.get("no") or {},
            }
        )
    rows.sort(key=lambda x: str(x.get("title") or ""))
    return rows


def _extract_rows(payload: Any) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    meta: Dict[str, Any] = {}
    results: List[Dict[str, Any]] = []
    if isinstance(payload, dict):
        meta = payload
        if isinstance(payload.get("results"), list):
            results = payload["results"]
        elif isinstance(payload.get("data"), list):
            results = payload["data"]
    elif isinstance(payload, list):
        results = payload
    if results and isinstance(results[0], dict) and "side" in results[0]:
        return _aggregate_rows(results), meta
    return _normalize_rows(results), meta


class DepthApp(App):
    CSS = """
    #header {
        height: 4;
    }
    #footer {
        height: 1;
    }
    DataTable {
        height: 1fr;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("n", "next_page", "Next Page"),
        ("p", "prev_page", "Prev Page"),
        ("r", "reload", "Reload"),
    ]

    def __init__(self, path: str, refresh: float, page_size: int) -> None:
        super().__init__()
        self.path = path
        self.refresh = refresh
        self.page_size = max(1, page_size)
        self.page = 0
        self.rows: List[Dict[str, Any]] = []
        self.meta: Dict[str, Any] = {}
        self.total_pages = 1

    def compose(self) -> ComposeResult:
        yield Static("", id="header")
        yield DataTable(id="table")
        yield Static("", id="footer")

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        try:
            table.clear(columns=True)
        except TypeError:
            table.clear()
        self._ensure_columns(table)
        self.action_reload()
        if self.refresh > 0:
            self.set_interval(self.refresh, self.action_reload)

    def action_reload(self) -> None:
        payload = _load_json(self.path)
        rows, meta = _extract_rows(payload)
        self.rows = rows
        self.meta = meta
        self.total_pages = max(1, int(math.ceil(len(rows) / self.page_size)))
        self.page = min(self.page, self.total_pages - 1)
        self._render()

    def action_next_page(self) -> None:
        if self.page + 1 < self.total_pages:
            self.page += 1
            self._render()

    def action_prev_page(self) -> None:
        if self.page > 0:
            self.page -= 1
            self._render()

    def _render(self) -> None:
        header = self.query_one("#header", Static)
        footer = self.query_one("#footer", Static)
        table = self.query_one(DataTable)

        ts = self.meta.get("timestamp") or time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        round_idx = self.meta.get("round")
        ok = self.meta.get("ok")
        failed = self.meta.get("failed")
        success = self.meta.get("success_rate")
        elapsed = self.meta.get("elapsed_seconds")
        skipped_cutoff = self.meta.get("skipped_cutoff")
        skipped_volume = self.meta.get("skipped_volume")
        header.update(
            "\n".join(
                [
                    f"Opinion Depth TUI  {ts}",
                    f"round={round_idx} count={len(self.rows)} ok={ok} failed={failed} "
                    f"success={success} elapsed={elapsed}",
                    f"skipped_cutoff={skipped_cutoff} skipped_volume={skipped_volume}",
                ]
            )
        )
        footer.update(f"page {self.page + 1}/{self.total_pages}  n/p:page  r:reload  q:quit")

        table.clear()
        self._ensure_columns(table)
        start = self.page * self.page_size
        end = min(len(self.rows), start + self.page_size)
        for row in self.rows[start:end]:
            yes = row.get("yes") or {}
            no = row.get("no") or {}
            title = _truncate(str(row.get("title") or ""), 40)
            table.add_row(
                title,
                _fmt_quote(yes.get("best_bid"), yes.get("best_bid_size")),
                _fmt_quote(yes.get("best_ask"), yes.get("best_ask_size")),
                _fmt_quote(no.get("best_bid"), no.get("best_bid_size")),
                _fmt_quote(no.get("best_ask"), no.get("best_ask_size")),
            )

    def _ensure_columns(self, table: DataTable) -> None:
        columns = getattr(table, "columns", None)
        if columns:
            return
        table.add_column("Market", width=40)
        table.add_column("Yes Bid", width=14)
        table.add_column("Yes Ask", width=14)
        table.add_column("No Bid", width=14)
        table.add_column("No Ask", width=14)


def main() -> int:
    ap = argparse.ArgumentParser(description="Opinion depth Textual UI")
    ap.add_argument("--input", default="opinion_depth_ui.json")
    ap.add_argument("--refresh", type=float, default=1.0)
    ap.add_argument("--page-size", type=int, default=15)
    args = ap.parse_args()
    app = DepthApp(args.input, args.refresh, args.page_size)
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
