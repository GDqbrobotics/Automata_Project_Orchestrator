# Development Journal — AUTOMATA GUI Orchestrator

Keep this file brief, is meant to show the next developer the work of the last one.

## Current state

Working prototype: TypeScript/Vite frontend + Python orchestrator + parallel-pipeline simulation.

- Launch everything: `python launch_gui.py --simulation --num-objects 3` (UI on http://localhost:3000)
- Validate: `python backend/simulation_test.py` — **always wrap in a timeout** (a bug can make it hang forever)
- All state machines are YAML-driven: `backend/config/workflow.yaml`, `object.yaml`, `subsystems/*.yaml`, `global.yaml`. Frontend display rules live in `frontend/src/config/`.

## Recent changes (2026-09-01 — verification pass + fixes)

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
