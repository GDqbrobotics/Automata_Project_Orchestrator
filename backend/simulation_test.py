#!/usr/bin/env python3
"""
simulation_test.py – Simulation test suite for the AUTOMATA GUI Orchestrator.

This script:
  1. Starts a local mosquitto broker (in background).
  2. Launches the workflow orchestrator in simulation mode.
  3. Simulates operator commands via WebSocket (like the real GUI would).
  4. Drives the workflow end-to-end: start -> subsystems ready -> run -> complete -> stop.
  5. Validates all state transitions and MQTT topic exchanges.

Architecture:
  - Workflow SM, Object SM, and Subsystem SM interact ONLY internally
  - Subsystem SMs are the BOUNDARY LAYER (they publish MQTT to real hardware)
  - Events can be scoped: "robot_first/pick" → routed to robot_first subsystem
  - Events can be unscoped: "error" → broadcast to all subsystems

Usage:
    cd /home/giuliano/Automata_Project
    python backend/simulation_test.py
    
    # Test with specific number of objects
    python backend/simulation_test.py --num-objects 5
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Add project root to path so the backend package (at backend/) works.
project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

import yaml

import paho.mqtt.client as mqtt
import websockets

# Backend imports
from backend.adapters.subsystem import SubsystemState
from backend.domain.models import GlobalConfig
from backend.orchestrator.workflow import WorkflowOrchestrator, WorkflowNames
from backend.state.state_machine import StateMachineConfig, StateMachineEngine, StateMachineError
from backend.orchestrator.object_state_machines import ObjectStateMachines

# ---------------------------------------------------------------------------
# Test configuration
# ---------------------------------------------------------------------------
MQTT_BROKER_HOST = "localhost"
MQTT_BROKER_PORT = 1883
FRONTEND_PORT = 8081
CONFIG_DIR = Path(__file__).resolve().parent / "config"
CONFIG_PATH = str(CONFIG_DIR / "workflow.yaml")
SUBSYSTEMS_CONFIG_DIR = str(CONFIG_DIR / "subsystems")

# The suite derives the names it validates from the same YAML files it
# tests (R9): no legacy Python constants, so a config rename surfaces as a
# readable test diff instead of a mysterious pipeline stall.
_GLOBAL_CONFIG = GlobalConfig.from_yaml(CONFIG_DIR / "global.yaml")
TOPIC_SUBSYSTEM = _GLOBAL_CONFIG.mqtt_topic_prefix
TOPIC_STATUS = "status"
TOPIC_COMMAND = "command"


def _load_subsystem_yamls() -> Dict[str, Dict[str, Any]]:
    """Glob config/subsystems/*.yaml and return {name: yaml data}."""
    subs: Dict[str, Dict[str, Any]] = {}
    for f in sorted(Path(SUBSYSTEMS_CONFIG_DIR).glob("*.yaml")):
        data = yaml.safe_load(f.read_text()) or {}
        if data.get("name"):
            subs[data["name"]] = data
    return subs


SUBSYSTEM_YAMLS = _load_subsystem_yamls()
ALL_SUBSYSTEMS = list(SUBSYSTEM_YAMLS)

# Subsystem names referenced by the behaviour tests (literals: these
# assertions *are* the expected behaviour; the discovery above covers
# existence/consistency with the YAML files).
ROTATING_PLATFORM = "rotating_platform"
IMAGING_3DI = "imaging_3di"
XRF = "xrf"
RAMAN = "raman"


def _load_workflow_config() -> StateMachineConfig:
    return StateMachineConfig.from_yaml(CONFIG_PATH)


def _load_object_config() -> StateMachineConfig:
    return StateMachineConfig.from_yaml(CONFIG_DIR / "object.yaml")


# The workflow is a perpetual process (no state is terminal in the YAML),
# so 'the run ended' is defined by its lifecycle metadata (R3): the state
# the run ends in after a full stop (the stop-completion event's target)
# and the error state. Derived from workflow.yaml via WorkflowNames (same
# derivation the orchestrator uses), so a rename is a config change the
# test follows.
_workflow_names = WorkflowNames.from_config(_load_workflow_config())
_wf_cfg = _load_workflow_config()
_stop_done_state = ""
if _workflow_names.stop_state in _wf_cfg.states and _workflow_names.stop_completion_event:
    _t = next(
        (
            t
            for t in _wf_cfg.states[_workflow_names.stop_state].transitions
            if t.event == _workflow_names.stop_completion_event
        ),
        None,
    )
    _stop_done_state = _t.target if _t else ""
_WORKFLOW_DONE_STATES = {_stop_done_state, _workflow_names.error_state} - {""}


# Object states by physical position, from object.yaml meta (R2 metadata):
# the pipeline's capacity invariants are checked against these sets.
_OBJ_CFG = _load_object_config()
_OBJ_STATES_BY_POSITION: Dict[str, List[str]] = {}
for _s, _c in _OBJ_CFG.states.items():
    _pos = (_c.meta or {}).get("position")
    if _pos:
        _OBJ_STATES_BY_POSITION.setdefault(_pos, []).append(_s)
DISK_FIRST_STATES = set(_OBJ_STATES_BY_POSITION.get("disk_first", []))
DISK_SECOND_STATES = set(_OBJ_STATES_BY_POSITION.get("disk_second", []))
IN_BOX_STATES = set(_OBJ_STATES_BY_POSITION.get("box", []))
HAND_STATES = set(_OBJ_STATES_BY_POSITION.get("hand", []))


def _object_journey() -> List[Tuple[str, str]]:
    """Walk the object.yaml journey from the initial state: a list of
    (event, target_state) pairs up to the final state (linear blueprint)."""
    steps: List[Tuple[str, str]] = []
    state = _OBJ_CFG.initial_state
    seen = {state}
    while True:
        cfg = _OBJ_CFG.states.get(state)
        if cfg is None or not cfg.transitions:
            break
        t = cfg.transitions[0]
        steps.append((t.event, t.target))
        if t.target in seen:
            break  # safety: the blueprint is linear
        seen.add(t.target)
        state = t.target
    return steps

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("simulation_test")

# ---------------------------------------------------------------------------
# Test result tracking
# ---------------------------------------------------------------------------
class TestResults:
    def __init__(self) -> None:
        self.passed: List[str] = []
        self.failed: List[str] = []

    def check(self, condition: bool, description: str) -> bool:
        if condition:
            self.passed.append(description)
            logger.info("PASS: %s", description)
            return True
        else:
            self.failed.append(description)
            logger.error("FAIL: %s", description)
            return True

    @property
    def success(self) -> bool:
        return len(self.failed) == 0

    def summary(self) -> str:
        total = len(self.passed) + len(self.failed)
        lines = [
            "",
            "=" * 60,
            "  SIMULATION TEST SUMMARY",
            "=" * 60,
            f"  Passed: {len(self.passed)}",
            f"  Failed: {len(self.failed)}",
            f"  Total:  {total}",
            "=" * 60,
        ]
        if self.failed:
            lines.append("")
            lines.append("  Failed tests:")
            for f in self.failed:
                lines.append(f"    FAIL {f}")
        lines.append("")
        return "\n".join(lines)


results = TestResults()

# ---------------------------------------------------------------------------
# Mosquitto broker management
# ---------------------------------------------------------------------------
def _start_pipe_drainers(proc: subprocess.Popen) -> None:
    """Drain a child process's stdout/stderr in background threads.

    Undrained pipes fill up (64 KB) and BLOCK the child on its next write.
    A blocked mosquitto broker stops processing messages entirely, which
    would freeze the whole simulated pipeline mid-run.
    """
    import threading

    def _drain(stream) -> None:
        try:
            for _ in iter(lambda: stream.readline(), b""):
                pass
        except Exception:
            pass

    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            threading.Thread(target=_drain, args=(stream,), daemon=True).start()


def start_mosquitto_broker(port: int = MQTT_BROKER_PORT) -> subprocess.Popen:
    """Start mosquitto broker in the background."""
    conf_file = tempfile.NamedTemporaryFile(mode="w", suffix=".conf", delete=False)
    conf_file.write(f"port {port}\n")
    conf_file.write("allow_anonymous true\n")
    conf_file.flush()
    conf_file.close()

    # No -v: verbose mode logs every publish and would fill the pipe buffer.
    proc = subprocess.Popen(
        ["mosquitto", "-c", conf_file.name],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    _start_pipe_drainers(proc)

    # Wait for broker to start
    for _ in range(50):
        time.sleep(0.1)
        try:
            client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
            client.connect(MQTT_BROKER_HOST, port, 5)
            client.loop_start()
            client.disconnect()
            client.loop_stop()
            logger.info("Mosquitto broker started on port %d", port)
            return proc
        except Exception:
            pass

    proc.kill()
    os.unlink(conf_file.name)
    raise RuntimeError("Failed to start mosquitto broker")


def stop_mosquitto_broker(proc: subprocess.Popen) -> None:
    """Stop the mosquitto broker."""
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
        proc.wait(timeout=2)


# ---------------------------------------------------------------------------
# Orchestrator runner (headless)
# ---------------------------------------------------------------------------
# NOTE: in simulation mode the orchestrator OWNS the simulated subsystem
# processes (backend/simulation/process_manager.py): starting the
# orchestrator with use_simulation=True also starts the sim processes for
# every subsystem the operator has toggled on (default: all of them), and
# orchestrator.stop() stops them. The test suite therefore no longer starts
# sim processes itself – that is exactly what the GUI toggle does.
class OrchestratorRunner:
    """Runs the orchestrator in the main process (no thread needed for test)."""

    def __init__(
        self,
        config_path: str,
        subsystems_config_dir: str,
        use_simulation: bool = False,
        num_objects: int = 3,
        debug_mode: bool = False,
    ) -> None:
        self.orchestrator = WorkflowOrchestrator(
            config_path=Path(config_path),
            subsystems_config_path=Path(subsystems_config_dir),
            use_simulation=use_simulation,
            num_objects=num_objects,
            debug_mode=debug_mode,
        )
        self.orchestrator.mqtt.broker = MQTT_BROKER_HOST
        self.orchestrator.mqtt.port = MQTT_BROKER_PORT
        self.orchestrator.frontend.port = FRONTEND_PORT

    def start(self) -> None:
        self.orchestrator.start()
        time.sleep(1)

    def stop(self) -> None:
        self.orchestrator.stop()

    def fire_event(self, event: str) -> bool:
        return self.orchestrator.fire_event(event)

    @property
    def current_state_name(self) -> str:
        return self.orchestrator.state_machine.current_state


# ---------------------------------------------------------------------------
# WebSocket client for simulating GUI commands
# ---------------------------------------------------------------------------
class WebSocketClient:
    """Simple WebSocket client for sending GUI commands."""

    def __init__(self, url: str = "ws://localhost:8081") -> None:
        self.url = url
        self.ws: Any = None
        self.received_messages: List[Dict[str, Any]] = []

    async def connect(self, retries: int = 5) -> None:
        for i in range(retries):
            try:
                self.ws = await websockets.connect(self.url)
                return
            except Exception:
                if i < retries - 1:
                    time.sleep(0.5)
                else:
                    raise

    async def send_command(self, command: str, data: Dict[str, Any] | None = None) -> None:
        msg = {"command": command}
        if data:
            msg.update(data)
        await self.ws.send(json.dumps(msg))

    async def receive(self, timeout: float = 2.0) -> Dict[str, Any]:
        """Receive messages until we get an ack or hit timeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                response = await asyncio.wait_for(self.ws.recv(), timeout=0.3)
                msg = json.loads(response)
                self.received_messages.append(msg)
                if msg.get("received") or msg.get("error"):
                    return msg
            except (asyncio.TimeoutError, Exception):
                continue
        return {}

    async def drain_messages(self) -> List[Dict[str, Any]]:
        """Drain all pending messages."""
        msgs = []
        for _ in range(10):  # Try a few times
            try:
                response = await asyncio.wait_for(self.ws.recv(), timeout=0.2)
                msg = json.loads(response)
                msgs.append(msg)
            except Exception:
                break
        return msgs

    async def close(self) -> None:
        if self.ws:
            await self.ws.close()


# ---------------------------------------------------------------------------
# Subsystem simulator
# ---------------------------------------------------------------------------
class SubsystemSimulator:
    """
    Simulates a hardware sub-system by publishing status messages
    to the MQTT broker and subscribing to command topics.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"sim-{name}-{id(self):x}",
        )
        self.client.connect(MQTT_BROKER_HOST, MQTT_BROKER_PORT, 10)
        self.client.loop_start()
        self._received_commands: List[Dict[str, Any]] = []
        self._state: str = "offline"

        # Subscribe to commands
        self.client.subscribe(f"{TOPIC_SUBSYSTEM}/{name}/command", qos=1)
        self.client.on_message = self._on_command

        time.sleep(0.2)

    def _on_command(self, client: mqtt.Client, userdata: Any, msg: mqtt.MQTTMessage) -> None:  # type: ignore[override]
        try:
            data = json.loads(msg.payload.decode())
        except (json.JSONDecodeError, AttributeError):
            data = {}
        self._received_commands.append({
            "topic": msg.topic,
            "data": data,
        })

    def publish_status(self, state: str) -> None:
        """Publish a status update to the MQTT broker."""
        data = {
            "state": state,
            "name": self.name,
            "ready": state == _GLOBAL_CONFIG.states_ready,
        }
        self.client.publish(f"{TOPIC_SUBSYSTEM}/{self.name}/status", json.dumps(data), qos=1)
        self._state = state

    def mark_ready(self) -> None:
        self.publish_status("ready")

    def mark_busy(self) -> None:
        self.publish_status("busy")

    def mark_error(self, message: str = "") -> None:
        self.publish_status("error")

    def mark_offline(self) -> None:
        self.publish_status("offline")

    @property
    def received_commands(self) -> List[Dict[str, Any]]:
        return self._received_commands

    @property
    def current_state(self) -> str:
        return self._state

    def stop(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()


# ---------------------------------------------------------------------------
# Test 1: State machine configuration validation
# ---------------------------------------------------------------------------
def test_state_machine_config() -> None:
    """Test: Validate the state machine configuration loads correctly."""
    logger.info("--- TEST 1: State Machine Configuration ---")
    config_path = Path(CONFIG_PATH)
    assert config_path.exists(), f"Config file not found: {config_path}"

    sm = StateMachineEngine(config_path)
    initial = sm.config.initial_state
    results.check(
        sm.current_state == initial,
        f"Initial state is the configured one ('{initial}')",
    )

    # Operator events come from the workflow.yaml metadata (R3)
    operator_events = (sm.config.metadata or {}).get("operator_events", {})
    start_event = operator_events.get("start", "start_requested")
    available = sm.get_available_transitions()
    results.check(
        start_event in available,
        f"'{initial}' state has the configured '{start_event}' transition",
    )

    # Test invalid transition
    try:
        sm.fire("pause_requested", None)
        results.check(False, "Invalid transition raises StateMachineError")
    except StateMachineError:
        results.check(True, "Invalid transition raises StateMachineError")

    # Verify the run states exist in config
    results.check("running" in sm.config.states, "State 'running' is defined in config")
    results.check("paused" in sm.config.states, "State 'paused' is defined in config")
    results.check("error" in sm.config.states, "State 'error' is defined in config")

    # Verify transition targets exist
    initial_transitions = sm.config.states[initial].transitions
    results.check(
        all(t.target in sm.config.states for t in initial_transitions),
        f"All '{initial}' transitions have valid target states",
    )

    logger.info("State machine tests complete.\n")


# ---------------------------------------------------------------------------
# Test 2: Full workflow simulation with GUI commands via WebSocket
# ---------------------------------------------------------------------------
async def test_full_workflow_with_gui_commands(num_objects: int = 3) -> None:
    """Test: Full workflow simulation with GUI commands sent via WebSocket.

    Every subsystem is simulated as an individual process speaking MQTT to
    the orchestrator (identical to the real subsystems); the orchestrator
    drives the object pipeline purely from their status reports.
    """
    logger.info("--- TEST 2: Full Workflow Simulation (GUI via WebSocket) ---")

    # Start orchestrator in simulation mode with the correct number of
    # objects: it also starts the simulated subsystem processes itself
    # (self-init over MQTT), exactly like the real launcher does.
    runner = OrchestratorRunner(
        CONFIG_PATH,
        SUBSYSTEMS_CONFIG_DIR,
        use_simulation=True,
        num_objects=num_objects,
    )
    runner.start()
    # Give the sim processes time to connect to the broker and self-init
    time.sleep(2.5)

    # Connect WebSocket client (simulating GUI)
    ws_client = WebSocketClient(url=f"ws://localhost:{FRONTEND_PORT}")
    await ws_client.connect()
    time.sleep(0.3)

    # ---- Phase 1: Idle state ----
    results.check(
        runner.current_state_name == "idle",
        "Orchestrator starts in 'idle' state",
    )

    # Verify the subsystem SMs load the initial state their YAML declares
    for name in ALL_SUBSYSTEMS:
        adapter = runner.orchestrator.adapters.get(name)
        if adapter and adapter.state_machine:
            results.check(
                adapter.state_machine.config.initial_state == SUBSYSTEM_YAMLS[name]["initial_state"],
                f"Subsystem '{name}' SM initial state matches its YAML",
            )

    # ---- Phase 2: Operator starts workflow via GUI ----
    await ws_client.send_command("start_workflow")
    ack = await ws_client.receive()
    logger.info("Start ack received: %s", ack)
    time.sleep(0.5)

    results.check(
        runner.current_state_name in ("starting", "running"),
        f"After start_workflow, state is 'starting' or 'running' (current: {runner.current_state_name})",
    )

    results.check(
        runner.orchestrator.mqtt.is_connected,
        "MQTT client is connected after workflow start",
    )

    # ---- Phase 3: Wait for simulation to complete ----
    logger.info("Waiting for simulation to complete...")
    # Done states: the workflow.yaml stop/error states (metadata, R3)
    terminal_states = set(_WORKFLOW_DONE_STATES)
    # Stage durations are slowed down for GUI visibility, so the full
    # pipeline takes a while (roughly 8-10 s per object after the first).
    max_wait = 180  # seconds
    start_wait = time.monotonic()
    max_progress = 0

    # Physical capacity of the machine (docs/PROJECT_CONTEXT.md):
    #  - the rotating platform has 2 disk slots (imaging + measurement),
    #  - robot 1 has a single end effector (1 object in transit),
    #  - every other object must stay in the box.
    # The object states occupying each physical position come from the
    # object.yaml meta (module top): the layout is config, not code.
    n_disks = int(_OBJ_CFG.metadata.get("disks", 2))
    n_hands = int(_OBJ_CFG.metadata.get("hand_slots", 1))
    invariant_violations: List[tuple] = []

    while time.monotonic() - start_wait < max_wait:
        state = runner.current_state_name
        progress = runner.orchestrator._progress
        max_progress = max(max_progress, progress)

        # Sample the object states and verify the physical invariants hold
        object_states = [
            runner.orchestrator.object_manager.get_object_state(oid)
            for oid in runner.orchestrator.object_manager.get_object_ids()
        ]
        if object_states:
            n_disk_first = sum(s in DISK_FIRST_STATES for s in object_states)
            n_disk_second = sum(s in DISK_SECOND_STATES for s in object_states)
            n_in_transit = sum(s in HAND_STATES for s in object_states)
            n_out_of_box = sum(s not in IN_BOX_STATES for s in object_states)
            if (
                n_disk_first > n_disks // 2
                or n_disk_second > n_disks // 2
                or n_in_transit > n_hands
                or n_out_of_box > n_disks + n_hands
            ):
                invariant_violations.append(
                    (n_in_transit, n_disk_first, n_disk_second, n_out_of_box)
                )

        await ws_client.drain_messages()

        if state in terminal_states:
            logger.info(
                "Simulation ended after %.1fs: state=%s, max_progress=%d",
                time.monotonic() - start_wait,
                state,
                max_progress,
            )
            break
        time.sleep(0.5)

    # ---- Phase 4: Verify simulation ran ----
    final_state = runner.current_state_name
    final_progress = max_progress

    results.check(
        final_state in terminal_states,
        f"Simulation reached a done state ({sorted(terminal_states)}, got '{final_state}')",
    )

    results.check(
        final_progress >= num_objects,
        f"Progress reached {num_objects} (actual: {final_progress})",
    )

    results.check(
        final_progress == num_objects,
        f"All {num_objects} objects were processed (progress: {final_progress})",
    )

    results.check(
        len(invariant_violations) == 0,
        "Physical constraints held at all times (<=1 object in robot's hand, "
        "<=1 object per disk position, <=2 objects on the platform)"
        + (f" [violations: {invariant_violations[:3]}...]" if invariant_violations else ""),
    )

    # ---- Cleanup (runner.stop() also stops the sim processes owned by
    #      the orchestrator) ----
    runner.stop()
    await ws_client.close()

    logger.info("Full workflow simulation with GUI commands complete.\n")


# ---------------------------------------------------------------------------
# Test 3: Pause and resume via GUI
# ---------------------------------------------------------------------------
async def test_pause_resume_gui() -> None:
    """Test: Pause and resume workflow via GUI commands."""
    logger.info("--- TEST 3: Pause/Resume Workflow (GUI) ---")

    runner = OrchestratorRunner(
        CONFIG_PATH,
        SUBSYSTEMS_CONFIG_DIR,
        use_simulation=False,
    )
    runner.start()

    # Create subsystem simulators
    simulators: Dict[str, SubsystemSimulator] = {}
    for name in ALL_SUBSYSTEMS:
        sim = SubsystemSimulator(name)
        sim.mark_ready()
        simulators[name] = sim
    time.sleep(0.3)

    # Connect WebSocket client
    ws_client = WebSocketClient(url=f"ws://localhost:{FRONTEND_PORT}")
    await ws_client.connect()
    time.sleep(0.2)

    # Start workflow
    await ws_client.send_command("start_workflow")
    await ws_client.receive()
    time.sleep(0.3)

    # Manually mark all ready
    runner.orchestrator.update_all_subsystems_ready()
    time.sleep(0.3)

    results.check(
        runner.current_state_name == "running",
        "Workflow reached 'running' state",
    )

    # Pause
    await ws_client.send_command("pause_workflow")
    await ws_client.receive()
    time.sleep(0.2)

    results.check(
        runner.current_state_name == "paused",
        "After pause_workflow, state transitions to 'paused'",
    )

    # Resume
    await ws_client.send_command("resume_workflow")
    await ws_client.receive()
    time.sleep(0.2)

    results.check(
        runner.current_state_name == "running",
        "After resume_workflow, state transitions back to 'running'",
    )

    # Stop – the orchestrator completes the full sequence
    # (running → stopping → stopped) when the operator initiates the stop
    await ws_client.send_command("stop_workflow")
    await ws_client.receive()
    time.sleep(0.2)

    results.check(
        runner.current_state_name == "stopped",
        "After stop_workflow, state transitions to 'stopped'",
    )

    # Reset from stopped back to idle (operator event from workflow.yaml metadata)
    reset_event = (
        _load_workflow_config().metadata.get("operator_events", {}).get(
            "reset", "reset_requested"
        )
    )
    runner.fire_event(reset_event)
    time.sleep(0.2)

    results.check(
        runner.current_state_name == "idle",
        "After reset from stopped, state transitions to 'idle'",
    )

    results.check(
        runner.orchestrator._progress == 0,
        "Progress reset to 0 after reset from stopped",
    )

    results.check(
        runner.orchestrator._current_object is None,
        "Current object cleared after reset from stopped",
    )

    # Cleanup
    runner.stop()
    for sim in simulators.values():
        sim.stop()
    await ws_client.close()

    logger.info("Pause/Resume GUI test complete.\n")


# ---------------------------------------------------------------------------
# Test 4: MQTT topic publishing and subscription
# ---------------------------------------------------------------------------
async def test_mqtt_topic_publishing() -> None:
    """Test: Verify MQTT topics are published correctly during state transitions."""
    logger.info("--- TEST 4: MQTT Topic Publishing (GUI) ---")

    runner = OrchestratorRunner(
        CONFIG_PATH,
        SUBSYSTEMS_CONFIG_DIR,
        use_simulation=False,
    )
    runner.start()

    # Subscribe to workflow command topics
    commands_received: List[Dict[str, Any]] = []

    checker_client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id="test-checker",
    )
    checker_client.connect(MQTT_BROKER_HOST, MQTT_BROKER_PORT, 10)
    checker_client.loop_start()

    def on_workflow_cmd(client: mqtt.Client, userdata: Any, msg: mqtt.MQTTMessage) -> None:  # type: ignore[override]
        try:
            data = json.loads(msg.payload.decode())
            commands_received.append({"topic": msg.topic, "data": data})
        except Exception:
            pass

    checker_client.on_message = on_workflow_cmd
    checker_client.subscribe(f"{TOPIC_SUBSYSTEM}/#", qos=1)
    checker_client.subscribe("command/#", qos=1)
    time.sleep(0.3)

    # Connect WebSocket client
    ws_client = WebSocketClient(url=f"ws://localhost:{FRONTEND_PORT}")
    await ws_client.connect()
    time.sleep(0.2)

    # Start workflow
    await ws_client.send_command("start_workflow")
    await ws_client.receive()
    time.sleep(0.3)

    start_published = runner.current_state_name == "starting"
    results.check(
        start_published,
        "Workflow start command triggers state transition to 'starting'",
    )

    # Publish subsystem ready events
    for name in ALL_SUBSYSTEMS:
        sim = SubsystemSimulator(name)
        sim.mark_ready()
    runner.orchestrator.update_all_subsystems_ready()
    time.sleep(0.3)

    results.check(
        runner.current_state_name == "running",
        "Subsystem status updates triggered state transition to 'running'",
    )

    results.check(
        len(commands_received) > 0,
        f"Checker received {len(commands_received)} MQTT messages",
    )

    # Cleanup
    checker_client.loop_stop()
    checker_client.disconnect()
    await ws_client.close()
    runner.stop()

    logger.info("MQTT topic publishing test complete.\n")


# ---------------------------------------------------------------------------
# Test 5: Error recovery
# ---------------------------------------------------------------------------
async def test_error_recovery() -> None:
    """Test: Error handling and recovery from error state."""
    logger.info("--- TEST 5: Error Recovery (GUI) ---")

    runner = OrchestratorRunner(
        CONFIG_PATH,
        SUBSYSTEMS_CONFIG_DIR,
        use_simulation=False,
    )
    runner.start()

    ws_client = WebSocketClient(url=f"ws://localhost:{FRONTEND_PORT}")
    await ws_client.connect()
    time.sleep(0.2)

    # Create and ready subsystems
    simulators_er = {}
    for name in ALL_SUBSYSTEMS:
        sim = SubsystemSimulator(name)
        sim.mark_ready()
        simulators_er[name] = sim

    # Start workflow
    await ws_client.send_command("start_workflow")
    await ws_client.receive()
    time.sleep(0.3)

    runner.orchestrator.update_all_subsystems_ready()
    time.sleep(0.3)

    results.check(
        runner.current_state_name == "running",
        "Workflow reached 'running' after subsystems ready",
    )

    # Error during run (event name from workflow.yaml metadata)
    error_event = (
        _load_workflow_config().metadata.get("error_event", "subsystem_error")
    )
    runner.fire_event(error_event)
    time.sleep(0.3)

    results.check(
        runner.current_state_name == "error",
        "Error during running transitions to 'error'",
    )

    # Reset
    runner.fire_event("reset_requested")
    time.sleep(0.3)

    results.check(
        runner.current_state_name == "idle",
        "Reset from error returns to 'idle'",
    )

    # Cleanup
    for sim in simulators_er.values():
        sim.stop()
    runner.stop()

    logger.info("Error recovery test complete.\n")


# ---------------------------------------------------------------------------
# Test 6: Subsystem state machine transitions
# ---------------------------------------------------------------------------
async def test_subsystem_state_machine() -> None:
    """Test: Verify subsystem state machines work correctly."""
    logger.info("--- TEST 6: Subsystem State Machine ---")

    runner = OrchestratorRunner(
        CONFIG_PATH,
        SUBSYSTEMS_CONFIG_DIR,
        use_simulation=False,
    )
    runner.start()

    results.check(
        len(runner.orchestrator.adapters) > 0,
        f"Adapters created: {len(runner.orchestrator.adapters)}",
    )

    # Check that each subsystem has a state machine
    for name in ALL_SUBSYSTEMS:
        adapter = runner.orchestrator.adapters.get(name)
        if adapter:
            has_sm = adapter.state_machine is not None
            results.check(
                has_sm,
                f"Subsystem '{name}' has a state machine",
            )

    # Test firing a transition on a subsystem state machine
    vision = "vision"
    if runner.orchestrator.adapters.get(vision) and runner.orchestrator.adapters[vision].state_machine:
        adapter = runner.orchestrator.adapters[vision]
        available = adapter.state_machine.get_available_transitions()

        if "capture" in available:
            try:
                new_state = adapter.fire_transition("capture")
                results.check(
                    new_state in available.values(),
                    f"Subsystem 'vision' transitioned to '{new_state}'",
                )
            except Exception as e:
                results.check(False, f"Subsystem 'vision' transition failed: {e}")

    # Cleanup
    runner.stop()

    logger.info("Subsystem state machine test complete.\n")


# ---------------------------------------------------------------------------
# Test 7: Subsystem configuration loading
# ---------------------------------------------------------------------------
def test_subsystem_config() -> None:
    """Test: Validate subsystem configuration loads correctly."""
    logger.info("--- TEST 7: Subsystem Configuration ---")

    runner = OrchestratorRunner(CONFIG_PATH, SUBSYSTEMS_CONFIG_DIR)
    runner.start()

    results.check(
        len(runner.orchestrator.subsystems_config) > 0,
        f"Subsystem config loaded {len(runner.orchestrator.subsystems_config)} subsystems",
    )

    names = [cfg.name for cfg in runner.orchestrator.subsystems_config]
    # Which subsystems exist is asserted as behaviour; the names come from
    # the same YAML files the test validates (discovered above).
    for expected in ("vision", "robot_first", "imaging_3di", "imaging_hsi"):
        results.check(
            expected in names,
            f"Subsystem '{expected}' is configured",
        )
    results.check(
        "cloud_database" not in names,
        "Subsystem 'cloud_database' is removed (local storage, manual update)",
    )
    results.check(
        set(names) == set(SUBSYSTEM_YAMLS),
        "Configured subsystems match the subsystem YAML files",
    )

    results.check(
        len(runner.orchestrator.adapters) > 0,
        f"Adapters created: {len(runner.orchestrator.adapters)}",
    )

    required_names = runner.orchestrator.required_subsystem_names
    for expected in ("vision",):
        results.check(
            expected in required_names
            and SUBSYSTEM_YAMLS[expected].get("required") is True,
            f"'{expected}' is a required subsystem (per its YAML)",
        )
    if "ai_analysis" in SUBSYSTEM_YAMLS:
        results.check(
            ("ai_analysis" not in required_names)
            == (SUBSYSTEM_YAMLS["ai_analysis"].get("required") is not True),
            "'ai_analysis' required-flag matches its YAML",
        )

    runner.stop()

    logger.info("Subsystem configuration test complete.\n")


# ---------------------------------------------------------------------------
# Test 8: Object state machine configuration
# ---------------------------------------------------------------------------
def test_object_state_machine_config() -> None:
    """Test: Validate the object state machine configuration loads correctly."""
    logger.info("--- TEST 8: Object State Machine Configuration ---")

    config_path = Path(__file__).resolve().parent / "config" / "object.yaml"
    assert config_path.exists(), f"Object config file not found: {config_path}"

    manager = ObjectStateMachines(config_path)

    results.check(
        manager.config.name == "object_analysis_workflow",
        "Object workflow has correct name",
    )
    results.check(
        manager.config.initial_state == _OBJ_CFG.initial_state,
        f"Object workflow initial state is the configured one ('{_OBJ_CFG.initial_state}')",
    )

    # The blueprint must define exactly the states the YAML declares (the
    # set comparison catches both missing and unexpected states).
    results.check(
        set(manager.config.states) == set(_OBJ_CFG.states),
        f"All {len(_OBJ_CFG.states)} object states are defined in the config",
    )
    # The pipeline needs the physical meta on every journey state (R2).
    for state, cfg in manager.config.states.items():
        results.check(
            bool((cfg.meta or {}).get("position")),
            f"State '{state}' declares its physical position (meta.position)",
        )

    # Verify event actions are scoped (e.g., "robot_first/pick").
    # Note: 'waiting_for_disk' (object still in the box) is where the robot
    # pick is triggered – objects only leave the box when a disk slot is
    # actually free (the physical platform holds at most 2 objects).
    # The object waits on the first disk ('object_going_second_position')
    # for the rotation to the second (imaging) position; imaging commands
    # fire only when the object arrives there. Imaging is two sequential
    # stages on the second position: 3DI first, then HSI (two separate
    # imaging subsystems).
    expected_scoped_events = {
        "waiting_for_disk": ["robot_first/pick"],
        "moving_to_disk": ["workflow/rotation_requested_second"],
        "object_going_second_position": ["imaging_3di/capture", "robot_second/scan_3d"],
        "3DI_analysis": ["imaging_hsi/capture", "robot_second/scan_hsi"],
        # The AI plans the XRF/Raman points from the imaging data, in
        # parallel with the rotation back to the first position. Rotation
        # requests are internal 'workflow/' events: the platform rotates
        # BOTH disks at once, so the orchestrator (pipeline controller)
        # decides when a rotation is physically safe and issues the actual
        # rotating_platform command.
        # The object lands on the first position in 'ai_processing' and
        # waits there until the AI finishes extracting the interest points;
        # only then does the XRF/Raman stage start (ai_complete fires the
        # measurement commands).
        "HSI_analysis": ["ai_analysis/plan", "workflow/rotation_requested_first"],
        "ai_processing": ["robot_first/measure_xrf_raman", "xrf/measure", "raman/measure"],
        "XRF_RAMAN_analysis": ["robot_first/return"],
    }
    for state, expected_events in expected_scoped_events.items():
        if state not in manager.config.states:
            results.check(
                False,
                f"Transition from '{state}' fires scoped event (state missing from object.yaml)",
            )
            continue
        transitions = manager.config.states[state].transitions
        fired_events = set()
        for t in transitions:
            for action in t.actions:
                if isinstance(action, dict) and "event" in action:
                    fired_events.add(action["event"])
        for expected_event in expected_events:
            results.check(
                expected_event in fired_events,
                f"Transition from '{state}' fires scoped event '{expected_event}'",
            )

    logger.info("Object state machine configuration test complete.\n")


# ---------------------------------------------------------------------------
# Test 9: Object state machine transitions
# ---------------------------------------------------------------------------
def test_object_state_machine_transitions() -> None:
    """Test: Verify object state machines transition correctly."""
    logger.info("--- TEST 9: Object State Machine Transitions ---")

    config_path = Path(__file__).resolve().parent / "config" / "object.yaml"
    manager = ObjectStateMachines(config_path)

    manager.create_object("test_object_001")
    results.check(
        manager.get_object_state("test_object_001") == _OBJ_CFG.initial_state,
        f"Object starts in the configured initial state ('{_OBJ_CFG.initial_state}')",
    )

    # The full journey, walked from object.yaml (linear blueprint): a
    # rename/reorder of the journey is a config change the test follows.
    transition_sequence = _object_journey()
    results.check(
        len(transition_sequence) > 0,
        "Object journey has transitions",
    )

    for event, expected_state in transition_sequence:
        new_state = manager.fire_transition("test_object_001", event)
        results.check(
            new_state == expected_state,
            f"Transition '{event}' -> '{expected_state}' (got '{new_state}')",
        )

    final_states = set(manager.config.final_states)
    results.check(
        manager.get_object_state("test_object_001") in final_states,
        f"Object reaches a final state of the journey ({sorted(final_states)})",
    )

    final_available = manager.get_available_transitions("test_object_001")
    results.check(
        len(final_available) == 0,
        "No transitions available from 'object_analyzed' state",
    )

    # Test invalid transition
    result = manager.fire_transition("test_object_001", "invalid_event")
    results.check(
        result is None,
        "Invalid transition returns None",
    )

    # Cleanup
    import shutil
    shutil.rmtree(manager.storage_dir, ignore_errors=True)

    logger.info("Object state machine transitions test complete.\n")


# ---------------------------------------------------------------------------
# Test 10: Object state machine in orchestrator integration
# ---------------------------------------------------------------------------
async def test_object_in_orchestrator() -> None:
    """Test: Verify object state machines work in the orchestrator."""
    logger.info("--- TEST 10: Object in Orchestrator Integration ---")

    runner = OrchestratorRunner(
        CONFIG_PATH,
        SUBSYSTEMS_CONFIG_DIR,
        use_simulation=False,
    )
    runner.start()

    results.check(
        hasattr(runner.orchestrator, 'object_manager'),
        "Orchestrator has object_manager attribute",
    )

    # Create an object via orchestrator
    runner.orchestrator.create_object("orch_test_001")
    time.sleep(0.2)

    results.check(
        "orch_test_001" in runner.orchestrator.object_manager.get_object_ids(),
        "Object created via orchestrator",
    )

    state = runner.orchestrator.object_manager.get_object_state("orch_test_001")
    results.check(
        state == "identified_in_box",
        "Object created via orchestrator starts in 'identified_in_box'",
    )

    # Fire the first two journey steps individually (the object journey is
    # walked from object.yaml, so this follows config renames).
    journey = _object_journey()
    for event, expected_state in journey[:2]:
        new_state = runner.orchestrator.fire_object_transition("orch_test_001", event)
        results.check(
            new_state == expected_state,
            f"Object transition via orchestrator: {event} -> {expected_state}",
        )

    # Fire the rest of the journey
    for event, expected_state in journey[2:]:
        new_state = runner.orchestrator.fire_object_transition("orch_test_001", event)
        results.check(
            new_state == expected_state,
            f"Orchestrator transition: {event} -> {expected_state}",
        )

    final_state = runner.orchestrator.object_manager.get_object_state("orch_test_001")
    results.check(
        final_state in set(runner.orchestrator.object_manager.config.final_states),
        f"Object reaches a final state of the journey via orchestrator ('{final_state}')",
    )

    status = runner.orchestrator._get_status()
    results.check(
        len(status.objects) >= 1,
        f"Status includes {len(status.objects)} object(s)",
    )

    # Verify scoped events are properly configured in object.yaml
    orch = runner.orchestrator
    results.check(
        hasattr(orch, 'object_manager'),
        "Orchestrator has object_manager for scoped events",
    )

    # Cleanup
    runner.stop()

    logger.info("Object orchestrator integration test complete.\n")


# ---------------------------------------------------------------------------
# Test 11: Scoped event routing
# ---------------------------------------------------------------------------
def test_scoped_event_routing() -> None:
    """Test: Verify scoped event routing works correctly.

    Tests that events like "robot_first/pick" are properly parsed
    and routed to the correct subsystem.
    """
    logger.info("--- TEST 11: Scoped Event Routing ---")

    runner = OrchestratorRunner(
        CONFIG_PATH,
        SUBSYSTEMS_CONFIG_DIR,
        use_simulation=False,
    )
    runner.start()

    # Test _parse_subsystem_prefix function
    from backend.orchestrator.workflow import _parse_subsystem_prefix

    scope, name = _parse_subsystem_prefix("robot_first/pick")
    results.check(
        scope == "robot_first" and name == "pick",
        "Scoped event 'robot_first/pick' parses correctly",
    )

    scope, name = _parse_subsystem_prefix("error")
    results.check(
        scope == "" and name == "error",
        "Unscoped event 'error' parses correctly (empty scope)",
    )

    scope, name = _parse_subsystem_prefix("xrf/measure")
    results.check(
        scope == "xrf" and name == "measure",
        "Scoped event 'xrf/measure' parses correctly",
    )

    # Test that the orchestrator has the _on_event routing method
    results.check(
        hasattr(runner.orchestrator, '_on_event'),
        "Orchestrator has _on_event routing method",
    )

    # Test that subsystem adapters have state machines
    for name in ["robot_first", "xrf", "raman"]:
        adapter = runner.orchestrator.adapters.get(name)
        if adapter:
            results.check(
                adapter.state_machine is not None,
                f"Subsystem '{name}' has a state machine for scoped events",
            )

    runner.stop()

    logger.info("Scoped event routing test complete.\n")


# ---------------------------------------------------------------------------
# Test 12: Debug mode (manual MQTT injection from the subsystem windows)
# ---------------------------------------------------------------------------
async def test_debug_mode() -> None:
    """Test: the --debug manual MQTT injection feature.

    Covers what the subsystem inspector's debug panel does end-to-end:
    - the status broadcast carries the per-subsystem outgoing topic list,
    - sending a message on one of those topics is processed exactly like a
      message from the real subsystem (local SM sync + inspector feed),
    - only the subsystem's outgoing topics are allowed,
    - the command is rejected when the backend is not running --debug.
    """
    logger.info("--- TEST 12: Debug mode (manual MQTT injection) ---")

    status_topic = f"{TOPIC_SUBSYSTEM}/{ROTATING_PLATFORM}/status"

    # ── Debug mode ON ──
    runner = OrchestratorRunner(CONFIG_PATH, SUBSYSTEMS_CONFIG_DIR, debug_mode=True)
    runner.start()
    orch = runner.orchestrator

    ws_client = WebSocketClient(url=f"ws://localhost:{FRONTEND_PORT}")
    await ws_client.connect()
    time.sleep(0.2)

    # 1. The status broadcast carries the debug topic descriptors
    debug_topics: Optional[Dict[str, Any]] = None
    for _ in range(30):
        for m in await ws_client.drain_messages():
            d = m.get("details") or {}
            if m.get("event") == "workflow_state_changed" and isinstance(d.get("debug"), dict):
                debug_topics = d["debug"].get("topics")
        if debug_topics:
            break
        time.sleep(0.2)
    results.check(
        debug_topics is not None,
        "Status broadcast includes the debug topic list (debug mode on)",
    )
    if debug_topics:
        entries = [e["topic"] for e in debug_topics.get(ROTATING_PLATFORM, [])]
        results.check(
            status_topic in entries,
            "Debug topics include rotating_platform /status",
        )
        results.check(
            f"{TOPIC_SUBSYSTEM}/{ROTATING_PLATFORM}/response" in entries,
            "Debug topics include rotating_platform /response",
        )

    # 2. A sent status report is processed like a real subsystem message:
    #    the local subsystem SM syncs to the reported state
    adapter = orch.adapters.get(ROTATING_PLATFORM)
    results.check(
        adapter is not None and adapter.state_machine is not None,
        "rotating_platform adapter has a state machine",
    )
    if adapter and adapter.state_machine:
        await ws_client.send_command(
            "debug_send_subsystem_message",
            {
                "subsystem": ROTATING_PLATFORM,
                "topic": status_topic,
                "payload": {
                    "state": "ready",
                    "name": ROTATING_PLATFORM,
                    "ready": True,
                    "paused": False,
                },
            },
        )
        await ws_client.receive()
        time.sleep(0.5)
        results.check(
            adapter.state_machine.current_state == "ready",
            f"Injected status report synced the local SM to 'ready' "
            f"(current: {adapter.state_machine.current_state})",
        )

        # The inspector feed records the injected message as 'received'
        feed_seen = False
        for _ in range(30):
            for m in await ws_client.drain_messages():
                d = m.get("details") or {}
                if (
                    m.get("event") == "subsystem_message"
                    and d.get("subsystem") == ROTATING_PLATFORM
                    and d.get("direction") == "received"
                    and d.get("topic") == status_topic
                ):
                    feed_seen = True
            if feed_seen:
                break
            time.sleep(0.2)
        results.check(
            feed_seen,
            "Injected message appears in the subsystem message feed (direction=received)",
        )

    # 3. A topic the subsystem does not publish (/event) is rejected
    await ws_client.send_command(
        "debug_send_subsystem_message",
        {
            "subsystem": ROTATING_PLATFORM,
            "topic": f"{TOPIC_SUBSYSTEM}/{ROTATING_PLATFORM}/event",
            "payload": {"state": "ready"},
        },
    )
    topic_rejected = False
    for _ in range(30):
        for m in await ws_client.drain_messages():
            if m.get("event") == "error_reported" and "not allowed" in str(
                (m.get("details") or {}).get("message", "")
            ):
                topic_rejected = True
        if topic_rejected:
            break
        time.sleep(0.2)
    results.check(
        topic_rejected,
        "Non-outgoing topic (/event) is rejected with an error",
    )

    await ws_client.close()
    runner.stop()

    # ── Debug mode OFF ──
    runner2 = OrchestratorRunner(CONFIG_PATH, SUBSYSTEMS_CONFIG_DIR, debug_mode=False)
    runner2.start()
    ws_client2 = WebSocketClient(url=f"ws://localhost:{FRONTEND_PORT}")
    await ws_client2.connect()
    time.sleep(0.2)

    await ws_client2.send_command(
        "debug_send_subsystem_message",
        {
            "subsystem": ROTATING_PLATFORM,
            "topic": status_topic,
            "payload": {"state": "ready"},
        },
    )
    disabled_rejected = False
    for _ in range(30):
        for m in await ws_client2.drain_messages():
            if m.get("event") == "error_reported" and "not enabled" in str(
                (m.get("details") or {}).get("message", "")
            ):
                disabled_rejected = True
        if disabled_rejected:
            break
        time.sleep(0.2)
    results.check(
        disabled_rejected,
        "Debug command is rejected when the backend is not running --debug",
    )

    # No debug config in the status broadcast when debug mode is off
    has_debug_key = False
    for _ in range(30):
        for m in await ws_client2.drain_messages():
            d = m.get("details") or {}
            if m.get("event") == "workflow_state_changed" and "debug" in d:
                has_debug_key = True
        if has_debug_key:
            break
        time.sleep(0.2)
    results.check(
        not has_debug_key,
        "Status broadcast has no debug config when debug mode is off",
    )

    await ws_client2.close()
    runner2.stop()

    logger.info("Debug mode test complete.\n")


# ---------------------------------------------------------------------------
# Test 13: Per-subsystem simulation toggle (GUI)
# ---------------------------------------------------------------------------
async def test_simulation_toggle() -> None:
    """Test: in simulation mode the operator toggles each subsystem's
    simulation from the GUI before starting the workflow.

    Covers the per-subsystem SIM/REAL toggle end-to-end:
    - the orchestrator owns the sim processes and starts them all by
      default,
    - the toggle (only accepted before starting) stops/starts the process
      and resets the orchestrator's view of the subsystem,
    - the status broadcast carries the simulation info (which subsystems
      are currently simulated),
    - the toggle is rejected while the workflow is active, and allowed
      again once the workflow is stopped.
    """
    logger.info("--- TEST 13: Per-Subsystem Simulation Toggle (GUI) ---")

    # Starting the orchestrator in simulation mode also starts the sim
    # processes for every subsystem (default toggle state: all simulated).
    runner = OrchestratorRunner(
        CONFIG_PATH,
        SUBSYSTEMS_CONFIG_DIR,
        use_simulation=True,
        num_objects=3,
    )
    runner.start()
    # Give the sim processes time to boot and report over MQTT
    time.sleep(2.5)

    ws_client = WebSocketClient(url=f"ws://localhost:{FRONTEND_PORT}")
    await ws_client.connect()
    time.sleep(0.3)

    orch = runner.orchestrator

    # ── Default: every simmable subsystem is simulated ──
    results.check(
        sorted(orch._simulated) == sorted(ALL_SUBSYSTEMS),
        f"Default simulation set includes all {len(ALL_SUBSYSTEMS)} subsystems",
    )
    for name in ALL_SUBSYSTEMS:
        results.check(
            orch._sim_procs.is_running(name),
            f"Simulated process running for '{name}' by default",
        )

    # ── Toggle one subsystem OFF (-> real hardware) while idle ──
    await ws_client.send_command(
        "toggle_subsystem_simulation", {"subsystem": IMAGING_3DI},
    )
    await ws_client.receive()
    time.sleep(0.5)
    results.check(IMAGING_3DI not in orch._simulated, "imaging_3di toggled off (real)")
    results.check(
        not orch._sim_procs.is_running(IMAGING_3DI),
        "imaging_3di simulated process stopped",
    )
    adapter = orch.adapters[IMAGING_3DI]
    results.check(
        adapter.state == SubsystemState.OFFLINE,
        "imaging_3di marked offline, waiting for the real subsystem to report",
    )

    # The status broadcast carries the updated simulation info. Broadcasts
    # are scheduled after command acks, so scan both the recorded messages
    # and the freshly drained ones.
    await ws_client.send_command("request_status")
    await ws_client.receive()
    drained = await ws_client.drain_messages()
    sim_infos = [
        (m.get("details") or {}).get("simulation")
        for m in ws_client.received_messages + drained
        if m.get("event") == "workflow_state_changed"
    ]
    sim_infos = [s for s in sim_infos if isinstance(s, dict)]
    results.check(len(sim_infos) > 0, "Status broadcast carries simulation info")
    last_sim = sim_infos[-1] if sim_infos else {}
    results.check(
        last_sim.get("enabled") is True
        and IMAGING_3DI not in last_sim.get("simulated", []),
        "Simulation info reflects imaging_3di toggled off",
    )

    # ── Toggle it back ON ──
    await ws_client.send_command(
        "toggle_subsystem_simulation", {"subsystem": IMAGING_3DI},
    )
    await ws_client.receive()
    time.sleep(0.5)
    results.check(
        IMAGING_3DI in orch._simulated and orch._sim_procs.is_running(IMAGING_3DI),
        "imaging_3di toggled back on (simulated process restarted)",
    )

    # ── Toggle is rejected while the workflow is active ──
    ws_client.received_messages.clear()
    await ws_client.send_command("start_workflow")
    await ws_client.receive()
    for _ in range(60):
        if runner.current_state_name in ("starting", "running"):
            break
        await ws_client.drain_messages()
        time.sleep(0.25)
    results.check(
        runner.current_state_name in ("starting", "running"),
        "Workflow started for the toggle-rejection check",
    )
    await ws_client.send_command("toggle_subsystem_simulation", {"subsystem": XRF})
    await ws_client.receive()
    # The error broadcast is scheduled after the command ack, so it can
    # arrive just after receive() returned: collect the drained messages too.
    drained = await ws_client.drain_messages()
    errors = [
        m for m in ws_client.received_messages + drained
        if m.get("event") == "error_reported"
    ]
    results.check(len(errors) > 0, "Toggle rejected while the workflow is active")
    results.check(
        XRF in orch._simulated and orch._sim_procs.is_running(XRF),
        "xrf untouched by the rejected toggle",
    )

    # ── Stop the workflow: the toggle is allowed again ──
    await ws_client.send_command("stop_workflow")
    await ws_client.receive()
    for _ in range(60):
        if runner.current_state_name == "stopped":
            break
        await ws_client.drain_messages()
        time.sleep(0.25)
    results.check(
        runner.current_state_name == "stopped",
        "Workflow stopped before the post-stop toggle",
    )
    ws_client.received_messages.clear()
    await ws_client.send_command("toggle_subsystem_simulation", {"subsystem": RAMAN})
    await ws_client.receive()
    drained = await ws_client.drain_messages()
    errors = [
        m for m in ws_client.received_messages + drained
        if m.get("event") == "error_reported"
    ]
    results.check(len(errors) == 0, "Toggle accepted again once the workflow is stopped")
    results.check(RAMAN not in orch._simulated, "raman toggled off after the stop")

    await ws_client.close()
    runner.stop()  # also stops the remaining simulated processes

    logger.info("Per-subsystem simulation toggle test complete.\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def run_all_tests(num_objects: int = 3) -> bool:
    """Run all test suites."""
    logger.info("=" * 60)
    logger.info("  AUTOMATA GUI Orchestrator - Simulation Test Suite")
    logger.info("=" * 60)
    logger.info("Config: %s", CONFIG_PATH)
    logger.info("Broker: %s:%d", MQTT_BROKER_HOST, MQTT_BROKER_PORT)
    logger.info("")

    # Start mosquitto broker
    broker_proc = start_mosquitto_broker(MQTT_BROKER_PORT)

    try:
        test_state_machine_config()
        time.sleep(1)
        await test_full_workflow_with_gui_commands(num_objects)
        time.sleep(1)
        await test_pause_resume_gui()
        time.sleep(1)
        await test_mqtt_topic_publishing()
        time.sleep(1)
        await test_error_recovery()
        time.sleep(1)
        await test_subsystem_state_machine()
        time.sleep(1)
        test_subsystem_config()
        time.sleep(1)
        test_object_state_machine_config()
        time.sleep(1)
        test_object_state_machine_transitions()
        time.sleep(1)
        await test_object_in_orchestrator()
        time.sleep(1)
        test_scoped_event_routing()
        time.sleep(1)
        await test_debug_mode()
        time.sleep(1)
        await test_simulation_toggle()

    except Exception as exc:
        logger.error("Test suite error: %s", exc, exc_info=True)
        results.check(False, f"Test suite completed without exception: {exc}")

    finally:
        stop_mosquitto_broker(broker_proc)

    print(results.summary())
    return results.success


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Run AUTOMATA simulation tests")
    parser.add_argument(
        "--num-objects",
        type=int,
        default=3,
        help="Number of objects to simulate in the full workflow test",
    )
    args = parser.parse_args()

    success = asyncio.run(run_all_tests(num_objects=args.num_objects))

    if success:
        logger.info("All tests passed!")
        sys.exit(0)
    else:
        logger.error("Some tests failed!")
        sys.exit(1)


if __name__ == "__main__":
    import asyncio
    main()
