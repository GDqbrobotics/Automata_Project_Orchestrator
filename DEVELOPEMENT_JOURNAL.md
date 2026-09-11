# Development Journal — AUTOMATA GUI Orchestrator

Keep this file brief, is meant to show the next developer the work of the last one.

## Current state

Working prototype: TypeScript/Vite frontend + Python orchestrator + **process-based simulation** (one simulated subsystem process per real subsystem, all MQTT).

- Launch everything: `python launch_gui.py --simulation --num-objects 3` (UI on http://localhost:3000; starts mosquitto + 9 `backend/simulation/*_sim.py` processes + backend + frontend)
- Validate: `timeout 500 python backend/simulation_test.py` — **always wrap in a timeout** (a bug can make it hang forever)
- All state machines are YAML-driven: `backend/config/workflow.yaml`, `object.yaml`, `subsystems/*.yaml`, `global.yaml`. Simulated subsystems load the *same* YAML as the orchestrator. Work durations live in the subsystem YAML metadata (`work_durations`).
- The object flow is driven by `backend/orchestrator/pipeline.py` (PipelineController) from MQTT status reports — identical code path for simulated and real subsystems.

## Latest changes (2026-09-11 — robot duty split: imaging on robot 2, XRF/Raman on robot 1)

Workflow update from docs/PROJECT_CONTEXT.md: robot 1 keeps pick-and-place
but now performs **XRF/Raman** (end effector: gripper+XRF+Raman); robot 2
performs the **3DI/HSI scan** (end effector: HSI+3DI). Consequences:

- **Positions swapped**: imaging now happens on the **second** disk
  position, XRF/Raman on the **first** one; the AI plans the points
  between imaging and measurement, in parallel with the rotation back.
- **object.yaml**: same 9 states, new order —
  `moving_to_disk → object_going_second_position → HSI_3DI_analysis →
  object_going_first_position → XRF_RAMAN_analysis → moving_to_box`.
  Scoped events moved accordingly (`robot_second/scan_hsi_3d` on arrival at
  the second position; `robot_first/measure_xrf_raman` + `xrf/measure` +
  `raman/measure` on arrival at the first; `ai_analysis/plan` on imaging
  complete; `robot_first/return` on measurements complete).
- **robot_first.yaml**: `measuring_hsi_3d` → `measuring_xrf_raman`
  (6 s, parallel to the analyzers). **robot_second.yaml**: `use_xrf`/
  `use_raman` → `scan_hsi_3d` (4 s, parallel to the imaging capture);
  now `required: true` (core imaging actuator).
- **global.yaml** `state_order` + frontend `DEFAULT_STATE_ORDER` updated to
  the new sequence.
- **pipeline.py** (minimal, unavoidable — stage↔position mapping lives in
  code): `MEASUREMENT_SUBSYSTEMS = (xrf, raman, robot_first, ai_analysis)`
  (first-position stage; AI counted only while still planning); imaging
  stage now starts in `_finish_rotation` (object arrives on the second
  disk) with the not-capturing skip guard moved there; robot 1's
  measuring completion counts as a measurement completion; the
  `return` command now comes from the YAML (`measurements_complete`), and
  `_complete_measurements()` tracks the return task so the robot's
  completion releases the first disk slot.
  **New gates** (robot 1 now measures AND transports — two deadlock
  scenarios found in simulation): a rotation that lands a
  `object_going_first_position` object on the first position waits for
  robot 1 to be free (it is commanded to measure it immediately), and no
  new pick is dispatched while the second-disk object is coming back for
  measurement (its `place` would collide with the landing).
- simulation_test.py expectations updated (disk-position state sets,
  scoped events, transition sequences).

## Latest changes (2026-09-11 — subsystem cards flashing "initializing" fix)

**Backend bug** (frontend was innocent): `WorkflowStatus.active_subsystems`
held `SubsystemInfo` snapshots taken once at startup (all `initializing`).
The main runtime paths that move the local subsystem SMs — MQTT status
reports (`_update_subsystem_status`/`_sync_subsystem_sm`) and scoped object
events (`_route_event_to_subsystem`) — never refreshed them, while *every*
full status broadcast (fired on each object transition via
`_broadcast_objects`) re-sent the stale snapshot, so the frontend (which
correctly treats full broadcasts as authoritative) flashed
"Initializing" — a state the SM can never return to — until the next
per-subsystem heartbeat (≤2 s).

- `WorkflowOrchestrator._refresh_subsystem_info(name)` rebuilds the
  snapshot from the adapter; called on every local SM move (status-report
  sync, scoped/routed events, broadcasts, start's stopped→ready).
- Reconnecting clients now get a FRESH full status
  (`FrontendServer.set_status_callback` + fresh `broadcast_with_display_config`
  in `_ws_handler`, cache only as fallback) — previously the cached
  `_last_status_payload` replayed the startup `initializing` states.
- Verified: live WS probe — 0 full broadcasts carrying stale
  `initializing` mid-run (was 25), mid-run reconnect shows live states;
  106/106 simulation tests.

## Next steps

1. **MQTT integration with real subsystems** — receive path is wired; still needed: reconnection/health checks, command/response correlation, retry policies.
2. **Reliability & recovery** — subsystem health checks, graceful degradation when non-critical subsystems are offline, workflow checkpoint/restore.
3. **CI/CD** — automated runs of `simulation_test.py`, linting (ruff/mypy, eslint/prettier), docker compose (orchestrator + mosquitto + frontend).
4. **Enhanced UI** — alarm/notification system, data export, per-object data view (images/measurement points).
