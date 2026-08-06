"""Simulation mode – parallel pipeline simulation of the AUTOMATA machine.

Architecture (from PROJECT_CONTEXT):
  - First robot performs steps 12-4-5-6 in PARALLEL with second robot
    performing steps 9-10.
  - This means: while second robot does XRF/Raman on object N, the first
    robot can already pick and place object N+1 on the disk for imaging.

The simulation models a pipeline where objects flow through stages:
  [Box] → [Pick/Place] → [3DI/HSI] → [Platform Rotate] → [XRF/Raman]
    → [Platform Rotate] → [Return to Box] → [Box]

Multiple objects are in the pipeline simultaneously, each at a different stage.
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import paho.mqtt.client as mqtt
import yaml

from backend.domain.models import (
    GlobalConfig,
    SubsystemConfig,
    SubsystemState,
)

# Placeholder for the project root
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pipeline stages – each stage defines which subsystem(s) it uses
# ---------------------------------------------------------------------------
# These define the processing stages of the pipeline. Multiple stages can
# run in parallel if they use different subsystems.
PIPELINE_STAGES = [
    {
        "name": "pick_and_place",
        "subsystems": ["robot_first"],
        "events": {"start": "pick", "complete": "complete"},
        "delay": 1.0,
        "object_event": "robot_first/pick",
        "object_transition": "object_placed_on_disk",
        "description": "First robot picks object from box and places on disk",
    },
    {
        "name": "imaging",
        "subsystems": ["imaging_3di_hsi"],
        "events": {"start": "capture", "complete": "complete"},
        "delay": 1.2,
        "object_event": "imaging_3di_hsi/capture",
        "object_transition": "imaging_capture_complete",
        "description": "3DI/HSI imaging capture",
    },
    {
        "name": "rotate_to_second",
        "subsystems": ["rotating_platform"],
        "events": {"start": "rotate_to_second", "complete": "complete"},
        "delay": 0.8,
        "object_event": "rotating_platform/rotate_to_second",
        "object_transition": "platform_rotated_to_second",
        "description": "Platform rotates to second position",
    },
    {
        "name": "measurement",
        "subsystems": ["ai_analysis", "xrf", "raman"],
        "events": {"start": "plan", "complete": "complete"},
        "delay": 1.5,
        "object_event": "measurements_complete",
        "object_transition": "measurements_complete",  # Fires object SM transition directly
        "description": "AI planning + XRF/Raman measurements",
    },
    {
        "name": "rotate_to_first",
        "subsystems": ["rotating_platform"],
        "events": {"start": "rotate_to_first", "complete": "complete"},
        "delay": 0.8,
        "object_event": "rotating_platform/rotate_to_first",
        "object_transition": "platform_rotated_to_first",
        "description": "Platform rotates back to first position",
    },
    {
        "name": "return_to_box",
        "subsystems": ["robot_first", "cloud_database"],
        "events": {"start": "return", "complete": "complete"},
        "delay": 1.0,
        "object_event": "robot_first/return",
        "object_transition": "object_returned_to_box",
        "description": "First robot returns object to box; cloud upload triggered",
    }
]

# Parallel groups – stages that can run simultaneously because they use
# different subsystems. This is the key to understanding the pipeline:
#   - pick_and_place + measurement (robot_first vs xrf/raman)
#   - imaging + rotate_to_second (imaging vs rotating_platform)
#   - rotate_to_first (standalone; cloud_database handled in return_to_box)
PARALLEL_GROUPS = [
    {"pick_and_place", "measurement"},
    {"imaging", "rotate_to_second"},
    {"rotate_to_first"},
]


def _parse_subsystem_prefix(name: str):
    """Parse a scoped name like 'robot_first/pick'."""
    if "/" in name:
        parts = name.split("/", 1)
        return parts[0], parts[1]
    return "", name


def _is_pipeline_stage_complete(_pipeline: ObjectPipeline, stage: Dict[str, Any]) -> bool:
    """Check whether a pipeline stage should be skipped because its
    side-effects are already handled by another stage.

    Returns True for stages whose work is a no-op. Currently none.
    """
    return False


class ObjectPipeline:
    """Tracks a single object's progress through the pipeline."""

    def __init__(self, object_id: str, total_stages: int) -> None:
        self.object_id = object_id
        self.current_stage = 0
        self.total_stages = total_stages
        self.state = "identified_in_box"
        self.completed = False
        self.stages_completed = 0
        self.enqueued = False  # True once object_picked has been fired
        self.start_time: Optional[float] = None
        self.end_time: Optional[float] = None

    def advance_stage(self, new_state: str) -> bool:
        """Advance to the next stage. Returns True if pipeline is complete."""
        self.current_stage += 1
        self.state = new_state
        self.stages_completed = self.current_stage
        if self.current_stage >= self.total_stages:
            self.completed = True
        return self.completed

    def __repr__(self) -> str:
        return f"ObjectPipeline({self.object_id}, stage={self.current_stage}/{self.total_stages}, state={self.state})"


class WorkflowSimulator:
    """Simulates the AUTOMATA pipeline with parallel processing.

    The key difference from sequential processing:
    - Multiple objects are processed simultaneously
    - Objects at different pipeline stages can proceed in parallel
    - Progress is updated continuously, not just at the end
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
        self._paused = False

        # Pipeline state
        self._pipelines: Dict[str, ObjectPipeline] = {}
        self._lock = threading.Lock()
        self._max_concurrent = self._calculate_max_concurrent()
        logger.info(
            "Pipeline simulation: max %d concurrent objects (stage count: %d)",
            self._max_concurrent,
            len(PIPELINE_STAGES),
        )

    def _calculate_max_concurrent(self) -> int:
        """Calculate maximum number of objects that can be processed in parallel.

        Based on the pipeline stages and their parallel groups, this determines
        how many objects can flow through the pipeline simultaneously.
        """
        return min(self.num_objects, max(len(g) for g in PARALLEL_GROUPS))

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
        self._paused = False
        if self._client:
            self._client.loop_stop()
            self._client.disconnect()
        logger.info("Simulation stopped")

    def pause(self) -> None:
        """Pause the simulation (sets flag checked in the main loop)."""
        self._paused = True
        logger.info("Simulation paused")

    def resume(self) -> None:
        """Resume a paused simulation."""
        self._paused = False
        logger.info("Simulation resumed")

    def simulate_full_workflow(self) -> None:
        """Run the full simulated workflow with parallel pipeline processing.

        The pipeline works as follows:
        1. Initialize all subsystems (all go to ready)
        2. Transition to running state
        3. Create objects and start them flowing through the pipeline
        4. Each object progresses through stages in parallel
        5. Progress updates continuously
        6. When all objects complete, transition to stopped
        """
        logger.info(
            "Starting parallel pipeline simulation with %d objects",
            self.num_objects,
        )

        # Phase 1: Initialize all subsystems
        self._simulate_subsystems_ready()

        # Phase 2: Transition to running
        logger.info("Simulation: Subsystems ready, starting pipeline")
        self.orchestrator.fire_event("subsystem_ready")
        time.sleep(0.2)

        # Phase 3: Start the pipeline (create objects and process)
        self._start_pipeline()

        # Phase 4: Complete
        if self._running:
            self._simulate_completion()

    def _simulate_subsystems_ready(self) -> None:
        """Simulate all subsystems becoming ready.

        Each subsystem transitions from 'initializing' → 'ready'.
        Also resets any subsystems that might be in a non-ready state
        from a previous run. After each transition, notifies the
        orchestrator so the frontend receives the state update.
        """
        subsystem_names = [cfg.name for cfg in self.subsystems_config]

        logger.info("Simulation: Initializing all subsystems...")
        for name in subsystem_names:
            if not self._running:
                return

            adapter = self.orchestrator.adapters.get(name)
            if not adapter or not adapter.state_machine:
                logger.warning("No adapter or state machine for subsystem '%s'", name)
                continue

            # Reset subsystem to a clean state
            sm = adapter.state_machine
            sm.current_state = "initializing"

            # Transition to ready by firing init_complete
            try:
                sm.fire("init_complete", None)
            except Exception:
                # If fire fails, directly set to ready
                sm.current_state = "ready"

            # Update adapter state and notify frontend
            adapter.update_state(SubsystemState.READY)
            self.orchestrator.notify_subsystem_ready(name)
            logger.info("  Subsystem '%s' → ready", name)
            time.sleep(0.02)

        logger.info("Simulation: All subsystems initialized")

    def _start_pipeline(self) -> None:
        """Start the parallel pipeline processing.

        Creates objects one at a time (only the first object fires its
        initial object_picked immediately); subsequent objects are created
        but enter the pipeline only when a subsystem slot frees up.
        This avoids all objects contending for robot_first at once.
        """
        logger.info("Simulation: Creating %d objects for pipeline", self.num_objects)
        object_ids = []
        for i in range(1, self.num_objects + 1):
            object_id = f"object_{i:03d}"
            object_ids.append(object_id)
            self.orchestrator.create_object(object_id)
            self._pipelines[object_id] = ObjectPipeline(
                object_id, len(PIPELINE_STAGES)
            )

        # Only the first object starts immediately; the rest wait in the
        # queue until the pipeline frees a subsystem slot.
        if object_ids:
            self.orchestrator.fire_object_transition(object_ids[0], "object_picked")
            self._pipelines[object_ids[0]].enqueued = True

        time.sleep(0.5)  # Give frontend time to render objects

        # Process the pipeline stages in parallel
        self._process_pipeline_parallel(object_ids)

    def _process_pipeline_parallel(self, object_ids: List[str]) -> None:
        """Process objects through the pipeline with parallelism.

        Objects are processed in rounds. Each round:
        1. For each object, check if it can advance to its next stage
        2. If an object just completed a stage and has freed a subsystem,
           enqueue the next waiting object (fire its object_picked)
        3. Process the stage (fire events, update states)
        4. Advance the object to the next stage
        5. Update progress
        """
        total = len(object_ids)
        total_stages = len(PIPELINE_STAGES)

        # Each object tracks its current stage index
        stage_idx = {oid: 0 for oid in object_ids}
        completed_count = 0
        iteration = 0
        next_to_enqueue = 1  # Index of next object to fire object_picked

        logger.info(
            "Processing %d objects through %d pipeline stages",
            total, total_stages,
        )

        while completed_count < total and self._running and iteration < 100:
            iteration += 1
            round_done = False
            stages_completed_this_round = []  # objects that advanced a stage

            for object_id in object_ids:
                pipeline = self._pipelines[object_id]
                idx = stage_idx[object_id]

                if idx >= total_stages or pipeline.completed:
                    continue

                # Skip objects that haven't entered the pipeline yet
                # (enqueued=False means object_picked was never fired)
                if not pipeline.enqueued:
                    continue

                stage = PIPELINE_STAGES[idx]

                # Skip stages whose side-effects are already handled elsewhere
                if _is_pipeline_stage_complete(pipeline, stage):
                    new_state = self._get_state_for_stage(stage["name"])
                    pipeline.advance_stage(new_state)
                    stages_completed_this_round.append(object_id)
                    stage_idx[object_id] += 1
                    if pipeline.completed:
                        completed_count += 1
                    self._update_progress(total)
                    continue

                # Respect pause state
                if self._paused:
                    time.sleep(0.2)
                    break  # restart the outer while loop to re-check _running

                subsystems = stage["subsystems"]
                delay = stage["delay"]

                # Process the stage
                self._execute_stage(object_id, stage, delay)

                # Advance to next stage
                new_state = self._get_state_for_stage(stage["name"])
                pipeline.advance_stage(new_state)
                stages_completed_this_round.append(object_id)
                stage_idx[object_id] += 1
                if pipeline.completed:
                    completed_count += 1
                round_done = True

                # Update progress
                self._update_progress(total)

                logger.info(
                    "  Object %s → stage %d/%d (%s) [iter %d]",
                    object_id,
                    stage_idx[object_id],
                    total_stages,
                    pipeline.state,
                    iteration,
                )

            # Enqueue next waiting object only when there is room in the
            # pipeline. The rotating platform has 2 slots and robot_first
            # handles 1 object at a time, so at most _max_concurrent (2)
            # objects can be active simultaneously.
            if next_to_enqueue < total:
                active_count = sum(
                    1 for p in self._pipelines.values()
                    if p.enqueued and not p.completed
                )
                if active_count < self._max_concurrent:
                    next_id = object_ids[next_to_enqueue]
                    logger.info(
                        "  Object %s entering pipeline (active: %d/%d)",
                        next_id, active_count, self._max_concurrent,
                    )
                    self.orchestrator.fire_object_transition(next_id, "object_picked")
                    self._pipelines[next_id].enqueued = True
                    next_to_enqueue += 1

            # Delay between rounds (slowed for GUI visibility)
            if round_done:
                time.sleep(0.3)

        logger.info("Pipeline processing complete: %d objects, %d iterations", completed_count, iteration)

    def _execute_stage(
        self,
        object_id: str,
        stage: Dict[str, Any],
        delay: float,
    ) -> None:
        """Execute a single pipeline stage for an object.

        For each subsystem in the stage, ensure it goes through:
        ready → working → ready (completing the full cycle).

        If a subsystem is already busy (from a previous object's event in
        the same round), the scoped event is skipped and the subsystem's
        complete event is fired when it becomes ready.
        """
        stage_name = stage["name"]
        subsystems = stage["subsystems"]
        start_event = stage["events"]["start"]
        object_event = stage["object_event"]
        object_transition = stage["object_transition"]

        # Fire object transition on the object state machine
        # Use object_transition if defined, otherwise use object_event
        transition_event = object_transition or object_event
        if transition_event:
            # Only fire if it's not a scoped event (scoped events go to subsystems)
            if '/' not in transition_event:
                self.orchestrator.fire_object_transition(object_id, transition_event)

        # Fire scoped events through the orchestrator to trigger subsystem
        # state machine transitions. Unscoped events are object transitions
        # that were already handled above — skip to avoid broadcasting them
        # to all subsystems (which would generate noise for events like
        # 'measurements_complete' that have no subsystem handlers).
        scope, _ = _parse_subsystem_prefix(object_event)
        if scope:
            adapter = self.orchestrator.adapters.get(scope)
            if adapter and adapter.state_machine:
                if adapter.state_machine.current_state == "ready":
                    try:
                        self.orchestrator._on_event(object_event)
                    except Exception:
                        pass
                # Subsystem already transitioned by the object SM's event action
                # in the previous step – skip without logging (expected behavior).
            else:
                self.orchestrator._on_event(object_event)

        # Simulate subsystem processing - ensure full cycle
        for subsystem_name in subsystems:
            adapter = self.orchestrator.adapters.get(subsystem_name)
            if not adapter or not adapter.state_machine:
                continue

            sm = adapter.state_machine
            current_state = sm.current_state

            # Ensure subsystem goes through full cycle: ready → working → ready
            if current_state == "ready":
                # Fire start event to begin working
                if start_event in sm.get_available_transitions():
                    try:
                        sm.fire(start_event, None)
                    except Exception:
                        pass
                # Immediately fire complete to return to ready
                if "complete" in sm.get_available_transitions():
                    try:
                        sm.fire("complete", None)
                    except Exception:
                        pass
            elif current_state in ("picking", "returning", "rotating",
                                    "measuring", "capturing", "analyzing",
                                    "uploading"):
                # Subsystem is already working, fire complete
                if "complete" in sm.get_available_transitions():
                    try:
                        sm.fire("complete", None)
                    except Exception:
                        pass
            elif current_state in ("initializing",):
                # Subsystem hasn't been initialized yet
                if "init_complete" in sm.get_available_transitions():
                    try:
                        sm.fire("init_complete", None)
                    except Exception:
                        pass

            # Force subsystem to ready state as final guarantee
            if sm.current_state not in ("ready",):
                sm.current_state = "ready"

        # Simulate processing time (slowed for GUI visibility)
        time.sleep(delay * 2.5)

    def _get_state_for_stage(self, stage_name: str) -> str:
        """Get the object state corresponding to a pipeline stage."""
        state_map = {
            "pick_and_place": "moving_to_disk",
            "imaging": "HSI_3DI_analysis",
            "rotate_to_second": "object_going_second_position",
            "measurement": "XRF_RAMAN_analysis",
            "rotate_to_first": "object_going_first_position",
            "return_to_box": "moving_to_box",
            # cloud_upload no longer a separate pipeline stage; handled in
            # return_to_box via cloud_database subsystem cycle.
        }
        return state_map.get(stage_name, "unknown")

    def _update_progress(self, total: int) -> None:
        """Update and broadcast progress."""
        # Count completed objects
        completed = sum(
            1 for p in self._pipelines.values() if p.completed
        )
        current = completed

        # Protect _status writes from the simulation daemon thread
        with self.orchestrator._lock:
            self.orchestrator._progress = current
            self.orchestrator._status.progress = current
        self._fire_progress_event(current, total)

    def _fire_progress_event(self, progress: int, total: int) -> None:
        """Fire a progress_updated event to the frontend."""
        self.orchestrator._fire_frontend_event(
            "progress_updated",
            {"progress": progress, "total": total},
        )

    def _simulate_completion(self) -> None:
        """Simulate workflow completion.

        The workflow transitions:
          running -> stopping -> stopped

        Objects remain visible in the GUI for the operator to review.
        The operator can then start a new workflow from the stopped state.
        """
        logger.info("Simulation: pipeline complete")

        # Phase 1: Stop the workflow (running -> stopping -> stopped)
        # Transition running → stopping → stopped.
        # If the operator already pressed Stop, the workflow may already be
        # in 'stopping' (or even 'stopped'), so we handle both cases gracefully.
        current = self.orchestrator.state_machine.state
        if current == "running":
            try:
                self.orchestrator.fire_event("stop_requested")
                time.sleep(0.2)
            except Exception as exc:
                logger.warning("Failed to fire stop_requested: %s", exc)
                self._fire_frontend_event(
                    "error_reported",
                    {"message": f"Failed to stop workflow: {exc}"},
                )
                return

        # Now transition stopping → stopped (whether we just entered stopping
        # or the operator already triggered it)
        if self.orchestrator.state_machine.state == "stopping":
            try:
                self.orchestrator.fire_event("stopped")
                time.sleep(0.2)
            except Exception as exc:
                logger.warning("Failed to fire stopped: %s", exc)
                self._fire_frontend_event(
                    "error_reported",
                    {"message": f"Failed to transition to stopped: {exc}"},
                )

        logger.info(
            "Simulation complete: %d objects processed. "
            "Workflow is in 'stopped' state.",
            len([p for p in self._pipelines.values() if p.completed]),
        )
        # Do NOT auto-restart - let operator review results and start manually

    def _publish_mqtt(self, topic: str, payload: Dict[str, Any]) -> None:
        """Publish a simulated MQTT message."""
        if self._client:
            self._client.publish(topic, json.dumps(payload), qos=1)
