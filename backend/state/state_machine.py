"""Declarative state-machine engine loaded from YAML config."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

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
                transitions.append(TransitionConfig(
                    event=t["event"],
                    target=t["target"],
                    actions=t.get("actions", []),
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
    """

    def __init__(self, config_path: str | Path, mqtt_client: Any = None) -> None:
        self.config_path = Path(config_path)
        self.mqtt_client = mqtt_client
        self.config: StateMachineConfig = StateMachineConfig.from_yaml(config_path)
        self.current_state: str = self.config.initial_state
        logger.info("State machine loaded: %s (initial: %s)",
                     self.config.name, self.current_state)

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
                    # Execute actions
                    self._execute_actions(transition.actions, event, status)
                    self.current_state = target
                    logger.info("Transition: %s -> %s (event: %s)", current, target, event)
                    return target
                else:
                    raise StateMachineError(f"Invalid target state: {target}")

        raise StateMachineError(f"No transition for event '{event}' in state '{current}'")

    # ------------------------------------------------------------------
    # Action execution
    # ------------------------------------------------------------------
    def _execute_actions(self, actions: List[str], event: str, status: Any = None) -> None:
        for action in actions:
            if isinstance(action, str):
                if action == "log_event":
                    logger.info("Action: log_event (event=%s)", event)
                elif action == "update_operator_ui":
                    logger.info("Action: update_operator_ui")
            elif isinstance(action, dict):
                for key, value in action.items():
                    if key == "publish_mqtt_message":
                        if self.mqtt_client:
                            self.mqtt_client.publish(f"automata/{value}", {"event": value})
