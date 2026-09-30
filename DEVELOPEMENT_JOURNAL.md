# Development Journal — AUTOMATA GUI Orchestrator

Keep this file brief, is meant to show the next developer the work of the last one. Latest changes paragraph always updated.  

## Current state

Working prototype: TypeScript/Vite frontend + Python orchestrator + **process-based simulation** (one simulated subsystem process per real subsystem, all MQTT).

- Launch everything: `python launch_gui.py --simulation --num-objects 3` (UI on http://localhost:3000; starts mosquitto + backend + frontend — in simulation mode the *backend* starts the `backend/simulation/*_sim.py` processes itself)
- Per-subsystem simulation toggle: in simulation mode each subsystem card has a **SIM/REAL** chip (before starting the workflow) to decide which subsystems are simulated and which are real
- Debug mode: `python launch_gui.py --debug` — subsystem inspector windows gain a panel to manually send the MQTT messages a real subsystem would publish (user-driven simulation)
- Validate: `timeout 500 python backend/simulation_test.py` — **always wrap in a timeout** (a bug can make it hang forever)
- All state machines are YAML-driven: `backend/config/workflow.yaml`, `object.yaml`, `subsystems/*.yaml`, `global.yaml`. Simulated subsystems load the *same* YAML as the orchestrator. Work durations live in the subsystem YAML metadata (`work_durations`).
- The object flow is driven by `backend/orchestrator/pipeline.py` (PipelineController) from MQTT status reports — identical code path for simulated and real subsystems.
- Object workflow has an explicit `ai_processing` state between `object_going_first_position` and `XRF_RAMAN_analysis`: the object waits on the first position until the AI finishes extracting the XRF/Raman interest points (see Latest changes 2026-09-14 / ai_processing).
- Object data is stored **locally**; the local database is updated manually — there is no cloud_database subsystem (operator alias IDs are the local-database IDs).
- Imaging on the second disk position is two sequential stages, **3DI first then HSI**, handled by two subsystems: `imaging_3di` and `imaging_hsi`.
- The orchestrator's local **subsystem SM is a mirror, not a predictor**: issuing a command does NOT advance it (real subsystems lag); it only follows the state the subsystem reports over MQTT. An expected-state **discrepancy check** warns after 10 s if a subsystem never reports the state it was commanded into.

## Latest changes (2026-09-30 — subsystem SM mirrors reported state + discrepancy warning)

The orchestrator's local subsystem state machines no longer advance when a command is issued — they used to jump to the working state immediately, which collides with the *real* state a subsystem reports over MQTT once execution latency kicks in (the local SM and the hardware disagreed, and the pipeline could gate on a state the hardware had not reached).

- **Commands no longer transition the local SM** (`SubsystemAdapter.fire_transition`): it now only publishes the command to `automata/subsystem/<name>/event` and records the state the subsystem is *expected* to enter. The local SM is advanced solely from MQTT status reports via the new `SubsystemAdapter.mirror_reported_state()`, which finds the YAML transition into the reported state and applies it **without re-publishing MQTT** (new `StateMachineEngine.event_to()` helper). The simulated subsystem processes are untouched — they still transition on commands, as the simulated hardware should.
- **Expected-state discrepancy check**: each command arms a 10 s timer (`STATE_EXPECT_TIMEOUT`, `backend/adapters/subsystem.py`). If the subsystem never reports the expected state (or otherwise shows it reacted — i.e. leaves the pre-command state) within the window, a warning is logged and a `subsystem_state_mismatch` event is sent to the frontend (shown as a warning in the activity log; the card keeps showing the real reported state). Timers are cleared on report, workflow reset, simulation toggle, and orchestrator stop.
- **Pipeline gates on the *effective* state** (`WorkflowOrchestrator.subsystem_effective_state`): the pending expected working state while a command is in flight, else the last reported state. This keeps physical gating correct now that the local SM lags commands (e.g. “is 3DI capturing?” stays true between the `capture` command and its confirming report).
- Frontend: new `subsystem_state_mismatch` event + `warning` log entry. Docs updated (`docs/STATE_MACHINE.md` principles 11–12).
- `simulation_test.py` still passes 149/149; the full end-to-end run (TEST 2, real simulated processes) raises no discrepancy warnings, while the no-hardware tests (3/4/5) correctly flag non-responding subsystems.

## Next steps

1. **MQTT integration with real subsystems** — receive path is wired; still needed: reconnection/health checks, command/response correlation, retry policies.
2. **Reliability & recovery** — subsystem health checks, graceful degradation when non-critical subsystems are offline, workflow checkpoint/restore.
3. **CI/CD** — automated runs of `simulation_test.py`, linting (ruff/mypy, eslint/prettier), docker compose (orchestrator + mosquitto + frontend).
4. **Enhanced UI** — alarm/notification system, data export, per-object data view (images/measurement points).
