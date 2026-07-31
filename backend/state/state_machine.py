"""Declarative state-machine engine loaded from YAML config."""

from __future__ import annotations

import json
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
    actions: List[str] = field(default_factory=list)
    # Additional event to fire after transition (for cross-level communication)
    on_complete_event: Optional[str] = None


@dataclass
class StateConfig:
    """Configuration for a single state."""
    name: str
    description: str = ""
    transitions: List[TransitionConfig] = field(default_factory=list)


@dataclass
class StateMachineConfig:
    """Full state machine configuration."""
    name: str = ""
    initial_state: str = "idle"
    states: Dict[str, StateConfig] = field(default_factory=dict)

    @classmethod
    def from_yaml(cls, config_path: str | Path) -> "StateMachineConfig":
        with open(config_path, "r") as f:
            data = yaml.safe_load(f)

        states: Dict[str, StateConfig] = {}
        for state_name, state_data in data.get("states", {}).items():
            transitions: List[TransitionConfig] = []
            for t in state_data.get("transitions", []):
                actions = t.get("actions", [])
                on_complete_event = None
                # Check if any action is an event action: {event: <name>}
                for action in actions:
                    if isinstance(action, dict) and "event" in action:
                        on_complete_event = action["event"]
                        break
                transitions.append(TransitionConfig(
                    event=t["event"],
                    target=t["target"],
                    actions=actions,
                    on_complete_event=on_complete_event,
                ))
            states[state_name] = StateConfig(
                name=state_name,
                description=state_data.get("description", ""),
                transitions=transitions,
            )

        return cls(
            name=data.get("name", ""),
            initial_state=data.get("initial_state", "idle"),
            states=states,
        )


class StateMachineEngine:
    """
    State-machine engine that reads its configuration from a YAML file.

    Supports:
      - explicit states and transitions
      - event-driven transitions
      - action execution during transitions
      - custom action handlers for extensibility

    Actions supported by default:
      - log_event: log the transition event
      - update_operator_ui: notify UI update
      - publish_mqtt_message: publish to an MQTT topic

    Custom actions can be registered via register_action().
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
        self._on_complete_event: Optional[Callable[[str], None]] = None

        # Register default action handlers
        self.register_action("log_event", self._action_log_event)
        self.register_action("update_operator_ui", self._action_update_ui)
        self.register_action("publish_mqtt_message", self._action_publish_mqtt)

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

    def set_on_complete_event_callback(
        self,
        callback: Callable[[str], None],
    ) -> None:
        """Set callback for events fired on transition completion.

        When a transition completes and has an 'event' action, this callback
        is invoked with the event name. Used for cross-level state machine
        communication (e.g., object state machine → orchestrator).

        Args:
            callback: Called with (event_name) when a transition completes
                      with an event action.
        """
        self._on_complete_event = callback
        logger.debug("Registered on_complete_event callback for %s",
                     self.config_name)

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

    # ------------------------------------------------------------------
    # Transition firing
    # ------------------------------------------------------------------
    def fire(self, event: str, status: Any = None) -> str:
        """
        Fire an event through the state machine.

        Returns the new state name.
        Raises StateMachineError if the transition is invalid.
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
                    # Fire the on_complete_event if defined
                    if transition.on_complete_event and self._on_complete_event:
                        self._on_complete_event(transition.on_complete_event)
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
        actions: List[str],
        event: str,
        status: Any = None,
    ) -> None:
        for action in actions:
            if isinstance(action, str):
                # Simple string action - look up handler
                handler = self._action_handlers.get(action)
                if handler:
                    try:
                        handler(None, event, status)
                    except Exception:
                        logger.exception("Action handler error: %s", action)
                else:
                    logger.warning("Unknown action: %s", action)
            elif isinstance(action, dict):
                # Dict action: {action_name: value}
                for key, value in action.items():
                    # Skip 'event' actions - they are handled by on_complete_event
                    # callback after the transition, not during action execution
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

    # ------------------------------------------------------------------
    # Default action handlers
    # ------------------------------------------------------------------
    def _action_log_event(self, value: Any, event: str, status: Any = None) -> None:
        logger.info("Action: log_event (event=%s)", event)

    def _action_update_ui(self, value: Any, event: str, status: Any = None) -> None:
        logger.info("Action: update_operator_ui")

    def _action_publish_mqtt(self, value: Any, event: str, status: Any = None) -> None:
        if self.mqtt_client:
            self.mqtt_client.publish(f"automata/{value}", {"event": value})
