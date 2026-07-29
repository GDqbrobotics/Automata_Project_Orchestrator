# GUI architecture recommendations

## 1. Overall approach

The GUI should be organized as a control layer rather than as a monolithic application. The key idea is to separate:

- operator interaction,
- workflow orchestration,
- subsystem integration,
- and configuration.

A good default is an event-driven architecture with a central state machine, implemented as a TypeScript frontend communicating with a Python orchestrator core.

## 2. Core architectural layers

### Frontend presentation layer (TypeScript)

Responsible for the interface seen by the operator:

- current system state,
- workflow progress,
- alarms and errors,
- manual actions such as start, pause, stop, and intervention.

This layer should stay thin and should not contain hardware-specific logic.

### Orchestration layer (Python)

This is the heart of the GUI. It decides what happens next based on the current state and incoming events.

Recommended behavior:

- explicit states such as idle, running, paused, completed, and error,
- transitions triggered by operator actions and subsystem events,
- clear handling of recovery and retry logic.

### Communication layer

The Python orchestrator communicates with remote subsystems through MQTT. The frontend should not talk directly to hardware or devices. This layer should provide:

- topic abstraction,
- message validation,
- connection lifecycle management,
- logging and tracing of commands and responses.

### Adapter layer

Each subsystem should be wrapped by an adapter that translates between the internal orchestrator model and the external protocol or device interface.

This keeps the core logic stable even if the underlying device interface changes.

## 3. Suggested module structure

A practical layout could look like this:

### Frontend structure (TypeScript)

- src/ui: screens and components
- src/state: frontend view state and UI models
- src/api: client code for communicating with the Python backend
- src/services: formatting, notifications, local helpers

### Backend structure (Python)

- orchestrator/: workflow engine and transition logic
- state/: state definitions and state-machine helpers
- messaging/: MQTT client and message handling
- adapters/: subsystem-specific wrappers
- config/: YAML or JSON workflow definitions
- domain/: shared models and event types
- services/: logging, persistence, notifications

## 4. Recommended design rules

- Keep the state machine as the source of truth.
- Use typed events and commands instead of ad hoc strings.
- Avoid direct calls from the UI into subsystem implementations.
- Make the GUI resilient to transient failures with retries and clear error states.
- Expose operator actions only where they are meaningful and safe.
- Log all important state transitions and commands for debugging.

## 5. User experience guidance

The operator experience should be simple and predictable:

- show the current stage clearly,
- show the status of each subsystem,
- expose fast actions for pause, resume, and abort,
- make warnings and failures easy to understand,
- avoid cluttering the interface with low-level technical details.

## 6. Practical recommendation

For this project, the best starting point is:

- a TypeScript frontend for the operator experience,
- a Python orchestration core for state-machine execution,
- a separate MQTT integration layer in Python,
- subsystem adapters with well-defined contracts,
- and a UI that reacts to state changes rather than issuing raw commands directly.

This approach will make the GUI easier to test, evolve, and maintain as the machine grows more complex.
