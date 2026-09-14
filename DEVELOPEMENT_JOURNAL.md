# Development Journal — AUTOMATA GUI Orchestrator

Keep this file brief, is meant to show the next developer the work of the last one.

## Current state

Working prototype: TypeScript/Vite frontend + Python orchestrator + **process-based simulation** (one simulated subsystem process per real subsystem, all MQTT).

- Launch everything: `python launch_gui.py --simulation --num-objects 3` (UI on http://localhost:3000; starts mosquitto + 9 `backend/simulation/*_sim.py` processes + backend + frontend)
- Debug mode: `python launch_gui.py --debug` — subsystem inspector windows gain a panel to manually send the MQTT messages a real subsystem would publish (user-driven simulation; see Latest changes 2026-09-14)
- Validate: `timeout 500 python backend/simulation_test.py` — **always wrap in a timeout** (a bug can make it hang forever)
- All state machines are YAML-driven: `backend/config/workflow.yaml`, `object.yaml`, `subsystems/*.yaml`, `global.yaml`. Simulated subsystems load the *same* YAML as the orchestrator. Work durations live in the subsystem YAML metadata (`work_durations`).
- The object flow is driven by `backend/orchestrator/pipeline.py` (PipelineController) from MQTT status reports — identical code path for simulated and real subsystems.
- Object workflow has an explicit `ai_processing` state between `object_going_first_position` and `XRF_RAMAN_analysis`: the object waits on the first position until the AI finishes extracting the XRF/Raman interest points (see Latest changes 2026-09-14 / ai_processing).

## Latest changes (2026-09-14 — object SM: ai_processing state before XRF_RAMAN_analysis)

New intermediate object state `ai_processing` between
`object_going_first_position` and `XRF_RAMAN_analysis`: the AI must finish
analyzing the object's imaging data (interest points) before the puntual
XRF/Raman measurements can start, and that wait is now an explicit object
state instead of an implicit bookkeeping detail of the pipeline.

- `object.yaml`: `object_going_first_position --platform_rotated_to_first-->
  ai_processing` (no actions); new `ai_processing --ai_complete-->
  XRF_RAMAN_analysis` fires `robot_first/measure_xrf_raman` + `xrf/measure`
  + `raman/measure` (moved here from the old `platform_rotated_to_first`
  transition). `ai_analysis/plan` still fires on `imaging_capture_complete`
  (the AI keeps running in parallel with the rotation back).
- `pipeline.py`: `MEASUREMENT_SUBSYSTEMS` no longer includes `ai_analysis`
  (it gates the stage start instead of joining the parallel set); new
  `_ai_object` bookkeeping — on rotation landing the object enters
  `ai_processing` and `_ai_object` tracks it; when the AI reports ready
  from `analyzing`, `_on_subsystem_completed` fires `ai_complete` on the
  object and starts measurement tracking (new helper
  `_start_measurement_tracking`, extracted from `_finish_rotation`). Skip
  guard: if the AI already finished during the rotation (or its plan
  command never landed), `ai_complete` fires immediately on landing so the
  pipeline never stalls. Run-finish condition + `reset()` include
  `_ai_object`.
- `global.yaml` state_order/state_colors + frontend `object-styles.ts`
  defaults: `ai_processing` inserted (new `planning` color class, green
  teal, in app.css). `ObjectState.AI_PROCESSING` added to the enum.
- simulation_test.py: expected state/transition sequences updated (121
  checks); the full-workflow run exercises BOTH paths — "AI planning done
  for X – starting measurements" (AI finishes after the rotation lands)
  and "AI already done for X – starting measurements immediately" (skip
  guard).

## Latest changes (2026-09-14 — debug mode: user-driven MQTT injection from subsystem windows)

New feature: `python launch_gui.py --debug`. In debug mode every subsystem
inspector window shows a debug panel: a selector of the MQTT topics the real
subsystem is expected to publish (`automata/subsystem/<name>/status` and
`/response`), a state quick-pick for `/status` (states come from the
subsystem YAML definition already sent to the frontend), an editable JSON
payload pre-filled with a sensible default, and a Send button. Sending
publishes the message on the broker AND feeds the orchestrator's own
`_on_mqtt_message` receive path (the broker does not loop a client's own
publishes back), so it is processed exactly like a message from the real
subsystem: local subsystem SM sync, pipeline reactions, and the inspector
feed shows it as `← received`. Like a simulation, but driven by the user.

- `launch_gui.py` / `backend/orchestrator/main.py`: new `--debug` flag →
  `WorkflowOrchestrator(debug_mode=...)`.
- `workflow.py`: `debug_mode` gate; `_build_debug_topics()` (per-subsystem
  outgoing topics + default payloads; vision's default response is an
  `objects_detected` list sized to `num_objects`); new WS command
  `debug_send_subsystem_message` (`_handle_debug_send_subsystem_message`)
  validates subsystem + topic (only `/status` and `/response` allowed) and
  payload (JSON object).
- `frontend_server.py`: `set_debug_topics()` — the descriptor travels with
  every status broadcast as `details.debug = {enabled, topics}` so
  (re)connecting clients get it; absent when debug mode is off.
- Frontend: `debug` in the store; the SM modal renders the panel (topic
  select, state select, JSON textarea, Send) built once in `open()` and
  never re-rendered, so operator edits survive live broadcasts; hidden for
  object modals and when debug mode is off. New WS command in contracts.
- `simulation_test.py`: TEST 12 (12 checks) — debug config in broadcast,
  injected status report syncs the local SM, feed shows it as received,
  `/event` topic rejected, command rejected when debug off, no `debug`
  key in broadcast when off.
- Fix (found while testing): `launch_gui.py` now starts children with
  `start_new_session=True` and shuts them down by process group —
  terminating npm used to orphan vite, which kept port 3000 bound and
  blocked the next launch (`check_ports_free`).

## Next steps

1. **MQTT integration with real subsystems** — receive path is wired; still needed: reconnection/health checks, command/response correlation, retry policies.
2. **Reliability & recovery** — subsystem health checks, graceful degradation when non-critical subsystems are offline, workflow checkpoint/restore.
3. **CI/CD** — automated runs of `simulation_test.py`, linting (ruff/mypy, eslint/prettier), docker compose (orchestrator + mosquitto + frontend).
4. **Enhanced UI** — alarm/notification system, data export, per-object data view (images/measurement points).
