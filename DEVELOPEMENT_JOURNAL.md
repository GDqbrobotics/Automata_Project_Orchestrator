# Development Journal — AUTOMATA GUI Orchestrator

Keep this file brief, is meant to show the next developer the work of the last one.

## Current state

Working prototype: TypeScript/Vite frontend + Python orchestrator + **process-based simulation** (one simulated subsystem process per real subsystem, all MQTT).

- Launch everything: `python launch_gui.py --simulation --num-objects 3` (UI on http://localhost:3000; starts mosquitto + 9 `backend/simulation/*_sim.py` processes + backend + frontend)
- Validate: `timeout 500 python backend/simulation_test.py` — **always wrap in a timeout** (a bug can make it hang forever)
- All state machines are YAML-driven: `backend/config/workflow.yaml`, `object.yaml`, `subsystems/*.yaml`, `global.yaml`. Simulated subsystems load the *same* YAML as the orchestrator. Work durations live in the subsystem YAML metadata (`work_durations`).
- The object flow is driven by `backend/orchestrator/pipeline.py` (PipelineController) from MQTT status reports — identical code path for simulated and real subsystems.

## Latest changes (2026-09-10 — alias ID assignment feature)

The alias button now works end-to-end (was log-only). The operator picks an
object in the control-deck selector, types the alias, and the backend stores
it; the machine keeps working with the auto-assigned ID and the alias is what
the cloud database uses (docs/PROJECT_CONTEXT.md).

- **Backend**: new `assign_alias` WS command (`WorkflowCommand.ASSIGN_ALIAS`)
  handled by `WorkflowOrchestrator._handle_assign_alias` — validates object
  exists, format (`[A-Za-z0-9_-. ]`, max 64), and uniqueness (aliases become
  DB IDs). Aliases live in `ObjectStateMachines._aliases` (persisted with the
  object state files, cleared on workflow reset), appear in every object
  status broadcast (`ObjectInfo.alias`), and ride in the
  `cloud_database/upload` MQTT payload: `adapter.fire_transition(event, data)`
  now merges optional context into the command payload — the upload carries
  `{object_id, alias}` (object.yaml fires `cloud_database/upload` on
  `object_returned_to_box`). Success feedback: new `object_alias_assigned`
  event; failures: `error_reported`.
- **Frontend**: alias row is now object `<select>` + input + Assign (Enter
  works); `App.assignAlias()` sends the command; object cards show the alias
  under the ID; store updates the object on `object_alias_assigned`.
- **Verified**: live WS probe — valid/invalid/duplicate/unknown/empty all
  behave, aliases visible on cards + selector populated, all 4 upload
  payloads carried `{object_id, alias}`; 106/106 simulation tests.

Verified: 106/106 simulation tests; headless-browser checks render all 10
SMs (no out-of-bounds elements, no node overlaps), store collapse/clear/
remove and modal open/close/Esc; live WS probe: SM defs in status,
sent/received topics per subsystem, transition-before-event ordering,
`pipeline (internal)` targets.

## Next steps

1. **MQTT integration with real subsystems** — receive path is wired; still needed: reconnection/health checks, command/response correlation, retry policies.
2. **Reliability & recovery** — subsystem health checks, graceful degradation when non-critical subsystems are offline, workflow checkpoint/restore.
3. **CI/CD** — automated runs of `simulation_test.py`, linting (ruff/mypy, eslint/prettier), docker compose (orchestrator + mosquitto + frontend).
4. **Enhanced UI** — alarm/notification system, data export, per-object data view (images/measurement points).
