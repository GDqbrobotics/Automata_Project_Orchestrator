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


class WorkflowSimulator:
    """Simulates subsystems responding to the workflow state machine.

    Uses the workflow_actions.yaml config to determine the sequence of
    subsystem commands for each object, and the object.yaml config to
    determine the object state transitions.
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
        """Build the simulation event chain from object.yaml + workflow_actions.yaml.

        Reads the object state machine from object.yaml to get the state
        transition sequence, and the workflow actions from workflow_actions.yaml
        to know which subsystem commands to simulate for each event.

        For multi-subsystem events (like trigger_measurements), all subsystems
        are simulated before the object transition fires.
        """
        # Load object.yaml to get the state transition order
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

        # Load workflow_actions.yaml
        actions_path = (
            _PROJECT_ROOT / "backend" / "config" / "workflow_actions.yaml"
        )
        actions: Dict[str, Any] = {}
        if actions_path.exists():
            with open(actions_path, "r") as f:
                actions_data = yaml.safe_load(f)
                actions = actions_data.get("event_actions", {})

        # Build the chain by walking through the object states in order
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
                event_name = transition.get("event")
                target_state = transition.get("target")

                # Get the event action from the transition
                event_action = self._get_event_action(transition)
                if not event_action:
                    continue

                # Look up the workflow actions for this event
                if event_action not in actions:
                    logger.warning(
                        "No workflow action found for event '%s', "
                        "simulator will skip this step",
                        event_action,
                    )
                    continue

                action_cmds = actions[event_action]

                # For the final state (object_analyzed), no subsystem transition needed
                if target_state == "object_analyzed":
                    for cmd in action_cmds:
                        chain.append({
                            "object_event": event_action,
                            "subsystem": cmd.get("subsystem", ""),
                            "subsystem_event": "start",
                            "object_transition": None,
                            "delay": self._default_delay,
                        })
                    continue

                # For regular transitions: all subsystem commands share the same
                # object_transition. The transition fires after ALL subsystems complete.
                # We create one step per subsystem, but only the last one fires
                # the object transition.
                for i, cmd in enumerate(action_cmds):
                    is_last = (i == len(action_cmds) - 1)
                    chain.append({
                        "object_event": event_action,
                        "subsystem": cmd.get("subsystem", ""),
                        "subsystem_event": "start",
                        "object_transition": target_state if is_last else None,
                        "delay": self._default_delay,
                    })

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
                "object_event": "trigger_robot_first_pick",
                "subsystem": "robot_first",
                "subsystem_event": "start",
                "object_transition": "object_placed_on_disk",
                "delay": 0.4,
            },
            {
                "object_event": "trigger_imaging_capture",
                "subsystem": "imaging_3di_hsi",
                "subsystem_event": "start",
                "object_transition": "imaging_capture_complete",
                "delay": 0.5,
            },
            {
                "object_event": "trigger_platform_rotate_to_second",
                "subsystem": "rotating_platform",
                "subsystem_event": "start",
                "object_transition": "platform_rotated_to_second",
                "delay": 0.3,
            },
            {
                "object_event": "trigger_measurements",
                "subsystem": "xrf",
                "subsystem_event": "start",
                "object_transition": "measurements_complete",
                "delay": 0.6,
            },
            {
                "object_event": "trigger_platform_rotate_to_first",
                "subsystem": "rotating_platform",
                "subsystem_event": "start",
                "object_transition": "platform_rotated_to_first",
                "delay": 0.3,
            },
            {
                "object_event": "trigger_robot_first_return",
                "subsystem": "robot_first",
                "subsystem_event": "start",
                "object_transition": "object_returned_to_box",
                "delay": 0.4,
            },
            {
                "object_event": "trigger_cloud_upload",
                "subsystem": "cloud_database",
                "subsystem_event": "start",
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

        Uses the config-driven event chain to:
        1. Object state machine fires event → orchestrator triggers subsystem
        2. Subsystem completes → simulation fires object transition
        3. Object state machine fires next event → orchestrator triggers next subsystem
        4. ... repeat until object is analyzed
        """
        self.orchestrator.set_current_object(object_id)
        self.orchestrator.set_total_objects(total)

        # Create the object state machine
        self.orchestrator.create_object(object_id)
        time.sleep(0.1)

        # Kick off the chain by firing the initial object transition
        # The vision system identifies the object → object_picked
        initial_transition = self.orchestrator.fire_object_transition(
            object_id, "object_picked"
        )
        if not initial_transition:
            logger.warning("Failed to fire initial transition for object '%s'", object_id)
            return

        # Process each step from the config-driven event chain
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

            # Fire the object transition only if this step specifies one.
            # For multi-subsystem events, the transition fires after the
            # last subsystem in the group completes.
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
        """Simulate a subsystem step: start → process → complete."""
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
            return

        # Determine the next state to transition to
        current_state = adapter.state_machine.current_state
        available = adapter.state_machine.get_available_transitions()

        # Try to find a useful transition
        next_event = None

        if current_state == "ready":
            if subsystem_event in available:
                next_event = subsystem_event
        elif current_state in ("running", "measuring", "capturing", "analyzing", "uploading", "tool_changing"):
            if "complete" in available:
                next_event = "complete"

        if next_event:
            try:
                self.orchestrator.fire_subsystem_transition(subsystem_name, next_event)
            except Exception as exc:
                logger.warning(
                    "Failed to fire '%s' on %s: %s", next_event, subsystem_name, exc,
                )

        # Update adapter state
        new_state = adapter.state_machine.current_state
        if subsystem_name in self.orchestrator.adapters:
            self.orchestrator.adapters[subsystem_name].update_state(SubsystemState.READY)

        # Simulate processing time
        time.sleep(delay)

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
