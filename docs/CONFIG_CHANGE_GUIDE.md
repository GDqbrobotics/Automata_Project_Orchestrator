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
| `parallel_actors` | Subsystems that MUST ALL complete for the state to end | `parallel_actors: [robot_first, xrf, raman]` |
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
- States with `actor` must have a transition whose event matches the actor's completion event (`complete` by convention)
- States with `parallel_actors` — the pipeline tracks each actor; state ends when ALL report ready
- `rotate_request`/`rotate_command` pairs must match `rotating_platform.yaml` events
- `task_chains` ops must correspond to states with `actor_op` set

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
    measuring_xrf_raman: 6.0
```

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

## Adding a New Subsystem (Checklist)

1. **Create** `backend/config/subsystems/my_subsystem.yaml` with:
   - `name`, `display_name`, `type`, `mqtt_topic`, `initial_state`, `required`
   - `states:` with transitions (at minimum: `initializing` → `ready` via `init_complete`)
   - `metadata:` with `init_duration`, `work_durations`, optionally `reports`

2. **Create** `backend/simulation/my_subsystem_sim.py`:
   ```python
   from backend.simulation.subsystem_sim import main
   if __name__ == "__main__":
       main()
   ```

3. **Restart** orchestrator — the subsystem card, MQTT topics, and simulation toggle appear automatically.

4. **Update** `object.yaml` if the new subsystem participates in the object journey:
   - Add `actor: my_subsystem` or `parallel_actors: [..., my_subsystem]` to relevant states
   - Add scoped events to transitions (e.g., `event: my_subsystem/do_work`)

5. **Run** `timeout 500 python backend/simulation_test.py` — new config-consistency checks will validate the addition.

---

## Modifying an Existing Subsystem

| Change | Files to Update | Restart Required? |
|--------|-----------------|-------------------|
| Add/rename state | Subsystem YAML | Yes |
| Add/rename transition event | Subsystem YAML + any YAML referencing the event (object.yaml, workflow.yaml) | Yes |
| Change simulation duration | Subsystem YAML `metadata.work_durations` | **No** (hot-reload in sim) |
| Change MQTT topic | Subsystem YAML `mqtt_topic` | Yes |
| Change required flag | Subsystem YAML `required` | Yes |
| Add reporting role | Subsystem YAML `metadata.reports` | Yes |

---

## Modifying the Object Journey

| Change | Files to Update | Restart Required? |
|--------|-----------------|-------------------|
| Add intermediate state | `object.yaml` (states + transitions + meta) | Yes |
| Reorder journey | `object.yaml` transitions | Yes |
| Change actor for a state | `object.yaml` state `meta.actor` | Yes |
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
states: {state_name: {description, meta{position, actor?, parallel_actors[], rotate_request?, rotate_command?, actor_op?}, transitions[{event, target, actions[]}]}}
```

### subsystems/*.yaml
```yaml
name, display_name, type, mqtt_topic, initial_state, required
metadata: {description, hardware, init_duration, work_durations{}, reports[{type, after{from, to}, object_ids{count_source, format?}}]}
states: {state_name: {description, transitions[{event, target, actions[]}]}}
```

---

*This guide is the contract between configuration and code. If a change requires code modification beyond what's described here, the architecture needs review.*