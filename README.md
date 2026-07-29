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

## Guidance for agents and contributors

If you are starting work in this repository, begin with:

- [AGENTS.md](AGENTS.md) for repo-specific instructions for agents,
- [docs/GUI_ARCHITECTURE.md](docs/GUI_ARCHITECTURE.md) for the recommended software architecture.

## Suggested next steps

1. Define the core state model for the orchestrator.
2. Split the UI from the orchestration and messaging layers.
3. Introduce adapters for each subsystem.
4. Move workflow rules into configuration files wherever possible.