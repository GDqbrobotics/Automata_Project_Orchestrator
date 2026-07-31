# GUI orchestrator for AUTOMATA

This repository contains the GUI orchestrator for the AUTOMATA machine. Its role is to provide the human-facing control interface and coordinate the workflow across the different subsystems.

## Repository scope

This repository is focused on:

- the operator interface,
- workflow orchestration,
- state-machine driven control,
- MQTT communication with external subsystems,
- configuration-based behavior for machine operations.

## Broader project context

The wider AUTOMATA project includes robotics, imaging systems, analyzers, AI processing, and cloud data storage. The high-level project context is described in [docs/PROJECT_CONTEXT.md](docs/PROJECT_CONTEXT.md).

## Architecture

```
┌──────────────────┐     ┌──────────────────────────┐     ┌─────────────────┐
│   Frontend (TS)  │────▶│   Orchestrator (PY)      │────▶│  Subsystems     │
│  • State badge   │     │  • State Machine (YAML)  │     │  • Vision       │
│  • Progress bar  │     │  • Workflow logic        │     │  • Robots       │
│  • Subsystem cards│    │  • MQTT routing          │     │  • Analyzers    │
│  • Event log     │     │  • WebSocket server      │     │  • Cloud DB     │
└──────────────────┘     └──────────────────────────┘     └─────────────────┘
```

The system is built on four pillars:

1. **Declarative state machine** – All workflow behavior is defined in a YAML configuration file (`backend/config/workflow.yaml`). States, transitions, events, and actions are externalized and easily extensible.

2. **Declarative subsystem configuration** – Each subsystem has its own YAML file in `backend/config/subsystems/` (e.g., `rotating_platform.yaml`, `vision.yaml`). Each file defines both subsystem metadata (name, type, required, metadata) and a complete state machine (states, transitions, events, actions). The orchestrator dynamically creates adapters with per-subsystem state machines and the frontend renders cards showing current subsystem state.

3. **Declarative per-object sub-workflows** – Each analyzed object has its own YAML-driven state machine (`backend/config/object.yaml`) that tracks the detailed analysis cycle from identification to completion. Object states are persisted to disk and the frontend renders cards showing per-object progress.

4. **MQTT communication** – The orchestrator communicates with all external subsystems (vision, robots, analyzers, AI, cloud) via MQTT topics. Subsystem adapters wrap each hardware/software component with a consistent API.

5. **Thin frontend** – The TypeScript UI is purely declarative: it renders state, captures operator actions, and displays logs. All business logic lives in the Python backend.

## Current status

A working prototype is complete with integrated frontend, backend, and simulation. The system can be launched with a single command and validated with simulation tests.

### Core components

| Component | Status | Description |
|---|---|---|
| State machine | ✅ Complete | YAML-driven engine with 7 states, full transition validation |
| Subsystem config | ✅ Complete | Per-subsystem YAML files with state machine definitions |
| Subsystem state machines | ✅ Complete | Each subsystem has its own state machine (ready, running, error, etc.) |
| Subsystem adapters | ✅ Dynamic | Adapters with per-subsystem state machines – add new subsystems by adding YAML |
| Object state machines | ✅ Complete | Per-object YAML-driven analysis workflows with disk persistence |
| Workflow | ✅ Complete | Orchestrator with start/pause/resume/stop/error/recovery |
| MQTT messaging | ✅ Complete | Publish/subscribe with topic routing to configured subsystems |
| Domain models | ✅ Complete | Typed dataclasses for workflow, subsystems, objects, events |
| Frontend UI | ✅ Complete | Real-time state display, progress bar, subsystem cards, object cards, event log |
| WebSocket server | ✅ Complete | Frontend-backend communication |
| Simulation mode | ✅ Complete | Simulates all configured subsystems and object analysis pipelines |
| Unified launch | ✅ Complete | Single command to start frontend + backend + simulation |
| Test suite | ✅ Complete | 108 passing tests covering all state machines, workflow, MQTT, objects, and integration |

## Subsystem configuration

Each subsystem has its own YAML file in `backend/config/subsystems/`. To adapt the system for a real machine:

1. Edit `subsystems.yaml` to match your hardware:
   - Change `display_name` for human-readable labels
   - Set `type` to categorize subsystems (camera, robot, analyzer, etc.)
   - Set `required: false` for optional subsystems
   - Add hardware metadata in the `metadata` section

2. To add a new subsystem:
   ```yaml
   - name: my_new_subsystem
     display_name: My New Subsystem
     type: custom
     mqtt_topic: "automata/subsystem/{name}"
     initial_state: ready
     required: false
     metadata:
       description: "Description of your subsystem"
   ```

3. The orchestrator will automatically:
   - Create an adapter for the new subsystem
   - Subscribe to its MQTT topics
   - Show it in the frontend subsystem grid
   - Include it in the simulation (if simulation mode is enabled)

4. To remove a subsystem, simply delete its YAML file.

### Subsystem YAML structure

Each subsystem YAML file defines both metadata and a state machine:

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
      - event: error
        target: error
        actions:
          - log_event

  # ... more states (paused, stopped, error) ...
```

**Supported actions**:
- `log_event` – Log the transition event
- `update_operator_ui` – Notify UI update
- `publish_mqtt_message: <topic>` – Publish to an MQTT topic

Custom actions can be registered via `register_action()` in the `StateMachineEngine`.

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

### Object configuration

Each analyzed object follows a declarative sub-workflow defined in `backend/config/object.yaml`. When the vision system identifies an object (or in simulation), the orchestrator creates a state machine instance for it.

**Design principle**: Object state machines do NOT publish MQTT messages. MQTT publishing is reserved for subsystem state machines only. Interactions between state machine levels are internal and driven by `event` actions in the YAML.

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

**Event-driven flow** (internal, no MQTT from object state machines):

```
1. Vision identifies object → orchestrator fires object_picked transition
2. Object transitions to moving_to_disk → fires event: trigger_robot_first_pick
3. Orchestrator handles event → publishes MQTT to robot_first (command: pick_object)
4. Robot completes → orchestrator fires object_placed_on_disk transition
5. Object transitions to HSI_3DI_analysis → fires event: trigger_imaging_capture
6. Orchestrator handles event → publishes MQTT to imaging_3di_hsi (command: start_capture)
7. 3DI/HSI completes → orchestrator fires imaging_capture_complete transition
... (continues until object_analyzed)
```

**Object lifecycle**:
1. Vision system identifies an object → `orchestrator.create_object(object_id)`
2. Subsystem events trigger object transitions (e.g., `object_placed_on_disk`)
3. Each transition is persisted to `backend/object_states/{object_id}.json`
4. Object reaches `object_analyzed` → progress counter increments
5. State survives orchestrator restarts via disk persistence

**Frontend object cards** show:
- **Object ID**
- **Current analysis phase** (color-coded badge)
- **State description**
- **Step number** (e.g., "Step 3 / 8")
- **Transition count**

### Object-to-subsystem event mapping

Object state machine transitions fire internal events that the orchestrator handles by triggering subsystem actions:

| Object Transition | Event Fired | Subsystem Triggered | Command |
|---|---|---|---|
| identified_in_box → moving_to_disk | `trigger_robot_first_pick` | robot_first | pick_object |
| moving_to_disk → HSI_3DI_analysis | `trigger_imaging_capture` | imaging_3di_hsi | start_capture |
| HSI_3DI_analysis → object_going_second_position | `trigger_platform_rotate_to_second` | rotating_platform | rotate_to_second |
| object_going_second_position → XRF_RAMAN_analysis | `trigger_measurements` | xrf, raman, ai_analysis | start_measurement |
| XRF_RAMAN_analysis → object_going_first_position | `trigger_platform_rotate_to_first` | rotating_platform | rotate_to_first |
| object_going_first_position → moving_to_box | `trigger_robot_first_return` | robot_first | return_object |
| moving_to_box → object_analyzed | `trigger_cloud_upload` | cloud_database | upload_data |
|---|---|
| Vision system | `object_picked` |
| Robot arm 1 (pick & place) | `object_picked`, `object_placed_on_disk` |
| 3DI/HSI imaging | `imaging_capture_complete` |
| Rotating platform (to second) | `platform_rotated_to_second` |
| XRF + Raman + AI analysis | `measurements_complete` |
| Rotating platform (to first) | `platform_rotated_to_first` |
| Robot arm 1 (return) | `object_returned_to_box` |
| Cloud database (upload) | `measurements_complete` (final) |

## How to run

### Prerequisites

- Python 3.10+
- Node.js 18+
- Mosquitto MQTT broker (installed automatically)

### Quick start

```bash
# Launch everything with simulation (default 3 objects)
python launch_gui.py --simulation

# Launch with custom number of objects
python launch_gui.py --simulation --num-objects 5

# Launch without simulation (no auto-subsystem responses)
python launch_gui.py
```

Then open `http://localhost:3000` in a browser.

### Backend

```bash
cd backend
pip install -r requirements.txt

# Start orchestrator
python -m backend.orchestrator.main --broker localhost --port 8080

# With simulation mode
python -m backend.orchestrator.main --broker localhost --port 8080 --simulation --num-objects 3
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

### Testing

```bash
# Run simulation tests (108 tests, validates full workflow)
cd backend
python simulation_test.py

# Test with custom number of objects
python simulation_test.py --num-objects 5
```

**Test coverage**:
- State machine configuration and transitions
- Full workflow simulation via WebSocket GUI commands
- Pause/resume/stop workflow states
- MQTT topic publishing and subscription
- Error recovery (error → idle transitions)
- Subsystem state machine transitions
- Subsystem YAML configuration loading
- Object state machine configuration and transitions
- Object state persistence to disk
- Object orchestrator integration

## Development

- [AGENTS.md](AGENTS.md) — agent instructions for this repo
- [docs/GUI_ARCHITECTURE.md](docs/GUI_ARCHITECTURE.md) — recommended software architecture
- [docs/STATE_MACHINE.md](docs/STATE_MACHINE.md) — state machine specification
- [docs/FRONTEND_BACKEND_BOUNDARY.md](docs/FRONTEND_BACKEND_BOUNDARY.md) — frontend/backend communication boundary
- [DEVELOPEMENT_JOURNAL.md](DEVELOPEMENT_JOURNAL.md) — development log and next steps
