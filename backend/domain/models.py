"""Domain models for the AUTOMATA orchestrator."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


class WorkflowState(str, enum.Enum):
    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


class SubsystemState(str, enum.Enum):
    READY = "ready"
    BUSY = "busy"
    OFFLINE = "offline"
    ERROR = "error"


class ObjectState(str, enum.Enum):
    IDENTIFIED = "identified_in_box"
    MOVING_TO_DISK = "moving_to_disk"
    HSI_3DI_ANALYSIS = "HSI_3DI_analysis"
    GOING_SECOND_POSITION = "object_going_second_position"
    XRF_RAMAN_ANALYSIS = "XRF_RAMAN_analysis"
    GOING_FIRST_POSITION = "object_going_first_position"
    MOVING_TO_BOX = "moving_to_box"
    ANALYZED = "object_analyzed"


class WorkflowCommand(str, enum.Enum):
    START_WORKFLOW = "start_workflow"
    PAUSE_WORKFLOW = "pause_workflow"
    RESUME_WORKFLOW = "resume_workflow"
    STOP_WORKFLOW = "stop_workflow"
    REQUEST_STATUS = "request_status"
    RESET_WORKFLOW = "reset_workflow"


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


@dataclass
class SubsystemConfig:
    """Configuration for a single subsystem, loaded from YAML.

    Contains both subsystem metadata (name, type, required, etc.)
    and the path to its state machine YAML file.
    """

    name: str
    display_name: str
    type: str = "generic"
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
class GlobalConfig:
    """Global configuration loaded from global.yaml.

    Contains all default values: broker, ports, simulation defaults,
    object workflow display settings, and workflow state messages.
    """

    mqtt_broker: str = "localhost"
    mqtt_port: int = 1883
    frontend_host: str = "0.0.0.0"
    frontend_port: int = 8080
    simulation_default_num_objects: int = 3
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
        sim_cfg = data.get("simulation", {})
        obj_cfg = data.get("object_workflow", {})
        wf_cfg = data.get("workflow_states", {})

        return cls(
            mqtt_broker=mqtt_cfg.get("broker", "localhost"),
            mqtt_port=mqtt_cfg.get("port", 1883),
            frontend_host=frontend_cfg.get("host", "0.0.0.0"),
            frontend_port=frontend_cfg.get("port", 8080),
            simulation_default_num_objects=sim_cfg.get(
                "default_num_objects", 3
            ),
            object_state_order=obj_cfg.get("state_order", []),
            object_state_colors=obj_cfg.get("state_colors", {}),
            workflow_state_messages=wf_cfg,
        )


@dataclass
class SubsystemInfo:
    """Runtime info about a subsystem, including its current state."""

    name: str
    display_name: str = ""
    type: str = "generic"
    current_state: str = "ready"
    state_description: str = ""
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
            "required": self.required,
            "metadata": self.metadata,
        }


@dataclass
class EventPayload:
    event_type: str
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event": self.event_type,
            **self.details,
        }


@dataclass
class ObjectInfo:
    """Runtime info about an analyzed object."""

    object_id: str = ""
    state: str = "identified_in_box"
    state_description: str = ""
    created: str = ""
    last_state_change: str = ""
    transitions: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "object_id": self.object_id,
            "state": self.state,
            "state_description": self.state_description,
            "created": self.created,
            "last_state_change": self.last_state_change,
            "transitions": self.transitions,
        }


@dataclass
class WorkflowStatus:
    state: WorkflowState
    total_objects: int = 0
    progress: int = 0
    current_object: Optional[str] = None
    active_subsystems: Dict[str, SubsystemInfo] = field(default_factory=dict)
    objects: List[ObjectInfo] = field(default_factory=list)
    error: Optional[str] = None
    last_update: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state.value,
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
