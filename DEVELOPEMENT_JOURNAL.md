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

### Simulation test suite rewritten & fixed (2026-07-31)

The `backend/simulation_test.py` was completely rewritten to work with the current codebase:

- **Fixed orchestrator initialization**: Updated to use new `WorkflowOrchestrator` constructor signature (now requires `subsystems_config_path` parameter)
- **Fixed state machine event name**: Changed `subsystems_ready` → `subsystem_ready` in the orchestrator to match the YAML config (`backend/config/workflow.yaml`)
- **Fixed adapter state access**: Changed `a.info.state` → `a.state` in `update_all_subsystems_ready()` and `_update_subsystem_status()` — `info` property returns `SubsystemInfo` (no `state` attribute), not the adapter itself
- **Fixed simulation completion transitions**: Changed `_simulate_completion()` to use proper state machine transitions (`stop_requested` → `stopped`) instead of directly calling `_update_state()`
- **Fixed MQTT disconnect logging**: Added `ReasonCode` enum handling for paho-mqtt 2.x compatibility
- **Fixed frontend server shutdown**: Added `reuse_port=True` to allow port reuse after shutdown
- **Test suite now passes**: All 36 tests pass (state machine config, full workflow simulation, pause/resume, MQTT publishing, error recovery, command routing, subsystem config)

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

### 1. Extended workflow logic (sub-workflow engine)
The current state machine handles high-level workflow states. The next layer is the **sub-workflow** engine that coordinates the detailed analysis cycle per object:
- 3DI/HSI imaging acquisition
- AI interest point extraction
- XRF/Raman puntual measurements at extracted points
- Cloud data upload per object

### 2. Configuration-driven sub-workflows
Move the per-object analysis sequence (pick → 3DI → HSI → AI → rotate → XRF → Raman → rotate → return) into a separate YAML configuration, making the full pipeline configurable without code changes.

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

### 6. Enhanced UI
- Implement alias ID assignment endpoint on the backend
- Add detailed object information panel
- Add alarm/notification system for operator warnings
- Add data export functionality
