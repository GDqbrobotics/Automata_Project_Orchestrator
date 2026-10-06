# Config Change Guide — eliminating hardcoded backend & frontend behaviour

Status: **analysis / change proposal** (2026-10-06). No code changes included;
this document inventories every piece of behaviour in the **Python backend**
(§1–§5) and the **TypeScript frontend** (§6–§9) that is hardcoded instead of
derived from `backend/config/*.yaml`, maps the coupling ("if I change this
YAML, what else breaks"), and specifies the changes needed so that the YAML
files become the *single source of truth*.

After the backend changes R1–R9 (§5) and the frontend changes F1–F8 (§9) are
done, "change a subsystem / object / workflow config" should require **no
backend code change at all** (only a new one-line
`backend/simulation/<name>_sim.py` script for a brand-new subsystem, which is
already a config-exercise) and **no frontend logic change** (only, possibly,
a new CSS color class for a brand-new color value).

```
Part I  — Backend      §1–§5
Part II — Frontend     §6–§9
Part III — Result      §10 (the new change guide), §11 (verification)
```

---

## 1. How configuration is consumed today

| Config file | Consumed by | Respected? |
|---|---|---|
| `config/global.yaml` | `domain/models.py` (`GlobalConfig.from_yaml`), `orchestrator/main.py` | ✅ fully respected (broker, ports, `default_num_objects`, `state_order`, `state_colors`, `workflow_states` messages) |
| `config/workflow.yaml` | `state/state_machine.py` (engine), `orchestrator/workflow.py` | ⚠️ transitions respected; **state/event *names* are duplicated in code** |
| `config/object.yaml` | `state/state_machine.py` (engine), `orchestrator/object_state_machines.py` | ⚠️ transitions respected; **state/event names and the whole dispatch logic are duplicated in `orchestrator/pipeline.py`** |
| `config/subsystems/<name>.yaml` | `orchestrator/workflow.py` (`_load_new_format`), `adapters/subsystem.py` (factory), `simulation/subsystem_sim.py` | ⚠️ state machine respected; **`mqtt_topic` field is ignored by the command path**, and any subsystem that takes part in the object flow is special-cased in `pipeline.py` by name |

The state-machine **engine** (`backend/state/state_machine.py`) is correctly
generic — it is the *consumers* (orchestrator, pipeline, adapters, simulation)
that carry the hardcoded knowledge.

---

## 2. Inventory of hardcoded backend behaviour

Line numbers refer to the tree at 2026-10-06.

### 2.1 `backend/orchestrator/pipeline.py` — the biggest offender

The entire object-flow knowledge of the machine lives here as Python
constants and name comparisons instead of being read from
`object.yaml` / `subsystems/*.yaml`:

- **Subsystem names** (lines 53–59): `VISION`, `ROBOT_FIRST`, `ROBOT_SECOND`
  (unused), `ROTATING_PLATFORM`, `IMAGING_3DI`, `IMAGING_HSI`, `AI_ANALYSIS`.
- **Parallel-work set** (line 64):
  `MEASUREMENT_SUBSYSTEMS = ("xrf", "raman", "robot_first")` — which
  subsystems must all complete for the measurement stage to end.
- **Object state names** (lines 67–75): `OBJ_WAITING`, `OBJ_GOING_SECOND`,
  `OBJ_3DI`, `OBJ_HSI`, `OBJ_GOING_FIRST`, plus three *unused* dead constants
  (`OBJ_AI_PROCESSING`, `OBJ_XRF_RAMAN`, `OBJ_MOVING_TO_BOX`).
- **Subsystem work-state names** (scattered): `"ready"` (lines 198, 401, 422,
  427, 449 — the "completion report" state), `"picking"` (228), `"placing"`
  (232), `"returning"` (236), `"capturing"` (247, 256, 340, 348),
  `"rotating"` (288), `"analyzing"` (268).
- **Non-work state set** (line 77):
  `NON_WORK_STATES = {"initializing", "ready", "stopped", "error"}` — the
  third copy of this set (see §2.3 and §2.6).
- **Object events fired by the pipeline** (must match `object.yaml`
  transition events exactly): `"queued_for_analysis"` (170),
  `"object_picked"` (459), `"object_placed_on_disk"` (237),
  `"platform_rotated_to_second"` (338), `"platform_rotated_to_first"` (361),
  `"3di_capture_complete"` (253, 345), `"hsi_capture_complete"` (264, 352),
  `"ai_complete"` (278, 372), `"measurements_complete"` (301),
  `"object_returned_to_box"` (242).
- **Subsystem commands issued by the pipeline** (must match subsystem YAML
  events): `"capture"` for vision (403), `"pick"`/`"place"` via `_command`
  (229, 232), `"return"` (via `_complete_measurements`),
  `"rotate_to_second"`/`"rotate_to_first"` (437).
- **Workflow state names** (lines 138–143): `"running"` starts dispatch,
  `("stopping", "stopped", "error", "idle")` suspend it; `_finish_workflow`
  (483–484) fires `"stop_requested"` then `"stopped"`.
- **Internal coordination event names** (line 190):
  `"rotation_requested_second"`, `"rotation_requested_first"` — the
  `workflow/`-scoped events fired by `object.yaml` transitions.
- **The machine's physical layout**: 2 disk slots (`_disk_first`,
  `_disk_second`), 1 end effector (`_robot_task`), coupled rotation
  (`_disks_stable`, lines 378–385), the "vision box scan" phase
  (`"scanning"`, line 401), and the pick→place / measure→return *chains* of
  robot 1 (`_on_subsystem_completed`, lines 224–245). None of this is in any
  YAML file.
- **Phase names** `"idle" / "scanning" / "pipeline" / "suspended"` (93, 139,
  144, 171, 180, 473, 479) — internal, but hardcoded strings.

### 2.2 `backend/orchestrator/workflow.py`

- **Workflow state names via enum** — `WorkflowState`
  (`backend/domain/models.py:9-16`) duplicates `workflow.yaml`
  `states:`. `WorkflowState(self.state_machine.state)` (lines 444, 467, 475,
  489, 505) raises `ValueError` if a state is renamed in the YAML.
- **Workflow events fired by operator handlers** (must match
  `workflow.yaml` events): `"start_requested"` (442, 883),
  `"pause_requested"` (466), `"resume_requested"` (474),
  `"stop_requested"` (482), `"reset_requested"` (501),
  `"subsystem_ready"` (460, 890, 1127, 1431), `"stopped"` (488).
  (`"subsystem_error"` exists in `workflow.yaml` but is **never fired by the
  backend** — only by `simulation_test.py:790`; a subsystem reporting
  `error` never moves the workflow to `error`.)
- **`_handle_start` (433–461)**: hardcodes the subsystem states
  `"stopped"` (452) and the `"ready"` event (453) to wake stopped
  subsystems, plus the *readiness gate* "all `required` adapters report
  `READY` → fire `subsystem_ready`".
- **`_handle_stop` (478–489)**: hardcodes the completion of the stop
  sequence — when the workflow is in `stopping`, fire `"stopped"`
  immediately.
- **`_handle_reset` (491–538)**: hardcodes the auto-start-after-reset rule
  and its interaction with simulation mode.
- **`_handle_toggle_subsystem_simulation` (655–…)**: hardcodes the allowed
  workflow states `(IDLE, STOPPED, ERROR)` (679).
- **`_on_event` routing (256–305)**: the special scopes `"workflow"` and
  `"all"` are hardcoded, and inside `all/` the events `"pause"` / `"resume"`
  (285) are hardcoded as *raw hardware commands* (bypass the local SMs and
  go straight to MQTT), while any other `all/<event>` is an event broadcast.
- **Alias validation (541–563)**: `ALIAS_PATTERN =
  re.compile(r"^[A-Za-z0-9_\-\. ]{1,64}$")` — the format rule for operator
  alias IDs is a code constant.
- **`create_object` (904–925)**: hardcodes the object *initial* state name
  `"identified_in_box"` and its description in the `OBJECT_ADDED` payload
  (914–915) instead of reading `object.yaml` `initial_state` + description.
- **`fire_object_transition` / `_count_analyzed_objects` (933–999)**:
  hardcode the object *final* state `"object_analyzed"` (967, 991) — this is
  what drives progress and the `PROGRESS_UPDATED` event.
- **`_broadcast_objects` (1346)**: default object state
  `"identified_in_box"`.
- **`_on_mqtt_message` (1009–1070)**: hardcodes the response type
  `"objects_detected"` (1048) — the only `/response` message the
  orchestrator acts on — and the topic prefix `"automata/subsystem/"` for
  routing (1021, 1056).
- **`_update_subsystem_status` (1075–1132)**: the readiness gate is coded a
  *second* time (1120–1131, on every status report while `STARTING`);
  `update_all_subsystems_ready` (1420–1436) a *third*.
- **`_map_reported_state` (1135–1149)**: hardcodes the reported state names
  `"stopped"`, `"offline"`, `"error"`.
- **`notify_subsystem_ready` (1396–1417)**: fallback state `"ready"` (1406).
- **`_build_debug_topics` (740–800)**: hardcodes the subsystem name
  `"vision"` (784) for the default `objects_detected` debug payload, the
  object ID format `object_{i:03d}` (786), and the `"ready"` boolean
  `current == "ready"` (766).
- **`_load_old_format` / `_load_new_format` (1472–1535)**: default
  `initial_state` `"ready"` (1490, 1517) and default `mqtt_topic`
  `"automata/subsystem/{name}"` (1487, 1515).

### 2.3 `backend/adapters/subsystem.py`

- **`REPORT_EVENTS = frozenset({"complete", "init_complete"})`** (line 34):
  the events that must *not* be published as MQTT commands. Renaming
  `complete`/`init_complete` in any subsystem YAML breaks the
  command/report separation.
- **`NON_WORK_STATES = frozenset({"initializing", "ready", "stopped",
  "error"})`** (line 37): second copy of the set.
- **`STATE_EXPECT_TIMEOUT = 10.0`** (line 43): the discrepancy-check window
  from STATE_MACHINE.md principle 12 — a code constant, not in
  `global.yaml`.
- **Hardcoded MQTT topic prefix** — `fire_transition` (line 181) publishes to
  `f"automata/subsystem/{self.name}/event"`, `send_command` (326) to
  `.../command`, `info` (106) builds `topic_prefix` the same way. The
  per-subsystem **`mqtt_topic` field in the YAML is loaded into
  `SubsystemConfig` and used only for *subscribing* status/response in
  `workflow.py:start()` (1550–1553)** — publishing ignores it. Editing
  `mqtt_topic` in a YAML therefore produces a split brain: the orchestrator
  subscribes to the new topic but keeps publishing/recognising
  `automata/subsystem/...` (`_on_mqtt_message` routes on the hardcoded
  prefix).
- **`ALL_SUBSYSTEMS` list + name aliases (411–432)**: `vision`,
  `robot_first`, … a hardcoded registry kept only so
  `simulation_test.py` can import it (the README already flags this as an
  add-manually step for new subsystems).
- **`SubsystemState` enum** (`domain/models.py:19-24`) and the dataclass
  defaults `initial_state="ready"` (line 67), `SubsystemInfo.current_state=
  "ready"` (`domain/models.py:186`), `ObjectInfo.state=
  "identified_in_box"` (`domain/models.py:229`).

### 2.4 `backend/domain/models.py`

- **`WorkflowState` enum (9–16)**: mirrors `workflow.yaml` — must be edited
  together with the YAML.
- **`ObjectState` enum (27–43)**: mirrors `object.yaml` — **defined but never
  used** by backend code (only imported by `simulation_test.py:61`).
- **`SubsystemState` enum (19–24)**: adapter-internal, but its members are
  state names that duplicate the subsystem YAMLs.
- **`SubsystemConfig.mqtt_topic` default (106)**, `initial_state` default
  (106).
- **`GlobalConfig.from_yaml` (117–152)**: respects `global.yaml` but there is
  **no config entry** for the constants listed in §2.2/§2.3 (timeout, alias
  pattern, non-work states, report events, topic prefix, sim defaults).

### 2.5 `backend/messaging/mqtt_client.py`

- **Topic layout constants (13–16)**: `TOPIC_SUBSYSTEM =
  "automata/subsystem"`, `TOPIC_COMMAND/STATUS/RESPONSE`. The topic tree is
  a code constant, shared by orchestrator, adapters, sim and debug panel —
  and inconsistent with the (ignored) per-YAML `mqtt_topic`.

### 2.6 `backend/simulation/`

- **`subsystem_sim.py`**
  - `NON_WORK_STATES` (line 53): **third copy** of the set.
  - `DEFAULT_INIT_DURATION = 1.5`, `DEFAULT_WORK_DURATION = 2.0`,
    `HEARTBEAT_INTERVAL = 2.0` (55–57): sim timing defaults as code
    constants (per-state durations *are* config-driven via
    `metadata.work_durations` — good).
  - Self-initialisation (115): schedules the event **`"init_complete"`**
    after `init_duration` — the event name is hardcoded; the sim assumes
    every subsystem boots in a state that has an `init_complete` transition.
  - **Pause/resume conventions** (`_on_event_message`, 153–173): the command
    strings `"pause"` / `"resume"` are hardcoded (they are sent by
    `workflow.py:_on_event` as raw `all/` commands).
  - **Vision special case** (`_after_state_change`, 197–215): `if
    self.name == "vision" and prev_state == "capturing" and new_state ==
    "ready"` → publish `{"type": "objects_detected", "objects":
    [f"object_{i:03d}", …]}`. Hardcoded subsystem name, work-state name,
    completion-state name, response `type`, and object-ID format.
  - Status payload (286–297): the `ready` field is computed as
    `current_state == "ready"` — hardcoded state name.
- **`process_manager.py`**
  - **`if name == "vision"` (115)**: only "vision" is launched with
    `--num-objects`; renaming the vision subsystem means the sim reports 0
    objects and the run cannot start.
  - `SIM_SCRIPT = "{name}_sim.py"` (30): discovery convention (fine, and
    already documented in the README).
- **`backend/simulation/<name>_sim.py`** (one-liners): each repeats the
  subsystem name (`main("vision", …)`, …) — required for the convention, but
  it is a duplicated name that must track YAML renames.

### 2.7 `backend/simulation_test.py` (test suite)

- Imports the legacy name constants from `adapters/subsystem.py` (50–59).
- Hardcodes subsystem names in assertions (`"vision"`, `"robot_first"`,
  `"imaging_3di"`, `"imaging_hsi"`, `"cloud_database" not in names`,
  `"ai_analysis"` required-flag checks — 880–916), workflow terminal states
  `{"stopped", "error"}` (487, 544), event names (397, 403, 646, 790),
  object state names and object events (932–1014, 1215).

  The suite is *supposed* to encode the expected behaviour, so some
  hardcoding is legitimate — but it should read the **names** from the same
  YAML files it validates (glob `config/subsystems/*.yaml`, read
  `initial_state`/`states`), not from legacy Python constants.

### 2.8 `backend/orchestrator/main.py`, `launch_gui.py`

- Mostly fine: paths/ports have CLI overrides and `global.yaml` defaults.
- `launch_gui.py`: the frontend port `3000` is hardcoded in the port
  conflict check (434); the `--num-objects` default `3` (465) duplicates
  `global.yaml`'s `simulation.default_num_objects` instead of reading it.

### 2.9 Frontend

The frontend (TypeScript, Vite, no framework) is analysed in **Part II**
(§6–§9): what is already config-driven (§6), the inventory of hardcoded
behaviour (§7), the coupling map (§8) and the fixes F1–F8 (§9).

TL;DR: the cards, inspector graphs and message feeds are fully data-driven
from the WebSocket status broadcasts, but the **operator button enable/disable
matrix** (`config/button-rules.ts`), the **workflow-state name union**
(`contracts.ts`), the **subsystem state→tone map** (`ui/app.ts`), the
**"analyzed"/"gray" color-class checks**, the **`'ready'`/`'workflow'` /
`'/status'` convention literals** and the **alias field constraints** are all
hardcoded in TypeScript and must track the YAMLs by hand.

---

## 3. The coupling map — "if I change this config, what else must I touch?"

This is the maintenance burden today. ✅ = works with YAML-only change;
⚠️ = code must be edited in the listed places; ✗ = behaviour silently
breaks (stall / wrong data) if the code is not edited.

### 3.1 `config/subsystems/<name>.yaml`

| Change | Result if code is NOT updated | Code locations to update |
|---|---|---|
| **Add a new subsystem file** | Works for card/topics/SIM-toggle only if a `backend/simulation/<name>_sim.py` exists; the subsystem is inert for the object flow | ✅ mostly (discovery is automatic). ⚠️ `adapters/subsystem.py:411` `ALL_SUBSYSTEMS` (tests); ⚠️ `pipeline.py` if it takes part in the flow (name constant + completion handling + parallel set); ⚠️ new sim script; ⚠️ `global.yaml` only if it needs display config |
| **Rename `name:`** | Pipeline never finds the subsystem (commands rejected, stage stalls); sim script no longer matches the discovery convention; debug panel / vision special case dead | `pipeline.py:53-64`; `process_manager.py:115` (if it's vision); `subsystem_sim.py:202` (if vision); `workflow.py:784` (if vision); all `object.yaml`/`workflow.yaml` scoped events referencing it; the sim script filename + its `main("…")`; `ALL_SUBSYSTEMS`; tests |
| **Rename a state** (e.g. `capturing` → `scanning`) | Completion detection in the pipeline fails → **pipeline stalls**; sim `work_durations` keys no longer match → sim work never completes; non-work classification wrong → readiness/discrepancy logic wrong | `pipeline.py` (`was_state ==` comparisons 228/232/236/247/256/268/288 and `_sm_state(…)` comparisons 340/348); `metadata.work_durations` keys in the same YAML; `NON_WORK_STATES` in `adapters/subsystem.py:37`, `pipeline.py:77`, `subsystem_sim.py:53` if the state is non-work; `subsystem_sim.py:202-204` (vision `capturing`→`ready` hook); `workflow.py:766` (`ready` field) |
| **Rename an event** (e.g. `pick`, `capture`, `rotate_to_second`) | Commands published by the pipeline/object-SM are rejected by the subsystem SM → stall; or completion events stop being mirrored | every other YAML that references `name/event` (scoped events in `object.yaml`, e.g. `robot_first/pick`, `imaging_3di/capture`, `xrf/measure`, `raman/measure`, `ai_analysis/plan`, `robot_second/scan_3d`); `pipeline.py` (`_command(…)` calls 229/403/437); `REPORT_EVENTS` in `adapters/subsystem.py:34` (if `complete`/`init_complete`); `subsystem_sim.py:115` (if `init_complete`); `workflow.py:453` (if `ready`) |
| **Rename the *final/ready* state `ready`** | Readiness gate, completion detection, discrepancy checks, status `ready` field — all break | everything in the two rows above, plus `workflow.py` (452-453, 766, 1120-1131, 1396-1417, 1420-1436), `adapters/subsystem.py` (`SubsystemState.READY`), `pipeline.py:198,401,422,427,449`, `subsystem_sim.py:202-204,291` |
| **Change `initial_state`** | Sim self-init assumes an `init_complete` transition exists from the initial state; `_handle_start`'s wake-up logic assumes `stopped`; toggle-reset assumes the SM resets to "initial" | `subsystem_sim.py:115` (event name), the YAML itself must define the boot transition; `workflow.py:452` |
| **Change `required: true/false`** | Works | ✅ (read from config: `adapter_factory.get_required()`) — the readiness *logic* is coded but reads the flag |
| **Change `mqtt_topic`** | Split brain: status/response *subscribed* on the new topic, but commands published and status *routed* on `automata/subsystem/…` → orchestrator never sees the subsystem | `adapters/subsystem.py:106,181,326`; `messaging/mqtt_client.py:13`; `workflow.py:1021,1056,1550-1553`; `subsystem_sim.py:97-99`; `process_manager.py` (topics are not used there) |
| **Change `metadata.work_durations` / `init_duration`** | Works (sim reads them); a work state without a duration never completes in the sim (`DEFAULT_WORK_DURATION` fallback is not even applied — see `subsystem_sim.py:183`) | ✅ |
| **Delete a subsystem file** | Works *unless* it is one of the flow subsystems | `pipeline.py` constants; `ALL_SUBSYSTEMS`; tests; sim script |

### 3.2 `config/object.yaml` (the object journey blueprint)

| Change | Result if code is NOT updated | Code locations to update |
|---|---|---|
| **Rename a state** | Pipeline gates (`_obj_state(…) == OBJ_*`) stop matching → objects never dispatched / disks never released → **full stall**; progress counting and `OBJECT_ADDED` payload show the wrong names | `pipeline.py:67-75` (`OBJ_*`); `workflow.py:914,967,991,1346` (initial + final state); `domain/models.py:27-43` (`ObjectState`, unused but exported); `global.yaml` `object_workflow.state_order/state_colors`; `frontend/src/config/object-styles.ts`; `frontend/src/styles/app.css` color classes |
| **Rename an event** | Pipeline fires events that no longer exist → `StateMachineError` logged, **stall**; scoped events in the transition actions stop routing to the right subsystem | `pipeline.py` (all `_fire_object(…)` calls: 170, 237, 242, 253, 264, 278, 301, 338, 345, 352, 361, 372, 459); `on_object_workflow_event` names (190) for the `workflow/rotation_requested_*` events; the scoped actions inside the YAML reference `subsystem/event`, so those must still exist in the subsystem YAMLs |
| **Change which subsystem acts at a stage** (e.g. a new analyzer in the measurement stage, or swap 3DI/HSI order) | The completion bookkeeping in `_on_subsystem_completed` / `_start_measurement_tracking` no longer matches reality → measurements never "complete" or complete too early | `pipeline.py:64` (`MEASUREMENT_SUBSYSTEMS`), 247–290 (per-subsystem completion branches), 305–322; the stage's transition actions in `object.yaml`; the new subsystem's YAML (`work_durations`); a sim script |
| **Add a new stage / reorder stages** | Same as above, plus the physical bookkeeping (`_disk_first`/`_disk_second`, `_imaging_object`, `_ai_object`, `_measuring_object`) has no config representation at all | `pipeline.py` (a structural change of `_finish_rotation`, `_advance`, `_on_subsystem_completed`) |
| **Change the machine geometry** (e.g. 3 disks, a second end effector) | Impossible without a rewrite — the capacity model is code | `pipeline.py` (capacity constants, `_disks_stable`) |
| **Change `initial_state`** | `create_object`'s `OBJECT_ADDED` payload and `_broadcast_objects` default stay `"identified_in_box"` | `workflow.py:914,1346`; `domain/models.py:229` |

### 3.3 `config/workflow.yaml` (the run lifecycle)

| Change | Result if code is NOT updated | Code locations to update |
|---|---|---|
| **Rename a state** (e.g. `starting` → `booting`) | `WorkflowState(self.state_machine.state)` raises `ValueError` → **every operator command crashes**; pipeline never sees `running` | `domain/models.py:9-16` (`WorkflowState`); `pipeline.py:138-143`; `workflow.py` (`WorkflowState.*` usages 170, 444, 467, 475, 487-489, 505, 679, 1120, 1128, 1424, 1431); `global.yaml` `workflow_states` messages; `frontend/src/styles/app.css` badge classes; tests (487, 544, 641, 1534) |
| **Rename an operator event** (e.g. `start_requested`) | Operator button fires an unknown event → `StateMachineError`, workflow never moves | `workflow.py` (442, 466, 474, 482, 501); tests (397, 403, 646, 790) |
| **Rename `subsystem_ready`** | Readiness gate never fires the transition → workflow stuck in `starting` | `workflow.py:460,890,1127,1431` |
| **Rename the `stopped` *event*** (stopping→stopped) | Operator stop and end-of-run never finish the sequence → stuck in `stopping` | `workflow.py:488`; `pipeline.py:484` |
| **Change the `all/pause`, `all/resume`, `all/stop` actions** | The `all/` scope handling in `_on_event` and the raw-command special case (`pause`/`resume`) in code no longer match | `workflow.py:283-292`; `subsystem_sim.py:158-163`; subsystem YAMLs (they don't model pause — by design) |
| **Change the readiness condition** ("all *required* ready" is the only condition) | Impossible in YAML today — the condition is coded | `workflow.py:455-460, 1120-1131, 1424-1436` |
| **Remove the `subsystem_error` transition** | No code effect (nothing fires it) — but note the workflow *cannot* enter `error` at runtime today; only tests exercise it | — (gap to close: a subsystem reporting `error` should be able to drive the workflow SM via a configured event) |

### 3.4 `config/global.yaml`

| Change | Result | Notes |
|---|---|---|
| `mqtt.broker/port`, `frontend.host/port`, `simulation.default_num_objects` | ✅ works (CLI can override) | `launch_gui.py:465` hardcodes default 3 instead of reading it |
| `object_workflow.state_order/state_colors` | ✅ works (sent to the frontend) | must track `object.yaml` state names; `frontend/src/config/object-styles.ts` mirrors them as fallback |
| `workflow_states` messages | ✅ works | keys must track `workflow.yaml` state names |
| **(missing)** `STATE_EXPECT_TIMEOUT`, alias pattern, non-work states, report events, topic prefix, sim heartbeat/duration defaults, object-ID format | not configurable | see §5 |

---

## 4. Why it happens (root cause)

The engine is generic, but four *roles* were never made declarative, so the
code had to learn them by name:

1. **Who does what at which stage, and who runs in parallel** — the object
   YAML declares *some* commands via scoped event actions, but the pipeline
   still hardcodes the actor→stage mapping, the parallel set, the robot
   task chains, and the disk-slot model.
2. **Which state means "not working"** (`NON_WORK_STATES`, duplicated 3×),
   which event means "work done" (`complete`/`init_complete`), which state
   means "idle and ready" (`ready`), which state means "object finished"
   (`object_analyzed`), which states allow a sim toggle (`idle/stopped/
   error`).
3. **The MQTT topic tree** (`automata/subsystem/<name>/{event,status,
   response}`) — per-YAML `mqtt_topic` exists but is only half used.
4. **Subsystem *roles*** (e.g. "the subsystem that reports the object list")
   — expressed as `if name == "vision"` in three places.

---

## 5. The changes needed (target design)

Ordered so that each step is independently shippable and keeps
`timeout 500 python backend/simulation_test.py` green.

### R1 — One vocabulary: move shared state-name constants to `global.yaml`

Add to `global.yaml`:

```yaml
states:
  non_work: [initializing, ready, stopped, error]   # single source (3 copies today)
  report_events: [complete, init_complete]          # events that never publish MQTT
  ready: ready                                      # "idle, can accept work" state
  hardware_commands: [pause, resume]                # all/-scoped raw commands
mqtt:
  broker: localhost
  port: 1883
  topic_prefix: automata/subsystem                  # was TOPIC_SUBSYSTEM
orchestration:
  state_expect_timeout: 10                          # was STATE_EXPECT_TIMEOUT
  alias_pattern: '^[A-Za-z0-9_\-\. ]{1,64}$'        # was ALIAS_PATTERN
simulation:
  default_num_objects: 3
  heartbeat_interval: 2.0
  default_init_duration: 1.5
  default_work_duration: 2.0
  object_id_format: 'object_{i:03d}'                # sim + debug panel
```

Code changes:

- `domain/models.py`: extend `GlobalConfig` with the new fields; pass it (or
  the parsed sections) to `WorkflowOrchestrator`, `PipelineController`,
  `SubsystemAdapter` and `SubsystemSimulator` instead of module constants.
- `adapters/subsystem.py`: `REPORT_EVENTS`, `NON_WORK_STATES`,
  `STATE_EXPECT_TIMEOUT` become config values (defaults kept for
  backwards compatibility).
- `orchestrator/pipeline.py`: `NON_WORK_STATES`, the `"ready"` comparisons,
  and the `workflow/` special-case event list in
  `workflow.py:_on_event` (283-292) come from config.
- `messaging/mqtt_client.py`: `TOPIC_SUBSYSTEM` becomes
  `GlobalConfig.mqtt.topic_prefix` (keep the constant as a default).
- `orchestrator/workflow.py`: `ALIAS_PATTERN` from config.
- `simulation/subsystem_sim.py`: `NON_WORK_STATES`,
  `DEFAULT_INIT_DURATION`, `DEFAULT_WORK_DURATION`, `HEARTBEAT_INTERVAL`,
  pause/resume names from config; also fix `subsystem_sim.py:183` so a work
  state *without* a configured duration falls back to
  `default_work_duration` (today it simply never completes).

**Result:** renaming `ready`/`capturing`-class conventions, the topic tree,
the timeout or the alias rule is a YAML-only change.

### R2 — The pipeline becomes data-driven from `object.yaml` stage metadata

Extend `object.yaml` with per-state metadata describing the *physical* facts
the pipeline needs (the states/transitions/events stay exactly as they are —
this only adds facts):

```yaml
metadata:
  # physical layout (was: hardcoded fields in pipeline.py)
  disks: 2                      # rotating platform disk slots
  hand_slots: 1                 # robot_first end effectors in flight

states:
  identified_in_box: { … }
  waiting_for_disk:
    meta: { position: box }
  moving_to_disk:
    meta: { position: hand }
  object_going_second_position:
    meta: { position: disk_first, rotate_request: rotation_requested_second }
  3DI_analysis:
    meta: { position: disk_second, actor: imaging_3di }
  HSI_analysis:
    meta: { position: disk_second, actor: imaging_hsi }
  object_going_first_position:
    meta: { position: disk_second, rotate_request: rotation_requested_first }
  ai_processing:
    meta: { position: disk_first, actor: ai_analysis }
  XRF_RAMAN_analysis:
    meta: { position: disk_first, parallel_actors: [robot_first, xrf, raman] }
  moving_to_box:
    meta: { position: disk_first, actor: robot_first, actor_op: return }
  object_analyzed: { … }
```

Plus a small declarative description of robot 1's pick→place and
measure→return chains (the only place where one subsystem performs two
consecutive commands per object):

```yaml
metadata:
  task_chains:
    robot_first:
      - { op: pick,   object_event: object_picked,
          next_command: place, done_event: object_placed_on_disk }
      - { op: return, done_event: object_returned_to_box, releases: disk_first }
```

Code changes in `orchestrator/pipeline.py`:

- Delete all `OBJ_*` constants and the `VISION/ROBOT_*/IMAGING_*/AI_*`
  name constants and `MEASUREMENT_SUBSYSTEMS`.
- At construction, build a lookup from the object blueprint:
  - `state → meta` (position, actor / parallel_actors, rotate_request),
  - `actor → object_event`: for each state `S` with `actor: X`, the object
    event that leaves `S` (e.g. `3DI_analysis --3di_capture_complete-->
    HSI_analysis`) is the event to fire when `X` reports completion while an
    object is in `S`. This makes the `was_state == "capturing"` branches
    unnecessary — completion is detected generically as
    "X reported the `ready` state after a work state" (R1's vocabulary).
  - the `task_chains` table drives `_on_subsystem_completed` for robot 1.
- `_advance`/`_finish_rotation`/`_disks_stable` keep the *physical* rules
  (coupled rotation, capacity) but read `disks`/`hand_slots` from metadata
  instead of assuming 2/1.
- `on_object_workflow_event` consumes `meta.rotate_request` names instead of
  the two hardcoded strings; unknown `workflow/` events stay logged no-ops.

**Result:** renaming object states/events, swapping 3DI↔HSI, adding an
analyzer to the measurement stage, or adding a stage = edit `object.yaml`
(plus the new subsystem's YAML + sim script). `pipeline.py` is not touched.

### R3 — Workflow lifecycle: operator events and readiness declared in `workflow.yaml`

Add a `metadata` section:

```yaml
metadata:
  operator_events:
    start: start_requested
    pause: pause_requested
    resume: resume_requested
    stop: stop_requested
    reset: reset_requested
  stop_completion_event: stopped          # fired when leaving `stopping`
  readiness:
    in_state: starting                    # workflow state to wait in
    fire: subsystem_ready                 # event when all required are ready
  toggle_allowed_in: [idle, stopped, error]
```

Code changes in `orchestrator/workflow.py`:

- **Delete the `WorkflowState` enum** (`domain/models.py:9-16`); the
  orchestrator keeps the raw state string from the engine and compares it
  against YAML-derived values (`WorkflowNames` loaded once from
  `workflow.yaml`: initial state, operator-event map, readiness, toggle
  list). `WorkflowStatus.state` becomes `str`.
- `_handle_start/_pause/_resume/_stop/_reset` fire the configured event
  names; `_handle_stop`'s stopping-completion and the three readiness-gate
  copies (455-460, 1120-1131, 1424-1436) collapse into one helper driven by
  the `readiness` config.
- `_on_event`: `hardware_commands` (R1) replaces the hardcoded
  `("pause", "resume")` special case.
- Close the `subsystem_error` gap: when a *required* subsystem reports the
  `error` state, fire the configured `subsystem_error` event on the
  workflow SM (the transition already exists in `workflow.yaml`).

**Result:** renaming workflow states/events is a YAML-only change for the
backend. The frontend's remaining copies of the workflow states (the button
matrix, the state-name union, the CSS badge classes) are removed by F1/F2/F7
(§9) — without F1, a renamed or new workflow state would leave the operator
with **all buttons disabled** in that state.

### R4 — Subsystem adapters honour the per-YAML `mqtt_topic`

- `adapters/subsystem.py`: `fire_transition` (181), `send_command` (326) and
  `info.topic_prefix` (106) build topics from `cfg.mqtt_topic`
  (`{name}` substitution), not from the literal `automata/subsystem`.
- `orchestrator/workflow.py:_on_mqtt_message`: route by the *configured*
  topic set of each adapter (status/response suffixes), not by
  `"automata/subsystem/" in topic`.
- `simulation/subsystem_sim.py:97-99` and `_build_debug_topics`
  (`workflow.py:740-800`) derive their topics the same way.

**Result:** `mqtt_topic` in a subsystem YAML becomes real; moving the topic
tree (e.g. to `automata2/…`) is a config change.

### R5 — Subsystem *roles* replace `if name == "vision"`

Declare capabilities in the subsystem YAML metadata instead of
name-comparisons:

```yaml
# config/subsystems/vision.yaml
metadata:
  reports:
    - type: objects_detected        # published on /response after capture
      after: { from: capturing, to: ready }
      object_ids: { format: 'object_{i:03d}', count_source: num_objects }
```

Code changes:

- `simulation/subsystem_sim.py:_after_state_change`: generic — for each
  `metadata.reports` entry, publish when the `from → to` transition happens
  (object IDs rendered from `object_id_format` + `num_objects`).
- `orchestrator/workflow.py:_build_debug_topics`: default response payload
  from `metadata.reports` instead of `if name == "vision"`.
- `simulation/process_manager.py:115`: pass `--num-objects` to every sim
  whose YAML has a report with `count_source: num_objects`.
- `orchestrator/workflow.py:_on_mqtt_message`: the `type` to treat as
  "objects detected" comes from the vision subsystem's config (or: any
  `/response` with `type: objects_detected` from a subsystem that declares
  it).

**Result:** renaming vision (or having two box-scanning cameras) needs no
Python edits beyond the sim script.

### R6 — Object initial/final states derived from `object.yaml`

- Initial: `ObjectStateMachines.config.initial_state` + its description
  replace the hardcoded payload in `workflow.py:create_object` (914-915) and
  the `_broadcast_objects` default (1346) and `ObjectInfo` default
  (`domain/models.py:229`).
- Final: a state is *final* when it has **no outgoing transitions** (that is
  already true for `object_analyzed`); the engine can expose
  `config.final_states`. `_count_analyzed_objects` /
  `fire_object_transition` (967, 991) use it.

**Result:** renaming or reworking the object journey's endpoints is
YAML-only.

### R7 — Delete the dead/duplicated model code

- Remove `ObjectState` (`domain/models.py:27-43`) — unused.
- Remove `ALL_SUBSYSTEMS` + the name aliases
  (`adapters/subsystem.py:411-432`); `simulation_test.py` discovers
  subsystems by globbing `backend/config/subsystems/*.yaml` and reads
  `name`/`required` from the files (assertions about *which* subsystems
  exist stay, but the names come from the config the test validates).
- `SubsystemState`: keep as an internal adapter enum if desired, but its
  `READY` member must equal the configured `states.ready` (or replace with
  strings).

### R8 — Sim scripts and discovery stay a documented convention

Keep `backend/simulation/<name>_sim.py` as the "simmable" marker (it is
already convention-based and documented in the README). The only
improvement: the one-liner could drop the name and pass it via argv/env
from `process_manager` (the manager already knows the name), removing the
last duplicated copy of each subsystem name.

### R9 — Launcher/test-suite hygiene (minor)

- `launch_gui.py`: read the `--num-objects` default from `global.yaml`
  (`simulation.default_num_objects`) instead of `3`; the frontend port
  `3000` check should read the same source the frontend uses.
- `simulation_test.py`: derive state/event names from the YAMLs it loads
  (terminal states from `workflow.yaml`, object events from
  `object.yaml`) so config renames surface as *readable* test diffs rather
  than mysterious pipeline stalls.

---

# Part II — Frontend (TypeScript)

The frontend is a thin WebSocket client: it renders whatever the backend
broadcasts and sends operator commands. Most of it is already data-driven;
the hardcoded parts are concentrated in the **operator control logic** (which
buttons exist and when they are enabled), a few **state-name literals**, and
**display conventions** shared with the backend.

## 6. What the frontend already gets from the config (do not touch)

| Feature | Source over the wire |
|---|---|
| Subsystem cards (names, display names, `required` badge, order) | `subsystems_config` in the status broadcast (from `subsystems/*.yaml`) |
| Subsystem/object state-machine inspector (SVG graph, descriptions, initial-state marker) | `subsystem_state_machines` / `object_state_machine` in the broadcast — **the same YAMLs the engines run** (`sm-graph.ts` is fully generic: layout, edges, wrapping, highlighting) |
| Object card color + step index + progress bar | `state_order` / `state_colors` in the broadcast (from `global.yaml`), via `buildStateStyles()` |
| Workflow state messages | `state_messages` in the broadcast (`updateWorkflowMessage` **prefers the backend value**, TS file is fallback only) |
| Debug panel (topics + default payloads) | `debug.topics` in the broadcast (built by the backend from the YAMLs) |
| SIM/REAL chips | `simulation` info in the broadcast |
| All state descriptions, transitions, message feeds | status broadcasts + events |

So: adding/renaming a subsystem or redrawing any state machine needs **no
frontend code change** — the inspector and cards follow the YAML automatically.

## 7. Frontend: inventory of hardcoded behaviour

Line numbers refer to `frontend/` at 2026-10-06.

### 7.1 `src/contracts.ts` + `src/state/store.ts` — the wire model

- **`WorkflowStateType` literal union** (`contracts.ts:30-37`):
  `'idle' | 'starting' | 'running' | 'paused' | 'stopping' | 'stopped' |
  'error'` — a type-level duplicate of `workflow.yaml` `states:`. Renaming a
  workflow state breaks compilation in `button-rules.ts` / `state-messages.ts`
  (both are keyed `Record<WorkflowStateType, …>`); at runtime it is worse
  (see 7.2).
- **`createInitialState`** (`store.ts:57,61`): `workflowState: 'idle'` and
  `message: 'Waiting for operator input'` — the initial workflow state and
  its message hardcoded (third copy after `index.html` and `global.yaml`).
- **`ready: newState === 'ready'`** (`store.ts:186`): the "ready" state name
  hardcoded when folding `subsystem_state_changed` events.
- **`object_added` fallback state `'identified_in_box'`** (`store.ts:422`):
  the object initial state hardcoded (duplicates the backend hardcoding in
  `workflow.py:create_object`; after R6 the backend sends the real state and
  this fallback only hides bugs).
- **Dead legacy wire branches** (`store.ts:86-135, 278`): `reduceState`
  still handles top-level `m.subsystems` (array), `m.subsystems_config`,
  `m.new_state` and `m.state` shapes that the current backend **never sends**
  — `frontend_server.py` always wraps payloads as
  `{event: <type>, details: {…}}`. These branches are vestiges of an older
  protocol; the live path is the `workflow_state_changed` details block.
  (Also note `UiState.message` is written but never rendered, and
  `SubsystemStatus.ready` is written but never read by any UI code — and the
  full-status `SubsystemInfo.to_dict()` in the backend does not even contain
  a `ready` field, so the contract is inconsistent with the wire.)
- **UI tuning constants** (`store.ts:16, 520`): message-history limit 100,
  log trim 200 — presentation-only, fine to keep in code.

### 7.2 `src/config/button-rules.ts` — the operator matrix (the biggest offender)

- **`BUTTON_RULES`** (`button-rules.ts:20-76`): the complete *which operator
  button is enabled in which workflow state* matrix, hardcoded:
  start from `idle/stopped/error`, pause from `starting/running`, resume from
  `paused`, stop from `starting/running/paused`, reset from `stopped/error`.
  This is a behavioural duplicate of the operator-facing transitions in
  `workflow.yaml`. The file header even admits it: *"should stay in sync
  with the backend configuration"* — i.e. sync by hand.
- **`getButtonRules` fallback** (`button-rules.ts:78-85`): an unknown state
  (any new state added to `workflow.yaml`) yields **all buttons disabled** —
  the operator is locked out of the machine until the TS file is edited.
- Used in two places: `updateActionButtons` (`ui/app.ts:451`) and the SIM
  chip gating, which reuses `rules.start` as "may toggle simulation"
  (`ui/app.ts:333-335`) — so the sim toggle is coupled to the same matrix
  (and to the backend's `toggle_allowed_in` rule, §2.2, a third copy of the
  same fact).

### 7.3 `src/config/state-messages.ts` — message fallbacks

- **`DEFAULT_STATE_MESSAGES`** (`state-messages.ts:12-20`): duplicate of
  `global.yaml` `workflow_states`, used as fallback when the backend map is
  missing, and for the **`error` state special case** in
  `updateWorkflowMessage` (`ui/app.ts:225-227`), which hardcodes
  `workflowState === 'error'` and interpolates the concrete error text
  (behaviour the backend's static message cannot express).

### 7.4 `src/ui/app.ts` — state-name literals in rendering

- **`subsystemTone()`** (`app.ts:34-51`): hardcoded state→tone classification
  for the card dot/label: `ready→ready`, `error→error`, `offline→offline`,
  `paused`/`tool_changing→warning`, `stopped`/`initializing`/`reset→idle`,
  everything else `→busy`. Three of the named states (`paused`,
  `tool_changing`, `reset`) **do not exist in any current subsystem YAML**
  (`reset` is an event name, not a state) — stale copies of an older
  vocabulary. A subsystem reporting a state that *should* look idle/warning
  but is not listed renders as generic `busy`.
- **`updateStateBadge`** (`app.ts:205-209`): `badge.className =
  'state-badge ' + workflowState` — the CSS class is the raw state name, so
  per-state styling lives in `app.css` under the state names (see 7.6).
- **`updateWorkflowMessage`** (`app.ts:222-234`): hardcodes the `'error'`
  state name for the concrete-error special case (otherwise correctly
  prefers the backend message).
- **Object card semantics on color names** (`app.ts:276, 282`):
  fallback color class `'gray'`, and `colorClass === 'analyzed'` →
  `is-done` styling — a *behavioural* check on a **configured color value**
  (`global.yaml` `state_colors.object_analyzed: analyzed`). Renaming that
  color value silently loses the "done" styling.
- **Subsystem card defaults** (`app.ts:345-346`): missing info → state
  `'offline'`; tone via `subsystemTone`.

### 7.5 `src/ui/sm-graph.ts` + `src/ui/state-machine-modal.ts` — protocol conventions

- **`'workflow'` scope convention** (`sm-graph.ts:61, 70`;
  `state-machine-modal.ts:478`): events whose scope is literally `workflow`
  are coloured/labelled "internal / → pipeline" — the backend's `workflow/`
  scope convention (§2.2) duplicated in three rendering places.
- **`formatObjectTarget`** (`state-machine-modal.ts:493-498`): hardcoded
  strings `'pipeline (internal)'` and `'all subsystems (broadcast)'` — these
  must **exactly match the `target` labels built in
  `backend/orchestrator/workflow.py:_on_object_event`** ("pipeline
  (internal)" / "all subsystems (broadcast)"). A pure cross-language string
coupling: rename the backend labels and the object feed starts mislabeling
  internal/broadcast targets as "X subsystem".
- **Debug panel conventions** (`state-machine-modal.ts:325, 342`):
  `topic.endsWith('/status')` decides whether the state quick-pick is shown
  (topic suffix convention, cf. §2.5), and
  `payload.ready = value === 'ready'` keeps the `ready` payload field in
  sync with the picked state (the `ready` state name, third frontend copy,
must match the backend status-payload convention in
  `subsystem_sim.py:291` / `workflow.py:766`).

### 7.6 `index.html` + `src/styles/app.css` + entry points

- **Static initial content** (`index.html:31-32, 66-68`): the badge starts
  as `class="state-badge idle"` with text `idle`, the workflow message starts
  as "Waiting for operator input" (duplicates `global.yaml` `workflow_states`
  `idle`), and the alias field has **`maxlength="64"`** — a hardcoded copy of
  the backend's `ALIAS_PATTERN` (R1 moves the pattern to `global.yaml`; the
  frontend must learn the constraint from the same place). Same page:
  **placeholder text "Alias for the *cloud database* (e.g. AMPHORA_7)"** —
  stale copy: the project has **no cloud** (data is stored locally, the
  alias is the *local-database* ID — see `docs/PROJECT_CONTEXT.md`, README,
  journal). It should say "local database".
- **Fixed operator button set** (`index.html:41-50` +
  `src/index.ts:11-18`): Start / Pause / Resume / Stop + Reset / Refresh
  Status, bound to the `WorkflowCommand` wire names. The *set* of physical
  buttons is inherently UI, but note the enable logic for them is the
  hardcoded matrix of 7.2.
- **`app.css` state-keyed classes**:
  - `.state-badge.idle/.starting/.running/.paused/.stopping/.stopped/.error`
    and — stale — **`.state-badge.completing`** (`app.css:199-219`): a class
    for a workflow state that no longer exists in `workflow.yaml`. New
    workflow states get no styling (and no badge color) until CSS is added;
    renaming a state orphans its class.
  - `.object-state.state-<color>` classes (`app.css:511-519`): keyed on the
    **color values** of `global.yaml` `state_colors` (`identified`, `waiting`,
    `moving`, `imaging`, `rotating`, `planning`, `measuring`, `analyzed`,
    `gray`). A brand-new color value needs a new class; renaming a value
    orphans the style.
  - Tone classes `.tone-ready/.tone-busy/.tone-error/.tone-warning/`
    `.tone-idle/.tone-offline` (`app.css:608-613, 680-685`): the six visual
    tones are a fixed visual vocabulary (fine), but which state maps to which
    tone is the hardcoded table of 7.4.
- **WS URL default `ws://localhost:8080`** in three places
  (`src/api/client.ts:8`, `src/ui/app.ts:62`, `src/index.ts:7`) plus the
  dev-server proxy target (`vite.config.ts:8`). `launch_gui.py` overrides it
  via `VITE_WS_URL`; a manual `npm run dev` uses the literals, which must
  track `global.yaml` `frontend.port`.

## 8. The frontend coupling map — "if I change this config, what breaks in the UI?"

✅ = works with YAML-only change; ⚠️ = frontend file must be edited; ✗ =
UI misbehaves (wrong/missing buttons or styling) if the TS is not edited.

### 8.1 `config/workflow.yaml`

| Change | Frontend impact | Frontend locations |
|---|---|---|
| **Rename a state** | ✗ TS compile errors + the renamed state renders with no badge style; if `idle` is renamed, the app's initial render shows the old name until the first broadcast | `contracts.ts:30-37`; `button-rules.ts:20`; `state-messages.ts:12`; `store.ts:57`; `app.css:199-219`; `index.html:31-32` |
| **Add a new state** (e.g. `completing`) | ✗ all operator buttons disabled in that state (matrix fallback, 7.2); no badge styling unless a class exists (`.state-badge.completing` already does — stale leftover, 7.6) | `button-rules.ts`; `app.css` |
| **Change operator transitions** (e.g. allow `stop_requested` from `idle`, or a new operator event) | ✗ buttons stay wrong (disabled where the workflow now allows, or no button at all for a new action) | `button-rules.ts`; for a new action also `index.html` + `index.ts` binding + `contracts.ts` `WorkflowCommand` |
| **Change `global.yaml workflow_states` messages** | ✅ nothing (backend messages are preferred at render) | — |

### 8.2 `config/object.yaml` + `global.yaml object_workflow`

| Change | Frontend impact | Frontend locations |
|---|---|---|
| **Rename an object state** | ⚠️ only the pre-broadcast fallbacks are wrong (cards are rendered from `state_order`/`state_colors` once the first status broadcast arrives); the `object_added` fallback shows the old name for one frame | `object-styles.ts:15-47` (fallback mirror); `store.ts:422`; `index.html`/initial text n/a |
| **Add/remove a state or change `state_order`** | ✅ nothing (order, steps and progress derive from the broadcast) — unless a state lacks a color entry: it renders as `gray` (acceptable) | — |
| **Introduce a new color value** in `state_colors` | ⚠️ no style for the new class | `app.css:511-519` (add `.state-<color>`); `object-styles.ts` fallback mirror |
| **Rename the `analyzed` color value** | ✗ completed objects lose the `is-done` styling silently | `app.ts:282` (fixed by F4) |

### 8.3 `config/subsystems/<name>.yaml`

| Change | Frontend impact | Frontend locations |
|---|---|---|
| **Add a subsystem** | ✅ cards + inspector follow the config automatically | — |
| **Rename a subsystem** | ✅ nothing (cards/inspector are name-driven from the broadcast) | — |
| **Rename a state** | ⚠️ tone may be wrong (new name falls back to `busy` — usually *fine* for work states); states that should look idle/warning (e.g. a renamed `initializing`/`stopped`) render as `busy` | `app.ts:34-51` (fixed by F3) |
| **Rename the `ready` state** | ✗ the `ready` payload field goes stale (store + debug panel), tone of the ready card wrong | `store.ts:186`; `state-machine-modal.ts:342`; `app.ts:36` (fixed by F3/F5) |
| **Rename an event** | ✅ nothing (graphs/feeds render the event names from the backend definition as-is) | — |

### 8.4 Backend protocol conventions shared with the UI

| Convention (today: backend code constant) | Frontend copy | Locations |
|---|---|---|
| `workflow/` scope = internal pipeline event | edge colouring/labels + object-feed event class | `sm-graph.ts:61,70`; `state-machine-modal.ts:478` |
| Target labels `"pipeline (internal)"` / `"all subsystems (broadcast)"` | feed target formatting (exact string match) | `state-machine-modal.ts:493-498` ↔ `workflow.py:_on_object_event` |
| Topic suffix `/status` carries a `state` field | debug-panel state quick-pick visibility | `state-machine-modal.ts:325` ↔ `workflow.py:_build_debug_topics` |
| Status payload field `ready = (state == ready)` | debug-panel payload sync | `state-machine-modal.ts:342` ↔ `subsystem_sim.py:291`, `workflow.py:766` |
| Alias format/length (64 chars) | `maxlength` + validation UX | `index.html:68` ↔ `workflow.py:541` |
| `frontend.port` (8080) | WS default URLs + vite proxy | `client.ts:8`; `app.ts:62`; `index.ts:7`; `vite.config.ts:8` |

## 9. Frontend changes (F1–F8)

Ordered so each step ships independently; `npm run build` (runs `tsc`) plus
the simulated end-to-end run (§11) must stay green after each step.

### F1 — Derive the button matrix from the workflow SM (delete `button-rules.ts`)

Two halves:

- **Backend**: include the *workflow* state machine definition in the
  broadcast (today `set_state_machine_definitions` sends subsystem SMs + the
  object blueprint, but not the workflow SM — `workflow.py:start()`), and
  include the operator-event map (R3's `metadata.operator_events`) in the
  same payload. One line each.
- **Frontend**: build the enable matrix at runtime: button *B* is enabled in
  state *S* ⟺ the workflow SM has a transition from *S* whose event maps to
  *B* in `operator_events` (start/pause/resume/stop/reset). Delete
  `BUTTON_RULES` and the `WorkflowStateType` keying; states become `string`.
  Unknown/new states get the *correct* buttons for free, and the sim-toggle
  gating (`app.ts:333-335`) reuses the derived "start available" predicate —
  which after R3 equals the backend's `toggle_allowed_in`, removing the
  third copy of that fact.

**Result:** any change to workflow states or operator transitions is a
YAML-only change for the UI as well.

### F2 — Delete the frontend's copy of the workflow state names

- `contracts.ts`: `WorkflowStateType` → `string` (keep the type alias for
  API stability if desired, defined as `string`).
- `state-messages.ts`: drop `DEFAULT_STATE_MESSAGES` (the backend map is
  already preferred at render); keep only the *error interpolation* helper —
  and drive it off the broadcasted message instead of a hardcoded
  `'error'` comparison: the backend can send the `error` state's message
  template (or the concrete message with the error detail) in the
  `error_reported` / status payload, so `updateWorkflowMessage` becomes
  "show `state.error` when the workflow is in the state the config marks as
  error, else show the configured message". (Minimal variant: keep the
  `'error'` literal but source it from the workflow SM def's state list —
  the state whose transitions the config marks `terminal_error`.)
- `store.ts:57,61`: initial `workflowState`/`message` from the first
  broadcast (the replay on connect already delivers it); the static
  `index.html` text stays as pre-connection cosmetics only.

### F3 — Subsystem tone from data (delete `subsystemTone`'s state list)

- **Backend**: add a `tone` field to `subsystem_status_updated` /
  `subsystem_state_changed` details (and the full-status subsystem entries),
  computed once in the orchestrator from the R1 vocabulary:
  `error→error`, `offline→offline`, `ready→ready`, other `non_work`→`idle`,
  anything else→`busy`. (A `warning` tone is available if a future state is
  marked `severity: warning` in subsystem metadata — optional.)
- **Frontend**: `subsystemTone()` becomes "use the backend tone, fallback
  `busy`"; the stale `paused`/`tool_changing`/`reset` entries disappear; the
  `ready` boolean in `store.ts:186` becomes backend-sent (or is dropped — no
  UI code reads it today).

**Result:** renaming or adding subsystem states needs no TS edit; new work
states still look `busy` by construction.

### F4 — Object "done" semantics from config

After R6 the backend knows the object *final* state; include it in the
display config (e.g. `state_messages` sibling: `final_state:
object_analyzed`, or reuse `state_order` last entry). `app.ts:282` checks
`obj.state === finalState` instead of `colorClass === 'analyzed'`; the
`'gray'` fallback stays (purely visual). Renaming the `analyzed` color
value no longer affects behaviour.

### F5 — Debug-panel conventions from descriptors

- `workflow.py:_build_debug_topics`: tag each topic descriptor with
  `kind: status | response` (and, for status, the `state_field: "state"` /
  `ready_field: "ready"` names). `state-machine-modal.ts:325` uses
  `kind === 'status'` instead of `endsWith('/status')`; `:342` recomputes
  `payload[ready_field] = (picked === configuredReadyState)` with the ready
  state name from the R1 global config broadcast.

### F6 — Drop the dead legacy wire branches in `store.ts`

Remove `m.subsystems` (array), top-level `m.subsystems_config`,
`m.new_state` and top-level `m.state` handling (`store.ts:86-135, 278`) —
the backend only ever sends `{event, details}`. Also drop the
write-only `UiState.message` and the inconsistent `SubsystemStatus.ready`
contract field (or make the backend send it — pick one, align the
contract). Reduces the protocol surface to exactly what
`frontend_server.py` emits.

### F7 — Copy + constraints

- `index.html:66`: "Alias for the **cloud** database" → "Alias ID (local
  database)" — the project has no cloud (PROJECT_CONTEXT, README, journal).
- Alias `maxlength` (and, if desired, client-side pattern validation with
  the same error message): source from the backend (R1 sends
  `alias_pattern` in the display config) so the 64-char limit and the
  allowed character set live in `global.yaml` only.
- `app.css`: delete the stale `.state-badge.completing` rule; document that
  badge classes are per-state-name (new state → new class, visual only) or,
  with F1's SM def, derive the badge class from a config-provided
  state→tone map (same idea as F3 for workflow states — optional).
- WS URL: keep one place (`client.ts`) reading `VITE_WS_URL` with the
  default; `vite.config.ts` proxy target must track `global.yaml
  frontend.port` (or drop the proxy and always use the absolute URL).

### F8 — `object-styles.ts` fallback: shrink, don't delete

The `DEFAULT_STATE_ORDER`/`DEFAULT_STATE_COLORS` mirror of `global.yaml`
exists so a bare `npm run dev` (no backend) still renders something. Keep
it, but mark it explicitly as *offline dev fallback* in the header comment,
and make CI/`tsc` the only thing that depends on it. (Deleting it entirely
is also fine if the "dev without backend" use case is dropped.)

---

# Part III — Result

## 10. After R1–R9 + F1–F8: the new change guide (what you actually edit)

| You changed | You still have to touch |
|---|---|
| A subsystem's states / events / durations / `required` / `mqtt_topic` | **Nothing in Python or TypeScript.** (Sim auto-adapts: same YAML, `work_durations` keys, roles from `metadata`; UI tone/inspector follow the broadcast, F3.) |
| Add a new subsystem | New `config/subsystems/<name>.yaml` + a one-line `backend/simulation/<name>_sim.py` (optional, for the SIM toggle). If it participates in the object flow: add it to the relevant `object.yaml` state `meta.actor` / `parallel_actors` / transition actions. UI: nothing (cards + inspector are config-driven). |
| Rename/delete a subsystem | Its YAML (name), the sim script filename, and the `object.yaml` refs — all declarative files. UI: nothing. |
| The object journey (states, order, stages, actors, parallelism) | `object.yaml` only (states + transitions + `meta`). Display: `global.yaml` `object_workflow` + its `object-styles.ts` dev-fallback mirror (F8) + a CSS class per *new* color value. |
| The run lifecycle (states, operator events, readiness, toggle rules) | `workflow.yaml` only (states + transitions + `metadata`). Display messages: `global.yaml` `workflow_states`. UI: buttons derive from the broadcast SM def (F1); badge CSS class per *new/renamed* state name (visual only, F7). |
| Subsystem vocabulary (non-work states, `ready`, report events) or MQTT topic tree | `global.yaml` only (R1/R4); tones and `ready` fields follow (F3/F5). |
| Broker/ports/timeouts/alias rules/sim timings | `global.yaml` only; the alias field constraint in `index.html` follows the broadcasted pattern (F7); `vite.config.ts` proxy target tracks `frontend.port`. |

Remaining *intentional* code: the state-machine **engine** (Python), the
MQTT client mechanism, the physical capacity *rules* in `pipeline.py`
(parameterised by `disks`/`hand_slots`), the WebSocket transport, the fixed
set of operator *buttons* (a physical UI fact), and **CSS color classes**
(purely visual: one class per color value / badge state name).

## 11. Verification

After each refactor step:

```bash
timeout 500 python backend/simulation_test.py      # backend behaviour
(cd frontend && npm run build)                     # tsc type-check + bundle
```

(always wrap the sim test in a timeout — a pipeline regression hangs the
suite). Additionally, for a change to a specific YAML, the cheapest smoke
test is a full simulated run:

```bash
python launch_gui.py --simulation --num-objects 3
```

and checking that: the run reaches `stopped` with all objects at the final
state and no `subsystem_state_mismatch` warnings (backend); the operator
buttons enable/disable correctly through the whole lifecycle, the badge and
cards show the right names/styles, the inspector renders the updated graphs,
and the alias field enforces the configured constraint (frontend).

Expected failure modes that the current hardcoding hides (and R1–R6 / F1–F5
expose): a stage whose actor completes but is not in `parallel_actors`
completes the stage too early; an object state without `meta.position`
stalls `_advance`; a renamed `init_complete` event leaves every sim stuck in
its initial state (the discrepancy warning will flag it after 10 s); a new
workflow state renders with **all operator buttons disabled** until the TS
matrix is updated (F1 removes this failure mode entirely).
