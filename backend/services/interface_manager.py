"""Real-interface process management (subsystem YAML ``metadata.interface``).

A real subsystem cannot report anything over MQTT until its interface
(ROS node, device daemon, camera service, ...) is running. The subsystem
YAML can therefore declare the shell commands that start and stop that
interface; the orchestrator executes them *headless* — detached from any
console, output appended to a log file — because from that moment the
subsystem state is known through MQTT messages only
(docs/CONFIG_CHANGE_GUIDE.md, R11).

Semantics (kept deliberately simple and idempotent):

- ``ensure_started`` launches ``startup`` at most once per "up" period:
  when the orchestrator starts, when a workflow starts (a subsystem left
  stopped gets its interface back before the wake transition), and when
  a subsystem is toggled SIM->REAL in simulation mode. Simulated
  subsystems never get an interface (the simulator *is* the subsystem).
- ``shutdown`` runs ``shutdown`` when the subsystem reports entering the
  ``stopped`` state (fire-and-forget: it runs on the MQTT receive path
  and must never block message handling) and — waited on, with a short
  timeout — at orchestrator shutdown. A still-alive tracked startup
  process is terminated best-effort afterwards so no orphan interface
  survives the orchestrator.

Both commands run through the shell: the configured value is a command
line, so a launcher that spawns a daemon and exits is as valid as a
long-running process. Scripts must be idempotent — a launcher executed
again after an orchestrator restart must not duplicate the interface.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from backend.domain.models import InterfaceConfig

logger = logging.getLogger(__name__)

# How long a *waited* shutdown command may take (orchestrator shutdown
# path only; the status path never waits on a stop script).
SHUTDOWN_WAIT_S = 5.0

# Called with (subsystem, action 'startup'|'shutdown', command, ok, detail)
CommandCallback = Callable[[str, str, str, bool, str], None]


def _detached_kwargs() -> Dict[str, Any]:
    """Popen kwargs that detach the child from any console (headless)."""
    if os.name == "posix":
        return {"start_new_session": True}
    creationflags = 0
    for flag in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP"):
        creationflags |= getattr(subprocess, flag, 0)
    return {"creationflags": creationflags}


class InterfaceProcessManager:
    """Launches and stops the real interfaces declared in the subsystem YAML.

    The manager owns no configuration: every call carries the
    ``InterfaceConfig`` read from the live YAML, so config changes are
    picked up without touching the manager.
    """

    def __init__(
        self,
        log_dir: Path,
        root_dir: Path,
        on_command: Optional[CommandCallback] = None,
    ) -> None:
        self._log_dir = Path(log_dir)
        self._root = Path(root_dir)
        self._on_command = on_command
        self._log_dir.mkdir(parents=True, exist_ok=True)
        # name -> Popen of the running *startup* command (if still alive)
        self._procs: Dict[str, subprocess.Popen] = {}
        # name -> interface believed up (started, not yet shut down)
        self._up: Dict[str, bool] = {}

    # -- public API ----------------------------------------------------

    def is_up(self, name: str) -> bool:
        """True while the manager believes the interface is running."""
        return bool(self._up.get(name))

    def ensure_started(self, name: str, cfg: InterfaceConfig) -> bool:
        """Launch the startup command if the interface is not up.

        Returns True when a new process was launched. Fire-and-forget:
        the command is never waited on.
        """
        if not cfg.enabled or not cfg.startup.strip():
            return False
        if self._up.get(name):
            return False
        log_path = self._log_dir / f"{name}.log"
        try:
            with open(log_path, "ab") as log_file:
                proc = subprocess.Popen(
                    cfg.startup,
                    shell=True,
                    cwd=str(self._resolve(cfg.cwd)) if cfg.cwd else None,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    **_detached_kwargs(),
                )
        except Exception as exc:  # noqa: BLE001 — report to the operator
            logger.error(
                "[interface] could not start '%s' for %s: %s", cfg.startup, name, exc
            )
            self._emit(name, "startup", cfg.startup, False, str(exc))
            return False
        self._procs[name] = proc
        self._up[name] = True
        logger.info(
            "[interface] started '%s' for %s (pid %s, log: %s)",
            cfg.startup,
            name,
            proc.pid,
            log_path,
        )
        self._emit(name, "startup", cfg.startup, True, f"pid {proc.pid}")
        return True

    def shutdown(self, name: str, cfg: InterfaceConfig, wait: bool = False) -> bool:
        """Run the shutdown command (if configured) and mark the interface down.

        ``wait=True`` is only used on the orchestrator shutdown path; on
        the MQTT receive path the command is fire-and-forget.
        """
        was_up = self._up.get(name)
        self._up[name] = False
        ran = False
        if cfg.enabled and cfg.shutdown.strip():
            detail = ""
            try:
                with open(self._log_dir / f"{name}.log", "ab") as log_file:
                    proc = subprocess.Popen(
                        cfg.shutdown,
                        shell=True,
                        cwd=str(self._resolve(cfg.cwd)) if cfg.cwd else None,
                        stdout=log_file,
                        stderr=subprocess.STDOUT,
                        **_detached_kwargs(),
                    )
                ran = True
                detail = f"pid {proc.pid}"
                if wait:
                    try:
                        proc.wait(timeout=SHUTDOWN_WAIT_S)
                        detail = "done"
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        detail = f"timeout after {SHUTDOWN_WAIT_S:.0f}s"
            except Exception as exc:  # noqa: BLE001 — report to the operator
                logger.error(
                    "[interface] could not stop '%s' for %s: %s",
                    cfg.shutdown,
                    name,
                    exc,
                )
                self._emit(name, "shutdown", cfg.shutdown, False, str(exc))
            else:
                logger.info(
                    "[interface] ran '%s' for %s (%s)", cfg.shutdown, name, detail
                )
                self._emit(name, "shutdown", cfg.shutdown, True, detail)
        # Best-effort: a tracked startup process that is still alive
        # (blocking-interface style) is terminated so no orphan interface
        # survives. A launcher that already exited is a no-op.
        proc = self._procs.pop(name, None)
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                logger.info(
                    "[interface] terminated startup process of %s (pid %s)",
                    name,
                    proc.pid,
                )
            except Exception:  # noqa: BLE001 — already gone / not ours anymore
                pass
        if was_up:
            logger.info("[interface] interface of %s is down (ran shutdown: %s)", name, ran)
        return ran

    def mark_down(self, name: str) -> None:
        """Mark the interface down *without* running any script.

        Used when a subsystem itself reports the stopped state: the
        shutdown script belongs to the workflow stop (Stop button, end of
        run) and to the orchestrator shutdown — this only keeps the
        up/down bookkeeping honest so the next workflow start relaunches
        the interface if needed. A tracked startup process that is still
        alive is terminated best-effort.
        """
        was_up = self._up.get(name)
        self._up[name] = False
        proc = self._procs.pop(name, None)
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                logger.info(
                    "[interface] terminated startup process of %s (pid %s)",
                    name,
                    proc.pid,
                )
            except Exception:  # noqa: BLE001 — already gone / not ours anymore
                pass
        if was_up:
            logger.info("[interface] interface of %s marked down (reported stopped)", name)

    def stop_all(self, configs: Dict[str, InterfaceConfig], wait: bool = False) -> None:
        """Shut down every interface the manager believes to be up.

        ``wait=False`` (workflow stop) is fire-and-forget so the command
        never blocks the caller; ``wait=True`` (orchestrator shutdown)
        waits briefly for each stop script.
        """
        for name in sorted(self._up):
            if self._up.get(name):
                self.shutdown(name, configs.get(name) or InterfaceConfig(), wait=wait)

    # -- internals -----------------------------------------------------

    def _resolve(self, path: str) -> Path:
        """Relative paths are resolved against the project root."""
        p = Path(path).expanduser()
        return p if p.is_absolute() else (self._root / p)

    def _emit(
        self, name: str, action: str, command: str, ok: bool, detail: str
    ) -> None:
        if self._on_command is None:
            return
        try:
            self._on_command(name, action, command, ok, detail)
        except Exception:  # noqa: BLE001 — never break process management on UI issues
            logger.exception("[interface] command callback failed")
