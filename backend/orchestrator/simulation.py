"""Simulation mode – simulates subsystems responding to workflow commands.

The simulation reads subsystem configuration from YAML to know which subsystems
to simulate and their properties. This keeps the simulation in sync with the
orchestrator's configuration.
"""

from __future__ import annotations

import json
import logging
import random
import time
from typing import Any, Dict, List, Optional

import paho.mqtt.client as mqtt

from backend.domain.models import BackendEvent, SubsystemConfig, SubsystemState, WorkflowState

logger = logging.getLogger(__name__)


# Default analysis steps per subsystem – used when config doesn't specify steps
DEFAULT_STEPS: Dict[str, str] = {
    "vision": "object_detection",
    "robot_first": "pick_and_place",
    "robot_second": "measurement_positioning",
    "rotating_platform": "position_rotation",
    "xrf": "xrf_measurement",
    "raman": "raman_measurement",
    "imaging_3di_hsi": "3di_hsi_imaging",
    "ai_analysis": "ai_analysis",
    "cloud_database": "cloud_upload",
}


class WorkflowSimulator:
    """Simulates subsystems responding to the workflow state machine."""

    def __init__(
        self,
        orchestrator: Any,
        broker_host: str = "localhost",
        broker_port: int = 1883,
        num_objects: int = 3,
        subsystems_config: List[SubsystemConfig] | None = None,
    ) -> None:
        self.orchestrator = orchestrator
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.num_objects = num_objects
        self.subsystems_config = subsystems_config or []
        self._client: Optional[mqtt.Client] = None
        self._running = False

    def start(self) -> None:
        """Start the MQTT simulation client."""
        self._running = True
        self._client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
        self._client.connect(self.broker_host, self.broker_port, 60)
        self._client.loop_start()
        logger.info("Simulation MQTT client started")
        logger.info(
            "Simulating %d subsystems: %s",
            len(self.subsystems_config),
            ", ".join(cfg.name for cfg in self.subsystems_config),
        )

    def stop(self) -> None:
        """Stop the simulation."""
        self._running = False
        if self._client:
            self._client.loop_stop()
            self._client.disconnect()
        logger.info("Simulation stopped")

    def simulate_full_workflow(self) -> None:
        """Run the full simulated workflow.

        Uses the subsystem configuration to determine which subsystems to simulate
        and in what order.
        """
        logger.info(
            "Starting full workflow simulation with %d objects and %d subsystems",
            self.num_objects,
            len(self.subsystems_config),
        )

        # Phase 1: All subsystems become ready
        self._simulate_subsystems_ready()

        # Phase 2: Simulate the analysis workflow for each object
        for i in range(self.num_objects):
            if not self._running:
                break
            object_id = f"object_{i+1:03d}"
            logger.info("Simulating analysis for %s", object_id)
            self._simulate_object_analysis(object_id, i + 1, self.num_objects)

        # Phase 3: Complete
        if self._running:
            self._simulate_completion()

    def _get_subsystem_names(self) -> List[str]:
        """Get the list of subsystem names from config."""
        if self.subsystems_config:
            return [cfg.name for cfg in self.subsystems_config]
        # Fallback to orchestrator's adapters if no config
        return list(self.orchestrator.adapters.keys())

    def _simulate_subsystems_ready(self) -> None:
        """Simulate all subsystems becoming ready."""
        subsystem_names = self._get_subsystem_names()

        for name in subsystem_names:
            if not self._running:
                return
            topic = f"automata/subsystem/{name}/status"
            payload = {"state": "ready", "timestamp": time.time()}
            self._publish_mqtt(topic, payload)
            logger.info("Simulation: subsystem %s -> ready", name)
            time.sleep(0.1)

        # Update orchestrator's subsystem states
        for name in subsystem_names:
            if name in self.orchestrator.adapters:
                self.orchestrator.adapters[name].update_state(SubsystemState.READY)

    def _simulate_object_analysis(
        self,
        object_id: str,
        current: int,
        total: int,
    ) -> None:
        """Simulate the analysis of a single object.

        Uses the configured subsystems to determine the analysis steps.
        """
        self.orchestrator.set_current_object(object_id)
        self.orchestrator.set_total_objects(total)

        # Get ordered list of subsystems to simulate
        # Required subsystems first, then optional ones
        required_names = self.orchestrator.required_subsystem_names
        optional_names = [
            name for name in self._get_subsystem_names()
            if name not in required_names
        ]
        ordered_subsystems = required_names + optional_names

        for subsystem_name in ordered_subsystems:
            if not self._running:
                break

            # Determine the step name from config or default
            step = DEFAULT_STEPS.get(subsystem_name, "processing")
            topic = f"automata/subsystem/{subsystem_name}/status"

            self._publish_mqtt(
                topic,
                {
                    "state": "busy",
                    "object": object_id,
                    "step": step,
                    "timestamp": time.time(),
                },
            )
            # Simulate processing time (shorter for optional subsystems)
            is_required = subsystem_name in required_names
            delay = random.uniform(0.15, 0.35) if is_required else random.uniform(0.05, 0.15)
            time.sleep(delay)

            # Return to ready
            self._publish_mqtt(
                topic,
                {
                    "state": "ready",
                    "object": object_id,
                    "step": "complete",
                    "timestamp": time.time(),
                },
            )

        # Update progress
        self.orchestrator._progress = current
        self.orchestrator._fire_frontend_event(
            "progress_updated",
            {"progress": current, "total": total},
        )

    def _simulate_completion(self) -> None:
        """Simulate workflow completion via proper state machine transitions.

        Transitions: running -> stopping (stop_requested) -> stopped (stopped)
        """
        logger.info("Simulation: workflow complete")

        # Transition: running -> stopping via stop_requested event
        try:
            self.orchestrator.fire_event("stop_requested")
            time.sleep(0.3)
        except Exception as exc:
            logger.warning("Failed to fire stop_requested: %s", exc)

        # Transition: stopping -> stopped via stopped event
        try:
            self.orchestrator.fire_event("stopped")
            time.sleep(0.3)
        except Exception as exc:
            logger.warning("Failed to fire stopped: %s", exc)

    def _publish_mqtt(self, topic: str, payload: Dict[str, Any]) -> None:
        """Publish a simulated MQTT message."""
        if self._client:
            self._client.publish(topic, json.dumps(payload), qos=1)
        # Also update the orchestrator's adapter
        parts = topic.split("/")
        if len(parts) >= 3:
            subsystem_name = parts[2]
            if subsystem_name in self.orchestrator.adapters:
                state_str = payload.get("state", "ready")
                try:
                    state = SubsystemState(state_str)
                except ValueError:
                    state = SubsystemState.READY
                self.orchestrator.adapters[subsystem_name].update_state(state)
