# Agent guidance for this repository

## Purpose

This repository is the GUI orchestrator for the AUTOMATA machine. It is not the full machine itself, and it should not be treated as a general-purpose project container for all subsystems.

## Primary responsibilities

Focus on the following areas:

- operator-facing GUI behavior in the TypeScript frontend,
- workflow orchestration and state transitions in the Python core,
- MQTT communication with external subsystems,
- configuration-driven state-machine logic,
- logging, error handling, and recovery.

## Recommended design principles

- Keep the UI thin and declarative.
- Put orchestration logic in a dedicated domain layer.
- Model the system with explicit states and events.
- Treat each external subsystem as an adapter rather than embedding hardware-specific logic in the UI.
- Prefer configuration files over hardcoded behavior whenever possible.
- Preserve clear separation between presentation, orchestration, and transport layers.

## Suggested architecture shape

Use a layered structure such as:

- Frontend layer: TypeScript UI screens, widgets, operator actions
- Core orchestrator layer: Python workflow coordination and state transitions
- State layer: machine and subsystem states
- Messaging layer: MQTT client and topic handling
- Adapter layer: integration with vision, robotics, analyzers, and cloud services
- Config layer: workflow definitions and state-machine configuration

## Language and boundary rules

- Use TypeScript for the frontend experience and UI state presentation.
- Use Python for the orchestration core, state-machine engine, adapters, and subsystem coordination.
- Keep the frontend thin: it should send user intents and render state, not contain hardware-specific logic.
- Prefer a clear IPC or HTTP/WebSocket boundary between the frontend and the Python backend.
- Avoid duplicating workflow logic in the UI.

## Working conventions

- Understand the current workflow state before changing behavior.
- When adding a new subsystem, add an adapter and update the configuration model.
- Keep messages and commands typed and well documented.
- Prefer small, testable modules over large monolithic components.
- Update documentation when architecture or workflow behavior changes.

## Notes for agents

If a contribution affects the workflow, the UI, or subsystem integration, review the architecture notes in docs/GUI_ARCHITECTURE.md before making changes.
