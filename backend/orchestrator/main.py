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

Configuration is loaded from backend/config/global.yaml by default.
CLI arguments override values from the config file.
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

from backend.domain.models import GlobalConfig  # noqa: E402
from backend.orchestrator.workflow import WorkflowOrchestrator  # noqa: E402

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "backend"
    / "config"
    / "global.yaml"
)
DEFAULT_WORKFLOW_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "backend"
    / "config"
    / "workflow.yaml"
)


def _load_global_config(args: argparse.Namespace) -> GlobalConfig:
    """Load global config from YAML, then override with CLI args."""
    config_path = Path(args.config_file) if args.config_file else DEFAULT_CONFIG_PATH
    if config_path.exists():
        global_config = GlobalConfig.from_yaml(config_path)
    else:
        global_config = GlobalConfig()
        logger.warning("Global config not found at %s, using defaults", config_path)

    # CLI overrides
    if args.broker is not None:
        global_config.mqtt_broker = args.broker
    if args.mqtt_port is not None:
        global_config.mqtt_port = args.mqtt_port
    if args.port is not None:
        global_config.frontend_port = args.port
    if args.num_objects is not None:
        global_config.simulation_default_num_objects = args.num_objects

    return global_config


def main() -> None:
    parser = argparse.ArgumentParser(description="Automata GUI Orchestrator")
    parser.add_argument(
        "--config",
        type=str,
        default=str(DEFAULT_WORKFLOW_PATH),
        help="Path to the workflow state-machine YAML config",
    )
    parser.add_argument(
        "--subsystems-config",
        type=str,
        default=str(
            Path(__file__).resolve().parent.parent.parent
            / "backend"
            / "config"
            / "subsystems"
        ),
        help="Path to the subsystems YAML config (file or directory)",
    )
    parser.add_argument(
        "--config-file",
        type=str,
        default=str(DEFAULT_CONFIG_PATH),
        help="Path to the global config YAML file",
    )
    parser.add_argument(
        "--broker",
        type=str,
        default=None,
        help="MQTT broker hostname (overrides global config)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="WebSocket frontend server port (overrides global config)",
    )
    parser.add_argument(
        "--mqtt-port",
        type=int,
        default=None,
        help="MQTT broker port (overrides global config)",
    )
    parser.add_argument(
        "--simulation",
        action="store_true",
        help="Enable simulation mode (simulate subsystems)",
    )
    parser.add_argument(
        "--num-objects",
        type=int,
        default=None,
        help="Number of objects to simulate (overrides global config)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug mode (subsystem inspector offers manual MQTT injection)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    # Load global config (defaults + CLI overrides)
    global_config = _load_global_config(args)

    # Configure logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Create orchestrator with global config
    orchestrator = WorkflowOrchestrator(
        config_path=Path(args.config),
        subsystems_config_path=Path(args.subsystems_config),
        global_config=global_config,
        use_simulation=args.simulation,
        num_objects=args.num_objects,
        debug_mode=args.debug,
    )

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
