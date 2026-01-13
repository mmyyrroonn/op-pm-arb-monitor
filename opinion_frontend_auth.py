#!/usr/bin/env python3
import argparse
import base64
import hashlib
import json
import os
import secrets
import sys
import time
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import to_checksum_address

TOKEN_ENDPOINT = "https://proxy.opinion.trade:8443/api/bsc/api/v1/user/token"
DEFAULT_DOMAIN = "app.opinion.trade"
DEFAULT_URI = "https://app.opinion.trade"
DEFAULT_CHAIN_ID = 56
DEFAULT_REFRESH_BEFORE = 300
DEFAULT_MIN_SLEEP = 30
DEFAULT_TIMEOUT = 20


def _now_iso(ts: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts))


def _generate_nonce() -> str:
    # 17-digit random nonce, matches observed frontend shape.
    return str(secrets.randbelow(10**17 - 10**16) + 10**16)


def _split_list(raw: str) -> List[str]:
    if not raw:
        return []
    raw = raw.strip()
    if not raw:
        return []
    raw = raw.replace("\n", ",")
    parts = [item.strip() for item in raw.split(",")]
    return [item for item in parts if item]


def _build_siwe_message(address: str, nonce: str, issued_at: str, chain_id: int) -> str:
    return (
        f"{DEFAULT_DOMAIN} wants you to sign in with your Ethereum account:\n"
        f"{address}\n\n"
        "Welcome to opinion.trade! By proceeding, you agree to our Privacy Policy and Terms of Use.\n\n"
        f"URI: {DEFAULT_URI}\n"
        "Version: 1\n"
        f"Chain ID: {chain_id}\n"
        f"Nonce: {nonce}\n"
        f"Issued At: {issued_at}"
    )


def _checksum_address(address: str) -> str:
    return to_checksum_address(address)


def _normalize_privkey(privkey: str, label: str = "private key") -> str:
    key = privkey.strip().strip('"').strip("'")
    if key[:2].lower() == "0x":
        key = key[2:]
    if len(key) != 64:
        raise ValueError(f"{label} length invalid (expected 32 bytes hex)")
    return "0x" + key


def _device_fingerprint(address: str) -> str:
    return hashlib.md5(address.lower().encode("utf-8")).hexdigest()


def _decode_jwt_exp(token: str) -> Optional[int]:
    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode("utf-8")))
    except Exception:
        return None
    exp = data.get("exp")
    try:
        return int(exp)
    except Exception:
        return None


def _resolve_private_keys(args: argparse.Namespace) -> List[str]:
    privkeys = []
    if args.private_keys:
        privkeys.extend(_split_list(args.private_keys))
    if args.private_key:
        privkeys.append(args.private_key)
    if not privkeys:
        privkeys.extend(_split_list(os.getenv("OPINION_PRIVATE_KEYS", "")))
    if not privkeys:
        env_key = os.getenv("OPINION_PRIVATE_KEY", "").strip()
        if env_key:
            privkeys.append(env_key)
    return privkeys


def _resolve_addresses(args: argparse.Namespace) -> List[str]:
    if args.addresses:
        return _split_list(args.addresses)
    if args.address:
        return [args.address]
    env_list = _split_list(os.getenv("OPINION_WALLET_ADDRESSES", ""))
    if env_list:
        return env_list
    env_single = os.getenv("OPINION_WALLET_ADDRESS", "").strip()
    if env_single:
        return [env_single]
    return []


def _resolve_device_fingerprints(args: argparse.Namespace, count: int) -> List[str]:
    if args.device_fingerprints:
        fps = _split_list(args.device_fingerprints)
    elif args.device_fingerprint:
        fps = [args.device_fingerprint.strip()]
    else:
        fps = _split_list(os.getenv("OPINION_DEVICE_FINGERPRINTS", ""))
        if not fps:
            env_single = os.getenv("OPINION_DEVICE_FINGERPRINT", "").strip()
            if env_single:
                fps = [env_single]
    if not fps:
        return []
    if len(fps) == 1:
        return fps * count
    if len(fps) != count:
        raise SystemExit("OPINION_DEVICE_FINGERPRINTS count must match private keys.")
    return fps


def _update_env_file(path: str, updates: Dict[str, str]) -> None:
    lines = []
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()

    out_lines = []
    seen = set()
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            out_lines.append(line)
            continue
        key, _, _ = line.partition("=")
        key = key.strip()
        if key in updates:
            out_lines.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out_lines.append(line)

    for key, val in updates.items():
        if key not in seen:
            out_lines.append(f"{key}={val}")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out_lines) + "\n")


def _request_token(
    address: str,
    privkey: str,
    device_fingerprint: str,
    chain_id: int,
    timeout_s: int,
) -> Dict[str, Any]:
    nonce = _generate_nonce()
    ts = int(time.time())
    issued_at = _now_iso(ts)
    siwe = _build_siwe_message(address, nonce, issued_at, chain_id)
    msg = encode_defunct(text=siwe)
    signed = Account.sign_message(msg, private_key=privkey)
    signature = signed.signature.hex()
    if signature.startswith("0x"):
        signature = signature[2:]

    payload = {
        "nonce": str(nonce),
        "timestamp": ts,
        "siwe_message": siwe,
        "sign": signature,
        "invite_code": "",
        "sources": "web",
        "sign_in_wallet_plugin": None,
    }
    headers = {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json",
        "referer": f"{DEFAULT_URI}/",
        "x-device-fingerprint": device_fingerprint,
        "x-device-kind": "web",
    }
    resp = requests.post(TOKEN_ENDPOINT, json=payload, headers=headers, timeout=timeout_s)
    resp.raise_for_status()
    data = resp.json()
    if data.get("errno") or data.get("errmsg"):
        raise RuntimeError(f"login failed: {data}")
    return data


def _status_payload(address: str, expire: int) -> Dict[str, Any]:
    return {
        "address": address,
        "expire": int(expire),
        "expires_in_seconds": int(expire - time.time()),
    }


def _build_env_updates(accounts: List[Dict[str, Any]]) -> Dict[str, str]:
    updates: Dict[str, str] = {}
    addresses = [acct["address"] for acct in accounts]
    updates["OPINION_FRONTEND_ADDRESSES"] = ",".join(addresses)
    tokens: List[str] = []
    expires: List[str] = []
    for idx, acct in enumerate(accounts):
        token = acct.get("token")
        expire = acct.get("expire")
        if not token or not expire:
            continue
        tokens.append(token)
        expires.append(str(int(expire)))
        address = acct["address"]
        suffix = address[2:].upper()
        updates[f"OPINION_FRONTEND_AUTH_{suffix}"] = token
        updates[f"OPINION_FRONTEND_TOKEN_EXPIRE_{suffix}"] = str(int(expire))
        updates[f"OPINION_DEVICE_FINGERPRINT_{suffix}"] = acct["device_fingerprint"]
        if idx == 0:
            updates["OPINION_DEVICE_FINGERPRINT"] = acct["device_fingerprint"]
    if tokens:
        updates["OPINION_FRONTEND_AUTH"] = ",".join(tokens)
    if expires:
        updates["OPINION_FRONTEND_TOKEN_EXPIRE"] = ",".join(expires)
    return updates


def _refresh_account(
    account: Dict[str, Any],
    chain_id: int,
    timeout_s: int,
    refresh_before: int,
) -> Dict[str, Any]:
    data = _request_token(
        address=account["address"],
        privkey=account["privkey"],
        device_fingerprint=account["device_fingerprint"],
        chain_id=chain_id,
        timeout_s=timeout_s,
    )
    result = data.get("result") or {}
    token = result.get("token")
    if not token:
        raise RuntimeError(f"token missing in response: {data}")
    expire = result.get("expire") or _decode_jwt_exp(token)
    if not expire:
        raise RuntimeError("token expire not found")
    expire = int(expire)
    account["token"] = token
    account["expire"] = expire
    account["next_refresh_at"] = expire - refresh_before
    return _status_payload(account["address"], expire)


def main() -> int:
    ap = argparse.ArgumentParser(description="Auto-login Opinion frontend and refresh Bearer token.")
    ap.add_argument("--env", default=".env")
    ap.add_argument("--once", action="store_true", help="Login once and exit.")
    ap.add_argument("--refresh-before", type=int, default=DEFAULT_REFRESH_BEFORE)
    ap.add_argument("--min-sleep", type=int, default=DEFAULT_MIN_SLEEP)
    ap.add_argument("--device-fingerprint", default=None)
    ap.add_argument("--device-fingerprints", default=None, help="Comma-separated device fingerprints.")
    ap.add_argument("--private-key", default=None)
    ap.add_argument("--private-keys", default=None, help="Comma-separated private keys.")
    ap.add_argument("--address", default=None)
    ap.add_argument("--addresses", default=None, help="Comma-separated addresses to validate.")
    ap.add_argument("--chain-id", type=int, default=DEFAULT_CHAIN_ID)
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    args = ap.parse_args()

    load_dotenv(args.env)

    privkey_raw_list = _resolve_private_keys(args)
    if not privkey_raw_list:
        raise SystemExit("Missing OPINION_PRIVATE_KEY(S) (set in .env or use --private-key/--private-keys).")
    privkeys = []
    for idx, key in enumerate(privkey_raw_list, start=1):
        privkeys.append(_normalize_privkey(key, f"private key #{idx}"))

    address_list = _resolve_addresses(args)
    if address_list:
        address_list = [_checksum_address(addr) for addr in address_list]
    if len(privkeys) > 1 and len(address_list) == 1:
        print(
            "Ignoring OPINION_WALLET_ADDRESS because multiple private keys were provided.",
            file=sys.stderr,
        )
        address_list = []
    if address_list and len(address_list) not in (1, len(privkeys)):
        raise SystemExit("OPINION_WALLET_ADDRESSES count must match private keys.")

    device_fps = _resolve_device_fingerprints(args, len(privkeys))

    accounts: List[Dict[str, Any]] = []
    for idx, privkey in enumerate(privkeys):
        account = Account.from_key(privkey)
        address = _checksum_address(account.address)
        if address_list and len(address_list) == len(privkeys):
            if address_list[idx].lower() != address.lower():
                raise SystemExit(f"Provided address does not match private key: {address_list[idx]}")
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

    statuses = []
    for acct in accounts:
        statuses.append(
            _refresh_account(
                acct,
                chain_id=args.chain_id,
                timeout_s=args.timeout,
                refresh_before=args.refresh_before,
            )
        )
    _update_env_file(args.env, _build_env_updates(accounts))
    print(json.dumps(statuses, ensure_ascii=True))

    if args.once:
        return 0

    while True:
        now = time.time()
        due = [acct for acct in accounts if acct["next_refresh_at"] <= now]
        if not due:
            next_refresh = min(acct["next_refresh_at"] for acct in accounts)
            sleep_for = int(next_refresh - now)
            if sleep_for < args.min_sleep:
                sleep_for = args.min_sleep
            time.sleep(sleep_for)
            continue

        statuses = []
        for acct in due:
            statuses.append(
                _refresh_account(
                    acct,
                    chain_id=args.chain_id,
                    timeout_s=args.timeout,
                    refresh_before=args.refresh_before,
                )
            )
        _update_env_file(args.env, _build_env_updates(accounts))
        print(json.dumps(statuses, ensure_ascii=True))


if __name__ == "__main__":
    raise SystemExit(main())
