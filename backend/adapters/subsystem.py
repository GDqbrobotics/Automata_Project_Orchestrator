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
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from backend.domain.models import (
    GlobalConfig,
    SubsystemConfig,
    SubsystemInfo,
    SubsystemState,
)
from backend.state.state_machine import StateMachineError

logger = logging.getLogger(__name__)


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

    The shared vocabulary (report events, non-work states, ready state)
    and the discrepancy timeout come from global.yaml (via GlobalConfig,
    docs/CONFIG_CHANGE_GUIDE.md R1); the MQTT topics come from the
    subsystem YAML's `mqtt_topic` field (R4).
    """

    name: str
    mqtt_client: Any
    display_name: str = ""
    subsystem_type: str = "generic"
    initial_state: str = "ready"
    required: bool = True
    metadata: Dict[str, Any] = field(default_factory=dict)
    # Per-subsystem MQTT topic template (subsystem YAML `mqtt_topic`).
    mqtt_topic: str = "automata/subsystem/{name}"
    # Shared vocabulary (global.yaml `states:`)
    ready_state: str = "ready"
    non_work_states: frozenset = field(
        default_factory=lambda: frozenset(
            {"initializing", "ready", "stopped", "error"}
        )
    )
    report_events: frozenset = field(
        default_factory=lambda: frozenset({"complete", "init_complete"})
    )
    # Discrepancy-check window (global.yaml orchestration.state_expect_timeout)
    state_expect_timeout: float = 10.0
    state: SubsystemState = SubsystemState.READY
    error: Optional[str] = None
    state_machine: Any = None  # StateMachineEngine instance
    state_machine_path: Optional[str] = None
    # Optional observer for every MQTT publish the adapter performs on this
    # subsystem's topics (used by the orchestrator to feed the frontend's
    # message inspector). Called with (topic, payload).
    on_mqtt_publish: Optional[Callable[[str, Any], None]] = None

    # ── Expected-state tracking (discrepancy check) ────────────────────
    # When a command is issued the orchestrator knows which state the
    # subsystem is expected to enter, but it does NOT advance the local SM:
    # the subsystem's real state (its MQTT status reports) is authoritative.
    # These fields track the pending expectation so a subsystem that never
    # reports the expected state can be flagged after state_expect_timeout
    # (global.yaml orchestration.state_expect_timeout).
    expected_state: Optional[str] = None       # state expected after last command
    expected_command: Optional[str] = None     # the command that set the expectation
    expected_pre_state: Optional[str] = None   # local SM state before the command
    expected_since: Optional[float] = None     # monotonic time the command was issued
    # Optional observer for a state discrepancy (set by the orchestrator).
    # Called with (name, expected_state, reported_state, command).
    on_state_discrepancy: Optional[Callable[[str, str, str, str], None]] = None
    _expected_reached: bool = field(default=False, repr=False)
    _expected_warned: bool = field(default=False, repr=False)
    _expected_timer: Optional[threading.Timer] = field(default=None, repr=False)
    _expected_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # ------------------------------------------------------------------
    # MQTT topics (from the per-YAML `mqtt_topic`, R4)
    # ------------------------------------------------------------------
    @property
    def topic_prefix(self) -> str:
        return self.mqtt_topic.replace("{name}", self.name)

    @property
    def event_topic(self) -> str:
        return self.topic_prefix + "/event"

    @property
    def command_topic(self) -> str:
        return self.topic_prefix + "/command"

    @property
    def status_topic(self) -> str:
        return self.topic_prefix + "/status"

    @property
    def response_topic(self) -> str:
        return self.topic_prefix + "/response"

    def tone(self, state: Optional[str]) -> str:
        """Visual tone for a raw subsystem state (F3)."""
        if state is None:
            return "offline"
        if state == "error":
            return "error"
        if state == "offline":
            return "offline"
        if state == self.ready_state:
            return "ready"
        if state in self.non_work_states:
            return "idle"
        return "busy"

    @property
    def info(self) -> SubsystemInfo:
        current = (
            self.state_machine.current_state
            if self.state_machine
            else self.initial_state
        )
        return SubsystemInfo(
            name=self.name,
            display_name=self.display_name or self.name,
            type=self.subsystem_type,
            current_state=current,
            state_description=self._get_state_description(),
            tone=self.tone(current),
            required=self.required,
            topic_prefix=self.topic_prefix,
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
            mqtt_topic=self.mqtt_topic,
            initial_state=self.initial_state,
            required=self.required,
            metadata=self.metadata,
            state_machine_path=self.state_machine_path,
        )

    def update_state(self, state: SubsystemState, error: Optional[str] = None) -> None:
        self.state = state
        if error:
            self.error = error

    def fire_transition(
        self, event: str, data: Optional[Dict[str, Any]] = None
    ) -> str:
        """Issue a command to the subsystem (BOUNDARY) without advancing the
        local state machine.

        The subsystem SM is the boundary layer between internal events and
        external MQTT communication: the command is published to
        automata/subsystem/<name>/event so the (simulated or real) subsystem
        receives it and acts on it. Completion reports (complete /
        init_complete) are excluded: they flow the other way, over the
        /status topic.

        IMPORTANT: the local state machine is NOT transitioned here. Real
        subsystems take time to react, so advancing the local SM on command
        would make it disagree with the state the subsystem actually reports
        over MQTT. Instead the local SM is a *mirror* of the subsystem's real
        state and is only advanced from its MQTT status reports (see
        :meth:`mirror_reported_state`). The state the subsystem is expected to
        enter is recorded for the discrepancy check (see
        ``state_expect_timeout``): if the subsystem never reports it, a
        warning is raised.

        Args:
            event: The command event.
            data: Optional context merged into the MQTT command payload.

        Returns the expected new state name (the command's target state).
        Raises SubsystemStateMachineError if there is no state machine, or
        StateMachineError if the event is not valid in the current state.
        """
        if self.state_machine is None:
            raise SubsystemStateMachineError(f"No state machine for subsystem '{self.name}'")
        sm = self.state_machine
        transitions = sm.get_available_transitions()
        if event not in transitions:
            raise StateMachineError(
                f"No transition for event '{event}' in state '{sm.current_state}'"
            )
        expected = transitions[event]
        if event not in self.report_events:
            # Outgoing command: record the expectation (discrepancy check) and
            # publish it. The local SM keeps its last reported state until the
            # subsystem confirms the new state over MQTT.
            self._record_expected(expected, event, sm.current_state)
            topic = self.event_topic
            payload = {
                "event": event,
                "source": "orchestrator",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            if data:
                payload.update(data)
            self.mqtt_client.publish(topic, payload, qos=1)
            logger.info(
                "[Boundary] Subsystem '%s' command '%s' -> expected '%s' (MQTT '%s')",
                self.name, event, expected, topic,
            )
            if self.on_mqtt_publish:
                self.on_mqtt_publish(topic, payload)
        return expected

    def mirror_reported_state(self, reported: str) -> Optional[str]:
        """Mirror the local state machine to a state reported over MQTT.

        The subsystem's real state (its MQTT status reports) is the source of
        truth. This advances the local SM to the reported state WITHOUT
        publishing any MQTT (the report already flowed subsystem ->
        orchestrator; re-publishing it as a command would loop). It also
        updates the expected-state discrepancy check (a report that matches
        the expectation, or that otherwise shows the subsystem reacted to the
        command, clears it).

        Returns the new local state, or ``None`` when the local SM could not
        be moved to the reported state.
        """
        if self.state_machine is None or not reported:
            return None
        sm = self.state_machine
        if reported == sm.current_state:
            self._note_report(reported)
            return reported
        event = sm.event_to(reported)
        if event is None:
            logger.warning(
                "Subsystem '%s': cannot mirror local SM to reported state '%s' "
                "(no transition from '%s' in its YAML)",
                self.name, reported, sm.current_state,
            )
        else:
            try:
                sm.fire(event)
            except StateMachineError as exc:
                logger.warning(
                    "Subsystem '%s': mirror to reported state '%s' failed: %s",
                    self.name, reported, exc,
                )
                return None
            logger.info(
                "Subsystem '%s': local SM mirrored to reported state '%s'",
                self.name, reported,
            )
        self._note_report(reported)
        return sm.current_state

    # ------------------------------------------------------------------
    # Expected-state tracking (discrepancy check)
    # ------------------------------------------------------------------
    def _record_expected(self, expected: str, command: str, pre_state: str) -> None:
        """Record the state the subsystem should enter after a command.

        (Re)arms the discrepancy check: a timer fires after
        state_expect_timeout and warns if the subsystem has not reported the
        expected state (or otherwise reacted to the command) by then.
        """
        with self._expected_lock:
            self.expected_state = expected
            self.expected_command = command
            self.expected_pre_state = pre_state
            self.expected_since = time.monotonic()
            self._expected_reached = False
            self._expected_warned = False
            self._reset_expected_timer_locked()

    def _reset_expected_timer_locked(self) -> None:
        if self._expected_timer is not None:
            self._expected_timer.cancel()
        self._expected_timer = threading.Timer(
            self.state_expect_timeout, self._check_expected
        )
        self._expected_timer.daemon = True
        self._expected_timer.start()

    def _clear_expected_locked(self) -> None:
        self.expected_state = None
        self.expected_command = None
        self.expected_pre_state = None
        self.expected_since = None
        self._expected_reached = False
        self._expected_warned = False
        if self._expected_timer is not None:
            self._expected_timer.cancel()
            self._expected_timer = None

    def _note_report(self, reported: str) -> None:
        """Update the discrepancy check with a state reported over MQTT."""
        with self._expected_lock:
            if self.expected_state is None:
                return
            reached = reported == self.expected_state or (
                self.expected_pre_state is not None and reported != self.expected_pre_state
            )
            if reached:
                self._clear_expected_locked()

    def reset_expected(self) -> None:
        """Drop any pending expectation (e.g. on reset / simulation toggle)."""
        with self._expected_lock:
            self._clear_expected_locked()

    def _check_expected(self) -> None:
        """Timer callback: warn if the expected state was never reported."""
        with self._expected_lock:
            if (
                self.expected_state is None
                or self._expected_reached
                or self._expected_warned
            ):
                return
            expected = self.expected_state
            command = self.expected_command or "?"
            self._expected_warned = True
        current = (
            self.state_machine.current_state if self.state_machine else None
        )
        message = (
            f"Subsystem '{self.name}': expected state '{expected}' after "
            f"command '{command}', but last reported state is '{current}' "
            f"after {self.state_expect_timeout:.0f}s"
        )
        logger.warning("%s (state discrepancy)", message)
        if self.on_state_discrepancy is not None:
            try:
                self.on_state_discrepancy(self.name, expected, current, command)
            except Exception:
                logger.exception(
                    "Subsystem '%s': state-discrepancy callback failed", self.name
                )

    def send_command(self, command: str, payload: Optional[Dict[str, Any]] = None) -> None:
        topic = self.command_topic
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
        global_config: Optional[GlobalConfig] = None,
    ) -> None:
        self.mqtt_client = mqtt_client
        self.subsystems_config = subsystems_config
        self.config_dir = config_dir or Path(__file__).resolve().parent.parent / "config" / "subsystems"
        # Shared vocabulary + discrepancy timeout (global.yaml, R1).
        self.global_config = global_config or GlobalConfig()
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
                mqtt_topic=cfg.mqtt_topic,
                ready_state=self.global_config.states_ready,
                non_work_states=self.global_config.non_work_states,
                report_events=self.global_config.report_events,
                state_expect_timeout=self.global_config.state_expect_timeout,
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
