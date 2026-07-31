"""MQTT client wrapper for subsystem communication."""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, Optional

import paho.mqtt.client as mqtt

logger = logging.getLogger(__name__)

TOPIC_SUBSYSTEM = "automata/subsystem"
TOPIC_COMMAND = "command"
TOPIC_STATUS = "status"
TOPIC_RESPONSE = "response"


class MqttClient:
    """Wrapper around the paho-mqtt client."""

    def __init__(self, broker: str = "localhost", port: int = 1883) -> None:
        self.broker = broker
        self.port = port
        self._client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._on_message: Optional[Callable[[str, bytes], None]] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def connect(self) -> None:
        self._client.connect(self.broker, self.port, 60)
        self._client.loop_start()
        logger.info("MQTT connected to %s:%d", self.broker, self.port)

    def disconnect(self) -> None:
        try:
            self._client.loop_stop(timeout=0.1)
            self._client.disconnect(timeout=0.1)
        except Exception:
            pass
        logger.info("MQTT disconnected")

    # ------------------------------------------------------------------
    # Publish / Subscribe
    # ------------------------------------------------------------------
    def publish(self, topic: str, payload: Any, qos: int = 1) -> None:
        self._client.publish(topic, json.dumps(payload) if not isinstance(payload, str) else payload, qos=qos)
        logger.debug("MQTT publish: %s", topic)

    def subscribe(self, topic: str, qos: int = 0) -> None:
        self._client.subscribe(topic, qos=qos)
        logger.debug("MQTT subscribe: %s", topic)

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------
    @property
    def is_connected(self) -> bool:
        """Check if MQTT client is connected."""
        return self._client.is_connected()

    def _on_connect(self, client: mqtt.Client, userdata: Any, flags: Any, rc: int, properties: Any = None) -> None:
        if rc == 0:
            logger.info("MQTT connection confirmed")
        else:
            logger.error("MQTT connection failed with code %d", rc)

    def _on_disconnect(self, client: mqtt.Client, userdata: Any, flags: Any, rc: int, properties: Any = None) -> None:
        # paho-mqtt 2.x passes a ReasonCode enum; older versions pass an int
        if rc is None:
            rc_val = 0
        elif isinstance(rc, int):
            rc_val = rc
        else:
            rc_val = int(rc) if hasattr(rc, '__int__') else 0
        logger.info("MQTT disconnected with code %d", rc_val)

    def _on_message_wrapper(self, client: mqtt.Client, userdata: Any, msg: mqtt.MQTTMessage) -> None:
        try:
            topic = msg.topic
            payload = msg.payload
            if self._on_message:
                self._on_message(topic, payload)
        except Exception as exc:
            logger.error("MQTT message handler error: %s", exc)
