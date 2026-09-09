#!/usr/bin/env python3
"""
Unified launcher for the AUTOMATA GUI Orchestrator.

This script starts both the backend (with optional simulation mode) and the
frontend development server in a single command.

Architecture:
  - Workflow, Object, and Subsystem SMs interact ONLY internally
  - Subsystem SMs are the BOUNDARY LAYER (publish MQTT to real hardware)
  - Events can be scoped: "robot_first/pick" → routed to robot_first subsystem
  - Events can be unscoped: "error" → broadcast to all subsystems

Usage:
    # Normal mode (no simulation)
    python launch_gui.py
    
    # Simulation mode with 5 objects
    python launch_gui.py --simulation --num-objects 5
    
    # With custom broker
    python launch_gui.py --broker 192.168.1.100 --simulation
    
    # Verbose logging
    python launch_gui.py --simulation --verbose
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

# Add project root to path
project_root = Path(__file__).resolve().parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("launch_gui")


def start_pipe_drainers(proc: subprocess.Popen) -> None:
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


def start_mosquitto_broker(port: int = 1883) -> subprocess.Popen:
    """Start mosquitto broker in the background."""
    import tempfile
    
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
    start_pipe_drainers(proc)

    # Wait for broker to start
    import paho.mqtt.client as mqtt
    for _ in range(50):
        time.sleep(0.1)
        try:
            client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
            client.connect("localhost", port, 5)
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


# One simulated process per subsystem (backend/simulation/<name>_sim.py).
# Each process runs the subsystem's own state machine (the same YAML the
# orchestrator uses) and communicates with the orchestrator exclusively
# over MQTT – identical to how the real subsystems will behave.
SIMULATED_SUBSYSTEMS = [
    "vision",
    "robot_first",
    "robot_second",
    "rotating_platform",
    "imaging_3di_hsi",
    "xrf",
    "raman",
    "ai_analysis",
    "cloud_database",
]


def start_subsystem_simulators(
    broker_host: str = "localhost",
    broker_port: int = 1883,
    num_objects: int = 3,
) -> list[subprocess.Popen]:
    """Start one simulated subsystem process per subsystem (simulation mode)."""
    procs: list[subprocess.Popen] = []
    sim_dir = project_root / "backend" / "simulation"
    for name in SIMULATED_SUBSYSTEMS:
        cmd = [
            sys.executable, str(sim_dir / f"{name}_sim.py"),
            "--broker", broker_host,
            "--port", str(broker_port),
        ]
        if name == "vision":
            cmd.extend(["--num-objects", str(num_objects)])
        logger.info("Starting simulated subsystem: %s", name)
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=str(project_root),
        )
        start_pipe_drainers(proc)
        procs.append(proc)
    return procs


def start_backend(
    broker_host: str = "localhost",
    broker_port: int = 1883,
    ws_port: int = 8080,
    use_simulation: bool = False,
    num_objects: int = 3,
    verbose: bool = False,
) -> subprocess.Popen:
    """Start the backend orchestrator."""
    cmd = [
        sys.executable, "-m", "backend.orchestrator.main",
        "--broker", broker_host,
        "--mqtt-port", str(broker_port),
        "--port", str(ws_port),
    ]
    
    if use_simulation:
        cmd.append("--simulation")
        cmd.extend(["--num-objects", str(num_objects)])
    
    if verbose:
        cmd.append("--verbose")
    
    logger.info("Starting backend with command: %s", " ".join(cmd))
    
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(project_root),
    )
    start_pipe_drainers(proc)
    return proc


def start_frontend(ws_port: int = 8080) -> subprocess.Popen:
    """Start the frontend development server."""
    cmd = [
        "npm", "run", "dev",
    ]
    
    # Set environment variable for backend WebSocket URL
    env = os.environ.copy()
    env["VITE_WS_URL"] = f"ws://localhost:{ws_port}"
    
    logger.info("Starting frontend dev server...")
    
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(project_root / "frontend"),
        env=env,
    )
    start_pipe_drainers(proc)
    return proc


def check_ports_free(port_whats: dict) -> None:
    """Refuse to start over an already running instance.

    Without this guard, a second launch silently joins the first one's
    environment (duplicate simulated subsystems publish to the same MQTT
    topics, and the second backend would fail to bind the WS port),
    leaving a confusing "process swamp".
    """
    import socket

    for port, what in port_whats.items():
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                logger.error(
                    "%s is already running: port %d is in use.", what, port
                )
                logger.error(
                    "Stop the existing instance first, or launch on "
                    "different ports (--ws-port / --mqtt-port)."
                )
                raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Launch AUTOMATA GUI Orchestrator")
    parser.add_argument(
        "--broker",
        type=str,
        default="localhost",
        help="MQTT broker hostname",
    )
    parser.add_argument(
        "--mqtt-port",
        type=int,
        default=1883,
        help="MQTT broker port",
    )
    parser.add_argument(
        "--ws-port",
        type=int,
        default=8080,
        help="WebSocket frontend server port",
    )
    parser.add_argument(
        "--simulation",
        action="store_true",
        help="Enable simulation mode",
    )
    parser.add_argument(
        "--num-objects",
        type=int,
        default=3,
        help="Number of objects to simulate",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    # Refuse to stack a second instance on top of a running one.
    check_ports_free(
        {
            args.ws_port: "Another orchestrator",
            3000: "Another frontend dev server",
        }
    )

    processes: list[subprocess.Popen] = []
    
    try:
        # Start mosquitto broker
        broker_proc = start_mosquitto_broker(args.mqtt_port)
        processes.append(broker_proc)
        
        # Start simulated subsystem processes (simulation mode only)
        if args.simulation:
            processes.extend(start_subsystem_simulators(
                broker_host=args.broker,
                broker_port=args.mqtt_port,
                num_objects=args.num_objects,
            ))
            time.sleep(0.5)
        
        # Start backend
        backend_proc = start_backend(
            broker_host=args.broker,
            broker_port=args.mqtt_port,
            ws_port=args.ws_port,
            use_simulation=args.simulation,
            num_objects=args.num_objects,
            verbose=args.verbose,
        )
        processes.append(backend_proc)
        
        # Give backend time to start
        time.sleep(2)
        
        # Start frontend
        frontend_proc = start_frontend(ws_port=args.ws_port)
        processes.append(frontend_proc)
        
        logger.info("=" * 60)
        logger.info("  AUTOMATA GUI Orchestrator launched")
        logger.info("=" * 60)
        logger.info("  Frontend: http://localhost:3000")
        logger.info("  Backend WebSocket: ws://localhost:%d", args.ws_port)
        logger.info("  MQTT broker: localhost:%d", args.mqtt_port)
        if args.simulation:
            logger.info(
                "  Simulation mode: %d objects (%d simulated subsystem processes)",
                args.num_objects, len(SIMULATED_SUBSYSTEMS),
            )
        logger.info("=" * 60)
        logger.info("Press Ctrl+C to stop all services")
        
        # Keep alive
        while True:
            time.sleep(1)
    
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    finally:
        logger.info("Shutting down...")
        for proc in processes:
            try:
                proc.terminate()
            except Exception:
                pass
        
        # Wait for processes to exit
        for proc in processes:
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
                proc.wait()
        
        logger.info("All services stopped")


if __name__ == "__main__":
    main()
