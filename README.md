# AUTOMATA GUI Orchestrator

The AUTOMATA GUI Orchestrator is the operator-facing control system for the AUTOMATA analytical machine. It manages workflow orchestration, object tracking, and subsystem communication through a clean, layered state-machine architecture.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  Frontend (TypeScript/React)                                     │
│  - Operator-facing UI                                            │
│  - WebSocket communication                                       │
└──────────────────────────────┬──────────────────────────────────┘
                               │ WebSocket
┌──────────────────────────────┴──────────────────────────────────┐
│  Orchestrator (Python) – Internal Event Bus                      │
│                                                                  │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────────┐  │
│  │  Workflow SM │───▶│  Object SMs  │───▶│  Internal Events  │  │
│  │  (workflow)  │    │  (per-object)│    │  "robot_first/pick"│  │
│  └──────────────┘    └──────────────┘    └──────────────────┘  │
│                                                                  │
│  Event Routing:                                                  │
│    - Scoped events: "robot_first/pick" → routed to robot_first   │
│    - Unscoped events: "error" → broadcast to all subsystems      │
└──────────────────────────┬──────────────────────────────────────┘
                           │ Internal Events
              ┌────────────┴────────────┐
              │  Subsystem Adapters      │
              │  ┌──────────────────┐   │
              │  │ Subsystem SM 1   │   │  ← BOUNDARY LAYER
              │  │ (robot_first)    │───┼──→ MQTT → Real Robot
              │  └──────────────────┘   │
              │  ┌──────────────────┐   │
              │  │ Subsystem SM 2   │   │
              │  │ (raman)          │───┼──→ MQTT → Real Raman
              │  └──────────────────┘   │
              └─────────────────────────┘
                           │ MQTT
              ┌────────────┴────────────┐
              │  Real Hardware Subsystems │
              │  (Robots, Analyzers, etc.)│
              └───────────────────────────┘
```

### Key Design Principles

1. **Internal Communication Only**: Workflow, Object, and Subsystem state machines interact ONLY through internal events. No MQTT between them.

2. **Subsystem SMs are the Boundary**: Only subsystem state machines publish MQTT messages to real hardware. They are the sole interface between the internal event system and external devices.

3. **Scoped Events**: Events can be scoped to target specific subsystems:
   - `event: measure_request` → internal event, handled by the engine
   - `event: robot_first/pick` → routed to the robot_first subsystem's state machine

4. **Scoped Actions**: Actions can target specific subsystems:
   - `action: error` → broadcast to all subsystems
   - `action: raman/measure_request` → send to raman subsystem only

5. **Configuration-Driven**: All state machines are defined in YAML files. Behavior changes by editing configs, not code.

## Directory Structure

```
backend/
├── adapters/
│   └── subsystem.py          # Subsystem adapter factory & classes
├── config/
│   ├── global.yaml           # Global config (MQTT, ports, simulation)
│   ├── workflow.yaml         # Workflow state machine definition
│   ├── object.yaml           # Object analysis state machine definition
│   └── subsystems/           # Per-subsystem YAML configs
│       ├── robot_first.yaml
│       ├── vision.yaml
│       ├── rotating_platform.yaml
│       ├── xrf.yaml
│       ├── raman.yaml
│       ├── imaging_3di_hsi.yaml
│       ├── ai_analysis.yaml
│       ├── cloud_database.yaml
│       └── robot_second.yaml
├── domain/
│   └── models.py             # Domain models (WorkflowState, SubsystemState, etc.)
├── messaging/
│   └── mqtt_client.py        # MQTT client wrapper
├── orchestrator/
│   ├── workflow.py           # Main orchestrator (event routing, command handling)
│   ├── object_state_machines.py  # Per-object state machine manager
│   └── simulation.py         # Simulation mode
├── services/
│   └── frontend_server.py    # WebSocket server for frontend
├── state/
│   └── state_machine.py      # MQTT-agnostic state machine engine
└── simulation_test.py        # End-to-end test suite
frontend/                       # TypeScript/React frontend
launch_gui.py                   # Unified launcher
```

## State Machine Layers

### 1. Workflow State Machine (`workflow.yaml`)

Manages the overall workflow lifecycle:
- `idle` → `starting` → `running` → `paused` / `stopping` → `stopped` / `error`

Events:
- `start_requested`, `pause_requested`, `resume_requested`, `stop_requested`
- `subsystem_ready`, `subsystem_error`, `reset_requested`
- Scoped actions: `all/stop`, `all/pause`, `all/resume`

### 2. Object State Machine (`object.yaml`)

Manages per-object analysis workflow:
- `identified_in_box` → `moving_to_disk` → `HSI_3DI_analysis` → ... → `object_analyzed`

Events are scoped to target specific subsystems:
- `robot_first/pick` → triggers robot_first subsystem
- `imaging_3di_hsi/capture` → triggers imaging subsystem
- `xrf/measure` → triggers XRF subsystem
- `raman/measure` → triggers Raman subsystem
- `cloud_database/upload` → triggers cloud upload

### 3. Subsystem State Machines (per-subsystem YAML files)

Each subsystem has its own state machine with states like:
- `initializing` → `ready` → `measuring`/`capturing`/`picking` → `ready`
- Error recovery: `error` → `reset` → `ready`

**Important**: Subsystem SMs only use `log_event` actions. MQTT publishing is handled automatically by the orchestrator's boundary layer callback when events are fired.

## Configuration Files

### workflow.yaml
```yaml
states:
  idle:
    transitions:
      - event: start_requested
        target: starting
        actions:
          - log_event
  running:
    transitions:
      - event: pause_requested
        target: paused
        actions:
          - event: all/pause    # Broadcast to all subsystems
```

### object.yaml
```yaml
states:
  HSI_3DI_analysis:
    transitions:
      - event: imaging_capture_complete
        target: object_going_second_position
        actions:
          - log_event
          - event: rotating_platform/rotate_to_second  # Scoped event
```

### subsystem/robot_first.yaml
```yaml
states:
  ready:
    transitions:
      - event: pick
        target: picking
        actions:
          - log_event
  picking:
    transitions:
      - event: complete
        target: ready
        actions:
          - log_event
```

## Running

### Normal Mode
```bash
python launch_gui.py
```

### Simulation Mode
```bash
python launch_gui.py --simulation --num-objects 5
```

### With Custom Broker
```bash
python launch_gui.py --broker 192.168.1.100 --simulation
```

### Verbose Logging
```bash
python launch_gui.py --simulation --verbose
```

### Run Tests
```bash
python backend/simulation_test.py
python backend/simulation_test.py --num-objects 5
```

## Event Scoping Reference

| Format | Example | Meaning |
|--------|---------|---------|
| Unscoped | `error` | Broadcast to all subsystems |
| Scoped | `robot_first/pick` | Route to robot_first subsystem |
| Scoped | `xrf/measure` | Route to xrf subsystem |
| Scoped | `all/stop` | Broadcast to all subsystems (alias) |

## MQTT Topics

Subsystem SMs are the only layer that publishes MQTT. Topics follow the pattern:
- `automata/subsystem/{name}/status` - Subsystem status updates
- `automata/subsystem/{name}/event` - Events fired by subsystem state machines
- `automata/subsystem/{name}/command` - Commands to subsystems
- `automata/subsystem/{name}/response` - Responses from subsystems

## Adding a New Subsystem

1. Create a YAML file in `backend/config/subsystems/` with the subsystem's name
2. Define the state machine with appropriate states and transitions
3. Use scoped events in `workflow.yaml` and `object.yaml` to communicate with it
4. The subsystem will be automatically discovered and its state machine loaded
