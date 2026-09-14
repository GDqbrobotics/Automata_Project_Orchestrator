# AUTOMATA GUI Orchestrator

The AUTOMATA GUI Orchestrator is the operator-facing control system for the AUTOMATA analytical machine. It manages workflow orchestration, object tracking, and subsystem communication through a clean, layered state-machine architecture.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  Frontend (TypeScript/Vite)                                      │
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
│   └── pipeline.py           # Pipeline controller (object flow through the 2-disk platform)
├── services/
│   └── frontend_server.py    # WebSocket server for frontend
├── simulation/               # Simulated subsystems: one process each, MQTT-only
│   ├── subsystem_sim.py      # Generic simulated subsystem (same YAML as the real one)
│   ├── robot_first_sim.py
│   ├── rotating_platform_sim.py
│   ├── vision_sim.py
│   ├── imaging_3di_hsi_sim.py
│   ├── xrf_sim.py
│   ├── raman_sim.py
│   ├── ai_analysis_sim.py
│   ├── robot_second_sim.py
│   └── cloud_database_sim.py
├── state/
│   └── state_machine.py      # MQTT-agnostic state machine engine
└── simulation_test.py        # End-to-end test suite
frontend/                       # TypeScript frontend (Vite, no framework)
├── index.html                  # App shell (structure only, no CSS/JS)
└── src/
    ├── index.ts                # Entry: wires App to DOM buttons
    ├── contracts.ts            # Typed message contracts (mirror backend)
    ├── api/client.ts           # WebSocket client
    ├── state/store.ts          # Reducer: WS events → UI state
    ├── config/                 # Button rules, object styles, state messages
    ├── styles/app.css          # All UI styles (design tokens + layout)
    └── ui/                     # App shell, SM graph, inspector modal
launch_gui.py                   # Unified launcher
```

## Simulation mode

In simulation mode each subsystem is simulated as an **individual process**
(`backend/simulation/<name>_sim.py`) that loads the *same* YAML state machine
the orchestrator uses and communicates with it **exclusively over MQTT** –
identical to how the real hardware subsystems will behave:

- commands flow `orchestrator → automata/subsystem/<name>/event`
  (published by the subsystem adapter on every state-machine transition),
- status reports flow `automata/subsystem/<name>/status → orchestrator`
  (on every state change + heartbeat),
- the vision system reports the detected box objects on
  `automata/subsystem/vision/response`.

The orchestrator treats simulated and real subsystems the same way; the
`PipelineController` drives the object flow (2 disk slots, 1 end effector,
coupled platform rotations) purely from the status reports. Work durations
are declared in each subsystem YAML (`metadata.work_durations`).

```bash
python launch_gui.py --simulation --num-objects 5   # starts broker + 9 sim processes + backend + frontend
```

## Debug mode (manual MQTT injection)

```bash
python launch_gui.py --debug
```

With `--debug`, every **subsystem inspector window** (click a subsystem card)
shows an extra **debug panel**: a selector with the list of MQTT topics the
real subsystem is expected to publish — `automata/subsystem/<name>/status`
(state reports) and `automata/subsystem/<name>/response` (e.g. the vision
object list) — plus an editable JSON payload pre-filled with a sensible
default. For the `/status` topic a state quick-pick (from the subsystem's
YAML states) fills the payload's `state`/`ready` fields. Pressing **Send**
publishes the message on that topic **as if the real subsystem sent it**:
the orchestrator processes it through the exact same receive path as live
MQTT traffic (subsystem state sync, pipeline reactions, and the window's
message feed shows it as `← received`).

It is a simulation you drive by hand: useful to test the orchestrator
against the expected subsystem behaviour without the real hardware (or to
replay a specific message, e.g. a different `objects_detected` list).
The backend rejects the command unless it was started with `--debug` and
only accepts the subsystem's outgoing topics.

## State Machine Layers

### 1. Workflow State Machine (`workflow.yaml`)

Manages the overall workflow lifecycle:
- `idle` → `starting` → `running` → `paused` / `stopping` → `stopped` / `error`

Events:
- `start_requested`, `pause_requested`, `resume_requested`, `stop_requested`
- `subsystem_ready`, `subsystem_error`, `reset_requested`
- Scoped actions: `all/stop`, `all/pause`, `all/resume`

### 2. Object State Machine (`object.yaml`)

Manages per-object analysis workflow (imaging on the second disk position,
XRF/Raman on the first one – robot 2 scans, robot 1 measures):
- `identified_in_box` → `waiting_for_disk` → `moving_to_disk` → `object_going_second_position` → `HSI_3DI_analysis` → `object_going_first_position` → `XRF_RAMAN_analysis` → ... → `object_analyzed`

Objects wait in `waiting_for_disk` (still in the box) until a disk slot is
free – the platform holds at most 2 objects and robot 1 carries at most 1.

Events are scoped to target specific subsystems:
- `robot_first/pick` → triggers robot_first subsystem
- `workflow/rotation_requested_second` → object placed on the first disk: the pipeline requests the rotation to the imaging position
- `imaging_3di_hsi/capture` + `robot_second/scan_hsi_3d` → object on the second disk: imaging starts
- `ai_analysis/plan` + `workflow/rotation_requested_first` → imaging done: the AI plans the points and the rotation back is requested
- `robot_first/measure_xrf_raman` + `xrf/measure` + `raman/measure` → object on the first disk: XRF/Raman start
- `robot_first/return` → measurements done: robot 1 returns the object to the box
- `cloud_database/upload` → triggers cloud upload

### 3. Subsystem State Machines (per-subsystem YAML files)

Each subsystem has its own state machine with states like:
- `initializing` → `ready` → `measuring`/`capturing`/`picking` → `ready`
- Error recovery: `error` → `reset` → `ready`

**Important**: Subsystem SMs only use `log_event` actions. MQTT publishing is handled automatically by the orchestrator's boundary layer callback when events are fired.

## State Machine Inspector (GUI)

Click any **subsystem card** or **object card** to open an inspector window
with:

- a **graphical view of the state machine** — rendered from the same YAML
  the backend engines run on, with the **current state enlightened**
  (hover a state to highlight its transitions),
- a **live message feed**:
  - subsystems → MQTT messages **sent** to the subsystem (commands on
    `automata/subsystem/<name>/event`) and **received** from it (status
    reports / responses), with repeated heartbeats collapsed into a ×N badge;
  - objects → **internal messages**: every state transition and the scoped
    events it fires (targeting a specific subsystem, the internal pipeline,
    or all subsystems).

The state machine definitions and message traffic are streamed over the
existing WebSocket (events `subsystem_message`, `object_message` and
`messages_cleared`); the definitions travel inside every status broadcast,
so the view always matches the actual machine.

## Alias IDs (cloud database)

From the **control deck** the operator assigns an alias ID to any tracked
object (object selector + alias + Assign). The machine keeps working with
the auto-assigned ID in the meantime; the alias is the ID the cloud database
uses for the object's record and is attached to the `cloud_database/upload`
command when the object's analysis completes (`{object_id, alias}`).
Assigned aliases are shown on the object cards. The backend validates the
alias (format + uniqueness) and reports errors in the event log.

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
  object_going_second_position:
    transitions:
      - event: platform_rotated_to_second
        target: HSI_3DI_analysis
        actions:
          - log_event
          - event: imaging_3di_hsi/capture          # Scoped event
          - event: robot_second/scan_hsi_3d         # Scoped event
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

### Debug Mode (manual MQTT injection from the subsystem windows)
```bash
python launch_gui.py --debug
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
