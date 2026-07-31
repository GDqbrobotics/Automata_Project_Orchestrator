# State machine specification

This document describes the state-machine configuration used by the AUTOMATA GUI orchestrator.

## Overview

The system uses **three levels** of state machines, all driven by declarative YAML configuration:

1. **Workflow state machine** – Manages the high-level workflow lifecycle (idle → starting → running → paused → stopping → stopped → error). Defined in `backend/config/workflow.yaml`.

2. **Subsystem state machines** – Each subsystem has its own state machine with its own states, transitions, and actions. Defined in individual YAML files in `backend/config/subsystems/` (e.g., `rotating_platform.yaml`, `vision.yaml`).

3. **Object state machines** – Each analyzed object has its own state machine instance that tracks the detailed analysis cycle from identification to completion. The blueprint is defined in `backend/config/object.yaml`, and each object gets a runtime instance persisted to disk.

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
- `initializing` – Subsystem is initializing (all subsystems start here)
- `ready` – Subsystem is idle and ready
- `running` / `measuring` / `capturing` / `analyzing` / `uploading` – Subsystem actively performing work
- `paused` – Subsystem temporarily suspended
- `stopped` – Subsystem has stopped
- `error` – Subsystem in error state

**Object states** (defined in `object.yaml`, one instance per object):
- `identified_in_box` – Object has been identified in the box by the vision system
- `moving_to_disk` – Object is being moved from the box to the rotating platform
- `HSI_3DI_analysis` – 3D imaging and hyperspectral imaging capture is being performed
- `object_going_second_position` – Rotating platform is moving the object to the second position
- `XRF_RAMAN_analysis` – XRF and Raman puntual measurements are being performed
- `object_going_first_position` – Rotating platform is moving the object back to the first position
- `moving_to_box` – Object is being moved from the disk back to the box
- `object_analyzed` – Object analysis is complete (terminal state)

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
- `init_complete` – Initialization complete (initializing → ready)
- `start` – Start working
- `stop` – Stop working
- `pause` / `resume` – Pause / resume
- `complete` – Operation completed
- `error` – Error occurred
- `reset` – Reset after error
- `tool_change` – Changing end effector (robot_first)

**Object events** (driven by subsystem completions):
- `object_picked` – Vision system identified object, robot picks it up
- `object_placed_on_disk` – Robot places object on the rotating platform
- `imaging_capture_complete` – 3DI/HSI imaging capture finished
- `platform_rotated_to_second` – Platform rotated to second measurement position
- `measurements_complete` – XRF/Raman measurements finished
- `platform_rotated_to_first` – Platform rotated back to first position
- `object_returned_to_box` – Robot returns object to the box
- `object_analyzed` – Analysis complete (final state, no transitions)

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

### Object state machine (`backend/config/object.yaml`)

Each analyzed object gets a runtime instance of this state machine. The orchestrator creates one instance per object, drives transitions based on subsystem completions, and persists the state to disk.

**Design principle**: Object state machines do NOT publish MQTT messages. MQTT publishing is reserved for subsystem state machines only. Interactions between state machine levels are internal and driven by `event` actions.

```yaml
name: object_analysis_workflow
initial_state: identified_in_box

states:
  identified_in_box:
    description: Object has been identified in the box by the vision system
    transitions:
      - event: object_picked
        target: moving_to_disk
        actions:
          - log_event
          - event: trigger_robot_first_pick

  moving_to_disk:
    description: Object is being moved from the box to the rotating platform disk
    transitions:
      - event: object_placed_on_disk
        target: HSI_3DI_analysis
        actions:
          - log_event
          - update_operator_ui
          - event: trigger_imaging_capture

  HSI_3DI_analysis:
    description: 3D imaging and hyperspectral imaging capture is being performed
    transitions:
      - event: imaging_capture_complete
        target: object_going_second_position
        actions:
          - log_event
          - event: trigger_platform_rotate_to_second

  object_going_second_position:
    description: Rotating platform is moving the object to the second measurement position
    transitions:
      - event: platform_rotated_to_second
        target: XRF_RAMAN_analysis
        actions:
          - log_event
          - event: trigger_measurements

  XRF_RAMAN_analysis:
    description: XRF and Raman puntual measurements are being performed at AI-extracted points
    transitions:
      - event: measurements_complete
        target: object_going_first_position
        actions:
          - log_event
          - event: trigger_platform_rotate_to_first

  object_going_first_position:
    description: Rotating platform is moving the object back to the first position
    transitions:
      - event: platform_rotated_to_first
        target: moving_to_box
        actions:
          - log_event
          - event: trigger_robot_first_return

  moving_to_box:
    description: Object is being moved from the disk back to its original position in the box
    transitions:
      - event: object_returned_to_box
        target: object_analyzed
        actions:
          - log_event
          - update_operator_ui
          - event: trigger_cloud_upload

  object_analyzed:
    description: Object analysis is complete – data will be uploaded to the cloud database
```

**Object state machine behavior**:
- Created when the vision system identifies an object (or in simulation)
- Transitions are driven by subsystem completion events
- State is persisted to `backend/object_states/{object_id}.json` after each transition
- State survives orchestrator restarts via disk persistence
- Terminal state (`object_analyzed`) has no transitions
- Each transition is logged with timestamps

**Event actions in object state machines**:
- Each transition can include an `event:` action (e.g., `event: trigger_robot_first_pick`)
- This event is fired AFTER the transition completes, not during action execution
- The orchestrator handles the event and triggers the appropriate subsystem action
- The subsystem action publishes MQTT commands to the hardware
- This keeps MQTT publishing centralized in the orchestrator's subsystem trigger methods

**Event-to-subsystem mapping**:

| Object Event | Subsystem Triggered | MQTT Command |
|---|---|---|
| `trigger_robot_first_pick` | robot_first | `pick_object` |
| `trigger_imaging_capture` | imaging_3di_hsi | `start_capture` |
| `trigger_platform_rotate_to_second` | rotating_platform | `rotate_to_second` |
| `trigger_measurements` | xrf, raman, ai_analysis | `start_measurement` |
| `trigger_platform_rotate_to_first` | rotating_platform | `rotate_to_first` |
| `trigger_robot_first_return` | robot_first | `return_object` |
| `trigger_cloud_upload` | cloud_database | `upload_data` |

### Subsystem state machines (`backend/config/subsystems/*.yaml`)

Each subsystem YAML file defines both subsystem metadata and its state machine.
All subsystems start in an `initializing` state and transition to `ready` when initialization completes.

```yaml
# Subsystem metadata
name: rotating_platform
display_name: Rotating Platform
type: platform
mqtt_topic: "automata/subsystem/{name}"
initial_state: initializing
required: true
metadata:
  description: "Motorized rotary stage with transparent disks"
  hardware: "Motorized rotary stage"

# State machine definition
states:
  initializing:
    description: "Subsystem is initializing"
    transitions:
      - event: init_complete
        target: ready
        actions:
          - log_event
          - publish_mqtt_message: subsystem/rotating_platform/ready

      - event: error
        target: error
        actions:
          - log_event

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

All subsystems start in `initializing` state and transition to `ready` via `init_complete`.

| File | Subsystem | Key states |
|---|---|---|
| `rotating_platform.yaml` | Rotating Platform | initializing → ready → running → paused → stopped → error |
| `vision.yaml` | Vision System | initializing → ready → running → stopped → error |
| `robot_first.yaml` | Robot Arm 1 | initializing → ready → tool_changing → running → paused → stopped → error |
| `robot_second.yaml` | Robot Arm 2 | initializing → ready → running → paused → stopped → error |
| `xrf.yaml` | XRF Analyzer | initializing → ready → measuring → stopped → error |
| `raman.yaml` | Raman Spectrometer | initializing → ready → measuring → stopped → error |
| `imaging_3di_hsi.yaml` | 3DI/HSI Imaging | initializing → ready → capturing → stopped → error |
| `ai_analysis.yaml` | AI Analysis | initializing → ready → analyzing → stopped → error |
| `cloud_database.yaml` | Cloud Database | initializing → ready → uploading → stopped → error |

## How the orchestrator manages state machines

The `WorkflowOrchestrator` coordinates all three levels:

1. **Loads** the workflow state machine from `workflow.yaml`
2. **Loads** each subsystem's YAML from `config/subsystems/*.yaml`
3. **Creates** a `SubsystemAdapter` for each subsystem, each owning its own `StateMachineEngine`
4. **Creates** an `ObjectStateMachines` manager that loads `object.yaml` and creates per-object instances
5. **Routes** incoming MQTT messages to update subsystem states
6. **Monitors** subsystem states to trigger workflow transitions (e.g., all subsystems ready → running)
7. **Drives** subsystem state machines via `fire_subsystem_transition(subsystem_name, event)`
8. **Drives** object state machines via `fire_object_transition(object_id, event)`
9. **Monitors** object completions to update progress counter

### Object state machine lifecycle

```
Vision system identifies object
    ↓
orchestrator.create_object("object_001")
    ↓
ObjectStateMachines creates StateMachineEngine instance
    ↓
State persisted to object_states/object_001.json
    ↓
Subsystem events fire transitions:
    object_picked → object_placed_on_disk → imaging_capture_complete → ...
    ↓
Final state: object_analyzed
    ↓
Progress counter increments
```

### Subsystem state machine transitions

### Subsystem state machine transitions

When a subsystem's state machine transitions, the orchestrator:

1. Fires the event on the subsystem's state machine
2. Publishes the new state via MQTT to `automata/subsystem/{name}/status`
3. Broadcasts the change to the frontend via WebSocket
4. Checks if the transition should affect the main workflow state

### Object state machine transitions

When a subsystem completes its work, the orchestrator fires the corresponding event on the object's state machine:

1. Subsystem transition completes (e.g., 3DI/HSI imaging finished)
2. Orchestrator calls `fire_object_transition(object_id, event)` (e.g., `imaging_capture_complete`)
3. Object state machine transitions to the next state
4. New state is persisted to disk
5. Change is broadcast to the frontend via WebSocket
6. If object reached `object_analyzed`, progress counter increments

### Frontend subsystem cards

The frontend renders a card for each subsystem showing:
- **Display name** (from YAML `display_name`)
- **Required indicator** (*) for required subsystems
- **Current state** (colored badge: ready=green, running=blue, error=red, etc.)
- **State description** (from the state machine YAML)

When a subsystem's state machine transitions, the change is:
1. Published via MQTT to `automata/subsystem/{name}/status`
2. Broadcast to the frontend via WebSocket
3. Displayed in the subsystem card with a state description

### Frontend object cards

The frontend renders a card for each object showing:
- **Object ID** (e.g., `object_001`)
- **Current analysis phase** (color-coded badge)
- **State description** (from the object state machine YAML)
- **Step number** (e.g., "Step 3 / 8")
- **Transition count** (number of completed transitions)

Object states are color-coded:
- `identified_in_box` – gray
- `moving_to_disk`, `moving_to_box` – blue (moving)
- `HSI_3DI_analysis` – purple (imaging)
- `object_going_second_position`, `object_going_first_position` – cyan (rotating)
- `XRF_RAMAN_analysis` – orange (measuring)
- `object_analyzed` – green (complete)

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
8. Object state machines should be independent of both workflow and subsystem state machines.
9. Object states should be persisted to disk for crash recovery.
10. Object transitions are driven by subsystem completions, not by direct operator input.

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
