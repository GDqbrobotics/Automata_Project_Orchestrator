"""Domain models for the AUTOMATA orchestrator."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


class WorkflowState(str, enum.Enum):
    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    COMPLETING = "completing"
    ERROR = "error"


class SubsystemState(str, enum.Enum):
    READY = "ready"
    BUSY = "busy"
    OFFLINE = "offline"
    ERROR = "error"


class WorkflowCommand(str, enum.Enum):
    START_WORKFLOW = "start_workflow"
    PAUSE_WORKFLOW = "pause_workflow"
    RESUME_WORKFLOW = "resume_workflow"
    STOP_WORKFLOW = "stop_workflow"
    REQUEST_STATUS = "request_status"


class Command(str, enum.Enum):
    START = "start_workflow"
    PAUSE = "pause_workflow"
    RESUME = "resume_workflow"
    STOP = "stop_workflow"
    STATUS = "request_status"


class BackendEvent(str, enum.Enum):
    WORKFLOW_STATE_CHANGED = "workflow_state_changed"
    SUBSYSTEM_STATUS_UPDATED = "subsystem_status_updated"
    ERROR_REPORTED = "error_reported"
    PROGRESS_UPDATED = "progress_updated"


@dataclass
class SubsystemConfig:
    """Configuration for a single subsystem, loaded from YAML."""

    name: str
    display_name: str
    type: str = "generic"
    mqtt_topic: str = "automata/subsystem/{name}"
    initial_state: str = "ready"
    required: bool = True
    metadata: Dict[str, Any] = field(default_factory=dict)

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
class SubsystemConfig:
    """Configuration for a single subsystem, loaded from YAML."""

    name: str
    display_name: str
    type: str = "generic"
    mqtt_topic: str = "automata/subsystem/{name}"
    initial_state: str = "ready"
    required: bool = True
    metadata: Dict[str, Any] = field(default_factory=dict)

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
class SubsystemInfo:
    name: str
    description: str
    topic_prefix: str


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
class WorkflowStatus:
    state: WorkflowState
    total_objects: int = 0
    progress: int = 0
    current_object: Optional[str] = None
    active_subsystems: Dict[str, SubsystemInfo] = field(default_factory=dict)
    error: Optional[str] = None
    last_update: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state.value,
            "total_objects": self.total_objects,
            "progress": self.progress,
            "current_object": self.current_object,
            "subsystems": {
                name: {"name": info.name, "state": info.description}
                for name, info in self.active_subsystems.items()
            },
            "error": self.error,
            "last_update": self.last_update,
        }
