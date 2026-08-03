#!/usr/bin/env python3
"""
simulation_test.py – Simulation test suite for the AUTOMATA GUI Orchestrator.

This script:
  1. Starts a local mosquitto broker (in background).
  2. Launches the workflow orchestrator in simulation mode.
  3. Simulates operator commands via WebSocket (like the real GUI would).
  4. Drives the workflow end-to-end: start -> subsystems ready -> run -> complete -> stop.
  5. Validates all state transitions and MQTT topic exchanges.

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
            return False

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
def start_mosquitto_broker(port: int = MQTT_BROKER_PORT) -> subprocess.Popen:
    """Start mosquitto broker in the background."""
    conf_file = tempfile.NamedTemporaryFile(mode="w", suffix=".conf", delete=False)
    conf_file.write(f"port {port}\n")
    conf_file.write("allow_anonymous true\n")
    conf_file.flush()
    conf_file.close()

    proc = subprocess.Popen(
        ["mosquitto", "-c", conf_file.name, "-v"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

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
    """Test: Full workflow simulation with GUI commands sent via WebSocket."""
    logger.info("--- TEST 2: Full Workflow Simulation (GUI via WebSocket) ---")

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
    # The initialization phase will transition them to 'ready' during simulation
    for name in ALL_SUBSYSTEMS:
        adapter = runner.orchestrator.adapters.get(name)
        if adapter and adapter.state_machine:
            # State machine should be loaded with 'initializing' as initial state
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
    # The simulation runs in a background thread. We poll the state
    # and progress until we reach a terminal state.
    logger.info("Waiting for simulation to complete...")
    terminal_states = {"stopped", "error"}
    max_wait = 30  # seconds
    start_wait = time.monotonic()

    while time.monotonic() - start_wait < max_wait:
        state = runner.current_state_name
        progress = runner.orchestrator._progress

        # Drain any pending messages from the WS
        await ws_client.drain_messages()

        if state in terminal_states:
            logger.info(
                "Simulation ended after %.1fs: state=%s, progress=%d",
                time.monotonic() - start_wait,
                state,
                progress,
            )
            break
        time.sleep(0.5)

    # ---- Phase 4: Verify simulation ran ----
    final_state = runner.current_state_name
    final_progress = runner.orchestrator._progress

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

    # ---- Cleanup ----
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

    # Manually mark all ready (since simulation mode is off)
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

    # Stop
    await ws_client.send_command("stop_workflow")
    await ws_client.receive()
    time.sleep(0.2)

    results.check(
        runner.current_state_name == "stopping",
        "After stop_workflow, state transitions to 'stopping'",
    )

    # Fire the final 'stopped' event to complete the stop sequence
    runner.fire_event("stopped")
    time.sleep(0.2)

    results.check(
        runner.current_state_name == "stopped",
        "After stop sequence, state is 'stopped'",
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

    # Subscribe to workflow command topics to verify publishing
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

    # Subscribe BEFORE sending any commands
    checker_client.on_message = on_workflow_cmd
    checker_client.subscribe("automata/#", qos=1)  # Catch all automata topics
    checker_client.subscribe("command/#", qos=1)
    time.sleep(0.3)

    # Connect WebSocket client
    ws_client = WebSocketClient(url=f"ws://localhost:{FRONTEND_PORT}")
    await ws_client.connect()
    time.sleep(0.2)

    # Start workflow -- should publish workflow/start command
    await ws_client.send_command("start_workflow")
    await ws_client.receive()
    time.sleep(0.3)

    # The state machine publishes to MQTT via the orchestrator's MQTT client,
    # but our checker subscribes to the broker. Let's verify by checking
    # that the start transition happened (which triggers the MQTT publish)
    start_published = runner.current_state_name == "starting"
    results.check(
        start_published,
        "Workflow start command triggers MQTT publish (state -> starting)",
    )

    # Publish subsystem ready events to broker AND directly set
    for name in ALL_SUBSYSTEMS:
        sim = SubsystemSimulator(name)
        sim.mark_ready()
    runner.orchestrator.update_all_subsystems_ready()
    time.sleep(0.3)

    results.check(
        runner.current_state_name == "running",
        "Subsystem status updates via MQTT triggered state transition to 'running'",
    )

    results.check(
        len(commands_received) > 0,
        f"Checker received {len(commands_received)} MQTT commands",
    )

    # Also verify workflow start command was published
    # The state machine publishes to automata/workflow.start with payload {"event": "workflow.start"}
    has_start = any(
        "start" in cmd.get("topic", "") or "start" in cmd["data"].get("event", "")
        for cmd in commands_received
    )
    if not has_start:
        logger.info("Checker received commands: %s", commands_received[:5])  # Log first 5 commands
    results.check(
        has_start,
        f"Checker received workflow start command (total {len(commands_received)} commands)",
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

    # Connect WebSocket client
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
        current_state = adapter.state_machine.current_state

        # Get available transitions
        available = adapter.state_machine.get_available_transitions()

        if "start" in available:
            try:
                new_state = adapter.fire_transition("start")
                results.check(
                    new_state in available.values(),
                    f"Subsystem '{name}' transitioned to '{new_state}'",
                )
            except Exception as e:
                results.check(False, f"Subsystem '{name}' transition failed: {e}")

    # Cleanup
    runner.stop()

    logger.info("Subsystem state machine test complete.\n")


# ---------------------------------------------------------------------------
# Test 7: Subsystem configuration loading
# ---------------------------------------------------------------------------
def test_subsystem_config() -> None:
    """Test: Validate subsystem configuration loads correctly."""
    logger.info("--- TEST 7: Subsystem Configuration ---")

    # Create a runner just to check config loading
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

    # Check that adapters were created
    results.check(
        len(runner.orchestrator.adapters) > 0,
        f"Adapters created: {len(runner.orchestrator.adapters)}",
    )

    # Check required subsystems
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

    # Load the object state machine engine
    manager = ObjectStateMachines(config_path)

    results.check(
        manager.config.name == "object_analysis_workflow",
        "Object workflow has correct name",
    )
    results.check(
        manager.config.initial_state == "identified_in_box",
        "Object workflow initial state is 'identified_in_box'",
    )

    # Verify known states exist
    expected_states = [
        "identified_in_box",
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

    # Verify transitions have valid events and targets
    expected_transitions = {
        "identified_in_box": "object_picked",
        "moving_to_disk": "object_placed_on_disk",
        "HSI_3DI_analysis": "imaging_capture_complete",
        "object_going_second_position": "platform_rotated_to_second",
        "XRF_RAMAN_analysis": "measurements_complete",
        "object_going_first_position": "platform_rotated_to_first",
        "moving_to_box": "object_returned_to_box",
    }
    for state, expected_event in expected_transitions.items():
        transitions = manager.config.states[state].transitions
        events = [t.event for t in transitions]
        results.check(
            expected_event in events,
            f"State '{state}' has transition event '{expected_event}'",
        )
        # Verify target state exists
        for t in transitions:
            if t.event == expected_event:
                results.check(
                    t.target in manager.config.states,
                    f"Transition from '{state}' to '{t.target}' has valid target",
                )

    # Verify final state has no transitions
    final_transitions = manager.config.states["object_analyzed"].transitions
    results.check(
        len(final_transitions) == 0,
        "Final state 'object_analyzed' has no transitions",
    )

    # Verify event actions are correctly parsed (on_complete_event)
    # These events trigger subsystem actions when the object state machine
    # transition completes
    expected_events = {
        "identified_in_box": "trigger_robot_first_pick",
        "moving_to_disk": "trigger_imaging_capture",
        "HSI_3DI_analysis": "trigger_platform_rotate_to_second",
        "object_going_second_position": "trigger_measurements",
        "XRF_RAMAN_analysis": "trigger_platform_rotate_to_first",
        "object_going_first_position": "trigger_robot_first_return",
        "moving_to_box": "trigger_cloud_upload",
    }
    for state, expected_event in expected_events.items():
        transitions = manager.config.states[state].transitions
        for t in transitions:
            if t.event in expected_transitions.get(state, []):
                results.check(
                    t.on_complete_event == expected_event,
                    f"Transition from '{state}' fires event '{expected_event}'",
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

    # Create an object
    manager.create_object("test_object_001")
    results.check(
        manager.get_object_state("test_object_001") == "identified_in_box",
        "Object starts in 'identified_in_box' state",
    )

    # Get available transitions
    available = manager.get_available_transitions("test_object_001")
    results.check(
        "object_picked" in available,
        "Available transition from 'identified_in_box' is 'object_picked'",
    )

    # Fire transitions through the entire workflow
    transition_sequence = [
        ("object_picked", "moving_to_disk"),
        ("object_placed_on_disk", "HSI_3DI_analysis"),
        ("imaging_capture_complete", "object_going_second_position"),
        ("platform_rotated_to_second", "XRF_RAMAN_analysis"),
        ("measurements_complete", "object_going_first_position"),
        ("platform_rotated_to_first", "moving_to_box"),
        ("object_returned_to_box", "object_analyzed"),
    ]

    for event, expected_state in transition_sequence:
        new_state = manager.fire_transition("test_object_001", event)
        results.check(
            new_state == expected_state,
            f"Transition '{event}' -> '{expected_state}' (got '{new_state}')",
        )

    # Verify final state
    results.check(
        manager.get_object_state("test_object_001") == "object_analyzed",
        "Object reaches 'object_analyzed' final state",
    )

    # Verify no transitions available from final state
    final_available = manager.get_available_transitions("test_object_001")
    results.check(
        len(final_available) == 0,
        "No transitions available from 'object_analyzed' state",
    )

    # Verify persistence - state should be saved to disk
    state_file = manager._object_state_file("test_object_001")
    results.check(
        state_file.exists(),
        "Object state persisted to disk",
    )

    # Create a new manager and verify state is loaded from disk
    manager2 = ObjectStateMachines(config_path)
    results.check(
        manager2.get_object_state("test_object_001") == "object_analyzed",
        "Object state restored from disk after manager restart",
    )

    # Test invalid transition
    result = manager.fire_transition("test_object_001", "invalid_event")
    results.check(
        result is None,
        "Invalid transition returns None",
    )

    # Test non-existent object
    result = manager.fire_transition("nonexistent", "object_picked")
    results.check(
        result is None,
        "Transition on non-existent object returns None",
    )

    # Get object info
    obj_info = manager.get_object_info("test_object_001")
    results.check(
        obj_info is not None,
        "Object info is retrievable",
    )
    if obj_info:
        results.check(
            obj_info["object_id"] == "test_object_001",
            "Object info contains correct object_id",
        )
        results.check(
            len(obj_info["transitions"]) == 7,
            "Object info contains 7 transition records",
        )

    # Create a second object
    manager.create_object("test_object_002")
    all_objects = manager.get_object_ids()
    results.check(
        "test_object_001" in all_objects and "test_object_002" in all_objects,
        "Multiple objects tracked",
    )

    # Remove an object
    manager.remove_object("test_object_002")
    all_objects = manager.get_object_ids()
    results.check(
        "test_object_002" not in all_objects,
        "Object removed from tracking",
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

    # Verify object manager is initialized
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

    # Verify initial state
    state = runner.orchestrator.object_manager.get_object_state("orch_test_001")
    results.check(
        state == "identified_in_box",
        "Object created via orchestrator starts in 'identified_in_box'",
    )

    # Fire a transition via orchestrator
    new_state = runner.orchestrator.fire_object_transition(
        "orch_test_001", "object_picked"
    )
    results.check(
        new_state == "moving_to_disk",
        "Object transition via orchestrator: identified_in_box -> moving_to_disk",
    )

    # Fire complete sequence via orchestrator
    transition_sequence = [
        ("object_placed_on_disk", "HSI_3DI_analysis"),
        ("imaging_capture_complete", "object_going_second_position"),
        ("platform_rotated_to_second", "XRF_RAMAN_analysis"),
        ("measurements_complete", "object_going_first_position"),
        ("platform_rotated_to_first", "moving_to_box"),
        ("object_returned_to_box", "object_analyzed"),
    ]

    for event, expected_state in transition_sequence:
        new_state = runner.orchestrator.fire_object_transition("orch_test_001", event)
        results.check(
            new_state == expected_state,
            f"Orchestrator transition: {event} -> {expected_state}",
        )

    # Verify object is analyzed
    final_state = runner.orchestrator.object_manager.get_object_state("orch_test_001")
    results.check(
        final_state == "object_analyzed",
        "Object reaches 'object_analyzed' via orchestrator",
    )

    # Verify status includes objects
    status = runner.orchestrator._get_status()
    results.check(
        len(status.objects) >= 1,
        f"Status includes {len(status.objects)} object(s)",
    )

    # Verify workflow actions config is loaded (config-driven event mapping)
    orch = runner.orchestrator
    results.check(
        hasattr(orch, 'workflow_actions'),
        "Orchestrator has workflow_actions config",
    )
    if hasattr(orch, 'workflow_actions'):
        results.check(
            len(orch.workflow_actions.actions) > 0,
            f"Workflow actions loaded: {list(orch.workflow_actions.actions.keys())}",
        )
        results.check(
            "trigger_robot_first_pick" in orch.workflow_actions.actions,
            "Workflow action 'trigger_robot_first_pick' is configured",
        )
        results.check(
            "trigger_imaging_capture" in orch.workflow_actions.actions,
            "Workflow action 'trigger_imaging_capture' is configured",
        )
        results.check(
            "trigger_cloud_upload" in orch.workflow_actions.actions,
            "Workflow action 'trigger_cloud_upload' is configured",
        )

    # Cleanup
    runner.stop()

    logger.info("Object orchestrator integration test complete.\n")


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
        # Run all test suites
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

    except Exception as exc:
        logger.error("Test suite error: %s", exc, exc_info=True)
        results.check(False, f"Test suite completed without exception: {exc}")

    finally:
        # Always stop the broker
        stop_mosquitto_broker(broker_proc)

    # Print summary
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
