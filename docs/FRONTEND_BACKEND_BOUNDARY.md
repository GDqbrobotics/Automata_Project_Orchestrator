# Frontend and backend boundary

## Purpose

This document defines the communication boundary between the TypeScript frontend and the Python orchestrator core.

## Recommended boundary

The frontend should be responsible for:

- rendering the operator interface,
- capturing operator actions,
- showing workflow state and errors,
- sending commands to the backend.

The Python backend should be responsible for:

- executing the workflow state machine,
- receiving and validating commands,
- communicating with MQTT brokers and subsystems,
- persisting or logging workflow state.

## Recommended transport

A simple and robust starting point is:

- HTTP or WebSocket for frontend-to-backend communication,
- JSON payloads for commands and state updates.

## Suggested message patterns

### Frontend to backend

- start_workflow
- pause_workflow
- resume_workflow
- stop_workflow
- acknowledge_alert
- request_status

### Backend to frontend

- workflow_state_changed
- subsystem_status_updated
- error_reported
- progress_updated

## Design rules

- Do not let the frontend directly invoke subsystem-specific logic.
- Do not let the backend expose UI-only behavior.
- Keep event payloads typed and versioned where possible.
- Treat the backend as the authority for workflow truth.
