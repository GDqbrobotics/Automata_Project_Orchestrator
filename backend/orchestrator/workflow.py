"""
Workflow orchestrator – the heart of the GUI.

Ties together:
  - State machine engine (declarative YAML config)
  - Subsystem configuration (per-subsystem YAML files)
  - MQTT messaging (subsystem communication)
  - Subsystem adapters with per-subsystem state machines
  - Frontend WebSocket server
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from backend.adapters.subsystem import (
    SubsystemAdapter,
    SubsystemAdapterFactory,
    SubsystemState,
)
from backend.domain.models import (
    BackendEvent,
    Command,
    EventPayload,
    SubsystemConfig,
    SubsystemInfo,
    WorkflowCommand,
    WorkflowState,
    WorkflowStatus,
)
from backend.messaging.mqtt_client import MqttClient, TOPIC_SUBSYSTEM
from backend.services.frontend_server import FrontendServer
from backend.state.state_machine import StateMachineEngine, StateMachineError

logger = logging.getLogger(__name__)


class WorkflowOrchestrator:
    """
    Central orchestrator that:
      1. Manages the workflow state machine
      2. Loads subsystem configuration from individual YAML files
      3. Creates and manages per-subsystem state machines
      4. Communicates with subsystems via MQTT
      5. Exposes state to the frontend via WebSocket
      6. Handles operator commands
    """

    def __init__(
        self,
        config_path: str | Path,
        subsystems_config_path: str | Path | None = None,
        use_simulation: bool = False,
        num_objects: int = 3,
    ) -> None:
        # MQTT client (created first so it can be passed to the state machines)
        self.mqtt = MqttClient(broker="localhost", port=1883)

        # State machine (passes mqtt_client so action handlers can publish)
        self.state_machine = StateMachineEngine(config_path, mqtt_client=self.mqtt)

        # Load subsystem configuration from YAML files
        self.subsystems_config: List[SubsystemConfig] = []
        self._load_subsystems_config(subsystems_config_path)

        # Create adapters with per-subsystem state machines
        self.adapter_factory = SubsystemAdapterFactory(
            self.mqtt,
            self.subsystems_config,
            config_dir=Path(__file__).resolve().parent.parent / "config" / "subsystems",
        )
        self.adapters: Dict[str, SubsystemAdapter] = self.adapter_factory.get_all()

        # Frontend server
        self.frontend = FrontendServer(host="0.0.0.0", port=8080)

        # Current workflow status
        self._status = WorkflowStatus(
            state=WorkflowState.IDLE,
            total_objects=0,
            active_subsystems={name: adapter.info for name, adapter in self.adapters.items()},
        )
        self._total_objects = 0
        self._progress = 0
        self._current_object: Optional[str] = None
        self._lock = threading.Lock()

        # Simulation mode
        self._use_simulation = use_simulation
        self._simulator: Any = None

        # Register command handlers with frontend server
        self.frontend.set_command_handler(WorkflowCommand.START_WORKFLOW, self._handle_start)
        self.frontend.set_command_handler(WorkflowCommand.PAUSE_WORKFLOW, self._handle_pause)
        self.frontend.set_command_handler(WorkflowCommand.RESUME_WORKFLOW, self._handle_resume)
        self.frontend.set_command_handler(WorkflowCommand.STOP_WORKFLOW, self._handle_stop)
        self.frontend.set_command_handler(WorkflowCommand.REQUEST_STATUS, self._handle_status)
        self.frontend.set_status_callback(self._get_status)

        # MQTT message dispatcher
        self.mqtt._on_message = self._on_mqtt_message

        # Registered subsystem callbacks
        self._subsystem_callbacks: Dict[str, callable] = {}

    # ------------------------------------------------------------------
    # Subsystem configuration loading
    # ------------------------------------------------------------------
    def _load_subsystems_config(self, config_path: str | Path | None) -> None:
        """Load subsystem configuration from YAML file.

        The config file can be either:
        - The old format: a single file with a 'subsystems' list
        - A directory of per-subsystem YAML files (new format)

        If config_path is a directory, it looks for *.yaml files.
        If config_path is a file, it loads the old format.
        If config_path is None, it tries the default locations.
        """
        # Determine config directory
        config_dir = Path(__file__).resolve().parent.parent / "config" / "subsystems"

        # If config_path is provided and is a file, try old format first
        if config_path is not None:
            config_path = Path(config_path)
            if config_path.is_file():
                self._load_old_format(config_path)
                return
            elif config_path.is_dir():
                config_dir = config_path

        # Try new format: load from config_dir/*.yaml
        yaml_files = sorted(config_dir.glob("*.yaml"))
        if yaml_files:
            self._load_new_format(config_dir, yaml_files)
            return

        # Fallback: try old format at default location
        old_config_path = Path(__file__).resolve().parent.parent.parent / "backend" / "config" / "subsystems.yaml"
        if old_config_path.exists():
            logger.info("Falling back to legacy subsystems.yaml")
            self._load_old_format(old_config_path)
            return

        logger.warning("No subsystem configuration found, using empty config")
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
            logger.info("Loaded subsystem config: %s (%s) – required=%s",
                        cfg.name, cfg.type, cfg.required)

    def _load_new_format(self, config_dir: Path, yaml_files: List[Path]) -> None:
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
            logger.info("Loaded subsystem config: %s (%s) – required=%s – state_machine=%s",
                        cfg.name, cfg.type, cfg.required, yaml_file.name)

    @property
    def subsystem_names(self) -> List[str]:
        """Return the list of configured subsystem names."""
        return self.adapter_factory.get_names()

    @property
    def required_subsystem_names(self) -> List[str]:
        """Return the list of required subsystem names."""
        return self.adapter_factory.get_required_names()

    # ------------------------------------------------------------------
    # Operator command handlers
    # ------------------------------------------------------------------
    def _handle_start(self, data: Dict[str, Any]) -> None:
        logger.info("Operator: start_workflow")
        try:
            new_state = self.state_machine.fire("start_requested", self._status)
            self._update_state(WorkflowState(new_state))
            self._fire_frontend_event(
                BackendEvent.WORKFLOW_STATE_CHANGED.value,
                {"new_state": new_state, "source": "operator"},
            )

            # If in simulation mode, start simulators and run workflow
            if self._use_simulation and new_state == "starting":
                self._start_simulation()
        except StateMachineError as exc:
            logger.error("Start rejected: %s", exc)
            self._fire_frontend_event(
                BackendEvent.ERROR_REPORTED.value,
                {"message": str(exc)},
            )

    def _start_simulation(self) -> None:
        """Start simulation subsystems and run the full workflow."""
        from backend.orchestrator.simulation import WorkflowSimulator  # noqa: E402

        if self._simulator is not None:
            logger.warning("Simulation already running")
            return

        self._simulator = WorkflowSimulator(
            orchestrator=self,
            broker_host=self.mqtt.broker,
            broker_port=self.mqtt.port,
            num_objects=self._total_objects or 3,
            subsystems_config=self.subsystems_config,
        )
        self._simulator.start()
        logger.info("Simulation mode: starting workflow with %d objects", self._simulator.num_objects)

        # Run simulation in a background thread so it doesn't block the main loop
        thread = threading.Thread(target=self._run_simulation_direct, daemon=True)
        thread.start()

    def _run_simulation(self) -> None:
        """Run the full workflow simulation."""
        try:
            # Wait for orchestrator to reach 'running' state
            logger.info("Waiting for orchestrator to reach 'running' state...")
            for _ in range(60):  # Wait up to 30 seconds
                if self.state_machine.current_state == "running":
                    logger.info("Orchestrator reached 'running' state")
                    break
                logger.debug("Waiting for orchestrator to reach 'running' state... current state: %s", self.state_machine.current_state)
                time.sleep(0.5)
            else:
                logger.error("Orchestrator did not reach 'running' state. Current state: %s", self.state_machine.current_state)
                return

            # Now run the simulation
            self._simulator.simulate_full_workflow()
        except Exception as exc:
            logger.error("Simulation error: %s", exc, exc_info=True)
            self._fire_frontend_event(
                BackendEvent.ERROR_REPORTED.value,
                {"message": f"Simulation error: {exc}"},
            )

    def _run_simulation_direct(self) -> None:
        """Run the full workflow simulation without waiting for 'running' state."""
        try:
            logger.info("Starting simulation workflow immediately...")
            self._simulator.simulate_full_workflow()
        except Exception as exc:
            logger.error("Simulation error: %s", exc, exc_info=True)
            self._fire_frontend_event(
                BackendEvent.ERROR_REPORTED.value,
                {"message": f"Simulation error: {exc}"},
            )

    def _handle_pause(self, data: Dict[str, Any]) -> None:
        logger.info("Operator: pause_workflow")
        try:
            new_state = self.state_machine.fire("pause_requested", self._status)
            self._update_state(WorkflowState(new_state))
            self._fire_frontend_event(
                BackendEvent.WORKFLOW_STATE_CHANGED.value,
                {"new_state": new_state, "source": "operator"},
            )
        except StateMachineError as exc:
            logger.error("Pause rejected: %s", exc)
            self._fire_frontend_event(
                BackendEvent.ERROR_REPORTED.value,
                {"message": str(exc)},
            )

    def _handle_resume(self, data: Dict[str, Any]) -> None:
        logger.info("Operator: resume_workflow")
        try:
            new_state = self.state_machine.fire("resume_requested", self._status)
            self._update_state(WorkflowState(new_state))
            self._fire_frontend_event(
                BackendEvent.WORKFLOW_STATE_CHANGED.value,
                {"new_state": new_state, "source": "operator"},
            )
        except StateMachineError as exc:
            logger.error("Resume rejected: %s", exc)
            self._fire_frontend_event(
                BackendEvent.ERROR_REPORTED.value,
                {"message": str(exc)},
            )

    def _handle_stop(self, data: Dict[str, Any]) -> None:
        logger.info("Operator: stop_workflow")
        try:
            new_state = self.state_machine.fire("stop_requested", self._status)
            self._update_state(WorkflowState(new_state))
            self._fire_frontend_event(
                BackendEvent.WORKFLOW_STATE_CHANGED.value,
                {"new_state": new_state, "source": "operator"},
            )
        except StateMachineError as exc:
            logger.error("Stop rejected: %s", exc)
            self._fire_frontend_event(
                BackendEvent.ERROR_REPORTED.value,
                {"message": str(exc)},
            )

    def _handle_status(self, data: Dict[str, Any]) -> None:
        logger.info("Operator: request_status")
        self.frontend.broadcast_status(self._status)

    # ------------------------------------------------------------------
    # MQTT message dispatcher
    # ------------------------------------------------------------------
    def _on_mqtt_message(self, topic: str, payload: bytes) -> None:
        """Route incoming MQTT messages to appropriate handlers."""
        logger.info("[Orchestrator] _on_mqtt_message called for topic=%s", topic)
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

    def register_subsystem_callback(self, topic_pattern: str, callback: callable) -> None:
        """Register a callback for MQTT messages matching a topic pattern."""
        self._subsystem_callbacks[topic_pattern] = callback

    # ------------------------------------------------------------------
    # Subsystem status updates
    # ------------------------------------------------------------------
    def _update_subsystem_status(self, subsystem_name: str, data: Dict[str, Any]) -> None:
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

        # Check if all required subsystems are ready (for starting -> running transition)
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
    # Subsystem state machine transitions
    # ------------------------------------------------------------------
    def fire_subsystem_transition(
        self,
        subsystem_name: str,
        event: str,
    ) -> bool:
        """Fire a transition on a subsystem's state machine.

        This method:
        1. Fires the event on the subsystem's state machine
        2. Publishes the new state via MQTT
        3. Updates the frontend with the new subsystem state
        4. Checks if the transition affects the main workflow state

        Returns True if successful, False otherwise.
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

            # Publish the state change via MQTT
            topic = f"automata/subsystem/{subsystem_name}/status"
            self.mqtt.publish(topic, {
                "state": new_state,
                "name": subsystem_name,
                "ready": new_state == "ready",
            })

            # Update the frontend with the new subsystem state
            self._fire_frontend_event(
                BackendEvent.SUBSYSTEM_STATE_CHANGED.value,
                {
                    "subsystem": subsystem_name,
                    "new_state": new_state,
                    "source": "state_machine",
                },
            )

            # Update the subsystem info in the status
            if subsystem_name in self._status.active_subsystems:
                info = self._status.active_subsystems[subsystem_name]
                info.current_state = new_state
                info.state_description = adapter._get_state_description()

            # Check if this transition affects the main workflow state
            self._check_workflow_state_on_subsystem_change(subsystem_name, new_state)

            return True

        except Exception as exc:
            logger.error("Failed to fire transition on subsystem '%s': %s", subsystem_name, exc)
            return False

    def _check_workflow_state_on_subsystem_change(
        self,
        subsystem_name: str,
        new_state: str,
    ) -> None:
        """Check if a subsystem state change should trigger a workflow state transition."""
        # If all subsystems become ready and we're in STARTING, move to RUNNING
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
    # State management
    # ------------------------------------------------------------------
    def _update_state(self, new_state: WorkflowState) -> None:
        self._status.state = new_state
        self._fire_frontend_event(
            BackendEvent.WORKFLOW_STATE_CHANGED.value,
            {"new_state": new_state.value},
        )
        self.frontend.broadcast_status(self._status)

    def _fire_frontend_event(self, event_type: str, details: Dict[str, Any]) -> None:
        payload = EventPayload(
            event_type=event_type,
            details=details,
        ).to_dict()
        self.frontend.broadcast_status(self._status)

    def _get_status(self) -> WorkflowStatus:
        return self._status

    # ------------------------------------------------------------------
    # Convenience: fire internal events
    # ------------------------------------------------------------------
    def fire_event(self, event: str) -> bool:
        """Fire an internal event through the state machine."""
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

    def set_subsystem_state(self, subsystem_name: str, state: SubsystemState, error: Optional[str] = None) -> None:
        """Directly set a subsystem's state (useful for testing)."""
        if subsystem_name not in self.adapters:
            return
        adapter = self.adapters[subsystem_name]
        adapter.update_state(state, error=error)
        logger.info("Subsystem %s set to %s", subsystem_name, state.value)

        # Check if all required subsystems are ready (for starting -> running transition)
        if self.state_machine.state == WorkflowState.STARTING:
            required_adapters = self.adapter_factory.get_required()
            all_ready = all(
                a.state == SubsystemState.READY for a in required_adapters
            )
            logger.info("All required subsystems ready check: %s", all_ready)
            if all_ready:
                logger.info("All required subsystems ready!")
                self.state_machine.fire("subsystem_ready", self._status)
                self._update_state(WorkflowState.RUNNING)
                self._fire_frontend_event(
                    BackendEvent.WORKFLOW_STATE_CHANGED.value,
                    {"new_state": "running", "source": "subsystems_ready"},
                )

    def update_all_subsystems_ready(self) -> None:
        """Mark all required subsystems as ready and trigger the transition check."""
        for adapter in self.adapter_factory.get_required():
            adapter.update_state(SubsystemState.READY)
        # Now check if all required are ready
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
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Start the orchestrator (connect MQTT and start frontend server)."""
        logger.info("Starting workflow orchestrator...")
        logger.info("Configured subsystems: %d", len(self.subsystems_config))
        for cfg in self.subsystems_config:
            logger.info("  - %s (%s) [required=%s]", cfg.name, cfg.type, cfg.required)

        self.mqtt.connect()
        # Subscribe to all configured subsystem status topics
        for cfg in self.subsystems_config:
            self.mqtt.subscribe(cfg.status_topic, qos=1)
            self.mqtt.subscribe(cfg.response_topic, qos=1)
        # Also subscribe to wildcard topics for flexibility
        self.mqtt.subscribe(f"{TOPIC_SUBSYSTEM}/*/status", qos=1)
        self.mqtt.subscribe(f"{TOPIC_SUBSYSTEM}/*/response", qos=1)
        self.frontend.start()

        # Set subsystem configuration for frontend broadcasts
        self.frontend.set_subsystem_config(
            [cfg.to_dict() for cfg in self.subsystems_config]
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
        # Stop simulation if running
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
