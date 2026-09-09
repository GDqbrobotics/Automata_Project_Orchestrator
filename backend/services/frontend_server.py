"""Simple HTTP + WebSocket server for frontend communication."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any, Callable, Dict, List, Optional

import websockets
from websockets.asyncio.server import ServerConnection

from backend.domain.models import (
    BackendEvent,
    EventPayload,
    SubsystemState,
    WorkflowCommand,
    WorkflowState,
    WorkflowStatus,
)

logger = logging.getLogger(__name__)


class FrontendServer:
    """
    WebSocket server that bridges operator commands to the orchestrator
    and pushes workflow state changes back to the frontend.

    This is a convenience layer – it does not contain any business logic.
    """

    def __init__(self, host: str = "0.0.0.0", port: int = 8080) -> None:
        self.host = host
        self.port = port
        self._connected_clients: set = set()
        self._command_handlers: Dict[WorkflowCommand, Any] = {}
        self._status_callback: Optional[Callable] = None
        self._subsystem_config: Optional[List[Dict[str, Any]]] = None
        self._lock = asyncio.Lock()
        self._broadcast_lock = threading.Lock()
        self._server: Optional[websockets.Server] = None
        self._event_loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._shutdown_event = threading.Event()
        # Startup handshake between start() and the server thread: the
        # thread signals that the bind attempt finished (success or error)
        # so a failed bind is surfaced loudly instead of dying silently.
        self._startup_done: Optional[threading.Event] = None
        self._start_error: Optional[Exception] = None
        self._workflow_display_config: Dict[str, Any] | None = None
        # State machine graph definitions (subsystem + object) sent with
        # every status broadcast, so the frontend can render them.
        self._state_machine_defs: Dict[str, Any] | None = None
        # Last full-status payload, replayed to newly connected clients so a
        # (re)connecting UI immediately gets state + SM definitions.
        self._last_status_payload: Dict[str, Any] | None = None

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------
    def set_command_handler(
        self,
        command: WorkflowCommand,
        handler: callable,
    ) -> None:
        """Register a handler for a workflow command."""
        self._command_handlers[command] = handler

    def set_status_callback(self, callback: Callable) -> None:
        """Register callback to fetch current WorkflowStatus."""
        self._status_callback = callback

    # ------------------------------------------------------------------
    # WebSocket handler
    # ------------------------------------------------------------------
    async def _ws_handler(self, websocket: ServerConnection) -> None:
        async with self._lock:
            self._connected_clients.add(websocket)
        logger.info("Frontend client connected (total: %d)", len(self._connected_clients))

        # Replay the last full status (workflow state, subsystems, objects
        # and state machine definitions) so a fresh/reconnecting client does
        # not have to wait for the next state change or press Refresh.
        if self._last_status_payload is not None:
            try:
                await websocket.send(json.dumps(self._last_status_payload))
            except Exception:
                pass

        try:
            async for raw in websocket:
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    await websocket.send(
                        json.dumps({"error": "Invalid JSON", "raw": raw})
                    )
                    continue

                cmd = data.get("command") or data.get("type")
                if cmd:
                    # Try to find the handler - check both member members and values
                    handler = None
                    for member in WorkflowCommand:
                        if member.value == cmd or member.name == cmd:
                            handler = self._command_handlers.get(member.value)
                            break
                    
                    if handler:
                        if asyncio.iscoroutinefunction(handler):
                            await handler(data)
                        else:
                            handler(data)
                    else:
                        logger.warning("No handler registered for command: %s", cmd)
                else:
                    logger.warning("Unrecognized message: %s", data)

                # Echo back a received-acknowledgement
                await websocket.send(json.dumps({"type": "ack", "received": True}))

        except websockets.ConnectionClosed:
            logger.info("Frontend client disconnected")
        except Exception as exc:
            logger.error("WebSocket handler error: %s", exc)
        finally:
            async with self._lock:
                self._connected_clients.discard(websocket)

    # ------------------------------------------------------------------
    # Broadcasting state
    # ------------------------------------------------------------------
    def broadcast_status(self, status: WorkflowStatus, extra_details: Dict[str, Any] | None = None) -> None:
        """Push current workflow status to all connected clients.

        Includes subsystem configuration so the frontend can build its
        subsystem cards dynamically.
        """
        status_dict = status.to_dict()
        # Include subsystem config if available
        if self._subsystem_config is not None:
            status_dict["subsystems_config"] = self._subsystem_config
            logger.debug("Including subsystem_config in broadcast (%d subsystems)", len(self._subsystem_config))
        else:
            logger.debug("subsystem_config is None, not including in broadcast")
        # Include state machine graph definitions (subsystem + object SMs)
        if self._state_machine_defs is not None:
            status_dict.update(self._state_machine_defs)
        # Include any extra details (e.g. object workflow state order/colors)
        if extra_details:
            status_dict.update(extra_details)

        payload = EventPayload(
            event_type=BackendEvent.WORKFLOW_STATE_CHANGED.value,
            details=status_dict,
        ).to_dict()
        self._last_status_payload = payload
        # Capture clients list while holding thread-safe lock
        with self._broadcast_lock:
            clients = list(self._connected_clients)
        if self._event_loop is not None and self._event_loop.is_running():
            asyncio.run_coroutine_threadsafe(self._broadcast(payload, clients), self._event_loop)

    def broadcast(self, payload: Dict[str, Any]) -> None:
        """Broadcast an arbitrary payload to all connected clients.

        This is used for events that are not workflow state changes,
        such as subsystem state changes or progress updates.
        """
        # Capture clients list while holding thread-safe lock
        with self._broadcast_lock:
            clients = list(self._connected_clients)
        if self._event_loop is not None and self._event_loop.is_running():
            asyncio.run_coroutine_threadsafe(self._broadcast(payload, clients), self._event_loop)

    def set_subsystem_config(self, config_list: List[Dict[str, Any]]) -> None:
        """Set the subsystem configuration to broadcast with status updates."""
        self._subsystem_config = config_list
        logger.info("Subsystem config set: %d subsystems", len(config_list))

    def set_state_machine_definitions(
        self,
        subsystem_defs: Dict[str, Dict[str, Any]],
        object_def: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Set state machine graph definitions to broadcast with status.

        The definitions come from the same YAML files that drive the
        engines (subsystem adapters + object blueprint), so the frontend
        always renders the actual machine, not a hardcoded copy.
        """
        self._state_machine_defs = {
            "subsystem_state_machines": subsystem_defs,
            "object_state_machine": object_def,
        }
        logger.info(
            "State machine definitions set: %d subsystems, object=%s",
            len(subsystem_defs),
            "yes" if object_def else "no",
        )

    def set_workflow_display_config(
        self,
        state_order: List[str],
        state_colors: Dict[str, str],
        state_messages: Optional[Dict[str, str]] = None,
    ) -> None:
        """Set object workflow display config (state order, colors and
        per-workflow-state display messages) to broadcast with status updates.

        The messages come from the `workflow_states` section of global.yaml
        so the UI text stays config-driven.
        """
        self._workflow_display_config = {
            "state_order": state_order,
            "state_colors": state_colors,
            "state_messages": state_messages or {},
        }
        logger.info(
            "Workflow display config set: %d states, %d messages",
            len(state_order),
            len(state_messages or {}),
        )



    async def _broadcast(self, payload: Dict[str, Any], clients: list) -> None:
        for ws in clients:
            try:
                await ws.send(json.dumps(payload))
            except Exception:
                pass

    def broadcast_with_display_config(self, status: WorkflowStatus) -> None:
        """Broadcast status including workflow display config (state order/colors)."""
        extra = None
        if self._workflow_display_config is not None:
            extra = self._workflow_display_config
        self.broadcast_status(status, extra_details=extra)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Start the WebSocket server in a background thread.

        Raises RuntimeError if the server cannot bind (e.g. the port is
        already in use). Deliberately NO reuse_port: a second listener on
        the same port would make the kernel load-balance operator
        connections across two orchestrator instances with divergent
        state (a silent "process swamp").
        """
        self._shutdown_event.clear()
        self._start_error = None
        self._startup_done = threading.Event()
        loop = asyncio.new_event_loop()
        self._event_loop = loop

        async def _run() -> None:
            # websockets 14.x API:
            # websockets.serve() returns a 'serve' object that needs to be awaited
            # to get the actual Server instance. The bind happens here.
            try:
                serve_obj = websockets.serve(
                    self._ws_handler,
                    self.host,
                    self.port,
                )
                self._server = await serve_obj
            except Exception as exc:
                self._start_error = exc
                self._startup_done.set()
                logger.error(
                    "Frontend server failed to bind ws://%s:%d: %s",
                    self.host, self.port, exc,
                )
                return
            logger.info("Frontend server listening on ws://%s:%d", self.host, self.port)
            self._startup_done.set()
            # Wait until the server is closed (by stop())
            await self._server.wait_closed()

        self._thread = threading.Thread(
            target=lambda: loop.run_until_complete(_run()),
            daemon=False,
        )
        self._thread.start()

        # Wait for the bind attempt to finish and fail loudly on error.
        if not self._startup_done.wait(timeout=10):
            raise RuntimeError(
                f"Frontend server did not start within 10s on "
                f"{self.host}:{self.port}"
            )
        if self._start_error is not None:
            raise RuntimeError(
                f"Frontend server could not start on {self.host}:{self.port} "
                f"({self._start_error}). Is another orchestrator already "
                "running? Stop it first or use a different port."
            ) from self._start_error

    async def _shutdown_server(self) -> None:
        """Close the server and wait for all connections to drain.

        Must run on the server's own event loop: websockets 14's
        ``Server.close()`` schedules its work with ``loop.create_task()``,
        which is NOT thread-safe when called from another thread (the loop
        is never woken and ``wait_closed()`` hangs forever).
        """
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    def stop(self) -> None:
        """Stop the WebSocket server and wait for it to fully shut down."""
        # Signal shutdown on the server's event loop, from this thread, in a
        # thread-safe way. The server thread exits on its own once the close
        # completes (its run_until_complete(_run()) returns).
        if (
            self._server is not None
            and self._event_loop is not None
            and self._event_loop.is_running()
        ):
            future = asyncio.run_coroutine_threadsafe(
                self._shutdown_server(), self._event_loop
            )
            try:
                future.result(timeout=2.0)
            except Exception:
                logger.exception("Timed out waiting for server shutdown")

        # Wait for the server thread to exit (should be immediate now).
        # We must NOT try to close the event loop from another thread.
        # The event loop will be garbage-collected when the thread exits
        # and all references are dropped.
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=0.5)

        # Clear references so the event loop and server can be GC'd
        self._event_loop = None
        self._server = None
        self._shutdown_event.set()
