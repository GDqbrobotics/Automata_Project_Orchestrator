# State Machine Design Principles

This document describes the design principles and configuration patterns used by the AUTOMATA GUI orchestrator.

## Overview

The system uses **three levels** of state machines, all driven by declarative YAML configuration:

1. **Workflow state machine** – High-level workflow lifecycle (idle → starting → running → paused → stopping → stopped → error). Defined in `backend/config/workflow.yaml`.

2. **Subsystem state machines** – Each subsystem has its own state machine with its own states, transitions, and actions. Defined in individual YAML files in `backend/config/subsystems/`.

3. **Object state machines** – Each analyzed object has its own state machine instance tracking the detailed analysis cycle. The blueprint is in `backend/config/object.yaml`.

## Core Concepts

### State
A state represents the current condition of a workflow, subsystem, or object.

- **Workflow states**: `idle`, `starting`, `running`, `paused`, `stopping`, `stopped`, `error`
- **Subsystem states**: Varies per subsystem (typically `initializing`, `ready`, `running`, `paused`, `stopped`, `error`)
- **Object states**: Defined per-object workflow (e.g., `identified_in_box`, `moving_to_disk`, `object_analyzed`)

### Event
An event is a trigger that causes a state machine to evaluate a transition.

- **Workflow events**: `start_requested`, `pause_requested`, `resume_requested`, `stop_requested`, `subsystem_ready`, `subsystem_error`, `reset_requested`
- **Subsystem events**: `init_complete`, `start`, `stop`, `pause`, `resume`, `complete`, `error`, `reset`
- **Object events**: `object_picked`, `object_placed_on_disk`, `imaging_capture_complete`, `measurements_complete`, etc.

### Transition
A transition moves a state machine from one state to another, triggered by an event. Defined in YAML with:
- `event` – The triggering event
- `target` – The destination state
- `actions` – Side effects to execute (e.g., `log_event`, `update_operator_ui`, `event: trigger_robot_first_pick`)

### Action
An action is a side effect executed during a transition.

**Built-in actions**:
- `log_event` – Log the transition
- `update_operator_ui` – Notify UI update
- `publish_mqtt_message: <topic>` – Publish to an MQTT topic
- `event: <name>` – Fire an internal event after transition (for cross-level communication)

Custom actions can be registered via `StateMachineEngine.register_action()`.

## Configuration Files

| File | Purpose |
|---|---|
| `backend/config/workflow.yaml` | Workflow lifecycle states and transitions |
| `backend/config/object.yaml` | Per-object analysis workflow blueprint |
| `backend/config/subsystems/*.yaml` | Subsystem metadata and state machine definitions |
| `backend/config/workflow_actions.yaml` | Maps object events to subsystem commands |
| `backend/config/global.yaml` | Global defaults (MQTT, ports, state colors, messages) |

## Three-Level Architecture

### Level 1: Workflow State Machine
Manages the high-level workflow lifecycle. Transitions are triggered by operator actions (via WebSocket) or subsystem events (via MQTT).

### Level 2: Subsystem State Machines
Each subsystem has its own state machine. The orchestrator drives transitions via `fire_subsystem_transition(subsystem_name, event)`. State changes are published via MQTT and broadcast to the frontend.

### Level 3: Object State Machines
Each analyzed object gets a state machine instance from the `object.yaml` blueprint. Transitions are driven by subsystem completions. State is persisted to `backend/object_states/{object_id}.json`.

## Cross-Level Communication

Object state machines fire internal events (not MQTT) that the orchestrator handles by triggering subsystem actions:

```
Object event → Orchestrator → Subsystem command → MQTT → Hardware
```

This keeps MQTT publishing centralized in the orchestrator's subsystem trigger methods.

## Design Principles

1. **All behavior is configuration-driven** – States, transitions, events, and actions are defined in YAML. Code provides the engine, not the logic.

2. **No hardcoded workflow logic** – The object-to-subsystem event mapping (`workflow_actions.yaml`), simulation event chain, and UI display rules are all derived from config.

3. **MQTT only at the subsystem level** – Object state machines fire internal events. Subsystem state machines publish MQTT.

4. **State machines are independent** – Workflow, subsystem, and object state machines are decoupled. Each level communicates through events.

5. **Object state persists to disk** – Survives orchestrator restarts via JSON files in `backend/object_states/`.

6. **Invalid transitions are rejected** – The state machine engine validates transitions at runtime and raises `StateMachineError` for invalid events.

7. **Frontend is reactive** – The UI renders state and captures operator actions. All business logic lives in the backend.

8. **Subsystem adapters are dynamic** – New subsystems are added by creating a YAML file. No code changes needed.

9. **Error and recovery are first-class states** – `error` and `stopped` states have explicit transitions back to `idle` via `reset_requested`.

10. **Simulation follows config** – The simulator derives its event chain from `object.yaml` and `workflow_actions.yaml`, not from hardcoded lists.

## Recommended Rules

1. All workflow behavior should be driven by state machine configuration.
2. The UI should react to state changes, not directly decide the next step.
3. Subsystem communication should be encapsulated behind adapters.
4. Each transition should be explicit and easy to inspect.
5. Invalid transitions should be rejected with a clear error.
6. Errors and recovery paths should be modeled as first-class states.
7. Subsystem state machines should be independent of the workflow state machine.
8. Object state machines should be independent of both workflow and subsystem state machines.
9. Object states should be persisted to disk for crash recovery.
10. Object transitions are driven by subsystem completions, not by direct operator input.

## Validation

The state-machine engine validates that:
- The initial state exists.
- Every transition target exists.
- The configuration is syntactically valid YAML.
- The orchestration runtime can load and execute it safely.
