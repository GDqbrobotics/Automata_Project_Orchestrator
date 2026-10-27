# Improvement ideas (beyond the wizard / MQTT-format task)

Ideas collected while implementing the configuration wizard and the
configurable MQTT message formats. They are **not** implemented — the task
was kept surgical — but each one is grounded in something the code does
today, with a note on the current state. Ordered roughly by value/effort.

---

## 1. Cross-file configuration validation (high value, low effort)

Today the wizard validates one file at a time (YAML parse + structural
checks). Nothing checks that a subsystem referenced by `object.yaml`
(`meta.actor`, `parallel_actors`) still exists, that a `rotate_command`
exists in `rotating_platform.yaml`, or that a scoped workflow event targets
a real subsystem. Deleting a subsystem in the wizard can therefore leave
`object.yaml` pointing at nothing until the next run fails.

**Idea:** add a `validate_all()` step to `ConfigService` (and to the
wizard's Review step) that loads every YAML and checks the cross-references
the `simulation_test.py` config tests already assert. Expose it also as a
`python -m backend.services.config_service --check` CLI so CI/pre-commit can
run it without a broker.

## 2. Backup + diff before save (high value, low effort)

The wizard overwrites files (atomically) with no way back from the UI.

**Idea:** keep a timestamped copy (`.bak` or a `config_backups/` folder) on
every save and let the wizard show a unified diff of pending changes in the
Review step (a simple `difflib` server-side). This also gives the operator a
"saved N minutes ago, here is what changed" trace for the machine's
configuration — valuable in a lab environment.

## 3. Hot-reload for the safe subsets (medium value, medium effort)

The wizard always asks for a restart. Some changes are safe to apply live:

- `mqtt_formats.yaml`: `WorkflowOrchestrator.message_formats` could be
  rebuilt and the adapters' `message_format` fields refreshed (the sim
  processes read the file themselves at startup, so they still need a
  restart or a signal).
- `global.yaml` display vocabulary (state order/colors/messages), alias
  pattern and simulation defaults: the frontend already renders whatever is
  broadcast, so a `reload_display_config` command would be trivial.
- `object.yaml` and subsystem state machines: genuinely restart-only.

**Idea:** a `reload_config` command that hot-applies the safe subsets and a
`restart_required` flag per file (the wizard already returns one
unconditionally), so only real state-machine changes force a relaunch.

## 4. Schema-driven wizard forms (medium value, medium effort)

The structured forms in `config-wizard.ts` are hand-written (and so are the
`FORMAT_FALLBACKS` placeholders). Every new YAML key means a frontend
change.

**Idea:** ship a small JSON-schema-like descriptor per config category from
the backend (generated next to each form or kept in
`backend/config/schemas/`) and render the forms generically. This also
makes the "Apply to YAML" patch paths data-driven and easier to test.

## 5. Config dry-run in simulation (medium value, medium effort)

After editing the object flow or a subsystem state machine, the operator
must restart and start a run to discover a broken journey.

**Idea:** add a "Validate in simulation" action to the wizard that runs the
existing config-consistency checks plus a headless simulated run
(`simulation_test.py` already does this end-to-end) against the *staged*
buffers in a temp config dir. Show the first failing transition in the
wizard before anything is written.

## 6. Replace the line-based YAML patcher with `ruamel.yaml` (medium value, low risk)

`ConfigService.patch_content` is a comment-preserving line editor restricted
to the documented schema (2-space indentation, scalar keys, flow lists). It
handles every current config file, but a patch inside a multi-line string or
a differently-formatted user file could miss. The state-machine editor is
better off: `render_state_machine` re-serializes the `states:` block (header
comments are preserved) but any comment *inside* the states block is lost.

**Idea:** use `ruamel.yaml` round-trip mode (comments and formatting are
preserved natively) for both the structured patches and the states block,
keeping the raw text editor as-is. That would add one pure-Python dependency
and could replace ~120 lines of patcher code plus the dumper workaround.

## 7. Configuration profiles / export-import (medium value, medium effort)

The machine may be configured differently per site or demo (which analyzers
exist, broker address, fake vs real hardware).

**Idea:** let the wizard export the whole `backend/config/` as a single
JSON/YAML bundle and import one back, with a profile name stored in
`global.yaml`. Combined with idea 2, this makes reconfiguring the machine a
one-click operation.

## 8. Audit trail for configuration changes (medium value, low effort)

The wizard logs saves (`logger.info("[wizard] saved %s")`) but the UI does
not show who changed what, when. In a shared lab machine that matters.

**Idea:** append a one-line JSON record per wizard save
(`backend/config/audit.log`: timestamp, file, action, before/after hash) and
surface it in the wizard's Review step. Since the file is config-adjacent
time-series data, keep it out of the YAML tree and add it to `.gitignore`.

## 9. MQTT protocol-error surfacing (medium value, low effort)

`_on_mqtt_message` currently ignores malformed payloads silently (wrong
JSON, missing `state`, unknown response type). The operator sees a card
stuck at `offline` with no explanation.

**Idea:** when a configured status/response topic receives a payload whose
configured field is missing, raise a `subsystem_message` warning with the
decoded payload and the expected field name (the format layer already knows
it) — this turns "my JSON key is different" into a visible, fixable warning,
which is exactly the use case `mqtt_formats.yaml` exists for.

## 10. Split the wizard component before it grows (low value now)

`config-wizard.ts` is ~1100 lines. It is cohesive, but the next step (idea 4)
would push it further.

**Idea:** split into `ui/config-wizard/` with `shell.ts`, `workflow-step.ts`,
`subsystems-step.ts`, `messages-step.ts`, `review-step.ts` and a shared
`model.ts`, mirroring the backend `services/config_service.py` sections.

## 11. Object data: aliases, exports and per-object views (product-level)

Noted while reading the project (see also the journal's next steps):

- Object state is persisted to JSON per object, but the operator **alias**
  lives only in memory/broadcasts; a restart or reset loses the mapping to
  the local database. Persisting it with the object state (and exporting a
  CSV/SQLite record per completed object) would close the manual DB loop.
- The object inspector shows transitions/events but not the data produced
  (images, measurement points). A per-object data view would make the GUI
  the single place a run is reviewed.

## 12. Structured task-chain editing (follow-up to the object editor)

The wizard now edits object states, their transitions/actions and their
physical `meta`, but the top-level `metadata` (`disks`, `disk_names`,
`hand_slots`, `task_chains`) is still raw YAML. A guided editor for the
pick/place/return chains (`op`, `object_event`, `next_command`,
`done_event`, `releases`) — validated against the object machine and the
subsystem events, exactly like `_validate_object` already does — would make
the last hand-edited corner of the flow wizard-managed, and would have made
the XRF/Raman split a fully guided operation.

## 13. CI for configuration and frontend (low effort, high payoff)

`simulation_test.py` is run manually and needs a broker. The config checks
(new tests 14/15) and `npm run build` need no hardware.

**Idea:** split the suite into a fast `config_test.py` (state machines,
object metas, message formats, config service — no broker) and the full
end-to-end `simulation_test.py`, wire the fast one plus `npm run build` into
a pre-commit hook / GitHub Actions job, and keep the full run nightly.

---

Ideas opened by the real-interface scripts feature (R11,
`metadata.interface`):

## 14. Interface health watchdog (high value, low effort)

`metadata.interface` launches the real interface and trusts it. Nothing
detects an interface that died right after starting (the subsystem simply
never reports `ready` and the run stalls in `starting` with only the
generic "not ready" message).

**Idea:** after a successful `ensure_started`, arm a readiness timer
(reuse the discrepancy-check machinery, `orchestration.state_expect_timeout`):
if the subsystem has not reported *any* state within N seconds of the
launch, emit an error event naming the interface and, optionally, re-run
the startup script once. A heartbeat gap on a subsystem whose interface we
manage could trigger the same recovery path.

## 15. Remote interfaces (the subsystems live on other PCs)

The startup/shutdown commands run on the orchestrator's machine, but
PROJECT_CONTEXT places most subsystems on other computers (Ubuntu and
Windows). Today the operator works around it by making the command an
`ssh host 'systemctl start xrf'` one-liner — which works but scatters
host names through the YAML.

**Idea:** support an optional `host:` key in `metadata.interface`
(executed via a configured `ssh`/agent command by the interface manager),
or document the one-liner pattern in R11 and validate that the configured
command is non-empty per enabled interface. A per-subsystem
`interface.host` keeps the YAML declarative and lets the wizard show
"runs on <host>".

## 16. Interface log viewer in the subsystem inspector (medium value, low effort)

The interface output already lands in `backend/interface_logs/<name>.log`,
but you have to open the file manually.

**Idea:** a `get_interface_log` command (debug mode, or always for
configured interfaces) that returns the last N lines of the log, rendered
in the subsystem inspector as a collapsible panel — the fastest way to
answer "why did the camera interface not come up?".

## 17. Generalized per-state script hooks (only if ever needed)

R11 deliberately attached the scripts to the subsystem (startup before
initialization, shutdown on `stopped`) instead of to states, because the
startup case is special: the interface must run *before* the subsystem
can report entering any state, so a "run on entering state X" hook could
never express it.

**Idea:** if a real use case appears for side effects on *other* state
entries (e.g. warm-up on `ready`, teardown on `error`), add an optional
`on_enter` script to the state editor instead of special-casing states in
Python — but keep the interface pair in `metadata.interface`, where the
orchestrator's proactive launch semantics live.

## 18. Theme variants from the design tokens (low effort, low priority)

After the UI polish pass the whole dashboard is styled from the `:root`
token block in `frontend/src/styles/app.css` (palette, elevation, radii,
fonts) — every component derives from it.

**Idea:** a second token set (light / high-contrast operator mode for
bright lab environments, or a colorblind-safe status palette) is now a
matter of swapping the `:root` block — e.g. a `data-theme` attribute on
`<body>` plus a small picker persisted in `localStorage`. No component
CSS would need to change; the only caution is keeping the backend-driven
tones (F3) legible in both themes.
