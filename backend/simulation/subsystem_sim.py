"""Generic simulated subsystem – a standalone process, MQTT-only.

Each simulated subsystem runs in its own process and talks to the GUI
orchestrator exclusively over MQTT – exactly like a real subsystem would:

* subscribes to  <mqtt_topic>/event   (commands issued by the
  orchestrator's subsystem state machine)
* publishes to   <mqtt_topic>/status  (state reports on every
  state change + a periodic heartbeat)

The state machine is loaded from the very same YAML file the orchestrator
uses (backend/config/subsystems/<name>.yaml), so the simulated subsystem is
behaviourally identical to the real one: same states, same events. Work
durations (how long each working state lasts) are declared in the YAML
metadata (work_durations / init_duration), keeping behaviour in config.

The shared vocabulary (which states are not work stages, the ready state,
the completion events, the pause/resume hardware commands, the MQTT topic
template, the sim timing defaults, the object-ID format) comes from
global.yaml – the same source the orchestrator uses
(docs/CONFIG_CHANGE_GUIDE.md, R1).

Pause / resume: the orchestrator forwards them as hardware commands
(global.yaml states.hardware_commands, ordered [pause, resume]); the
simulator freezes (and resumes) the pending work timer while keeping its
state machine in the working state – as a real frozen robot would.

Reporting roles: when the YAML metadata declares a report (R5) and the
declared `after: {from, to}` transition happens, the simulated subsystem
publishes a response on its /response topic (e.g. the object list after a
box scan).

Usage (one process per subsystem):
    python backend/simulation/robot_first_sim.py --broker localhost --port 1883
    python backend/simulation/vision_sim.py --num-objects 5
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Ensure the project root is importable (backend package).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import yaml  # noqa: E402

from backend.domain.models import GlobalConfig  # noqa: E402
from backend.messaging.message_format import MessageFormatSet  # noqa: E402
from backend.messaging.mqtt_client import MqttClient, TOPIC_SUBSYSTEM  # noqa: E402
from backend.state.state_machine import StateMachineConfig  # noqa: E402
from backend.state.state_machine import StateMachineEngine, StateMachineError  # noqa: E402

logger = logging.getLogger("subsystem_sim")


class SubsystemSimulator:
    """Simulates one subsystem behind the MQTT boundary.

    Mirrors the real subsystem contract:
      - receives commands (events) on <mqtt_topic>/event
      - reports its state on <mqtt_topic>/status
    """

    def __init__(
        self,
        name: str,
        broker: str = "localhost",
        port: int = 1883,
        config_dir: Optional[str] = None,
        num_objects: int = 0,
    ) -> None:
        self.name = name
        self.num_objects = num_objects

        yaml_path = Path(config_dir) if config_dir else (
            _PROJECT_ROOT / "backend" / "config" / "subsystems"
        )
        self.yaml_path = yaml_path / f"{name}.yaml"
        if not self.yaml_path.exists():
            raise FileNotFoundError(f"No subsystem YAML at {self.yaml_path}")

        # Same YAML, same engine as the orchestrator: behaviour is identical.
        self.config: StateMachineConfig = StateMachineConfig.from_yaml(self.yaml_path)
        self.sm = StateMachineEngine(self.yaml_path, config_name=f"sim:{name}")

        raw = yaml.safe_load(self.yaml_path.read_text()) or {}
        metadata = raw.get("metadata") or {}

        # Shared vocabulary + sim defaults from global.yaml (R1): the sim
        # and the orchestrator must agree on what a state name means.
        global_path = yaml_path.parent / "global.yaml"
        self.global_config = (
            GlobalConfig.from_yaml(global_path) if global_path.exists()
            else GlobalConfig()
        )
        self._non_work: frozenset = self.global_config.non_work_states
        self._ready_state: str = self.global_config.states_ready
        self._report_events: frozenset = self.global_config.report_events
        # Ordered: [pause, resume] (global.yaml states.hardware_commands)
        self._hardware_commands: List[str] = list(
            self.global_config.states_hardware_commands
        )
        self._object_id_format: str = (
            self.global_config.simulation_object_id_format
        )

        # Reporting role (R5): responses published on /response.
        self._reports: List[Dict] = list(metadata.get("reports") or [])

        # MQTT payload formats (mqtt_formats.yaml, R10): the simulator must
        # speak exactly the shape the orchestrator parses/builds.
        self._format = MessageFormatSet.load(yaml_path.parent).for_subsystem(name)

        self.work_durations: Dict[str, float] = {
            str(k): float(v) for k, v in (metadata.get("work_durations") or {}).items()
        }
        self.init_duration = float(
            metadata.get(
                "init_duration",
                self.global_config.simulation_default_init_duration,
            )
        )

        self.mqtt = MqttClient(broker=broker, port=port)
        self.mqtt._on_message = self._on_event_message
        # Topics from the per-YAML mqtt_topic (R4); the global prefix is
        # the default when the YAML does not override it.
        topic_template = raw.get("mqtt_topic") or (
            f"{TOPIC_SUBSYSTEM}/{{name}}"
        )
        prefix = topic_template.replace("{name}", name)
        self._event_topic = f"{prefix}/event"
        self._status_topic = f"{prefix}/status"
        self._response_topic = f"{prefix}/response"

        self._running = False
        self._paused = False
        self._lock = threading.Lock()
        self._timer: Optional[threading.Timer] = None
        self._deadline: Optional[float] = None
        self._pending: Optional[Tuple[str, str]] = None  # (event, state)
        # The object the last received command belongs to (commands carry
        # object_id, R12): points reports reference it when the report is
        # about the object being worked on (e.g. the AI points of interest).
        self._last_object_id: Optional[str] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        self._running = True
        self.mqtt.connect()
        self.mqtt.subscribe(self._event_topic, qos=1)
        self._publish_status()
        threading.Thread(
            target=self._heartbeat_loop, name=f"sim-hb-{self.name}", daemon=True
        ).start()
        # Self-init like real hardware: leave the initial state for the
        # ready state after the boot time. The transition event is derived
        # from the YAML (initial -> ready), not hardcoded.
        init_event = self.config.event_to(self.sm.current_state, self._ready_state)
        if init_event:
            self._schedule(init_event, self.init_duration, self.sm.current_state)
        else:
            logger.warning(
                "[%s] no transition from initial state '%s' to the ready "
                "state '%s' – the sim will not self-initialize",
                self.name, self.sm.current_state, self._ready_state,
            )
        logger.info(
            "[%s] simulated subsystem started (state: %s, work durations: %s)",
            self.name, self.sm.current_state,
            self.work_durations or "n/a",
        )

    def run(self) -> None:
        """Block until SIGINT/SIGTERM, then shut down cleanly."""
        stopped = threading.Event()

        def _signal_handler(signum: int, _frame: object) -> None:
            logger.info("[%s] signal %d received, shutting down", self.name, signum)
            stopped.set()

        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)
        while not stopped.is_set():
            time.sleep(0.2)
        self.stop()

    def stop(self) -> None:
        self._running = False
        with self._lock:
            self._cancel_timer_locked()
        self.mqtt.disconnect()
        logger.info("[%s] simulated subsystem stopped", self.name)

    # ------------------------------------------------------------------
    # MQTT: incoming commands
    # ------------------------------------------------------------------
    def _on_event_message(self, _topic: str, payload: bytes) -> None:
        try:
            data = json.loads(payload.decode())
        except (json.JSONDecodeError, AttributeError):
            return
        event = self._format.command.extract_event(data)
        if not event:
            return
        # Object-driven commands carry the object they belong to (R12):
        # remembered so the points reports reference the right object.
        object_id = self._format.command.extract_object_id(data)
        if object_id:
            self._last_object_id = object_id
        logger.info(
            "[%s] command '%s' received%s",
            self.name, event, f" (object: {object_id})" if object_id else "",
        )
        # Raw hardware commands (ordered [pause, resume], from global.yaml):
        # they bypass the state machine (it does not model pause).
        if self._hardware_commands and event == self._hardware_commands[0]:
            self._pause()
            return
        if len(self._hardware_commands) > 1 and event == self._hardware_commands[1]:
            self._resume()
            return
        if not self._running:
            return
        self._transition(event)

    # ------------------------------------------------------------------
    # State machine + work timing
    # ------------------------------------------------------------------
    def _transition(self, event: str) -> None:
        prev_state = self.sm.current_state
        try:
            new_state = self.sm.fire(event)
        except StateMachineError as exc:
            logger.warning(
                "[%s] event '%s' rejected in state '%s': %s",
                self.name, event, prev_state, exc,
            )
            return
        logger.info("[%s] %s -> %s (event: %s)", self.name, prev_state, new_state, event)
        self._after_state_change(prev_state, new_state)
        # Leaving a work state (stop, error, ...) cancels the paused flag:
        # pausing only makes sense while work is in progress.
        if new_state in self._non_work:
            self._paused = False
        self._publish_status()
        # Enter a work stage: schedule its completion after the configured
        # duration (pause freezes the timer). A work state without a
        # configured duration falls back to the global default instead of
        # never completing.
        if new_state not in self._non_work:
            duration = self.work_durations.get(
                new_state, self.global_config.simulation_default_work_duration
            )
            complete_event = self.config.event_to(new_state, self._ready_state)
            if complete_event:
                self._schedule(complete_event, duration, new_state)
            else:
                logger.warning(
                    "[%s] work state '%s' has no transition to the ready "
                    "state – it will not self-complete",
                    self.name, new_state,
                )

    def _after_state_change(self, prev_state: str, new_state: str) -> None:
        """Hook for subsystem-specific reporting behaviour.

        Generic (R5): for each report declared in the YAML metadata,
        publish it on /response when the declared `after` transition
        happens. Reports with a `points:` section (R12) publish the
        object's geometry instead of an object list: one message per
        discovered object when the report declares `object_ids`, else one
        message for the object of the last received command.
        """
        for rep in self._reports:
            after = rep.get("after") or {}
            if (
                after.get("from") != prev_state
                or after.get("to") != new_state
            ):
                continue
            ids_cfg = rep.get("object_ids") or {}
            object_ids: List[str] = []
            if ids_cfg.get("count_source") == "num_objects" and self.num_objects > 0:
                fmt = ids_cfg.get("format", self._object_id_format)
                object_ids = [fmt.format(i=i) for i in range(1, self.num_objects + 1)]

            if isinstance(rep.get("points"), dict):
                self._publish_points_report(rep, object_ids)
                continue

            logger.info(
                "[%s] reporting %d objects ('%s')",
                self.name, len(object_ids), rep.get("type"),
            )
            self.mqtt.publish(
                self._response_topic,
                self._format.response.build(
                    rep.get("type", "ok"), object_ids, len(object_ids)
                ),
                qos=1,
            )

    def _publish_points_report(self, rep: Dict, object_ids: List[str]) -> None:
        """Publish the fake geometry of a points report (R12).

        Real producers send their actual measurements; the simulated ones
        generate deterministic placeholder coordinates. The number of
        points is whatever this producer reports: with a declared `count`
        it is fixed, otherwise it is derived per object (R13b) — the
        downstream flow adapts to the received data.
        """
        points_cfg = rep.get("points") or {}
        report_type = str(rep.get("type") or "object_points")
        targets = object_ids or (
            [self._last_object_id] if self._last_object_id else []
        )
        if not targets:
            logger.warning(
                "[%s] points report '%s' has no object to reference "
                "(the triggering command carried no object id)",
                self.name, report_type,
            )
            return
        for oid in targets:
            coords = self._fake_points(oid, points_cfg)
            logger.info(
                "[%s] reporting %d point(s) for object '%s' ('%s')",
                self.name, len(coords), oid, report_type,
            )
            self.mqtt.publish(
                self._response_topic,
                self._format.response.build_points(report_type, oid, coords),
                qos=1,
            )

    def _fake_points(self, object_id: str, points_cfg: Dict) -> List[Any]:
        """Deterministic placeholder geometry for one object (R12).

        Stable per object id so a run can be replayed and compared;
        ``dimensions`` 3 renders [x, y, z], 6 renders [x, y, z, r, p, y].

        - ``count`` declared: exactly that many points, distributed
          round-robin over the ``labels`` vocabulary (labeled objects) or
          plain coordinate lists when no labels are declared.
        - ``count`` omitted (R13b): a dynamic number of points per
          declared label (1..2, stable per object id) — the number of
          points is whatever the producer reports, and the dispatch
          adapts to it.
        """
        digits = int("".join(c for c in object_id if c.isdigit())[-3:] or 0)
        dimensions = int(points_cfg.get("dimensions") or 3)
        labels = [str(l) for l in (points_cfg.get("labels") or [])]

        def _coords(i: int) -> List[float]:
            x = round(0.10 + 0.001 * ((digits + 17 * i) % 100), 4)
            y = round(0.20 + 0.001 * ((digits * 3 + 11 * i) % 100), 4)
            z = round(0.05 + 0.001 * ((digits + 7 * i) % 50), 4)
            if dimensions >= 6:
                yaw = round(
                    ((digits * 13 + 45 * i) % 360) * 3.14159265 / 180.0, 4
                )
                return [x, y, z, 0.0, 0.0, yaw]
            return [x, y, z]

        count_cfg = points_cfg.get("count")
        points: List[Any] = []
        if count_cfg is not None:
            total = max(1, int(count_cfg))
            for i in range(total):
                if labels:
                    points.append(
                        {"label": labels[i % len(labels)], "coordinates": _coords(i)}
                    )
                else:
                    points.append(_coords(i))
            return points

        # Dynamic mode: a stable per-object count per declared label.
        i = 0
        for idx, label in enumerate(labels):
            for _ in range(1 + (digits + 7 * idx) % 2):
                points.append({"label": label, "coordinates": _coords(i)})
                i += 1
        if not labels:
            points = [_coords(i) for i in range(1 + digits % 2)]
        return points

    # ------------------------------------------------------------------
    # Work timer (pause-aware)
    # ------------------------------------------------------------------
    def _schedule(self, event: str, delay: float, state: str) -> None:
        with self._lock:
            if not self._running:
                return
            self._cancel_timer_locked()
            self._pending = (event, state)
            self._deadline = time.monotonic() + max(0.0, delay)
            self._start_timer_locked(self._remaining_locked())

    def _start_timer_locked(self, delay: float) -> None:
        self._timer = threading.Timer(delay, self._fire_scheduled)
        self._timer.daemon = True
        self._timer.start()

    def _remaining_locked(self) -> float:
        if self._deadline is None:
            return 0.0
        return max(0.0, self._deadline - time.monotonic())

    def _cancel_timer_locked(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _fire_scheduled(self) -> None:
        with self._lock:
            if not self._running:
                return
            pending = self._pending
            self._pending = None
            self._deadline = None
            self._timer = None
        if pending is None:
            return
        event, state = pending
        if self.sm.current_state != state:
            logger.debug(
                "[%s] skipping scheduled '%s' (state changed to '%s')",
                self.name, event, self.sm.current_state,
            )
            return
        self._transition(event)

    def _pause(self) -> None:
        with self._lock:
            self._paused = True
            if self._pending is not None:
                self._cancel_timer_locked()
        self._publish_status()
        logger.info("[%s] work paused (state: %s)", self.name, self.sm.current_state)

    def _resume(self) -> None:
        with self._lock:
            self._paused = False
            if self._pending is not None and self._timer is None and self._deadline is not None:
                # Re-anchor the deadline: keep the remaining work duration.
                remaining = self._remaining_locked()
                self._deadline = time.monotonic() + remaining
                self._start_timer_locked(remaining)
        self._publish_status()
        logger.info("[%s] work resumed (state: %s)", self.name, self.sm.current_state)

    # ------------------------------------------------------------------
    # MQTT: outgoing status
    # ------------------------------------------------------------------
    def _publish_status(self) -> None:
        self.mqtt.publish(
            self._status_topic,
            self._format.status.build(
                state=self.sm.current_state,
                name=self.name,
                ready=self.sm.current_state == self._ready_state,
                paused=self._paused,
            ),
            qos=1,
        )

    def _heartbeat_loop(self) -> None:
        while self._running:
            time.sleep(self.global_config.simulation_heartbeat_interval)
            if self._running:
                self._publish_status()


# ---------------------------------------------------------------------------
# Entry point (used by the per-subsystem scripts)
# ---------------------------------------------------------------------------
def main(name: Optional[str] = None, default_num_objects: Optional[int] = None) -> None:
    # The per-subsystem one-liners call main() with no arguments: the name
    # comes from the script's own file name (<name>_sim.py), the discovery
    # convention the orchestrator already relies on (R8).
    if name is None:
        script = Path(sys.argv[0]).name
        name = script[:-len("_sim.py")] if script.endswith("_sim.py") else script
        if not name:
            raise SystemExit(
                "Cannot derive the subsystem name from the script name; "
                "pass it explicitly: main('<name>')"
            )
    parser = argparse.ArgumentParser(
        description=f"Simulated '{name}' subsystem (standalone MQTT process)"
    )
    parser.add_argument("--broker", default="localhost", help="MQTT broker hostname")
    parser.add_argument("--port", type=int, default=1883, help="MQTT broker port")
    parser.add_argument(
        "--config-dir",
        default=str(_PROJECT_ROOT / "backend" / "config" / "subsystems"),
        help="Directory containing the subsystem YAML files",
    )
    parser.add_argument(
        "--num-objects",
        type=int,
        default=None,
        help=(
            "Objects to report after a box scan (subsystems whose YAML "
            "declares a report with count_source: num_objects)"
        ),
    )
    args = parser.parse_args()

    # --num-objects default from global.yaml (simulation.default_num_objects)
    if args.num_objects is None:
        global_path = Path(args.config_dir).parent / "global.yaml"
        args.num_objects = (
            GlobalConfig.from_yaml(global_path).simulation_default_num_objects
            if global_path.exists()
            else 0
        )
    elif default_num_objects is not None and args.num_objects == 0:
        args.num_objects = default_num_objects

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    sim = SubsystemSimulator(
        name=name,
        broker=args.broker,
        port=args.port,
        config_dir=args.config_dir,
        num_objects=args.num_objects,
    )
    sim.start()
    sim.run()


if __name__ == "__main__":
    main()
