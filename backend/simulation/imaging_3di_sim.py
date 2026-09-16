#!/usr/bin/env python3
"""Simulated imaging_3di subsystem – standalone process, MQTT-only.

Runs the imaging_3di state machine (backend/config/subsystems/imaging_3di.yaml, the
same file the orchestrator uses) as a separate process that communicates
with the GUI orchestrator exclusively over MQTT – identical to how the
real imaging_3di subsystem will behave.

Usage:
    python backend/simulation/imaging_3di_sim.py --broker localhost --port 1883
"""
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from backend.simulation.subsystem_sim import main  # noqa: E402

if __name__ == "__main__":
    main("imaging_3di", default_num_objects=0)
