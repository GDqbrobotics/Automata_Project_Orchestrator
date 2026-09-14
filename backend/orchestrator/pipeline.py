"""Pipeline controller – orchestrates the object flow through the machine.

This is the orchestration logic of the AUTOMATA workflow
(docs/PROJECT_CONTEXT.md). It dispatches objects from the box to the two
disk positions and back, and it is driven exclusively by:

* workflow state changes (start / pause / stop),
* the vision system's object-detection response (objects discovered in the
  box, with IDs and analysis order),
* subsystem state reports arriving over MQTT.

The controller works identically whether the subsystems behind the MQTT
boundary are simulated processes (backend/simulation/*_sim.py) or the real
hardware: it only issues commands through the subsystem state machines
(the boundary layer) and reacts to their state reports. No subsystem
specific logic is embedded here – subsystem behaviour lives in the
subsystem YAML files and in the subsystems themselves.

Physical capacity constraints enforced here (the machine can only hold so
many objects outside the box at the same time):

* at most 2 objects on the platform – one per disk position,
* at most 1 object in the robot's hand – robot 1 has one end effector,
* a platform rotation moves BOTH disks at once, so it is only commanded
  when every occupied disk holds an object that finished its stage.

Stage mapping (docs/PROJECT_CONTEXT.md workflow):

* first disk position:  robot 1 – pick / place / XRF+Raman / return to box
* second disk position: robot 2 – 3DI+HSI scan (imaging interface)
* the AI plans the XRF/Raman points between imaging and measurement:
  it starts in parallel with the rotation back to the first position, and
  the object waits in the ai_processing state until the AI reports done
  (only then do the XRF/Raman measurements start)

Threading: methods may be called from the MQTT callback thread, the
WebSocket command thread, or re-entrantly from object-transition events.
The controller lock is an RLock; ``on_object_workflow_event`` (called
while the orchestrator lock is held) only sets flags and never fires
transitions.
"""

from __future__ import annotations

import logging
import threading
from typing import List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# Subsystem names
VISION = "vision"
ROBOT_FIRST = "robot_first"
ROBOT_SECOND = "robot_second"
ROTATING_PLATFORM = "rotating_platform"
IMAGING_3DI_HSI = "imaging_3di_hsi"
AI_ANALYSIS = "ai_analysis"
CLOUD_DB = "cloud_database"
# Subsystems that work in parallel during the measurement stage on the
# FIRST disk position (robot 1 + XRF/Raman analyzers). The AI is NOT in the
# set: it must finish planning the points BEFORE the stage starts (the
# object waits in ai_processing until the AI reports done).
MEASUREMENT_SUBSYSTEMS: Tuple[str, ...] = ("xrf", "raman", "robot_first")

# Object states used for pipeline coordination (see object.yaml)
OBJ_WAITING = "waiting_for_disk"            # in the box, queued
OBJ_GOING_SECOND = "object_going_second_position"   # on the first disk, waiting for rotation to imaging
OBJ_HSI = "HSI_3DI_analysis"                # on the second disk, being imaged
OBJ_GOING_FIRST = "object_going_first_position"     # on the second disk, waiting for rotation back to measurement
OBJ_AI_PROCESSING = "ai_processing"         # on the first disk, AI planning the XRF/Raman points
OBJ_XRF_RAMAN = "XRF_RAMAN_analysis"        # on the first disk, being measured
OBJ_MOVING_TO_BOX = "moving_to_box"         # on the first disk, waiting for return

# Non-working subsystem states (a report into any other state is a work stage)
NON_WORK_STATES = {"initializing", "ready", "stopped", "error"}


class PipelineController:
    """Dispatches objects through the two-disk pipeline.

    Event-driven: reacts to subsystem status reports (MQTT), to the vision
    object-detection response, and to workflow state changes.
    """

    def __init__(self, orchestrator: object) -> None:
        self._orch = orchestrator
        self._lock = threading.RLock()

        # Phases: idle (no active run) -> scanning (box scan in progress)
        #        -> pipeline (objects flowing) -> suspended (run over/stopped)
        self._phase = "idle"
        self._order: List[str] = []
        self._queue_pos = 0
        self._disk_first: Optional[str] = None     # object on the imaging position
        self._disk_second: Optional[str] = None    # object on the measurement position
        self._robot_task: Optional[Tuple[str, str]] = None  # (op, object_id)
        self._imaging_object: Optional[str] = None
        # Object waiting in ai_processing for the AI's point planning
        self._ai_object: Optional[str] = None
        self._measuring_object: Optional[str] = None
        self._measuring_pending: Set[str] = set()
        self._rotation_requested = False
        self._rotating = False

    # ------------------------------------------------------------------
    # Low-level helpers
    # ------------------------------------------------------------------
    def _obj_state(self, object_id: Optional[str]) -> Optional[str]:
        if object_id is None:
            return None
        return self._orch.object_manager.get_object_state(object_id)

    def _sm_state(self, name: str) -> Optional[str]:
        adapter = self._orch.adapters.get(name)
        if adapter and adapter.state_machine:
            return adapter.state_machine.current_state
        return None

    def _fire_object(self, object_id: str, event: str) -> bool:
        new_state = self._orch.fire_object_transition(object_id, event)
        if new_state is None:
            logger.error("Pipeline: object '%s' rejected event '%s'", object_id, event)
            return False
        return True

    def _command(self, name: str, event: str) -> None:
        """Issue a command through the subsystem SM (boundary layer)."""
        self._orch.fire_subsystem_transition(name, event)

    # ------------------------------------------------------------------
    # Public API (called by the orchestrator)
    # ------------------------------------------------------------------
    def on_workflow_state(self, state: str) -> None:
        with self._lock:
            if state == "running":
                if self._phase == "idle":
                    self._phase = "scanning"
                    logger.info("Pipeline: workflow running – requesting box scan")
                    self._advance()
            elif state in ("stopping", "stopped", "error", "idle"):
                if self._phase in ("scanning", "pipeline"):
                    self._phase = "suspended"
                    logger.info("Pipeline: workflow '%s' – dispatch suspended", state)

    def on_objects_detected(self, object_ids: List[str]) -> None:
        """Vision reported the objects in the box (IDs + analysis order)."""
        with self._lock:
            if self._phase != "scanning":
                logger.warning(
                    "Pipeline: objects detected in phase '%s', ignoring", self._phase
                )
                return
            if not object_ids:
                logger.warning("Pipeline: vision reported no objects – ending run")
                self._phase = "suspended"
                self._finish_workflow()
                return
            logger.info(
                "Pipeline: vision identified %d objects: %s",
                len(object_ids), ", ".join(object_ids),
            )
            self._orch.set_total_objects(len(object_ids))
            for object_id in object_ids:
                self._orch.create_object(object_id)
                # Analysis order established: the object queues up in the box
                # (identified_in_box -> waiting_for_disk).
                self._fire_object(object_id, "queued_for_analysis")
            self._order = list(object_ids)
            self._queue_pos = 0
            self._phase = "pipeline"
            self._advance()

    def on_object_workflow_event(self, object_id: str, event: str) -> None:
        """Internal workflow-scope events fired by object SM transitions.

        NOTE: called while the orchestrator lock is held (object event
        path) – only set flags here, never fire transitions.
        """
        with self._lock:
            if event in ("rotation_requested_second", "rotation_requested_first"):
                self._rotation_requested = True
                logger.debug("Pipeline: rotation requested (by %s)", object_id)
            else:
                logger.debug(
                    "Pipeline: unhandled workflow event '%s' from %s",
                    event, object_id,
                )

    def on_subsystem_status(self, name: str, state: str, was_state: Optional[str]) -> None:
        """A subsystem reported a state change over MQTT."""
        with self._lock:
            if self._phase not in ("scanning", "pipeline"):
                return
            completed = (
                state == "ready"
                and was_state is not None
                and was_state not in NON_WORK_STATES
            )
            if completed:
                self._on_subsystem_completed(name, was_state)
            self._advance()

    def reset(self) -> None:
        """Clear all pipeline bookkeeping (new workflow run)."""
        with self._lock:
            self._phase = "idle"
            self._order = []
            self._queue_pos = 0
            self._disk_first = None
            self._disk_second = None
            self._robot_task = None
            self._imaging_object = None
            self._ai_object = None
            self._measuring_object = None
            self._measuring_pending = set()
            self._rotation_requested = False
            self._rotating = False

    # ------------------------------------------------------------------
    # Reactions to subsystem completions
    # ------------------------------------------------------------------
    def _on_subsystem_completed(self, name: str, was_state: str) -> None:
        if name == ROBOT_FIRST and self._robot_task:
            op, object_id = self._robot_task
            if op == "pick" and was_state == "picking":
                # The object is in the robot's hand: place it on the disk.
                self._command(ROBOT_FIRST, "place")
                self._robot_task = ("place", object_id)
            elif op == "place" and was_state == "placing":
                # The object is on the first disk: it waits there for the
                # rotation to the second (imaging) position.
                self._robot_task = None
                self._fire_object(object_id, "object_placed_on_disk")
            elif op == "return" and was_state == "returning":
                # The object is back in the box.
                self._robot_task = None
                if self._disk_first == object_id:
                    self._disk_first = None
                self._fire_object(object_id, "object_returned_to_box")
            # (robot 1's XRF/Raman completion has no pick/place/return task
            #  in flight and is counted as a measurement completion below)
            return

        if name == IMAGING_3DI_HSI and was_state == "capturing" and self._imaging_object:
            object_id = self._imaging_object
            self._imaging_object = None
            # -> object_going_first_position; the object transition starts
            #    the AI (point planning) and requests the rotation back.
            #    Robot 2's scan completion is not tracked: the imaging
            #    interface owns the data the AI needs.
            self._fire_object(object_id, "imaging_capture_complete")
            return

        if name == AI_ANALYSIS and was_state == "analyzing" and self._ai_object:
            # The AI finished extracting the interest points for the object
            # waiting in ai_processing: the XRF/Raman stage starts now
            # (the object transition commands robot 1 + the analyzers).
            object_id = self._ai_object
            self._ai_object = None
            logger.info(
                "Pipeline: AI planning done for %s – starting measurements",
                object_id,
            )
            if self._fire_object(object_id, "ai_complete"):
                self._start_measurement_tracking(object_id)
            return

        if name in MEASUREMENT_SUBSYSTEMS and self._measuring_object:
            self._measuring_pending.discard(name)
            if not self._measuring_pending:
                self._complete_measurements(self._measuring_object)
            return

        if name == ROTATING_PLATFORM and was_state == "rotating" and self._rotating:
            self._finish_rotation()

    def _complete_measurements(self, object_id: str) -> None:
        """All measurement subsystems are done: send robot 1 back to the box.

        The measurements_complete transition moves the object to
        moving_to_box and commands robot 1 (robot_first/return); the task
        is tracked here so the robot's completion releases the first disk
        slot (robot 1 has no task in flight while measuring).
        """
        self._measuring_object = None
        self._robot_task = ("return", object_id)
        self._fire_object(object_id, "measurements_complete")

    def _start_measurement_tracking(self, object_id: str) -> None:
        """Start tracking the parallel measurement subsystems for an object
        that just entered the XRF/Raman stage (first position).

        The stage ends when every subsystem that actually entered its
        working state reports ready (guards against a command that did not
        land because a subsystem was not in the expected state).
        """
        self._measuring_object = object_id
        self._measuring_pending = {
            m for m in MEASUREMENT_SUBSYSTEMS
            if (s := self._sm_state(m)) not in (None, *NON_WORK_STATES)
        }
        if not self._measuring_pending:
            logger.warning(
                "Pipeline: no measurement subsystem active – "
                "completing measurements immediately"
            )
            self._complete_measurements(object_id)

    def _finish_rotation(self) -> None:
        """Apply the coupled disk swap and start the stages on both sides.

        After the swap, the object that was on the first disk is on the
        second (imaging) position, and the object that was on the second
        disk is back on the first (XRF/Raman measurement) position.
        """
        first, second = self._disk_first, self._disk_second

        if first is not None and self._obj_state(first) == OBJ_GOING_SECOND:
            # -> HSI_3DI_analysis; the object transition commands the
            #    imaging subsystem and robot 2's scan (second position).
            if self._fire_object(first, "platform_rotated_to_second"):
                self._imaging_object = first
                if self._sm_state(IMAGING_3DI_HSI) != "capturing":
                    # Imaging subsystem unavailable: skip the stage so the
                    # pipeline does not stall.
                    logger.warning(
                        "Pipeline: imaging not capturing – completing stage immediately"
                    )
                    self._imaging_object = None
                    self._fire_object(first, "imaging_capture_complete")

        if second is not None and self._obj_state(second) == OBJ_GOING_FIRST:
            # -> ai_processing: the object waits on the first position for
            #    the AI to finish planning the XRF/Raman points (the AI was
            #    started when the imaging completed, in parallel with this
            #    rotation). When the AI reports done, the object transition
            #    commands robot 1 + the analyzers.
            if self._fire_object(second, "platform_rotated_to_first"):
                self._ai_object = second
                if self._sm_state(AI_ANALYSIS) in (None, *NON_WORK_STATES):
                    # AI not working (already finished during the rotation,
                    # or its plan command never landed): skip the wait so
                    # the pipeline does not stall.
                    logger.info(
                        "Pipeline: AI already done for %s – starting measurements immediately",
                        second,
                    )
                    self._ai_object = None
                    if self._fire_object(second, "ai_complete"):
                        self._start_measurement_tracking(second)

        self._disk_first, self._disk_second = second, first
        self._rotating = False
        logger.info(
            "Pipeline: platform rotation done (first=%s, second=%s)",
            self._disk_first, self._disk_second,
        )

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------
    def _disks_stable(self) -> bool:
        """A rotation moves BOTH disks at once, so every occupied disk must
        hold an object that has finished its stage."""
        if self._disk_first is not None and self._obj_state(self._disk_first) != OBJ_GOING_SECOND:
            return False
        if self._disk_second is not None and self._obj_state(self._disk_second) != OBJ_GOING_FIRST:
            return False
        return True

    def _advance(self) -> None:
        """Issue the next possible commands.

        Called on every pipeline-relevant change while holding the
        controller lock (and not the orchestrator lock).
        """
        if self._phase == "scanning":
            if self._sm_state(VISION) == "ready":
                logger.info("Pipeline: commanding vision box scan")
                self._command(VISION, "capture")
            return
        if self._phase != "pipeline":
            return

        # Robot 1 performs the XRF/Raman stage: an object waiting on the
        # second disk to come back (OBJ_GOING_FIRST) needs robot 1 the
        # moment the rotation lands it on the first position, so robot 1
        # must be free then (gate 1) and no new pick may be started in the
        # meantime (gate 2).
        second_waiting_for_measurement = (
            self._disk_second is not None
            and self._obj_state(self._disk_second) == OBJ_GOING_FIRST
        )

        # 1. Pending platform rotation (both disks must be stable)
        if (
            self._rotation_requested
            and not self._rotating
            and self._sm_state(ROTATING_PLATFORM) == "ready"
            and self._disks_stable()
            and not (
                second_waiting_for_measurement
                and (
                    self._sm_state(ROBOT_FIRST) != "ready"
                    or self._robot_task is not None
                )
            )
        ):
            target = (
                "second"
                if self._disk_first is not None
                and self._obj_state(self._disk_first) == OBJ_GOING_SECOND
                else "first"
            )
            logger.info("Pipeline: commanding platform rotation to %s position", target)
            self._command(ROTATING_PLATFORM, f"rotate_to_{target}")
            self._rotation_requested = False
            self._rotating = True

        # 2. Robot 1 dispatch: the next object leaves the box only when the
        #    first disk position is free (2 disk slots, 1 end effector),
        #    and never while the second-disk object is coming back for
        #    XRF/Raman (robot 1 is needed there as soon as it lands).
        if (
            self._robot_task is None
            and self._sm_state(ROBOT_FIRST) == "ready"
            and self._disk_first is None
            and not second_waiting_for_measurement
            and self._queue_pos < len(self._order)
        ):
            object_id = self._order[self._queue_pos]
            if self._obj_state(object_id) == OBJ_WAITING:
                logger.info("Pipeline: dispatching %s to the platform", object_id)
                self._disk_first = object_id
                self._queue_pos += 1
                if self._fire_object(object_id, "object_picked"):
                    self._robot_task = ("pick", object_id)
                else:
                    self._disk_first = None
                    self._queue_pos -= 1

        # 3. Run finished?
        if (
            self._queue_pos >= len(self._order)
            and self._disk_first is None
            and self._disk_second is None
            and self._robot_task is None
            and not self._rotating
            and self._imaging_object is None
            and self._ai_object is None
            and self._measuring_object is None
        ):
            self._phase = "suspended"
            self._finish_workflow()

    def _finish_workflow(self) -> None:
        """Complete the run: running -> stopping -> stopped (objects remain
        visible in the GUI for the operator to review)."""
        logger.info("Pipeline: %d object(s) processed – stopping the workflow", len(self._order))
        self._orch.fire_event("stop_requested")
        self._orch.fire_event("stopped")
