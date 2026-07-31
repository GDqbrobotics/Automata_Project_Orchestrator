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
    def broadcast_status(self, status: WorkflowStatus) -> None:
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

        payload = EventPayload(
            event_type=BackendEvent.WORKFLOW_STATE_CHANGED.value,
            details=status_dict,
        ).to_dict()
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



    async def _broadcast(self, payload: Dict[str, Any], clients: list) -> None:
        for ws in clients:
            try:
                await ws.send(json.dumps(payload))
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Start the WebSocket server in a background thread."""
        self._shutdown_event.clear()
        loop = asyncio.new_event_loop()
        self._event_loop = loop

        async def _run() -> None:
            # websockets 14.x API:
            # websockets.serve() returns a 'serve' object that needs to be awaited
            # to get the actual Server instance.
            serve_obj = websockets.serve(
                self._ws_handler,
                self.host,
                self.port,
                reuse_port=True,
            )
            self._server = await serve_obj
            logger.info("Frontend server listening on ws://%s:%d", self.host, self.port)
            # Wait until the server is closed (by stop())
            await self._server.wait_closed()

        self._thread = threading.Thread(
            target=lambda: loop.run_until_complete(_run()),
            daemon=False,
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the WebSocket server and wait for it to fully shut down."""
        # Signal shutdown by closing the server
        if self._server is not None:
            self._server.close()

        # Wait for the server thread to exit (with timeout)
        # We must NOT try to close the event loop from another thread.
        # The event loop will be garbage-collected when the thread exits
        # and all references are dropped.
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=0.5)

        # Clear references so the event loop and server can be GC'd
        self._event_loop = None
        self._server = None
        self._shutdown_event.set()
