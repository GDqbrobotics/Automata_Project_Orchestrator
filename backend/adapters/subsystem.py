"""Subsystem adapters – wrappers around MQTT communication with each subsystem.

Adapters are created dynamically from the subsystems.yaml configuration.
Each adapter knows its MQTT topics, current state, and metadata.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from backend.domain.models import SubsystemConfig, SubsystemInfo, SubsystemState

logger = logging.getLogger(__name__)


@dataclass
class SubsystemAdapter:
    """Generic adapter for a subsystem, created from config."""

    name: str
    mqtt_client: Any
    display_name: str = ""
    subsystem_type: str = "generic"
    initial_state: str = "ready"
    required: bool = True
    metadata: Dict[str, Any] = field(default_factory=dict)
    state: SubsystemState = SubsystemState.OFFLINE
    error: Optional[str] = None

    @property
    def info(self) -> SubsystemInfo:
        return SubsystemInfo(
            name=self.name,
            description=self.display_name or self.name,
            topic_prefix=f"automata/subsystem/{self.name}",
        )

    @property
    def config(self) -> SubsystemConfig:
        return SubsystemConfig(
            name=self.name,
            display_name=self.display_name or self.name,
            type=self.subsystem_type,
            initial_state=self.initial_state,
            required=self.required,
            metadata=self.metadata,
        )

    def update_state(self, state: SubsystemState, error: Optional[str] = None) -> None:
        self.state = state
        if error:
            self.error = error

    def send_command(self, command: str, payload: Optional[Dict[str, Any]] = None) -> None:
        topic = f"automata/subsystem/{self.name}/command"
        self.mqtt_client.publish(topic, payload or {})

    def is_ready(self) -> bool:
        return self.state == SubsystemState.READY


class SubsystemAdapterFactory:
    """Factory that creates SubsystemAdapter instances from config."""

    def __init__(self, mqtt_client: Any, subsystems_config: List[SubsystemConfig]) -> None:
        self.mqtt_client = mqtt_client
        self.subsystems_config = subsystems_config
        self.adapters: Dict[str, SubsystemAdapter] = {}
        self._build_adapters()

    def _build_adapters(self) -> None:
        """Create an adapter for each configured subsystem."""
        for cfg in self.subsystems_config:
            self.adapters[cfg.name] = SubsystemAdapter(
                name=cfg.name,
                mqtt_client=self.mqtt_client,
                display_name=cfg.display_name,
                subsystem_type=cfg.type,
                initial_state=cfg.initial_state,
                required=cfg.required,
                metadata=cfg.metadata,
            )
            logger.info("Adapter created: %s (%s)", cfg.name, cfg.type)

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
