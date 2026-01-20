import json
import os
from typing import Any, Dict


def load_state(path: str) -> Dict[str, Any]:
    if not path or not os.path.exists(path):
        return {"orders": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "orders" in data:
            return data
    except Exception:
        pass
    return {"orders": {}}


def save_state(path: str, state: Dict[str, Any]) -> None:
    if not path:
        return
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=True, indent=2)
        f.write("\n")
