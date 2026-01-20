import json
import os
from typing import Any, Dict


def load_config(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _env(name: str, default: str = "") -> str:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip()


def resolve_secret(cfg: Dict[str, Any], key: str, env_key: str, default: str = "") -> str:
    if key in cfg and cfg.get(key):
        return str(cfg.get(key)).strip()
    if env_key:
        return _env(env_key, default)
    return default
