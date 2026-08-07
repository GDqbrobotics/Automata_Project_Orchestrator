# Development Journal — AUTOMATA GUI Orchestrator

Keep this file brief, is meant to show the next developer the work of the last one.   

## Current state

A working prototype is complete with integrated frontend, backend, and simulation. The system can be launched with a single command and validated with simulation tests.

## Recent changes

Major refactoring: moved all hardcoded workflow logic into YAML configuration files. The system now follows the principle that **behavior lives in config, not in code**.

**New config files**:
- `backend/config/workflow_actions.yaml` – Maps object state machine events to subsystem commands (previously hardcoded as 7 Python methods)
- `backend/config/global.yaml` – Centralized defaults for MQTT broker, frontend port, simulation defaults, object state colors/order, and workflow state messages

**New frontend config files**:
- `frontend/src/config/object-styles.ts` – Object state colors, display order, and step mapping
- `frontend/src/config/button-rules.ts` – Button enable/disable rules per workflow state
- `frontend/src/config/state-messages.ts` – Workflow state display messages

**Backend changes**:
- `WorkflowOrchestrator` now accepts a `GlobalConfig` parameter (loaded from `global.yaml`)
- Replaced 7 hardcoded `_trigger_*` methods with generic `_execute_workflow_action()` that reads from `workflow_actions.yaml`
- Added `_resolve_placeholders()` for `{{object_id}}` substitution in payloads
- `WorkflowActionCommand`, `WorkflowAction`, `WorkflowActionsConfig`, `GlobalConfig` dataclasses added to `domain/models.py`
- Simulation's `_build_event_chain()` derives the full event chain from `object.yaml` + `workflow_actions.yaml` instead of using a hardcoded list
- CLI entry point loads `global.yaml` by default, CLI args override config values

**Frontend changes**:
- `app.ts` uses config-driven state colors, button rules, and state messages
- `store.ts` uses config-driven state messages
- All hardcoded display rules moved to `src/config/`

**Simulation changes**:
- Simulation now reads the event chain from config files, not from hardcoded Python lists
- Multi-subsystem events (like `trigger_measurements`) correctly simulate all subsystems before advancing the object state
- Fallback to default event chain if config files are missing

**Test results**: 123 tests passing (was 123), all tests updated to verify config-driven behavior.

**Impact**: Adding a new analysis step now requires only editing YAML files — no Python or TypeScript code changes needed.

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
