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
- Object data is stored **locally**; the local database is updated manually — there is no cloud_database subsystem (operator alias IDs are the local-database IDs).
- Imaging on the second disk position is two sequential stages, **3DI first then HSI**, handled by two subsystems: `imaging_3di` and `imaging_hsi` (see Latest changes 2026-09-16).

## Latest changes (2026-09-16 — cloud_database removed; imaging split into 3DI + HSI)

Two subsystem changes (see `docs/PROJECT_CONTEXT.md` for the machine context):

- **cloud_database removed** — object data is stored locally on the machine
  and the local database is updated manually (no upload command anymore).
  `object.yaml`: `moving_to_box` no longer fires `cloud_database/upload`,
  and `object_analyzed` now says the data is stored locally. `workflow.py`
  `_on_object_event` no longer builds the cloud-upload payload (object_id/
  alias). Deleted `backend/config/subsystems/cloud_database.yaml` and
  `backend/simulation/cloud_database_sim.py`; adapter constants
  (`CLOUD_DB`), the `launch_gui.py` sim list, tests and docs updated.
  Alias IDs are KEPT: they are now the local-database IDs (wording updated
  in backend, frontend tooltips/comments and docs).
- **imaging_3di_hsi split into imaging_3di + imaging_hsi** (3DI before HSI).
  New YAMLs + sims `imaging_3di` (capturing 3.0 s) and `imaging_hsi`
  (capturing 4.0 s); the combined YAML/sim are deleted. `robot_second.yaml`
  has two scan stages: `scan_3d` → `scanning_3d` (2.5 s) and `scan_hsi` →
  `scanning_hsi` (3.5 s). Object workflow: the single `HSI_3DI_analysis`
  state is replaced by `3DI_analysis` (fires `imaging_3di/capture` +
  `robot_second/scan_3d`) → `3di_capture_complete` → `HSI_analysis` (fires
  `imaging_hsi/capture` + `robot_second/scan_hsi`) → `hsi_capture_complete`
  → `object_going_first_position` (the `ai_analysis/plan` +
  `workflow/rotation_requested_first` actions moved here from the old
  combined state). `pipeline.py`: completion handlers for both imaging
  subsystems (3DI completion keeps `_imaging_object` set, HSI completion
  clears it; both guard on the object's current state) and a two-stage skip
  guard in `_finish_rotation` (an unavailable 3DI/HSI subsystem skips its
  stage immediately). `global.yaml` state_order/state_colors, frontend
  `object-styles.ts` defaults and the `ObjectState` enum were updated.
- simulation_test.py: expected states/transitions/scoped events updated
  (TEST 2 disk-position states, TEST 7 subsystem config — now also checks
  `cloud_database` is gone, TEST 8, TEST 9, TEST 10).

## Next steps

1. **MQTT integration with real subsystems** — receive path is wired; still needed: reconnection/health checks, command/response correlation, retry policies.
2. **Reliability & recovery** — subsystem health checks, graceful degradation when non-critical subsystems are offline, workflow checkpoint/restore.
3. **CI/CD** — automated runs of `simulation_test.py`, linting (ruff/mypy, eslint/prettier), docker compose (orchestrator + mosquitto + frontend).
4. **Enhanced UI** — alarm/notification system, data export, per-object data view (images/measurement points).
