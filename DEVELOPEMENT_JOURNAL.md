# Development Journal — AUTOMATA GUI Orchestrator

## Current state

A working prototype is complete with integrated frontend, backend, and simulation. The system can be launched with a single command and validated with simulation tests.

### Implemented ✅

**Backend (Python)**
- Declarative state machine engine loaded from YAML config
- Workflow orchestrator with 9 states: idle, starting, running, paused, stopping, stopped, completing, error
- **Declarative subsystem configuration** – `backend/config/subsystems.yaml` defines all subsystems (name, display name, type, MQTT topics, initial state, required flag, metadata)
- Subsystem adapters created dynamically from config – no hardcoded adapter classes
- MQTT client with publish/subscribe and topic routing to configured subsystems
- Domain models (WorkflowState, SubsystemState, WorkflowStatus, EventPayload, SubsystemConfig)
- WebSocket frontend server for operator UI communication (websockets 14.x compatible)
- CLI entry point with `--config`, `--subsystems-config`, `--simulation`, `--num-objects`, `--verbose` flags
- Simulation mode: simulates all configured subsystems responding to operator commands

**Frontend (TypeScript)**
- Dark-themed SPA connected to live backend WebSocket
- State badge showing current workflow state with color coding
- Progress bar showing analysis progress
- **Dynamic subsystem cards** – rendered from backend subsystem config, shows display names and required indicators
- Operator action buttons: start, pause, resume, stop, refresh status, reset
- Event log with timestamped, color-coded entries
- Alias ID assignment input for manual object labeling
- Connection indicator (green/red dot) updates on WebSocket open/close/error

**Unified Launch**
- Single command to start both frontend and backend with optional simulation
- Automatic mosquitto broker management
- Clean shutdown on Ctrl+C

**Testing**
- `backend/simulation_test.py` — comprehensive simulation test suite (36 tests, all passing)
- Tests state machine configuration and transitions
- Tests full workflow simulation via WebSocket GUI commands
- Tests pause/resume/stop workflow states
- Tests MQTT topic publishing and subscription
- Tests error recovery (error → idle transitions)
- Tests subsystem command routing
- Tests subsystem YAML configuration loading

### Architecture

```
┌──────────────────┐     ┌──────────────────────────┐     ┌─────────────────┐
│   Frontend (TS)  │────▶│   Orchestrator (PY)      │────▶│  Subsystems     │
│  • State badge   │     │  • State Machine (YAML)  │     │  • Vision       │
│  • Progress bar  │     │  • Workflow logic        │     │  • Robots       │
│  • Subsystem cards│    │  • MQTT routing          │     │  • Analyzers    │
│  • Event log     │     │  • WebSocket server      │     │  • Cloud DB     │
└──────────────────┘     └──────────────────────────┘     └─────────────────┘
                              ▲
                              │
                         ┌─────────┐
                         │Launch   │
                         │Script   │
                         └─────────┘
```

### Quick Start

```bash
# Launch everything with simulation (default 3 objects)
python launch_gui.py --simulation

# Launch with custom number of objects
python launch_gui.py --simulation --num-objects 5

# Launch without simulation (no auto-subsystem responses)
python launch_gui.py

# Launch with debug logging
python launch_gui.py --simulation --verbose
```

Then open `http://localhost:3000` in a browser.

## Recent changes

### Per-subsystem state machines (2026-07-31)

Major architectural change: each subsystem now has its own YAML file with a declarative state machine definition.

**New subsystem YAML files** (`backend/config/subsystems/*.yaml`):
- `rotating_platform.yaml` – State machine: ready → running → paused → stopped → error
- `vision.yaml` – State machine: ready → running → stopped → error
- `robot_first.yaml` – State machine: ready → tool_changing → running → paused → stopped → error
- `robot_second.yaml` – State machine: ready → running → paused → stopped → error
- `xrf.yaml` – State machine: ready → measuring → stopped → error
- `raman.yaml` – State machine: ready → measuring → stopped → error
- `imaging_3di_hsi.yaml` – State machine: ready → capturing → stopped → error
- `ai_analysis.yaml` – State machine: ready → analyzing → stopped → error
- `cloud_database.yaml` – State machine: ready → uploading → stopped → error

**Backend changes**:
- `StateMachineEngine` now supports custom action handlers registered via `register_action()`
- `SubsystemAdapter` now owns its own `StateMachineEngine` instance
- `SubsystemAdapterFactory` loads per-subsystem YAML files and creates state machines
- `WorkflowOrchestrator` loads subsystem configs from `config/subsystems/*.yaml` (new format) or falls back to legacy `subsystems.yaml`
- Added `fire_subsystem_transition()` method to drive subsystem state machines and broadcast state changes
- Subsystem state changes are published via MQTT and broadcast to frontend

**Frontend changes**:
- Subsystem cards now show current state with state description
- New `subsystem_state_changed` event type for state machine transitions
- CSS styles added for all subsystem states (ready, running, measuring, capturing, analyzing, uploading, paused, stopped, error)

**Simulation changes**:
- Simulation now drives each subsystem's state machine through transitions
- Subsystem state machines transition through states (ready → running → complete → ready) for each object
- Proper state machine transitions for workflow completion (stop_requested → stopped)

**Domain model changes**:
- `SubsystemConfig` now includes `state_machine_path` field
- `SubsystemInfo` now includes `current_state`, `state_description`, and `display_name`
- New `BackendEvent.SUBSYSTEM_STATE_CHANGED` event type

### Simulation test suite rewritten & fixed (2026-07-31)

### Per-object state machines (2026-07-31)

Major architectural change: each analyzed object now has its own YAML-driven state machine instance that tracks the detailed analysis cycle from identification to completion.

**New files**:
- `backend/config/object.yaml` – Blueprint defining the 8-state per-object analysis workflow
- `backend/orchestrator/object_state_machines.py` – `ObjectStateMachines` manager: creates/destroys instances, persists state to JSON files, fires transitions, notifies orchestrator

**Backend changes**:
- `ObjectStateMachines` class manages per-object state machine instances
- Each object gets a `StateMachineEngine` loaded from `object.yaml` blueprint
- Object state is persisted to `backend/object_states/{object_id}.json` (survives restarts)
- `WorkflowOrchestrator` integrates `ObjectStateMachines` with new methods:
  - `create_object(object_id)` – creates an object instance and notifies frontend
  - `remove_object(object_id)` – removes an object instance
  - `fire_object_transition(object_id, event)` – fires a transition on the object's state machine
  - `_on_object_state_change()` – callback for object state changes
  - `_broadcast_objects()` – broadcasts object list to frontend
- Simulation now creates objects and drives their full 8-state analysis pipeline
- New domain models: `ObjectState` enum, `ObjectInfo` dataclass, new `BackendEvent` types

**Frontend changes**:
- `ObjectInfo` interface added to contracts
- New `object_state_changed`, `object_added`, `object_removed` event types
- `UiState` now tracks `objects: ObjectInfo[]`
- `reduceState()` handles object events and updates the object list
- `updateObjectsGrid()` renders object cards with state, step number, and transition count
- CSS styles added for object card states (identified, moving, imaging, rotating, measuring, analyzed)
- New "Object Analysis Progress" panel in the UI

**Simulation changes**:
- Simulation now creates an object for each item to analyze
- Maps subsystem completions to object events (e.g., `imaging_capture_complete` from 3DI/HSI subsystem)
- Drives each object through the full 8-state pipeline
- Tests 8-10 added: config validation, transition testing with persistence, orchestrator integration

**Test results**: 108 tests passing (was 43)

### Object workflow states

The per-object state machine follows this sequence:

```
identified_in_box
    → object_picked → moving_to_disk
    → object_placed_on_disk → HSI_3DI_analysis
    → imaging_capture_complete → object_going_second_position
    → platform_rotated_to_second → XRF_RAMAN_analysis
    → measurements_complete → object_going_first_position
    → platform_rotated_to_first → moving_to_box
    → object_returned_to_box → object_analyzed (final)
```

Events are driven by subsystem completions. For example, when the 3DI/HSI imaging subsystem finishes capturing, it fires `imaging_capture_complete` on the object state machine, advancing it from `HSI_3DI_analysis` to `object_going_second_position`.

### Subsystem configuration via YAML

### Subsystem configuration via YAML

Subsystems are now fully configurable through `backend/config/subsystems.yaml`:

- **Backend**: The orchestrator loads subsystem definitions from YAML and creates adapters dynamically. No hardcoded adapter classes. The `SubsystemConfig` dataclass represents each subsystem's configuration.
- **Frontend**: Subsystem cards are rendered dynamically from the backend config. Display names, types, and required indicators are shown.
- **Simulation**: The simulator reads the subsystem config to know which subsystems to simulate and in what order.
- **MQTT**: The orchestrator subscribes to topics defined in the config.

To add a new subsystem, simply add an entry to `subsystems.yaml`. The system will automatically:
1. Create an adapter for it
2. Subscribe to its MQTT topics
3. Show it in the frontend grid
4. Include it in the simulation

## Next steps

### 1. Extended workflow logic (sub-workflow engine) ✅
Per-object state machines are now implemented. Each object has its own YAML-driven state machine (`backend/config/object.yaml`) with full lifecycle tracking and disk persistence.

### 2. Configuration-driven sub-workflows ✅
The per-object analysis sequence is now fully configurable via `object.yaml`. No code changes needed to modify the pipeline.

### 3. MQTT integration with real subsystems
Wire the orchestrator's MQTT message dispatcher so that real subsystems (via their Docker/ROS2 nodes) can publish status updates on `automata/subsystem/{name}/status` and receive commands on `automata/subsystem/{name}/command`.

### 4. Reliability & recovery
- Subsystem health checks and automatic reconnection
- Retry policies for failed measurements
- Persistent state checkpoint (save/load workflow state on disk)
- Graceful degradation when non-critical subsystems are offline

### 5. CI/CD
- Add automated tests (unit tests for adapters, state machine, MQTT routing)
- Add linting (ruff, mypy for Python; eslint, prettier for TypeScript)
- Add Docker compose setup for orchestrator + mosquitto + frontend

### 6. Enhanced UI (partially done)
- ✅ Add object cards panel showing per-object analysis progress
- Implement alias ID assignment endpoint on the backend
- Add detailed object information panel (expandable object cards)
- Add alarm/notification system for operator warnings
- Add data export functionality
