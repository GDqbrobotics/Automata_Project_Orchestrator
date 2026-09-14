# Development Journal — AUTOMATA GUI Orchestrator

Keep this file brief, is meant to show the next developer the work of the last one.

## Current state

Working prototype: TypeScript/Vite frontend + Python orchestrator + **process-based simulation** (one simulated subsystem process per real subsystem, all MQTT).

- Launch everything: `python launch_gui.py --simulation --num-objects 3` (UI on http://localhost:3000; starts mosquitto + 9 `backend/simulation/*_sim.py` processes + backend + frontend)
- Debug mode: `python launch_gui.py --debug` — subsystem inspector windows gain a panel to manually send the MQTT messages a real subsystem would publish (user-driven simulation; see Latest changes 2026-09-14)
- Validate: `timeout 500 python backend/simulation_test.py` — **always wrap in a timeout** (a bug can make it hang forever)
- All state machines are YAML-driven: `backend/config/workflow.yaml`, `object.yaml`, `subsystems/*.yaml`, `global.yaml`. Simulated subsystems load the *same* YAML as the orchestrator. Work durations live in the subsystem YAML metadata (`work_durations`).
- The object flow is driven by `backend/orchestrator/pipeline.py` (PipelineController) from MQTT status reports — identical code path for simulated and real subsystems.

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
