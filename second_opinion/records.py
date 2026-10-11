"""Decision records: one JSON line per decision, append only.

A human ruling on a card appends a second line with the same id. Nothing is rewritten.
"""
from __future__ import annotations

import json
from collections import deque
from pathlib import Path
from typing import Any


class RecordStore:
    def __init__(self, path: Path | str | None, keep: int = 200):
        self.path = Path(path) if path else None
        self.recent: deque[dict[str, Any]] = deque(maxlen=keep)   # bounded, for the page
        self.written = 0
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, record: dict[str, Any]) -> None:
        """Raises OSError if the line cannot be written. Callers must not run the action then."""
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        if self.path:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        self.recent.append(record)
        self.written += 1
