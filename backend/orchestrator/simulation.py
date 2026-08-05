"""Simulation mode – simulates subsystems responding to workflow commands.

The simulation reads subsystem configuration from individual YAML files
to know which subsystems to simulate, their state machines, and how to
drive them through transitions.

With the new architecture:
  - Object state machine events are scoped (e.g., "robot_first/pick")
  - The orchestrator routes these to the appropriate subsystem SMs
  - The simulation drives subsystem SMs through their transitions
  - Subsystem SMs are the boundary layer (MQTT publishing)
"""

from __future__ import annotations

import json
import logging
import random
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import paho.mqtt.client as mqtt
import yaml

from backend.domain.models import (
    BackendEvent,
    GlobalConfig,
    SubsystemConfig,
    SubsystemState,
    WorkflowState,
)

# Placeholder for the project root
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

logger = logging.getLogger(__name__)


def _parse_subsystem_prefix(name: str):
    """Parse a scoped name like 'robot_first/pick'."""
    if "/" in name:
        parts = name.split("/", 1)
        return parts[0], parts[1]
    return "", name


class WorkflowSimulator:
    """Simulates subsystems responding to the workflow state machine.

    Uses the object.yaml config to determine the sequence of
    scoped events for each object, and drives subsystem state machines
    through their transitions.
    """

    def __init__(
        self,
        orchestrator: Any,
        broker_host: str = "localhost",
        broker_port: int = 1883,
        num_objects: int = 3,
        subsystems_config: List[SubsystemConfig] | None = None,
        global_config: GlobalConfig | None = None,
    ) -> None:
        self.orchestrator = orchestrator
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.num_objects = num_objects
        self.subsystems_config = subsystems_config or []
        self._global_config = global_config or GlobalConfig()
        self._client: Optional[mqtt.Client] = None
        self._running = False

        # Derived event chain (built from config at runtime)
        self._event_chain: List[Dict[str, Any]] = []
        self._default_delay: float = 0.4
        self._build_event_chain()

    def _build_event_chain(self) -> None:
        """Build the simulation event chain from object.yaml.

        Reads the object state machine from object.yaml to get the state
        transition sequence, and extracts the scoped events (e.g.,
        "robot_first/pick") to know which subsystems to drive.
        """
        object_config_path = (
            _PROJECT_ROOT / "backend" / "config" / "object.yaml"
        )
        if not object_config_path.exists():
            logger.warning(
                "object.yaml not found at %s, using default event chain",
                object_config_path,
            )
            self._event_chain = self._default_event_chain()
            return

        with open(object_config_path, "r") as f:
            object_data = yaml.safe_load(f)

        states = object_data.get("states", {})
        chain: List[Dict[str, Any]] = []

        # Get state order from global config or derive from YAML order
        state_order = (
            self._global_config.object_state_order
            if self._global_config.object_state_order
            else list(states.keys())
        )

        for state_name in state_order:
            state_data = states.get(state_name, {})
            for transition in state_data.get("transitions", []):
                event_action = self._get_event_action(transition)
                if not event_action:
                    continue

                target_state = transition.get("target")
                # Parse scoped events (e.g., "robot_first/pick")
                scope, local_event = _parse_subsystem_prefix(event_action)

                if scope and local_event:
                    chain.append({
                        "object_event": event_action,
                        "subsystem": scope,
                        "subsystem_event": local_event,
                        "object_transition": target_state,
                        "delay": self._default_delay,
                    })
                else:
                    # Unscoped event – skip (handled by orchestrator internally)
                    logger.debug(
                        "Skipping unscoped event '%s' in object transition",
                        event_action,
                    )

        self._event_chain = chain if chain else self._default_event_chain()
        logger.info("Simulation event chain built with %d steps", len(self._event_chain))

    @staticmethod
    def _get_event_action(transition: Dict[str, Any]) -> Optional[str]:
        """Extract the event action name from a transition."""
        for action in transition.get("actions", []):
            if isinstance(action, dict) and "event" in action:
                return action["event"]
            elif isinstance(action, str) and action.startswith("event:"):
                return action.split(":", 1)[1].strip()
        return None

    @staticmethod
    def _default_event_chain() -> List[Dict[str, Any]]:
        """Fallback event chain when config files are missing."""
        return [
            {
                "object_event": "robot_first/pick",
                "subsystem": "robot_first",
                "subsystem_event": "pick",
                "object_transition": "object_placed_on_disk",
                "delay": 0.4,
            },
            {
                "object_event": "imaging_3di_hsi/capture",
                "subsystem": "imaging_3di_hsi",
                "subsystem_event": "capture",
                "object_transition": "imaging_capture_complete",
                "delay": 0.5,
            },
            {
                "object_event": "rotating_platform/rotate_to_second",
                "subsystem": "rotating_platform",
                "subsystem_event": "rotate_to_second",
                "object_transition": "platform_rotated_to_second",
                "delay": 0.3,
            },
            {
                "object_event": "xrf/measure",
                "subsystem": "xrf",
                "subsystem_event": "measure",
                "object_transition": None,
                "delay": 0.4,
            },
            {
                "object_event": "raman/measure",
                "subsystem": "raman",
                "subsystem_event": "measure",
                "object_transition": "measurements_complete",
                "delay": 0.4,
            },
            {
                "object_event": "rotating_platform/rotate_to_first",
                "subsystem": "rotating_platform",
                "subsystem_event": "rotate_to_first",
                "object_transition": "platform_rotated_to_first",
                "delay": 0.3,
            },
            {
                "object_event": "robot_first/return",
                "subsystem": "robot_first",
                "subsystem_event": "return",
                "object_transition": "object_returned_to_box",
                "delay": 0.4,
            },
            {
                "object_event": "cloud_database/upload",
                "subsystem": "cloud_database",
                "subsystem_event": "upload",
                "object_transition": None,
                "delay": 0.3,
            },
        ]

    def start(self) -> None:
        """Start the MQTT simulation client."""
        self._running = True
        self._client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
        self._client.connect(self.broker_host, self.broker_port, 60)
        self._client.loop_start()
        logger.info("Simulation MQTT client started")

    def stop(self) -> None:
        """Stop the simulation."""
        self._running = False
        if self._client:
            self._client.loop_stop()
            self._client.disconnect()
        logger.info("Simulation stopped")

    def simulate_full_workflow(self) -> None:
        """Run the full simulated workflow.

        Drives each subsystem's state machine through transitions
        to simulate realistic behavior for each object.
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

    def _simulate_subsystems_ready(self) -> None:
        """Simulate all subsystems becoming ready."""
        subsystem_names = [cfg.name for cfg in self.subsystems_config]

        logger.info("Simulation: Initializing all subsystems...")
        for name in subsystem_names:
            if not self._running:
                return

            adapter = self.orchestrator.adapters.get(name)
            if adapter and adapter.state_machine:
                current = adapter.state_machine.current_state
                if current == "initializing":
                    try:
                        self.orchestrator.fire_subsystem_transition(name, "init_complete")
                        logger.info("Simulation: subsystem %s initialized -> ready", name)
                    except Exception as exc:
                        logger.warning("Failed to initialize %s: %s", name, exc)

                if name in self.orchestrator.adapters:
                    self.orchestrator.adapters[name].update_state(SubsystemState.READY)

                time.sleep(0.1)

        # Transition to running state
        if self._running:
            logger.info("Simulation: All subsystems initialized, starting analysis...")

    def _simulate_object_analysis(
        self,
        object_id: str,
        current: int,
        total: int,
    ) -> None:
        """Simulate the analysis of a single object."""
        self.orchestrator.set_current_object(object_id)
        self.orchestrator.set_total_objects(total)

        # Create the object state machine
        self.orchestrator.create_object(object_id)
        time.sleep(0.1)

        # Kick off the chain by firing the initial object transition
        self.orchestrator.fire_object_transition(object_id, "object_picked")
        time.sleep(0.1)

        # Process each step from the event chain
        for step in self._event_chain:
            if not self._running:
                break

            subsystem_name = step.get("subsystem")
            subsystem_event = step.get("subsystem_event", "start")
            delay = step.get("delay", self._default_delay)
            obj_transition = step.get("object_transition")

            self._simulate_subsystem_step(
                subsystem_name, subsystem_event, delay, object_id,
            )

            # Fire the object transition if this step specifies one
            if obj_transition:
                self.orchestrator.fire_object_transition(object_id, obj_transition)
                time.sleep(0.1)

        # Update progress
        self.orchestrator._progress = current
        self.orchestrator._fire_frontend_event(
            "progress_updated",
            {"progress": current, "total": total},
        )

    def _simulate_subsystem_step(
        self,
        subsystem_name: str,
        subsystem_event: str,
        delay: float,
        object_id: str,
    ) -> None:
        """Simulate a subsystem step: event → process → complete."""
        adapter = self.orchestrator.adapters.get(subsystem_name)
        if not adapter or not adapter.state_machine:
            # No state machine, just simulate
            time.sleep(random.uniform(0.2, 0.5))
            return

        current_state = adapter.state_machine.current_state
        available = adapter.state_machine.get_available_transitions()

        # Determine the next event to fire
        next_event = None

        if current_state == "ready":
            if subsystem_event in available:
                next_event = subsystem_event
        elif current_state in ("picking", "returning", "rotating", "measuring",
                                "capturing", "analyzing", "uploading"):
            if "complete" in available:
                next_event = "complete"

        if next_event:
            try:
                self.orchestrator.fire_subsystem_transition(subsystem_name, next_event)
            except Exception as exc:
                logger.warning(
                    "Failed to fire '%s' on %s: %s", next_event, subsystem_name, exc,
                )

        # Simulate processing time
        time.sleep(delay)

    def _simulate_completion(self) -> None:
        """Simulate workflow completion via proper state machine transitions.

        After completion, the workflow transitions:
          running -> stopping -> stopped -> idle -> starting -> running

        This creates a clean cycle so the operator can see the results
        and start a new workflow immediately.
        """
        logger.info("Simulation: workflow complete")

        # Phase 1: Stop the workflow (running -> stopping -> stopped)
        try:
            self.orchestrator.fire_event("stop_requested")
            time.sleep(0.2)
        except Exception as exc:
            logger.warning("Failed to fire stop_requested: %s", exc)

        try:
            self.orchestrator.fire_event("stopped")
            time.sleep(0.2)
        except Exception as exc:
            logger.warning("Failed to fire stopped: %s", exc)

        # Phase 2: Reset to idle (stopped -> idle)
        try:
            self.orchestrator.fire_event("reset_requested")
            time.sleep(0.2)
        except Exception as exc:
            logger.warning("Failed to fire reset_requested: %s", exc)

        # Phase 3: Auto-start a new workflow (idle -> starting -> running)
        logger.info("Simulation: auto-starting new workflow cycle")
        try:
            # Reset workflow state before starting (clears objects, progress)
            self.orchestrator._reset_workflow_state()
            self.orchestrator.fire_event("start_requested")
            time.sleep(0.2)
        except Exception as exc:
            logger.warning("Failed to fire start_requested: %s", exc)

        # If simulation mode is enabled, restart the full simulation
        if self._running and self.orchestrator._use_simulation:
            logger.info("Simulation: restarting full workflow cycle")
            # Reset the simulator for the next cycle
            self._build_event_chain()  # Rebuild in case config changed
            self.simulate_full_workflow()

    def _publish_mqtt(self, topic: str, payload: Dict[str, Any]) -> None:
        """Publish a simulated MQTT message."""
        if self._client:
            self._client.publish(topic, json.dumps(payload), qos=1)
