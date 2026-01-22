import json
import os
import threading
import time
from typing import Any, Dict, Optional


class RawNetDumper:
    def __init__(self, *, enabled: bool, directory: str, max_chars: int = 200000) -> None:
        self.enabled = bool(enabled)
        self.directory = str(directory or "").strip()
        self.max_chars = int(max_chars) if max_chars is not None else 0
        self._lock = threading.Lock()
        self._counter = 0

    def dump(
        self,
        kind: str,
        *,
        meta: Optional[Dict[str, Any]] = None,
        payload: Any = None,
        raw_text: Optional[str] = None,
    ) -> None:
        if not self.enabled or not self.directory or not kind:
            return
        try:
            os.makedirs(self.directory, exist_ok=True)
        except Exception:
            return

        safe_kind = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in kind)
        ts = time.strftime("%Y%m%d_%H%M%S")
        with self._lock:
            self._counter += 1
            seq = self._counter
        filename = f"{ts}_{safe_kind}_{seq:04d}.json"
        path = os.path.join(self.directory, filename)

        record: Dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "kind": kind,
        }
        if meta:
            record["meta"] = meta
        if raw_text is not None:
            text = str(raw_text)
            if self.max_chars > 0 and len(text) > self.max_chars:
                record["raw_text"] = text[: self.max_chars]
                record["raw_truncated"] = True
            else:
                record["raw_text"] = text
        if payload is not None:
            record["payload"] = payload

        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(record, f, ensure_ascii=True, indent=2, default=str)
                f.write("\n")
        except Exception:
            return
