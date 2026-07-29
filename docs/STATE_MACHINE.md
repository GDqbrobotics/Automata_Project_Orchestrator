# State machine specification

This document describes the expected structure for the state-machine configuration used by the GUI orchestrator.

## Purpose

The GUI orchestrator should not hardcode every workflow decision. Instead, it should read a declarative state-machine configuration that defines:

- the set of valid states,
- the allowed transitions between states,
- the events that trigger transitions,
- the actions associated with each transition,
- the conditions that must be satisfied before moving forward.

## Core concepts

### State
A state represents the current condition of the machine or one of its subsystems.

Examples:
- idle
- starting
- running
- paused
- completing
- error
- stopped

### Event
An event is an external or internal trigger that causes the state machine to evaluate a transition.

Examples:
- start_requested
- pause_requested
- resume_requested
- stop_requested
- subsystem_ready
- subsystem_error
- workflow_completed

### Transition
A transition is a valid move from one state to another, triggered by an event and optionally guarded by a condition.

### Action
An action is a side effect executed during a transition.

Examples:
- send_command_to_subsystem
- publish_mqtt_message
- log_event
- update_operator_ui
- record_workflow_progress

## Suggested configuration shape

A YAML file can define the state machine in a clear and extensible structure.

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

  running:
    description: Workflow in progress
    transitions:
      - event: pause_requested
        target: paused
        actions:
          - publish_mqtt_message: workflow.pause

      - event: stop_requested
        target: stopped
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
        target: stopped
        actions:
          - publish_mqtt_message: workflow.stop

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

## Recommended rules

1. All workflow behavior should be driven by the state machine configuration.
2. The UI should react to state changes, not directly decide the next step.
3. Subsystem communication should be encapsulated behind adapters.
4. Each transition should be explicit and easy to inspect.
5. Invalid transitions should be rejected with a clear error.
6. Errors and recovery paths should be modeled as first-class states or transitions.

## Validation expectations

The state-machine engine should validate that:

- the initial state exists,
- every transition target exists,
- no transition is defined for an unknown event without a fallback,
- the configuration is syntactically valid YAML,
- the orchestration runtime can load and execute it safely.

## Suggested implementation pattern

The runtime can follow this simple flow:

1. Receive an event.
2. Look up the current state.
3. Find a matching transition for the event.
4. Check any guard conditions.
5. Execute transition actions.
6. Update the active state.
7. Notify the UI and relevant adapters.

## Notes for future implementation

As the project grows, the state-machine model can be extended with:

- hierarchical states,
- parallel substates,
- timed transitions,
- retry policies,
- operator intervention states,
- subsystem-specific state machines that are coordinated by the main workflow machine.
