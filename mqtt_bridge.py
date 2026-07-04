"""
mqtt_bridge.py
--------------
Bridges the Honeywell state into *your* MQTT broker and lets other systems
(a BMS, Home Assistant, Node-RED, custom controllers) drive the thermostats.

The Honeywell API itself has no MQTT and no webhook-to-you; it only pushes to an
Azure Event Hub for enterprise accounts. So this bridge is how facility systems
integrate: the poller publishes state here, and commands arriving here are turned
into API calls.

Topic layout (BASE defaults to "honeywell"):

  Published (retained):
    honeywell/<deviceID>/state          full JSON snapshot of the thermostat
    honeywell/<deviceID>/online         "true" / "false"
    honeywell/status/bridge             "online" / "offline" (LWT)

  Published (not retained):
    honeywell/<deviceID>/event          JSON of a single change event
    honeywell/alerts                    JSON of an alert (offline, temp, etc.)

  Subscribed (you publish these to send commands):
    honeywell/<deviceID>/set            JSON body, e.g.
                                        {"heatSetpoint": 70, "thermostatSetpointStatus": "TemporaryHold"}
    honeywell/<deviceID>/set/heat       plain number -> heat setpoint (TemporaryHold)
    honeywell/<deviceID>/set/cool       plain number -> cool setpoint (TemporaryHold)
    honeywell/<deviceID>/set/mode       "Heat" | "Cool" | "Off" | "Auto"
    honeywell/<deviceID>/set/fan        "Auto" | "On" | "Circulate"

The command handler is injected so this module stays decoupled from the client.
It must have the signature:  handler(device_id: str, command: dict) -> None
"""

from __future__ import annotations

import json
import logging
from typing import Callable, Optional

try:
    import paho.mqtt.client as mqtt
except ImportError:  # pragma: no cover
    mqtt = None

log = logging.getLogger("honeywell.mqtt")

CommandHandler = Callable[[str, dict], None]


class MqttBridge:
    def __init__(
        self,
        host: str,
        port: int = 1883,
        base_topic: str = "honeywell",
        username: str = "",
        password: str = "",
        command_handler: Optional[CommandHandler] = None,
        trigger_handler: Optional[Callable[[str, str], None]] = None,
    ):
        if mqtt is None:
            raise RuntimeError("paho-mqtt is not installed. `pip install paho-mqtt`")
        self.base = base_topic.rstrip("/")
        self.host = host
        self.port = port
        self.command_handler = command_handler
        # trigger_handler(topic, payload) is called for messages on subscribed
        # automation trigger topics (e.g. a generator status topic).
        self.trigger_handler = trigger_handler
        self._trigger_topics: set[str] = set()
        self._connected = False

        self._client = mqtt.Client(client_id=f"{self.base}-bridge")
        if username:
            self._client.username_pw_set(username, password)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        # Last will: if the bridge dies, subscribers see it go offline.
        self._client.will_set(f"{self.base}/status/bridge", "offline", retain=True)

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        log.info("Connecting to MQTT %s:%s", self.host, self.port)
        self._client.connect(self.host, self.port, keepalive=60)
        self._client.loop_start()  # runs the network loop in a background thread

    def stop(self) -> None:
        try:
            self._client.publish(f"{self.base}/status/bridge", "offline", retain=True)
            self._client.loop_stop()
            self._client.disconnect()
        except Exception:  # pragma: no cover
            pass

    @property
    def connected(self) -> bool:
        return self._connected

    def set_trigger_handler(self, handler: Callable[[str, str], None]) -> None:
        self.trigger_handler = handler

    def sync_trigger_topics(self, topics: set[str]) -> None:
        """Make our subscriptions match `topics` exactly. Safe to call anytime;
        subscribes new ones and unsubscribes removed ones."""
        topics = set(topics)
        to_add = topics - self._trigger_topics
        to_remove = self._trigger_topics - topics
        self._trigger_topics = topics
        if not self._connected:
            return  # on_connect will subscribe the current set
        for t in to_add:
            self._client.subscribe(t)
            log.info("Subscribed to trigger topic '%s'", t)
        for t in to_remove:
            self._client.unsubscribe(t)
            log.info("Unsubscribed from trigger topic '%s'", t)

    def _on_connect(self, client, userdata, flags, rc):
        if rc != 0:
            log.error("MQTT connect failed with code %s", rc)
            return
        self._connected = True
        client.publish(f"{self.base}/status/bridge", "online", retain=True)
        # Subscribe to every command topic under our namespace.
        client.subscribe(f"{self.base}/+/set")
        client.subscribe(f"{self.base}/+/set/+")
        # Re-subscribe to any automation trigger topics (survives reconnects).
        for topic in list(self._trigger_topics):
            client.subscribe(topic)
        log.info("MQTT connected; subscribed to command topics and %d trigger topic(s).",
                 len(self._trigger_topics))

    # ------------------------------------------------------------- inbound

    def _on_message(self, client, userdata, msg):
        # Automation trigger topics take priority and are matched exactly.
        if msg.topic in self._trigger_topics:
            if self.trigger_handler:
                try:
                    self.trigger_handler(msg.topic, msg.payload.decode(errors="replace"))
                except Exception as exc:
                    log.error("Trigger handler failed for %s: %s", msg.topic, exc)
            return

        if self.command_handler is None:
            return
        # Only messages under our command namespace are commands.
        if not msg.topic.startswith(self.base + "/"):
            return
        try:
            parts = msg.topic.split("/")
            # base / <deviceID> / set [ / <field> ]
            device_id = parts[1]
            payload = msg.payload.decode().strip()
            command: dict

            if parts[-1] == "set":  # full JSON command
                command = json.loads(payload) if payload else {}
            else:
                field = parts[-1]
                if field == "heat":
                    command = {"heatSetpoint": float(payload),
                               "thermostatSetpointStatus": "TemporaryHold"}
                elif field == "cool":
                    command = {"coolSetpoint": float(payload),
                               "thermostatSetpointStatus": "TemporaryHold"}
                elif field == "mode":
                    command = {"mode": payload}
                elif field == "fan":
                    command = {"fan": payload}
                else:
                    log.warning("Unknown command field '%s'", field)
                    return

            log.info("MQTT command for %s: %s", device_id, command)
            self.command_handler(device_id, command)
        except Exception as exc:
            log.error("Failed to handle MQTT message on %s: %s", msg.topic, exc)

    # ------------------------------------------------------------- outbound

    def publish_state(self, device: dict) -> None:
        if not self._connected:
            return
        did = device.get("deviceID")
        if not did:
            return
        self._client.publish(f"{self.base}/{did}/state", json.dumps(device), retain=True)
        self._client.publish(f"{self.base}/{did}/online",
                             "true" if device.get("online") else "false", retain=True)

    def publish_event(self, event: dict) -> None:
        if not self._connected:
            return
        did = event.get("deviceID", "unknown")
        self._client.publish(f"{self.base}/{did}/event", json.dumps(event))

    def publish_alert(self, alert: dict) -> None:
        if not self._connected:
            return
        self._client.publish(f"{self.base}/alerts", json.dumps(alert))
