#!/usr/bin/env python3
"""Simulated xrf subsystem – standalone process, MQTT-only.

Runs the xrf state machine (backend/config/subsystems/xrf.yaml, the
same file the orchestrator uses) as a separate process that communicates
with the GUI orchestrator exclusively over MQTT – identical to how the
real xrf subsystem will behave.

Usage:
    python backend/simulation/xrf_sim.py --broker localhost --port 1883
"""
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from backend.simulation.subsystem_sim import main  # noqa: E402

if __name__ == "__main__":
    main("xrf", default_num_objects=0)
