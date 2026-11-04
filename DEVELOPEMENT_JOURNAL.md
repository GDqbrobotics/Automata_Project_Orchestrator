# Development Journal — AUTOMATA GUI Orchestrator

Keep this file brief, is meant to show the next developer the work of the last one. Latest changes paragraph always updated.  

## Current state

Working prototype: TypeScript/Vite frontend + Python orchestrator + **process-based simulation** (one simulated subsystem process per real subsystem, all MQTT).

- Launch everything: `python launch_gui.py --simulation --num-objects 3` (UI on http://localhost:3000; starts mosquitto + backend + frontend — in simulation mode the *backend* starts the `backend/simulation/*_sim.py` processes itself)
- Per-subsystem simulation toggle: in simulation mode each subsystem card has a **SIM/REAL** chip (before starting the workflow) to decide which subsystems are simulated and which are real
- Real-interface scripts: a subsystem's `metadata.interface` (R11) declares the headless startup/shutdown commands for its real interface; the orchestrator launches them (start, wake, SIM→REAL toggle / when the subsystem reports `stopped`) via `backend/services/interface_manager.py`
- Debug mode: `python launch_gui.py --debug` — subsystem inspector windows gain a panel to manually send the MQTT messages a real subsystem would publish (user-driven simulation), and the top bar reveals the **configuration wizard**
- MQTT payload formats are configurable: `backend/config/mqtt_formats.yaml` maps every message kind to payload fields, with per-subsystem overrides (R10, `backend/messaging/message_format.py`); the adapters, the orchestrator receive path and the simulated subsystems all build/parse through it
- Configuration wizard (debug mode only): 4 steps — workflow/global/object YAML, subsystem edit/add/remove, MQTT formats, review & save; backend `backend/services/config_service.py` validates, comment-preservingly patches and atomically writes, so the browser never touches files
- Validation: `timeout 500 python backend/simulation_test.py` — **203 checks** (C14 message formats, C15 config service, C16 interface scripts, wizard checks in C12); frontend `npm run build` clean
- Validate: `timeout 500 python backend/simulation_test.py` — **always wrap in a timeout** (a bug can make it hang forever)
- All state machines are YAML-driven: `backend/config/workflow.yaml`, `object.yaml`, `subsystems/*.yaml`, `global.yaml`. Simulated subsystems load the *same* YAML as the orchestrator. Work durations live in the subsystem YAML metadata (`work_durations`).
- The object flow is driven by `backend/orchestrator/pipeline.py` (PipelineController) from MQTT status reports — identical code path for simulated and real subsystems.
- Object workflow has an explicit `ai_processing` state between `object_going_first_position` and `XRF_analysis`: the object waits on the first position until the AI finishes extracting the XRF/Raman interest points; the analysis itself is two sequential stages, `XRF_analysis` then `RAMAN_analysis` (see Latest changes 2026-09-14 / ai_processing and 2026-10-27 / completion contract).
- Object data is stored **locally**; the local database is updated manually — there is no cloud_database subsystem (operator alias IDs are the local-database IDs).
- Imaging on the second disk position is two sequential stages, **3DI first then HSI**, handled by two subsystems: `imaging_3di` and `imaging_hsi`.
- The orchestrator's local **subsystem SM is a mirror, not a predictor**: issuing a command does NOT advance it (real subsystems lag); it only follows the state the subsystem reports over MQTT. An expected-state **discrepancy check** warns if a subsystem never reports the state it was commanded into (window: `orchestration.state_expect_timeout` in `global.yaml`, default 10 s).
- **The YAML is the single source of truth** (docs/CONFIG_CHANGE_GUIDE.md, implemented 2026-10-08): no state/event names, subsystem names, topics, durations or button rules are hardcoded in Python or TypeScript. The GUI derives its operator-button matrix, state-machine graphs, tones and display vocabulary from the same YAML, broadcast at startup; a rename in `backend/config/` is a config-only change.

## Latest changes (2026-11-04 — interface lifecycle + broker-start fixes)

Two fixes from the operator's first real-hardware attempt (Robot Arm 1
switched to REAL in simulation mode):

- **Toggling SIM→REAL no longer starts the subsystem's interface** — that
  hook launched the real stack (`docker compose up`) the moment the chip
  was pressed. The interface follows the *workflow* only: it starts at
  workflow start and stops at the workflow stop state / GUI close. The
  operator flow is: toggle the subsystems to REAL (routing only, nothing
  starts), press Start — the orchestrator launches their interfaces and
  waits in `starting` until the hardware reports ready.
- **The launcher now refuses a busy MQTT port.** A stale broker left on
  1883 (e.g. bound to loopback only, as a bare `mosquitto` run loads the
  system config) made the launcher's own broker die silently at bind time
  while its wait loop 'succeeded' against the stale one — the real
  subsystems (reaching the broker through the LAN address) then got
  TCP connect failures ("MQTT client is not connected"). The launch now
  probe-binds the port up front (`check_ports_free` also covers it) and
  the generated mosquitto config uses an explicit `listener` (all
  interfaces).
- Diagnosed from `backend/interface_logs/robot_first.log` (the interface
  log captured the whole docker-compose run and the node's connect
  errors) — exactly what the interface logs are for.
- Tests: new Test 18 (SIM→REAL toggle starts no interface; workflow start
  does; workflow stop shuts it down). Full suite **214/214**.

## Earlier changes (2026-10-27 — object measurement points, R12)

Objects now carry geometric data through their lifecycle, all of it
config-declared (R12 in `docs/CONFIG_CHANGE_GUIDE.md`):

- **Producers** declare a `points:` section in a `metadata.reports[]`
  entry (kind / count / dimensions / optional labels): vision publishes
  two gripper contact points `[x y z]` per discovered object; ai_analysis
  publishes the surface points of interest `[x y z r p y]`, one per
  analysis, labeled `xrf` / `raman`. Payloads ride the `/response` topic
  through the R10 `response` format (`object_id_field` + `points_field`).
- Object-driven commands now carry the **object id** (`command` format
  `object_id_field`, set only when the command belongs to an object), so
  every subsystem — and every real one — knows what it works on.
- The orchestrator stores the points per object ID
  (`backend/orchestrator/object_points.py`) and renders them in the
  **object window** (Measurement points section: kind, source, labels,
  coordinates).
- **Consumers** declare `metadata.points_requests` (command → source +
  kind, optionally narrowed by label or index): right after the
  orchestrator publishes such a command, the selected points are pushed —
  in order — on the consumer's dedicated topic (`<mqtt_topic>/points`,
  `points_command` format). A command fired before its points are stored
  (the first pick races the box-scan report) is deferred and delivered as
  soon as the report arrives.
- The simulated producers generate deterministic placeholder geometry;
  validation cross-checks requests against the consumer's own events and
  the producer's declared kinds/labels/count. Store cleared on reset.
- Tests: new Test 17 (full simulated run: every robot command — pick,
  place, measure_xrf, measure_raman, return — receives its points push
  with the right shape; the object status carries both sets) + validation
  refusals in Test 15. **211/211** pass; frontend build clean; verified in
  the object window with headless-Chrome screenshots.

## Earlier changes (2026-10-27 — UI polish pass)

A CSS-only aesthetic pass over the operator dashboard (no behavior
changes; all class names and the YAML-driven derivation rules stay).

- Refreshed design tokens: refined dark palette with an elevation ladder
  (`surface` → `surface-4`), soft shadows, one focus ring, Inter/system
  font stack, tabular numerals, `prefers-reduced-motion` support.
- Top bar is now an elevated toolbar (brand tile + uppercase subtitle);
  panel headers got an accent tick and right-aligned count chips; buttons
  use inline SVG icons and gradient/raised styling; the event log renders
  the source as a chip (no more `[brackets]`) with an empty-state hint.
- **Fixed a pre-existing layout bug**: `.panel { display: flex }` was
  defined *after* `.control-panel { display: grid }` with equal
  specificity, so the control deck's three-column grid never applied and
  state/progress/actions always stacked vertically. The deck now uses
  `.panel.control-panel` (and the ≤1100px override matches) — the deck
  finally renders as designed.
- Modal/wizard polish: stronger backdrop blur, deeper shadows, hover and
  focus rings on inputs. Object-card top row wraps so long IDs stay
  readable next to long state names.
- Verified with headless-Chrome screenshots at 1680/1280/900 px widths,
  idle and mid-run, including the inspector modal and the wizard.
  Backend suite still **203/203** (backend untouched); `npm run build` clean.

## Earlier changes (2026-10-27 — subsystem real-interface scripts)

A real subsystem cannot initialize until its interface (ROS node, device
daemon, …) runs. The subsystem YAML can now declare the commands that
start and stop that interface (`metadata.interface`, R11 in
`docs/CONFIG_CHANGE_GUIDE.md`), and the orchestrator executes them
**headless** — detached, output appended to
`backend/interface_logs/<name>.log` — because from that moment the
subsystem state is known through MQTT only (`backend/services/interface_manager.py`).

- The lifecycle follows the **workflow, not the GUI process**: `startup`
  runs when the operator starts the workflow (a subsystem left `stopped`
  gets its interface back before the wake transition) and on a SIM→REAL
  toggle — launching the GUI starts nothing; `shutdown` runs when the
  workflow reaches its stop state (the Stop button or the automatic
  end-of-run stop, fire-and-forget) and, waited on with a 5 s timeout,
  when the GUI closes. A subsystem reporting `stopped` only marks its
  interface down (no script). A still-alive tracked startup process is
  terminated best-effort so no orphan interface survives.
- Values are shell command lines (a launcher that spawns a daemon and
  exits is as valid as a long-running process); scripts must be
  idempotent; relative paths resolve against the project root; validation
  refuses an enabled interface without a `startup` command; every
  launch/stop is mirrored to the frontend event log (`interface_command`).
- Wizard: Subsystems → Basics → *Interface (real hardware)* (enabled
  checkbox + startup/shutdown/cwd, same comment-preserving patches).
- Tests: C16 `test_interface_scripts` (startup at start, shutdown on the
  stopped report, exactly-once semantics) + validation refusals in C15;
  **203/203** pass, frontend build clean.

## Earlier changes (2026-10-27 — configuration wizard + configurable MQTT formats)

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

## Next steps

1. **MQTT integration with real subsystems** — receive path is wired; still needed: reconnection/health checks, command/response correlation, retry policies.
2. **Reliability & recovery** — subsystem health checks, graceful degradation when non-critical subsystems are offline, workflow checkpoint/restore.
3. **CI/CD** — automated runs of `simulation_test.py`, linting (ruff/mypy, eslint/prettier), docker compose (orchestrator + mosquitto + frontend).
4. **Enhanced UI** — alarm/notification system, data export, per-object data view (images/measurement points).
