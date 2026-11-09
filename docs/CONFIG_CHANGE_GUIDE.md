# Configuration Change Guide

This document describes the rules and patterns for modifying AUTOMATA configuration files. **All behaviour lives in YAML** — changing a duration, a state, or an event means editing a file, not touching Python/TypeScript code.

---

## Configuration Files Overview

| File | Purpose | Hot-reload |
|------|---------|------------|
| `backend/config/global.yaml` | Global defaults: MQTT, frontend, shared vocabulary, orchestration rules, simulation, display settings | Partial (requires restart for some) |
| `backend/config/workflow.yaml` | Workflow lifecycle state machine + metadata (operator events, readiness gate, error handling) | No (restart required) |
| `backend/config/object.yaml` | Object analysis journey blueprint + metadata (machine layout, task chains) | No (restart required) |
| `backend/config/subsystems/*.yaml` | Per-subsystem state machine + metadata (display, simulation timings, reporting roles) | Partial (sim durations hot-reload) |

---

## R1 — Shared Vocabulary (global.yaml)

**Single source of truth** for subsystem state semantics. Backend code **must not** hardcode these values.

```yaml
states:
  non_work: [initializing, ready, stopped, error]    # states that are NOT work stages
  report_events: [complete, init_complete]           # events flowing subsystem → orchestrator
  ready: ready                                        # the "idle, can accept work" state
  hardware_commands: [pause, resume]                 # raw MQTT commands under `all/` scope
```

**Rules:**
- Adding a new non-work state → add to `non_work` list
- Adding a new completion event → add to `report_events` list
- Changing `ready` state name → update all subsystem YAMLs' `initial_state` and transitions
- `hardware_commands` order matters: `[pause, resume]`

**Frontend impact:** Tone mapping (ready/busy/error/idle/offline) derives from this vocabulary.

---

## R2 — Object Flow (object.yaml)

The object journey is a **linear state machine** (one outgoing transition per state). Physical facts live in `meta:` per state.

### State `meta` fields

| Field | Purpose | Example |
|-------|---------|---------|
| `position` | Where the object physically is: `box`, `hand`, or disk name | `position: disk_first` |
| `actor` | Single subsystem working on the object (its completion advances the object) | `actor: imaging_3di` |
| `parallel_actors` | Subsystems that MUST ALL complete for the state to end | `parallel_actors: [robot_first, xrf]` |
| `rotate_request` | Internal `workflow/` event requesting platform rotation | `rotate_request: rotation_requested_second` |
| `rotate_command` | The `rotating_platform` command that fulfils the request | `rotate_command: rotate_to_second` |
| `actor_op` | Task-chain op name (for states where robot does multi-step work) | `actor_op: return` |

### Metadata (machine layout)

```yaml
metadata:
  disks: 2                          # number of disk slots
  disk_names: [disk_first, disk_second]
  hand_slots: 1                     # robot end effectors
  task_chains:                      # multi-step robot work per object
    robot_first:
      - op: pick
        object_event: object_picked     # object transition that starts this op
        next_command: place             # command issued when pick completes
        done_event: object_placed_on_disk
      - op: return
        done_event: object_returned_to_box
        releases: disk_first            # disk slot freed when done
```

**Rules:**
- Every state in the journey must declare `meta.position`
- States with `actor` / `parallel_actors` must reference configured subsystems
  whose state machine can actually complete (see “Completion contract”)
- States with `parallel_actors` — the pipeline tracks each actor; state ends when ALL report ready
- The event on an object state's outgoing transition is an **internal label**
  (e.g. `xrf_measurement_complete`), not something a subsystem sends: the
  pipeline fires it when the actor(s) complete
- The transition **into** a stage carries the commands that start it (e.g.
  `ai_complete` fires `robot_first/measure_xrf` + `xrf/measure`); that is how
  a sequential split like XRF → Raman is wired
- `rotate_request`/`rotate_command` pairs must match `rotating_platform.yaml` events
- `task_chains` ops must correspond to states with `actor_op` set

### Completion contract (why a run can hang)

A subsystem completes a stage by reporting its **ready state** over MQTT
after a work state — there is no "measurement complete" event a subsystem
can fire (`event:` actions on subsystem transitions are ignored and now
rejected by validation). Therefore:

- every subsystem **work state** must have a transition back to the ready
  state, or the workflow waits for that subsystem forever;
- an object state with `actor` / `parallel_actors` ends only when the
  actor(s) report ready — the object transition event is just the label.

Both rules are cross-checked by `ConfigService._validate_object` before the
wizard saves anything, and the full split journey is exercised by
`simulation_test.py`.

### Final States

States with **no outgoing transitions** are final states (e.g., `object_analyzed`). The frontend derives "done" styling from this — **no hardcoded `object_analyzed` in code**.

---

## R3 — Workflow Vocabulary (workflow.yaml)

The workflow lifecycle state machine + its `metadata:` section.

### Metadata Fields

| Field | Purpose |
|-------|---------|
| `operator_events` | Map: button name → event (start, pause, resume, stop, reset) |
| `stop_completion_event` | Event fired when leaving `stopping` to complete operator-initiated stop |
| `readiness` | `{in_state: starting, fire: subsystem_ready}` — gate: when all required subsystems report ready, fire this |
| `error_event` | Event fired on workflow SM when a required subsystem reports error |
| `error_state` | The state representing workflow error (GUI shows concrete error detail here) |
| `toggle_allowed_in` | States where simulation toggles are accepted (same as where Start is enabled) |
| `wake` | `{from_state: stopped, to_state: ready}` — subsystems in `from_state` are moved to `to_state` on workflow start |

### Operator Button Matrix (F1)

The GUI **derives** which buttons are enabled in each state from the workflow SM transitions + `operator_events` map. **No hardcoded per-state table in frontend.**

Example: if `idle` has transition `start_requested` and `operator_events.start = start_requested`, the Start button is enabled in `idle`.

---

## R4 — MQTT Topics (Per-Subsystem YAML)

Each subsystem declares its own topic tree:

```yaml
mqtt_topic: "automata/subsystem/{name}"   # {name} substituted with subsystem name
```

This yields:
- `automata/subsystem/robot_first/event`     (orchestrator → subsystem commands)
- `automata/subsystem/robot_first/command`   (legacy/direct commands)
- `automata/subsystem/robot_first/status`    (subsystem → orchestrator state reports)
- `automata/subsystem/robot_first/response`  (subsystem → orchestrator responses, e.g. object list)

**Rules:**
- Subsystem YAML `mqtt_topic` is the routing truth (adapter properties + orchestrator subscriptions)
- Global `mqtt.topic_prefix` is the default; per-subsystem override allowed

---

## R5 — Reporting Roles (Subsystem YAML `metadata.reports`)

Declares what a subsystem publishes on its `/response` topic after specific transitions.

```yaml
metadata:
  reports:
    - type: objects_detected          # message type
      after: { from: capturing, to: ready }  # transition that triggers the report
      object_ids:
        count_source: num_objects     # or "fixed", "detected"
        format: "object_{i:03d}"      # uses global.yaml simulation.object_id_format
```

**Rules:**
- `after.from` → `after.to` must be a valid transition in the subsystem's state machine
- `count_source: num_objects` means the sim process manager passes `--num-objects` to this simulator
- The orchestrator consumes reports generically — **no `if name == "vision"` special cases**

---

## R10 — MQTT Message Formats (`mqtt_formats.yaml`)

Every MQTT payload exchanged with a subsystem is built and parsed through
`backend/config/mqtt_formats.yaml` — the field-mapping layer (`backend/messaging/message_format.py`). A real subsystem that already speaks a fixed payload shape is integrated by editing this file, **no Python changes**:

```yaml
default:
  command:                       # orchestrator → subsystem /event
    event_field: event
    source_field: source
    source_value: orchestrator
    timestamp_field: timestamp
    include_timestamp: true
    static_fields: {}            # extra constant fields (dotted paths allowed)
  hardware_command:              # orchestrator → subsystem /event (pause/resume)
    event_field: event
    source_field: source
    source_value: orchestrator
    include_timestamp: false
  status:                        # subsystem → orchestrator /status
    state_field: state
    ready_field: ready
    paused_field: paused
    name_field: name
  response:                      # subsystem → orchestrator /response
    type_field: type
    objects_field: objects
    count_field: count

subsystems:                      # per-subsystem overrides, deep-merged over default
  some_subsystem:
    command:
      event_field: cmd
    status:
      state_field: data.state   # dotted paths reach nested fields
```

**Rules:**
- One source for both the orchestrator and the simulated subsystems (`backend/simulation/subsystem_sim.py`): the adapter (publisher), the orchestrator receive path, the debug topic descriptors and the sims all read the same format, so changing a field name cannot make them drift apart.
- `command` is used by `SubsystemAdapter.fire_transition` / `send_command`; `hardware_command` by the raw pause/resume broadcast; `status` by `_update_subsystem_status`; `response` by the reporting-role consumption (R5) and debug defaults.
- Dotted field names (`data.state`) read/write nested payloads; missing paths extract as `None`.
- The file may declare an empty/partial `default:` — unspecified fields fall back to the built-in defaults.
- `static_fields` are merged first (formats win over context data).

**Frontend impact:** the debug topic descriptors carry the configured `state_field` / `ready_field` (F5), so the inspector's state quick-pick edits exactly the fields the orchestrator parses.

---

## R11 — Real-Interface Scripts (`metadata.interface`)

A real subsystem cannot initialize until its interface (ROS node, device
daemon, camera service, …) is running. The subsystem YAML declares the
shell commands that start and stop that interface; the orchestrator
executes them **headless** (detached, output appended to
`backend/interface_logs/<name>.log`) because from that moment the
subsystem state is known through MQTT messages only
(`backend/services/interface_manager.py`).

```yaml
# subsystems/<name>.yaml
metadata:
  interface:
    enabled: true
    startup: interfaces/start_xrf.sh     # shell command line (required)
    shutdown: interfaces/stop_xrf.sh     # optional
    cwd: /opt/xrf                        # optional working directory
```

| Hook | When it runs |
|---|---|
| `startup` | **when the workflow starts** (`start_requested` — before the wake transitions are sent). Neither launching the GUI nor toggling a subsystem SIM→REAL powers the hardware. Skipped while a simulated process serves the subsystem, and skipped while the interface is already up (subsystems left `stopped` with a shut-down interface are relaunched here too) |
| `shutdown` | when the workflow **reaches its stop state** (the operator Stop button or the automatic end-of-run stop; fire-and-forget) and at orchestrator shutdown, i.e. when the GUI closes (waited, 5 s timeout) |

A subsystem *reporting* the `stopped` state only marks its interface down
(no script) so the bookkeeping stays honest; the script itself belongs to
the workflow stop and the GUI shutdown.

**Rules:**
- `enabled: false` (or no `interface` section at all) → the orchestrator never touches the interface (backwards compatible).
- The value is a **command line** run through the shell, so a launcher that spawns a daemon and exits (`systemctl start …`, `ssh host …`) is as valid as a long-running process; a still-alive tracked startup process is terminated best-effort after the shutdown script.
- Scripts must be **idempotent**: after an orchestrator restart the startup command runs again.
- Relative paths are resolved against the project root; the startup/shutdown commands are validated (an enabled interface without `startup` is refused).
- Every launch/stop is mirrored to the frontend event log (`interface_command`) and to `backend/interface_logs/<name>.log`.
- Simulated subsystems are never touched: in simulation mode the simulator *is* the subsystem.

**Wizard:** Subsystems → Basics → *Interface (real hardware)* — enable/disable and edit the commands (same comment-preserving patch as every other field).

**Operator flow (simulation mode):** toggle the subsystems that must run on real hardware to REAL (this only switches the routing — nothing is started), then press Start: the orchestrator launches their interfaces headless and waits in `starting` until the hardware reports ready.

---

## R12 — Object Measurement Points (`points` reports + `points_requests`)

During its lifecycle an object accumulates geometry produced by the
subsystems that work on it, and the robot needs it pushed to it at the
right moment. All of it is configuration-declared; the orchestrator only
stores, selects and forwards.

**Producers** declare a `points` section inside a `metadata.reports[]`
entry (R5): when the `after` transition happens, the subsystem publishes
the geometry of one object on its `/response` topic:

```yaml
# vision.yaml — two gripper contact points [x y z] per discovered object
reports:
  - type: objects_detected          # the box-scan object list (existing)
    after: { from: capturing, to: ready }
    object_ids: { count_source: num_objects }
  - type: object_points             # one message PER discovered object
    after: { from: capturing, to: ready }
    object_ids: { count_source: num_objects }
    points: { kind: gripper_contacts, count: 2, dimensions: 3 }

# ai_analysis.yaml — surface points of interest [x y z r p y], one per
# analysis, labeled after the analyzer that will measure it. The number
# of points is whatever the report carries (dynamic, R13b); `labels` is
# the label vocabulary the dispatch routes on. A declared `count` would
# fix the simulated amount.
reports:
  - type: object_points
    after: { from: analyzing, to: ready }
    points:
      kind: points_of_interest
      dimensions: 6
      labels: [xrf, raman]
```

Payload shape (R10 `response` format, `object_id_field` + `points_field`):
`{"type": "object_points", "object_id": "object_001",
  "points": [[x, y, z], ...]}` — a plain coordinate list, or
`{"label": "xrf", "coordinates": [...]}` objects when labeled.

**Storage**: the orchestrator keeps the points per object ID
(`backend/orchestrator/object_points.py`) and shows them in the object
window (Measurement points section, labels included).

**Consumers** declare which stored points each of their commands needs
(`metadata.points_requests` in the consumer's YAML). Right after the
orchestrator publishes such a command, the selected points — in order —
are pushed on the consumer's dedicated topic (`mqtt_topic` + `/points`,
suffix configurable via `metadata.points_topic`):

```yaml
# robot_first.yaml
metadata:
  points_requests:
    - { command: pick,         source: vision,      kind: gripper_contacts }
    - { command: place,        source: vision,      kind: gripper_contacts }
    - { command: return,       source: vision,      kind: gripper_contacts }
    - { command: measure_xrf,  source: ai_analysis, kind: points_of_interest, label: xrf }
    - { command: measure_raman, source: ai_analysis, kind: points_of_interest, label: raman }
```

Push payload (R10 `points_command` format):
`{"event": "pick", "object_id": "object_001", "points": [[...], ...],
  "source": "orchestrator", "timestamp": ...}`.

**Rules:**
- Object-driven commands carry the object ID (`command` format
  `object_id_field`, set only when the command belongs to an object —
  broadcasts like stop/pause do not). Producers use it to reference the
  object in their points reports; consumers use it to know what they work on.
- If a command fires before the points are stored (the first pick of a
  freshly discovered object races the points report), the push is
  **deferred** and delivered as soon as the report arrives — a consumer
  is guaranteed to receive the points of every commanded action.
- `dimensions` is `3` ([x y z]) or `6` ([x y z r p y]). The **number of
  points is whatever the report carries** (R13b): `labels` (when
  present) is the label *vocabulary* the dispatch routes on, not a
  fixed positional sequence, and `count` is optional (a fixed simulated
  amount; omit it and the simulated producer derives a per-object
  count). A request selects a specific point (`label:`), by position
  (`index:`), consumes them one per command (`select: next`), or pushes
  all stored points of the kind in report order (default).
- Validation cross-checks both sides: a request's `command` must be an
  event of the consumer's own state machine, and its `source`/`kind`
  must match a declared points report (labels included).
- Simulated producers generate deterministic placeholder coordinates
  (stable per object ID) with the declared count/dimensions; real
  producers send their actual measurements through the same formats.
- The points store is cleared on workflow reset (the points belong to
  the run's objects).

**Frontend impact:** the object window renders each stored points set
(kind chip, source, per-point labels and coordinates).

---

## R13 — Multi-Point Analysis Dispatch (`points_dispatch` + `select: next`)

A producer may deliver several points of interest per object (e.g. the AI
labels N points `xrf` and M points `raman`); each one is measured in its
own analysis round. The object journey models this with a **dispatch
state** — a state with no actor that the pipeline exits immediately by
firing exactly one of its outgoing events:

```yaml
# object.yaml
puntual_measurements:
  meta:
    position: disk_first
    points_dispatch:
      done_event: puntual_measurements_complete
      remaining:
        - { event: analyze_next_xrf_point,  kind: points_of_interest, label: xrf }
        - { event: analyze_next_raman_point, kind: points_of_interest, label: raman }
  transitions:
    - event: analyze_next_xrf_point
      target: XRF_analysis
      actions: [log_event, {event: robot_first/measure_xrf}, {event: xrf/measure}]
    - event: analyze_next_raman_point
      target: RAMAN_analysis
      actions: [log_event, {event: robot_first/measure_raman}, {event: raman/measure}]
    - event: puntual_measurements_complete
      target: moving_to_box
      actions: [log_event, {event: robot_first/return}]

XRF_analysis:
  meta: { position: disk_first, parallel_actors: [robot_first, xrf] }
  transitions:
    - event: xrf_measurement_complete      # internal label (principle 13)
      target: puntual_measurements
```

**How it works:** when the object enters the dispatch state, the pipeline
fires the FIRST declared event whose label still has unconsumed points in
the object's stored points (R12), and `done_event` when none remain. Each
analysis round **consumes** its point at command time — the consumer's
points request declares `select: next`:

```yaml
# robot_first.yaml
points_requests:
  - { command: measure_xrf, source: ai_analysis, kind: points_of_interest,
      label: xrf, select: next }
```

so every `measure_xrf` command pushes exactly the next unconsumed `xrf`
point (a per-object consumption cursor advances in the points store), the
analysis runs, the parallel actors report ready, the object returns to
the dispatch state, and the cycle repeats until `done_event`.

**Rules:**
- The producer must publish its points report **before** reporting the
  ready state — the dispatch decision reads the stored points at the
  moment the object enters the dispatch state (the generic simulator does
  exactly that; it is the natural real-hardware contract too).
- The dispatch events, the `done_event`, the `label`/`kind` pairs and the
  analysis return transitions are all validated cross-file (the events
  must leave the dispatch state; the labels must be declared by a
  producer's points report).
- `select` is `all` (default — every stored point of the label/kind in
  one push) or `next` (one point per command, consumed in order).
- A points report that replaces a previous one resets that kind's
  consumption cursors; the whole store is cleared on workflow reset.

---

## R6 — Object Journey Final States (Derived, Not Hardcoded)

The object workflow's final state(s) are **states with no outgoing transitions** in `object.yaml`. Code uses `StateMachineConfig.final_states` property.

**Frontend (F4):** "Done" styling follows the object journey's final states from the broadcast config, not a hardcoded color class.

---

## R7 — Simulation: One Generic Simulator

All simulated subsystems use `backend/simulation/subsystem_sim.py` via a one-line entry script:

```
backend/simulation/robot_first_sim.py   → calls subsystem_sim.main("robot_first")
backend/simulation/xrf_sim.py           → calls subsystem_sim.main("xrf")
```

**Discovery:** Orchestrator finds simulators by script presence (`<name>_sim.py`). Subsystem name derived from script name.

**Simulation timings** live in subsystem YAML `metadata`:
```yaml
metadata:
  init_duration: 1.5
  work_durations:
    picking: 2.0
    placing: 2.0
    measuring_xrf: 3.0
    measuring_raman: 3.0
```

Every work state named here must have a transition back to the ready
state ("Completion contract", R2), otherwise the simulator warns and the
workflow waits for it forever.

---

## R8 — Simulation Test Derives Names from YAML

`simulation_test.py` discovers subsystem names from `config/subsystems/*.yaml` and validates invariants against `object.yaml` meta. **No legacy Python constants** — a config rename surfaces as a readable test diff.

---

## R9 — Launcher & Orchestrator Share Defaults

`launch_gui.py` reads argparse defaults from `global.yaml` so launcher and orchestrator can never drift apart.

---

## F1–F8 — Frontend Derivation Rules

| Rule | What Frontend Derives | From |
|------|----------------------|------|
| **F1** | Operator button matrix (which buttons enabled in each state) | `workflow_state_machine` + `metadata.operator_events` |
| **F2** | State machine graphs (subsystem/object/workflow inspectors) | Broadcast `StateMachineDef` objects |
| **F3** | Subsystem visual tones (ready/busy/error/idle/offline/warning) | Backend computes from `global.yaml` vocabulary; frontend renders as-is |
| **F4** | Object "done" styling | `objectFinalStates` from broadcast config (object.yaml final states) |
| **F5** | Debug panel state/ready fields | Topic descriptors (`kind`, `state_field`, `ready_field`) + configured `readyState` |
| **F6** | Object state display order & colors | `objectStateOrder` + `objectStateColors` from `global.yaml` |
| **F7** | Alias input constraint (pattern + max length) | `aliasPattern` from `global.yaml` |
| **F8** | Workflow state display messages | `workflowStateMessages` from `global.yaml` (only error interpolation in frontend) |

---

## Configuration Wizard (debug mode)

The GUI shows a **Configuration wizard** button when launched with
`python launch_gui.py --debug` (the same flag that enables the MQTT
injection panel). It guides the operator through the edits that would
otherwise mean hand-editing YAML, and writes only through the backend
(`backend/services/config_service.py`):

| Step | What it edits |
|---|---|
| ① Workflow | `workflow.yaml` metadata (operator events, readiness gate, error state), `global.yaml` settings, `object.yaml` summary; the structured **state-machine editor** for `workflow.yaml` and `object.yaml` (object states also edit their physical `meta`); raw YAML editor for all three |
| ② Subsystems | existing subsystem files (basics form, structured **state-machine editor**, raw YAML), **create** from a commented template, **stage removal** with a reference check against the other configs |
| ③ MQTT formats | `mqtt_formats.yaml` structured fields per message kind + per-subsystem overrides |
| ④ Review | validate everything and save all modified/new/deleted files in one pass |

Rules the wizard enforces (same as this guide):

- **Debug mode only, workflow not running** — write commands are rejected
  otherwise (the operator could break a live run).
- The raw YAML text is the source of truth; the structured forms are applied
  server-side as **comment-preserving line patches** (`patch_config`), so the
  rest of the file (documentation comments included) is never reformatted.
- Every file is **validated before it is written** (YAML parse + structural
  checks: `initial_state`, transition targets, report `after` transitions,
  message-format kinds). Invalid content never lands on disk.
- Adding a subsystem stages the YAML (and optionally the `<name>_sim.py`
  script); deleting one stops its simulated process and reports which other
  files still reference it.
- The **state-machine editor** (Workflow step for `workflow.yaml` /
  `object.yaml`, Subsystems → State machine for a subsystem) edits states
  (add / remove / description / initial), transitions (event, target) and
  the messages they carry (actions: `log_event`, fired event, raw action)
  as structured data. For object states it also edits the physical `meta`
  (position, actor, parallel actors, rotate request/command, actor op);
  activating it is validated against the configured subsystems
  (unknown actors and actors that cannot complete are refused). `render_state_machine` re-serializes **only the
  ``states:`` block**: the file header (name, metadata and their comments)
  is preserved, and the candidate document is validated as a whole
  (transition targets, state names, `initial_state`, report `after`
  transitions) before anything is returned. Pending drafts are applied and
  saved automatically by the Review step.
- **A restart is required** after saving: state machines, adapters and MQTT
  formats are loaded at startup.

---

## Adding a New Subsystem (Checklist)

1. **Create** `backend/config/subsystems/my_subsystem.yaml` with:
   - `name`, `display_name`, `type`, `mqtt_topic`, `initial_state`, `required`
   - `states:` with transitions (at minimum: `initializing` → `ready` via `init_complete`)
   - `metadata:` with `init_duration`, `work_durations`, optionally `reports`

   *(or use the GUI wizard in debug mode: Subsystems → New subsystem)*

2. **Create** `backend/simulation/my_subsystem_sim.py`:
   ```python
   from backend.simulation.subsystem_sim import main
   if __name__ == "__main__":
       main()
   ```

3. *(Optional)* If the real subsystem speaks a different payload shape, add
   a per-subsystem override under `subsystems:` in `mqtt_formats.yaml` (R10).

4. **Restart** orchestrator — the subsystem card, MQTT topics, and simulation toggle appear automatically.

5. **Update** `object.yaml` if the new subsystem participates in the object journey:
   - Add `actor: my_subsystem` or `parallel_actors: [..., my_subsystem]` to relevant states
   - Add scoped events to transitions (e.g., `event: my_subsystem/do_work`)

6. **Run** `timeout 500 python backend/simulation_test.py` — new config-consistency checks will validate the addition.

---

## Modifying an Existing Subsystem

| Change | Files to Update | Restart Required? |
|--------|-----------------|-------------------|
| Add/rename state | Subsystem YAML | Yes |
| Add/rename transition event | Subsystem YAML + any YAML referencing the event (object.yaml, workflow.yaml) | Yes |
| Change simulation duration | Subsystem YAML `metadata.work_durations` | **No** (hot-reload in sim) |
| Change MQTT topic | Subsystem YAML `mqtt_topic` | Yes |
| Change MQTT payload fields | `mqtt_formats.yaml` (default or per-subsystem override, R10) | Yes |
| Change required flag | Subsystem YAML `required` | Yes |
| Add reporting role | Subsystem YAML `metadata.reports` | Yes |

---

## Modifying the Object Journey

| Change | Files to Update | Restart Required? |
|--------|-----------------|-------------------|
| Add intermediate state | `object.yaml` (states + transitions + meta) | Yes |
| Reorder journey | `object.yaml` transitions | Yes |
| Change actor for a state | `object.yaml` state `meta.actor` (wizard: Workflow → object → State machine → meta) | Yes |
| Add parallel actor | `object.yaml` state `meta.parallel_actors` | Yes |
| Change rotation request/command | `object.yaml` state `meta.rotate_request/command` + `rotating_platform.yaml` | Yes |
| Update display order/colors | `global.yaml` `object_workflow.state_order/colors` | **No** (frontend hot-reload) |
| Add task chain op | `object.yaml` `metadata.task_chains` | Yes |

---

## Modifying the Workflow Lifecycle

| Change | Files to Update | Restart Required? |
|--------|-----------------|-------------------|
| Add workflow state | `workflow.yaml` states | Yes |
| Change operator button mapping | `workflow.yaml` `metadata.operator_events` | Yes |
| Change readiness gate | `workflow.yaml` `metadata.readiness` | Yes |
| Change error state/message | `workflow.yaml` `metadata.error_state` + `global.yaml` `workflow_states` | Yes |
| Change toggle-allowed states | `workflow.yaml` `metadata.toggle_allowed_in` | Yes |

---

## Validation Checklist (Before Commit)

- [ ] `python -c "import yaml; yaml.safe_load(open('backend/config/global.yaml'))"` — all YAMLs parse
- [ ] `timeout 500 python backend/simulation_test.py` — all 151+ tests pass
- [ ] `cd frontend && npm run build` — TypeScript compiles, no type errors
- [ ] If added subsystem: simulator script exists in `backend/simulation/`
- [ ] If changed object journey: `object.yaml` states all have `meta.position`
- [ ] If changed workflow: `operator_events` values match transition events in states

---

## Anti-Patterns to Avoid

| Anti-Pattern | Why It Breaks | Correct Approach |
|--------------|---------------|------------------|
| Hardcoding state names in Python/TypeScript | Config rename breaks code | Derive from broadcast config / YAML |
| `if subsystem_name == "vision":` special cases | New subsystem requires code change | Use `metadata.reports` generic consumption |
| Frontend has its own button enable table | Workflow YAML change doesn't update UI | Derive from `workflow_state_machine` + `metadata` |
| Duplicate vocabulary in multiple files | Drift → subtle bugs | Single source in `global.yaml` |
| Adding final state without removing transition | `final_states` property breaks | Ensure final states have zero outgoing transitions |

---

## Quick Reference: YAML Schema Keys

### global.yaml
```yaml
mqtt: {broker, port, topic_prefix}
frontend: {host, port, dev_port}
states: {non_work[], report_events[], ready, hardware_commands[]}
orchestration: {state_expect_timeout, alias_pattern}
simulation: {default_num_objects, heartbeat_interval, default_init_duration, default_work_duration, object_id_format}
object_workflow: {state_order[], state_colors{}}
workflow_states: {state_name: "message"}
```

### workflow.yaml
```yaml
name, initial_state
metadata: {operator_events{}, stop_completion_event, readiness{in_state, fire}, error_event, error_state, toggle_allowed_in[], wake{from_state, to_state}}
states: {state_name: {description, transitions[{event, target, actions[]}]}}
```

### object.yaml
```yaml
name, initial_state
metadata: {disks, disk_names[], hand_slots, task_chains{subsystem[{op, object_event, next_command?, done_event, releases?}]}}
states: {state_name: {description, meta{position, actor?, parallel_actors[], rotate_request?, rotate_command?, actor_op?, points_dispatch{done_event, remaining[{event, kind, label}]}}, transitions[{event, target, actions[]}]}}
```

### subsystems/*.yaml
```yaml
name, display_name, type, mqtt_topic, initial_state, required
metadata: {description, hardware, init_duration, work_durations{}, reports[{type, after{from, to}, object_ids{count_source, format?}, points{kind, count?, dimensions, labels?}}], interface{enabled, startup, shutdown?, cwd?}, points_topic?, points_requests[{command, source, kind, label?|index?, select?}]}
states: {state_name: {description, transitions[{event, target, actions[]}]}}
```

### mqtt_formats.yaml
```yaml
# kinds: command (object_id_field), hardware_command, status,
# response (object_id_field, points_field), points_command
default: {command{}, hardware_command{}, status{}, response{}, points_command{}}
subsystems: {name: {command?, hardware_command?, status?, response?, points_command?}}
```

---

*This guide is the contract between configuration and code. If a change requires code modification beyond what's described here, the architecture needs review.*