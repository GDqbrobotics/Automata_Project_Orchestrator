"""Declarative state-machine engine loaded from YAML config.

This engine is MQTT-agnostic. It handles:
  - State transitions triggered by events
  - Action execution after transitions
  - Event firing on transition completion (on_complete_event)

Events and actions support subsystem scoping:
  - event: measure_request         → internal event
  - event: robot_first/pick        → routed to robot_first subsystem
  - action: error                  → broadcast action
  - action: raman/measure_request  → subsystem-specific action

The engine fires events via a callback mechanism. Subsystem adapters
override the event callback to publish MQTT messages (boundary layer).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)


class StateMachineError(Exception):
    """Raised when an invalid state transition is attempted."""

    pass


@dataclass
class TransitionConfig:
    """Configuration for a single state transition."""
    event: str
    target: str
    actions: List[Any] = field(default_factory=list)


@dataclass
class StateConfig:
    """Configuration for a single state."""
    name: str
    description: str = ""
    transitions: List[TransitionConfig] = field(default_factory=list)
    # Per-state physical/role metadata (object.yaml `meta:`): where the
    # object is, which subsystem works on it, rotation requests, ...
    # The engine treats it as opaque; consumers (pipeline controller) read it.
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class StateMachineConfig:
    """Full state machine configuration."""
    name: str = ""
    initial_state: str = "idle"
    states: Dict[str, StateConfig] = field(default_factory=dict)
    # Top-level `metadata:` section (workflow lifecycle rules in
    # workflow.yaml, machine layout / task chains in object.yaml, sim
    # timings and reporting roles in subsystem YAMLs). Opaque to the
    # engine; consumers read the keys they know.
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_yaml(cls, config_path: str | Path) -> "StateMachineConfig":
        with open(config_path, "r") as f:
            data = yaml.safe_load(f)

        states: Dict[str, StateConfig] = {}
        for state_name, state_data in data.get("states", {}).items():
            transitions: List[TransitionConfig] = []
            for t in state_data.get("transitions", []):
                transitions.append(TransitionConfig(
                    event=t["event"],
                    target=t["target"],
                    actions=t.get("actions", []),
                ))
            states[state_name] = StateConfig(
                name=state_name,
                description=state_data.get("description", ""),
                transitions=transitions,
                meta=state_data.get("meta", {}) or {},
            )

        return cls(
            name=data.get("name", ""),
            initial_state=data.get("initial_state", "idle"),
            states=states,
            metadata=data.get("metadata", {}) or {},
        )

    @property
    def final_states(self) -> List[str]:
        """States with no outgoing transitions (the journey's endpoints).

        Used to derive the object workflow's final state(s) instead of
        hardcoding 'object_analyzed' (docs/CONFIG_CHANGE_GUIDE.md, R6).
        """
        return [s for s, c in self.states.items() if not c.transitions]

    def event_to(self, source: str, target: str) -> Optional[str]:
        """Return the event that leads from `source` to `target`.

        Like the engine's current-state helper, but for an explicit source
        state (the sim uses it to derive its self-init / completion events
        from the configured `ready` state instead of hardcoding them).
        """
        state = self.states.get(source)
        if state is None:
            return None
        for t in state.transitions:
            if t.target == target:
                return t.event
        return None

    def to_dict(self) -> Dict[str, Any]:
        """Serialize the state machine definition (for the frontend).

        Returns a plain dict describing the full graph (states, their
        descriptions and transitions) so the UI can render it without
        needing access to the YAML files.
        """
        return {
            "name": self.name,
            "initial_state": self.initial_state,
            "states": {
                state_name: {
                    "description": state.description,
                    "transitions": [
                        {"event": t.event, "target": t.target}
                        for t in state.transitions
                    ],
                }
                for state_name, state in self.states.items()
            },
            # The GUI derives the operator button matrix from the workflow
            # SM definition + this metadata (docs/CONFIG_CHANGE_GUIDE.md, F1).
            "metadata": self.metadata,
        }


def _parse_subsystem_prefix(name: str) -> tuple[str, str]:
    """Parse a scoped name like 'robot_first/measure_request'.

    Returns:
        (scope, local_name) - e.g. ("robot_first", "measure_request")
        If no '/' is found, scope is empty and local_name is the full name.
    """
    if "/" in name:
        parts = name.split("/", 1)
        return parts[0], parts[1]
    return "", name


class StateMachineEngine:
    """
    MQTT-agnostic state-machine engine.

    Events and actions support subsystem scoping via the '/' separator:
      - 'robot_first/measure_request' has scope='robot_first', name='measure_request'

    The engine fires events via a callback (_on_event_callback). Subsystem
    adapters wire this up to publish MQTT messages. The workflow and object
    state machines wire it up to propagate events internally.

    Actions are executed after a transition completes. Action handlers are
    registered via register_action(). Default handlers include:
      - log_event: log the action
      - (custom handlers registered by the orchestrator)
    """

    def __init__(
        self,
        config_path: str | Path,
        mqtt_client: Any = None,
        config_name: str | None = None,
    ) -> None:
        self.config_path = Path(config_path)
        self.mqtt_client = mqtt_client
        self.config_name = config_name or self.config_path.stem
        self.config: StateMachineConfig = StateMachineConfig.from_yaml(config_path)
        self.current_state: str = self.config.initial_state
        self._action_handlers: Dict[str, Callable] = {}
        self._on_event_callback: Optional[Callable[[str], None]] = None

        # Register default action handlers
        self.register_action("log_event", self._action_log_event)

        logger.info("State machine loaded: %s (initial: %s)",
                     self.config_name, self.current_state)

    def register_action(self, name: str, handler: Callable) -> None:
        """Register a custom action handler.

        Args:
            name: The action name as used in the YAML config.
            handler: A callable that receives (action_value, event, status).
        """
        self._action_handlers[name] = handler
        logger.debug("Registered action handler: %s", name)

    def set_on_event_callback(
        self,
        callback: Callable[[str], None],
    ) -> None:
        """Set callback for events fired on transition completion.

        When a transition completes and has an 'event' action, this callback
        is invoked with the event name. This is how state machines communicate
        across layers (e.g., object SM → orchestrator, subsystem SM → MQTT).

        Args:
            callback: Called with (event_name) when a transition completes
                      with an event action.
        """
        self._on_event_callback = callback
        logger.debug("Registered on_event callback for %s", self.config_name)

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------
    @property
    def state(self) -> str:
        return self.current_state

    def get_available_transitions(self) -> Dict[str, str]:
        """Return available transitions for the current state.

        Returns:
            Dict mapping event names to target state names.
        """
        current = self.current_state
        if current not in self.config.states:
            return {}
        transitions = self.config.states[current].transitions
        return {
            t.event: t.target
            for t in transitions
        }

    def event_to(self, target_state: str) -> Optional[str]:
        """Return the event that leads from the current state to ``target_state``.

        Used by the subsystem adapters to mirror the local state machine to a
        state reported over MQTT (the subsystem's real state is the source of
        truth). Returns ``None`` when no such transition is defined.
        """
        current = self.current_state
        if current not in self.config.states:
            return None
        for t in self.config.states[current].transitions:
            if t.target == target_state:
                return t.event
        return None

    def reset_to_initial(self) -> str:
        """Force the machine back to its configured initial state.

        Used when the orchestrator's view of a subsystem must be wiped
        (e.g. the simulated process behind it was stopped and the
        subsystem is expected to report its state again from scratch).
        This bypasses the transition graph on purpose: it is an operator-
        initiated reset of the orchestrator's local view, not a machine
        event.

        Returns the (new) current state.
        """
        previous = self.current_state
        self.current_state = self.config.initial_state
        if previous != self.current_state:
            logger.info(
                "State machine '%s': reset %s -> %s",
                self.config_name, previous, self.current_state,
            )
        return self.current_state

    # ------------------------------------------------------------------
    # Transition firing
    # ------------------------------------------------------------------
    def fire(self, event: str, status: Any = None) -> str:
        """
        Fire an event through the state machine.

        Args:
            event: The event name (optionally scoped: 'robot_first/pick').
            status: Optional status data passed to action handlers.

        Returns:
            The new state name.
        Raises:
            StateMachineError: If the transition is invalid.
        """
        current = self.current_state
        states = self.config.states

        if current not in states:
            raise StateMachineError(f"Unknown current state: {current}")

        transitions = states[current].transitions
        for transition in transitions:
            if transition.event == event:
                target = transition.target
                if target and target in states:
                    # Execute actions before transitioning
                    self._execute_actions(transition.actions, event, status)
                    self.current_state = target
                    logger.info(
                        "State machine '%s': %s -> %s (event: %s)",
                        self.config_name, current, target, event,
                    )
                    # Fire the on_complete event if defined
                    self._fire_event_actions(transition.actions, event, status)
                    return target
                else:
                    raise StateMachineError(f"Invalid target state: {target}")

        raise StateMachineError(
            f"No transition for event '{event}' in state '{current}'"
        )

    # ------------------------------------------------------------------
    # Action execution
    # ------------------------------------------------------------------
    def _execute_actions(
        self,
        actions: List[Any],
        event: str,
        status: Any = None,
    ) -> None:
        """Execute non-event actions during a transition."""
        for action in actions:
            if isinstance(action, str):
                handler = self._action_handlers.get(action)
                if handler:
                    try:
                        handler(None, event, status)
                    except Exception:
                        logger.exception("Action handler error: %s", action)
                else:
                    logger.warning("Unknown action: %s", action)
            elif isinstance(action, dict):
                for key, value in action.items():
                    # Skip 'event' keys - handled by _fire_event_actions
                    if key == "event":
                        continue
                    handler = self._action_handlers.get(key)
                    if handler:
                        try:
                            handler(value, event, status)
                        except Exception:
                            logger.exception(
                                "Action handler error: %s = %s", key, value,
                            )
                    else:
                        logger.warning("Unknown action: %s", key)

    def _fire_event_actions(
        self,
        actions: List[Any],
        event: str,
        status: Any = None,
    ) -> None:
        """Fire event actions (on_complete events) via the callback.

        Event actions are defined in the YAML as:
          actions:
            - event: some_event_name

        The callback receives the event name and handles cross-layer
        communication (internal routing or MQTT publishing).
        """
        for action in actions:
            if isinstance(action, dict) and "event" in action:
                event_name = action["event"]
                if event_name and self._on_event_callback:
                    logger.debug(
                        "Firing event '%s' from state machine '%s'",
                        event_name, self.config_name,
                    )
                    self._on_event_callback(event_name)

    # ------------------------------------------------------------------
    # Default action handlers
    # ------------------------------------------------------------------
    def _action_log_event(self, value: Any, event: str, status: Any = None) -> None:
        logger.info("Action: log_event (event=%s)", event)
