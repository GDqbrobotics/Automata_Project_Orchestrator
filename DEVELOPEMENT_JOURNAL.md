# Development Journal — AUTOMATA GUI Orchestrator

Keep this file brief, is meant to show the next developer the work of the last one.

## Current state

Working prototype: TypeScript/Vite frontend + Python orchestrator + **process-based simulation** (one simulated subsystem process per real subsystem, all MQTT).

- Launch everything: `python launch_gui.py --simulation --num-objects 3` (UI on http://localhost:3000; starts mosquitto + 9 `backend/simulation/*_sim.py` processes + backend + frontend)
- Validate: `timeout 500 python backend/simulation_test.py` — **always wrap in a timeout** (a bug can make it hang forever)
- All state machines are YAML-driven: `backend/config/workflow.yaml`, `object.yaml`, `subsystems/*.yaml`, `global.yaml`. Simulated subsystems load the *same* YAML as the orchestrator. Work durations live in the subsystem YAML metadata (`work_durations`).
- The object flow is driven by `backend/orchestrator/pipeline.py` (PipelineController) from MQTT status reports — identical code path for simulated and real subsystems.

## Recent changes (2026-09-09 — frontend layout overhaul + legacy cleanup)

The GUI got a proper operator-HMI layout. Frontend is plain TypeScript/Vite
+ DOM (no framework — README's old "React" mention fixed).

- **New layout** (`frontend/index.html` + `frontend/src/styles/app.css`):
  fixed single-viewport dashboard — top bar (brand + connection pill),
  full-width **control deck** (big state badge, progress bar + %, large touch
  buttons, alias row), objects panel (3fr) + subsystems panel (2fr) side by
  side with internal scroll, full-width event log at the bottom. Responsive
  fallback (single column, page scroll) below 1100px.
- **CSS extracted** from `index.html` (was ~500 lines inline) into
  `src/styles/app.css` with design tokens; `index.html` is structure only.
- **Cards**: object cards now show a per-object step progress bar + step
  counter (100% green for `object_analyzed`); subsystem cards use semantic
  tone dots (ready/busy/warning/error/idle/offline — `subsystemTone()` in
  `app.ts`) instead of a CSS class per raw state name.
- **Legacy removed**: `src/main.ts` (unused placeholder), dead code in
  `app.ts` (unused imports `_extractStateOrder`/`_extractStateColors`/
  `_backendConfigApplied`), unused `getStateColor`/`getObjectStateIndex` in
  `object-styles.ts`, dead `app.start` re-patch in `index.ts`.
- **Verified**: `tsc` + `vite build` clean; live CDP probes (headless Chrome
  over `launch_gui.py --simulation`): idle (9 subsystems, required badges),
  running (badge/progress/object bars/tone dots/logs), subsystem + object
  inspector modals (graph, current state, ×N heartbeat collapse, Esc close),
  stopped (4/4, button rules), 800px viewport (no horizontal scroll).
- **Fixed (backend)**: a second launch could silently join a running
  stack — `FrontendServer` bound with `reuse_port=True` (SO_REUSEPORT), so
  multiple backends coexisted on one WS port and the kernel load-balanced
  operator connections across them (the "process swamp"); a failed bind
  also died silently inside the server thread. Now: no `reuse_port`,
  `start()` waits for the bind and raises a clear RuntimeError if the port
  is taken, and `launch_gui.py` refuses to start when the WS/frontend
  ports are already in use (exit 1, no processes spawned).

## Previous changes (2026-09-08 — state machine inspector modal)

Clicking a **subsystem card** or an **object card** in the GUI now opens a
modal with a **graphical view of that state machine** (current state
enlightened) and a **live message feed**.

- **New frontend events** (`backend/domain/models.py`): `subsystem_message`
  (one MQTT message of a subsystem: direction sent/received, topic, payload),
  `object_message` (one object's internal messages: `kind=transition`
  from/to/event, or `kind=event` with the fired event and its target —
  subsystem name, `pipeline (internal)` for `workflow/` scope, or
  broadcast), `messages_cleared` (fired on workflow reset).
- **Graph definitions on the wire**: every status broadcast now carries
  `subsystem_state_machines` + `object_state_machine`, serialized from the
  same YAML the engines run (`StateMachineConfig.to_dict()`,
  `backend/state/state_machine.py`); newly (re)connecting WS clients get
  the last full status replayed immediately (`frontend_server.py`), so the
  modal is populated without pressing Refresh.
- **MQTT sent recording**: `SubsystemAdapter.on_mqtt_publish` observer —
  the adapter boundary is the single choke point recording 'sent' traffic;
  'received' traffic is recorded in the orchestrator's MQTT dispatcher;
  raw pause/resume broadcasts too (`backend/orchestrator/workflow.py`).
- **Object message ordering**: events fired during a transition are
  buffered in the orchestrator (`_object_event_buffer`) and flushed right
  after the transition record, so the feed reads transition → its events.
- **Frontend**: `src/ui/sm-graph.ts` (layered SVG layout: longest-path
  depth, max 3 per column, max 5 per row, back-edges routed under the
  nodes, scoped events colored, hover highlights a node's transitions,
  native tooltips with descriptions) and `src/ui/state-machine-modal.ts`
  (modal + live feed, Esc/overlay/× close). The store keeps the last 100
  messages per subsystem/object and collapses consecutive duplicates
  (status heartbeats → ×N badge); object history is removed on
  `object_removed`.
- Also fixed: `workflow_state_changed` handler now accepts `details.state`
  (full status payloads), not only `new_state` — needed so the replayed
  status updates the workflow badge on (re)connect.

Verified: 106/106 simulation tests; headless-browser checks render all 10
SMs (no out-of-bounds elements, no node overlaps), store collapse/clear/
remove and modal open/close/Esc; live WS probe: SM defs in status,
sent/received topics per subsystem, transition-before-event ordering,
`pipeline (internal)` targets.

## Next steps

1. **MQTT integration with real subsystems** — receive path is wired; still needed: reconnection/health checks, command/response correlation, retry policies.
2. **Reliability & recovery** — subsystem health checks, graceful degradation when non-critical subsystems are offline, workflow checkpoint/restore.
3. **CI/CD** — automated runs of `simulation_test.py`, linting (ruff/mypy, eslint/prettier), docker compose (orchestrator + mosquitto + frontend).
4. **Enhanced UI** — backend endpoint for alias ID assignment (the frontend button currently only logs), alarm/notification system, data export.
