# State Machine Design Principles

This document describes the design principles and configuration patterns used by the AUTOMATA GUI orchestrator.

## Three-Level Architecture

The system uses three layers of declarative state machines, all driven by YAML configuration:

| Level | Scope | Blueprint | Persistence |
|---|---|---|---|
| **Workflow** | Whole system lifecycle | `backend/config/workflow.yaml` | No |
| **Subsystem** | One per hardware/software subsystem | `backend/config/subsystems/<name>.yaml` | No |
| **Object** | One instance per analyzed sample | `backend/config/object.yaml` | `backend/object_states/<id>.json` |

Each level is independent. Communication flows upward through **internal events** (not MQTT):

```
Object SM ──event──► Orchestrator ──► Subsystem SM ──MQTT──► Hardware
Workflow SM ──event──► Orchestrator ──► (broadcast / route to subsystems)
```

Subsystem state machines are the **boundary layer**: they are the only level that publishes MQTT.

## Core Concepts

**State** — A named condition (`idle`, `running`, `error`, `ready`, `identified_in_box`, …). Every state machine has an `initial_state` and a set of named states, each with an optional description.

**Event** — A trigger that causes the engine to look up a transition. Events can be **unscoped** (`error`, `start_requested`) or **scoped** (`robot_first/pick`, `all/resume`). Scoping routes the event to a specific subsystem; the special `all/` scope broadcasts to every subsystem.

**Transition** — A mapping from `(current_state, event)` → `(target_state, actions)`. Defined entirely in YAML. Invalid transitions raise `StateMachineError`.

**Action** — A side effect executed during a transition. Actions are either strings (built-in handlers) or dicts with a single key (custom handlers). The only built-in action is `log_event`. Custom actions are registered at runtime via `StateMachineEngine.register_action()`. Event actions (`event: name`) fire the on-transition-completion callback for cross-level communication.

## Configuration Files

| File | Purpose |
|---|---|
| `backend/config/workflow.yaml` | Workflow lifecycle states and transitions |
| `backend/config/object.yaml` | Per-object analysis workflow blueprint |
| `backend/config/subsystems/*.yaml` | Subsystem metadata and state machine definitions |
| `backend/config/global.yaml` | Global defaults (MQTT broker, frontend port, state colors/order) |

## Design Principles

1. **Behavior lives in config, not code.** States, transitions, events, and actions are defined in YAML. The engine provides the mechanism; configuration provides the logic.

2. **MQTT is confined to the boundary.** Object and workflow state machines never publish MQTT. Only subsystem state machines do, via the orchestrator's boundary callback.

3. **Internal events drive cross-level communication.** State machines communicate through scoped internal events routed by the orchestrator, not through shared memory or direct method calls.

4. **Object state is persistent.** Each object's state machine is saved to `backend/object_states/<id>.json` on every transition and restored on restart.

5. **Invalid transitions are rejected.** The engine validates at runtime and raises `StateMachineError` for undefined events in the current state.

6. **Subsystem adapters are pluggable.** Adding a new subsystem requires only a YAML file — no code changes.

7. **Error and recovery are first-class states.** `error` and `stopped` have explicit transitions back to `idle` via `reset_requested`.

8. **The frontend is reactive, not decisional.** The UI renders state and captures operator intent; all workflow logic resides in the backend state machines.

9. **Simulation is identical to reality.** In simulation mode each subsystem runs as a separate process (`backend/simulation/<name>_sim.py`) that loads the *same* YAML state machine and communicates with the orchestrator exclusively over MQTT, exactly like the real hardware. The orchestrator's `PipelineController` (`backend/orchestrator/pipeline.py`) drives the object flow from subsystem status reports: which subsystems act and which run in parallel is declared in `object.yaml` (scoped events), while physical capacity rules (2 disk slots, 1 end effector, coupled rotations) live in the controller.

10. **Scoping is uniform.** The same `scope/event` syntax works for both event actions and action targets, enabling consistent routing across all three levels.
