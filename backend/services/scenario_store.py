"""Scenario persistence for stop/start recovery (R14).

When the operator enables *Keep object scenario*, stopping the workflow
writes a **scenario snapshot** — the pipeline bookkeeping (analysis order,
queue position, disk occupancy), the stored measurement points and the
workflow counters — next to the per-object state files the object state
machines already maintain (`backend/object_states/*.json`). Starting the
workflow again then restores the whole scenario instead of throwing the
objects away: the subsystems restart and re-initialize exactly as they
always do, but the run continues from where it stopped.

The operator's switch itself is persisted too (`settings.json`), so the
choice survives a restart of the GUI.

Files live in a dedicated directory (`backend/scenario_state/`,
gitignored). Writes are atomic (temp file + rename) like every other
persisted artifact in this project.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

SNAPSHOT_VERSION = 1


class ScenarioError(ValueError):
    """A saved scenario cannot be restored (stale/unsafe/invalid)."""


class ScenarioStore:
    """File-backed store for the scenario snapshot and the operator switch."""

    def __init__(self, dir_path: str | Path) -> None:
        self._dir = Path(dir_path)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._snapshot_path = self._dir / "scenario.json"
        self._settings_path = self._dir / "settings.json"

    # -- operator switch -------------------------------------------------

    def load_settings(self) -> bool:
        """Whether *Keep object scenario* is enabled (default: off)."""
        try:
            data = json.loads(self._settings_path.read_text())
            return bool(data.get("scenario_memory", False))
        except (OSError, ValueError, AttributeError):
            return False

    def save_settings(self, enabled: bool) -> None:
        self._atomic_write(self._settings_path, {"scenario_memory": bool(enabled)})

    # -- snapshot ----------------------------------------------------------

    def save(self, snapshot: Dict[str, Any]) -> None:
        """Persist the scenario snapshot (atomic)."""
        payload = {"version": SNAPSHOT_VERSION, **snapshot}
        self._atomic_write(self._snapshot_path, payload)
        logger.info(
            "[scenario] snapshot saved (%d objects, saved_at %s)",
            len((snapshot.get("objects") or [])),
            snapshot.get("saved_at"),
        )

    def load(self) -> Optional[Dict[str, Any]]:
        """The stored snapshot, or ``None`` when absent/corrupt/stale."""
        try:
            data = json.loads(self._snapshot_path.read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("version") != SNAPSHOT_VERSION:
            logger.warning("[scenario] ignoring an incompatible snapshot")
            return None
        return data

    def available(self) -> bool:
        """A restorable snapshot exists on disk."""
        return self._snapshot_path.exists()

    def clear(self) -> None:
        """Discard the snapshot (fresh start / reset / consumed)."""
        try:
            if self._snapshot_path.exists():
                self._snapshot_path.unlink()
                logger.info("[scenario] snapshot discarded")
        except OSError as exc:
            logger.warning("[scenario] could not discard the snapshot: %s", exc)

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _atomic_write(path: Path, payload: Dict[str, Any]) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, path)
