# State machine specification

This document describes the state-machine configuration used by the AUTOMATA GUI orchestrator.

## Overview

The system uses **two levels** of state machines, both driven by declarative YAML configuration:

1. **Workflow state machine** – Manages the high-level workflow lifecycle (idle → starting → running → paused → stopping → stopped → error). Defined in `backend/config/workflow.yaml`.

2. **Subsystem state machines** – Each subsystem has its own state machine with its own states, transitions, and actions. Defined in individual YAML files in `backend/config/subsystems/` (e.g., `rotating_platform.yaml`, `vision.yaml`).

## Core concepts

### State
A state represents the current condition of either the workflow or a subsystem.

**Workflow states**:
- `idle` – Waiting for operator input
- `starting` – Preparing the workflow (waiting for subsystems)
- `running` – Workflow in progress
- `paused` – Workflow temporarily suspended
- `stopping` – Stopping workflow
- `stopped` – Workflow ended
- `error` – Workflow interrupted by error

**Subsystem states** (varies per subsystem, examples):
- `ready` – Subsystem is idle and ready
- `running` / `measuring` / `capturing` / `analyzing` / `uploading` – Subsystem actively performing work
- `paused` – Subsystem temporarily suspended
- `stopped` – Subsystem has stopped
- `error` – Subsystem in error state

### Event
An event is an external or internal trigger that causes a state machine to evaluate a transition.

**Workflow events**:
- `start_requested` – Operator starts the workflow
- `pause_requested` – Operator pauses the workflow
- `resume_requested` – Operator resumes the workflow
- `stop_requested` – Operator stops the workflow
- `subsystem_ready` – All required subsystems are ready
- `subsystem_error` – A subsystem reports an error
- `stopped` – Subsystems have stopped (for stopping → stopped transition)
- `reset_requested` – Operator resets after an error

**Subsystem events** (varies per subsystem, examples):
- `start` – Start working
- `stop` – Stop working
- `pause` / `resume` – Pause / resume
- `complete` – Operation completed
- `error` – Error occurred
- `reset` – Reset after error
- `tool_change` – Changing end effector (robot_first)

### Transition
A transition is a valid move from one state to another, triggered by an event.

### Action
An action is a side effect executed during a transition.

**Built-in actions**:
- `log_event` – Log the transition event
- `update_operator_ui` – Notify that the UI should update
- `publish_mqtt_message: <topic>` – Publish a message to an MQTT topic

Custom actions can be registered via `StateMachineEngine.register_action()`.

## Configuration structure

### Workflow state machine (`backend/config/workflow.yaml`)

```yaml
name: automata_gui_workflow
initial_state: idle

states:
  idle:
    description: Waiting for operator input
    transitions:
      - event: start_requested
        target: starting
        actions:
          - log_event
          - publish_mqtt_message: workflow.start

  starting:
    description: Preparing the workflow
    transitions:
      - event: subsystem_ready
        target: running
        actions:
          - log_event
          - update_operator_ui

      - event: stop_requested
        target: stopped
        actions:
          - publish_mqtt_message: workflow.stop

  running:
    description: Workflow in progress
    transitions:
      - event: pause_requested
        target: paused
        actions:
          - publish_mqtt_message: workflow.pause

      - event: stop_requested
        target: stopping
        actions:
          - publish_mqtt_message: workflow.stop

      - event: subsystem_error
        target: error
        actions:
          - log_event
          - update_operator_ui

  paused:
    description: Workflow temporarily suspended
    transitions:
      - event: resume_requested
        target: running
        actions:
          - publish_mqtt_message: workflow.resume

      - event: stop_requested
        target: stopping
        actions:
          - publish_mqtt_message: workflow.stop

  stopping:
    description: Stopping workflow
    transitions:
      - event: stopped
        target: stopped
        actions:
          - log_event

  stopped:
    description: Workflow ended by operator request

  error:
    description: Workflow interrupted by error
    transitions:
      - event: reset_requested
        target: idle
        actions:
          - log_event
          - clear_error_state
```

### Subsystem state machines (`backend/config/subsystems/*.yaml`)

Each subsystem YAML file defines both subsystem metadata and its state machine:

```yaml
# Subsystem metadata
name: rotating_platform
display_name: Rotating Platform
type: platform
mqtt_topic: "automata/subsystem/{name}"
initial_state: ready
required: true
metadata:
  description: "Motorized rotary stage with transparent disks"
  hardware: "Motorized rotary stage"

# State machine definition
states:
  ready:
    description: "Subsystem is ready and idle"
    transitions:
      - event: start
        target: running
        actions:
          - log_event
          - publish_mqtt_message: subsystem/rotating_platform/running

      - event: error
        target: error
        actions:
          - log_event

  running:
    description: "Subsystem is actively performing operations"
    transitions:
      - event: pause
        target: paused
        actions:
          - log_event

      - event: stop
        target: stopped
        actions:
          - log_event
          - publish_mqtt_message: subsystem/rotating_platform/stopped

      - event: error
        target: error
        actions:
          - log_event

  paused:
    description: "Subsystem is paused, waiting for resume"
    transitions:
      - event: resume
        target: running
        actions:
          - log_event

      - event: stop
        target: stopped
        actions:
          - log_event

  stopped:
    description: "Subsystem has stopped"
    transitions:
      - event: start
        target: running
        actions:
          - log_event

      - event: ready
        target: ready
        actions:
          - log_event

  error:
    description: "Subsystem is in error state"
    transitions:
      - event: reset
        target: ready
        actions:
          - log_event
```

### Available subsystem YAML files

| File | Subsystem | Key states |
|---|---|---|
| `rotating_platform.yaml` | Rotating Platform | ready → running → paused → stopped → error |
| `vision.yaml` | Vision System | ready → running → stopped → error |
| `robot_first.yaml` | Robot Arm 1 | ready → tool_changing → running → paused → stopped → error |
| `robot_second.yaml` | Robot Arm 2 | ready → running → paused → stopped → error |
| `xrf.yaml` | XRF Analyzer | ready → measuring → stopped → error |
| `raman.yaml` | Raman Spectrometer | ready → measuring → stopped → error |
| `imaging_3di_hsi.yaml` | 3DI/HSI Imaging | ready → capturing → stopped → error |
| `ai_analysis.yaml` | AI Analysis | ready → analyzing → stopped → error |
| `cloud_database.yaml` | Cloud Database | ready → uploading → stopped → error |

## How the orchestrator manages state machines

The `WorkflowOrchestrator` coordinates both levels:

1. **Loads** the workflow state machine from `workflow.yaml`
2. **Loads** each subsystem's YAML from `config/subsystems/*.yaml`
3. **Creates** a `SubsystemAdapter` for each subsystem, each owning its own `StateMachineEngine`
4. **Routes** incoming MQTT messages to update subsystem states
5. **Monitors** subsystem states to trigger workflow transitions (e.g., all subsystems ready → running)
6. **Drives** subsystem state machines via `fire_subsystem_transition(subsystem_name, event)`

### Subsystem state machine transitions

When a subsystem's state machine transitions, the orchestrator:

1. Fires the event on the subsystem's state machine
2. Publishes the new state via MQTT to `automata/subsystem/{name}/status`
3. Broadcasts the change to the frontend via WebSocket
4. Checks if the transition should affect the main workflow state

### Frontend subsystem cards

The frontend renders a card for each subsystem showing:
- **Display name** (from YAML `display_name`)
- **Required indicator** (*) for required subsystems
- **Current state** (colored badge: ready=green, running=blue, error=red, etc.)
- **State description** (from the state machine YAML)

## Recommended rules

1. All workflow behavior should be driven by state machine configuration.
2. The UI should react to state changes, not directly decide the next step.
3. Subsystem communication should be encapsulated behind adapters.
4. Each transition should be explicit and easy to inspect.
5. Invalid transitions should be rejected with a clear error.
6. Errors and recovery paths should be modeled as first-class states.
7. Subsystem state machines should be independent of the workflow state machine.

## Validation expectations

The state-machine engine validates that:

- The initial state exists.
- Every transition target exists.
- No transition is defined for an unknown event without a fallback.
- The configuration is syntactically valid YAML.
- The orchestration runtime can load and execute it safely.

## Implementation pattern

The runtime follows this flow for any state machine:

1. Receive an event.
2. Look up the current state.
3. Find a matching transition for the event.
4. Execute transition actions.
5. Update the active state.
6. Notify the UI and relevant adapters.
