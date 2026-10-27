"""
Workflow orchestrator – the heart of the GUI.

Architecture:
  ┌─────────────────────────────────────────────────────────┐
  │  Orchestrator (internal event bus)                       │
  │  ┌──────────┐  ┌──────────┐  ┌──────────────────────┐  │
  │  │Workflow SM│──│Object SM │──│ Internal Events      │  │
  │  │(workflow) │  │(objects) │  │ "robot_first/pick"   │  │
  │  └──────────┘  └──────────┘  └──────────────────────┘  │
  └──────────────────────────┬──────────────────────────────┘
                             │ Internal Events
               ┌─────────────┴─────────────┐
               │  Subsystem Adapters       │
               │  ┌──────────────────┐     │
               │  │ Subsystem SM 1   │     │  ← Only layer with MQTT
               │  │ (robot_first)    │───┼──→ MQTT → real robot
               │  └──────────────────┘     │
               │  ┌──────────────────┐     │
               │  │ Subsystem SM 2   │     │
               │  │ (raman)          │───┼──→ MQTT → real raman
               │  └──────────────────┘     │
               └───────────────────────────┘

Rules:
  - Workflow, Object, and Subsystem interact ONLY internally (no MQTT)
  - Subsystem SMs are the BOUNDARY: they publish MQTT to real subsystems
  - Events in YAML can be scoped: "robot_first/measure_request"
  - Actions in YAML can be scoped: "raman/measure_request" sends to raman

Event scoping:
  - event: measure_request          → internal, handled by this engine
  - event: robot_first/pick         → routed to robot_first subsystem SM
  - action: error                   → broadcast to ALL subsystems
  - action: raman/measure_request   → send to raman subsystem only

Action scoping:
  - action: log_event               → internal logging
  - action: subsystem_command       → send MQTT command to subsystem
  - action: broadcast_error         → broadcast error to all subsystems
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import yaml

# Placeholder for the project root
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

from backend.adapters.subsystem import (
    SubsystemAdapter,
    SubsystemAdapterFactory,
    SubsystemState,
)
from backend.domain.models import (
    BackendEvent,
    EventPayload,
    GlobalConfig,
    ObjectInfo,
    SubsystemConfig,
    WorkflowCommand,
    WorkflowStatus,
)
from backend.messaging.message_format import MessageFormatSet
from backend.messaging.mqtt_client import MqttClient
from backend.services.config_service import ConfigError, ConfigService
from backend.services.frontend_server import FrontendServer
from backend.simulation.process_manager import SubsystemProcessManager
from backend.state.state_machine import StateMachineEngine, StateMachineError
from backend.orchestrator.object_state_machines import ObjectStateMachines
from backend.orchestrator.pipeline import PipelineController

logger = logging.getLogger(__name__)


def _parse_subsystem_prefix(name: str) -> Tuple[str, str]:
    """Parse a scoped name like 'robot_first/measure_request'.

    Returns:
        (scope, local_name) - e.g. ("robot_first", "measure_request")
        If no '/' is found, scope is empty and local_name is the full name.
    """
    if "/" in name:
        parts = name.split("/", 1)
        return parts[0], parts[1]
    return "", name


@dataclass
class WorkflowNames:
    """The workflow lifecycle vocabulary, loaded once from workflow.yaml
    (states + its `metadata:` section).

    Replaces the old WorkflowState enum: the orchestrator compares the raw
    state name from the engine against these YAML-derived values (R3), so
    renaming a state or event in workflow.yaml is a config-only change.
    """

    initial_state: str
    # operator button -> event (metadata.operator_events)
    operator_events: Dict[str, str] = field(default_factory=dict)
    # fired when leaving the stop state (metadata.stop_completion_event)
    stop_completion_event: str = ""
    # readiness gate (metadata.readiness)
    readiness_in_state: str = ""
    readiness_fire: str = ""
    # event fired when a *required* subsystem reports error (metadata.error_event)
    error_event: str = ""
    # state that represents a workflow error (metadata.error_state)
    error_state: str = ""
    # simulation toggles are accepted only in these states (metadata.toggle_allowed_in)
    toggle_allowed_in: List[str] = field(default_factory=list)
    # subsystem state woken to `wake_to_state` on start (metadata.wake)
    wake_from_state: str = ""
    wake_to_state: str = ""
    # derived: the state the run is in once the readiness gate fired
    run_state: str = ""
    # derived: the stop state (target of the stop event from the run state)
    stop_state: str = ""
    # derived: states in which the pipeline suspends dispatch
    suspend_states: Set[str] = field(default_factory=set)

    @classmethod
    def from_config(cls, config: Any) -> "WorkflowNames":
        md = config.metadata or {}
        op = md.get("operator_events") or {}
        readiness = md.get("readiness") or {}
        wake = md.get("wake") or {}

        # run state = target of the readiness event from the readiness state
        run_state = ""
        in_state = readiness.get("in_state", "")
        fire = readiness.get("fire", "")
        if in_state in config.states and fire:
            for t in config.states[in_state].transitions:
                if t.event == fire:
                    run_state = t.target
                    break

        # stop state = target of the stop operator event from the run state
        stop_state = ""
        stop_event = op.get("stop", "")
        if run_state and stop_event and run_state in config.states:
            for t in config.states[run_state].transitions:
                if t.event == stop_event:
                    stop_state = t.target
                    break

        return cls(
            initial_state=config.initial_state,
            operator_events=dict(op),
            stop_completion_event=md.get("stop_completion_event", ""),
            readiness_in_state=in_state,
            readiness_fire=fire,
            error_event=md.get("error_event", ""),
            error_state=md.get("error_state", ""),
            toggle_allowed_in=list(md.get("toggle_allowed_in") or []),
            wake_from_state=wake.get("from_state", ""),
            wake_to_state=wake.get("to_state", ""),
            run_state=run_state,
            stop_state=stop_state,
            suspend_states={
                config.initial_state,
                stop_state,
                *config.final_states,
            } - {""},
        )


class WorkflowOrchestrator:
    """
    Central orchestrator that:
      1. Manages the workflow state machine (internal events only)
      2. Manages per-object state machines (internal events only)
      3. Creates and manages per-subsystem state machines (boundary layer)
      4. Routes internal events between layers
      5. Subsystem SMs publish MQTT to real hardware (boundary)
      6. Exposes state to the frontend via WebSocket
      7. Handles operator commands
    """

    def __init__(
        self,
        config_path: str | Path,
        subsystems_config_path: str | Path | None = None,
        global_config: GlobalConfig | None = None,
        use_simulation: bool = False,
        num_objects: int | None = None,
        debug_mode: bool = False,
    ) -> None:
        # Apply global config (CLI args may have overridden defaults)
        self._global_config = global_config or GlobalConfig()

        # MQTT client (used by subsystem adapters only)
        self.mqtt = MqttClient(
            broker=self._global_config.mqtt_broker,
            port=self._global_config.mqtt_port,
        )

        # ─── Workflow State Machine ───
        # No MQTT client passed – workflow SM only uses internal events
        self.state_machine = StateMachineEngine(
            config_path, mqtt_client=None,
        )
        # Wire up internal event routing for the workflow SM
        self.state_machine.set_on_event_callback(self._on_event)
        # Lifecycle vocabulary from workflow.yaml (states + metadata, R3)
        self.workflow_names = WorkflowNames.from_config(self.state_machine.config)

        # ─── Subsystem Configuration & Adapters ───
        self.subsystems_config: List[SubsystemConfig] = []
        self._load_subsystems_config(subsystems_config_path)

        # MQTT payload formats (mqtt_formats.yaml, R10): every command the
        # adapters publish and every report the orchestrator parses goes
        # through this single field-mapping layer.
        self.message_formats = MessageFormatSet.load(
            _PROJECT_ROOT / "backend" / "config"
        )
        self.adapter_factory = SubsystemAdapterFactory(
            self.mqtt,
            self.subsystems_config,
            config_dir=_PROJECT_ROOT / "backend" / "config" / "subsystems",
            global_config=self._global_config,
            message_formats=self.message_formats,
        )
        self.adapters: Dict[str, SubsystemAdapter] = self.adapter_factory.get_all()

        # Record every MQTT publish on the boundary for the frontend's
        # message inspector (subsystem detail modal).
        for _name, _adapter in self.adapters.items():
            _adapter.on_mqtt_publish = self._make_publish_recorder(_name)

        # A subsystem that is commanded into a state but never reports it
        # (checked after STATE_EXPECT_TIMEOUT) raises a discrepancy warning.
        for _adapter in self.adapters.values():
            _adapter.on_state_discrepancy = self._on_subsystem_state_mismatch

        # Note: the subsystem -> MQTT boundary is owned by the adapters
        # (SubsystemAdapter.fire_transition publishes every command to
        # automata/subsystem/<name>/event).

        # ─── Frontend Server ───
        self.frontend = FrontendServer(
            host=self._global_config.frontend_host,
            port=self._global_config.frontend_port,
        )

        # ─── Workflow Status ───
        self._status = WorkflowStatus(
            state=self.workflow_names.initial_state,
            total_objects=0,
            active_subsystems={
                name: adapter.info
                for name, adapter in self.adapters.items()
            },
        )
        self._total_objects = num_objects or self._global_config.simulation_default_num_objects
        self._progress = 0
        self._current_object: Optional[str] = None
        self._lock = threading.Lock()

        # ─── Object State Machines ───
        object_config_path = _PROJECT_ROOT / "backend" / "config" / "object.yaml"
        object_storage_dir = _PROJECT_ROOT / "backend" / "object_states"
        self.object_manager = ObjectStateMachines(
            config_path=object_config_path,
            storage_dir=object_storage_dir,
        )
        # Clear any persisted objects from previous runs
        self.object_manager.clear()
        # Wire up object SM callbacks
        self.object_manager.on_change(self._on_object_state_change)
        self.object_manager.on_event(self._on_object_event)

        # ─── MQTT Message Dispatcher ───
        self.mqtt._on_message = self._on_mqtt_message
        self._subsystem_callbacks: Dict[str, Callable] = {}

        # The response `type`s the orchestrator acts on, declared by the
        # subsystems' reporting roles (metadata.reports, R5) – e.g. the
        # box scanner's object list. Anything else is logged only.
        self._report_types: Set[str] = {
            rep.get("type")
            for adapter in self.adapters.values()
            for rep in (adapter.metadata.get("reports") or [])
            if rep.get("type")
        }

        # Object internal-event messages fired during the in-progress
        # transition. They are flushed by fire_object_transition() together
        # with the transition record, so the frontend shows the transition
        # first and then the events it fired (not interleaved out of order).
        self._object_event_buffer: Dict[str, List[Dict[str, Any]]] = {}

        # ─── Pipeline Controller ───
        # Orchestrates the object flow (PROJECT_CONTEXT workflow). It is
        # event-driven on subsystem status reports arriving over MQTT, so it
        # works identically with real subsystems and with the simulated
        # subsystem processes (backend/simulation/*_sim.py).
        self._pipeline = PipelineController(self)

        # ─── Simulation Mode ───
        # In simulation mode the subsystems behind the MQTT boundary are
        # simulated processes (backend/simulation/*_sim.py) owned by this
        # orchestrator: the operator can toggle each subsystem between
        # simulated and real from the GUI before starting the workflow
        # (default: every simmable subsystem is simulated). The
        # orchestrator code path is the same in both modes.
        self._use_simulation = use_simulation
        self._sim_procs: Optional[SubsystemProcessManager] = None
        self._simulated: Set[str] = set()
        if use_simulation:
            # Subsystems that report an object list counted from the box
            # scan (metadata.reports, R5) need --num-objects: the manager
            # passes it only to those (no "if name == vision" special case).
            reporters = {
                name
                for name, adapter in self.adapters.items()
                for rep in (adapter.metadata.get("reports") or [])
                if (rep.get("object_ids") or {}).get("count_source") == "num_objects"
            }
            self._sim_procs = SubsystemProcessManager(
                # Follow the live MQTT client so the sim processes always
                # target the broker the orchestrator itself uses.
                endpoint=lambda: (self.mqtt.broker, self.mqtt.port),
                num_objects=self._total_objects,
                reporters=reporters,
            )
            self._simulated = set(
                self._sim_procs.available(list(self.adapters.keys()))
            )

        # ─── Command Handlers ───
        self.frontend.set_command_handler(WorkflowCommand.START_WORKFLOW, self._handle_start)
        self.frontend.set_command_handler(WorkflowCommand.PAUSE_WORKFLOW, self._handle_pause)
        self.frontend.set_command_handler(WorkflowCommand.RESUME_WORKFLOW, self._handle_resume)
        self.frontend.set_command_handler(WorkflowCommand.STOP_WORKFLOW, self._handle_stop)
        self.frontend.set_command_handler(WorkflowCommand.REQUEST_STATUS, self._handle_status)
        self.frontend.set_command_handler(WorkflowCommand.RESET_WORKFLOW, self._handle_reset)
        self.frontend.set_command_handler(WorkflowCommand.ASSIGN_ALIAS, self._handle_assign_alias)
        self.frontend.set_command_handler(
            WorkflowCommand.TOGGLE_SUBSYSTEM_SIMULATION,
            self._handle_toggle_subsystem_simulation,
        )
        self.frontend.set_command_handler(
            WorkflowCommand.DEBUG_SEND_SUBSYSTEM_MESSAGE,
            self._handle_debug_send_subsystem_message,
        )
        # Configuration wizard (debug mode only): guided editing of the
        # YAML files under backend/config while the workflow is not running.
        self.frontend.set_command_handler(
            WorkflowCommand.GET_CONFIG, self._handle_get_config,
        )
        self.frontend.set_command_handler(
            WorkflowCommand.VALIDATE_CONFIG, self._handle_validate_config,
        )
        self.frontend.set_command_handler(
            WorkflowCommand.PATCH_CONFIG, self._handle_patch_config,
        )
        self.frontend.set_command_handler(
            WorkflowCommand.SAVE_CONFIG, self._handle_save_config,
        )
        self.frontend.set_command_handler(
            WorkflowCommand.CREATE_SUBSYSTEM, self._handle_create_subsystem,
        )
        self.frontend.set_command_handler(
            WorkflowCommand.DELETE_SUBSYSTEM, self._handle_delete_subsystem,
        )
        self.frontend.set_command_handler(
            WorkflowCommand.WRITE_SIMULATOR, self._handle_write_simulator,
        )
        self.frontend.set_command_handler(
            WorkflowCommand.RENDER_MESSAGE_FORMATS,
            self._handle_render_message_formats,
        )
        self.frontend.set_command_handler(
            WorkflowCommand.RENDER_STATE_MACHINE,
            self._handle_render_state_machine,
        )

        # ─── Configuration Wizard ───
        # Transport-agnostic service behind the commands above: snapshot,
        # validation, comment-preserving patches, save, add/remove
        # subsystem and MQTT-format rendering (backend/services/config_service.py).
        self.config_service = ConfigService(
            config_dir=_PROJECT_ROOT / "backend" / "config",
            simulation_dir=_PROJECT_ROOT / "backend" / "simulation",
            root_dir=_PROJECT_ROOT,
        )

        # ─── Debug Mode ───
        # When enabled (launch_gui.py --debug) the subsystem inspector
        # shows a selector of the MQTT topics a real subsystem is expected
        # to publish, and the operator can send one of them manually
        # (user-driven simulation; see _handle_debug_send_subsystem_message).
        self._debug_mode = debug_mode
    def _on_event(self, event_name: str, data: Optional[Dict[str, Any]] = None) -> None:
        """Route internal events fired by state machines.

        `data` (optional) is merged into the MQTT command payload when the
        event is routed to a subsystem.

        Events can be:
          - Unscoped: "error" → broadcast to all subsystems
          - Scoped: "robot_first/pick" → route to robot_first subsystem SM
          - Scoped with "workflow" prefix → internal-only, no MQTT, no subsystem broadcast
          - Workflow events: "subsystems_start" → handle workflow-level action

        The event flows:
          1. Workflow SM fires event → _on_event (this method)
          2. If scoped with 'workflow/' prefix → internal no-op (pipeline coordination)
          3. If scoped (has '/'), route to target subsystem SM
          4. If unscoped "error", broadcast to all subsystems
          5. Object SM fires event → _on_object_event (separate callback)
        """
        scope, local_event = _parse_subsystem_prefix(event_name)

        if scope == "workflow":
            # Internal pipeline coordination event — consumed by orchestrator,
            # never broadcast to subsystems or published via MQTT.
            logger.debug("Internal workflow event '%s' consumed (no-op)", event_name)
            return

        if scope == "all":
            # Raw hardware commands (global.yaml states.hardware_commands,
            # ordered [pause, resume]): forwarded to every subsystem over
            # MQTT without touching the local SMs (they do not model
            # pause; the subsystem freezes its work and keeps its state).
            if local_event in self._global_config.states_hardware_commands:
                logger.info("Broadcasting command 'all/%s' to all subsystems", local_event)
                self._broadcast_command_to_subsystems(local_event)
            else:
                # Special scope: broadcast to all subsystems
                logger.info("Broadcasting 'all/%s' to all subsystems", local_event)
                self._broadcast_event_to_subsystems(local_event)
        elif scope:
            # Scoped event: route to the specified subsystem
            logger.info(
                "Routing scoped event '%s' → subsystem '%s'",
                event_name, scope,
            )
            self._route_event_to_subsystem(scope, local_event, data=data)
        else:
            # Unscoped event: broadcast to all subsystems
            logger.info("Broadcasting unscoped event '%s' to all subsystems", event_name)
            self._broadcast_event_to_subsystems(event_name)

    def _route_event_to_subsystem(
        self,
        subsystem_name: str,
        event_name: str,
        data: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Route a scoped event to a specific subsystem (issue a command).

        The event is published to the subsystem over MQTT (the boundary).
        The orchestrator's local subsystem SM is NOT advanced: the
        subsystem's real state is authoritative and arrives as MQTT status
        reports, which mirror the local SM (see _update_subsystem_status).
        The state the subsystem is expected to enter is tracked so a slow or
        unresponsive subsystem is flagged (discrepancy check).

        Args:
            subsystem_name: The target subsystem.
            event_name: The command event to issue.
            data: Optional context merged into the MQTT command payload.
        """
        adapter = self.adapters.get(subsystem_name)
        if adapter is None:
            logger.warning("No adapter found for scoped event target '%s'", subsystem_name)
            return

        if adapter.state_machine is None:
            logger.warning("No state machine for subsystem '%s'", subsystem_name)
            return

        try:
            expected = adapter.fire_transition(event_name, data)
            logger.info(
                "Subsystem '%s' commanded '%s' -> expected '%s' (current: '%s')",
                subsystem_name, event_name, expected,
                adapter.state_machine.current_state,
            )
            # Keep the full-status snapshot fresh (it reflects the last
            # reported state; the card updates when the subsystem confirms).
            self._refresh_subsystem_info(subsystem_name)
        except StateMachineError as exc:
            logger.warning(
                "Subsystem '%s' rejected event '%s': %s",
                subsystem_name, event_name, exc,
            )

    def _broadcast_event_to_subsystems(self, event_name: str) -> None:
        """Broadcast an event to all subsystems.

        Args:
            event_name: The event name to broadcast.
        """
        for cfg in self.subsystems_config:
            adapter = self.adapters.get(cfg.name)
            if adapter and adapter.state_machine:
                try:
                    adapter.fire_transition(event_name)
                    self._refresh_subsystem_info(cfg.name)
                except StateMachineError:
                    # Some subsystems may not have a transition for this event
                    logger.debug(
                        "Subsystem '%s' has no transition for event '%s'",
                        cfg.name, event_name,
                    )

    def _broadcast_command_to_subsystems(self, command: str) -> None:
        """Send a raw command to every subsystem over MQTT.

        Used for hardware commands that the local subsystem SMs do not model
        (pause/resume): the subsystems receive them on their /event topic and
        handle them locally (e.g. freezing the ongoing work).
        """
        for name, adapter in self.adapters.items():
            topic = adapter.event_topic
            # Built through the subsystem's hardware-command format (R10).
            payload = adapter.message_format.hardware_command.build(command)
            self.mqtt.publish(topic, payload, qos=1)
            self._fire_subsystem_message(name, "sent", topic, payload)

    # ------------------------------------------------------------------
    # Object Event Handler
    # ------------------------------------------------------------------
    def _on_object_event(self, object_id: str, event_name: str) -> None:
        """Handle events fired by object state machine transitions.

        Object SM events are routed like other internal events:
          - workflow/ scope: pipeline coordination, consumed by the
            PipelineController (with the object id)
          - Scoped: "robot_first/pick" → route to robot_first subsystem
          - Unscoped: "error" → broadcast to all subsystems

        Args:
            object_id: The object that fired the event.
            event_name: The event name (may be scoped).
        """
        logger.info(
            "Object '%s' fired event '%s'",
            object_id, event_name,
        )
        scope, local_event = _parse_subsystem_prefix(event_name)

        # Record the internal message for the frontend inspector. It is
        # buffered and flushed by fire_object_transition() after the
        # transition record (see _object_event_buffer).
        target = (
            "pipeline (internal)"
            if scope == "workflow"
            else (scope if scope else "all subsystems (broadcast)")
        )
        self._object_event_buffer.setdefault(object_id, []).append({
            "object_id": object_id,
            "kind": "event",
            "event": event_name,
            "target": target,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        if scope == "workflow":
            # Internal pipeline coordination – consumed by the controller.
            # NOTE: this runs while the orchestrator lock is held (object
            # transition path); the controller only sets flags here.
            self._pipeline.on_object_workflow_event(object_id, local_event)
            return
        # Reuse the same routing logic as workflow events
        self._on_event(event_name)

    # ------------------------------------------------------------------
    # Operator Command Handlers
    # ------------------------------------------------------------------
    def _check_readiness(self, source: str) -> None:
        """The readiness gate (workflow.yaml metadata.readiness), in one
        place: while the workflow waits in the readiness state, the moment
        all *required* subsystems report the ready state, the readiness
        event is fired (starting -> running)."""
        wf = self.workflow_names
        if self.state_machine.state != wf.readiness_in_state:
            return
        required_adapters = self.adapter_factory.get_required()
        if all(a.state == SubsystemState.READY for a in required_adapters):
            logger.info("All required subsystems ready!")
            if self.fire_event(wf.readiness_fire):
                self._fire_frontend_event(
                    BackendEvent.WORKFLOW_STATE_CHANGED.value,
                    {"new_state": wf.run_state, "source": source},
                )

    def _handle_start(self, data: Dict[str, Any]) -> None:
        logger.info("Operator: start_workflow")
        wf = self.workflow_names
        # Simulation mode: make sure the simulated subsystem processes
        # that are toggled on are actually running (restart any that died).
        self._ensure_simulated_processes_running()
        # Always clear previous state before starting a new workflow.
        # This handles starting from stopped, error, or idle.
        self._reset_workflow_state()

        if not self.fire_event(wf.operator_events.get("start", "start_requested")):
            return  # error already logged by fire_event

        self._update_state(self.state_machine.state)

        # Subsystems left in the wake-from state after a previous run go
        # back to the wake-to state (the transition event is looked up in
        # the subsystem SM; the command is published over MQTT).
        if wf.wake_from_state and wf.wake_to_state:
            for name, adapter in self.adapters.items():
                sm = adapter.state_machine
                if sm is None or sm.current_state != wf.wake_from_state:
                    continue
                event = sm.config.event_to(wf.wake_from_state, wf.wake_to_state)
                if event and event not in adapter.report_events:
                    adapter.fire_transition(event)
                    self._refresh_subsystem_info(name)

        # If all required subsystems already report ready, enter running now;
        # otherwise the next status report over MQTT will trigger it.
        self._check_readiness("operator")

    def _handle_pause(self, data: Dict[str, Any]) -> None:
        logger.info("Operator: pause_workflow")
        wf = self.workflow_names
        # The workflow SM action 'all/pause' forwards a pause command to
        # every subsystem over MQTT (they freeze their work, keeping state).
        if not self.fire_event(wf.operator_events.get("pause", "pause_requested")):
            return  # error already logged by fire_event
        self._update_state(self.state_machine.state)

    def _handle_resume(self, data: Dict[str, Any]) -> None:
        logger.info("Operator: resume_workflow")
        wf = self.workflow_names
        # The workflow SM action 'all/resume' forwards a resume command to
        # every subsystem over MQTT.
        if not self.fire_event(wf.operator_events.get("resume", "resume_requested")):
            return  # error already logged by fire_event
        self._update_state(self.state_machine.state)

    def _handle_stop(self, data: Dict[str, Any]) -> None:
        logger.info("Operator: stop_workflow")
        wf = self.workflow_names
        # The workflow SM action 'all/stop' stops every subsystem over MQTT.
        # running → stopping
        if not self.fire_event(wf.operator_events.get("stop", "stop_requested")):
            return  # error already logged by fire_event
        # stopping → stopped (complete the transition immediately when the
        # operator initiates the stop; the pipeline controller only performs
        # the automatic stop sequence at the end of a successful run).
        # The stop state is derived in WorkflowNames (target of the stop
        # operator event fired from the run state).
        if wf.stop_state and self.state_machine.state == wf.stop_state and wf.stop_completion_event:
            self.fire_event(wf.stop_completion_event)
            self._update_state(self.state_machine.state)

    def _handle_reset(self, data: Dict[str, Any]) -> None:
        """Handle workflow reset (from error or stopped state back to idle).

        Resets the entire workflow: clears all object state machines,
        resets progress, and transitions the workflow state machine to idle.
        Stops any running simulation first to avoid race conditions.
        """
        logger.info("Operator: reset_workflow")
        wf = self.workflow_names
        self._reset_workflow_state()

        if not self.fire_event(wf.operator_events.get("reset", "reset_requested")):
            return  # error already logged by fire_event

        new_state = self.state_machine.state
        self._update_state(new_state)
        self._fire_frontend_event(
            BackendEvent.WORKFLOW_STATE_CHANGED.value,
            {"new_state": new_state, "source": "operator"},
        )
        self._fire_frontend_event(
            BackendEvent.OBJECT_REMOVED.value,
            {"message": "All objects cleared on workflow reset"},
        )

        # Only auto-start after reset if simulation is NOT enabled.
        # In simulation mode the operator must press Start to begin a new run.
        required_adapters = self.adapter_factory.get_required()
        all_ready = all(
            a.state == SubsystemState.READY for a in required_adapters
        )
        if all_ready and not self._use_simulation:
            self._start_workflow_immediately()
        else:
            logger.info(
                "Not all required subsystems ready after reset"
                if not all_ready
                else "Simulation mode: waiting for operator start after reset"
            )
            for name, adapter in self.adapters.items():
                self._status.active_subsystems[name] = adapter.info

    def _handle_status(self, data: Dict[str, Any]) -> None:
        logger.info("Operator: request_status")
        self.frontend.broadcast_status(self._status)

    # Alias IDs: the operator tags objects during the workflow; the alias
    # is the ID the local database uses for the object's record while the
    # machine keeps working with the auto-assigned object_id
    # (docs/PROJECT_CONTEXT.md). The local database is updated manually –
    # there is no cloud subsystem. The format rule comes from global.yaml
    # (orchestration.alias_pattern) and is broadcast to the GUI (F7).
    def _alias_pattern(self) -> "re.Pattern":
        return re.compile(self._global_config.alias_pattern)

    def _handle_assign_alias(self, data: Dict[str, Any]) -> None:
        """Handle operator alias-ID assignment for an object.

        Command payload: {command: assign_alias, object_id: str, alias: str}
        """
        object_id = str(data.get("object_id") or "").strip()
        alias = str(data.get("alias") or "").strip()

        if not object_id:
            self._fire_frontend_event(
                BackendEvent.ERROR_REPORTED.value,
                {"message": "Alias assignment: missing object_id"},
            )
            return
        if not alias:
            self._fire_frontend_event(
                BackendEvent.ERROR_REPORTED.value,
                {"message": f"Alias assignment: empty alias for '{object_id}'"},
            )
            return
        if not self._alias_pattern().match(alias):
            self._fire_frontend_event(
                BackendEvent.ERROR_REPORTED.value,
                {
                    "message": (
                        f"Alias '{alias}' invalid: must match "
                        f"{self._global_config.alias_pattern}"
                        " (configured in global.yaml)"
                    )
                },
            )
            return

        with self._lock:
            if self.object_manager.get_object_state(object_id) is None:
                self._fire_frontend_event(
                    BackendEvent.ERROR_REPORTED.value,
                    {"message": f"Alias assignment: unknown object '{object_id}'"},
                )
                return
            # Aliases become local database IDs: keep them unique (the
            # machine's own object_ids are unique by construction).
            alias_lc = alias.lower()
            for oid in self.object_manager.get_object_ids():
                if oid != object_id and self.object_manager.get_alias(oid).lower() == alias_lc:
                    self._fire_frontend_event(
                        BackendEvent.ERROR_REPORTED.value,
                        {"message": f"Alias '{alias}' is already assigned to {oid}"},
                    )
                    return
            self.object_manager.set_alias(object_id, alias)

        logger.info("Operator: assigned alias '%s' to %s", alias, object_id)
        self._fire_frontend_event(
            BackendEvent.OBJECT_ALIAS_ASSIGNED.value,
            {"object_id": object_id, "alias": alias, "source": "operator"},
        )
        self._broadcast_objects()

    # ------------------------------------------------------------------
    # Simulation mode (--simulation): per-subsystem simulation toggle
    # ------------------------------------------------------------------
    @property
    def simulation_info(self) -> Dict[str, Any]:
        """Simulation info for the frontend ({} when not in simulation mode)."""
        if self._sim_procs is None:
            return {}
        return {
            "enabled": True,
            "simulated": sorted(self._simulated),
            "available": sorted(self._sim_procs.available(list(self.adapters.keys()))),
        }

    def _ensure_simulated_processes_running(self) -> None:
        """(Re)start the simulated process of every toggled-on subsystem."""
        if self._sim_procs is None:
            return
        for name in sorted(self._simulated):
            if not self._sim_procs.is_running(name):
                logger.info("[sim] ensuring simulated process for '%s'", name)
                self._sim_procs.start(name)

    def _reset_subsystem_view(self, name: str) -> None:
        """Reset the orchestrator's local view of a subsystem.

        Used after toggling its simulation on or off: the state history of
        the previous (simulated) process is meaningless for a fresh start,
        so the local subsystem SM goes back to its initial state and the
        adapter waits for the first status report over MQTT (offline) –
        exactly like a not-yet-seen real subsystem.
        """
        adapter = self.adapters.get(name)
        if adapter is None:
            return
        if adapter.state_machine is not None:
            adapter.state_machine.reset_to_initial()
        adapter.reset_expected()
        adapter.update_state(SubsystemState.OFFLINE)
        self._refresh_subsystem_info(name)
        new_state = (
            adapter.state_machine.current_state
            if adapter.state_machine
            else adapter.initial_state
        )
        self._fire_frontend_event(
            BackendEvent.SUBSYSTEM_STATE_CHANGED.value,
            {
                "subsystem": name,
                "new_state": new_state,
                "state_description": adapter._get_state_description(),
                "tone": adapter.tone(new_state),
                "source": "simulation_toggle",
            },
        )

    def _handle_toggle_subsystem_simulation(self, data: Dict[str, Any]) -> None:
        """Toggle one subsystem between simulated and real (simulation mode).

        Command payload: {command, subsystem: str}

        Only accepted before the workflow starts (idle / stopped / error –
        the same states where the operator can press Start): a subsystem is
        a black box behind the MQTT boundary, and switching it in the middle
        of a run would leave the pipeline without a valid counterpart.
        """
        name = str(data.get("subsystem") or "").strip()

        if self._sim_procs is None:
            self._fire_frontend_event(
                BackendEvent.ERROR_REPORTED.value,
                {
                    "message": (
                        "Simulation toggles are only available when "
                        "launched with --simulation"
                    )
                },
            )
            return
        if self.state_machine.state not in self.workflow_names.toggle_allowed_in:
            self._fire_frontend_event(
                BackendEvent.ERROR_REPORTED.value,
                {
                    "message": (
                        "Subsystem simulation can only be changed before "
                        "starting the workflow"
                    )
                },
            )
            return
        if name not in self.adapters:
            self._fire_frontend_event(
                BackendEvent.ERROR_REPORTED.value,
                {"message": f"Simulation toggle: unknown subsystem '{name}'"},
            )
            return
        if not self._sim_procs.has_sim(name):
            self._fire_frontend_event(
                BackendEvent.ERROR_REPORTED.value,
                {
                    "message": (
                        f"Simulation toggle: no simulator available "
                        f"for '{name}'"
                    )
                },
            )
            return

        if name in self._simulated:
            # Simulated -> real: stop the process; the real subsystem is
            # then expected to report over MQTT like in normal mode.
            self._simulated.discard(name)
            self._sim_procs.stop(name)
            logger.info(
                "[sim] subsystem '%s' toggled OFF (real hardware expected)",
                name,
            )
        else:
            # Real -> simulated: (re)start the process; it self-initializes
            # (initializing -> ready) and reports over MQTT like hardware.
            self._simulated.add(name)
            self._sim_procs.start(name)
            logger.info(
                "[sim] subsystem '%s' toggled ON (simulated process started)",
                name,
            )

        self._reset_subsystem_view(name)
        # Propagate the new simulation set to all connected frontends.
        self.frontend.set_simulation_info(self.simulation_info)
        self._fire_frontend_event(
            BackendEvent.SUBSYSTEM_SIMULATION_TOGGLED.value,
            {"subsystem": name, "simulated": name in self._simulated},
        )
        self.frontend.broadcast_with_display_config(self._status)

    # ------------------------------------------------------------------
    # Debug mode (--debug): user-driven MQTT injection
    # ------------------------------------------------------------------
    def _build_debug_topics(self) -> Dict[str, List[Dict[str, Any]]]:
        """Describe the MQTT topics each real subsystem is expected to publish.

        Sent to the frontend (debug mode only) so the subsystem inspector can
        offer a selector of the outgoing topics with sensible default
        payloads. The orchestrator's receive path consumes /status (state
        reports, incl. heartbeats) and /response (e.g. the box scanner's
        object list), so those are the topics a real subsystem sends.

        Topics come from each subsystem's configured `mqtt_topic` (R4);
        the default response payload comes from the subsystem's declared
        reporting role (metadata.reports, R5). Each descriptor is tagged
        with its kind so the GUI derives its conveniences from data (F5).
        """
        topics: Dict[str, List[Dict[str, Any]]] = {}
        for name, adapter in self.adapters.items():
            current = (
                adapter.state_machine.current_state
                if adapter.state_machine
                else adapter.initial_state
            )
            # Topic descriptors carry the configured field names (R10) so
            # the GUI's state quick-pick edits the same fields the
            # orchestrator parses.
            status_fmt = adapter.message_format.status
            response_fmt = adapter.message_format.response
            entries: List[Dict[str, Any]] = [
                {
                    "topic": adapter.status_topic,
                    "kind": "status",
                    "state_field": status_fmt.state_field,
                    "ready_field": status_fmt.ready_field,
                    "description": (
                        "State report (published by the subsystem on every "
                        "state change + heartbeat)"
                    ),
                    "default_payload": status_fmt.build(
                        current, name, current == adapter.ready_state, False
                    ),
                },
                {
                    "topic": adapter.response_topic,
                    "kind": "response",
                    "description": "Response message",
                    # A subsystem that declares a reporting role (R5) gets a
                    # payload for its first report; every other response is
                    # just logged by the orchestrator.
                    "default_payload": (
                        self._report_payload(adapter)
                        if adapter.metadata.get("reports")
                        else response_fmt.build("ok")
                    ),
                },
            ]
            topics[name] = entries
        return topics

    def _report_payload(self, adapter: SubsystemAdapter) -> Dict[str, Any]:
        """Default payload for a declared report (metadata.reports[0]):
        the report type + object IDs rendered from the configured format."""
        rep = (adapter.metadata.get("reports") or [{}])[0]
        ids_cfg = rep.get("object_ids") or {}
        fmt = ids_cfg.get("format", self._global_config.simulation_object_id_format)
        count = (
            self._total_objects
            if ids_cfg.get("count_source") == "num_objects"
            else 0
        )
        objects = [fmt.format(i=i) for i in range(1, count + 1)]
        # Built through the subsystem's response format (R10).
        return adapter.message_format.response.build(
            rep.get("type", "ok"), objects, count
        )

    def _handle_debug_send_subsystem_message(self, data: Dict[str, Any]) -> None:
        """Debug mode: inject an MQTT message on a subsystem's outgoing topic.

        The operator picks one of the topics a real subsystem is expected to
        publish (see _build_debug_topics) and a payload; the orchestrator
        publishes it to the broker and feeds its own MQTT receive path with
        the same bytes, so it is processed exactly like a message coming
        from the real subsystem (status sync, pipeline, inspector feed).

        Command payload: {command, subsystem, topic, payload}
        """
        if not self._debug_mode:
            self._fire_frontend_event(
                BackendEvent.ERROR_REPORTED.value,
                {"message": "Debug mode is not enabled (start with --debug)"},
            )
            return

        name = str(data.get("subsystem") or "").strip()
        topic = str(data.get("topic") or "").strip()
        payload = data.get("payload")

        if name not in self.adapters:
            self._fire_frontend_event(
                BackendEvent.ERROR_REPORTED.value,
                {"message": f"Debug: unknown subsystem '{name}'"},
            )
            return
        allowed = (
            self.adapters[name].status_topic,
            self.adapters[name].response_topic,
        )
        if topic not in allowed:
            self._fire_frontend_event(
                BackendEvent.ERROR_REPORTED.value,
                {
                    "message": (
                        f"Debug: topic '{topic}' not allowed for '{name}' "
                        f"(expected one of: {', '.join(allowed)})"
                    )
                },
            )
            return
        if not isinstance(payload, dict):
            self._fire_frontend_event(
                BackendEvent.ERROR_REPORTED.value,
                {"message": "Debug: payload must be a JSON object"},
            )
            return

        logger.info("[debug] operator injects MQTT message on %s: %s", topic, payload)
        # Publish to the broker (visible to any other subscriber), then feed
        # the orchestrator's own receive path: the broker does not loop a
        # client's own publishes back to that client, so without this the
        # orchestrator would never process its injected message.
        self.mqtt.publish(topic, payload, qos=1)
        self._on_mqtt_message(topic, json.dumps(payload).encode())

    # ------------------------------------------------------------------
    # Configuration wizard (--debug): guided editing of backend/config
    # ------------------------------------------------------------------
    def _config_editing_blocked(self) -> Optional[str]:
        """The message to refuse a wizard operation with (None = allowed).

        Writes are only possible in debug mode and while the workflow is
        not running: a live run must never race a configuration change
        (state machines, subsystems and formats are loaded at startup; the
        wizard always ends with a restart notice).
        """
        if not self._debug_mode:
            return (
                "Configuration wizard is only available in debug mode "
                "(start with --debug)"
            )
        if self.state_machine.state not in self.workflow_names.toggle_allowed_in:
            return "Configuration can only be changed before starting the workflow"
        return None

    def _wizard_error(self, message: str) -> None:
        self._fire_frontend_event(
            BackendEvent.ERROR_REPORTED.value, {"message": message}
        )

    def _handle_get_config(self, data: Dict[str, Any]) -> None:
        """Wizard step 0: send the editable config snapshot (files + formats)."""
        blocked = self._config_editing_blocked()
        if blocked:
            self._wizard_error(blocked)
            return
        snapshot = self.config_service.snapshot()
        snapshot["editable"] = True
        snapshot["debug"] = True
        self._fire_frontend_event(BackendEvent.CONFIG_SNAPSHOT.value, snapshot)

    def _handle_validate_config(self, data: Dict[str, Any]) -> None:
        """Wizard: validate a candidate file without writing it."""
        blocked = self._config_editing_blocked()
        if blocked:
            self._wizard_error(blocked)
            return
        path = str(data.get("path") or "")
        content = data.get("content")
        if not path or not isinstance(content, str):
            self._wizard_error("Validation: 'path' and 'content' are required")
            return
        errors = self.config_service.validate_content(path, content)
        self._fire_frontend_event(
            BackendEvent.CONFIG_VALIDATED.value,
            {"path": path, "valid": not errors, "errors": errors},
        )

    def _handle_patch_config(self, data: Dict[str, Any]) -> None:
        """Wizard: apply structured edits to a buffer (no disk write)."""
        blocked = self._config_editing_blocked()
        if blocked:
            self._wizard_error(blocked)
            return
        path = str(data.get("path") or "")
        content = data.get("content")
        patches = data.get("patches") or []
        if not path or not isinstance(content, str):
            self._wizard_error("Patch: 'path' and 'content' are required")
            return
        try:
            result = self.config_service.patch_content(path, content, patches)
        except ConfigError as exc:
            self._wizard_error(f"Patch failed: {exc}")
            return
        self._fire_frontend_event(BackendEvent.CONFIG_PATCHED.value, result)

    def _handle_save_config(self, data: Dict[str, Any]) -> None:
        """Wizard: validate and write one edited config file."""
        blocked = self._config_editing_blocked()
        if blocked:
            self._wizard_error(blocked)
            return
        path = str(data.get("path") or "")
        content = data.get("content")
        if not path or not isinstance(content, str):
            self._wizard_error("Save: 'path' and 'content' are required")
            return
        try:
            result = self.config_service.save(path, content)
        except ConfigError as exc:
            self._wizard_error(f"Save failed for '{path}': {exc}")
            return
        result["restart_required"] = True
        self._fire_frontend_event(BackendEvent.CONFIG_SAVED.value, result)
        logger.info("[wizard] saved %s", path)

    def _handle_create_subsystem(self, data: Dict[str, Any]) -> None:
        """Wizard: stage a new subsystem YAML from a guided spec."""
        blocked = self._config_editing_blocked()
        if blocked:
            self._wizard_error(blocked)
            return
        spec = data.get("spec")
        if not isinstance(spec, dict):
            self._wizard_error("Create subsystem: missing 'spec'")
            return
        try:
            result = self.config_service.create_subsystem(spec)
        except ConfigError as exc:
            self._wizard_error(f"Create subsystem failed: {exc}")
            return
        result["created"] = True
        result["restart_required"] = True
        self._fire_frontend_event(BackendEvent.CONFIG_PATCHED.value, result)

    def _handle_delete_subsystem(self, data: Dict[str, Any]) -> None:
        """Wizard: delete a subsystem YAML (and optionally its simulator)."""
        blocked = self._config_editing_blocked()
        if blocked:
            self._wizard_error(blocked)
            return
        name = str(data.get("name") or "").strip()
        if not name:
            self._wizard_error("Delete subsystem: missing 'name'")
            return
        # A deleted subsystem must not keep publishing from a simulated
        # process; the orchestrator's in-memory view is rebuilt on restart.
        if self._sim_procs is not None and self._sim_procs.is_running(name):
            self._sim_procs.stop(name)
            self._simulated.discard(name)
        try:
            result = self.config_service.delete_subsystem(
                name,
                delete_simulator=bool(data.get("delete_simulator", True)),
            )
        except ConfigError as exc:
            self._wizard_error(f"Delete subsystem failed: {exc}")
            return
        result["restart_required"] = True
        self._fire_frontend_event(BackendEvent.SUBSYSTEM_DELETED.value, result)
        logger.info("[wizard] deleted subsystem %s", name)

    def _handle_write_simulator(self, data: Dict[str, Any]) -> None:
        """Wizard: write the one-line simulator script of a new subsystem."""
        blocked = self._config_editing_blocked()
        if blocked:
            self._wizard_error(blocked)
            return
        name = str(data.get("name") or "").strip()
        if not name:
            self._wizard_error("Simulator: missing 'name'")
            return
        try:
            result = self.config_service.write_simulator(name)
        except ConfigError as exc:
            self._wizard_error(f"Simulator creation failed: {exc}")
            return
        result["restart_required"] = True
        self._fire_frontend_event(BackendEvent.CONFIG_SAVED.value, result)

    def _handle_render_state_machine(self, data: Dict[str, Any]) -> None:
        """Wizard: rebuild a file's `states:` block from the structured editor.

        Payload: {command, path, content, states, initial_state?}. The raw
        text (header comments included) is preserved; only the state machine
        is re-serialized. Nothing is written: the wizard keeps the result as
        its unsaved buffer and saves it in the review step.
        """
        blocked = self._config_editing_blocked()
        if blocked:
            self._wizard_error(blocked)
            return
        path = str(data.get("path") or "")
        content = data.get("content")
        states = data.get("states")
        initial_state = data.get("initial_state")
        if not path or not isinstance(content, str) or not isinstance(states, dict):
            self._wizard_error(
                "State machine: 'path', 'content' and 'states' are required"
            )
            return
        if initial_state is not None and not isinstance(initial_state, str):
            self._wizard_error("State machine: 'initial_state' must be a string")
            return
        try:
            result = self.config_service.render_state_machine(
                path, content, states, initial_state
            )
        except ConfigError as exc:
            self._wizard_error(f"State machine failed: {exc}")
            return
        self._fire_frontend_event(BackendEvent.CONFIG_PATCHED.value, result)

    def _handle_render_message_formats(self, data: Dict[str, Any]) -> None:
        """Wizard: rebuild mqtt_formats.yaml from the structured form."""
        blocked = self._config_editing_blocked()
        if blocked:
            self._wizard_error(blocked)
            return
        formats = data.get("formats")
        if not isinstance(formats, dict):
            self._wizard_error("Message formats: missing 'formats'")
            return
        try:
            result = self.config_service.render_message_formats(formats)
        except ConfigError as exc:
            self._wizard_error(f"Message formats failed: {exc}")
            return
        self._fire_frontend_event(BackendEvent.CONFIG_PATCHED.value, result)

    # ------------------------------------------------------------------
    # Workflow Helpers
    # ------------------------------------------------------------------
    def _reset_workflow_state(self) -> None:
        """Reset all workflow state (objects, progress, etc.) before starting a new workflow.

        This is called when the operator starts a new workflow from any terminal
        state (stopped, error, idle). It clears all objects, resets progress,
        and broadcasts the cleanup to the frontend.
        """
        self.object_manager.clear()
        self._pipeline.reset()
        self._progress = 0
        self._current_object = None
        self._status.progress = 0
        self._status.current_object = None
        self._status.objects = []
        self._object_event_buffer.clear()
        # Drop any pending expected-state checks from the previous run so a
        # stale command cannot raise a spurious discrepancy warning.
        self._clear_all_subsystem_expected()
        # Clear the frontend message inspector history (subsystem + object)
        self._fire_frontend_event(BackendEvent.MESSAGES_CLEARED.value, {})
        logger.info("Workflow state reset: cleared objects and progress")

    def _clear_all_subsystem_expected(self) -> None:
        """Drop every pending expected-state check (cancel their timers)."""
        for _adapter in self.adapters.values():
            _adapter.reset_expected()

    def _start_workflow_immediately(self) -> None:
        """Start the workflow immediately from idle when all subsystems are ready."""
        wf = self.workflow_names
        try:
            new_state = self.state_machine.fire(
                wf.operator_events.get("start", "start_requested"), self._status
            )
            self._update_state(new_state)
            self._fire_frontend_event(
                BackendEvent.WORKFLOW_STATE_CHANGED.value,
                {"new_state": new_state, "source": "subsystems_ready"},
            )

            new_state = self.state_machine.fire(wf.readiness_fire, self._status)
            self._update_state(new_state)
            self._fire_frontend_event(
                BackendEvent.WORKFLOW_STATE_CHANGED.value,
                {"new_state": new_state, "source": "subsystems_ready"},
            )

            logger.info("Workflow started immediately (all subsystems ready)")
        except StateMachineError as exc:
            logger.error("Failed to start workflow immediately: %s", exc)

    # ------------------------------------------------------------------
    # Object State Machine Management
    # ------------------------------------------------------------------
    @property
    def _object_initial(self) -> Tuple[str, str]:
        """(initial state, its description) from object.yaml (R6)."""
        cfg = self.object_manager.config
        state = cfg.initial_state
        desc = cfg.states.get(state)
        return state, (desc.description if desc else "")

    def create_object(self, object_id: str) -> None:
        """Create a new object state machine instance."""
        with self._lock:
            self.object_manager.create_object(object_id)
        logger.info("Created object state machine: %s", object_id)

        state, description = self._object_initial
        self._fire_frontend_event(
            BackendEvent.OBJECT_ADDED.value,
            {
                "object_id": object_id,
                "state": state,
                "state_description": description,
            },
        )
        self._broadcast_objects()

    def remove_object(self, object_id: str) -> None:
        """Remove an object state machine instance."""
        with self._lock:
            self.object_manager.remove_object(object_id)
            self._object_event_buffer.pop(object_id, None)
        logger.info("Removed object state machine: %s", object_id)

        self._fire_frontend_event(
            BackendEvent.OBJECT_REMOVED.value,
            {"object_id": object_id},
        )
        self._broadcast_objects()

    def fire_object_transition(
        self,
        object_id: str,
        event: str,
    ) -> Optional[str]:
        """Fire a transition on an object's state machine."""
        with self._lock:
            new_state = self.object_manager.fire_transition(object_id, event)

        if new_state:
            self._fire_frontend_event(
                BackendEvent.OBJECT_STATE_CHANGED.value,
                {"object_id": object_id, "new_state": new_state},
            )
            # Object message inspector: the transition first, then the
            # internal events the transition fired (buffered by
            # _on_object_event while the engine ran its event actions).
            record = self.object_manager.get_last_transition(object_id)
            if record:
                self._fire_frontend_event(
                    BackendEvent.OBJECT_MESSAGE.value,
                    {
                        "object_id": object_id,
                        "kind": "transition",
                        "from": record.get("from"),
                        "to": record.get("to"),
                        "event": record.get("event"),
                        "timestamp": record.get("timestamp"),
                    },
                )
            for msg in self._object_event_buffer.pop(object_id, []):
                self._fire_frontend_event(BackendEvent.OBJECT_MESSAGE.value, msg)
            self._broadcast_objects()

            # Final states are the object.yaml states without outgoing
            # transitions (R6): no hardcoded 'object_analyzed'.
            if new_state in self.object_final_states:
                logger.info("Object '%s' analysis complete", object_id)
                analyzed = self._count_analyzed_objects()
                with self._lock:
                    self._progress = analyzed
                    # Keep _status in sync so full-status broadcasts do not
                    # overwrite the frontend progress with a stale value
                    self._status.progress = analyzed
                self._fire_frontend_event(
                    BackendEvent.PROGRESS_UPDATED.value,
                    {"progress": analyzed, "total": self._total_objects},
                )

        return new_state

    @property
    def object_final_states(self) -> List[str]:
        """The object journey's final states (object.yaml states without
        outgoing transitions, R6)."""
        return self.object_manager.config.final_states

    def _count_analyzed_objects(self) -> int:
        """Count objects that have reached a final state of the object
        journey.

        Progress must reflect *completed* objects, not the total number of
        known objects (which would make the UI show 100% prematurely).
        """
        final_states = self.object_final_states
        return sum(
            1
            for oid in self.object_manager.get_object_ids()
            if self.object_manager.get_object_state(oid) in final_states
        )

    def _on_object_state_change(
        self,
        object_id: str,
        new_state: str,
        event: str,
    ) -> None:
        """Callback for object state machine transitions."""
        logger.info(
            "Object '%s': %s (event: %s)",
            object_id, new_state, event,
        )

    # ------------------------------------------------------------------
    # MQTT Message Dispatcher
    # ------------------------------------------------------------------
    def _configured_topic_owner(self, topic: str) -> Optional[Tuple[str, str]]:
        """Route an incoming topic to (subsystem, kind) using the
        *configured* topics of each adapter (R4): a subsystem may live on
        any topic tree, not just the global prefix."""
        for name, adapter in self.adapters.items():
            if topic == adapter.status_topic:
                return name, "status"
            if topic == adapter.response_topic:
                return name, "response"
        return None

    def _on_mqtt_message(self, topic: str, payload: bytes) -> None:
        """Route incoming MQTT messages to appropriate handlers."""
        logger.info("[Orchestrator] MQTT message on topic=%s", topic)
        topic_str = topic
        try:
            data = json.loads(payload.decode())
        except (json.JSONDecodeError, AttributeError):
            data = payload.decode()

        logger.info("[Orchestrator] MQTT dispatch %s -> %s", topic_str, data)

        owner = self._configured_topic_owner(topic_str)
        if owner is not None:
            name, kind = owner
            # Record incoming subsystem traffic for the frontend message
            # inspector (status reports, responses, ...).
            self._fire_subsystem_message(name, "received", topic_str, data)

            # Match against registered subsystem callbacks
            for pattern, callback in self._subsystem_callbacks.items():
                import fnmatch
                if fnmatch.fnmatchcase(topic_str, pattern):
                    callback(topic_str, data)
                    return

            # Subsystem responses (e.g. the box scanner's object list after
            # a scan). The payload is decoded through the subsystem's
            # configured response format (R10); only the declared report
            # types are acted on (R5), every other response is logged.
            if kind == "response" and isinstance(data, dict):
                adapter = self.adapters.get(name)
                response_fmt = (
                    adapter.message_format.response
                    if adapter is not None
                    else MessageFormatSet().default.response
                )
                report_type, report_objects, _ = response_fmt.extract(data)
                if report_type in self._report_types:
                    logger.info(
                        "Report '%s' from subsystem: %s",
                        report_type, report_objects,
                    )
                    self._pipeline.on_objects_detected(report_objects)
                return

            # Status reports (state sync + pipeline).
            if kind == "status":
                self._update_subsystem_status(name, data)

    def register_subsystem_callback(
        self,
        topic_pattern: str,
        callback: Callable,
    ) -> None:
        """Register a callback for MQTT messages matching a topic pattern."""
        self._subsystem_callbacks[topic_pattern] = callback

    # ------------------------------------------------------------------
    # Subsystem Status Updates
    # ------------------------------------------------------------------
    def _refresh_subsystem_info(self, subsystem_name: str) -> None:
        """Keep the full-status snapshot of a subsystem up to date.

        _status.active_subsystems stores SubsystemInfo snapshots; every
        full status broadcast re-sends them to the frontend. They must be
        refreshed whenever the adapter's local state machine moves, or the
        UI would flash stale states (e.g. 'initializing' – a state the SM
        can never return to) on every broadcast.
        """
        adapter = self.adapters.get(subsystem_name)
        if adapter is not None:
            self._status.active_subsystems[subsystem_name] = adapter.info

    def _update_subsystem_status(
        self,
        subsystem_name: str,
        data: Dict[str, Any],
    ) -> None:
        if subsystem_name not in self.adapters:
            return
        adapter = self.adapters[subsystem_name]
        # The reported state field comes from the subsystem's status format
        # (mqtt_formats.yaml, R10).
        state_str = str(
            adapter.message_format.status.extract_state(data) or ""
        ).lower()
        try:
            state = SubsystemState(state_str)
        except ValueError:
            state = self._map_reported_state(state_str)

        was_state = (
            adapter.state_machine.current_state if adapter.state_machine else None
        )
        adapter.update_state(state)

        # Mirror the local subsystem SM to the reported state: the local SM
        # is the orchestrator's view of the subsystem and must stay in sync
        # so command gating works (e.g. the robot is 'complete' once it
        # reports ready).
        self._sync_subsystem_sm(subsystem_name, state_str, was_state)
        # Keep the full-status snapshot in sync with the local SM (it is
        # re-sent to the frontend on every status broadcast).
        self._refresh_subsystem_info(subsystem_name)

        self._fire_frontend_event(
            BackendEvent.SUBSYSTEM_STATUS_UPDATED.value,
            {
                "subsystem": subsystem_name,
                # Raw reported state (e.g. 'picking', 'rotating', ...).
                "state": state_str or state.value,
                "state_description": adapter._get_state_description(),
                # Visual tone from the configured vocabulary (F3): the UI
                # renders it as-is instead of enumerating state names.
                "tone": adapter.tone(state_str or state.value),
                "details": data,
            },
        )

        # Let the pipeline controller react to the report (it issues the
        # next commands of the object flow).
        self._pipeline.on_subsystem_status(subsystem_name, state_str, was_state)

        # A *required* subsystem reporting the error state drives the
        # workflow SM over its configured error event (the workflow.yaml
        # transition already models the move to the error state).
        self._on_required_subsystem_error(subsystem_name, state_str)

        # Readiness gate (starting → running), single implementation.
        self._check_readiness("subsystems_ready")

    def _on_required_subsystem_error(self, name: str, state_str: str) -> None:
        """A *required* subsystem reported the error state: fire the
        configured error event on the workflow SM, if the current state
        models it (it does from starting/running)."""
        adapter = self.adapters.get(name)
        if adapter is None or not adapter.required:
            return
        if state_str != "error":
            return
        event = self.workflow_names.error_event
        if not event or event not in self.state_machine.get_available_transitions():
            return
        self._status.error = f"Subsystem '{name}' reported an error"
        self._fire_frontend_event(
            BackendEvent.ERROR_REPORTED.value,
            {"message": f"Subsystem '{name}' reported an error"},
        )
        self.fire_event(event)

    @staticmethod
    def _map_reported_state(state_str: str) -> SubsystemState:
        """Map a reported subsystem state to the adapter state enum.

        Working states (picking, rotating, measuring, ...) count as busy:
        the subsystem is active but not ready.
        """
        if state_str == "stopped":
            return SubsystemState.STOPPED
        if state_str == "offline":
            return SubsystemState.OFFLINE
        if state_str == "error":
            return SubsystemState.ERROR
        return SubsystemState.BUSY

    def _sync_subsystem_sm(
        self,
        subsystem_name: str,
        reported: str,
        was_state: Optional[str],
    ) -> None:
        """Mirror the local subsystem SM to the state reported over MQTT.

        The subsystem's real state (its MQTT status reports) is the source of
        truth: the local SM follows it. The adapter performs the transition
        WITHOUT re-publishing MQTT (the report already flowed subsystem ->
        orchestrator), and updates the expected-state discrepancy check.
        """
        adapter = self.adapters.get(subsystem_name)
        if not adapter or not adapter.state_machine or not reported:
            return
        # No-op when already in the reported state (still updates the
        # expected-state check, e.g. a confirming heartbeat).
        adapter.mirror_reported_state(reported)

    def _on_subsystem_state_mismatch(
        self,
        subsystem_name: str,
        expected: str,
        reported: str,
        command: str,
    ) -> None:
        """A commanded state was never reported within the timeout.

        The subsystem's real state (over MQTT) lags the state it was told to
        enter: it may be slow, stuck, or not receiving commands. Surface it as
        a warning (log + frontend) without changing the workflow state.
        """
        message = (
            f"Subsystem '{subsystem_name}': expected state '{expected}' after "
            f"command '{command}', but last reported state is '{reported}' "
            "after the expected-state timeout"
        )
        logger.warning(message)
        self._fire_frontend_event(
            BackendEvent.SUBSYSTEM_STATE_MISMATCH.value,
            {
                "subsystem": subsystem_name,
                "expected_state": expected,
                "reported_state": reported,
                "command": command,
                "message": message,
            },
        )

    def subsystem_effective_state(self, subsystem_name: str) -> Optional[str]:
        """The state the pipeline should treat a subsystem as being in.

        If a command that would move the subsystem into a working state has
        been issued but not yet confirmed by a status report, that (expected)
        state is returned – the subsystem is in flight. Otherwise the last
        reported state is returned. This keeps physical gating correct even
        though the local SM no longer advances on command.
        """
        adapter = self.adapters.get(subsystem_name)
        if adapter is None or adapter.state_machine is None:
            return None
        expected = adapter.expected_state
        if expected is not None and expected not in adapter.non_work_states:
            return expected
        return adapter.state_machine.current_state

    # ------------------------------------------------------------------
    # Subsystem State Machine Transitions
    # ------------------------------------------------------------------
    def fire_subsystem_transition(
        self,
        subsystem_name: str,
        event: str,
    ) -> bool:
        """Issue a command to a subsystem (boundary layer).

        This is used by the pipeline controller and external callers. The
        command is published over MQTT; the local subsystem SM is NOT advanced
        (the subsystem's reported state is authoritative and mirrors it). The
        expected state is tracked for the discrepancy check.
        """
        adapter = self.adapters.get(subsystem_name)
        if adapter is None:
            logger.warning("No adapter found for subsystem '%s'", subsystem_name)
            return False

        if adapter.state_machine is None:
            logger.warning("No state machine for subsystem '%s'", subsystem_name)
            return False

        try:
            expected = adapter.fire_transition(event)  # boundary: publishes MQTT
            logger.info(
                "Subsystem '%s' commanded '%s' -> expected '%s' (current: '%s')",
                subsystem_name, event, expected, adapter.state_machine.current_state,
            )
            # Keep the full-status snapshot fresh; the card reflects the last
            # reported state and updates when the subsystem confirms.
            self._refresh_subsystem_info(subsystem_name)
            return True

        except Exception as exc:
            logger.error(
                "Failed to issue command '%s' on subsystem '%s': %s",
                event, subsystem_name, exc,
            )
            return False

    # ------------------------------------------------------------------
    # State Management
    # ------------------------------------------------------------------
    def _update_state(self, new_state: str) -> None:
        self._status.state = new_state
        self._fire_frontend_event(
            BackendEvent.WORKFLOW_STATE_CHANGED.value,
            {"new_state": new_state},
        )
        # Notify the pipeline controller (it starts/stops dispatching)
        self._pipeline.on_workflow_state(new_state)
        # Broadcast full status including objects for the frontend
        self.frontend.broadcast_with_display_config(self._status)

    def _fire_frontend_event(
        self,
        event_type: str,
        details: Dict[str, Any],
    ) -> None:
        payload = EventPayload(
            event_type=event_type,
            details=details,
        ).to_dict()
        self.frontend.broadcast(payload)

    def _make_publish_recorder(
        self, subsystem_name: str
    ) -> Callable[[str, Any], None]:
        """Build the on_mqtt_publish observer for a subsystem adapter.

        The adapter calls it for every MQTT publish on its topics
        (fire_transition commands, send_command), so this is the single
        choke point that records 'sent' traffic for the frontend.
        """
        def _record(topic: str, payload: Any) -> None:
            self._fire_subsystem_message(subsystem_name, "sent", topic, payload)

        return _record

    def _fire_subsystem_message(
        self,
        subsystem_name: str,
        direction: str,
        topic: str,
        payload: Any,
    ) -> None:
        """Broadcast one MQTT message of a subsystem to the frontend.

        direction is 'sent' (orchestrator -> subsystem) or 'received'
        (subsystem -> orchestrator).
        """
        self._fire_frontend_event(
            BackendEvent.SUBSYSTEM_MESSAGE.value,
            {
                "subsystem": subsystem_name,
                "direction": direction,
                "topic": topic,
                "payload": payload,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    def _get_status(self) -> WorkflowStatus:
        with self._lock:
            self._status.objects = [
                ObjectInfo(
                    object_id=obj["object_id"],
                    state=obj["state"],
                    state_description=obj.get("state_description", ""),
                    created=obj.get("created", ""),
                    last_state_change=obj.get("last_state_change", ""),
                    transitions=obj.get("transitions", []),
                    alias=obj.get("alias", ""),
                )
                for obj in self.object_manager.get_all_objects_info()
            ]
        return self._status


    def _broadcast_objects(self) -> None:
        """Broadcast the current list of objects to the frontend."""
        with self._lock:
            objects_info = self.object_manager.get_all_objects_info()
            # Keep _status in sync so broadcast_with_display_config sends
            # the correct objects list (not an empty array that would
            # overwrite the frontend's objects with []).
            initial_state, _ = self._object_initial
            self._status.objects = [
                ObjectInfo(
                    object_id=obj["object_id"],
                    state=obj.get("state") or initial_state,
                    state_description=obj.get("state_description", ""),
                    created=obj.get("created", ""),
                    last_state_change=obj.get("last_state_change", ""),
                    transitions=obj.get("transitions", []),
                    alias=obj.get("alias", ""),
                )
                for obj in objects_info
            ]

        self._fire_frontend_event(
            BackendEvent.WORKFLOW_STATE_CHANGED.value,
            {"objects": objects_info},
        )
        self.frontend.broadcast_with_display_config(self._status)

    # ------------------------------------------------------------------
    # Convenience Methods
    # ------------------------------------------------------------------
    def fire_event(self, event: str) -> bool:
        """Fire an internal event through the workflow state machine."""
        try:
            new_state = self.state_machine.fire(event, self._status)
            self._update_state(new_state)
            return True
        except StateMachineError as exc:
            logger.error("Event '%s' rejected: %s", event, exc)
            return False

    def set_total_objects(self, total: int) -> None:
        self._total_objects = total
        self._status.total_objects = total

    def set_current_object(self, object_id: Optional[str]) -> None:
        self._current_object = object_id
        self._status.current_object = object_id

    def set_subsystem_state(
        self,
        subsystem_name: str,
        state: SubsystemState,
        error: Optional[str] = None,
    ) -> None:
        """Directly set a subsystem's state (useful for testing)."""
        if subsystem_name not in self.adapters:
            return
        adapter = self.adapters[subsystem_name]
        adapter.update_state(state, error=error)
        logger.info("Subsystem %s set to %s", subsystem_name, state.value)

    def notify_subsystem_ready(self, subsystem_name: str) -> None:
        """Notify the frontend that a subsystem has reached ready state.

        Used by the simulator after directly manipulating subsystem state
        machines (e.g. during initialization), where the normal event-driven
        broadcast path is bypassed.
        """
        adapter = self.adapters.get(subsystem_name)
        if not adapter:
            return
        new_state = (
            adapter.state_machine.current_state
            if adapter.state_machine
            else adapter.initial_state
        )
        self._fire_frontend_event(
            BackendEvent.SUBSYSTEM_STATE_CHANGED.value,
            {
                "subsystem": subsystem_name,
                "new_state": new_state,
                "state_description": adapter._get_state_description(),
                "tone": adapter.tone(new_state),
                "source": "simulation_init",
            },
        )
        # Keep the status dict in sync so broadcast_with_display_config is correct
        self._refresh_subsystem_info(subsystem_name)
        logger.info("Subsystem '%s' notified as ready for frontend", subsystem_name)

    def update_all_subsystems_ready(self) -> None:
        """Mark all required subsystems as ready (and pass the readiness
        gate if the workflow is waiting for it)."""
        for adapter in self.adapter_factory.get_required():
            adapter.update_state(SubsystemState.READY)
        self._check_readiness("all_ready")

    # ------------------------------------------------------------------
    # Configuration Loading
    # ------------------------------------------------------------------
    def _load_subsystems_config(
        self,
        config_path: str | Path | None,
    ) -> None:
        """Load subsystem configuration from YAML files."""
        config_dir = Path(__file__).resolve().parent.parent / "config" / "subsystems"

        if config_path is not None:
            config_path = Path(config_path)
            if config_path.is_file():
                self._load_old_format(config_path)
                return
            elif config_path.is_dir():
                config_dir = config_path

        yaml_files = sorted(config_dir.glob("*.yaml"))
        if yaml_files:
            self._load_new_format(config_dir, yaml_files)
            return

        old_config_path = (
            Path(__file__).resolve().parent.parent.parent
            / "backend" / "config" / "subsystems.yaml"
        )
        if old_config_path.exists():
            logger.info("Falling back to legacy subsystems.yaml")
            self._load_old_format(old_config_path)
            return

        logger.warning("No subsystem configuration found")
        self.subsystems_config = []

    def _load_old_format(self, config_path: Path) -> None:
        """Load subsystem config from the old single-file format."""
        with open(config_path, "r") as f:
            data = yaml.safe_load(f)

        if not data or "subsystems" not in data:
            logger.warning("No subsystems section in config file")
            self.subsystems_config = []
            return

        self.subsystems_config = []
        config_dir = config_path.parent.parent / "config" / "subsystems"
        for item in data["subsystems"]:
            cfg = SubsystemConfig(
                name=item["name"],
                display_name=item.get("display_name", item["name"]),
                type=item.get("type", "generic"),
                mqtt_topic=item.get(
                    "mqtt_topic",
                    self._global_config.mqtt_topic_prefix + "/{name}",
                ),
                initial_state=item.get(
                    "initial_state", self._global_config.states_ready
                ),
                required=item.get("required", True),
                metadata=item.get("metadata", {}),
                state_machine_path=str(config_dir / f"{item['name']}.yaml"),
            )
            self.subsystems_config.append(cfg)

    def _load_new_format(
        self,
        config_dir: Path,
        yaml_files: List[Path],
    ) -> None:
        """Load subsystem config from individual YAML files."""
        self.subsystems_config = []
        for yaml_file in yaml_files:
            with open(yaml_file, "r") as f:
                data = yaml.safe_load(f)

            if not data or "name" not in data:
                logger.warning("Skipping invalid subsystem YAML: %s", yaml_file)
                continue

            cfg = SubsystemConfig(
                name=data["name"],
                display_name=data.get("display_name", data["name"]),
                type=data.get("type", "generic"),
                mqtt_topic=data.get(
                    "mqtt_topic",
                    self._global_config.mqtt_topic_prefix + "/{name}",
                ),
                initial_state=data.get(
                    "initial_state", self._global_config.states_ready
                ),
                required=data.get("required", True),
                metadata=data.get("metadata", {}),
                state_machine_path=str(yaml_file),
            )
            self.subsystems_config.append(cfg)

    @property
    def subsystem_names(self) -> List[str]:
        """Return the list of configured subsystem names."""
        return self.adapter_factory.get_names()

    @property
    def required_subsystem_names(self) -> List[str]:
        """Return the list of required subsystem names."""
        return self.adapter_factory.get_required_names()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Start the orchestrator (connect MQTT and start frontend server)."""
        logger.info("Starting workflow orchestrator...")
        logger.info("Configured subsystems: %d", len(self.subsystems_config))
        for cfg in self.subsystems_config:
            logger.info("  - %s (%s) [required=%s]", cfg.name, cfg.type, cfg.required)

        self.mqtt.connect()
        # Subscribe to every subsystem's *configured* topics (R4): the
        # per-YAML mqtt_topic is the single routing truth. The global
        # prefix wildcards stay as a convenience for traffic on the default
        # tree (the receive path routes only on the configured topics).
        for cfg in self.subsystems_config:
            self.mqtt.subscribe(cfg.status_topic, qos=1)
            self.mqtt.subscribe(cfg.response_topic, qos=1)
        prefix = self._global_config.mqtt_topic_prefix
        self.mqtt.subscribe(f"{prefix}/*/status", qos=1)
        self.mqtt.subscribe(f"{prefix}/*/response", qos=1)
        self.frontend.start()

        self.frontend.set_subsystem_config(
            [cfg.to_dict() for cfg in self.subsystems_config]
        )
        # Debug mode: let the subsystem inspector offer the outgoing-topic
        # selector (user-driven MQTT injection).
        if self._debug_mode:
            self.frontend.set_debug_topics(self._build_debug_topics())
            logger.info("Debug mode enabled: subsystem inspector offers manual MQTT injection")
        # Simulation mode: the orchestrator owns the simulated subsystem
        # processes. Start the ones toggled on (default: all simmable) and
        # advertise the per-subsystem toggle to the frontend.
        if self._sim_procs is not None:
            self.frontend.set_simulation_info(self.simulation_info)
            self._ensure_simulated_processes_running()
            logger.info(
                "Simulation mode: %d of %d subsystems simulated",
                len(self._simulated), len(self.adapters),
            )
        # (Re)connecting clients get a FRESH full status (built from the
        # live subsystem snapshots) instead of the cached last broadcast.
        self.frontend.set_status_callback(lambda: self._status)
        # State machine graph definitions for the frontend inspector modal
        # (subsystem SMs + object blueprint + workflow SM) — the same YAML
        # the engines run. The workflow definition carries the operator-
        # event map so the GUI derives its button matrix from it (F1).
        self.frontend.set_state_machine_definitions(
            {
                name: adapter.state_machine.config.to_dict()
                for name, adapter in self.adapters.items()
                if adapter.state_machine is not None
            },
            self.object_manager.config.to_dict(),
            self.state_machine.config.to_dict(),
        )
        # Send object workflow display config (state order, colors,
        # per-workflow-state messages, the object journey's final states
        # for the "done" styling (F4), and the shared vocabulary the UI
        # must not re-hardcode (ready state, alias pattern — F5/F7))
        self.frontend.set_workflow_display_config(
            self._global_config.object_state_order,
            self._global_config.object_state_colors,
            self._global_config.workflow_state_messages,
            final_states=self.object_manager.config.final_states,
            ready_state=self._global_config.states_ready,
            alias_pattern=self._global_config.alias_pattern,
        )

        # Full status broadcast (includes the SM definitions) so clients
        # that are connected right away get the complete picture.
        self.frontend.broadcast_with_display_config(self._status)
        logger.info("Workflow orchestrator started")

    def stop(self) -> None:
        """Stop the orchestrator cleanly."""
        logger.info("Stopping workflow orchestrator...")
        if self._sim_procs is not None:
            self._sim_procs.stop_all()
        # Cancel any pending expected-state timers so they do not fire
        # (and warn) after the orchestrator has shut down.
        self._clear_all_subsystem_expected()
        self.frontend.stop()
        self.mqtt.disconnect()
        logger.info("Workflow orchestrator stopped")

    def __enter__(self) -> "WorkflowOrchestrator":
        self.start()
        return self

    def __exit__(self, *args: Any) -> None:
        self.stop()
