#!/usr/bin/env python3
import argparse
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from textual.app import App, ComposeResult
from textual.widgets import DataTable, Static

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from mm.config import load_config, resolve_secret
from opinion_clob_sdk import Client


def _get_field(obj: Any, keys: List[str]) -> Any:
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


def _to_int(val: Any) -> Optional[int]:
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _fmt_ts(val: Any) -> str:
    ts = _to_int(val)
    if ts is None:
        return "--"
    if ts > 10_000_000_000:
        ts = int(ts / 1000)
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
    except Exception:
        return "--"


def _fmt_price(val: Any) -> str:
    num = _to_float(val)
    if num is None:
        return "--"
    return f"{num:.4f}"


def _fmt_size(val: Any) -> str:
    num = _to_float(val)
    if num is None:
        return "--"
    if abs(num) >= 1000:
        return f"{num:.1f}"
    return f"{num:.2f}"


def _fmt_money(val: Any) -> str:
    num = _to_float(val)
    if num is None:
        return "--"
    if abs(num) >= 1000:
        return f"{num:,.2f}"
    return f"{num:.4f}"


def _fmt_percent(val: Any) -> str:
    if val is None:
        return "--"
    text = str(val)
    if "%" in text:
        return text
    num = _to_float(val)
    if num is None:
        return text
    if abs(num) <= 1:
        num *= 100
    return f"{num:.2f}%"


def _truncate(text: Any, width: int) -> str:
    if width <= 0:
        return ""
    raw = str(text) if text is not None else ""
    if len(raw) <= width:
        return raw
    if width <= 3:
        return raw[:width]
    return raw[: width - 3] + "..."


def _short_addr(addr: Any, width: int = 12) -> str:
    if not addr:
        return "--"
    text = str(addr)
    if len(text) <= width:
        return text
    head = max(4, width // 2 - 1)
    tail = max(3, width - head - 3)
    return f"{text[:head]}...{text[-tail:]}"


def _normalize_side(val: Any) -> Optional[str]:
    if val is None:
        return None
    if isinstance(val, str):
        text = val.strip()
        if text:
            return text
    num = _to_int(val)
    if num is None:
        return None
    if num == 1:
        return "Buy"
    if num == 2:
        return "Sell"
    return None


def _normalize_outcome(val: Any) -> Optional[str]:
    if val is None:
        return None
    if isinstance(val, str):
        text = val.strip()
        if text:
            return text
    num = _to_int(val)
    if num is None:
        return None
    if num == 1:
        return "Yes"
    if num == 2:
        return "No"
    return None


def _build_client(config: Dict[str, Any]) -> Client:
    op_cfg = config.get("opinion", {})
    api_key = resolve_secret(op_cfg, "api_key", op_cfg.get("api_key_env", ""))
    rpc_url = resolve_secret(op_cfg, "rpc_url", op_cfg.get("rpc_url_env", ""))
    private_key = resolve_secret(op_cfg, "private_key", op_cfg.get("private_key_env", ""))
    multi_sig_addr = resolve_secret(op_cfg, "multi_sig_addr", op_cfg.get("multi_sig_addr_env", ""))
    return Client(
        host=op_cfg.get("host", ""),
        apikey=api_key,
        chain_id=int(op_cfg.get("chain_id", 56)),
        rpc_url=rpc_url,
        private_key=private_key,
        multi_sig_addr=multi_sig_addr,
    )


def _parse_list(client: Client, response: Any, label: str, errors: List[str]) -> List[Any]:
    try:
        return client._parse_list_response(response, label)
    except Exception as exc:
        errors.append(f"{label} failed: {exc}")
        return []


def _fetch_orders(
    client: Client,
    status: str,
    limit: int,
    max_pages: int,
    errors: List[str],
) -> List[Any]:
    all_orders: List[Any] = []
    page = 1
    limit = max(1, int(limit))
    max_pages = max(1, int(max_pages))
    while page <= max_pages:
        try:
            resp = client.get_my_orders(
                market_id=0,
                status=status,
                limit=limit,
                page=page,
            )
        except Exception as exc:
            errors.append(f"orders page {page} failed: {exc}")
            break
        rows = _parse_list(client, resp, f"orders page {page}", errors)
        if not rows:
            break
        all_orders.extend(rows)
        if len(rows) < limit:
            break
        page += 1
    return all_orders


def _fetch_positions(
    client: Client,
    limit: int,
    max_pages: int,
    errors: List[str],
) -> List[Any]:
    all_positions: List[Any] = []
    page = 1
    limit = max(1, int(limit))
    max_pages = max(1, int(max_pages))
    while page <= max_pages:
        try:
            resp = client.get_my_positions(
                market_id=0,
                limit=limit,
                page=page,
            )
        except Exception as exc:
            errors.append(f"positions page {page} failed: {exc}")
            break
        rows = _parse_list(client, resp, f"positions page {page}", errors)
        if not rows:
            break
        all_positions.extend(rows)
        if len(rows) < limit:
            break
        page += 1
    return all_positions


def _fetch_balances(client: Client, errors: List[str]) -> List[Any]:
    try:
        resp = client.get_my_balances()
    except Exception as exc:
        errors.append(f"balances failed: {exc}")
        return []
    result = _get_field(resp, ["result"])
    balances = _get_field(result, ["balances"]) if result is not None else None
    if isinstance(balances, list):
        return balances
    return []


def _fetch_quote_token_symbols(client: Client, errors: List[str]) -> Dict[str, str]:
    try:
        resp = client.get_quote_tokens()
    except Exception as exc:
        errors.append(f"quote tokens failed: {exc}")
        return {}
    rows = _parse_list(client, resp, "quote tokens", errors)
    symbols: Dict[str, str] = {}
    for row in rows:
        addr = _get_field(row, ["quoteTokenAddress", "quote_token_address"])
        symbol = _get_field(row, ["symbol", "quoteTokenName", "quote_token_name"])
        if not addr:
            continue
        symbols[str(addr).lower()] = str(symbol) if symbol else str(addr)
    return symbols


def _normalize_orders(orders: List[Any]) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for order in orders:
        market_title = _get_field(order, ["marketTitle", "market_title", "rootMarketTitle", "root_market_title"])
        status_enum = _get_field(order, ["statusEnum", "status_enum"])
        status_raw = _get_field(order, ["status"])
        status_code = _status_code_from_text(status_raw) or _status_code_from_text(status_enum)
        normalized.append(
            {
                "order_id": _get_field(order, ["orderId", "order_id"]),
                "market_id": _get_field(order, ["marketId", "market_id"]),
                "market_title": market_title or "",
                "side": _get_field(order, ["sideEnum", "side_enum"]) or _normalize_side(_get_field(order, ["side"])),
                "outcome": _get_field(order, ["outcomeSideEnum", "outcome_side_enum", "outcome"]) or _normalize_outcome(
                    _get_field(order, ["outcomeSide", "outcome_side"])
                ),
                "price": _get_field(order, ["price"]),
                "order_shares": _get_field(order, ["orderShares", "order_shares"]),
                "filled_shares": _get_field(order, ["filledShares", "filled_shares"]),
                "status": status_enum or str(status_raw or ""),
                "status_code": status_code,
                "created_at": _get_field(order, ["createdAt", "created_at"]),
            }
        )
    normalized.sort(key=lambda x: _to_int(x.get("created_at")) or 0, reverse=True)
    return normalized


def _normalize_positions(positions: List[Any]) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for pos in positions:
        market_title = _get_field(pos, ["marketTitle", "market_title", "rootMarketTitle", "root_market_title"])
        normalized.append(
            {
                "market_id": _get_field(pos, ["marketId", "market_id"]),
                "market_title": market_title or "",
                "outcome": _get_field(pos, ["outcomeSideEnum", "outcome_side_enum", "outcome"]) or _normalize_outcome(
                    _get_field(pos, ["outcomeSide", "outcome_side"])
                ),
                "shares_owned": _get_field(pos, ["sharesOwned", "shares_owned"]),
                "shares_frozen": _get_field(pos, ["sharesFrozen", "shares_frozen"]),
                "avg_entry_price": _get_field(pos, ["avgEntryPrice", "avg_entry_price"]),
                "current_value": _get_field(pos, ["currentValueInQuoteToken", "current_value_in_quote_token"]),
                "unrealized_pnl": _get_field(pos, ["unrealizedPnl", "unrealized_pnl"]),
                "unrealized_pnl_pct": _get_field(pos, ["unrealizedPnlPercent", "unrealized_pnl_percent"]),
                "daily_pnl_pct": _get_field(pos, ["dailyPnlChangePercent", "daily_pnl_change_percent"]),
                "status": _get_field(pos, ["marketStatusEnum", "market_status_enum"]),
            }
        )
    normalized.sort(key=lambda x: str(x.get("market_title") or ""))
    return normalized


def _normalize_balances(balances: List[Any], symbols: Dict[str, str]) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for bal in balances:
        token = _get_field(bal, ["quoteToken", "quote_token"])
        token_key = str(token).lower() if token else ""
        normalized.append(
            {
                "token": token or "",
                "symbol": symbols.get(token_key, ""),
                "available": _get_field(bal, ["availableBalance", "available_balance"]),
                "frozen": _get_field(bal, ["frozenBalance", "frozen_balance"]),
                "total": _get_field(bal, ["totalBalance", "total_balance"]),
            }
        )
    normalized.sort(key=lambda x: str(x.get("symbol") or x.get("token") or ""))
    return normalized


def _build_market_summary(
    orders: List[Dict[str, Any]],
    positions: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    summary: Dict[str, Dict[str, Any]] = {}
    for order in orders:
        market_id = order.get("market_id")
        if market_id is None:
            continue
        key = str(market_id)
        entry = summary.setdefault(
            key,
            {
                "market_id": market_id,
                "market_title": order.get("market_title") or "",
                "orders": 0,
                "positions": 0,
                "shares": 0.0,
                "status": "",
            },
        )
        entry["orders"] += 1
        if order.get("market_title") and not entry.get("market_title"):
            entry["market_title"] = order.get("market_title")

    for pos in positions:
        market_id = pos.get("market_id")
        if market_id is None:
            continue
        key = str(market_id)
        entry = summary.setdefault(
            key,
            {
                "market_id": market_id,
                "market_title": pos.get("market_title") or "",
                "orders": 0,
                "positions": 0,
                "shares": 0.0,
                "status": "",
            },
        )
        entry["positions"] += 1
        shares = _to_float(pos.get("shares_owned")) or 0.0
        entry["shares"] += shares
        status = pos.get("status")
        if status and not entry.get("status"):
            entry["status"] = status
        if pos.get("market_title") and not entry.get("market_title"):
            entry["market_title"] = pos.get("market_title")

    rows = list(summary.values())
    rows.sort(key=lambda x: str(x.get("market_title") or ""))
    return rows


class AccountApp(App):
    CSS = """
    #header {
        height: 4;
    }
    #errors {
        height: 2;
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
        ("r", "reload", "Reload"),
        ("o", "view_orders", "Orders"),
        ("n", "view_pending", "Pending"),
        ("p", "view_positions", "Positions"),
        ("b", "view_balances", "Balances"),
        ("m", "view_markets", "Markets"),
    ]

    def __init__(
        self,
        *,
        client: Client,
        refresh: float,
        view: str,
        orders_status: str,
        orders_limit: int,
        orders_max_pages: int,
        positions_limit: int,
        positions_max_pages: int,
    ) -> None:
        super().__init__()
        self.client = client
        # Avoid clobbering App.refresh() method.
        self.refresh_interval = refresh
        self.view = view
        self.orders_status = orders_status
        self.orders_limit = orders_limit
        self.orders_max_pages = orders_max_pages
        self.positions_limit = positions_limit
        self.positions_max_pages = positions_max_pages
        self.orders: List[Dict[str, Any]] = []
        self.positions: List[Dict[str, Any]] = []
        self.balances: List[Dict[str, Any]] = []
        self.markets: List[Dict[str, Any]] = []
        self.errors: List[str] = []
        self.last_refresh: Optional[float] = None
        self._loading = False
        self._symbols: Dict[str, str] = {}

    def compose(self) -> ComposeResult:
        yield Static("", id="header")
        yield Static("", id="errors")
        yield DataTable(id="table")
        yield Static("", id="footer")

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        try:
            table.clear(columns=True)
        except TypeError:
            table.clear()
        self.action_reload()
        if self.refresh_interval > 0:
            self.set_interval(self.refresh_interval, self.action_reload)

    def action_reload(self) -> None:
        if self._loading:
            return
        self._loading = True
        try:
            errors: List[str] = []
            if not self._symbols:
                self._symbols = _fetch_quote_token_symbols(self.client, errors)
            raw_orders = _fetch_orders(
                self.client,
                self.orders_status,
                self.orders_limit,
                self.orders_max_pages,
                errors,
            )
            raw_positions = _fetch_positions(
                self.client,
                self.positions_limit,
                self.positions_max_pages,
                errors,
            )
            raw_balances = _fetch_balances(self.client, errors)
            self.orders = _normalize_orders(raw_orders)
            self.positions = _normalize_positions(raw_positions)
            self.balances = _normalize_balances(raw_balances, self._symbols)
            self.markets = _build_market_summary(self.orders, self.positions)
            self.errors = errors
        except Exception as exc:
            self.errors = [f"reload failed: {exc}"]
        finally:
            self.last_refresh = time.time()
            self._loading = False
            self._render()

    def action_view_orders(self) -> None:
        self.view = "orders"
        self._render()

    def action_view_pending(self) -> None:
        self.view = "pending"
        self._render()

    def action_view_positions(self) -> None:
        self.view = "positions"
        self._render()

    def action_view_balances(self) -> None:
        self.view = "balances"
        self._render()

    def action_view_markets(self) -> None:
        self.view = "markets"
        self._render()

    def _render(self) -> None:
        header = self.query_one("#header", Static)
        errors = self.query_one("#errors", Static)
        footer = self.query_one("#footer", Static)
        table = self.query_one(DataTable)

        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.last_refresh or time.time()))
        status_label = self.orders_status if self.orders_status else "all"
        pending_count = sum(1 for row in self.orders if _is_pending_order(row))
        header_lines = [
            f"Opinion Account Monitor  {ts}",
            f"view={self.view} orders={len(self.orders)} pending={pending_count} positions={len(self.positions)} "
            f"balances={len(self.balances)} markets={len(self.markets)}",
            f"orders_status={status_label} orders_pages={self.orders_max_pages} "
            f"positions_pages={self.positions_max_pages} refresh={self.refresh_interval}s",
        ]
        header.update("\n".join(header_lines))

        if self.errors:
            errors.update("\n".join(self.errors[:2]))
        else:
            errors.update("")

        footer.update("o/n/p/b/m:view  r:reload  q:quit")

        self._render_table(table)

    def _reset_table(self, table: DataTable, columns: List[Tuple[str, int]]) -> None:
        try:
            table.clear(columns=True)
        except TypeError:
            table.clear()
        for title, width in columns:
            table.add_column(title, width=width)

    def _render_table(self, table: DataTable) -> None:
        if self.view == "positions":
            self._reset_table(
                table,
                [
                    ("Market", 36),
                    ("ID", 8),
                    ("Outcome", 8),
                    ("Shares", 10),
                    ("Frozen", 10),
                    ("Avg Px", 10),
                    ("Value", 12),
                    ("UPnL", 12),
                    ("UPnL%", 8),
                    ("Daily%", 8),
                ],
            )
            for row in self.positions:
                title = _truncate(row.get("market_title") or "", 36)
                table.add_row(
                    title,
                    str(row.get("market_id") or "--"),
                    _truncate(row.get("outcome") or "--", 8),
                    _fmt_size(row.get("shares_owned")),
                    _fmt_size(row.get("shares_frozen")),
                    _fmt_price(row.get("avg_entry_price")),
                    _fmt_money(row.get("current_value")),
                    _fmt_money(row.get("unrealized_pnl")),
                    _fmt_percent(row.get("unrealized_pnl_pct")),
                    _fmt_percent(row.get("daily_pnl_pct")),
                )
            return

        if self.view == "balances":
            self._reset_table(
                table,
                [
                    ("Token", 14),
                    ("Symbol", 10),
                    ("Available", 14),
                    ("Frozen", 14),
                    ("Total", 14),
                ],
            )
            for row in self.balances:
                table.add_row(
                    _short_addr(row.get("token"), 14),
                    _truncate(row.get("symbol") or "--", 10),
                    _fmt_money(row.get("available")),
                    _fmt_money(row.get("frozen")),
                    _fmt_money(row.get("total")),
                )
            return

        if self.view == "markets":
            self._reset_table(
                table,
                [
                    ("Market", 40),
                    ("ID", 8),
                    ("Orders", 8),
                    ("Positions", 10),
                    ("Shares", 12),
                    ("Status", 12),
                ],
            )
            for row in self.markets:
                table.add_row(
                    _truncate(row.get("market_title") or "", 40),
                    str(row.get("market_id") or "--"),
                    str(row.get("orders") or 0),
                    str(row.get("positions") or 0),
                    _fmt_size(row.get("shares")),
                    _truncate(row.get("status") or "--", 12),
                )
            return

        if self.view == "pending":
            rows = [row for row in self.orders if _is_pending_order(row)]
        else:
            rows = self.orders

        self._reset_table(
            table,
            [
                ("Created", 16),
                ("Market", 36),
                ("Side", 6),
                ("Outcome", 8),
                ("Price", 8),
                ("Size", 8),
                ("Filled", 8),
                ("Status", 10),
                ("Order ID", 14),
            ],
        )
        for row in rows:
            title = _truncate(row.get("market_title") or "", 36)
            order_id = _truncate(row.get("order_id") or "", 14)
            table.add_row(
                _fmt_ts(row.get("created_at")),
                title,
                _truncate(row.get("side") or "--", 6),
                _truncate(row.get("outcome") or "--", 8),
                _fmt_price(row.get("price")),
                _fmt_size(row.get("order_shares")),
                _fmt_size(row.get("filled_shares")),
                _truncate(row.get("status") or "--", 10),
                order_id,
            )


def _normalize_status_arg(status: str) -> str:
    if not status:
        return ""
    val = status.strip().lower()
    if val in ("all", "any", "none", "0"):
        return ""
    return status.strip()


def _status_code_from_text(text: Any) -> Optional[int]:
    if text is None:
        return None
    if isinstance(text, int):
        return text
    raw = str(text).strip()
    if not raw:
        return None
    if raw.isdigit():
        return int(raw)
    lowered = raw.lower()
    mapping = {
        "pending": 1,
        "finished": 2,
        "canceled": 3,
        "cancelled": 3,
        "expired": 4,
        "failed": 5,
    }
    for key, code in mapping.items():
        if lowered.startswith(key):
            return code
    return None


def _is_pending_order(order: Dict[str, Any]) -> bool:
    code = _to_int(order.get("status_code"))
    if code is not None:
        return code == 1
    status_text = order.get("status")
    code = _status_code_from_text(status_text)
    if code is not None:
        return code == 1
    return False


def main() -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(description="Opinion account monitor (Textual TUI).")
    ap.add_argument("--config", default="mm_config.json", help="Config JSON path.")
    ap.add_argument("--refresh", type=float, default=5.0, help="Refresh interval seconds.")
    ap.add_argument("--view", default="orders", help="Initial view: orders|pending|positions|balances|markets.")
    ap.add_argument("--orders-status", default="all", help="Order status filter (use 'all' for no filter).")
    ap.add_argument("--orders-limit", type=int, default=0, help="Orders page size (0=from config).")
    ap.add_argument("--orders-max-pages", type=int, default=0, help="Orders max pages (0=from config).")
    ap.add_argument("--positions-limit", type=int, default=0, help="Positions page size (0=from config).")
    ap.add_argument("--positions-max-pages", type=int, default=0, help="Positions max pages (0=from config).")
    args = ap.parse_args()

    config = load_config(args.config)
    client = _build_client(config)

    order_sync_cfg = config.get("order_sync", {}) if isinstance(config, dict) else {}
    orders_limit = int(args.orders_limit or order_sync_cfg.get("limit", 20))
    orders_max_pages = int(args.orders_max_pages or order_sync_cfg.get("max_pages", 3))
    positions_limit = int(args.positions_limit or order_sync_cfg.get("limit", 20))
    positions_max_pages = int(args.positions_max_pages or order_sync_cfg.get("max_pages", 3))
    view = args.view.strip().lower()
    if view not in ("orders", "pending", "positions", "balances", "markets"):
        view = "orders"

    app = AccountApp(
        client=client,
        refresh=args.refresh,
        view=view,
        orders_status=_normalize_status_arg(args.orders_status),
        orders_limit=orders_limit,
        orders_max_pages=orders_max_pages,
        positions_limit=positions_limit,
        positions_max_pages=positions_max_pages,
    )
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
