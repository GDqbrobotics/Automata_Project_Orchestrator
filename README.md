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

The system is built on three pillars:

1. **Declarative state machine** – All workflow behavior is defined in a YAML configuration file (`backend/config/workflow.yaml`). States, transitions, events, and actions are externalized and easily extensible.

2. **Declarative subsystem configuration** – Each subsystem has its own YAML file in `backend/config/subsystems/` (e.g., `rotating_platform.yaml`, `vision.yaml`). Each file defines both subsystem metadata (name, type, required, metadata) and a complete state machine (states, transitions, events, actions). The orchestrator dynamically creates adapters with per-subsystem state machines and the frontend renders cards showing current subsystem state.

3. **MQTT communication** – The orchestrator communicates with all external subsystems (vision, robots, analyzers, AI, cloud) via MQTT topics. Subsystem adapters wrap each hardware/software component with a consistent API.

4. **Thin frontend** – The TypeScript UI is purely declarative: it renders state, captures operator actions, and displays logs. All business logic lives in the Python backend.

## Current status

A working prototype is complete with integrated frontend, backend, and simulation. The system can be launched with a single command and validated with simulation tests.

### Core components

| Component | Status | Description |
|---|---|---|
| State machine | ✅ Complete | YAML-driven engine with 7 states, full transition validation |
| Subsystem config | ✅ Complete | Per-subsystem YAML files with state machine definitions |
| Subsystem state machines | ✅ Complete | Each subsystem has its own state machine (ready, running, error, etc.) |
| Subsystem adapters | ✅ Dynamic | Adapters with per-subsystem state machines – add new subsystems by adding YAML |
| Workflow | ✅ Complete | Orchestrator with start/pause/resume/stop/error/recovery |
| MQTT messaging | ✅ Complete | Publish/subscribe with topic routing to configured subsystems |
| Domain models | ✅ Complete | Typed dataclasses for workflow, subsystems, events |
| Frontend UI | ✅ Complete | Real-time state display, progress bar, dynamic subsystem cards with state descriptions, event log |
| WebSocket server | ✅ Complete | Frontend-backend communication |
| Simulation mode | ✅ Complete | Simulates all configured subsystems responding to GUI commands |
| Unified launch | ✅ Complete | Single command to start frontend + backend + simulation |
| Test suite | ✅ Complete | 43 passing tests: state machine, workflow, MQTT, error recovery, subsystem state machines |

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
# Run simulation tests (43 tests, validates full workflow)
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

## Development

- [AGENTS.md](AGENTS.md) — agent instructions for this repo
- [docs/GUI_ARCHITECTURE.md](docs/GUI_ARCHITECTURE.md) — recommended software architecture
- [docs/STATE_MACHINE.md](docs/STATE_MACHINE.md) — state machine specification
- [docs/FRONTEND_BACKEND_BOUNDARY.md](docs/FRONTEND_BACKEND_BOUNDARY.md) — frontend/backend communication boundary
- [DEVELOPEMENT_JOURNAL.md](DEVELOPEMENT_JOURNAL.md) — development log and next steps
