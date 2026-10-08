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
    
    # Debug mode (manual MQTT injection from the subsystem windows)
    python launch_gui.py --debug
    
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
        # Own process group: shutdown can kill the whole tree (npm would
        # otherwise leave its vite child behind, holding the frontend port).
        start_new_session=True,
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


def start_backend(
    broker_host: str = "localhost",
    broker_port: int = 1883,
    ws_port: int = 8080,
    use_simulation: bool = False,
    num_objects: int = 3,
    verbose: bool = False,
    debug: bool = False,
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
    
    if debug:
        cmd.append("--debug")
    
    if verbose:
        cmd.append("--verbose")
    
    logger.info("Starting backend with command: %s", " ".join(cmd))
    
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(project_root),
        start_new_session=True,
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
        start_new_session=True,
    )
    start_pipe_drainers(proc)
    return proc


def terminate_process_tree(proc: subprocess.Popen, sig: int) -> None:
    """Signal the child's whole process group (falls back to the child alone).

    Needed for `npm run dev`: terminating only the npm process would leave
    its vite child running, still bound to the frontend port.
    """
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            if sig == signal.SIGKILL:
                proc.kill()
            else:
                proc.terminate()
        except Exception:
            pass


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


def _load_global_defaults() -> dict:
    """Defaults from backend/config/global.yaml (single source of truth).

    CLI args still override them; the YAML is only read for the argparse
    defaults so launcher and orchestrator can never drift apart (R9).
    """
    import yaml

    path = project_root / "backend" / "config" / "global.yaml"
    defaults = {
        "broker": "localhost",
        "mqtt_port": 1883,
        "ws_port": 8080,
        "num_objects": 3,
        "dev_port": 3000,
    }
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except OSError:
        return defaults
    mqtt_cfg = data.get("mqtt", {})
    frontend_cfg = data.get("frontend", {})
    sim_cfg = data.get("simulation", {})
    defaults["broker"] = mqtt_cfg.get("broker", defaults["broker"])
    defaults["mqtt_port"] = mqtt_cfg.get("port", defaults["mqtt_port"])
    defaults["ws_port"] = frontend_cfg.get("port", defaults["ws_port"])
    defaults["dev_port"] = frontend_cfg.get("dev_port", defaults["dev_port"])
    defaults["num_objects"] = sim_cfg.get(
        "default_num_objects", defaults["num_objects"]
    )
    return defaults


def main() -> None:
    defaults = _load_global_defaults()
    parser = argparse.ArgumentParser(description="Launch AUTOMATA GUI Orchestrator")
    parser.add_argument(
        "--broker",
        type=str,
        default=defaults["broker"],
        help="MQTT broker hostname",
    )
    parser.add_argument(
        "--mqtt-port",
        type=int,
        default=defaults["mqtt_port"],
        help="MQTT broker port",
    )
    parser.add_argument(
        "--ws-port",
        type=int,
        default=defaults["ws_port"],
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
        default=defaults["num_objects"],
        help="Number of objects to simulate",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help=(
            "Debug mode: each subsystem inspector window shows a selector "
            "of the MQTT topics the real subsystem is expected to publish, "
            "and the user can send one of them (user-driven simulation)"
        ),
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    # Refuse to stack a second instance on top of a running one. The dev
    # server port comes from global.yaml frontend.dev_port (it must track
    # the port in frontend/vite.config.ts).
    check_ports_free(
        {
            args.ws_port: "Another orchestrator",
            defaults["dev_port"]: "Another frontend dev server",
        }
    )

    processes: list[subprocess.Popen] = []
    
    try:
        # Start mosquitto broker
        broker_proc = start_mosquitto_broker(args.mqtt_port)
        processes.append(broker_proc)
        
        # In simulation mode the backend orchestrator owns the simulated
        # subsystem processes (it starts them on startup and lets the
        # operator toggle each subsystem's simulation from the GUI before
        # the workflow starts), so the launcher does not start them itself.
        
        # Start backend
        backend_proc = start_backend(
            broker_host=args.broker,
            broker_port=args.mqtt_port,
            ws_port=args.ws_port,
            use_simulation=args.simulation,
            num_objects=args.num_objects,
            verbose=args.verbose,
            debug=args.debug,
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
        logger.info("  Frontend: http://localhost:%d", defaults["dev_port"])
        logger.info("  Backend WebSocket: ws://localhost:%d", args.ws_port)
        logger.info("  MQTT broker: localhost:%d", args.mqtt_port)
        if args.simulation:
            logger.info(
                "  Simulation mode: %d objects – the backend starts the "
                "simulated subsystem processes; toggle each subsystem "
                "SIM/REAL from its card before starting the workflow",
                args.num_objects,
            )
        if args.debug:
            logger.info(
                "  Debug mode: subsystem windows offer manual MQTT message injection"
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
                terminate_process_tree(proc, signal.SIGTERM)
            except Exception:
                pass
        
        # Wait for processes to exit
        for proc in processes:
            try:
                proc.wait(timeout=5)
            except Exception:
                terminate_process_tree(proc, signal.SIGKILL)
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass
        
        logger.info("All services stopped")


if __name__ == "__main__":
    main()
