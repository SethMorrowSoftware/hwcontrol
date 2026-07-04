"""
mqtt_bridge.py
--------------
Bridges the Honeywell state into *your* MQTT broker and lets other systems
(a BMS, Home Assistant, Node-RED, custom controllers) drive the thermostats.

The Honeywell API itself has no MQTT and no webhook-to-you; it only pushes to an
Azure Event Hub for enterprise accounts. So this bridge is how facility systems
integrate: the poller publishes state here, and commands arriving here are turned
into API calls.

Reliability notes (this is the path the generator load-shed rides on):

  * We connect with clean_session=False and a stable client id, and subscribe /
    publish at QoS 1, so a brief broker blip during an outage does not silently
    drop the "utility restored" message that triggers the restore. The broker
    queues QoS-1 messages for our session and re-delivers on reconnect. Pair this
    with a *retained* generator-status topic on the publisher side so a
    reconnecting bridge always re-reads the current state.
  * An on_disconnect callback flips `connected` back to False and raises an
    operator alert. Without it the status lied ("connected" forever) and state
    publishes silently no-op'd on a dead socket during the one failure the system
    calls critical.

Topic layout (BASE defaults to "honeywell"):

  Published (retained, QoS 1):
    honeywell/<deviceID>/state          full JSON snapshot of the thermostat
    honeywell/<deviceID>/online         "true" / "false"
    honeywell/status/bridge             "online" / "offline" (LWT)

  Published (not retained, QoS 1):
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
import threading
from typing import Callable, Optional

try:
    import paho.mqtt.client as mqtt
except ImportError:  # pragma: no cover
    mqtt = None

log = logging.getLogger("honeywell.mqtt")

CommandHandler = Callable[[str, dict], None]

# QoS 1 (at-least-once) on the paths the automation engine depends on: a
# duplicate is harmless (the engine's on_change edge logic de-dupes), but a
# dropped "generator off" is not.
_QOS = 1


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
        on_connection_change: Optional[Callable[[bool], None]] = None,
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
        # on_connection_change(connected: bool) lets the app raise an operator
        # alert when the broker link drops or comes back.
        self.on_connection_change = on_connection_change
        self._trigger_topics: set[str] = set()
        self._topics_lock = threading.Lock()
        self._connected = False

        # paho-mqtt 2.x requires an explicit callback API version; 1.x doesn't
        # know the argument. Pin to the v1 callback signatures either way so the
        # on_connect/on_message handlers below work on both major versions.
        # clean_session=False + a stable client id gives us a persistent session
        # so QoS-1 messages queued while we're briefly disconnected are redelivered.
        client_id = f"{self.base}-bridge"
        if hasattr(mqtt, "CallbackAPIVersion"):
            self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1,
                                       client_id=client_id, clean_session=False)
        else:
            self._client = mqtt.Client(client_id=client_id, clean_session=False)
        if username:
            self._client.username_pw_set(username, password)
        # Keep trying to reconnect with capped backoff if the broker goes away.
        try:
            self._client.reconnect_delay_set(min_delay=1, max_delay=60)
        except Exception:  # pragma: no cover - older paho
            pass
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message
        # Last will: if the bridge dies, subscribers see it go offline.
        self._client.will_set(f"{self.base}/status/bridge", "offline", qos=_QOS, retain=True)

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        log.info("Connecting to MQTT %s:%s", self.host, self.port)
        self._client.connect(self.host, self.port, keepalive=60)
        self._client.loop_start()  # runs the network loop in a background thread

    def stop(self) -> None:
        # Publish a graceful "offline" and let it flush BEFORE tearing down the
        # network loop, otherwise the QoS-1 message never leaves the socket and
        # subscribers only find out via the ungraceful-close LWT.
        try:
            if self._connected:
                info = self._client.publish(f"{self.base}/status/bridge", "offline",
                                            qos=_QOS, retain=True)
                try:
                    info.wait_for_publish(timeout=2)
                except Exception:
                    pass
            self._client.disconnect()
            self._client.loop_stop()
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
        with self._topics_lock:
            to_add = topics - self._trigger_topics
            to_remove = self._trigger_topics - topics
            self._trigger_topics = topics
            connected = self._connected
        if not connected:
            return  # on_connect will subscribe the current set
        for t in to_add:
            self._client.subscribe(t, qos=_QOS)
            log.info("Subscribed to trigger topic '%s'", t)
        for t in to_remove:
            self._client.unsubscribe(t)
            log.info("Unsubscribed from trigger topic '%s'", t)

    def _on_connect(self, client, userdata, flags, rc, *args):
        if rc != 0:
            log.error("MQTT connect failed with code %s", rc)
            return
        self._connected = True
        client.publish(f"{self.base}/status/bridge", "online", qos=_QOS, retain=True)
        # Subscribe to every command topic under our namespace.
        client.subscribe(f"{self.base}/+/set", qos=_QOS)
        client.subscribe(f"{self.base}/+/set/+", qos=_QOS)
        # Re-subscribe to any automation trigger topics (survives reconnects).
        with self._topics_lock:
            topics = list(self._trigger_topics)
        for topic in topics:
            client.subscribe(topic, qos=_QOS)
        log.info("MQTT connected; subscribed to command topics and %d trigger topic(s).",
                 len(topics))
        if self.on_connection_change:
            try:
                self.on_connection_change(True)
            except Exception as exc:  # pragma: no cover
                log.error("on_connection_change(True) failed: %s", exc)

    def _on_disconnect(self, client, userdata, rc, *args):
        """Flip our status back to disconnected and raise an operator alert.
        Without this the app believed MQTT was healthy forever after the first
        connect - blinding it to exactly the outage that matters."""
        was = self._connected
        self._connected = False
        if rc != 0:
            log.warning("MQTT disconnected unexpectedly (rc=%s); auto-reconnecting.", rc)
        else:
            log.info("MQTT disconnected.")
        if was and self.on_connection_change:
            try:
                self.on_connection_change(False)
            except Exception as exc:  # pragma: no cover
                log.error("on_connection_change(False) failed: %s", exc)

    # ------------------------------------------------------------- inbound

    def _on_message(self, client, userdata, msg):
        # Automation trigger topics take priority and are matched exactly.
        with self._topics_lock:
            is_trigger = msg.topic in self._trigger_topics
        if is_trigger:
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
            # Strip the (possibly multi-segment) base, leaving:
            #   <deviceID> / set [ / <field> ]
            base_len = len(self.base.split("/"))
            rest = msg.topic.split("/")[base_len:]
            if len(rest) < 2:
                return
            device_id = rest[0]
            payload = msg.payload.decode(errors="replace").strip()
            command: dict

            if rest[-1] == "set":  # full JSON command
                command = json.loads(payload) if payload else {}
            else:
                field = rest[-1]
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

            if not command:
                # An empty/retained '{}' would still cost a real API refresh.
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
        self._client.publish(f"{self.base}/{did}/state", json.dumps(device), qos=_QOS, retain=True)
        self._client.publish(f"{self.base}/{did}/online",
                             "true" if device.get("online") else "false", qos=_QOS, retain=True)

    def publish_event(self, event: dict) -> None:
        if not self._connected:
            return
        did = event.get("deviceID", "unknown")
        self._client.publish(f"{self.base}/{did}/event", json.dumps(event), qos=_QOS)

    def publish_alert(self, alert: dict) -> None:
        if not self._connected:
            return
        self._client.publish(f"{self.base}/alerts", json.dumps(alert), qos=_QOS)
