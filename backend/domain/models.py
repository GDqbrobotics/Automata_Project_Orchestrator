"""Domain models for the AUTOMATA orchestrator."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


class SubsystemState(str, enum.Enum):
    """Adapter-internal subsystem state.

    READY/STOPPED/ERROR/OFFLINE mirror the shared vocabulary
    (global.yaml `states:`); BUSY is the catch-all for work stages (any
    reported state outside the non-work set).
    """

    READY = "ready"
    BUSY = "busy"
    OFFLINE = "offline"
    ERROR = "error"
    STOPPED = "stopped"


class WorkflowCommand(str, enum.Enum):
    START_WORKFLOW = "start_workflow"
    PAUSE_WORKFLOW = "pause_workflow"
    RESUME_WORKFLOW = "resume_workflow"
    STOP_WORKFLOW = "stop_workflow"
    REQUEST_STATUS = "request_status"
    RESET_WORKFLOW = "reset_workflow"
    ASSIGN_ALIAS = "assign_alias"
    # Simulation mode only (--simulation): toggle whether one subsystem
    # is served by its simulated process or by the real hardware. Only
    # accepted before the workflow starts (idle / stopped / error).
    TOGGLE_SUBSYSTEM_SIMULATION = "toggle_subsystem_simulation"
    # Debug mode only (--debug): inject an MQTT message on one of the
    # topics a real subsystem publishes, as if the subsystem sent it.
    DEBUG_SEND_SUBSYSTEM_MESSAGE = "debug_send_subsystem_message"
    # Debug mode only (--debug): the configuration wizard. It reads,
    # validates, patches and saves the YAML files under backend/config/
    # while the workflow is not running.
    GET_CONFIG = "get_config"
    VALIDATE_CONFIG = "validate_config"
    PATCH_CONFIG = "patch_config"
    SAVE_CONFIG = "save_config"
    CREATE_SUBSYSTEM = "create_subsystem"
    DELETE_SUBSYSTEM = "delete_subsystem"
    WRITE_SIMULATOR = "write_simulator"
    RENDER_MESSAGE_FORMATS = "render_message_formats"
    # Rebuild the `states:` block of a config file from the wizard's
    # structured state-machine editor (states/transitions/messages).
    RENDER_STATE_MACHINE = "render_state_machine"


class Command(str, enum.Enum):
    START = "start_workflow"
    PAUSE = "pause_workflow"
    RESUME = "resume_workflow"
    STOP = "stop_workflow"
    STATUS = "request_status"


class BackendEvent(str, enum.Enum):
    WORKFLOW_STATE_CHANGED = "workflow_state_changed"
    SUBSYSTEM_STATUS_UPDATED = "subsystem_status_updated"
    SUBSYSTEM_STATE_CHANGED = "subsystem_state_changed"
    ERROR_REPORTED = "error_reported"
    PROGRESS_UPDATED = "progress_updated"
    OBJECT_STATE_CHANGED = "object_state_changed"
    OBJECT_ADDED = "object_added"
    OBJECT_REMOVED = "object_removed"
    OBJECT_ALIAS_ASSIGNED = "object_alias_assigned"  # operator set an alias ID
    # Simulation mode: the operator toggled a subsystem's simulation
    # (subsystem is now served by its simulated process / by real hardware)
    SUBSYSTEM_SIMULATION_TOGGLED = "subsystem_simulation_toggled"
    # Message traffic for the state-machine inspector modal:
    SUBSYSTEM_MESSAGE = "subsystem_message"    # MQTT sent to / received from a subsystem
    OBJECT_MESSAGE = "object_message"          # internal messages of one object's SM
    MESSAGES_CLEARED = "messages_cleared"      # history reset (workflow reset)
    # A subsystem was commanded into a state but, after the configured
    # timeout (global.yaml orchestration.state_expect_timeout), has not
    # reported that (or any other) state: its real state lags the
    # expectation (see adapters/subsystem.py).
    SUBSYSTEM_STATE_MISMATCH = "subsystem_state_mismatch"
    # Configuration wizard (debug mode only): snapshot of the editable
    # config files, an in-memory patch result, a saved file, a validation
    # result and a deleted subsystem.
    CONFIG_SNAPSHOT = "config_snapshot"
    CONFIG_PATCHED = "config_patched"
    CONFIG_VALIDATED = "config_validated"
    CONFIG_SAVED = "config_saved"
    SUBSYSTEM_DELETED = "subsystem_deleted"
    # The orchestrator launched / stopped the real interface of a
    # subsystem (subsystem YAML metadata.interface, R11) — headless, the
    # subsystem state is known through MQTT from then on.
    INTERFACE_COMMAND = "interface_command"


@dataclass
class SubsystemConfig:
    """Configuration for a single subsystem, loaded from YAML.

    Contains both subsystem metadata (name, type, required, etc.)
    and the path to its state machine YAML file.
    """

    name: str
    display_name: str
    type: str = "generic"
    # Per-subsystem topic template (`{name}` is substituted). May differ
    # from the global `mqtt.topic_prefix` (docs/CONFIG_CHANGE_GUIDE.md, R4).
    mqtt_topic: str = "automata/subsystem/{name}"
    initial_state: str = "ready"
    required: bool = True
    metadata: Dict[str, Any] = field(default_factory=dict)
    state_machine_path: Optional[str] = None

    @property
    def command_topic(self) -> str:
        return self.mqtt_topic.replace("{name}", self.name) + "/command"

    @property
    def status_topic(self) -> str:
        return self.mqtt_topic.replace("{name}", self.name) + "/status"

    @property
    def response_topic(self) -> str:
        return self.mqtt_topic.replace("{name}", self.name) + "/response"

    @property
    def points_topic(self) -> str:
        """Dedicated topic the orchestrator pushes an object's stored
        points on, right after the command that needs them (R12). The
        suffix is configurable for systems that already have a topic for
        it."""
        suffix = "points"
        if isinstance(self.metadata, dict):
            suffix = str(self.metadata.get("points_topic") or "points")
        return self.mqtt_topic.replace("{name}", self.name) + "/" + suffix

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "type": self.type,
            "mqtt_topic": self.mqtt_topic,
            "initial_state": self.initial_state,
            "required": self.required,
            "metadata": self.metadata,
        }


@dataclass
class InterfaceConfig:
    """Real-interface scripts of a subsystem (subsystem YAML metadata).

    A real subsystem cannot initialize until its interface (ROS node,
    device daemon, ...) is running. ``metadata.interface`` declares the
    shell commands the orchestrator executes *headless*:

    - ``startup``  - launched when the subsystem's interface is needed
      (orchestrator start, workflow start / wake of a stopped subsystem,
      SIM->REAL toggle): the subsystem boots and reports its state over
      MQTT like any hardware.
    - ``shutdown`` - launched when the subsystem reports entering the
      ``stopped`` state and, waited on, at orchestrator shutdown.
    """

    enabled: bool = False
    startup: str = ""
    shutdown: str = ""
    cwd: str = ""

    @classmethod
    def from_metadata(cls, metadata: Optional[Dict[str, Any]]) -> "InterfaceConfig":
        raw = (metadata or {}).get("interface")
        if not isinstance(raw, dict):
            return cls()
        return cls(
            enabled=bool(raw.get("enabled", False)),
            startup=str(raw.get("startup") or ""),
            shutdown=str(raw.get("shutdown") or ""),
            cwd=str(raw.get("cwd") or ""),
        )


@dataclass
class GlobalConfig:
    """Global configuration loaded from global.yaml.

    Contains all default values: broker, ports, the shared subsystem
    vocabulary (states/events that backend code used to hardcode), the
    MQTT topic tree, orchestration rules, simulation defaults, object
    workflow display settings, and workflow state messages.
    """

    mqtt_broker: str = "localhost"
    mqtt_port: int = 1883
    mqtt_topic_prefix: str = "automata/subsystem"
    frontend_host: str = "0.0.0.0"
    frontend_port: int = 8080
    frontend_dev_port: int = 3000
    # Shared subsystem vocabulary (global.yaml `states:`)
    states_non_work: List[str] = field(
        default_factory=lambda: ["initializing", "ready", "stopped", "error"]
    )
    states_report_events: List[str] = field(
        default_factory=lambda: ["complete", "init_complete"]
    )
    states_ready: str = "ready"
    # Ordered: [pause, resume]
    states_hardware_commands: List[str] = field(
        default_factory=lambda: ["pause", "resume"]
    )
    # Orchestration rules (global.yaml `orchestration:`)
    state_expect_timeout: float = 10.0
    alias_pattern: str = r"^[A-Za-z0-9_\-\. ]{1,64}$"
    simulation_default_num_objects: int = 3
    simulation_heartbeat_interval: float = 2.0
    simulation_default_init_duration: float = 1.5
    simulation_default_work_duration: float = 2.0
    simulation_object_id_format: str = "object_{i:03d}"
    object_state_order: List[str] = field(default_factory=list)
    object_state_colors: Dict[str, str] = field(default_factory=dict)
    workflow_state_messages: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_yaml(cls, config_path: str | Path) -> "GlobalConfig":
        with open(config_path, "r") as f:
            data = yaml.safe_load(f)
        if not data:
            return cls()

        mqtt_cfg = data.get("mqtt", {})
        frontend_cfg = data.get("frontend", {})
        states_cfg = data.get("states", {})
        orch_cfg = data.get("orchestration", {})
        sim_cfg = data.get("simulation", {})
        obj_cfg = data.get("object_workflow", {})
        wf_cfg = data.get("workflow_states", {})

        cfg = cls()
        cfg.mqtt_broker = mqtt_cfg.get("broker", cfg.mqtt_broker)
        cfg.mqtt_port = mqtt_cfg.get("port", cfg.mqtt_port)
        cfg.mqtt_topic_prefix = mqtt_cfg.get("topic_prefix", cfg.mqtt_topic_prefix)
        cfg.frontend_host = frontend_cfg.get("host", cfg.frontend_host)
        cfg.frontend_port = frontend_cfg.get("port", cfg.frontend_port)
        cfg.frontend_dev_port = frontend_cfg.get("dev_port", cfg.frontend_dev_port)
        cfg.states_non_work = states_cfg.get("non_work", cfg.states_non_work)
        cfg.states_report_events = states_cfg.get(
            "report_events", cfg.states_report_events
        )
        cfg.states_ready = states_cfg.get("ready", cfg.states_ready)
        cfg.states_hardware_commands = states_cfg.get(
            "hardware_commands", cfg.states_hardware_commands
        )
        cfg.state_expect_timeout = float(
            orch_cfg.get("state_expect_timeout", cfg.state_expect_timeout)
        )
        cfg.alias_pattern = orch_cfg.get("alias_pattern", cfg.alias_pattern)
        cfg.simulation_default_num_objects = sim_cfg.get(
            "default_num_objects", cfg.simulation_default_num_objects
        )
        cfg.simulation_heartbeat_interval = float(
            sim_cfg.get("heartbeat_interval", cfg.simulation_heartbeat_interval)
        )
        cfg.simulation_default_init_duration = float(
            sim_cfg.get(
                "default_init_duration", cfg.simulation_default_init_duration
            )
        )
        cfg.simulation_default_work_duration = float(
            sim_cfg.get(
                "default_work_duration", cfg.simulation_default_work_duration
            )
        )
        cfg.simulation_object_id_format = sim_cfg.get(
            "object_id_format", cfg.simulation_object_id_format
        )
        cfg.object_state_order = obj_cfg.get("state_order", [])
        cfg.object_state_colors = obj_cfg.get("state_colors", {})
        cfg.workflow_state_messages = wf_cfg
        return cfg

    # ------------------------------------------------------------------
    # Shared vocabulary helpers
    # ------------------------------------------------------------------
    @property
    def non_work_states(self) -> frozenset:
        return frozenset(self.states_non_work)

    @property
    def report_events(self) -> frozenset:
        return frozenset(self.states_report_events)

    def tone_for(self, state: Optional[str]) -> str:
        """Map a raw subsystem state to the GUI's visual tone vocabulary
        (F3): error/offline/ready, other non-work states -> idle, work
        stages -> busy. Computed once here so the frontend never has to
        enumerate state names."""
        if state is None:
            return "offline"
        if state == "error":
            return "error"
        if state == "offline":
            return "offline"
        if state == self.states_ready:
            return "ready"
        if state in self.non_work_states:
            return "idle"
        return "busy"


@dataclass
class SubsystemInfo:
    """Runtime info about a subsystem, including its current state."""

    name: str
    display_name: str = ""
    type: str = "generic"
    current_state: str = ""
    state_description: str = ""
    # Visual tone (ready/busy/error/idle/offline/warning) computed by the
    # orchestrator from the configured vocabulary; the UI renders it as-is.
    tone: str = ""
    required: bool = True
    topic_prefix: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "type": self.type,
            "state": self.current_state,
            "state_description": self.state_description,
            "tone": self.tone,
            "required": self.required,
            "metadata": self.metadata,
        }


@dataclass
class EventPayload:
    event_type: str
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict format expected by the frontend.

        The frontend expects messages structured as:
        {"event": <type>, "details": {<key>: <value>, ...}}

        This is consistent with how the frontend's reduceState function
        parses incoming WebSocket messages.
        """
        return {
            "event": self.event_type,
            "details": self.details,
        }


@dataclass
class ObjectInfo:
    """Runtime info about an analyzed object."""

    object_id: str = ""
    # The orchestrator always fills this from object.yaml (the initial
    # state / the live state) – no hardcoded default (R6).
    state: str = ""
    state_description: str = ""
    created: str = ""
    last_state_change: str = ""
    transitions: List[Dict[str, Any]] = field(default_factory=list)
    # Operator-assigned alias ID for the local database ("" = none yet).
    # The machine keeps working with object_id; the alias is the ID the
    # locally stored data gets when the database is updated manually.
    alias: str = ""
    # Measurement points received for this object over MQTT (R12): the
    # gripper contact points from vision, the AI points of interest, ...
    # Each entry: {kind, source, object_id, received_at, points: [...]}
    points: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "object_id": self.object_id,
            "state": self.state,
            "state_description": self.state_description,
            "created": self.created,
            "last_state_change": self.last_state_change,
            "transitions": self.transitions,
            "alias": self.alias,
            "points": self.points,
        }


@dataclass
class WorkflowStatus:
    # The raw state name from the workflow SM engine (workflow.yaml is the
    # source of truth; there is no Python enum duplicate – R3).
    state: str = ""
    total_objects: int = 0
    progress: int = 0
    current_object: Optional[str] = None
    active_subsystems: Dict[str, SubsystemInfo] = field(default_factory=dict)
    objects: List[ObjectInfo] = field(default_factory=list)
    error: Optional[str] = None
    last_update: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "total_objects": self.total_objects,
            "progress": self.progress,
            "current_object": self.current_object,
            "subsystems": {
                name: info.to_dict()
                for name, info in self.active_subsystems.items()
            },
            "objects": [obj.to_dict() for obj in self.objects],
            "error": self.error,
            "last_update": self.last_update,
        }
