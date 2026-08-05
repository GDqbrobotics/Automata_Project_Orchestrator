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


def start_mosquitto_broker(port: int = 1883) -> subprocess.Popen:
    """Start mosquitto broker in the background."""
    import tempfile
    
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
    return proc


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
    
    processes: list[subprocess.Popen] = []
    
    try:
        # Start mosquitto broker
        broker_proc = start_mosquitto_broker(args.mqtt_port)
        processes.append(broker_proc)
        
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
            logger.info("  Simulation mode: %d objects", args.num_objects)
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
