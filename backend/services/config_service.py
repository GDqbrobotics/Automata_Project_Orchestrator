"""Configuration service for the GUI wizard (debug mode only).

Owns every write the wizard can perform on ``backend/config/``:

* **snapshot()** – the editable files (raw text + parsed metadata) the
  wizard renders;
* **validate_content()** – YAML + structural checks before anything is
  written (invalid config never lands on disk);
* **save()** – atomic write of an edited file;
* **patch_content()** – comment-preserving line edits for the wizard's
  structured forms (the raw YAML editor is the source of truth; structured
  fields patched into it never lose the rest of the file);
* **create_subsystem()/delete_subsystem()** – add/remove a subsystem YAML
  (plus its simulator script) and report references to it elsewhere;
* **render_message_formats()** – rebuild ``mqtt_formats.yaml`` from the
  wizard's structured MQTT-format form.

The service is transport-agnostic: the orchestrator exposes it through the
WebSocket commands ``get_config`` / ``validate_config`` / ``patch_config`` /
``save_config`` / ``create_subsystem`` / ``delete_subsystem`` /
``render_message_formats`` and the frontend only ever sends/receives JSON.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

# Subsystem names become both the YAML file name and the MQTT topic segment:
# keep them in this shape so a wizard-created subsystem is routable.
SUBSYSTEM_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# The wizard may only touch these files (relative to the config dir).
_EDITABLE_TOP_LEVEL = {"global.yaml", "workflow.yaml", "object.yaml", "mqtt_formats.yaml"}

# Standard state-machine skeleton used when the wizard creates a subsystem.
_SUBSYSTEM_STATE_TEMPLATE = """\
states:
  initializing:
    description: "{display_name} is initializing"
    transitions:
      - event: init_complete
        target: ready
        actions:
          - log_event

      - event: error
        target: error
        actions:
          - log_event

  ready:
    description: "{display_name} is ready and idle"
    transitions:
      - event: {work_event}
        target: {work_state}
        actions:
          - log_event

      - event: error
        target: error
        actions:
          - log_event

  {work_state}:
    description: "{display_name} is working"
    transitions:
      - event: complete
        target: ready
        actions:
          - log_event

      - event: stop
        target: stopped
        actions:
          - log_event

      - event: error
        target: error
        actions:
          - log_event

  stopped:
    description: "{display_name} has stopped"
    transitions:
      - event: ready
        target: ready
        actions:
          - log_event

      - event: {work_event}
        target: {work_state}
        actions:
          - log_event

  error:
    description: "{display_name} is in error state"
    transitions:
      - event: reset
        target: ready
        actions:
          - log_event
"""

_MQTT_FORMATS_HEADER = """\
# MQTT message payload formats (docs/CONFIG_CHANGE_GUIDE.md, R10)
#
# Every payload the orchestrator exchanges with a subsystem is built and
# parsed through this file — the field mapping layer between the internal
# event model and what the real hardware actually speaks. Field names may
# be dotted paths into nested payloads (e.g. `state_field: data.state`).
#
# The wizard (debug mode) edits this file through structured forms; the
# whole document can also be edited by hand like any other config file.
#
"""


class ConfigError(Exception):
    """Raised when a wizard config operation is invalid or unsafe."""


# ---------------------------------------------------------------------------
# YAML line helpers (comment-preserving structured patches)
# ---------------------------------------------------------------------------
def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _is_key(line: str, key: str) -> bool:
    stripped = line.strip()
    return stripped == f"{key}:" or stripped.startswith(f"{key}: ")


def _block_end(lines: List[str], header_idx: int) -> int:
    """First line index after the block whose header is ``header_idx``."""
    header_indent = _indent(lines[header_idx])
    j = header_idx + 1
    while j < len(lines):
        stripped = lines[j].strip()
        if stripped and not stripped.startswith("#"):
            if _indent(lines[j]) <= header_indent:
                break
        j += 1
    return j


def _find_key(lines: List[str], path: List[str]) -> Optional[int]:
    """Line index of a nested key (``["metadata", "operator_events", "start"]``).

    Matching is scoped by the parent blocks (2-space indentation, the
    convention used by every config file in this repo), so a same-named key
    in a sibling block is never matched.
    """
    start, end = 0, len(lines)
    indent = 0
    found: Optional[int] = None
    for part in path:
        hit: Optional[int] = None
        for j in range(start, end):
            stripped = lines[j].strip()
            if not stripped or stripped.startswith("#"):
                continue
            if _indent(lines[j]) != indent:
                continue
            if _is_key(lines[j], part):
                hit = j
                break
        if hit is None:
            return None
        found = hit
        start, end = hit + 1, _block_end(lines, hit)
        indent += 2
    return found


def _dump_value(value: Any) -> str:
    """Serialize one YAML scalar/flow value without the document marker."""
    text = yaml.safe_dump(value, default_flow_style=True, allow_unicode=True).strip()
    if text.endswith("..."):
        text = text[:-3].strip()
    return text


def _insert_key(lines: List[str], path: List[str], value: Any) -> None:
    """Insert ``path: value``, creating missing parent mappings."""
    *parent, key = path
    if parent:
        parent_idx = _find_key(lines, parent)
        if parent_idx is None:
            _insert_key(lines, parent, None)
            parent_idx = _find_key(lines, parent)
        assert parent_idx is not None
        insert_at = _block_end(lines, parent_idx)
        indent = 2 * len(parent)
    else:
        insert_at = len(lines)
        indent = 0
    scalar = "" if value is None else f" {_dump_value(value)}"
    lines.insert(insert_at, " " * indent + f"{key}:{scalar}")


def _apply_patch(lines: List[str], path: List[str], value: Any) -> None:
    idx = _find_key(lines, path)
    if idx is None:
        _insert_key(lines, path, value)
        return
    indent = _indent(lines[idx])
    scalar = "" if value is None else f" {_dump_value(value)}"
    lines[idx] = " " * indent + f"{path[-1]}:{scalar}"


class _IndentDumper(yaml.SafeDumper):
    """PyYAML dumper that indents block sequences like the repo's YAML
    (``- item`` is indented under its key) instead of flush-left."""

    def increase_indent(self, flow: bool = False, indentless: bool = False):
        return super().increase_indent(flow, False)


def _replace_states_block(
    content: str,
    states: Dict[str, Any],
    initial_state: Optional[str] = None,
) -> str:
    """Regenerate the top-level ``states:`` block of a config file.

    Everything before the block (name, metadata and their documentation
    comments) and any trailing content are preserved verbatim; only the
    state machine itself is re-serialized from the structured editor's
    data.
    """
    lines = content.splitlines()
    states_idx: Optional[int] = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if _indent(line) == 0 and _is_key(line, "states"):
            states_idx = i
            break

    if states_idx is None:
        header = list(lines)
        tail: List[str] = []
    else:
        header = lines[:states_idx]
        end = _block_end(lines, states_idx)
        # Trailing blank/comment lines belong to the file, not the block.
        tail_start = end
        while tail_start > states_idx:
            prev = lines[tail_start - 1].strip()
            if prev == "" or prev.startswith("#"):
                tail_start -= 1
            else:
                break
        tail = lines[tail_start:end] + lines[end:]

    if initial_state is not None:
        _apply_patch(header, ["initial_state"], initial_state)

    while header and header[-1].strip() == "":
        header.pop()

    body = yaml.dump(
        {"states": states},
        Dumper=_IndentDumper,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    ).rstrip("\n").splitlines()

    result = list(header)
    if result and result[-1].strip() != "":
        result.append("")
    result.extend(body)
    while result and result[-1].strip() == "":
        result.pop()
    if tail and any(line.strip() for line in tail):
        result.append("")
        result.extend(tail)
    return "\n".join(result) + "\n"


# ---------------------------------------------------------------------------
# Config service
# ---------------------------------------------------------------------------
class ConfigService:
    """Read/validate/patch/write operations for the wizard."""

    def __init__(
        self,
        config_dir: str | Path,
        simulation_dir: str | Path,
        root_dir: Optional[str | Path] = None,
    ) -> None:
        self.config_dir = Path(config_dir).resolve()
        self.simulation_dir = Path(simulation_dir).resolve()
        self.root_dir = (
            Path(root_dir).resolve()
            if root_dir is not None
            else self.config_dir.parent.parent
        )

    # ------------------------------------------------------------------
    # Paths
    # ------------------------------------------------------------------
    def _resolve(self, path_str: str) -> Path:
        """Resolve a wizard path (relative to the repo root) under config/.

        Only ``backend/config/**.yaml`` is writable: an absolute or relative
        path resolving outside the config directory is rejected, so the
        wizard can never be tricked into editing arbitrary files.
        """
        candidate = Path(path_str)
        candidate = (
            candidate if candidate.is_absolute() else self.root_dir / candidate
        ).resolve()
        if not candidate.is_relative_to(self.config_dir):
            raise ConfigError(
                f"Path '{path_str}' is outside the configuration directory"
            )
        if candidate.suffix.lower() not in (".yaml", ".yml"):
            raise ConfigError(f"Path '{path_str}' is not a YAML file")
        rel = candidate.relative_to(self.config_dir)
        parts = rel.parts
        if len(parts) == 1:
            if rel.name not in _EDITABLE_TOP_LEVEL:
                raise ConfigError(f"File '{rel.name}' is not editable by the wizard")
        elif parts[0] != "subsystems" or len(parts) != 2:
            raise ConfigError(f"Path '{path_str}' is not editable by the wizard")
        return candidate

    def _rel(self, path: Path) -> str:
        return str(path.relative_to(self.root_dir))

    def _category(self, path: Path) -> str:
        rel = path.relative_to(self.config_dir)
        if rel.parts[0] == "subsystems":
            return "subsystem"
        if rel.name == "mqtt_formats.yaml":
            return "message_formats"
        return rel.stem

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------
    def snapshot(self) -> Dict[str, Any]:
        """Everything the wizard needs to render its steps."""
        files: List[Dict[str, Any]] = []
        for path in self._editable_files():
            content = path.read_text()
            entry: Dict[str, Any] = {
                "path": self._rel(path),
                "label": path.name,
                "category": self._category(path),
                "content": content,
                "parsed": self._parse(content),
                # Existing problems are shown as soon as the wizard opens,
                # so a save never has to discover them first.
                "errors": self.validate_content(self._rel(path), content),
            }
            if entry["category"] == "subsystem":
                name = (entry["parsed"] or {}).get("name") or path.stem
                entry["name"] = name
                entry["sim_exists"] = (
                    self.simulation_dir / f"{name}_sim.py"
                ).exists()
            files.append(entry)

        return {
            "files": files,
            "message_formats": self.message_formats_dict(),
            "editable": True,
        }

    def _editable_files(self) -> List[Path]:
        files = [
            self.config_dir / name
            for name in sorted(_EDITABLE_TOP_LEVEL)
            if (self.config_dir / name).exists()
        ]
        files.extend(sorted((self.config_dir / "subsystems").glob("*.yaml")))
        return files

    @staticmethod
    def _parse(content: str) -> Optional[Dict[str, Any]]:
        try:
            data = yaml.safe_load(content)
        except yaml.YAMLError:
            return None
        return data if isinstance(data, dict) else None

    def message_formats_dict(self) -> Dict[str, Any]:
        """The parsed ``mqtt_formats.yaml`` (empty structure when missing)."""
        path = self.config_dir / "mqtt_formats.yaml"
        if not path.exists():
            return {"default": {}, "subsystems": {}}
        data = self._parse(path.read_text()) or {}
        return {
            "default": data.get("default") or {},
            "subsystems": data.get("subsystems") or {},
        }

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    def validate_content(self, path_str: str, content: str) -> List[str]:
        """Return the validation errors of a candidate file ([] = valid)."""
        try:
            path = self._resolve(path_str)
        except ConfigError as exc:
            return [str(exc)]

        try:
            data = yaml.safe_load(content)
        except yaml.YAMLError as exc:
            return [f"YAML parse error: {exc}"]

        if data is None:
            return ["Configuration is empty"]
        if not isinstance(data, dict):
            return ["Top-level configuration must be a mapping"]

        category = self._category(path)
        if category == "subsystem":
            return self._validate_subsystem(data)
        if category == "object":
            return self._validate_object(data)
        if category == "workflow":
            return self._validate_state_machine(data)
        if category == "message_formats":
            return self._validate_message_formats(data)
        return self._validate_global(data)

    # ------------------------------------------------------------------
    # Shared vocabulary / cross-file inventory (validation helpers)
    # ------------------------------------------------------------------
    def _shared_vocabulary(self) -> Dict[str, Any]:
        """The `global.yaml` shared vocabulary the completion contract uses."""
        data: Dict[str, Any] = {}
        try:
            data = yaml.safe_load((self.config_dir / "global.yaml").read_text()) or {}
        except (OSError, yaml.YAMLError):
            pass
        states = data.get("states") or {}
        non_work = states.get("non_work")
        if not isinstance(non_work, list) or not non_work:
            non_work = ["initializing", "ready", "stopped", "error"]
        return {
            "non_work": {str(s) for s in non_work},
            "ready": str(states.get("ready") or "ready"),
        }

    def _subsystem_machines(self) -> Dict[str, Dict[str, Any]]:
        """{subsystem name: states mapping} for every subsystem YAML."""
        machines: Dict[str, Dict[str, Any]] = {}
        subs_dir = self.config_dir / "subsystems"
        if not subs_dir.exists():
            return machines
        for path in sorted(subs_dir.glob("*.yaml")):
            data = self._parse(path.read_text()) or {}
            name = data.get("name") or path.stem
            states = data.get("states")
            machines[str(name)] = states if isinstance(states, dict) else {}
        return machines

    @staticmethod
    def _subsystem_events(states: Dict[str, Any]) -> set:
        return {
            t.get("event")
            for state_def in states.values()
            if isinstance(state_def, dict)
            for t in state_def.get("transitions") or []
            if isinstance(t, dict)
        }

    @staticmethod
    def _actor_can_complete(
        actor: str, machines: Dict[str, Dict[str, Any]], non_work: set, ready: str
    ) -> bool:
        """Does the actor have a work state that transitions back to ready?

        That is exactly what the pipeline watches for: a subsystem reporting
        the ready state after a work state is its completion.
        """
        states = machines.get(actor)
        if states is None:
            return False
        for state_name, state_def in states.items():
            if state_name in non_work or not isinstance(state_def, dict):
                continue
            for t in state_def.get("transitions") or []:
                if isinstance(t, dict) and t.get("target") == ready:
                    return True
        return False

    @staticmethod
    def _validate_state_machine(data: Dict[str, Any]) -> List[str]:
        errors: List[str] = []
        states = data.get("states")
        if not isinstance(states, dict) or not states:
            return ["'states' must be a non-empty mapping"]
        initial = data.get("initial_state")
        if not isinstance(initial, str) or initial not in states:
            errors.append(
                f"'initial_state' ('{initial}') must be one of: {', '.join(states)}"
            )
        for state_name, state in states.items():
            if not isinstance(state_name, str) or not re.match(
                r"^[A-Za-z0-9_]+$", state_name
            ):
                errors.append(
                    f"State '{state_name}' must be named with letters, digits or "
                    "underscores"
                )
                continue
            if not isinstance(state, dict):
                errors.append(f"State '{state_name}' must be a mapping")
                continue
            transitions = state.get("transitions") or []
            if not isinstance(transitions, list):
                errors.append(f"State '{state_name}': 'transitions' must be a list")
                continue
            for t in transitions:
                if (
                    not isinstance(t, dict)
                    or not isinstance(t.get("event"), str)
                    or not t.get("event")
                    or not isinstance(t.get("target"), str)
                    or not t.get("target")
                ):
                    errors.append(
                        f"State '{state_name}': every transition needs a string "
                        "'event' and 'target'"
                    )
                    continue
                if t["target"] not in states:
                    errors.append(
                        f"State '{state_name}': transition target "
                        f"'{t['target']}' is not a defined state"
                    )
        return errors

    def _validate_subsystem(self, data: Dict[str, Any]) -> List[str]:
        errors = self._validate_state_machine(data)
        name = data.get("name")
        if not isinstance(name, str) or not name:
            errors.append("'name' is required")
        elif not SUBSYSTEM_NAME_RE.match(name):
            errors.append(
                "'name' must be lower_snake_case (letters, digits, underscore)"
            )
        if not isinstance(data.get("required", True), bool):
            errors.append("'required' must be true or false")
        # Reporting roles (R5): the declared `after` transition must exist.
        metadata = data.get("metadata") or {}
        # Real-interface scripts (R11): an enabled interface needs the
        # command that starts it, otherwise the orchestrator could never
        # bring the subsystem's interface up.
        interface = metadata.get("interface") if isinstance(metadata, dict) else None
        if interface is not None:
            if not isinstance(interface, dict):
                errors.append("metadata.interface must be a mapping")
            else:
                if not isinstance(interface.get("enabled", False), bool):
                    errors.append("metadata.interface.enabled must be true or false")
                if interface.get("enabled") and not str(interface.get("startup") or "").strip():
                    errors.append(
                        "metadata.interface: 'startup' is required when "
                        "'enabled' is true (the command that starts the "
                        "subsystem's real interface)"
                    )
                for key in ("startup", "shutdown", "cwd"):
                    if key in interface and not str(interface.get(key) or "").strip():
                        errors.append(
                            f"metadata.interface.{key} must not be empty "
                            f"(remove the key instead)"
                        )
        # Reporting roles (R5): the declared `after` transition must exist.
        for rep in metadata.get("reports") or []:
            if not isinstance(rep, dict) or not rep.get("type"):
                errors.append("metadata.reports: every report needs a 'type'")
                continue
            after = rep.get("after") or {}
            frm, to = after.get("from"), after.get("to")
            states = data.get("states") or {}
            if (
                frm not in states
                or not any(
                    t.get("target") == to for t in (states[frm].get("transitions") or [])
                )
            ):
                errors.append(
                    f"metadata.reports['{rep.get('type')}']: no transition "
                    f"'{frm}' -> '{to}' in the state machine"
                )

        # ── Completion contract ────────────────────────────────────────
        # The pipeline (and the generic simulator) detect completion when a
        # subsystem reports the ready state after a work state: a work state
        # with no way back to ready would make the workflow wait forever.
        # Subsystem transitions are also not allowed to fire internal
        # events: that path is not wired (subsystems only report states).
        vocabulary = self._shared_vocabulary()
        ready = vocabulary["ready"]
        non_work = vocabulary["non_work"]
        states = data.get("states") or {}
        for state_name, state_def in states.items():
            if not isinstance(state_def, dict):
                continue
            transitions = state_def.get("transitions") or []
            targets = {t.get("target") for t in transitions if isinstance(t, dict)}
            if state_name not in non_work and ready not in targets:
                errors.append(
                    f"State '{state_name}' is a work state with no transition back to "
                    f"the ready state '{ready}': the subsystem could never report "
                    "completion and the workflow would wait for it forever"
                )
            for t in transitions:
                if not isinstance(t, dict):
                    continue
                for action in t.get("actions") or []:
                    if isinstance(action, dict) and "event" in action:
                        errors.append(
                            f"State '{state_name}': subsystem transitions cannot fire "
                            f"internal events ('{action.get('event')}' would be ignored) – "
                            "a subsystem reports its result by returning to the ready state, "
                            "and the orchestrator then advances the object"
                        )
                    elif action != "log_event":
                        errors.append(
                            f"State '{state_name}': unsupported action '{action}' "
                            "(subsystem transitions only support 'log_event')"
                        )
        return errors

    def _validate_object(self, data: Dict[str, Any]) -> List[str]:
        """Validate the object blueprint, cross-checking the subsystems.

        Structural errors first (they would cascade); then the physical
        facts (position + machine layout) and the actors/task chains, so a
        journey that references a missing subsystem or an actor that can
        never report completion is refused before a run can hang.
        """
        errors = self._validate_state_machine(data)
        if errors:
            return errors

        states = data.get("states") or {}
        metadata = data.get("metadata") or {}
        vocabulary = self._shared_vocabulary()
        ready = vocabulary["ready"]
        non_work = vocabulary["non_work"]
        machines = self._subsystem_machines()

        disk_names = [str(d) for d in (metadata.get("disk_names") or [])]
        valid_positions = {"box", "hand", *disk_names}
        journey_events = {
            t.get("event")
            for state_def in states.values()
            if isinstance(state_def, dict)
            for t in state_def.get("transitions") or []
            if isinstance(t, dict)
        }
        # Events of every subsystem (for rotate commands / next_command).
        all_subsystem_events = set()
        for states_map in machines.values():
            all_subsystem_events |= self._subsystem_events(states_map)

        for state_name, state_def in states.items():
            if not isinstance(state_def, dict):
                continue
            meta = state_def.get("meta") or {}
            position = meta.get("position")
            if not position:
                errors.append(f"State '{state_name}': meta.position is required")
            elif position not in valid_positions:
                errors.append(
                    f"State '{state_name}': position '{position}' must be 'box', "
                    f"'hand' or one of the disk names {disk_names}"
                )

            actors = []
            if meta.get("actor"):
                actors.append(str(meta["actor"]))
            actors.extend(str(a) for a in (meta.get("parallel_actors") or []))
            for actor in actors:
                if actor not in machines:
                    errors.append(
                        f"State '{state_name}': actor '{actor}' is not a configured subsystem"
                    )
                elif not self._actor_can_complete(actor, machines, non_work, ready):
                    errors.append(
                        f"State '{state_name}': actor '{actor}' has no work state that "
                        f"returns to '{ready}' – the object would wait for it forever "
                        "(fix the subsystem's state machine)"
                    )

            op = meta.get("actor_op")
            if op:
                actor = str(meta.get("actor") or "")
                chain_ops = {
                    entry.get("op")
                    for entry in (metadata.get("task_chains") or {}).get(actor, []) or []
                    if isinstance(entry, dict)
                }
                if op not in chain_ops:
                    errors.append(
                        f"State '{state_name}': actor_op '{op}' is not a task_chain "
                        f"op of '{actor}'"
                    )

            command = meta.get("rotate_command")
            if command and command not in all_subsystem_events:
                errors.append(
                    f"State '{state_name}': rotate_command '{command}' is not an "
                    "event of any configured subsystem"
                )

        for subsystem, entries in (metadata.get("task_chains") or {}).items():
            if subsystem not in machines:
                errors.append(
                    f"task_chains: '{subsystem}' is not a configured subsystem"
                )
            for entry in entries or []:
                if not isinstance(entry, dict):
                    errors.append(
                        f"task_chains['{subsystem}']: every entry must be a mapping"
                    )
                    continue
                for key in ("object_event", "done_event"):
                    event = entry.get(key)
                    if event and event not in journey_events:
                        errors.append(
                            f"task_chains['{subsystem}'].{key} '{event}' is not an "
                            "object transition event"
                        )
                command = entry.get("next_command")
                if command and command not in all_subsystem_events:
                    errors.append(
                        f"task_chains['{subsystem}'].next_command '{command}' is not "
                        "an event of any configured subsystem"
                    )
                releases = entry.get("releases")
                if releases and releases not in disk_names:
                    errors.append(
                        f"task_chains['{subsystem}'].releases '{releases}' is not one "
                        f"of the disk names {disk_names}"
                    )
        return errors

    @staticmethod
    def _validate_message_formats(data: Dict[str, Any]) -> List[str]:
        errors: List[str] = []
        if "default" in data and not isinstance(data["default"], dict):
            errors.append("'default' must be a mapping")
        subsystems = data.get("subsystems", {})
        if not isinstance(subsystems, dict):
            errors.append("'subsystems' must be a mapping of overrides")
            return errors
        for name, override in subsystems.items():
            if not isinstance(override, dict):
                errors.append(f"'subsystems.{name}' must be a mapping")
                continue
            for kind in override:
                if kind not in ("command", "hardware_command", "status", "response"):
                    errors.append(
                        f"'subsystems.{name}': unknown format kind '{kind}'"
                    )
        return errors

    @staticmethod
    def _validate_global(data: Dict[str, Any]) -> List[str]:
        errors: List[str] = []
        if "mqtt" in data and not isinstance(data["mqtt"], dict):
            errors.append("'mqtt' must be a mapping")
        if "simulation" in data and not isinstance(data["simulation"], dict):
            errors.append("'simulation' must be a mapping")
        return errors

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------
    def save(self, path_str: str, content: str) -> Dict[str, Any]:
        """Validate then atomically write one config file."""
        errors = self.validate_content(path_str, content)
        if errors:
            raise ConfigError("; ".join(errors))
        path = self._resolve(path_str)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(content)
        os.replace(tmp, path)
        logger.info("Config saved through wizard: %s", self._rel(path))
        return {"path": self._rel(path), "saved": True}

    def patch_content(
        self,
        path_str: str,
        content: str,
        patches: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Apply structured ``[{path: [...], value: ...}]`` edits to ``content``.

        The lines are edited in place (comments and untouched formatting are
        preserved); the result is validated before being returned. Nothing
        is written to disk here – the wizard keeps the patched text as its
        unsaved buffer and saves it (all files together) in the review step.
        """
        path = self._resolve(path_str)
        lines = content.splitlines()
        for patch in patches or []:
            key_path = [str(p) for p in (patch.get("path") or [])]
            if not key_path:
                raise ConfigError("Every patch needs a 'path'")
            _apply_patch(lines, key_path, patch.get("value"))
        patched = "\n".join(lines) + "\n"
        errors = self.validate_content(path_str, patched)
        if errors:
            raise ConfigError("; ".join(errors))
        return {
            "path": self._rel(path),
            "content": patched,
            # Keep the structured views in sync with the patched buffer.
            "parsed": self._parse(patched),
            "errors": [],
        }

    def render_state_machine(
        self,
        path_str: str,
        content: str,
        states: Dict[str, Any],
        initial_state: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Rebuild a config file's state machine from the structured editor.

        The candidate document (header + new ``states:`` + optional new
        ``initial_state``) is validated as a whole before the regenerated
        text is returned; nothing is written to disk. The response carries
        ``state_machine: true`` so the wizard can tell this patch kind from
        a generic one.
        """
        path = self._resolve(path_str)
        if not isinstance(states, dict) or not states:
            raise ConfigError("State machine must define at least one state")
        try:
            data = yaml.safe_load(content)
        except yaml.YAMLError as exc:
            raise ConfigError(f"YAML parse error: {exc}")
        if not isinstance(data, dict):
            raise ConfigError("Top-level configuration must be a mapping")

        data["states"] = states
        if initial_state is not None:
            data["initial_state"] = initial_state

        category = self._category(path)
        if category == "subsystem":
            errors = self._validate_subsystem(data)
        elif category == "object":
            errors = self._validate_object(data)
        else:
            errors = self._validate_state_machine(data)
        if errors:
            raise ConfigError("; ".join(errors))

        new_content = _replace_states_block(content, states, initial_state)
        reparsed = yaml.safe_load(new_content)
        if reparsed != data:
            raise ConfigError(
                "State-machine rendering changed the document unexpectedly"
            )
        return {
            "path": self._rel(path),
            "content": new_content,
            "parsed": reparsed,
            "state_machine": True,
            "errors": [],
        }

    # ------------------------------------------------------------------
    # Subsystems: add / remove
    # ------------------------------------------------------------------
    def create_subsystem(self, spec: Dict[str, Any]) -> Dict[str, Any]:
        """Render (but do not write) a new subsystem YAML from a spec.

        The wizard treats it like any other edited file: the content is
        staged locally and saved together with the other changes.
        """
        name = str(spec.get("name") or "").strip()
        if not SUBSYSTEM_NAME_RE.match(name):
            raise ConfigError(
                "Subsystem name must be lower_snake_case (letters, digits, underscore)"
            )
        path = self.config_dir / "subsystems" / f"{name}.yaml"
        if path.exists():
            raise ConfigError(f"Subsystem '{name}' already exists")

        display_name = str(spec.get("display_name") or name).strip()
        work_state = str(spec.get("work_state") or "working").strip()
        work_event = str(spec.get("work_event") or "start").strip()
        if not re.match(r"^[a-z][a-z0-9_]*$", work_state):
            raise ConfigError("Work state must be lower_snake_case")
        if not work_event:
            raise ConfigError("Work event must not be empty")

        init_duration = spec.get("init_duration", 1.5)
        work_durations: Dict[str, float] = {}
        raw_durations = spec.get("work_durations") or {}
        if isinstance(raw_durations, dict):
            for state, seconds in raw_durations.items():
                try:
                    work_durations[str(state)] = float(seconds)
                except (TypeError, ValueError):
                    raise ConfigError(
                        f"Work duration for '{state}' must be a number"
                    )
        if work_state not in work_durations:
            work_durations[work_state] = 2.0

        header = (
            f"# {display_name} subsystem configuration\n"
            "# Created with the configuration wizard (debug mode).\n"
        )
        body: Dict[str, Any] = {
            "name": name,
            "display_name": display_name,
            "type": str(spec.get("type") or "generic"),
            "mqtt_topic": str(
                spec.get("mqtt_topic") or "automata/subsystem/{name}"
            ),
            "initial_state": "initializing",
            "required": bool(spec.get("required", True)),
            "metadata": {
                "description": str(spec.get("description") or f"{display_name} subsystem"),
            },
        }
        if spec.get("hardware"):
            body["metadata"]["hardware"] = str(spec["hardware"])
        body["metadata"]["init_duration"] = float(init_duration)
        body["metadata"]["work_durations"] = work_durations

        yaml_body = yaml.safe_dump(
            body, sort_keys=False, allow_unicode=True, default_flow_style=False
        )
        state_machine = _SUBSYSTEM_STATE_TEMPLATE.format(
            display_name=display_name,
            work_state=work_state,
            work_event=work_event,
        )
        # Discoverability note for the real-interface scripts (R11): the
        # block itself stays commented out so a new subsystem is inert
        # until the operator fills it in (or uses the wizard's Basics tab).
        interface_note = (
            "\n# Real-interface scripts (docs/CONFIG_CHANGE_GUIDE.md, R11):\n"
            "# uncomment under metadata: (or edit in the wizard's Basics tab):\n"
            "#   interface:\n"
            "#     enabled: true\n"
            "#     startup: interfaces/start_{name}.sh   # shell command, headless\n"
            "#     shutdown: interfaces/stop_{name}.sh   # optional\n"
            "# The orchestrator runs 'startup' when the interface is needed\n"
            "# (orchestrator start, wake, SIM->REAL) and 'shutdown' when the\n"
            "# subsystem reports stopped; output goes to\n"
            "# backend/interface_logs/{name}.log.\n"
        ).format(name=name)
        content = header + yaml_body + state_machine + interface_note

        rel = self._rel(path)
        errors = self.validate_content(rel, content)
        if errors:
            raise ConfigError("Generated subsystem is invalid: " + "; ".join(errors))
        return {
            "path": rel,
            "content": content,
            "name": name,
            # Parsed copy so the wizard can render the structured forms of
            # the new file before it has ever been saved.
            "parsed": yaml.safe_load(content),
            "sim_exists": (self.simulation_dir / f"{name}_sim.py").exists(),
            "errors": [],
        }

    def delete_subsystem(self, name: str, delete_simulator: bool = True) -> Dict[str, Any]:
        """Delete a subsystem YAML (and optionally its simulator script)."""
        # The name is used to build filesystem paths: validate it exactly
        # like create_subsystem so `../..` can never escape the config dir.
        if not SUBSYSTEM_NAME_RE.match(name):
            raise ConfigError(f"Invalid subsystem name '{name}'")
        path = (self.config_dir / "subsystems" / f"{name}.yaml").resolve()
        if not path.exists():
            raise ConfigError(f"Subsystem '{name}' does not exist")
        references = self.subsystem_references(name, exclude=path)
        deleted: List[str] = [self._rel(path)]
        path.unlink()
        sim = self.simulation_dir / f"{name}_sim.py"
        if delete_simulator and sim.exists():
            sim.unlink()
            deleted.append(str(sim.relative_to(self.root_dir)))
        logger.info("Subsystem deleted through wizard: %s", name)
        return {
            "name": name,
            "deleted": deleted,
            "references": references,
        }

    def write_simulator(self, name: str) -> Dict[str, Any]:
        """Write the one-line simulator script for a wizard-created subsystem."""
        if not SUBSYSTEM_NAME_RE.match(name):
            raise ConfigError(f"Invalid subsystem name '{name}'")
        path = self.simulation_dir / f"{name}_sim.py"
        path.write_text(
            "from backend.simulation.subsystem_sim import main\n\n\n"
            'if __name__ == "__main__":\n    main()\n'
        )
        logger.info("Simulator created through wizard: %s", path.name)
        return {"path": str(path.relative_to(self.root_dir)), "saved": True}

    def subsystem_references(
        self, name: str, exclude: Optional[Path] = None
    ) -> List[Dict[str, Any]]:
        """Other config files/lines still referencing a subsystem name.

        Shown before deleting a subsystem so the operator sees what else
        needs updating (e.g. ``object.yaml`` actor declarations).
        """
        pattern = re.compile(rf"(?<![\w/]){re.escape(name)}(?![\w])")
        references: List[Dict[str, Any]] = []
        for path in self._editable_files():
            if exclude is not None and path.resolve() == exclude.resolve():
                continue
            hits: List[Dict[str, Any]] = []
            for lineno, line in enumerate(path.read_text().splitlines(), start=1):
                if pattern.search(line):
                    hits.append({"line": lineno, "text": line.strip()})
                    if len(hits) >= 5:
                        break
            if hits:
                references.append({"path": self._rel(path), "hits": hits})
        return references

    # ------------------------------------------------------------------
    # MQTT message formats
    # ------------------------------------------------------------------
    def render_message_formats(self, formats: Dict[str, Any]) -> Dict[str, Any]:
        """Rebuild ``mqtt_formats.yaml`` from the wizard's structured form.

        The wizard sends the full document (``default`` + ``subsystems``);
        empty strings / empty mappings are dropped so the file only keeps
        meaningful values. The existing comment header is preserved.
        """
        data = self._normalize_formats(formats)
        path = self.config_dir / "mqtt_formats.yaml"
        header = _MQTT_FORMATS_HEADER
        if path.exists():
            existing = path.read_text().splitlines()
            for i, line in enumerate(existing):
                if line.startswith("default:"):
                    preserved = "\n".join(existing[:i]).rstrip()
                    if preserved:
                        header = preserved + "\n"
                    break
        body = yaml.safe_dump(
            data, sort_keys=False, allow_unicode=True, default_flow_style=False
        )
        content = header + body
        rel = self._rel(path)
        errors = self.validate_content(rel, content)
        if errors:
            raise ConfigError("Generated message formats are invalid: " + "; ".join(errors))
        return {"path": rel, "content": content, "errors": []}

    @staticmethod
    def _normalize_formats(formats: Dict[str, Any]) -> Dict[str, Any]:
        def clean(value: Any) -> Any:
            if isinstance(value, dict):
                cleaned = {k: clean(v) for k, v in value.items()}
                return {k: v for k, v in cleaned.items() if v not in ("", None, {})}
            if isinstance(value, str):
                return value.strip()
            return value

        normalized = clean(formats or {})
        normalized.setdefault("default", {})
        if "subsystems" not in normalized:
            normalized["subsystems"] = {}
        return normalized
