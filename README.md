# GUI orchestrator for AUTOMATA

This repository contains the GUI orchestrator for the AUTOMATA machine. Its role is to provide the human-facing control interface and coordinate the workflow across the different subsystems.

## Repository scope

This repository is focused on:

- the operator interface,
- workflow orchestration,
- state-machine driven control,
- MQTT communication with external subsystems,
- **configuration-based behavior** – all workflow logic lives in YAML files.

## Broader project context

The wider AUTOMATA project includes robotics, imaging systems, analyzers, AI processing, and cloud data storage. The high-level project context is described in [docs/PROJECT_CONTEXT.md](docs/PROJECT_CONTEXT.md).

## Architecture

```
┌──────────────────┐     ┌──────────────────────────┐     ┌─────────────────┐
│   Frontend (TS)  │────▶│   Orchestrator (PY)      │────▶│  Subsystems     │
│  • State badge   │     │  • State Machine (YAML)  │     │  • Vision       │
│  • Progress bar  │     │  • Config-driven logic   │     │  • Robots       │
│  • Subsystem cards│    │  • MQTT routing          │     │  • Analyzers    │
│  • Event log     │     │  • WebSocket server      │     │  • Cloud DB     │
└──────────────────┘     └──────────────────────────┘     └─────────────────┘
```

The system is built on **configuration-driven** principles:

1. **Declarative state machine** – All workflow behavior is defined in YAML configuration files. States, transitions, events, and actions are externalized and easily extensible.

2. **Declarative subsystem configuration** – Each subsystem has its own YAML file in `backend/config/subsystems/`. Each file defines both subsystem metadata (name, type, required, metadata) and a complete state machine. The orchestrator dynamically creates adapters with per-subsystem state machines.

3. **Declarative per-object sub-workflows** – Each analyzed object has its own YAML-driven state machine (`backend/config/object.yaml`) that tracks the detailed analysis cycle from identification to completion. Object states are persisted to disk.

4. **Declarative event-to-command mapping** – The mapping between object state machine events and subsystem commands is defined in `backend/config/workflow_actions.yaml`. No hardcoded Python methods.

5. **Declarative global defaults** – All default values (MQTT broker, ports, simulation defaults, state colors, messages) are centralized in `backend/config/global.yaml`.

6. **Thin frontend** – The TypeScript UI is purely declarative: it renders state, captures operator actions, and displays logs. All business logic lives in the Python backend, driven by config.

## Configuration files

| File | Purpose |
|---|---|
| `backend/config/workflow.yaml` | Workflow lifecycle states and transitions |
| `backend/config/object.yaml` | Per-object analysis workflow blueprint |
| `backend/config/subsystems/*.yaml` | Subsystem metadata and state machine definitions |
| `backend/config/workflow_actions.yaml` | Maps object events to subsystem commands |
| `backend/config/global.yaml` | Global defaults (MQTT, ports, state colors, messages) |

To add a new analysis step, edit `object.yaml` and `workflow_actions.yaml` — no code changes needed.

## Current status

A working prototype is complete with integrated frontend, backend, and simulation. The system can be launched with a single command and validated with simulation tests.

### Core components

| Component | Status | Description |
|---|---|---|
| State machine engine | ✅ Complete | YAML-driven engine with full transition validation |
| Workflow state machine | ✅ Complete | 7 states with full lifecycle (idle → error → reset) |
| Subsystem config | ✅ Complete | Per-subsystem YAML files with state machine definitions |
| Subsystem adapters | ✅ Dynamic | Adapters created from config — add new subsystems by adding YAML |
| Object state machines | ✅ Complete | Per-object YAML-driven analysis workflows with disk persistence |
| Event-to-command mapping | ✅ Config-driven | `workflow_actions.yaml` replaces hardcoded Python trigger methods |
| Global defaults | ✅ Config-driven | `global.yaml` replaces hardcoded CLI defaults |
| MQTT messaging | ✅ Complete | Publish/subscribe with topic routing to configured subsystems |
| Domain models | ✅ Complete | Typed dataclasses for workflow, subsystems, objects, events |
| Frontend UI | ✅ Complete | Real-time state display, progress, subsystem/object cards, event log |
| WebSocket server | ✅ Complete | Frontend-backend communication |
| Simulation mode | ✅ Config-driven | Simulation derives event chain from `object.yaml` + `workflow_actions.yaml` |
| Unified launch | ✅ Complete | Single command to start frontend + backend + simulation |
| Test suite | ✅ Complete | **123 passing tests** covering all state machines, workflow, MQTT, objects, and integration |

## Subsystem configuration

Each subsystem has its own YAML file in `backend/config/subsystems/`. To adapt the system for a real machine:

1. Add a new YAML file in `backend/config/subsystems/` with subsystem metadata and state machine definition.
2. The orchestrator will automatically:
   - Create an adapter for the new subsystem
   - Subscribe to its MQTT topics
   - Show it in the frontend subsystem grid
   - Include it in the simulation (if simulation mode is enabled)

3. To remove a subsystem, simply delete its YAML file.

### Subsystem YAML structure

Each subsystem YAML file defines both metadata and a state machine.

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

Object state machine transitions fire internal events that the orchestrator handles by triggering subsystem actions. This mapping is defined in `backend/config/workflow_actions.yaml`:

| Object Event | Subsystem(s) | Command(s) |
|---|---|---|
| `trigger_robot_first_pick` | robot_first | `pick_object` |
| `trigger_imaging_capture` | imaging_3di_hsi | `start_capture` |
| `trigger_platform_rotate_to_second` | rotating_platform | `rotate_to_second` |
| `trigger_measurements` | xrf, raman, ai_analysis | `start_measurement`, `start_measurement`, `plan_measurements` |
| `trigger_platform_rotate_to_first` | rotating_platform | `rotate_to_first` |
| `trigger_robot_first_return` | robot_first | `return_object` |
| `trigger_cloud_upload` | cloud_database | `upload_data` |

To add a new analysis step, add an entry to `workflow_actions.yaml`.

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

# Start orchestrator (loads defaults from global.yaml)
python -m backend.orchestrator.main

# With simulation mode
python -m backend.orchestrator.main --simulation --num-objects 3
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

### Testing

```bash
# Run simulation tests (123 tests, validates full workflow)
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
- Config-driven event-to-command mapping
- Config-driven simulation event chain

## Development

- [AGENTS.md](AGENTS.md) — agent instructions for this repo
- [docs/GUI_ARCHITECTURE.md](docs/GUI_ARCHITECTURE.md) — recommended software architecture
- [docs/STATE_MACHINE.md](docs/STATE_MACHINE.md) — state machine design principles
- [docs/FRONTEND_BACKEND_BOUNDARY.md](docs/FRONTEND_BACKEND_BOUNDARY.md) — frontend/backend communication boundary
- [DEVELOPEMENT_JOURNAL.md](DEVELOPEMENT_JOURNAL.md) — development log and next steps
