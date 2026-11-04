"""Configurable MQTT payload formats (the field-mapping layer).

Every MQTT message the orchestrator exchanges with a subsystem is built and
parsed through a :class:`MessageFormat`, loaded from
``backend/config/mqtt_formats.yaml`` (docs/CONFIG_CHANGE_GUIDE.md, R10):

* ``default``     – the format applied to every subsystem;
* ``subsystems``  – per-subsystem overrides, deep-merged over ``default``.

Each message kind maps the logical values the orchestrator cares about to
payload fields:

===============  =======================================  ====================
kind             direction                                logical values
===============  =======================================  ====================
command          orchestrator → subsystem ``/event``      event, source, time
hardware_command orchestrator → subsystem ``/event``      event (pause/resume)
status           subsystem → orchestrator ``/status``    state, ready, paused
response         subsystem → orchestrator ``/response``  type, objects, count
===============  =======================================  ====================

Field names may be dotted paths into nested payloads (``data.state``), so
a real subsystem that already speaks a fixed shape can be integrated by
editing one YAML file: the adapter (publisher), the orchestrator receive
path and the simulated subsystem all build/parse through this single
layer, which is what keeps them from drifting apart.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

logger = logging.getLogger(__name__)

# File name under backend/config/ (single source for payload formats).
DEFAULT_FORMATS_FILENAME = "mqtt_formats.yaml"


# ---------------------------------------------------------------------------
# Nested-path helpers (dotted field names)
# ---------------------------------------------------------------------------
def get_path(payload: Any, path: str) -> Any:
    """Read a dotted path (``a.b.c``) from a nested mapping.

    Returns ``None`` when any segment is missing or the payload is not a
    mapping, so extraction is always total and safe.
    """
    if not path:
        return None
    node = payload
    for part in str(path).split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return None
    return node


def set_path(payload: Dict[str, Any], path: str, value: Any) -> None:
    """Write ``value`` at a dotted path, creating intermediate mappings."""
    parts = str(path).split(".") if path else []
    if not parts:
        return
    node = payload
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = value


def _text(value: Any, default: str) -> str:
    return str(value) if value is not None else default


def _bool(value: Any, default: bool) -> bool:
    return bool(value) if value is not None else default


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge ``override`` into ``base`` (override wins)."""
    merged = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


# ---------------------------------------------------------------------------
# Per-kind formats
# ---------------------------------------------------------------------------
@dataclass
class CommandFormat:
    """Orchestrator → subsystem command payload (``/event``, ``/command``)."""

    event_field: str = "event"
    source_field: str = "source"
    source_value: str = "orchestrator"
    timestamp_field: str = "timestamp"
    include_timestamp: bool = True
    # The object the command belongs to (dotted path allowed). Only set
    # when the orchestrator issues the command *for an object* — global
    # broadcasts (stop, pause) carry no object id.
    object_id_field: str = "object_id"
    # Extra constant fields some subsystems expect (dotted paths allowed).
    static_fields: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "CommandFormat":
        data = data or {}
        return cls(
            event_field=_text(data.get("event_field"), "event"),
            source_field=_text(data.get("source_field"), "source"),
            source_value=_text(data.get("source_value"), "orchestrator"),
            timestamp_field=_text(data.get("timestamp_field"), "timestamp"),
            include_timestamp=_bool(data.get("include_timestamp"), True),
            object_id_field=_text(data.get("object_id_field"), "object_id"),
            static_fields=dict(data.get("static_fields") or {}),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_field": self.event_field,
            "source_field": self.source_field,
            "source_value": self.source_value,
            "timestamp_field": self.timestamp_field,
            "include_timestamp": self.include_timestamp,
            "object_id_field": self.object_id_field,
            "static_fields": dict(self.static_fields),
        }

    def build(
        self,
        event: str,
        data: Optional[Dict[str, Any]] = None,
        timestamp: Optional[str] = None,
        object_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build the outgoing payload for a command event.

        Context ``data`` merges at the top level (as it always did); the
        format's own fields win, so an event cannot be overwritten by a
        stray context key. ``object_id`` (the object the command belongs
        to) is set last through the configured ``object_id_field``.
        """
        payload: Dict[str, Any] = {}
        for key, value in self.static_fields.items():
            set_path(payload, key, value)
        if self.event_field:
            set_path(payload, self.event_field, event)
        if self.source_field:
            set_path(payload, self.source_field, self.source_value)
        if self.include_timestamp and self.timestamp_field:
            set_path(
                payload,
                self.timestamp_field,
                timestamp or datetime.now(timezone.utc).isoformat(),
            )
        for key, value in (data or {}).items():
            payload[key] = value
        if object_id is not None and self.object_id_field:
            set_path(payload, self.object_id_field, object_id)
        return payload

    def extract_event(self, payload: Any) -> Optional[str]:
        value = get_path(payload, self.event_field)
        return str(value) if value is not None else None

    def extract_object_id(self, payload: Any) -> Optional[str]:
        value = get_path(payload, self.object_id_field)
        return str(value) if value is not None else None


@dataclass
class StatusFormat:
    """Subsystem → orchestrator state report (``/status``)."""

    state_field: str = "state"
    ready_field: str = "ready"
    paused_field: str = "paused"
    name_field: str = "name"

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "StatusFormat":
        data = data or {}
        return cls(
            state_field=_text(data.get("state_field"), "state"),
            ready_field=_text(data.get("ready_field"), "ready"),
            paused_field=_text(data.get("paused_field"), "paused"),
            name_field=_text(data.get("name_field"), "name"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state_field": self.state_field,
            "ready_field": self.ready_field,
            "paused_field": self.paused_field,
            "name_field": self.name_field,
        }

    def build(
        self,
        state: str,
        name: Optional[str] = None,
        ready: Optional[bool] = None,
        paused: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Build a status report payload (used by the simulated subsystems)."""
        payload: Dict[str, Any] = {}
        if self.state_field:
            set_path(payload, self.state_field, state)
        if self.name_field and name is not None:
            set_path(payload, self.name_field, name)
        if self.ready_field and ready is not None:
            set_path(payload, self.ready_field, ready)
        if self.paused_field and paused is not None:
            set_path(payload, self.paused_field, paused)
        return payload

    def extract_state(self, payload: Any) -> Optional[str]:
        value = get_path(payload, self.state_field)
        return str(value) if value is not None else None

    def extract_ready(self, payload: Any) -> Optional[bool]:
        value = get_path(payload, self.ready_field)
        return bool(value) if value is not None else None

    def extract_paused(self, payload: Any) -> Optional[bool]:
        value = get_path(payload, self.paused_field)
        return bool(value) if value is not None else None

    def extract_name(self, payload: Any) -> Optional[str]:
        value = get_path(payload, self.name_field)
        return str(value) if value is not None else None


@dataclass
class ResponseFormat:
    """Subsystem → orchestrator response message (``/response``)."""

    type_field: str = "type"
    objects_field: str = "objects"
    count_field: str = "count"
    # Points reports (R5 `points` roles): the object the points belong to
    # and the coordinate list itself.
    object_id_field: str = "object_id"
    points_field: str = "points"

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "ResponseFormat":
        data = data or {}
        return cls(
            type_field=_text(data.get("type_field"), "type"),
            objects_field=_text(data.get("objects_field"), "objects"),
            count_field=_text(data.get("count_field"), "count"),
            object_id_field=_text(data.get("object_id_field"), "object_id"),
            points_field=_text(data.get("points_field"), "points"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type_field": self.type_field,
            "objects_field": self.objects_field,
            "count_field": self.count_field,
            "object_id_field": self.object_id_field,
            "points_field": self.points_field,
        }

    def build(
        self,
        report_type: str,
        objects: Optional[list] = None,
        count: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Build a response payload (used for debug defaults + sim reports)."""
        payload: Dict[str, Any] = {}
        if self.type_field:
            set_path(payload, self.type_field, report_type)
        if objects is not None and self.objects_field:
            set_path(payload, self.objects_field, list(objects))
        if count is not None and self.count_field:
            set_path(payload, self.count_field, count)
        return payload

    def extract(self, payload: Any) -> Tuple[Optional[str], list, Optional[int]]:
        """Extract ``(type, objects, count)`` from a response payload.

        ``objects`` is always a list (empty when absent or malformed); the
        count falls back to ``len(objects)`` when the payload omits it.
        """
        report_type = get_path(payload, self.type_field)
        report_type = str(report_type) if report_type is not None else None
        raw_objects = get_path(payload, self.objects_field)
        objects = list(raw_objects) if isinstance(raw_objects, list) else []
        raw_count = get_path(payload, self.count_field)
        count: Optional[int]
        try:
            count = int(raw_count) if raw_count is not None else len(objects)
        except (TypeError, ValueError):
            count = len(objects)
        return report_type, objects, count

    def build_points(
        self,
        report_type: str,
        object_id: Optional[str],
        points: List[Any],
    ) -> Dict[str, Any]:
        """Build a points-report payload (sim reports + debug defaults).

        ``points`` is a list of coordinate lists (``[x, y, z(, r, p, y)]``).
        """
        payload: Dict[str, Any] = {}
        if self.type_field:
            set_path(payload, self.type_field, report_type)
        if object_id is not None and self.object_id_field:
            set_path(payload, self.object_id_field, object_id)
        if self.points_field:
            set_path(payload, self.points_field, list(points))
        return payload

    def extract_points(self, payload: Any) -> Tuple[Optional[str], List[Any]]:
        """Extract ``(object_id, points)`` from a points-report payload.

        ``points`` accepts both shapes a real system may send: a plain list
        of coordinate lists, or a list of ``{label, coordinates}`` objects
        (labels are kept by the orchestrator's points store).
        """
        raw_id = get_path(payload, self.object_id_field)
        object_id = str(raw_id) if raw_id is not None else None
        raw_points = get_path(payload, self.points_field)
        points = list(raw_points) if isinstance(raw_points, list) else []
        return object_id, points


@dataclass
class PointsCommandFormat:
    """Orchestrator → consumer push of an object's stored points.

    Published on the consumer's dedicated points topic
    (``<mqtt_topic>/points``) right after the command that needs them
    (R12): the payload names the command it belongs to, the object and the
    selected coordinates, in order.
    """

    event_field: str = "event"
    object_id_field: str = "object_id"
    points_field: str = "points"
    source_field: str = "source"
    source_value: str = "orchestrator"
    timestamp_field: str = "timestamp"
    include_timestamp: bool = True

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "PointsCommandFormat":
        data = data or {}
        return cls(
            event_field=_text(data.get("event_field"), "event"),
            object_id_field=_text(data.get("object_id_field"), "object_id"),
            points_field=_text(data.get("points_field"), "points"),
            source_field=_text(data.get("source_field"), "source"),
            source_value=_text(data.get("source_value"), "orchestrator"),
            timestamp_field=_text(data.get("timestamp_field"), "timestamp"),
            include_timestamp=_bool(data.get("include_timestamp"), True),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_field": self.event_field,
            "object_id_field": self.object_id_field,
            "points_field": self.points_field,
            "source_field": self.source_field,
            "source_value": self.source_value,
            "timestamp_field": self.timestamp_field,
            "include_timestamp": self.include_timestamp,
        }

    def build(
        self,
        event: str,
        object_id: str,
        points: List[Any],
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        if self.event_field:
            set_path(payload, self.event_field, event)
        if self.object_id_field:
            set_path(payload, self.object_id_field, object_id)
        if self.points_field:
            set_path(payload, self.points_field, list(points))
        if self.source_field:
            set_path(payload, self.source_field, self.source_value)
        if self.include_timestamp and self.timestamp_field:
            set_path(payload, self.timestamp_field, datetime.now(timezone.utc).isoformat())
        return payload


@dataclass
class MessageFormat:
    """All message formats of one subsystem."""

    command: CommandFormat = field(default_factory=CommandFormat)
    hardware_command: CommandFormat = field(
        default_factory=lambda: CommandFormat(include_timestamp=False)
    )
    status: StatusFormat = field(default_factory=StatusFormat)
    response: ResponseFormat = field(default_factory=ResponseFormat)
    points_command: PointsCommandFormat = field(default_factory=PointsCommandFormat)

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "MessageFormat":
        data = data or {}
        hardware_raw = data.get("hardware_command")
        hardware = (
            CommandFormat.from_dict(hardware_raw)
            if isinstance(hardware_raw, dict)
            # Not declared: like a command but with no timestamp (the
            # historical pause/resume payload).
            else CommandFormat(include_timestamp=False)
        )
        return cls(
            command=CommandFormat.from_dict(data.get("command")),
            hardware_command=hardware,
            status=StatusFormat.from_dict(data.get("status")),
            response=ResponseFormat.from_dict(data.get("response")),
            points_command=PointsCommandFormat.from_dict(data.get("points_command")),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "command": self.command.to_dict(),
            "hardware_command": self.hardware_command.to_dict(),
            "status": self.status.to_dict(),
            "response": self.response.to_dict(),
            "points_command": self.points_command.to_dict(),
        }


class MessageFormatSet:
    """The ``mqtt_formats.yaml`` document: a default + per-subsystem overrides."""

    def __init__(
        self,
        default: Optional[Dict[str, Any]] = None,
        subsystems: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> None:
        self._default_data: Dict[str, Any] = dict(default or {})
        self._subsystems: Dict[str, Dict[str, Any]] = dict(subsystems or {})
        self.default = MessageFormat.from_dict(self._default_data)
        self._cache: Dict[str, MessageFormat] = {}

    # -- Loading -----------------------------------------------------------
    @classmethod
    def from_yaml(cls, path: str | Path) -> "MessageFormatSet":
        with open(path, "r") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            logger.warning("Ignoring malformed MQTT formats file: %s", path)
            data = {}
        subsystems = data.get("subsystems") or {}
        if not isinstance(subsystems, dict):
            subsystems = {}
        return cls(default=data.get("default") or {}, subsystems=subsystems)

    @classmethod
    def load(cls, config_dir: str | Path) -> "MessageFormatSet":
        """Load ``<config_dir>/mqtt_formats.yaml``; built-in defaults if absent."""
        path = Path(config_dir) / DEFAULT_FORMATS_FILENAME
        if path.exists():
            return cls.from_yaml(path)
        logger.warning(
            "No MQTT formats file at %s – using built-in default payloads", path
        )
        return cls()

    # -- Lookup ------------------------------------------------------------
    def for_subsystem(self, name: str) -> MessageFormat:
        """The effective format of a subsystem (defaults + its overrides)."""
        if name in self._cache:
            return self._cache[name]
        override = self._subsystems.get(name) or {}
        merged = _deep_merge(self._default_data, override) if override else self._default_data
        fmt = MessageFormat.from_dict(merged)
        self._cache[name] = fmt
        return fmt

    def to_dict(self) -> Dict[str, Any]:
        return {
            "default": self._default_data,
            "subsystems": self._subsystems,
        }
