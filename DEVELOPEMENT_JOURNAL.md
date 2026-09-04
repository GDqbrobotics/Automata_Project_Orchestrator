# Development Journal — AUTOMATA GUI Orchestrator

Keep this file brief, is meant to show the next developer the work of the last one.

## Current state

Working prototype: TypeScript/Vite frontend + Python orchestrator + **process-based simulation** (one simulated subsystem process per real subsystem, all MQTT).

- Launch everything: `python launch_gui.py --simulation --num-objects 3` (UI on http://localhost:3000; starts mosquitto + 9 `backend/simulation/*_sim.py` processes + backend + frontend)
- Validate: `timeout 500 python backend/simulation_test.py` — **always wrap in a timeout** (a bug can make it hang forever)
- All state machines are YAML-driven: `backend/config/workflow.yaml`, `object.yaml`, `subsystems/*.yaml`, `global.yaml`. Simulated subsystems load the *same* YAML as the orchestrator. Work durations live in the subsystem YAML metadata (`work_durations`).
- The object flow is driven by `backend/orchestrator/pipeline.py` (PipelineController) from MQTT status reports — identical code path for simulated and real subsystems.

## Recent changes (2026-09-04 — process-based simulation, MQTT-only boundary)

The in-process simulator (`orchestrator/simulation.py`, deleted) drove subsystem
state machines directly, so simulated subsystems were NOT the same as the real
ones. Now every simulated subsystem is an **individual process** identical to
its real counterpart:

- **`backend/simulation/`**: `subsystem_sim.py` (generic) + one thin script per
  subsystem (`robot_first_sim.py`, `rotating_platform_sim.py`, `vision_sim.py`,
  ...). Each process loads the *same* YAML SM as the orchestrator, receives
  commands on `automata/subsystem/<name>/event`, reports on
  `automata/subsystem/<name>/status` (every change + 2 s heartbeat), handles
  pause/resume by freezing its work timer. Durations come from YAML metadata
  (`work_durations`/`init_duration`). Vision publishes the detected objects on
  `automata/subsystem/vision/response` (`objects_detected`).
- **`backend/orchestrator/pipeline.py`** (new, permanent — also the real-mode
  logic): event-driven `PipelineController`. Vision scan → object discovery →
  dispatch respecting physical capacity (2 disk slots, 1 end effector) →
  coupled platform rotations (only when both disks are stable) → completion →
  automatic workflow stop. Reacts only to status reports; works identically
  with simulated or real subsystems.
- **MQTT boundary fixed (latent bug)**: subsystem SM transitions never
  published MQTT (they only had `log_event` actions, so the old on-event
  callback never fired). Publishing now happens in
  `SubsystemAdapter.fire_transition` on every transition, except completion
  reports (`complete`/`init_complete` — `REPORT_EVENTS`), which flow the other
  way via `/status`.
- **Orchestrator**: local subsystem SMs are mirrored from status reports
  (`_sync_subsystem_sm`: reported `ready` fires `complete`/`init_complete`/...)
  so command gating stays correct; pause/resume are forwarded to all
  subsystems as raw MQTT commands (`_broadcast_command_to_subsystems`);
  `start` brings stopped subsystems back to ready and fast-paths
  `subsystem_ready` when all required adapters already report ready;
  adapter initial state is now `offline` until the first report; frontend gets
  the raw reported state (`picking`, `rotating`, ...) for subsystem badges.
- **object.yaml**: `imaging_capture_complete`/`measurements_complete` now fire
  internal `workflow/rotation_requested_second|first` events (consumed by the
  controller, which decides when a rotation is physically safe); the
  `platform_rotated_to_second` transition also commands `robot_second/use_xrf`.
- **`launch_gui.py`**: starts the 9 simulated subsystem processes in
  simulation mode.
- **Bug found & fixed in test infra**: mosquitto was started with `-v` and
  its pipes were never drained — after ~64 KB of verbose logs the broker
  *blocked on write* and froze the whole pipeline mid-run (looked like a
  stall at object 2). Broker now runs without `-v` and all child pipes
  (broker/sims/backend/frontend) are drained in both the test and the launcher.

Verified: full suite 106/106 (3 and 5 objects); end-to-end probe: vision
scan → 4 objects → continuous 2-disk flow → auto-stop; pause freezes the
pipeline over MQTT and resume finishes it; physical invariants (≤1 in robot's
hand, ≤1 per disk, ≤2 on platform) asserted during the test run.

## Previous changes (2026-09-03 — physical 2-slot compatibility + visible stages)

The pipeline violated the machine's physical capacity: the platform has only
**2 disk slots** and robot 1 only **one end effector**, but with N > 2 objects
all objects left the box at workflow start (probe: 5 objects → peak 5 out of
the box, 4 simultaneously in `moving_to_disk`).

- **Object SM** (`object.yaml`): new state `waiting_for_disk` —
  `identified_in_box --queued_for_analysis--> waiting_for_disk
  --object_picked--> moving_to_disk`. Objects stay in the box (visible
  queue in the GUI) until a disk slot is actually free.
- **Simulation**: the worker now claims a free first-disk slot *before*
  the robot picks the object; the disk slot is released exactly once (owner
  check in `finally`); cloud uploads are serialized (`_cloud_lock`, the cloud
  SM allows one upload at a time); the platform thread exits when the
  pipeline is done. New helper `_complete_subsystem(name, working_state)`
  fires `complete` only if the subsystem is really in that working state,
  so a rejected stage start can never end another object's session.
- **Robot 1 SM** (`robot_first.yaml`): new states `placing` and
  `measuring_hsi_3d`. The robot holds the imaging tool during 3DI/HSI:
  the object transition `object_placed_on_disk` now also fires
  `robot_first/measure_hsi_3d`, and the worker completes it together with
  the imaging subsystem. (Event was added as `mesure_hsi_3d` — renamed to
  `measure_hsi_3d`.)
- **Slower stages for GUI visibility**: vision 3 s, pick 2 s, place 2 s,
  imaging 4 s, rotation 2 s, measurement 6 s, return 2.5 s, upload 2 s
  (constants at the top of `simulation.py`). Test `max_wait` raised to 180 s.
- **GUI colors**: `global.yaml` state colors now use the CSS class names
  (`identified`, `waiting`, `moving`, `imaging`, `rotating`, `measuring`,
  `analyzed` — before they used `gray`/`blue`/... which matched no CSS
  rule, so badges rendered uncolored). Added CSS for `state-waiting` and
  for subsystem working states that had none (`picking`, `placing`,
  `returning`, `measuring_hsi_3d`, `rotating`, `using_xrf`, `using_raman`).
- **Tests**: 106/106. The full-workflow test now samples object states
  during the run and asserts the physical invariants: ≤ 1 object in the
  robot's hand, ≤ 1 object per disk position, ≤ 2 objects on the platform.

Verified: probe with 5 objects → peak 2 objects out of the box, robot in
`measuring_hsi_3d` in 100% of imaging samples, all 9 subsystems reach their
working states; live GUI run shows the queued objects and all transitions.

## Previous changes (2026-09-02 — continuous pipeline simulation)

The simulation previously processed objects in slow sequential rounds: subsystems
flipped to their working state and back to `ready` within milliseconds (the UI
always showed `ready`), and the next object could not start until the previous
one finished.

`backend/orchestrator/simulation.py` was rewritten to model the machine as it
physically works (docs/PROJECT_CONTEXT.md):

- **Two disk positions**: one object can be imaged (first position) while
  another is measured (second position); objects now flow continuously
  (object N+1 is picked and imaged while object N is under XRF/Raman).
- **Coupled platform rotations**: a dedicated platform thread rotates BOTH
disks at once (imaging-done → measurement position, measurement-done → back),
  only when both objects have finished their stage.
- **One worker thread per object** drives the object state machine
  (`object.yaml` stays the source of truth); `robot_first` is shared and
  serialized with a lock; the vision subsystem scans the box at workflow
  start (objects are created after the scan).
- **Visible subsystem states**: subsystems now stay in their working state
  (picking, capturing, rotating, measuring, analyzing, using_xrf, uploading)
  for the whole duration of each stage (1–3 s each, tuned for GUI visibility).
  `robot_second` participates in the measurement stage; `state_description`
is now included in subsystem state events (subsystem cards show it).
- **Progress sync**: `fire_object_transition` now also updates
  `_status.progress`, so full-status broadcasts no longer reset the UI
  progress bar.

Verified: full test suite 101/101; live end-to-end with 3 objects shows
parallel pick/imaging overlapping measurement, monotonic progress, all 9
subsystems transitioning, and working pause/resume.

## Previous changes (2026-09-01 — verification pass + fixes)

Full doc-vs-code verification was performed; the repo now matches its documentation. Fixed:

- **MQTT receive path (was dead)**: `MqttClient` never registered `_on_message_wrapper` on the paho client, so incoming `automata/subsystem/*/status` messages were silently dropped. Now registered in `MqttClient.__init__`; real subsystems can drive the orchestrator over MQTT (e.g. `starting → running` on status updates).
- **Progress reporting**: `fire_object_transition()` broadcast the number of *known* objects instead of *analyzed* ones, so the UI flickered (e.g. 2/2 → 1/2). It now counts objects in `object_analyzed` (helper `_count_analyzed_objects()`) and writes under the orchestrator lock.
- **Stale test**: `simulation_test.py` expected an intermediate `stopping` state after `stop_workflow`, but the orchestrator completes `stopping → stopped` immediately. Test updated; suite passes 101/101.
- **Config-driven state messages**: the `workflow_states` messages in `global.yaml` are now sent to the frontend with the display config and rendered in the UI (`frontend/src/config/state-messages.ts` remains the fallback).
- **`VITE_WS_URL` honored**: the frontend now reads the env var set by `launch_gui.py`, so `--ws-port` actually reaches the UI (default `ws://localhost:8080` otherwise).
- **Cleanup**: `ObjectStateMachines.clear()` removes persisted `object_states/*.json` files (they accumulated across runs); removed unused `shared/contracts.*` (the frontend uses its own `frontend/src/contracts.ts`).

## Next steps

1. **MQTT integration with real subsystems** — receive path is wired; still needed: reconnection/health checks, command/response correlation, retry policies.
2. **Reliability & recovery** — subsystem health checks, graceful degradation when non-critical subsystems are offline, workflow checkpoint/restore.
3. **CI/CD** — automated runs of `simulation_test.py`, linting (ruff/mypy, eslint/prettier), docker compose (orchestrator + mosquitto + frontend).
4. **Enhanced UI** — backend endpoint for alias ID assignment (the frontend button currently only logs), expandable object detail panel, alarm/notification system, data export.
