"""Process manager for the simulated subsystems (simulation mode).

In simulation mode the simulated subsystem processes (one per subsystem,
``backend/simulation/<name>_sim.py``) are owned by the orchestrator: it
starts them for every subsystem the operator has toggled as *simulated*
(default: all of them) and stops them when the operator switches a
subsystem back to *real*. This is what makes the per-subsystem simulation
toggle in the GUI possible – the same orchestrator code path works for any
mix of simulated and real subsystems, and the orchestrator never knows or
cares that a subsystem is fake (it only ever sees MQTT).

A subsystem is simmable when the script ``<name>_sim.py`` exists next to
this file, so adding a simulated subsystem is purely a configuration
exercise (see README "Configuration").
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

SIM_SCRIPT = "{name}_sim.py"

# Project root (two levels up: backend/simulation/ -> project root)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _drain_stream(stream) -> None:
    """Read a child stream until EOF so its pipe buffer never fills up.

    Undrained pipes fill (64 KB) and BLOCK the child on its next write; a
    blocked simulated subsystem would silently stop reporting over MQTT.
    """
    try:
        for _ in iter(lambda: stream.readline(), b""):
            pass
    except Exception:
        pass


def _start_pipe_drainers(proc: subprocess.Popen) -> None:
    """Drain a child process's stdout/stderr in background threads."""
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            threading.Thread(target=_drain_stream, args=(stream,), daemon=True).start()


class SubsystemProcessManager:
    """Starts and stops one simulated subsystem process per subsystem.

    Each process runs the subsystem's own state machine (the same YAML the
    orchestrator uses) and communicates over MQTT only – identical to how
    the real subsystems behave.
    """

    def __init__(
        self,
        endpoint: Callable[[], Tuple[str, int]],
        num_objects: int = 0,
        sim_dir: Optional[Path] = None,
    ) -> None:
        # Endpoint is resolved lazily (callable -> (broker_host, broker_port))
        # so the sim processes always follow the orchestrator's live MQTT
        # client, even if the broker/port were overridden after the
        # orchestrator was constructed (tests, custom setups).
        self._endpoint = endpoint
        self._num_objects = num_objects
        self._sim_dir = Path(sim_dir) if sim_dir else Path(__file__).resolve().parent
        self._procs: Dict[str, subprocess.Popen] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------
    def has_sim(self, name: str) -> bool:
        """True when a simulated subsystem script exists for this name."""
        return (self._sim_dir / SIM_SCRIPT.format(name=name)).exists()

    def available(self, names: List[str]) -> List[str]:
        """Filter a list of subsystem names down to the simmable ones."""
        return [n for n in names if self.has_sim(n)]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def is_running(self, name: str) -> bool:
        """True when the simulated process for this subsystem is alive."""
        with self._lock:
            proc = self._procs.get(name)
            return proc is not None and proc.poll() is None

    def start(self, name: str) -> bool:
        """Start the simulated subsystem process (idempotent)."""
        with self._lock:
            if self.is_running(name):
                return True
            script = self._sim_dir / SIM_SCRIPT.format(name=name)
            if not script.exists():
                logger.error("[sim] no simulator script for '%s' at %s", name, script)
                return False
            broker_host, broker_port = self._endpoint()
            cmd = [
                sys.executable, str(script),
                "--broker", broker_host,
                "--port", str(broker_port),
            ]
            if name == "vision":
                cmd.extend(["--num-objects", str(self._num_objects)])
            logger.info("[sim] starting simulated subsystem '%s'", name)
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=str(_PROJECT_ROOT),
                # Own process group so shutdown can kill the whole tree.
                start_new_session=True,
            )
            _start_pipe_drainers(proc)
            self._procs[name] = proc
            return True

    def stop(self, name: str, timeout: float = 5.0) -> None:
        """Stop the simulated subsystem process (safe when not running)."""
        with self._lock:
            proc = self._procs.pop(name, None)
        if proc is None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.terminate()
            except Exception:
                pass
        try:
            proc.wait(timeout=timeout)
        except Exception:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            try:
                proc.wait(timeout=2)
            except Exception:
                pass
        logger.info("[sim] stopped simulated subsystem '%s'", name)

    def stop_all(self) -> None:
        """Stop every simulated subsystem process."""
        with self._lock:
            names = list(self._procs.keys())
        for name in names:
            self.stop(name)
