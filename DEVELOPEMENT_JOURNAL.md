# Development Journal — AUTOMATA GUI Orchestrator

Keep this file brief, is meant to show the next developer the work of the last one. Latest changes paragraph always updated.  

## Current state

Working prototype: TypeScript/Vite frontend + Python orchestrator + **process-based simulation** (one simulated subsystem process per real subsystem, all MQTT).

- Launch everything: `python launch_gui.py --simulation --num-objects 3` (UI on http://localhost:3000; starts mosquitto + backend + frontend — in simulation mode the *backend* starts the `backend/simulation/*_sim.py` processes itself)
- Per-subsystem simulation toggle: in simulation mode each subsystem card has a **SIM/REAL** chip (before starting the workflow) to decide which subsystems are simulated and which are real
- Debug mode: `python launch_gui.py --debug` — subsystem inspector windows gain a panel to manually send the MQTT messages a real subsystem would publish (user-driven simulation), and the top bar reveals the **configuration wizard**
- MQTT payload formats are configurable: `backend/config/mqtt_formats.yaml` maps every message kind to payload fields, with per-subsystem overrides (R10, `backend/messaging/message_format.py`); the adapters, the orchestrator receive path and the simulated subsystems all build/parse through it
- Configuration wizard (debug mode only): 4 steps — workflow/global/object YAML, subsystem edit/add/remove, MQTT formats, review & save; backend `backend/services/config_service.py` validates, comment-preservingly patches and atomically writes, so the browser never touches files
- Validation: `timeout 500 python backend/simulation_test.py` — 151 tests + 28 new checks (C14 message formats, C15 config service, wizard checks in C12); frontend `npm run build` clean
- Validate: `timeout 500 python backend/simulation_test.py` — **always wrap in a timeout** (a bug can make it hang forever)
- All state machines are YAML-driven: `backend/config/workflow.yaml`, `object.yaml`, `subsystems/*.yaml`, `global.yaml`. Simulated subsystems load the *same* YAML as the orchestrator. Work durations live in the subsystem YAML metadata (`work_durations`).
- The object flow is driven by `backend/orchestrator/pipeline.py` (PipelineController) from MQTT status reports — identical code path for simulated and real subsystems.
- Object workflow has an explicit `ai_processing` state between `object_going_first_position` and `XRF_RAMAN_analysis`: the object waits on the first position until the AI finishes extracting the XRF/Raman interest points (see Latest changes 2026-09-14 / ai_processing).
- Object data is stored **locally**; the local database is updated manually — there is no cloud_database subsystem (operator alias IDs are the local-database IDs).
- Imaging on the second disk position is two sequential stages, **3DI first then HSI**, handled by two subsystems: `imaging_3di` and `imaging_hsi`.
- The orchestrator's local **subsystem SM is a mirror, not a predictor**: issuing a command does NOT advance it (real subsystems lag); it only follows the state the subsystem reports over MQTT. An expected-state **discrepancy check** warns if a subsystem never reports the state it was commanded into (window: `orchestration.state_expect_timeout` in `global.yaml`, default 10 s).
- **The YAML is the single source of truth** (docs/CONFIG_CHANGE_GUIDE.md, implemented 2026-10-08): no state/event names, subsystem names, topics, durations or button rules are hardcoded in Python or TypeScript. The GUI derives its operator-button matrix, state-machine graphs, tones and display vocabulary from the same YAML, broadcast at startup; a rename in `backend/config/` is a config-only change.

## Latest changes (2026-10-27 — configuration wizard + configurable MQTT formats)

Two additions, both debug-friendly and fully config-driven.

**1. MQTT message formats are now configurable (R10).** New field-mapping
layer `backend/config/mqtt_formats.yaml` + `backend/messaging/message_format.py`.
Each message kind (`command`, `hardware_command`, `status`, `response`) maps
the logical values to payload fields; per-subsystem overrides are deep-merged
over `default:`, and dotted field names reach nested payloads (`data.state`).
Integrated in `SubsystemAdapter.fire_transition`/`send_command`,
`WorkflowOrchestrator._broadcast_command_to_subsystems`/`_update_subsystem_status`/
`_on_mqtt_message`/`_build_debug_topics`/`_report_payload` and in
`backend/simulation/subsystem_sim.py`, so orchestrator and sim can never
drift. Defaults reproduce the previous payloads exactly (151/151 pre-existing
tests pass unchanged).

**2. Configuration wizard (debug mode).** `⚙` in the top bar (revealed by
`--debug`, gated server-side on debug + workflow-not-running). Four steps:
workflow metadata/global settings/object blueprint (structured forms that
apply comment-preserving line patches + raw YAML editor), subsystems
(basics, a **structured state-machine editor**, addition from a commented
template, staged removal with reference check), MQTT formats (structured
fields + per-subsystem overrides), review & save (validate all, sequential
save, then create simulators / delete staged files). New commands
`get_config`, `validate_config`, `patch_config`, `save_config`,
`create_subsystem`, `delete_subsystem`, `write_simulator`,
`render_message_formats`, `render_state_machine`; new events
`config_snapshot`, `config_patched`, `config_validated`, `config_saved`,
`subsystem_deleted`. The browser holds no filesystem access:
`backend/services/config_service.py` validates (YAML + structural checks),
patches by line (comments preserved) and writes atomically. A restart is
always requested after saving.

**2b. State-machine editor (subsystems).** Inside the wizard's Subsystems
step, a third tab edits the machine as structured data: states (add / remove
/ description / initial), transitions (event, target) and their actions
(`log_event`, a fired scoped/internal event, or a raw preserved action).
`ConfigService.render_state_machine` validates the whole candidate document
and re-serializes **only the `states:` block** with an indenting dumper;
the file header (name, metadata and their comments) survives unchanged.
Pending drafts are rendered automatically at save time (`render_sm` ops in
the frontend save queue) and everything stays client-side until the Review
step writes it. The same editor is available for `workflow.yaml` and
`object.yaml` (Workflow step); for object states it also edits the physical
`meta` (position, actor, parallel actors, rotate request/command, actor op).

**2c. Completion contract validation (why a run can hang).** Splitting a
subsystem stage (e.g. `robot_first.measuring_xrf_raman` → `measuring_xrf` +
`measuring_raman`) only works if **every work state transitions back to the
ready state**: the pipeline (and the generic simulator) treats a work →
ready status report as completion, so a work state with no way back stalls
the object forever. Subsystem transitions also cannot fire internal events
(`event:` actions are ignored — the engine callback is not wired on either
the sim or the mirror), so the wizard now rejects them with an explanation.
`ConfigService` gained the cross-file object validation: actors must be
configured subsystems with a completion path, positions must match the
layout, rotate commands must exist, and task chains must reference real
object events/subsystem commands (see `docs/STATE_MACHINE.md` principle 13
and R2's completion contract). `simulation_test.py` covers the split journey
and the new refusals.

- Docs: `docs/CONFIG_CHANGE_GUIDE.md` (R10 + wizard section), `README.md`,
  `docs/IMPROVEMENT_IDEAS.md` (out-of-scope ideas report).
- Validation: `timeout 500 python backend/simulation_test.py` → pass
  (+28 new checks), `cd frontend && npm run build` clean.

## Earlier changes (2026-10-08 — continuous two-object flow: the platform stays full)

The next object now enters the platform as soon as the entry position frees up, instead of waiting for the current object to be completely measured and returned. Apart from the very start and the very end, **two objects are on the rotating platform at all times**: one being measured on the first position, the other being imaged on the second (the newcomer's imaging overlaps the returning object's measurement). The 3-object test run drops from ~69 s to ~58 s; steady-state throughput gains the measurement+return duration (~13 s) per object.

- **What changed** (only `backend/orchestrator/pipeline.py`, `PipelineController._advance`): dispatch is now checked **before** the pending rotation, and the dispatch gate no longer blocks a pick while a disk object waits to come back to the measurement position (the `robot_needed_soon` condition stays on the *rotation* gate only).
- **Why it is safe**: the return-rotation cannot start while the newcomer is in transit — an in-transit object sits on the entry disk with `meta.position: hand`, so `_disks_stable()` is false until the placement — and a placed newcomer means the robot is free the moment the rotation lands, so the measurement commands (robot + XRF + Raman) are never lost (the stall that motivated the old dispatch gate cannot reappear). A defensive `not self._rotating` guard keeps the robot away from a moving platform.
- This is a scheduling policy, not a layout fact: it is **not** expressible in the YAML alone (the config defines the machine and the journey; the controller decides *when* a pick/rotation is allowed) — which is why the change lives in `pipeline.py` even though no config file changed.
- Validation: `timeout 500 python backend/simulation_test.py` → **151/151**; the physical invariants (≤1 object per disk position, ≤1 in the robot's hand, ≤2 on the platform) held through the run.

## Earlier changes (2026-10-08 — YAML single source of truth: R1–R9 + F1–F8)

Implemented [docs/CONFIG_CHANGE_GUIDE.md](docs/CONFIG_CHANGE_GUIDE.md): every piece of behaviour that was hardcoded in the backend or the frontend is now derived from `backend/config/*.yaml`.

- **R1 shared vocabulary → `global.yaml`**: the non-work states, the ready state, the completion/report events, the pause/resume hardware commands, the MQTT topic prefix and the state-expect timeout live in `global.yaml` (`states:`, `mqtt.topic_prefix`, `orchestration:`); adapters, sims and the pipeline read them from `GlobalConfig` (the old `ALL_SUBSYSTEMS`/name constants and `STATE_EXPECT_TIMEOUT` are gone).
- **R2/R6 object flow → `object.yaml`**: `pipeline.py` no longer knows state names or subsystems. Per-state `meta:` (position / actor / parallel_actors / rotate_request / rotate_command / actor_op) plus top-level `metadata:` (disks, disk_names, hand_slots, task_chains) drive dispatch, the coupled-rotation gates, the measurement stage and the pick→place task chain. Object final states come from the blueprint (states without outgoing transitions).
- **R3 workflow vocabulary → `workflow.yaml`**: new `WorkflowNames` dataclass loads the `metadata:` section (operator events, readiness gate, error event/state, stop-completion event, wake transition, toggle-allowed states) and derives the run/stop/suspend states; the `WorkflowState`/`ObjectState` enums are deleted, the orchestrator compares raw state names from the engine.
- **R4 topics**: the per-YAML `mqtt_topic` is the routing truth (adapter properties + orchestrator subscriptions); **R5 reports**: a subsystem's reporting role (e.g. the box-scan object list) is declared in its YAML `metadata.reports` and consumed generically (no `if name == "vision"` left); **R7/R8/R9**: the sims are one generic `subsystem_sim.py` (`main()` derives the name from `<name>_sim.py`), and `simulation_test.py` derives every name/invariant it asserts from the same YAML files it tests.
- **Pipeline fix found while validating**: a new pick could start while a disk object was waiting to rotate back to the measurement position, so the robot was busy (placing) when the measurement stage commanded it and the run stalled after one object. The dispatch gate now blocks any pick while an object is about to return to the robot's position (the rotation gate keeps the robot-state condition); the full 3-object run completes in ~70 s and the physical invariants hold.
- **Frontend F1–F8**: the operator-button matrix is derived from the broadcast `workflow_state_machine` + `metadata.operator_events` (`config/operator-buttons.ts`, replaces `button-rules.ts`); subsystem tones come from the backend (`SubsystemStatus.tone`, computed from the configured vocabulary); the object "done" styling follows the object journey's final states; the alias constraint (pattern + length) comes from `global.yaml`; the debug panel's state/ready fields come from the topic descriptors (`kind`/`state_field`/`ready_field`) and the configured ready state. `WorkflowStateType` is now an open `string`; dead payload fields are dropped. `tsc`/`vite build` pass.
- **Docs**: README (config section, adding a subsystem), `docs/STATE_MACHINE.md` (principle 12), guide status flipped to *implemented*.
- Validation: `timeout 500 python backend/simulation_test.py` → **151/151** (up from 149: the new config-consistency checks). Frontend: `npm run build` clean.

## Next steps

1. **MQTT integration with real subsystems** — receive path is wired; still needed: reconnection/health checks, command/response correlation, retry policies.
2. **Reliability & recovery** — subsystem health checks, graceful degradation when non-critical subsystems are offline, workflow checkpoint/restore.
3. **CI/CD** — automated runs of `simulation_test.py`, linting (ruff/mypy, eslint/prettier), docker compose (orchestrator + mosquitto + frontend).
4. **Enhanced UI** — alarm/notification system, data export, per-object data view (images/measurement points).
