"""Pipeline controller – orchestrates the object flow through the machine.

This is the orchestration logic of the AUTOMATA workflow
(docs/PROJECT_CONTEXT.md). It dispatches objects from the box to the disk
positions and back, and it is driven exclusively by:

* workflow state changes (start / pause / stop),
* the box scanner's object-detection response (objects discovered in the
  box, with IDs and analysis order),
* subsystem state reports arriving over MQTT.

The controller works identically whether the subsystems behind the MQTT
boundary are simulated processes (backend/simulation/*_sim.py) or the real
hardware: it only issues commands through the subsystem state machines
(the boundary layer) and reacts to their state reports. No subsystem
specific logic is embedded here – subsystem behaviour lives in the
subsystem YAML files and in the subsystems themselves.

What it knows about the *flow* comes from the object blueprint
(backend/config/object.yaml, docs/CONFIG_CHANGE_GUIDE.md R2):

* per-state ``meta`` – where the object physically is (``position``: box /
  hand / disk name), which subsystem works on it (``actor`` or
  ``parallel_actors``), and rotation requests/commands;
* top-level ``metadata`` – the machine layout (``disks``, ``disk_names``,
  ``hand_slots``) and the ``task_chains`` (the multi-step work of one
  subsystem per object, e.g. the pick -> place chain).

What is coded here is only the *physical rules* (parameterised by the
layout above): a rotation moves every disk at once, so it is only issued
when every occupied disk holds an object that requested it; the
dispatching robot must be free before a rotation that brings an object
back to the disk where the robot will act on it; and dispatch is checked
before rotation and allowed while an object waits to come back, so the
next object is placed first and the platform holds two objects at all
times (the newcomer's imaging overlaps the returning object's
measurement – see `_advance`).

Threading: methods may be called from the MQTT callback thread, the
WebSocket command thread, or re-entrantly from object-transition events.
The controller lock is an RLock; ``on_object_workflow_event`` (called
while the orchestrator lock is held) only sets flags and never fires
transitions.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


class PipelineController:
    """Dispatches objects through the disk pipeline.

    Event-driven: reacts to subsystem status reports (MQTT), to the box
    scanner's object-detection response, and to workflow state changes.
    """

    def __init__(self, orchestrator: object) -> None:
        self._orch = orchestrator
        self._lock = threading.RLock()

        cfg = orchestrator.object_manager.config
        meta = cfg.metadata

        # ── Machine layout (object.yaml metadata) ────────────────────
        self._disk_count = int(meta.get("disks", 2))
        self._disk_names: List[str] = list(
            meta.get("disk_names")
            or [f"disk_{i}" for i in range(1, self._disk_count + 1)]
        )
        self._hand_slots = int(meta.get("hand_slots", 1))

        # ── Per-state physical facts (object.yaml states.*.meta) ─────
        self._state_meta: Dict[str, Dict[str, Any]] = {
            s: (c.meta or {}) for s, c in cfg.states.items()
        }
        # The object journey is linear: every state has at most one
        # outgoing transition – its event is how the object leaves the
        # state (fired when the state's actor reports completion).
        self._leave_event: Dict[str, str] = {}
        self._leave_target: Dict[str, str] = {}
        for s, c in cfg.states.items():
            if c.transitions:
                self._leave_event[s] = c.transitions[0].event
                self._leave_target[s] = c.transitions[0].target

        # ── Task chains (multi-step work of one subsystem per object) ─
        self._task_chains: Dict[str, List[Dict[str, Any]]] = (
            meta.get("task_chains") or {}
        )
        self._chain_by_object_event: Dict[str, Tuple[str, Dict[str, Any]]] = {}
        self._chain_by_op: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for subsystem, entries in self._task_chains.items():
            for entry in entries:
                if entry.get("object_event"):
                    self._chain_by_object_event[entry["object_event"]] = (
                        subsystem,
                        entry,
                    )
                if entry.get("op"):
                    self._chain_by_op[(subsystem, entry["op"])] = entry
        self._chain_subsystem: Optional[str] = next(iter(self._task_chains), None)

        # Dispatch: the box state whose outgoing transition starts a chain
        # op (and commands the robot through the transition actions), the
        # event that leaves it, and the disk the object lands on once the
        # op's done_event fires.
        self._dispatch_state: Optional[str] = None
        self._dispatch_event: Optional[str] = None
        for s in self._states_with_position("box"):
            ev = self._leave_event.get(s)
            if ev in self._chain_by_object_event:
                self._dispatch_state = s
                self._dispatch_event = ev
                break
        self._entry_disk: Optional[int] = None
        if self._dispatch_state is not None:
            _sub, entry = self._chain_by_object_event[self._dispatch_event]
            for s, ev in self._leave_event.items():
                if ev == entry.get("done_event"):
                    pos = self._state_meta.get(
                        self._leave_target.get(s, ""), {}
                    ).get("position")
                    if pos in self._disk_names:
                        self._entry_disk = self._disk_names.index(pos)
                    break

        # ── Rotation: the platform is the subsystem that can issue the
        # rotate_command events declared in the state metas ───────────
        rotate_commands = {
            m.get("rotate_command")
            for m in self._state_meta.values()
            if m.get("rotate_command")
        }
        self._platform: Optional[str] = None
        if rotate_commands:
            for name, adapter in orchestrator.adapters.items():
                sm = adapter.state_machine
                if sm is None:
                    continue
                events = {
                    t.event
                    for c in sm.config.states.values()
                    for t in c.transitions
                }
                if rotate_commands.issubset(events):
                    self._platform = name
                    break

        # Rotation requests are the meta.rotate_request values; the
        # workflow/ scope events with any other name stay logged no-ops.
        self._rotate_requests: Set[str] = {
            m.get("rotate_request")
            for m in self._state_meta.values()
            if m.get("rotate_request")
        }

        # ── Box scan: the subsystem that reports the object list (R5) ─
        ready = orchestrator._global_config.states_ready
        self._reporter: Optional[str] = None
        self._scan_command: Optional[str] = None
        for name, adapter in orchestrator.adapters.items():
            for rep in adapter.metadata.get("reports") or []:
                if (rep.get("object_ids") or {}).get("count_source") != "num_objects":
                    continue
                after = rep.get("after") or {}
                sm = adapter.state_machine
                if sm is None or not after.get("from"):
                    continue
                event = sm.config.event_to(ready, after["from"])
                if event:
                    self._reporter = name
                    self._scan_command = event
                    break
            if self._reporter is not None:
                break

        # ── Single-actor states per subsystem (for completion lookup) ─
        self._actor_states: Dict[str, List[str]] = {}
        for s, m in self._state_meta.items():
            actor = m.get("actor")
            if actor and not m.get("actor_op"):
                self._actor_states.setdefault(actor, []).append(s)

        # The object event that leaves the initial state (queuing).
        self._queue_event = self._leave_event.get(cfg.initial_state)

        # ── Phases / bookkeeping ──────────────────────────────────────
        # Phases: idle (no active run) -> scanning (box scan in progress)
        #        -> pipeline (objects flowing) -> suspended (run over/stopped)
        self._phase = "idle"
        self._order: List[str] = []
        self._queue_pos = 0
        self._disks: List[Optional[str]] = [None] * self._disk_count
        # (object_id, completion action) of the dispatching robot's task
        self._robot_task: Optional[Tuple[str, Dict[str, Any]]] = None
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
        # The orchestrator's local subsystem SM no longer advances on command
        # (the real reported state is authoritative), so ask the orchestrator
        # for the *effective* state: the pending expected working state if a
        # command is in flight, else the last reported state.
        return self._orch.subsystem_effective_state(name)

    def _ready_state(self) -> str:
        return self._orch._global_config.states_ready

    def _fire_object(self, object_id: str, event: str) -> bool:
        new_state = self._orch.fire_object_transition(object_id, event)
        if new_state is None:
            logger.error("Pipeline: object '%s' rejected event '%s'", object_id, event)
            return False
        return True

    def _fire_object_event(self, object_id: str, event: str) -> bool:
        """Fire an object transition and settle the state it enters
        (measurement tracking, task chains, stage skipping)."""
        if not self._fire_object(object_id, event):
            return False
        self._settle(object_id)
        return True

    def _command(
        self, name: str, event: str, object_id: Optional[str] = None
    ) -> None:
        """Issue a command through the subsystem SM (boundary layer).

        ``object_id`` (when the command belongs to an object) is embedded
        in the payload and drives the points push for consumers that
        requested the object's stored points (R12)."""
        self._orch.fire_subsystem_transition(name, event, object_id=object_id)

    def _states_with_position(self, position: str) -> List[str]:
        return [s for s, m in self._state_meta.items() if m.get("position") == position]

    def _release_disk(self, idx: int, object_id: str) -> None:
        if idx < len(self._disks) and self._disks[idx] == object_id:
            self._disks[idx] = None

    def _chain_completion(self, subsystem: str, op: str) -> Dict[str, Any]:
        """The action to take when the subsystem's `op` work completes:
        issue `next_command` first (if any), then fire `done_event` on the
        object (releasing its disk slot, if any)."""
        entry = self._chain_by_op.get((subsystem, op)) or {}
        done: Dict[str, Any] = {"kind": "event", "event": entry.get("done_event")}
        if entry.get("releases"):
            done["releases"] = entry["releases"]
        if entry.get("next_command"):
            return {
                "kind": "command",
                "event": entry["next_command"],
                "next": done,
            }
        return done

    def _object_in_state(self, state: str) -> Optional[str]:
        seen: Set[str] = set()
        candidates = list(self._order) + [d for d in self._disks if d]
        for oid in candidates:
            if oid in seen:
                continue
            seen.add(oid)
            if self._obj_state(oid) == state:
                return oid
        return None

    # ------------------------------------------------------------------
    # Public API (called by the orchestrator)
    # ------------------------------------------------------------------
    def on_workflow_state(self, state: str) -> None:
        with self._lock:
            wf = self._orch.workflow_names
            if state == wf.run_state:
                if self._phase == "idle":
                    self._phase = "scanning"
                    logger.info("Pipeline: workflow running – requesting box scan")
                    self._advance()
            elif state in wf.suspend_states:
                if self._phase in ("scanning", "pipeline"):
                    self._phase = "suspended"
                    logger.info("Pipeline: workflow '%s' – dispatch suspended", state)

    def on_objects_detected(self, object_ids: List[str]) -> None:
        """The box scanner reported the objects in the box (IDs + order)."""
        with self._lock:
            if self._phase != "scanning":
                logger.warning(
                    "Pipeline: objects detected in phase '%s', ignoring", self._phase
                )
                return
            if not object_ids:
                logger.warning("Pipeline: box scan reported no objects – ending run")
                self._phase = "suspended"
                self._finish_workflow()
                return
            logger.info(
                "Pipeline: box scan identified %d objects: %s",
                len(object_ids), ", ".join(object_ids),
            )
            self._orch.set_total_objects(len(object_ids))
            for object_id in object_ids:
                self._orch.create_object(object_id)
                # Analysis order established: the object queues up in the box
                # (initial state -> first waiting state).
                if self._queue_event:
                    self._fire_object_event(object_id, self._queue_event)
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
            if event in self._rotate_requests:
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
            non_work = self._orch._global_config.non_work_states
            completed = (
                state == self._ready_state()
                and was_state is not None
                and was_state not in non_work
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
            self._disks = [None] * self._disk_count
            self._robot_task = None
            self._measuring_object = None
            self._measuring_pending = set()
            self._rotation_requested = False
            self._rotating = False

    # ------------------------------------------------------------------
    # Reactions to subsystem completions
    # ------------------------------------------------------------------
    def _on_subsystem_completed(self, name: str, was_state: str) -> None:
        # 1. Task chain (multi-step work, e.g. pick -> place): the reported
        #    work state completes the current op – issue the follow-up
        #    command, or fire the done_event (and release the disk).
        if name in self._task_chains and self._robot_task is not None:
            object_id, action = self._robot_task
            if action.get("kind") == "command":
                self._command(name, action["event"], object_id=object_id)
                self._robot_task = (object_id, action["next"])
            else:
                self._robot_task = None
                releases = action.get("releases")
                if releases in self._disk_names:
                    self._release_disk(self._disk_names.index(releases), object_id)
                self._fire_object_event(object_id, action["event"])
            return

        # 2. Rotation finished (the platform reported ready after rotating).
        if name == self._platform and self._rotating:
            self._finish_rotation()
            return

        # 3. Measurement stage: one of the parallel actors is done; the
        #    stage ends when every parallel actor that entered its work
        #    state has reported back.
        if self._measuring_object is not None:
            state = self._obj_state(self._measuring_object)
            meta = self._state_meta.get(state or "", {})
            parallel = meta.get("parallel_actors") or []
            if name in parallel:
                self._measuring_pending.discard(name)
                if not self._measuring_pending:
                    self._complete_measurements(self._measuring_object, parallel)
                return

        # 4. Single-actor stage: the actor is done – the object leaves the
        #    state over its outgoing transition.
        for state in self._actor_states.get(name, []):
            object_id = self._object_in_state(state)
            if object_id is not None and self._obj_state(object_id) == state:
                event = self._leave_event.get(state)
                if event:
                    self._fire_object_event(object_id, event)
                return

    def _start_measurement_tracking(self, object_id: str, actors: List[str]) -> None:
        """Start tracking the parallel actors for an object that just
        entered a parallel-actor state (e.g. the measurement stage).

        The stage ends when every subsystem that actually entered its
        working state reports ready (guards against a command that did not
        land because a subsystem was not in the expected state).
        """
        self._measuring_object = object_id
        non_work = self._orch._global_config.non_work_states
        self._measuring_pending = {
            a for a in actors if self._sm_state(a) not in (None, *non_work)
        }
        if not self._measuring_pending:
            logger.warning(
                "Pipeline: no measurement subsystem active – "
                "completing measurements immediately"
            )
            self._complete_measurements(object_id, actors)

    def _complete_measurements(self, object_id: str, actors: List[str]) -> None:
        """All parallel actors are done: leave the state. The outgoing
        transition commands the next work (e.g. the robot's return); the
        task-chain bookkeeping picks it up when the object arrives."""
        self._measuring_object = None
        self._measuring_pending = set()
        state = self._obj_state(object_id)
        event = self._leave_event.get(state or "")
        if event:
            self._fire_object_event(object_id, event)

    def _settle(self, object_id: str) -> None:
        """Settle the state an object just entered.

        - parallel-actor state: start the completion tracking;
        - task-chain actor state (meta.actor_op): the op's command was
          fired by the transition that entered the state – track it;
        - single-actor state: if the actor is not working (its command
          never landed, or it already finished), complete the stage
          immediately so the pipeline does not stall – then settle the
          next state.
        """
        state = self._obj_state(object_id)
        if state is None:
            return
        meta = self._state_meta.get(state, {})

        parallel = meta.get("parallel_actors")
        if parallel:
            self._start_measurement_tracking(object_id, list(parallel))
            return

        actor = meta.get("actor")
        if actor is None:
            return

        op = meta.get("actor_op")
        if op and self._chain_by_op.get((actor, op)):
            self._robot_task = (object_id, self._chain_completion(actor, op))
            return

        non_work = self._orch._global_config.non_work_states
        if self._sm_state(actor) in (None, *non_work):
            event = self._leave_event.get(state)
            if event:
                logger.warning(
                    "Pipeline: %s not working in %s – completing stage immediately",
                    actor, state,
                )
                self._fire_object_event(object_id, event)  # recurses

    def _finish_rotation(self) -> None:
        """Apply the coupled disk rotation and start the arriving stages.

        After the rotation, every object that was waiting on its disk
        leaves its waiting state over its outgoing transition (which
        commands the stages at the new position).
        """
        for object_id in self._disks:
            if object_id is None:
                continue
            state = self._obj_state(object_id)
            meta = self._state_meta.get(state or "", {})
            if not meta.get("rotate_request"):
                continue
            event = self._leave_event.get(state or "")
            if event and self._obj_state(object_id) == state:
                self._fire_object_event(object_id, event)

        # A rotation moves every disk to the next position (disk i -> i+1;
        # with two disks that is the coupled swap).
        self._disks = self._disks[1:] + self._disks[:1]
        self._rotating = False
        logger.info(
            "Pipeline: platform rotation done (disks=%s)", self._disks,
        )

    # ------------------------------------------------------------------
    # Physical gates
    # ------------------------------------------------------------------
    def _disks_stable(self) -> bool:
        """A rotation moves EVERY disk at once, so every occupied disk must
        hold an object that requested the rotation (finished its stage)."""
        for idx, object_id in enumerate(self._disks):
            if object_id is None:
                continue
            state = self._obj_state(object_id)
            meta = self._state_meta.get(state or "", {})
            if meta.get("position") != self._disk_names[idx]:
                return False
            if not meta.get("rotate_request"):
                return False
        return True

    def _subsystem_needed_on(
        self, subsystem: str, start_state: str, disk_name: str
    ) -> bool:
        """Walk the object journey forward from `start_state` (it is linear
        in object.yaml); while the object stays on `disk_name`, does
        `subsystem` act on it (single or parallel actor)?"""
        state = start_state
        seen: Set[str] = set()
        while state and state not in seen:
            seen.add(state)
            meta = self._state_meta.get(state, {})
            if meta.get("position") != disk_name:
                return False
            actors = set(meta.get("parallel_actors") or [])
            if meta.get("actor"):
                actors.add(meta["actor"])
            if subsystem in actors:
                return True
            state = self._leave_target.get(state)
        return False

    def _robot_needed_soon(self) -> bool:
        """Is a disk object about to be rotated back to a position where the
        dispatching robot must act on it (the measurement stage)?

        Gates the return-rotation (combined with the robot's availability):
        the rotation may not land while the robot is busy or has a task in
        flight, because the measurement stage commands it the moment the
        object arrives. Robot dispatch is NOT gated on this: the newcomer
        is placed first, so the robot is free when the rotation lands (the
        stable-disk gate keeps the rotation waiting until the placement).
        """
        if self._chain_subsystem is None or self._disk_count < 2:
            return False
        for idx, object_id in enumerate(self._disks):
            if object_id is None:
                continue
            state = self._obj_state(object_id)
            meta = self._state_meta.get(state or "", {})
            if not meta.get("rotate_request"):
                continue
            start = self._leave_target.get(state or "")
            if start is None:
                continue
            dest = self._disk_names[(idx + 1) % self._disk_count]
            if self._subsystem_needed_on(self._chain_subsystem, start, dest):
                return True
        return False

    def _pending_rotation_command(self) -> Optional[str]:
        """The rotate_command of the first disk whose object requested the
        rotation (the request from the entry side takes precedence)."""
        for object_id in self._disks:
            if object_id is None:
                continue
            meta = self._state_meta.get(self._obj_state(object_id) or "", {})
            command = meta.get("rotate_command")
            if command:
                return command
        return None

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------
    def _advance(self) -> None:
        """Issue the next possible commands.

        Called on every pipeline-relevant change while holding the
        controller lock (and not the orchestrator lock).
        """
        if self._phase == "scanning":
            if (
                self._reporter is not None
                and self._scan_command is not None
                and self._sm_state(self._reporter) == self._ready_state()
            ):
                logger.info(
                    "Pipeline: commanding box scan (%s/%s)",
                    self._reporter, self._scan_command,
                )
                self._command(self._reporter, self._scan_command)
            return
        if self._phase != "pipeline":
            return

        robot_needed_soon = self._robot_needed_soon()

        # 1. Robot dispatch – checked BEFORE the pending rotation: the next
        #    object leaves the box as soon as the entry disk position is
        #    free and the robot is free, even while a disk object is waiting
        #    to come back to the robot's position. Placing the newcomer
        #    first keeps two objects on the platform at all times (its
        #    imaging overlaps the returning object's measurement) and is
        #    safe: the return-rotation below cannot start while the newcomer
        #    is in transit (an in-transit object sits on the entry disk with
        #    meta.position 'hand', so _disks_stable() is false until it is
        #    placed), and a placed newcomer means the robot is free when the
        #    rotation lands – the measurement commands are never lost.
        if (
            self._robot_task is None
            and self._hand_slots > 0
            and not self._rotating
            and self._chain_subsystem is not None
            and self._sm_state(self._chain_subsystem) == self._ready_state()
            and self._entry_disk is not None
            and self._disks[self._entry_disk] is None
            and self._queue_pos < len(self._order)
        ):
            object_id = self._order[self._queue_pos]
            if self._obj_state(object_id) == self._dispatch_state:
                logger.info("Pipeline: dispatching %s to the platform", object_id)
                self._disks[self._entry_disk] = object_id
                self._queue_pos += 1
                if self._dispatch_event and self._fire_object_event(
                    object_id, self._dispatch_event
                ):
                    subsystem, entry = self._chain_by_object_event[self._dispatch_event]
                    self._robot_task = (
                        object_id,
                        self._chain_completion(subsystem, entry["op"]),
                    )
                else:
                    self._disks[self._entry_disk] = None
                    self._queue_pos -= 1

        # 2. Pending platform rotation (every disk must be stable – an
        #    in-transit object on the entry disk keeps it unstable until
        #    placed – and if an object comes back to the measurement
        #    position the dispatching robot must be ready and task-free: it
        #    is needed the moment the rotation lands).
        if (
            self._rotation_requested
            and not self._rotating
            and self._platform is not None
            and self._sm_state(self._platform) == self._ready_state()
            and self._disks_stable()
            and not (
                robot_needed_soon
                and (
                    self._sm_state(self._chain_subsystem) != self._ready_state()
                    or self._robot_task is not None
                )
            )
        ):
            command = self._pending_rotation_command()
            if command:
                logger.info("Pipeline: commanding platform rotation (%s)", command)
                self._command(self._platform, command)
                self._rotation_requested = False
                self._rotating = True

        # 3. Run finished? (every object back in the box)
        if (
            self._queue_pos >= len(self._order)
            and all(d is None for d in self._disks)
            and self._robot_task is None
            and not self._rotating
            and self._measuring_object is None
        ):
            self._phase = "suspended"
            self._finish_workflow()

    def _finish_workflow(self) -> None:
        """Complete the run: running -> stopping -> stopped (objects remain
        visible in the GUI for the operator to review)."""
        wf = self._orch.workflow_names
        logger.info(
            "Pipeline: %d object(s) processed – stopping the workflow",
            len(self._order),
        )
        self._orch.fire_event(wf.operator_events.get("stop", "stop_requested"))
        self._orch.fire_event(
            wf.stop_completion_event or "stopped"
        )
