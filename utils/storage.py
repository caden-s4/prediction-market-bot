"""
utils.storage – lightweight key-value state persistence.

Stores bot state (bankroll, open position IDs, etc.) between restarts
using a simple JSON file so no external DB is required for basic state.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_PATH = Path(__file__).parent.parent / "data" / "state.json"


class StateStore:
    """Simple JSON-backed key-value store for bot runtime state."""

    def __init__(self, path: Path = _DEFAULT_PATH) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict = self._load()

    def _load(self) -> dict:
        if self._path.exists():
            try:
                return json.loads(self._path.read_text())
            except Exception as exc:
                logger.warning("StateStore: could not load %s: %s", self._path, exc)
        return {}

    def _save(self) -> None:
        try:
            self._path.write_text(json.dumps(self._data, indent=2))
        except Exception as exc:
            logger.error("StateStore: save failed: %s", exc)

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value
        self._save()

    def delete(self, key: str) -> None:
        self._data.pop(key, None)
        self._save()

    def all(self) -> dict:
        return dict(self._data)
