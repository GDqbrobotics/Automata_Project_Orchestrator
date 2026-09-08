"""Subsystem adapters – wrappers around MQTT communication with each subsystem.

Adapters are created dynamically from individual subsystem YAML files
(e.g., rotating_platform.yaml, vision.yaml). Each adapter owns its own
state machine engine that drives the subsystem's internal state transitions.

IMPORTANT: Subsystem SMs are the BOUNDARY LAYER between internal events
and external MQTT communication. The orchestrator wires up the boundary
callback that publishes MQTT messages when subsystem SMs fire events.

Subsystems communicate with each other ONLY through internal events
routed by the orchestrator – they never send MQTT to each other directly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from backend.domain.models import SubsystemConfig, SubsystemInfo, SubsystemState

logger = logging.getLogger(__name__)

# Completion reports flow subsystem -> orchestrator over the /status topics.
# When the local subsystem SM mirrors such a report it fires these events
# locally, and they must NOT be re-published as commands.
REPORT_EVENTS = frozenset({"complete", "init_complete"})


class SubsystemStateMachineError(Exception):
    """Raised when an invalid transition is attempted on a subsystem state machine."""
    pass


@dataclass
class SubsystemAdapter:
    """Generic adapter for a subsystem, created from config.

    Each adapter owns its own state machine engine that manages
    the subsystem's internal state (ready, running, paused, error, etc.).

    The state machine boundary callback (set by the orchestrator)
    publishes MQTT messages when the subsystem fires events.
    """

    name: str
    mqtt_client: Any
    display_name: str = ""
    subsystem_type: str = "generic"
    initial_state: str = "ready"
    required: bool = True
    metadata: Dict[str, Any] = field(default_factory=dict)
    state: SubsystemState = SubsystemState.READY
    error: Optional[str] = None
    state_machine: Any = None  # StateMachineEngine instance
    state_machine_path: Optional[str] = None
    # Optional observer for every MQTT publish the adapter performs on this
    # subsystem's topics (used by the orchestrator to feed the frontend's
    # message inspector). Called with (topic, payload).
    on_mqtt_publish: Optional[Callable[[str, Any], None]] = None

    @property
    def info(self) -> SubsystemInfo:
        return SubsystemInfo(
            name=self.name,
            display_name=self.display_name or self.name,
            type=self.subsystem_type,
            current_state=self.state_machine.current_state if self.state_machine else self.initial_state,
            state_description=self._get_state_description(),
            required=self.required,
            topic_prefix=f"automata/subsystem/{self.name}",
            metadata=self.metadata,
        )

    def _get_state_description(self) -> str:
        """Get the description for the current state from the state machine config."""
        if not self.state_machine or not self.state_machine.config:
            return ""
        current = self.state_machine.current_state
        if current in self.state_machine.config.states:
            return self.state_machine.config.states[current].description
        return ""

    @property
    def config(self) -> SubsystemConfig:
        return SubsystemConfig(
            name=self.name,
            display_name=self.display_name or self.name,
            type=self.subsystem_type,
            initial_state=self.initial_state,
            required=self.required,
            metadata=self.metadata,
            state_machine_path=self.state_machine_path,
        )

    def update_state(self, state: SubsystemState, error: Optional[str] = None) -> None:
        self.state = state
        if error:
            self.error = error

    def fire_transition(self, event: str) -> str:
        """Fire a transition on the subsystem's state machine (BOUNDARY).

        The subsystem SM is the boundary layer between internal events and
        external MQTT communication: every successful transition publishes
        the command to automata/subsystem/<name>/event, so the (simulated or
        real) subsystem receives it and acts on it. Completion reports
        (complete / init_complete) are excluded: they flow the other way,
        over the /status topic.

        Returns the new state name.
        Raises SubsystemStateMachineError if the transition is invalid.
        """
        if self.state_machine is None:
            raise SubsystemStateMachineError(f"No state machine for subsystem '{self.name}'")
        new_state = self.state_machine.fire(event, None)
        if event not in REPORT_EVENTS:
            topic = f"automata/subsystem/{self.name}/event"
            payload = {
                "event": event,
                "source": "orchestrator",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            self.mqtt_client.publish(topic, payload, qos=1)
            logger.info(
                "[Boundary] Subsystem '%s' command '%s' -> %s (MQTT '%s')",
                self.name, event, new_state, topic,
            )
            if self.on_mqtt_publish:
                self.on_mqtt_publish(topic, payload)
        return new_state

    def send_command(self, command: str, payload: Optional[Dict[str, Any]] = None) -> None:
        topic = f"automata/subsystem/{self.name}/command"
        self.mqtt_client.publish(topic, payload or {})
        if self.on_mqtt_publish:
            self.on_mqtt_publish(topic, payload or {})

    def is_ready(self) -> bool:
        return self.state == SubsystemState.READY


class SubsystemAdapterFactory:
    """Factory that creates SubsystemAdapter instances from config."""

    def __init__(
        self,
        mqtt_client: Any,
        subsystems_config: List[SubsystemConfig],
        config_dir: Optional[Path] = None,
    ) -> None:
        self.mqtt_client = mqtt_client
        self.subsystems_config = subsystems_config
        self.config_dir = config_dir or Path(__file__).resolve().parent.parent / "config" / "subsystems"
        self.adapters: Dict[str, SubsystemAdapter] = {}
        self._build_adapters()

    def _build_adapters(self) -> None:
        """Create an adapter for each configured subsystem, including its state machine."""
        for cfg in self.subsystems_config:
            # Determine the state machine YAML path
            state_machine_path = cfg.state_machine_path
            if state_machine_path is None:
                # Default: look for {name}.yaml in the subsystems config directory
                state_machine_path = str(self.config_dir / f"{cfg.name}.yaml")

            adapter = SubsystemAdapter(
                name=cfg.name,
                mqtt_client=self.mqtt_client,
                display_name=cfg.display_name,
                subsystem_type=cfg.type,
                initial_state=cfg.initial_state,
                required=cfg.required,
                metadata=cfg.metadata,
                state_machine_path=state_machine_path,
                # Until the first status report arrives over MQTT the
                # subsystem's real state is unknown – report it as offline
                # (the readiness gate must not pass on an unconfirmed state).
                state=SubsystemState.OFFLINE,
            )

            # Load the state machine if the YAML file exists
            sm_path = Path(state_machine_path)
            if sm_path.exists():
                from backend.state.state_machine import StateMachineEngine
                adapter.state_machine = StateMachineEngine(sm_path, mqtt_client=self.mqtt_client)
                logger.info(
                    "Adapter created: %s (%s) [required=%s, state_machine=%s]",
                    cfg.name, cfg.type, cfg.required, sm_path.name,
                )
            else:
                logger.warning(
                    "State machine YAML not found at %s for subsystem '%s'. "
                    "Adapter will use basic state tracking.",
                    state_machine_path, cfg.name,
                )

            self.adapters[cfg.name] = adapter

    def get(self, name: str) -> Optional[SubsystemAdapter]:
        return self.adapters.get(name)

    def get_all(self) -> Dict[str, SubsystemAdapter]:
        return self.adapters

    def get_required(self) -> List[SubsystemAdapter]:
        return [a for a in self.adapters.values() if a.required]

    def get_names(self) -> List[str]:
        return list(self.adapters.keys())

    def get_required_names(self) -> List[str]:
        return [a.name for a in self.adapters.values() if a.required]


# ------------------------------------------------------------------
# Legacy constants for backward compatibility (simulation_test.py)
# ------------------------------------------------------------------
ALL_SUBSYSTEMS: List[str] = [
    "vision",
    "robot_first",
    "robot_second",
    "rotating_platform",
    "xrf",
    "raman",
    "imaging_3di_hsi",
    "ai_analysis",
    "cloud_database",
]

# Aliases used in simulation_test.py
VISION = "vision"
ROBOT_FIRST = "robot_first"
ROBOT_SECOND = "robot_second"
ROTATING_PLATFORM = "rotating_platform"
XRF = "xrf"
RAMAN = "raman"
IMAGING_3DI_HSI = "imaging_3di_hsi"
AI_ANALYSIS = "ai_analysis"
CLOUD_DB = "cloud_database"
