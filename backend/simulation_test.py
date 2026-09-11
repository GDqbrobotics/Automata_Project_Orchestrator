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
from typing import Any, Dict, List, Optional

# Add project root to path so the backend package (at backend/) works.
project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

import paho.mqtt.client as mqtt
import websockets

# Backend imports
from backend.adapters.subsystem import (
    ALL_SUBSYSTEMS,
    VISION,
    AI_ANALYSIS,
    IMAGING_3DI_HSI,
    ROTATING_PLATFORM,
    XRF,
    RAMAN,
    ROBOT_FIRST,
    ROBOT_SECOND,
    CLOUD_DB,
)
from backend.domain.models import WorkflowState, SubsystemState, ObjectState
from backend.messaging.mqtt_client import (
    TOPIC_COMMAND,
    TOPIC_STATUS,
    TOPIC_SUBSYSTEM,
)
from backend.orchestrator.workflow import WorkflowOrchestrator
from backend.state.state_machine import StateMachineEngine, StateMachineError
from backend.orchestrator.object_state_machines import ObjectStateMachines

# ---------------------------------------------------------------------------
# Test configuration
# ---------------------------------------------------------------------------
MQTT_BROKER_HOST = "localhost"
MQTT_BROKER_PORT = 1883
FRONTEND_PORT = 8081
CONFIG_PATH = str(Path(__file__).resolve().parent / "config" / "workflow.yaml")
SUBSYSTEMS_CONFIG_DIR = str(Path(__file__).resolve().parent / "config" / "subsystems")

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
# Simulated subsystem processes (backend/simulation/*_sim.py)
# ---------------------------------------------------------------------------
# In simulation mode every subsystem is simulated as an individual process
# that communicates with the orchestrator exclusively over MQTT – identical
# to how the real subsystems will behave.
ACTIVE_SIM_PROCS: List[subprocess.Popen] = []


def start_simulator_processes(port: int, num_objects: int) -> None:
    """Start one simulated subsystem process per configured subsystem."""
    stop_simulator_processes()  # defensive: clean up leftovers from a failed run
    sim_dir = Path(__file__).resolve().parent / "simulation"
    for name in ALL_SUBSYSTEMS:
        cmd = [
            sys.executable, str(sim_dir / f"{name}_sim.py"),
            "--broker", MQTT_BROKER_HOST,
            "--port", str(port),
        ]
        if name == "vision":
            cmd.extend(["--num-objects", str(num_objects)])
        logger.info("Starting simulated subsystem process: %s", name)
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        _start_pipe_drainers(proc)
        ACTIVE_SIM_PROCS.append(proc)


def stop_simulator_processes() -> None:
    """Stop all simulated subsystem processes."""
    for proc in ACTIVE_SIM_PROCS:
        try:
            proc.terminate()
        except Exception:
            pass
    for proc in ACTIVE_SIM_PROCS:
        try:
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    ACTIVE_SIM_PROCS.clear()


# ---------------------------------------------------------------------------
# Orchestrator runner (headless)
# ---------------------------------------------------------------------------
class OrchestratorRunner:
    """Runs the orchestrator in the main process (no thread needed for test)."""

    def __init__(
        self,
        config_path: str,
        subsystems_config_dir: str,
        use_simulation: bool = False,
        num_objects: int = 3,
    ) -> None:
        self.orchestrator = WorkflowOrchestrator(
            config_path=Path(config_path),
            subsystems_config_path=Path(subsystems_config_dir),
            use_simulation=use_simulation,
            num_objects=num_objects,
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
    def state(self) -> WorkflowState:
        return self.orchestrator.state_machine.state

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
            "ready": state == "ready",
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
    results.check(
        sm.current_state == "idle",
        "Initial state is 'idle'",
    )

    available = sm.get_available_transitions()
    results.check(
        "start_requested" in available,
        "idle state has 'start_requested' transition",
    )

    # Test invalid transition
    try:
        sm.fire("pause_requested", None)
        results.check(False, "Invalid transition raises StateMachineError")
    except StateMachineError:
        results.check(True, "Invalid transition raises StateMachineError")

    # Verify known states exist in config
    results.check("running" in sm.config.states, "State 'running' is defined in config")
    results.check("paused" in sm.config.states, "State 'paused' is defined in config")
    results.check("error" in sm.config.states, "State 'error' is defined in config")

    # Verify transition targets exist
    idle_transitions = sm.config.states["idle"].transitions
    results.check(
        all(t.target in sm.config.states for t in idle_transitions),
        "All idle transitions have valid target states",
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

    # Start the simulated subsystem processes (self-init over MQTT)
    start_simulator_processes(MQTT_BROKER_PORT, num_objects)
    # Give them time to connect to the broker and self-initialize
    time.sleep(2.5)

    # Start orchestrator in simulation mode with the correct number of objects
    runner = OrchestratorRunner(
        CONFIG_PATH,
        SUBSYSTEMS_CONFIG_DIR,
        use_simulation=True,
        num_objects=num_objects,
    )
    runner.start()

    # Connect WebSocket client (simulating GUI)
    ws_client = WebSocketClient(url=f"ws://localhost:{FRONTEND_PORT}")
    await ws_client.connect()
    time.sleep(0.3)

    # ---- Phase 1: Idle state ----
    results.check(
        runner.current_state_name == "idle",
        "Orchestrator starts in 'idle' state",
    )

    # Verify subsystems are created with 'initializing' as initial state
    for name in ALL_SUBSYSTEMS:
        adapter = runner.orchestrator.adapters.get(name)
        if adapter and adapter.state_machine:
            results.check(
                adapter.state_machine.config.initial_state == "initializing",
                f"Subsystem '{name}' YAML defines 'initializing' as initial state",
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
    # Terminal states: stopped/error = workflow completion
    terminal_states = {"stopped", "error"}
    # Stage durations are slowed down for GUI visibility, so the full
    # pipeline takes a while (roughly 8-10 s per object after the first).
    max_wait = 180  # seconds
    start_wait = time.monotonic()
    max_progress = 0

    # Physical capacity of the machine (docs/PROJECT_CONTEXT.md):
    #  - the rotating platform has 2 disk slots (imaging + measurement),
    #  - robot 1 has a single end effector (1 object in transit),
    #  - every other object must stay in the box.
    # Object states occupying each physical position (imaging happens on
    # the second disk, XRF/Raman measurement on the first one):
    DISK_FIRST_STATES = {"object_going_second_position", "XRF_RAMAN_analysis", "moving_to_box"}
    DISK_SECOND_STATES = {"HSI_3DI_analysis", "object_going_first_position"}
    IN_BOX_STATES = {"identified_in_box", "waiting_for_disk", "object_analyzed"}
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
            n_in_transit = sum(s == "moving_to_disk" for s in object_states)
            n_out_of_box = sum(s not in IN_BOX_STATES for s in object_states)
            if n_disk_first > 1 or n_disk_second > 1 or n_in_transit > 1 or n_out_of_box > 3:
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
        final_state in ("stopped", "error"),
        f"Simulation reached terminal state ('stopped' or 'error', got '{final_state}')",
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

    # ---- Cleanup ----
    runner.stop()
    await ws_client.close()
    stop_simulator_processes()

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

    # Reset from stopped back to idle
    runner.fire_event("reset_requested")
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
    checker_client.subscribe("automata/#", qos=1)
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

    # Error during run
    runner.fire_event("subsystem_error")
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
    if runner.orchestrator.adapters.get(VISION) and runner.orchestrator.adapters[VISION].state_machine:
        adapter = runner.orchestrator.adapters[VISION]
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
    results.check(
        "vision" in names,
        "Subsystem 'vision' is configured",
    )
    results.check(
        "robot_first" in names,
        "Subsystem 'robot_first' is configured",
    )
    results.check(
        "cloud_database" in names,
        "Subsystem 'cloud_database' is configured",
    )

    results.check(
        len(runner.orchestrator.adapters) > 0,
        f"Adapters created: {len(runner.orchestrator.adapters)}",
    )

    required_names = runner.orchestrator.required_subsystem_names
    results.check(
        "vision" in required_names,
        "'vision' is a required subsystem",
    )
    results.check(
        "ai_analysis" not in required_names,
        "'ai_analysis' is not a required subsystem",
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
        manager.config.initial_state == "identified_in_box",
        "Object workflow initial state is 'identified_in_box'",
    )

    expected_states = [
        "identified_in_box",
        "waiting_for_disk",
        "moving_to_disk",
        "HSI_3DI_analysis",
        "object_going_second_position",
        "XRF_RAMAN_analysis",
        "object_going_first_position",
        "moving_to_box",
        "object_analyzed",
    ]
    for state in expected_states:
        results.check(
            state in manager.config.states,
            f"State '{state}' is defined in object config",
        )

    # Verify event actions are scoped (e.g., "robot_first/pick").
    # Note: 'waiting_for_disk' (object still in the box) is where the robot
    # pick is triggered – objects only leave the box when a disk slot is
    # actually free (the physical platform holds at most 2 objects).
    # The object waits on the first disk ('object_going_second_position')
    # for the rotation to the second (imaging) position; imaging commands
    # fire only when the object arrives there.
    expected_scoped_events = {
        "waiting_for_disk": ["robot_first/pick"],
        "moving_to_disk": ["workflow/rotation_requested_second"],
        "object_going_second_position": ["imaging_3di_hsi/capture", "robot_second/scan_hsi_3d"],
        # The AI plans the XRF/Raman points from the imaging data, in
        # parallel with the rotation back to the first position. Rotation
        # requests are internal 'workflow/' events: the platform rotates
        # BOTH disks at once, so the orchestrator (pipeline controller)
        # decides when a rotation is physically safe and issues the actual
        # rotating_platform command.
        "HSI_3DI_analysis": ["ai_analysis/plan", "workflow/rotation_requested_first"],
        "object_going_first_position": ["robot_first/measure_xrf_raman", "xrf/measure", "raman/measure"],
        "XRF_RAMAN_analysis": ["robot_first/return"],
        "moving_to_box": ["cloud_database/upload"],
    }
    for state, expected_events in expected_scoped_events.items():
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
        manager.get_object_state("test_object_001") == "identified_in_box",
        "Object starts in 'identified_in_box' state",
    )

    available = manager.get_available_transitions("test_object_001")
    results.check(
        "queued_for_analysis" in available,
        "Available transition from 'identified_in_box' is 'queued_for_analysis'",
    )

    transition_sequence = [
        ("queued_for_analysis", "waiting_for_disk"),
        ("object_picked", "moving_to_disk"),
        ("object_placed_on_disk", "object_going_second_position"),
        ("platform_rotated_to_second", "HSI_3DI_analysis"),
        ("imaging_capture_complete", "object_going_first_position"),
        ("platform_rotated_to_first", "XRF_RAMAN_analysis"),
        ("measurements_complete", "moving_to_box"),
        ("object_returned_to_box", "object_analyzed"),
    ]

    for event, expected_state in transition_sequence:
        new_state = manager.fire_transition("test_object_001", event)
        results.check(
            new_state == expected_state,
            f"Transition '{event}' -> '{expected_state}' (got '{new_state}')",
        )

    results.check(
        manager.get_object_state("test_object_001") == "object_analyzed",
        "Object reaches 'object_analyzed' final state",
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

    # Fire the queueing transition (analysis order established by vision)
    new_state = runner.orchestrator.fire_object_transition(
        "orch_test_001", "queued_for_analysis"
    )
    results.check(
        new_state == "waiting_for_disk",
        "Object transition via orchestrator: identified_in_box -> waiting_for_disk",
    )

    # Fire the pick (the disk slot is free, the robot takes the object)
    new_state = runner.orchestrator.fire_object_transition(
        "orch_test_001", "object_picked"
    )
    results.check(
        new_state == "moving_to_disk",
        "Object transition via orchestrator: waiting_for_disk -> moving_to_disk",
    )

    # Fire complete sequence
    transition_sequence = [
        ("object_placed_on_disk", "object_going_second_position"),
        ("platform_rotated_to_second", "HSI_3DI_analysis"),
        ("imaging_capture_complete", "object_going_first_position"),
        ("platform_rotated_to_first", "XRF_RAMAN_analysis"),
        ("measurements_complete", "moving_to_box"),
        ("object_returned_to_box", "object_analyzed"),
    ]

    for event, expected_state in transition_sequence:
        new_state = runner.orchestrator.fire_object_transition("orch_test_001", event)
        results.check(
            new_state == expected_state,
            f"Orchestrator transition: {event} -> {expected_state}",
        )

    final_state = runner.orchestrator.object_manager.get_object_state("orch_test_001")
    results.check(
        final_state == "object_analyzed",
        "Object reaches 'object_analyzed' via orchestrator",
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

    except Exception as exc:
        logger.error("Test suite error: %s", exc, exc_info=True)
        results.check(False, f"Test suite completed without exception: {exc}")

    finally:
        stop_simulator_processes()
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
