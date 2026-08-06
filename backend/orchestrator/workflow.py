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
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

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
    Command,
    EventPayload,
    GlobalConfig,
    ObjectInfo,
    SubsystemConfig,
    SubsystemInfo,
    WorkflowCommand,
    WorkflowState,
    WorkflowStatus,
)
from backend.messaging.mqtt_client import MqttClient, TOPIC_SUBSYSTEM
from backend.services.frontend_server import FrontendServer
from backend.state.state_machine import StateMachineEngine, StateMachineError
from backend.orchestrator.object_state_machines import ObjectStateMachines

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

        # ─── Subsystem Configuration & Adapters ───
        self.subsystems_config: List[SubsystemConfig] = []
        self._load_subsystems_config(subsystems_config_path)

        self.adapter_factory = SubsystemAdapterFactory(
            self.mqtt,
            self.subsystems_config,
            config_dir=_PROJECT_ROOT / "backend" / "config" / "subsystems",
        )
        self.adapters: Dict[str, SubsystemAdapter] = self.adapter_factory.get_all()

        # Wire up subsystem SM event callbacks to publish MQTT (boundary)
        self._setup_subsystem_event_callbacks()

        # ─── Frontend Server ───
        self.frontend = FrontendServer(
            host=self._global_config.frontend_host,
            port=self._global_config.frontend_port,
        )

        # ─── Workflow Status ───
        self._status = WorkflowStatus(
            state=WorkflowState.IDLE,
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

        # ─── Simulation Mode ───
        self._use_simulation = use_simulation
        self._simulator: Any = None

        # ─── Command Handlers ───
        self.frontend.set_command_handler(WorkflowCommand.START_WORKFLOW, self._handle_start)
        self.frontend.set_command_handler(WorkflowCommand.PAUSE_WORKFLOW, self._handle_pause)
        self.frontend.set_command_handler(WorkflowCommand.RESUME_WORKFLOW, self._handle_resume)
        self.frontend.set_command_handler(WorkflowCommand.STOP_WORKFLOW, self._handle_stop)
        self.frontend.set_command_handler(WorkflowCommand.REQUEST_STATUS, self._handle_status)
        self.frontend.set_command_handler(WorkflowCommand.RESET_WORKFLOW, self._handle_reset)

    # ------------------------------------------------------------------
    # Subsystem Event Callbacks (Boundary Layer)
    # ------------------------------------------------------------------
    def _setup_subsystem_event_callbacks(self) -> None:
        """Wire up subsystem SM event callbacks to publish MQTT.

        This is the boundary layer: subsystem SMs fire events via their
        on_event_callback, which publishes MQTT messages to real subsystems.
        """
        for cfg in self.subsystems_config:
            adapter = self.adapters.get(cfg.name)
            if adapter and adapter.state_machine:
                adapter.state_machine.set_on_event_callback(
                    lambda event_name, name=cfg.name: self._publish_subsystem_event(name, event_name),
                )

    def _publish_subsystem_event(self, subsystem_name: str, event_name: str) -> None:
        """Publish an event from a subsystem SM to MQTT (boundary layer).

        This is the ONLY place in the system that publishes MQTT messages
        triggered by state machine transitions. Subsystem SMs are the
        boundary between internal events and external hardware communication.

        Args:
            subsystem_name: The subsystem that fired the event.
            event_name: The event name (may be scoped or unscoped).
        """
        # Publish the event to the subsystem's status topic
        topic = f"automata/subsystem/{subsystem_name}/event"
        self.mqtt.publish(topic, {
            "event": event_name,
            "source": subsystem_name,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        logger.info(
            "[Boundary] Subsystem '%s' event '%s' → MQTT topic '%s'",
            subsystem_name, event_name, topic,
        )

    # ------------------------------------------------------------------
    # Internal Event Routing (Workflow ↔ Object ↔ Subsystems)
    # ------------------------------------------------------------------
    def _on_event(self, event_name: str) -> None:
        """Route internal events fired by state machines.

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
            # Special scope: broadcast to all subsystems
            logger.info("Broadcasting 'all/%s' to all subsystems", local_event)
            self._broadcast_event_to_subsystems(local_event)
        elif scope:
            # Scoped event: route to the specified subsystem
            logger.info(
                "Routing scoped event '%s' → subsystem '%s'",
                event_name, scope,
            )
            self._route_event_to_subsystem(scope, local_event)
        else:
            # Unscoped event: broadcast to all subsystems
            logger.info("Broadcasting unscoped event '%s' to all subsystems", event_name)
            self._broadcast_event_to_subsystems(event_name)

    def _route_event_to_subsystem(
        self,
        subsystem_name: str,
        event_name: str,
    ) -> None:
        """Fire an event on a specific subsystem's state machine.

        This is internal communication only – no MQTT involved.
        The subsystem's state machine handles the event and may fire
        its own on_event_callback to publish MQTT.

        Args:
            subsystem_name: The target subsystem.
            event_name: The event to fire on that subsystem.
        """
        adapter = self.adapters.get(subsystem_name)
        if adapter is None:
            logger.warning("No adapter found for scoped event target '%s'", subsystem_name)
            return

        if adapter.state_machine is None:
            logger.warning("No state machine for subsystem '%s'", subsystem_name)
            return

        try:
            new_state = adapter.fire_transition(event_name)
            logger.info(
                "Subsystem '%s' transitioned: %s (event: %s)",
                subsystem_name, new_state, event_name,
            )
            # Update frontend with subsystem state change
            self._fire_frontend_event(
                BackendEvent.SUBSYSTEM_STATE_CHANGED.value,
                {
                    "subsystem": subsystem_name,
                    "new_state": new_state,
                    "event": event_name,
                },
            )
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
                except StateMachineError:
                    # Some subsystems may not have a transition for this event
                    logger.debug(
                        "Subsystem '%s' has no transition for event '%s'",
                        cfg.name, event_name,
                    )

    # ------------------------------------------------------------------
    # Object Event Handler
    # ------------------------------------------------------------------
    def _on_object_event(self, object_id: str, event_name: str) -> None:
        """Handle events fired by object state machine transitions.

        Object SM events are routed like other internal events:
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
        # Reuse the same routing logic as workflow events
        self._on_event(event_name)

    # ------------------------------------------------------------------
    # Operator Command Handlers
    # ------------------------------------------------------------------
    def _handle_start(self, data: Dict[str, Any]) -> None:
        logger.info("Operator: start_workflow")
        # Always clear previous state before starting a new workflow.
        # This handles starting from stopped, error, or idle.
        self._reset_workflow_state()

        if not self.fire_event("start_requested"):
            return  # error already logged by fire_event

        new_state = WorkflowState(self.state_machine.state)
        self._update_state(new_state)

        if self._use_simulation and new_state == WorkflowState.STARTING:
            self._start_simulation()

    def _handle_pause(self, data: Dict[str, Any]) -> None:
        logger.info("Operator: pause_workflow")
        # Pause the simulation if it is running
        if self._simulator is not None:
            self._simulator.pause()
        if not self.fire_event("pause_requested"):
            return  # error already logged by fire_event
        self._update_state(WorkflowState(self.state_machine.state))

    def _handle_resume(self, data: Dict[str, Any]) -> None:
        logger.info("Operator: resume_workflow")
        # Resume the simulation if it was paused
        if self._simulator is not None:
            self._simulator.resume()
        if not self.fire_event("resume_requested"):
            return  # error already logged by fire_event
        self._update_state(WorkflowState(self.state_machine.state))

    def _handle_stop(self, data: Dict[str, Any]) -> None:
        logger.info("Operator: stop_workflow")
        # Stop the simulation if it is running
        if self._simulator is not None:
            self._simulator.stop()
            self._simulator = None
        # running → stopping
        if not self.fire_event("stop_requested"):
            return  # error already logged by fire_event
        # stopping → stopped (complete the transition immediately when
        # the operator initiates the stop, since the simulator is already
        # stopped and _simulate_completion will not run)
        if self.state_machine.state == WorkflowState.STOPPING:
            self.fire_event("stopped")
            self._update_state(WorkflowState.STOPPED)

    def _handle_reset(self, data: Dict[str, Any]) -> None:
        """Handle workflow reset (from error or stopped state back to idle).

        Resets the entire workflow: clears all object state machines,
        resets progress, and transitions the workflow state machine to idle.
        Stops any running simulation first to avoid race conditions.
        """
        logger.info("Operator: reset_workflow")
        # Stop any running simulation to avoid race conditions
        if self._simulator is not None:
            self._simulator.stop()
            self._simulator = None

        self._reset_workflow_state()

        if not self.fire_event("reset_requested"):
            return  # error already logged by fire_event

        new_state = WorkflowState(self.state_machine.state)
        self._update_state(new_state)
        self._fire_frontend_event(
            BackendEvent.WORKFLOW_STATE_CHANGED.value,
            {"new_state": new_state.value, "source": "operator"},
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
        self._progress = 0
        self._current_object = None
        self._status.progress = 0
        self._status.current_object = None
        self._status.objects = []
        logger.info("Workflow state reset: cleared objects and progress")

    def _start_workflow_immediately(self) -> None:
        """Start the workflow immediately from idle when all subsystems are ready."""
        try:
            new_state = self.state_machine.fire("start_requested", self._status)
            self._update_state(WorkflowState(new_state))
            self._fire_frontend_event(
                BackendEvent.WORKFLOW_STATE_CHANGED.value,
                {"new_state": new_state, "source": "subsystems_ready"},
            )

            new_state = self.state_machine.fire("subsystem_ready", self._status)
            self._update_state(WorkflowState(new_state))
            self._fire_frontend_event(
                BackendEvent.WORKFLOW_STATE_CHANGED.value,
                {"new_state": new_state, "source": "subsystems_ready"},
            )

            logger.info("Workflow started immediately (all subsystems ready)")
        except StateMachineError as exc:
            logger.error("Failed to start workflow immediately: %s", exc)

    # ------------------------------------------------------------------
    # Simulation
    # ------------------------------------------------------------------
    def _start_simulation(self) -> None:
        """Start simulation subsystems and run the full workflow."""
        from backend.orchestrator.simulation import WorkflowSimulator

        if self._simulator is not None:
            logger.warning("Simulation already running")
            return

        self._simulator = WorkflowSimulator(
            orchestrator=self,
            broker_host=self.mqtt.broker,
            broker_port=self.mqtt.port,
            num_objects=self._total_objects or 3,
            subsystems_config=self.subsystems_config,
            global_config=self._global_config,
        )
        self._simulator.start()

        thread = threading.Thread(target=self._run_simulation_direct, daemon=True)
        thread.start()

    def _run_simulation_direct(self) -> None:
        """Run the full workflow simulation in a background thread."""
        try:
            logger.info("Starting simulation workflow immediately...")
            self._simulator.simulate_full_workflow()
        except Exception as exc:
            logger.error("Simulation error: %s", exc, exc_info=True)
            self._fire_frontend_event(
                BackendEvent.ERROR_REPORTED.value,
                {"message": f"Simulation error: {exc}"},
            )

    # ------------------------------------------------------------------
    # Object State Machine Management
    # ------------------------------------------------------------------
    def create_object(self, object_id: str) -> None:
        """Create a new object state machine instance."""
        with self._lock:
            self.object_manager.create_object(object_id)
        logger.info("Created object state machine: %s", object_id)

        self._fire_frontend_event(
            BackendEvent.OBJECT_ADDED.value,
            {
                "object_id": object_id,
                "state": "identified_in_box",
                "state_description": "Object has been identified in the box",
            },
        )
        self._broadcast_objects()

    def remove_object(self, object_id: str) -> None:
        """Remove an object state machine instance."""
        with self._lock:
            self.object_manager.remove_object(object_id)
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
            self._broadcast_objects()

            if new_state == "object_analyzed":
                logger.info("Object '%s' analysis complete", object_id)
                self._progress = len(self.object_manager.get_object_ids())
                self._fire_frontend_event(
                    BackendEvent.PROGRESS_UPDATED.value,
                    {"progress": self._progress, "total": self._total_objects},
                )

        return new_state

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
    def _on_mqtt_message(self, topic: str, payload: bytes) -> None:
        """Route incoming MQTT messages to appropriate handlers."""
        logger.info("[Orchestrator] MQTT message on topic=%s", topic)
        topic_str = topic
        try:
            data = json.loads(payload.decode())
        except (json.JSONDecodeError, AttributeError):
            data = payload.decode()

        logger.info("[Orchestrator] MQTT dispatch %s -> %s", topic_str, data)

        # Match against registered subsystem callbacks
        for pattern, callback in self._subsystem_callbacks.items():
            import fnmatch
            if fnmatch.fnmatchcase(topic_str, pattern):
                callback(topic_str, data)
                return

        # Default: check for subsystem status updates
        if "automata/subsystem/" in topic_str and "status" in topic_str:
            parts = topic_str.split("/")
            if len(parts) >= 3:
                subsystem_name = parts[2]
                if subsystem_name in self.adapters:
                    self._update_subsystem_status(subsystem_name, data)

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
    def _update_subsystem_status(
        self,
        subsystem_name: str,
        data: Dict[str, Any],
    ) -> None:
        if subsystem_name not in self.adapters:
            return
        adapter = self.adapters[subsystem_name]
        state_str = data.get("state", "").lower()
        try:
            state = SubsystemState(state_str)
        except ValueError:
            state = SubsystemState.OFFLINE

        adapter.update_state(state)

        self._fire_frontend_event(
            BackendEvent.SUBSYSTEM_STATUS_UPDATED.value,
            {
                "subsystem": subsystem_name,
                "state": state.value,
                "details": data,
            },
        )

        # Check if all required subsystems are ready (for starting → running)
        if self.state_machine.state == WorkflowState.STARTING:
            required_adapters = self.adapter_factory.get_required()
            all_ready = all(
                a.state == SubsystemState.READY for a in required_adapters
            )
            if all_ready:
                logger.info("All required subsystems ready!")
                self.state_machine.fire("subsystem_ready", self._status)
                self._update_state(WorkflowState.RUNNING)
                self._fire_frontend_event(
                    BackendEvent.WORKFLOW_STATE_CHANGED.value,
                    {"new_state": "running", "source": "subsystems_ready"},
                )

    # ------------------------------------------------------------------
    # Subsystem State Machine Transitions
    # ------------------------------------------------------------------
    def fire_subsystem_transition(
        self,
        subsystem_name: str,
        event: str,
    ) -> bool:
        """Fire a transition on a subsystem's state machine.

        This is used by the simulation and external callers.
        The subsystem SM handles MQTT publishing via its boundary callback.
        """
        adapter = self.adapters.get(subsystem_name)
        if adapter is None:
            logger.warning("No adapter found for subsystem '%s'", subsystem_name)
            return False

        if adapter.state_machine is None:
            logger.warning("No state machine for subsystem '%s'", subsystem_name)
            return False

        try:
            new_state = adapter.fire_transition(event)
            logger.info(
                "Subsystem '%s' transition: %s -> %s (event: %s)",
                subsystem_name, adapter.state_machine.current_state,
                new_state, event,
            )

            self._fire_frontend_event(
                BackendEvent.SUBSYSTEM_STATE_CHANGED.value,
                {
                    "subsystem": subsystem_name,
                    "new_state": new_state,
                    "source": "state_machine",
                },
            )

            if subsystem_name in self._status.active_subsystems:
                info = self._status.active_subsystems[subsystem_name]
                info.current_state = new_state
                info.state_description = adapter._get_state_description()

            self._check_workflow_state_on_subsystem_change(subsystem_name, new_state)
            return True

        except Exception as exc:
            logger.error(
                "Failed to fire transition on subsystem '%s': %s",
                subsystem_name, exc,
            )
            return False

    def _check_workflow_state_on_subsystem_change(
        self,
        subsystem_name: str,
        new_state: str,
    ) -> None:
        """Check if a subsystem state change should trigger a workflow state transition."""
        if new_state == "ready" and self.state_machine.state == WorkflowState.STARTING:
            required_adapters = self.adapter_factory.get_required()
            all_ready = all(
                a.state == SubsystemState.READY for a in required_adapters
            )
            if all_ready:
                logger.info("All required subsystems ready!")
                self.state_machine.fire("subsystem_ready", self._status)
                self._update_state(WorkflowState.RUNNING)
                self._fire_frontend_event(
                    BackendEvent.WORKFLOW_STATE_CHANGED.value,
                    {"new_state": "running", "source": "subsystems_ready"},
                )

    # ------------------------------------------------------------------
    # State Management
    # ------------------------------------------------------------------
    def _update_state(self, new_state: WorkflowState) -> None:
        self._status.state = new_state
        self._fire_frontend_event(
            BackendEvent.WORKFLOW_STATE_CHANGED.value,
            {"new_state": new_state.value},
        )
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
            self._status.objects = [
                ObjectInfo(
                    object_id=obj["object_id"],
                    state=obj.get("state", "identified_in_box"),
                    state_description=obj.get("state_description", ""),
                    created=obj.get("created", ""),
                    last_state_change=obj.get("last_state_change", ""),
                    transitions=obj.get("transitions", []),
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
            self._update_state(WorkflowState(new_state))
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
        new_state = adapter.state_machine.current_state if adapter.state_machine else "ready"
        self._fire_frontend_event(
            BackendEvent.SUBSYSTEM_STATE_CHANGED.value,
            {
                "subsystem": subsystem_name,
                "new_state": new_state,
                "source": "simulation_init",
            },
        )
        # Keep the status dict in sync so broadcast_with_display_config is correct
        if subsystem_name in self._status.active_subsystems:
            info = self._status.active_subsystems[subsystem_name]
            info.current_state = new_state
            info.state_description = adapter._get_state_description()
        logger.info("Subsystem '%s' notified as ready for frontend", subsystem_name)

    def update_all_subsystems_ready(self) -> None:
        """Mark all required subsystems as ready."""
        for adapter in self.adapter_factory.get_required():
            adapter.update_state(SubsystemState.READY)
        if self.state_machine.state == WorkflowState.STARTING:
            required_adapters = self.adapter_factory.get_required()
            all_ready = all(
                a.state == SubsystemState.READY for a in required_adapters
            )
            if all_ready:
                self.state_machine.fire("subsystem_ready", self._status)
                self._update_state(WorkflowState.RUNNING)
                self._fire_frontend_event(
                    BackendEvent.WORKFLOW_STATE_CHANGED.value,
                    {"new_state": "running", "source": "all_ready"},
                )

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
                mqtt_topic=item.get("mqtt_topic", "automata/subsystem/{name}"),
                initial_state=item.get("initial_state", "ready"),
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
                mqtt_topic=data.get("mqtt_topic", "automata/subsystem/{name}"),
                initial_state=data.get("initial_state", "ready"),
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
        for cfg in self.subsystems_config:
            self.mqtt.subscribe(cfg.status_topic, qos=1)
            self.mqtt.subscribe(cfg.response_topic, qos=1)
        self.mqtt.subscribe(f"{TOPIC_SUBSYSTEM}/*/status", qos=1)
        self.mqtt.subscribe(f"{TOPIC_SUBSYSTEM}/*/response", qos=1)
        self.frontend.start()

        self.frontend.set_subsystem_config(
            [cfg.to_dict() for cfg in self.subsystems_config]
        )
        # Send object workflow display config (state order + colors) to frontend
        self.frontend.set_workflow_display_config(
            self._global_config.object_state_order,
            self._global_config.object_state_colors,
        )

        self._fire_frontend_event(
            BackendEvent.WORKFLOW_STATE_CHANGED.value,
            {"new_state": "idle", "source": "orchestrator_start"},
        )
        if self._use_simulation:
            logger.info("Simulation mode enabled")
        logger.info("Workflow orchestrator started")

    def stop(self) -> None:
        """Stop the orchestrator cleanly."""
        logger.info("Stopping workflow orchestrator...")
        if self._simulator is not None:
            self._simulator.stop()
            self._simulator = None
        self.frontend.stop()
        self.mqtt.disconnect()
        logger.info("Workflow orchestrator stopped")

    def __enter__(self) -> "WorkflowOrchestrator":
        self.start()
        return self

    def __exit__(self, *args: Any) -> None:
        self.stop()
