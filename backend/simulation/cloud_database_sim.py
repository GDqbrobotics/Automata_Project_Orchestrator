#!/usr/bin/env python3
"""Simulated cloud_database subsystem – standalone process, MQTT-only.

Runs the cloud_database state machine (backend/config/subsystems/cloud_database.yaml, the
same file the orchestrator uses) as a separate process that communicates
with the GUI orchestrator exclusively over MQTT – identical to how the
real cloud_database subsystem will behave.

Usage:
    python backend/simulation/cloud_database_sim.py --broker localhost --port 1883
"""
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from backend.simulation.subsystem_sim import main  # noqa: E402

if __name__ == "__main__":
    main("cloud_database", default_num_objects=0)
