"""Generic simulated subsystem – a standalone process, MQTT-only.

Each simulated subsystem runs in its own process and talks to the GUI
orchestrator exclusively over MQTT – exactly like a real subsystem would:

* subscribes to  automata/subsystem/<name>/event   (commands issued by the
  orchestrator's subsystem state machine)
* publishes to   automata/subsystem/<name>/status  (state reports on every
  state change + a periodic heartbeat)

The state machine is loaded from the very same YAML file the orchestrator
uses (backend/config/subsystems/<name>.yaml), so the simulated subsystem is
behaviourally identical to the real one: same states, same events. Work
durations (how long each working state lasts) are declared in the YAML
metadata (work_durations / init_duration), keeping behaviour in config.

Pause / resume: the orchestrator forwards them as hardware commands; the
simulator freezes (and resumes) the pending work timer while keeping its
state machine in the working state – as a real frozen robot would.

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
from typing import Dict, Optional, Tuple

# Ensure the project root is importable (backend package).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import yaml  # noqa: E402

from backend.messaging.mqtt_client import MqttClient, TOPIC_SUBSYSTEM  # noqa: E402
from backend.state.state_machine import StateMachineConfig  # noqa: E402
from backend.state.state_machine import StateMachineEngine, StateMachineError  # noqa: E402

logger = logging.getLogger("subsystem_sim")

# States that are not work stages: entering any other state starts a work
# stage whose duration comes from metadata.work_durations.
NON_WORK_STATES = {"initializing", "ready", "stopped", "error"}

DEFAULT_INIT_DURATION = 1.5   # seconds in 'initializing' before 'ready'
DEFAULT_WORK_DURATION = 2.0   # fallback for working states without a duration
HEARTBEAT_INTERVAL = 2.0      # status re-publication period (seconds)


class SubsystemSimulator:
    """Simulates one subsystem behind the MQTT boundary.

    Mirrors the real subsystem contract:
      - receives commands (events) on automata/subsystem/<name>/event
      - reports its state on automata/subsystem/<name>/status
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
        self.work_durations: Dict[str, float] = {
            str(k): float(v) for k, v in (metadata.get("work_durations") or {}).items()
        }
        self.init_duration = float(metadata.get("init_duration", DEFAULT_INIT_DURATION))

        self.mqtt = MqttClient(broker=broker, port=port)
        self.mqtt._on_message = self._on_event_message
        self._event_topic = f"{TOPIC_SUBSYSTEM}/{name}/event"
        self._status_topic = f"{TOPIC_SUBSYSTEM}/{name}/status"
        self._response_topic = f"{TOPIC_SUBSYSTEM}/{name}/response"

        self._running = False
        self._paused = False
        self._lock = threading.Lock()
        self._timer: Optional[threading.Timer] = None
        self._deadline: Optional[float] = None
        self._pending: Optional[Tuple[str, str]] = None  # (event, state)

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
        # Self-init like real hardware: initializing -> ready after boot time.
        self._schedule("init_complete", self.init_duration, self.sm.current_state)
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
        event = data.get("event")
        if not event:
            return
        logger.info("[%s] command '%s' received", self.name, event)
        if event == "pause":
            self._pause()
            return
        if event == "resume":
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
        if new_state in NON_WORK_STATES:
            self._paused = False
        self._publish_status()
        # Enter a work stage: schedule its completion after the configured
        # duration (pause freezes the timer).
        if new_state not in NON_WORK_STATES and new_state in self.work_durations:
            self._schedule("complete", self.work_durations[new_state], new_state)

    def _after_state_change(self, prev_state: str, new_state: str) -> None:
        """Hook for subsystem-specific reporting behaviour."""
        # Vision: after a box scan completes, report the detected objects
        # (IDs + analysis order) to the orchestrator.
        if (
            self.name == "vision"
            and prev_state == "capturing"
            and new_state == "ready"
        ):
            object_ids = [f"object_{i:03d}" for i in range(1, self.num_objects + 1)]
            logger.info("[%s] reporting %d detected objects", self.name, len(object_ids))
            self.mqtt.publish(
                self._response_topic,
                {
                    "type": "objects_detected",
                    "objects": object_ids,
                    "count": len(object_ids),
                },
                qos=1,
            )

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
            {
                "state": self.sm.current_state,
                "name": self.name,
                "ready": self.sm.current_state == "ready",
                "paused": self._paused,
            },
            qos=1,
        )

    def _heartbeat_loop(self) -> None:
        while self._running:
            time.sleep(HEARTBEAT_INTERVAL)
            if self._running:
                self._publish_status()


# ---------------------------------------------------------------------------
# Entry point (used by the per-subsystem scripts)
# ---------------------------------------------------------------------------
def main(name: str, default_num_objects: int = 0) -> None:
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
        default=default_num_objects,
        help="Objects to report after a box scan (vision only)",
    )
    args = parser.parse_args()

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
    main("generic")
