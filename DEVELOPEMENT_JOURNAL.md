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

## Latest changes (2026-11-04 — multi-point analysis dispatch, R13)

The puntual analyses now walk ALL the AI points of interest, one per
round (R13 in `docs/CONFIG_CHANGE_GUIDE.md`, built on the object journey
the operator drafted: the `puntual_measurements` dispatch state):

- `object.yaml`: after `ai_processing`, the object reaches
  `puntual_measurements` — a state with **no actor** whose
  `meta.points_dispatch` declares, per points label, the event that leads
  to its analysis (`analyze_next_xrf_point` → XRF_analysis,
  `analyze_next_raman_point` → RAMAN_analysis) and the
  `puntual_measurements_complete` done event. Each analysis returns to
  the dispatch state over its completion event; each dispatch transition
  carries that round's commands (robot + analyzer).
- The pipeline fires the dispatch: entering the state, it fires the first
  declared event whose label still has unconsumed points, and the done
  event when none remain. The number of points is **whatever the AI
  report carried** (dynamic, R13b — `labels` in the producer YAML is the
  label vocabulary, not a fixed count); the robot's measure requests
  declare `select: next` — each measurement command **consumes** the next
  unconsumed point of its label (per-object cursors in the points store).
- Validation: dispatch events must leave the dispatch state and reference
  producer-declared labels; `select` must be `all`/`next`.
- **Found and fixed a paho-mqtt 2.x API break**: `MqttClient.disconnect()`
  called `loop_stop(timeout=…)`/`disconnect(timeout=…)` — neither accepts
  a timeout in paho 2.1, the `TypeError` was silently swallowed, and every
  "stopped" orchestrator kept its MQTT connection and subscriptions alive,
  processing messages forever (duplicated stores/pushes across tests, and
  a likely cause of transient physical-invariant flaps). Disconnects now
  actually happen.
- Tests: Test 17 asserts one push per measurement round with distinct,
  in-order coordinates; the journey-walk helper handles the dispatch
  branch. **221/221** pass.

## Earlier changes (2026-11-04 — interface lifecycle + broker-start fixes)

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

## Next steps

1. **MQTT integration with real subsystems** — receive path is wired; still needed: reconnection/health checks, command/response correlation, retry policies.
2. **Reliability & recovery** — subsystem health checks, graceful degradation when non-critical subsystems are offline, workflow checkpoint/restore.
3. **CI/CD** — automated runs of `simulation_test.py`, linting (ruff/mypy, eslint/prettier), docker compose (orchestrator + mosquitto + frontend).
4. **Enhanced UI** — alarm/notification system, data export, per-object data view (images/measurement points).
