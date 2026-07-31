"""Simulation mode – simulates subsystems responding to workflow commands.

The simulation reads subsystem configuration from individual YAML files
to know which subsystems to simulate, their state machines, and how to
drive them through transitions.
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


class WorkflowSimulator:
    """Simulates subsystems responding to the workflow state machine.

    Each subsystem's state machine is driven through its transitions
    to simulate realistic behavior.
    """

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

        Drives each subsystem's state machine through transitions
        to simulate realistic behavior for each object.
        """
        logger.info(
            "Starting full workflow simulation with %d objects and %d subsystems",
            self.num_objects,
            len(self.subsystems_config),
        )

        # Phase 1: All subsystems become ready (drive state machines to ready)
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
        """Simulate all subsystems becoming ready by driving their state machines.

        Each subsystem starts in 'initializing' state and must transition
        to 'ready' via the 'init_complete' event.
        """
        subsystem_names = self._get_subsystem_names()

        # Phase 1: Initialize all subsystems (initializing -> ready)
        logger.info("Simulation: Initializing all subsystems...")
        for name in subsystem_names:
            if not self._running:
                return

            adapter = self.orchestrator.adapters.get(name)
            if adapter and adapter.state_machine:
                current = adapter.state_machine.current_state
                if current == "initializing":
                    try:
                        # Use orchestrator's fire_subsystem_transition to ensure
                        # frontend notifications happen correctly
                        self.orchestrator.fire_subsystem_transition(name, "init_complete")
                        logger.info("Simulation: subsystem %s initialized -> ready", name)
                    except Exception as exc:
                        logger.warning("Failed to initialize %s: %s", name, exc)

                # Update orchestrator's adapter state
                if name in self.orchestrator.adapters:
                    self.orchestrator.adapters[name].update_state(SubsystemState.READY)

                time.sleep(0.1)

        # Phase 2: Simulate analysis for each object
        logger.info("Simulation: All subsystems initialized, starting analysis...")
        for i in range(self.num_objects):
            if not self._running:
                break
            object_id = f"object_{i+1:03d}"
            logger.info("Simulating analysis for %s", object_id)
            self._simulate_object_analysis(object_id, i + 1, self.num_objects)

        # Phase 3: Complete
        if self._running:
            self._simulate_completion()

    def _simulate_object_analysis(
        self,
        object_id: str,
        current: int,
        total: int,
    ) -> None:
        """Simulate the analysis of a single object.

        Drives each subsystem's state machine through its transitions
        to simulate realistic behavior, and fires corresponding
        object state machine transitions.
        """
        self.orchestrator.set_current_object(object_id)
        self.orchestrator.set_total_objects(total)

        # Create the object state machine
        self.orchestrator.create_object(object_id)
        time.sleep(0.1)

        # Get ordered list of subsystems to simulate
        required_names = self.orchestrator.required_subsystem_names
        optional_names = [
            name for name in self._get_subsystem_names()
            if name not in required_names
        ]
        ordered_subsystems = required_names + optional_names

        # Subsystem -> object event mapping
        # Maps subsystem names to the object event that fires when they complete
        subsystem_event_map = {
            "vision": "object_picked",
            "robot_first": None,  # Handled separately below
            "imaging_3di_hsi": "imaging_capture_complete",
            "rotating_platform": None,  # Handled separately below
            "xrf": "measurements_complete",
            "raman": "measurements_complete",
            "ai_analysis": "measurements_complete",
            "cloud_database": None,
        }

        for subsystem_name in ordered_subsystems:
            if not self._running:
                break

            adapter = self.orchestrator.adapters.get(subsystem_name)
            if not adapter or not adapter.state_machine:
                # No state machine, just publish a status update
                topic = f"automata/subsystem/{subsystem_name}/status"
                self._publish_mqtt(
                    topic,
                    {
                        "state": "busy",
                        "object": object_id,
                        "step": "processing",
                        "timestamp": time.time(),
                    },
                )
                time.sleep(random.uniform(0.2, 0.5))
                self._publish_mqtt(
                    topic,
                    {
                        "state": "ready",
                        "object": object_id,
                        "step": "complete",
                        "timestamp": time.time(),
                    },
                )
                continue

            # Determine the next state to transition to
            current_state = adapter.state_machine.current_state
            available = adapter.state_machine.get_available_transitions()

            # Try to find a useful transition
            next_event = None
            next_state = None

            # If we're in ready, try to start
            if current_state == "ready":
                if "start" in available:
                    next_event = "start"
                    next_state = available["start"]
                elif "start" in adapter.state_machine.config.states.get("ready", adapter.state_machine.config.states.get("idle", type('obj', (), {'transitions': []})())).transitions if hasattr(adapter.state_machine.config.states.get("ready"), 'transitions') else []:
                    for t in adapter.state_machine.config.states.get("ready", type('obj', (), {'transitions': []})).transitions:
                        if t.event == "start":
                            next_event = "start"
                            next_state = t.target
                            break

            # If we're in a running/processing state, try to complete or stop
            elif current_state in ("running", "measuring", "capturing", "analyzing", "uploading", "tool_changing"):
                if "complete" in available:
                    next_event = "complete"
                    next_state = available["complete"]
                elif "stop" in available:
                    next_event = "stop"
                    next_state = available["stop"]

            # If we're in paused, resume
            elif current_state == "paused":
                if "resume" in available:
                    next_event = "resume"
                    next_state = available["resume"]

            # If we're in error, reset
            elif current_state == "error":
                if "reset" in available:
                    next_event = "reset"
                    next_state = available["reset"]

            # If we're in stopped, start again
            elif current_state == "stopped":
                if "start" in available:
                    next_event = "start"
                    next_state = available["start"]

            if next_event and next_state:
                try:
                    # Use orchestrator's fire_subsystem_transition to ensure
                    # frontend notifications happen correctly
                    self.orchestrator.fire_subsystem_transition(subsystem_name, next_event)
                except Exception as exc:
                    logger.warning(
                        "Failed to fire '%s' on %s: %s", next_event, subsystem_name, exc,
                    )
                    continue

            # Update adapter state
            new_state = adapter.state_machine.current_state
            if subsystem_name in self.orchestrator.adapters:
                self.orchestrator.adapters[subsystem_name].update_state(SubsystemState.READY)

            # Simulate processing time
            is_required = subsystem_name in required_names
            delay = random.uniform(0.3, 0.6) if is_required else random.uniform(0.1, 0.3)
            time.sleep(delay)

            # Fire the corresponding object state transition
            obj_event = subsystem_event_map.get(subsystem_name)
            if obj_event:
                # Special handling: robot_first handles multiple transitions
                if subsystem_name == "robot_first":
                    # Check current object state to determine next event
                    obj_state = self.orchestrator.object_manager.get_object_state(object_id)
                    if obj_state == "identified_in_box":
                        # Robot first picks up the object
                        self.orchestrator.fire_object_transition(object_id, "object_picked")
                        time.sleep(0.1)
                        # Robot first places it on the disk
                        self.orchestrator.fire_object_transition(object_id, "object_placed_on_disk")
                        time.sleep(0.1)
                    elif obj_state in ("XRF_RAMAN_analysis", "object_going_first_position"):
                        # Robot first returns the object to the box
                        self.orchestrator.fire_object_transition(object_id, "object_returned_to_box")
                        time.sleep(0.1)
                elif subsystem_name == "rotating_platform":
                    # Check current object state to determine rotation direction
                    obj_state = self.orchestrator.object_manager.get_object_state(object_id)
                    if obj_state == "HSI_3DI_analysis":
                        # Rotate to second position for measurements
                        self.orchestrator.fire_object_transition(object_id, "platform_rotated_to_second")
                        time.sleep(0.1)
                    elif obj_state == "XRF_RAMAN_analysis":
                        # Rotate back to first position
                        self.orchestrator.fire_object_transition(object_id, "platform_rotated_to_first")
                        time.sleep(0.1)
                elif subsystem_name == "cloud_database":
                    # Cloud upload is the final step
                    self.orchestrator.fire_object_transition(object_id, "measurements_complete")
                    time.sleep(0.1)
                else:
                    self.orchestrator.fire_object_transition(object_id, obj_event)

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
            time.sleep(0.1)
        except Exception as exc:
            logger.warning("Failed to fire stop_requested: %s", exc)

        # Transition: stopping -> stopped via stopped event
        try:
            self.orchestrator.fire_event("stopped")
            time.sleep(0.1)
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
