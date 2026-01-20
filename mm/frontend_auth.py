import os
import threading
import time
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

from eth_account import Account

from opinion_frontend_auth import (
    DEFAULT_CHAIN_ID,
    DEFAULT_MIN_SLEEP,
    DEFAULT_REFRESH_BEFORE,
    DEFAULT_TIMEOUT,
    _build_env_updates,
    _checksum_address,
    _device_fingerprint,
    _normalize_privkey,
    _refresh_account,
    _resolve_addresses,
    _resolve_device_fingerprints,
    _resolve_private_keys,
    _update_env_file,
)


def _build_accounts(logger: Optional[Any] = None) -> List[Dict[str, Any]]:
    args = SimpleNamespace(
        private_keys=None,
        private_key=None,
        addresses=None,
        address=None,
        device_fingerprints=None,
        device_fingerprint=None,
    )
    privkey_raw_list = _resolve_private_keys(args)
    if not privkey_raw_list:
        raise RuntimeError(
            "Missing OPINION_PRIVATE_KEY(S) (set in .env or use OPINION_PRIVATE_KEYS)."
        )
    privkeys = []
    for idx, key in enumerate(privkey_raw_list, start=1):
        privkeys.append(_normalize_privkey(key, f"private key #{idx}"))

    address_list = _resolve_addresses(args)
    if address_list:
        address_list = [_checksum_address(addr) for addr in address_list]
    if len(privkeys) > 1 and len(address_list) == 1:
        if logger:
            logger.warning(
                "Ignoring OPINION_WALLET_ADDRESS because multiple private keys were provided."
            )
        address_list = []
    if address_list and len(address_list) not in (1, len(privkeys)):
        raise RuntimeError("OPINION_WALLET_ADDRESSES count must match private keys.")

    try:
        device_fps = _resolve_device_fingerprints(args, len(privkeys))
    except SystemExit as exc:
        raise RuntimeError(str(exc)) from exc

    accounts: List[Dict[str, Any]] = []
    for idx, privkey in enumerate(privkeys):
        account = Account.from_key(privkey)
        address = _checksum_address(account.address)
        if address_list and len(address_list) == len(privkeys):
            if address_list[idx].lower() != address.lower():
                raise RuntimeError(f"Provided address does not match private key: {address_list[idx]}")
        device_fp = device_fps[idx] if device_fps else _device_fingerprint(address)
        accounts.append(
            {
                "privkey": privkey,
                "address": address,
                "device_fingerprint": device_fp,
                "token": None,
                "expire": None,
                "next_refresh_at": 0,
            }
        )
    return accounts


class FrontendAuthRefresher:
    def __init__(
        self,
        update_callback: Callable[[str, str], None],
        *,
        chain_id: int = DEFAULT_CHAIN_ID,
        refresh_before: int = DEFAULT_REFRESH_BEFORE,
        min_sleep: int = DEFAULT_MIN_SLEEP,
        timeout_s: int = DEFAULT_TIMEOUT,
        refresh_interval_s: Optional[int] = None,
        env_path: Optional[str] = ".env",
        persist_env: bool = True,
        refresh_on_start: bool = True,
        logger: Optional[Any] = None,
    ) -> None:
        self.update_callback = update_callback
        self.chain_id = chain_id
        self.refresh_before = refresh_before
        self.min_sleep = max(1, int(min_sleep))
        self.timeout_s = timeout_s
        self.refresh_interval_s = refresh_interval_s
        self.env_path = env_path
        self.persist_env = persist_env
        self.refresh_on_start = refresh_on_start
        self.logger = logger
        self.accounts = _build_accounts(logger=logger)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _log(self, level: str, msg: str, *args: Any) -> None:
        if self.logger:
            log_fn = getattr(self.logger, level, None)
            if log_fn:
                log_fn(msg, *args)
                return
        if args:
            msg = msg % args
        print(msg)

    def _apply_updates(self, updates: Dict[str, str]) -> None:
        for key, val in updates.items():
            os.environ[key] = val
        if self.persist_env and self.env_path:
            _update_env_file(self.env_path, updates)

    def _update_client(self) -> None:
        tokens = [acct.get("token") for acct in self.accounts if acct.get("token")]
        fps = [acct.get("device_fingerprint") for acct in self.accounts if acct.get("device_fingerprint")]
        if not tokens:
            self._log("warning", "frontend auth refresh missing tokens; skip client update")
            return
        self.update_callback(",".join(tokens), ",".join(fps))

    def _refresh_accounts(self, due: List[Dict[str, Any]]) -> None:
        for acct in due:
            _refresh_account(
                acct,
                chain_id=self.chain_id,
                timeout_s=self.timeout_s,
                refresh_before=self.refresh_before,
            )
            if self.refresh_interval_s:
                target = time.time() + self.refresh_interval_s
                acct["next_refresh_at"] = min(acct["next_refresh_at"], target)
        updates = _build_env_updates(self.accounts)
        self._apply_updates(updates)
        self._update_client()

    def _run(self) -> None:
        if self.refresh_on_start:
            try:
                self._refresh_accounts(self.accounts)
                self._log("info", "frontend auth refreshed accounts=%d", len(self.accounts))
            except Exception as exc:
                self._log("warning", "frontend auth refresh failed err=%s", exc)
                self._stop.wait(self.min_sleep)

        while not self._stop.is_set():
            now = time.time()
            due = [acct for acct in self.accounts if acct["next_refresh_at"] <= now]
            if not due:
                next_refresh = min(acct["next_refresh_at"] for acct in self.accounts)
                sleep_for = int(max(self.min_sleep, next_refresh - now))
                self._stop.wait(sleep_for)
                continue

            try:
                self._refresh_accounts(due)
                self._log("info", "frontend auth refreshed accounts=%d", len(due))
            except Exception as exc:
                self._log("warning", "frontend auth refresh failed err=%s", exc)
                self._stop.wait(self.min_sleep)
