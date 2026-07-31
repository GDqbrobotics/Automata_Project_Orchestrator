#!/usr/bin/env python3
"""
Main entry point for the Automata GUI Orchestrator.

Usage:
    # Normal mode (no simulation)
    python -m backend.orchestrator.main
    
    # Simulation mode (with simulated subsystems)
    python -m backend.orchestrator.main --simulation --num-objects 5
    
    # With custom broker
    python -m backend.orchestrator.main --broker 192.168.1.100 --simulation
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from pathlib import Path

# Ensure the project root is on the path so the backend package
# (at backend/) can be found as "backend".
project_root = Path(__file__).resolve().parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from backend.orchestrator.workflow import WorkflowOrchestrator  # noqa: E402

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="Automata GUI Orchestrator")
    parser.add_argument(
        "--config",
        type=str,
        default=str(Path(__file__).resolve().parent.parent.parent / "backend" / "config" / "workflow.yaml"),
        help="Path to the workflow state-machine YAML config",
    )
    parser.add_argument(
        "--subsystems-config",
        type=str,
        default=str(Path(__file__).resolve().parent.parent.parent / "backend" / "config" / "subsystems.yaml"),
        help="Path to the subsystems YAML config file",
    )
    parser.add_argument(
        "--broker",
        type=str,
        default="localhost",
        help="MQTT broker hostname",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="WebSocket frontend server port",
    )
    parser.add_argument(
        "--mqtt-port",
        type=int,
        default=1883,
        help="MQTT broker port",
    )
    parser.add_argument(
        "--simulation",
        action="store_true",
        help="Enable simulation mode (simulate subsystems)",
    )
    parser.add_argument(
        "--num-objects",
        type=int,
        default=3,
        help="Number of objects to simulate in simulation mode",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    # Configure logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Create orchestrator
    orchestrator = WorkflowOrchestrator(
        config_path=Path(args.config),
        subsystems_config_path=Path(args.subsystems_config),
        use_simulation=args.simulation,
        num_objects=args.num_objects,
    )
    
    # Override broker settings
    orchestrator.mqtt.broker = args.broker
    orchestrator.mqtt.port = args.mqtt_port
    orchestrator.frontend.port = args.port

    def _shutdown(signum: int, frame: object) -> None:
        logger.info("Received signal %d, shutting down...", signum)
        orchestrator.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    orchestrator.start()

    # Keep alive – in production this would be a real main loop
    logger.info("Orchestrator running. Press Ctrl+C to stop.")
    try:
        while True:
            import time
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        orchestrator.stop()


if __name__ == "__main__":
    main()
