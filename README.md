# AUTOMATA GUI Orchestrator

AUTOMATA is a machine that automatically analyzes archaeological samples:
3D imaging (3DI), hyperspectral imaging (HSI), XRF and Raman measurements,
used to build digital twins of pottery pieces and lithics.

This repository is the **operator-facing brain** of the machine: a
touchscreen GUI plus a Python orchestrator that coordinates all the
subsystems (robots, analyzers, cameras, rotating platform, AI) and drives
the analysis workflow. It never talks to hardware directly — every
subsystem is a black box speaking **MQTT**.

> For the full picture of the machine, the physical layout and the
> intended workflow, read [docs/PROJECT_CONTEXT.md](docs/PROJECT_CONTEXT.md).

## How it works

The whole system is modeled as **state machines defined in YAML**, in
three levels:

1. **Workflow machine** — the life of a run: *idle → starting → running →
   paused / stopping → stopped / error*. Driven by the operator (Start,
   Pause, Stop, Reset) and by subsystem readiness or errors.
2. **Object machines** — one instance per sample, all running the same
   blueprint: the journey from the box, through imaging, AI analysis,
   measurements and back to the box. Each object's state and full
   transition history are saved to disk after every step.
3. **Subsystem machines** — one per component (each robot, each analyzer,
   the platform, the cameras, the AI, the imaging modules): their own
   *ready / working / error* states.

Only the subsystem machines touch MQTT; they are the **boundary** between
the orchestrator and the outside world. The workflow and object machines
communicate exclusively through **internal events**, some of which are
*scoped* to a target: an event written `imaging_3di/capture` goes only to
the 3DI subsystem, `all/stop` goes to everyone. When a subsystem machine
fires a transition, the orchestrator publishes the command to
`automata/subsystem/<name>/event`; the subsystem answers with status
reports on `automata/subsystem/<name>/status`.

On top of the state machines, a **pipeline controller** keeps track of the
physical flow: the rotating platform has two disk positions and robot 1
has a single end effector, so at most two objects are off the box at any
time. The controller listens to the subsystem status reports and fires the
object transitions (pick, place, rotate, image, measure) only when it is
physically safe to do so.

```
┌──────────────────┐  WebSocket  ┌─────────────────────────────────────────┐
│  Touchscreen GUI │◄───────────►│  Python orchestrator                    │
│  (TypeScript)    │             │  workflow SM · object SMs · pipeline    │
└──────────────────┘             │  subsystem SMs ── the MQTT boundary     │
                                 └──────────────────┬──────────────────────┘
                                                    │ MQTT
                     ┌──────────────┬───────────────┼───────────────┬───────────┐
                     ▼              ▼               ▼               ▼           ▼
                 robot 1        robot 2        rotating        analyzers   vision / AI
```

### The object's journey

1. The operator loads a box full of samples and presses **Start**.
2. The camera identifies the objects, assigns them IDs and an analysis order.
3. Robot 1 moves the first object onto a disk; the platform rotates it to
   the imaging position.
4. Robot 2 performs **3DI first, then HSI** — two sequential stages, two
   separate imaging subsystems (the AI needs both datasets).
5. While the platform rotates the object back, the AI extracts the points
   of interest from the imaging data.
6. Robot 1 performs XRF and Raman measurements at those points.
7. The object goes back into the box and the next one starts. Thanks to the
   two disks and two robots, the machine works on up to two objects at a
   time.
8. All data is **stored locally**; the local database is updated manually.

During the run the operator can assign each object an **alias ID**
(operator-friendly name, validated for format and uniqueness). The machine
keeps working with the auto-generated ID, while the alias is the ID the
object's record gets in the local database.

## Requirements

| Component | Version | Why |
|---|---|---|
| Python | ≥ 3.10 | the orchestrator |
| Python packages | `paho-mqtt`, `PyYAML`, `websockets` (see `requirements.txt`) | MQTT client, YAML configs, frontend WebSocket server |
| mosquitto | any recent release (2.x tested) | the MQTT broker — the launcher starts it automatically, but the binary must be installed |
| Node.js + npm | ≥ 18 | the frontend development server |
| Ports | 1883 (MQTT), 8080 (backend WebSocket), 3000 (frontend) | defaults, all overridable via CLI flags |

## Installation

```bash
# 1. Get the code
git clone https://github.com/GDqbrobotics/Automata_Project_Orchestrator.git
cd Automata_Project_Orchestrator

# 2. Python environment (the orchestrator)
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. MQTT broker (the launcher starts a mosquitto instance for you,
#    but the program itself must be installed)
sudo apt install mosquitto          # Debian/Ubuntu

# 4. Frontend dependencies
cd frontend
npm install
cd ..
```

## Running

Everything is started from a single command:

```bash
python launch_gui.py --simulation --num-objects 5
```

This starts the MQTT broker, **nine simulated subsystem processes**, the
backend and the frontend; the operator UI is then served at
<http://localhost:3000>. It is the recommended way to try the system: in
simulation mode every subsystem is a real, separate process that loads the
*same* YAML state machine the orchestrator uses and communicates over MQTT
exactly like the real hardware would. Nothing in the orchestrator knows or
cares that the subsystems are fake.

Other useful invocations:

| Command | What it does |
|---|---|
| `python launch_gui.py` | normal mode — talks to the real subsystems over MQTT |
| `python launch_gui.py --simulation --num-objects N` | simulation with N objects in the box |
| `python launch_gui.py --broker <host> --mqtt-port <port>` | connect to an existing broker |
| `python launch_gui.py --debug` | adds a debug panel to the subsystem windows (see below) |
| `python launch_gui.py --ws-port <port>` | move the backend WebSocket port |
| `python launch_gui.py --verbose` | detailed logging |

`Ctrl+C` stops every process started by the launcher. Refusing to start
over an already-running instance (port conflict) is intentional.

### Using the GUI

- **Control deck** — Start, Pause, Resume, Stop and Reset the workflow.
- **Subsystem cards** — the live state of every subsystem. Clicking a card
  opens its *inspector*: a graphical view of that subsystem's state machine
  (current state highlighted, rendered from the same YAML the machine runs
  on) plus a live feed of the MQTT traffic to and from it.
- **Object cards** — one card per analyzed sample, showing where it is in
  its journey, plus an overall progress counter. Clicking a card shows the
  object's state machine and the internal events it fired.
- **Alias assignment** — pick an object, type an alias, press Assign.
- **Debug panel** (only with `--debug`) — inside a subsystem inspector you
  can hand-inject the status/response messages a real subsystem would
  publish, to test the orchestrator without hardware or to replay a
  specific message (e.g. a different object list from the camera).

## Configuration

All behavior lives in YAML files under `backend/config/` — changing a
duration, a state or an event means editing a file, not touching Python
code. The state-machine engine validates every transition at runtime and
rejects anything the configuration does not define.

- **`global.yaml`** — global defaults: broker address and ports, simulation
  defaults, the display order and colors of the object states, and the
  human-readable message shown for each workflow state.
- **`workflow.yaml`** — the workflow state machine (run lifecycle).
- **`object.yaml`** — the blueprint of one object's journey; every sample
  gets its own instance of it.
- **`subsystems/<name>.yaml`** — one file per subsystem: metadata (display
  name, whether it is required for the run to start, simulated work
  durations) and the subsystem's state machine.

**Adding a subsystem** is mostly a configuration exercise: create a new
YAML in `backend/config/subsystems/` with the subsystem's name and state
machine, and the orchestrator discovers it automatically (its card, its
MQTT topics, its place in the GUI). To have it simulated as well, copy one
of the existing one-line scripts `backend/simulation/<name>_sim.py` for the
new name, and add the name to the `SIMULATED_SUBSYSTEMS` list in
`launch_gui.py` and to the `ALL_SUBSYSTEMS` list in
`backend/adapters/subsystem.py` (used by the test suite).

## Testing

```bash
timeout 500 python backend/simulation_test.py
```

The suite starts a broker, all simulated subsystems and the orchestrator,
then drives a complete run through the WebSocket (like the real GUI would)
and validates every state transition, the MQTT exchanges and the physical
invariants of the machine (one object per disk, one in transit). Always
wrap it in a timeout: a regression can make the pipeline hang, and without
a timeout the suite would too.

## Project layout

```
backend/
├── config/               # all YAML: global, workflow, object, subsystems/
├── orchestrator/         # the brain: workflow.py (routing), pipeline.py (object flow)
├── state/                # the MQTT-agnostic state-machine engine
├── adapters/             # subsystem adapters (the MQTT boundary)
├── messaging/            # MQTT client wrapper
├── domain/               # shared models (states, statuses, configs)
├── services/             # WebSocket server for the frontend
├── simulation/           # simulated subsystems: one process each, MQTT-only
├── object_states/        # persisted per-object states (runtime data)
└── simulation_test.py    # end-to-end test suite
frontend/                 # TypeScript UI (Vite, no framework)
launch_gui.py             # the single entry point for everything
```

## Further reading

- [docs/PROJECT_CONTEXT.md](docs/PROJECT_CONTEXT.md) — the machine, its
  physical design and the intended workflow.
- [docs/GUI_ARCHITECTURE.md](docs/GUI_ARCHITECTURE.md) — architecture
  principles and layering.
- [docs/STATE_MACHINE.md](docs/STATE_MACHINE.md) — the state-machine design
  rules and how the YAML configuration works.
- [DEVELOPEMENT_JOURNAL.md](DEVELOPEMENT_JOURNAL.md) — what changed recently;
  the right place to look before starting a development session.
