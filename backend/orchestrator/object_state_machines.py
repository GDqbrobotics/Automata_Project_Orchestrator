"""Object state machine manager – per-object analysis workflows.

Each analyzed object gets its own state machine instance driven by the
object.yaml blueprint. Events from subsystem state machines trigger
transitions in the corresponding object state machine.

Object state is persisted to disk so it survives restarts.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml

from backend.state.state_machine import StateMachineEngine, StateMachineError, StateMachineConfig

logger = logging.getLogger(__name__)


class ObjectStateMachines:
    """Manages per-object state machine instances.

    Responsibilities:
    - Load the object.yaml blueprint
    - Create one state machine instance per object
    - Forward subsystem events to the correct object state machine
    - Persist object state to disk
    - Notify the orchestrator when object state changes
    """

    def __init__(
        self,
        config_path: str | Path,
        storage_dir: str | Path | None = None,
    ) -> None:
        self.config_path = Path(config_path)
        self.storage_dir = Path(storage_dir) if storage_dir else self._default_storage_dir()
        self.storage_dir.mkdir(parents=True, exist_ok=True)

        # Load the blueprint
        self.config: StateMachineConfig = StateMachineConfig.from_yaml(self.config_path)
        logger.info("Object workflow blueprint loaded: %s (initial: %s)",
                     self.config.name, self.config.initial_state)

        # Per-object state machines keyed by object_id
        self._machines: Dict[str, StateMachineEngine] = {}
        # Per-object operator-assigned alias IDs (local database)
        self._aliases: Dict[str, str] = {}
        # Per-object state descriptions keyed by object_id
        self._state_descriptions: Dict[str, str] = {}
        # Per-object timestamps
        self._timestamps: Dict[str, Dict[str, str]] = {}
        # Transition log per object
        self._transition_logs: Dict[str, List[Dict[str, Any]]] = {}

        # Callback for object state changes
        self._on_change: Optional[Callable[[str, str, str], None]] = None
        # Callback for events fired on transition completion
        self._on_event: Optional[Callable[[str, str], None]] = None
        # Lock for thread safety
        self._lock = threading.RLock()

        # Load persisted state from disk
        self._load_all_from_disk()

    def _default_storage_dir(self) -> Path:
        """Return default storage directory for object state files."""
        return Path(__file__).resolve().parent.parent / "object_states"

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------
    def on_change(self, callback: Callable[[str, str, str], None]) -> None:
        """Register a callback for object state changes.

        Args:
            callback: Called with (object_id, new_state, event) when an object transitions.
        """
        self._on_change = callback

    def on_event(self, callback: Callable[[str, str], None]) -> None:
        """Register a callback for events fired on transition completion.

        Called with (object_id, event_name) when a transition completes
        with an 'event' action. The orchestrator routes these events
        internally (scoped events go to specific subsystems, unscoped
        events broadcast to all).

        Args:
            callback: Called with (object_id, event_name).
        """
        self._on_event = callback

    # ------------------------------------------------------------------
    # Object lifecycle
    # ------------------------------------------------------------------
    def create_object(self, object_id: str) -> None:
        """Create a new object state machine instance.

        Args:
            object_id: Unique identifier for the object (e.g., "object_001").
        """
        with self._lock:
            if object_id in self._machines:
                logger.warning("Object '%s' already exists, reloading from disk", object_id)
                self._load_object_from_disk(object_id)
                return

            engine = StateMachineEngine(
                config_path=self.config_path,
                config_name=f"object:{object_id}",
            )
            # Wire up the on_event callback for internal event routing
            engine.set_on_event_callback(
                lambda event_name, oid=object_id: self._fire_event(oid, event_name),
            )
            self._machines[object_id] = engine
            self._aliases[object_id] = ""
            self._state_descriptions[object_id] = engine.config.states.get(
                engine.current_state, type('obj', (), {'description': ''})()
            ).description if engine.current_state in engine.config.states else ""
            self._timestamps[object_id] = {
                "created": _now_iso(),
                "current_state": _now_iso(),
            }
            self._transition_logs[object_id] = []
            logger.info("Object '%s' created in state '%s'", object_id, engine.current_state)

            # Persist to disk
            self._save_object_to_disk(object_id)

    def remove_object(self, object_id: str) -> None:
        """Remove an object state machine instance.

        Args:
            object_id: Unique identifier for the object.
        """
        with self._lock:
            if object_id in self._machines:
                del self._machines[object_id]
                self._aliases.pop(object_id, None)
                self._state_descriptions.pop(object_id, None)
                self._timestamps.pop(object_id, None)
                self._transition_logs.pop(object_id, None)
                # Remove persisted state file
                state_file = self._object_state_file(object_id)
                if state_file.exists():
                    state_file.unlink()
                logger.info("Object '%s' removed", object_id)

    def get_object_ids(self) -> List[str]:
        """Return list of all tracked object IDs."""
        with self._lock:
            return list(self._machines.keys())

    # ------------------------------------------------------------------
    # State queries
    # ------------------------------------------------------------------
    def get_object_state(self, object_id: str) -> Optional[str]:
        """Get the current state of an object."""
        with self._lock:
            machine = self._machines.get(object_id)
            if machine:
                return machine.current_state
            return None

    def get_object_state_description(self, object_id: str) -> str:
        """Get the description of an object's current state."""
        with self._lock:
            machine = self._machines.get(object_id)
            if machine and machine.current_state in machine.config.states:
                return machine.config.states[machine.current_state].description
            return ""

    def get_object_info(self, object_id: str) -> Optional[Dict[str, Any]]:
        """Get full info about an object including state, description, and timestamps."""
        with self._lock:
            machine = self._machines.get(object_id)
            if not machine:
                return None

            state_desc = self._state_descriptions.get(object_id, "")
            timestamps = self._timestamps.get(object_id, {})
            transitions = list(self._transition_logs.get(object_id, []))

            return {
                "object_id": object_id,
                "state": machine.current_state,
                "state_description": state_desc,
                "created": timestamps.get("created", ""),
                "last_state_change": timestamps.get("current_state", ""),
                "transitions": transitions,
                "alias": self._aliases.get(object_id, ""),
            }

    # ------------------------------------------------------------------
    # Alias IDs (operator-assigned, used by the local database)
    # ------------------------------------------------------------------
    def set_alias(self, object_id: str, alias: str) -> bool:
        """Set (or replace) the operator-assigned alias ID of an object.

        The machine keeps working with the auto-assigned object_id; the
        alias is what the local database uses for the object's record
        (data is stored locally and the database is updated manually).

        Args:
            object_id: The object to tag.
            alias: The operator-chosen alias (validated by the caller).

        Returns:
            True if the alias was stored, False if the object is unknown.
        """
        with self._lock:
            if object_id not in self._machines:
                return False
            self._aliases[object_id] = alias
            self._save_object_to_disk(object_id)
            return True

    def get_alias(self, object_id: str) -> str:
        """Return the alias ID of an object ("" if none assigned)."""
        with self._lock:
            return self._aliases.get(object_id, "")

    def get_all_objects_info(self) -> List[Dict[str, Any]]:
        """Get info about all tracked objects."""
        with self._lock:
            result = []
            for object_id in list(self._machines.keys()):
                info = self.get_object_info(object_id)
                if info:
                    result.append(info)
            return result

    def get_last_transition(self, object_id: str) -> Optional[Dict[str, Any]]:
        """Get the most recent transition record for an object.

        Returns a dict with keys from/to/event/timestamp, or None if the
        object has not transitioned yet (or is unknown).
        """
        with self._lock:
            logs = self._transition_logs.get(object_id, [])
            return dict(logs[-1]) if logs else None

    def get_available_transitions(self, object_id: str) -> Dict[str, str]:
        """Get available transitions for an object's state machine."""
        with self._lock:
            machine = self._machines.get(object_id)
            if machine:
                return machine.get_available_transitions()
            return {}

    # ------------------------------------------------------------------
    # Transition firing
    # ------------------------------------------------------------------
    def fire_transition(self, object_id: str, event: str) -> Optional[str]:
        """Fire a transition on an object's state machine.

        This is the main method called by the orchestrator when subsystem
        events indicate an object should advance to the next phase.

        Args:
            object_id: The object to transition.
            event: The event name (e.g., "object_placed_on_disk").

        Returns:
            The new state name, or None if the transition failed.
        """
        with self._lock:
            machine = self._machines.get(object_id)
            if not machine:
                logger.warning("Object '%s' not found for event '%s'", object_id, event)
                return None

            current_state = machine.current_state
            available = machine.get_available_transitions()

            if event not in available:
                logger.warning(
                    "No transition for event '%s' in object '%s' (state: %s, available: %s)",
                    event, object_id, current_state, list(available.keys()),
                )
                return None

            try:
                new_state = machine.fire(event)
                state_desc = machine.config.states.get(
                    new_state, type('obj', (), {'description': ''})()
                ).description if new_state in machine.config.states else ""

                # Record the transition
                transition_record = {
                    "from": current_state,
                    "to": new_state,
                    "event": event,
                    "timestamp": _now_iso(),
                }
                self._transition_logs.setdefault(object_id, []).append(transition_record)
                self._state_descriptions[object_id] = state_desc
                self._timestamps[object_id]["current_state"] = _now_iso()

                # Persist to disk
                self._save_object_to_disk(object_id)

                logger.info(
                    "Object '%s': %s -> %s (event: %s)",
                    object_id, current_state, new_state, event,
                )

                # Notify callback
                if self._on_change:
                    self._on_change(object_id, new_state, event)

                return new_state

            except StateMachineError as exc:
                logger.error("Failed to fire transition on object '%s': %s", object_id, exc)
                return None

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _object_state_file(self, object_id: str) -> Path:
        """Get the path to an object's state file."""
        return self.storage_dir / f"{object_id}.json"

    def _save_object_to_disk(self, object_id: str) -> None:
        """Save an object's state machine state to disk."""
        machine = self._machines.get(object_id)
        if not machine:
            return

        data = {
            "object_id": object_id,
            "state": machine.current_state,
            "state_description": self._state_descriptions.get(object_id, ""),
            "timestamps": self._timestamps.get(object_id, {}),
            "transitions": self._transition_logs.get(object_id, []),
            "alias": self._aliases.get(object_id, ""),
        }

        state_file = self._object_state_file(object_id)
        state_file.parent.mkdir(parents=True, exist_ok=True)
        with open(state_file, "w") as f:
            json.dump(data, f, indent=2)

    def _load_object_from_disk(self, object_id: str) -> None:
        """Load an object's state from disk and restore its state machine."""
        state_file = self._object_state_file(object_id)
        if not state_file.exists():
            logger.warning("State file not found for object '%s'", object_id)
            return

        try:
            with open(state_file, "r") as f:
                data = json.load(f)

            engine = StateMachineEngine(
                config_path=self.config_path,
                config_name=f"object:{object_id}",
            )
            # Wire up the on_event callback for internal event routing
            engine.set_on_event_callback(
                lambda event_name, oid=object_id: self._fire_event(oid, event_name),
            )
            # Restore the persisted state
            machine = engine
            machine.current_state = data.get("state", machine.config.initial_state)

            self._machines[object_id] = machine
            self._aliases[object_id] = data.get("alias", "")
            self._state_descriptions[object_id] = data.get("state_description", "")
            self._timestamps[object_id] = data.get("timestamps", {})
            self._transition_logs[object_id] = data.get("transitions", [])

            logger.info("Object '%s' restored from disk in state '%s'",
                        object_id, machine.current_state)
        except Exception as exc:
            logger.error("Failed to load object '%s' from disk: %s", object_id, exc)

    def _load_all_from_disk(self) -> None:
        """Load all persisted object states from disk."""
        if not self.storage_dir.exists():
            return

        for state_file in sorted(self.storage_dir.glob("*.json")):
            object_id = state_file.stem
            self._load_object_from_disk(object_id)

    # ------------------------------------------------------------------
    # Event firing
    # ------------------------------------------------------------------
    def _fire_event(self, object_id: str, event_name: str) -> None:
        """Fire an event from an object state machine transition.

        Called by the state machine engine when a transition completes
        with an 'event' action. The callback notifies the orchestrator
        so it can route the event internally (scoped events go to
        specific subsystems, unscoped events broadcast to all).

        Args:
            object_id: The object that fired the event.
            event_name: The event name (may be scoped like 'robot_first/pick').
        """
        if self._on_event:
            self._on_event(object_id, event_name)
        else:
            logger.warning(
                "No event callback registered for object '%s' event '%s'",
                object_id, event_name,
            )

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------
    def clear(self) -> None:
        """Remove all object state machines and their persisted state files."""
        with self._lock:
            self._machines.clear()
            self._aliases.clear()
            self._state_descriptions.clear()
            self._timestamps.clear()
            self._transition_logs.clear()
            # Also remove persisted files so stale state from previous runs
            # does not accumulate on disk (the orchestrator clears objects
            # on every start, so leftovers are never restored into a run).
            if self.storage_dir.exists():
                for state_file in self.storage_dir.glob("*.json"):
                    try:
                        state_file.unlink()
                    except OSError as exc:
                        logger.warning(
                            "Could not remove persisted state %s: %s",
                            state_file, exc,
                        )
            logger.info("All object state machines cleared")


# ------------------------------------------------------------------
# Utility
# ------------------------------------------------------------------
def _now_iso() -> str:
    """Return current UTC time as ISO format string."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
