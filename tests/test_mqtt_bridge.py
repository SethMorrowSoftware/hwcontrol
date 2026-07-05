"""Regression tests for the MQTT bridge: startup with an unreachable broker,
and handler dispatch off the network-loop thread (in arrival order)."""
import os
import sys
import threading
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mqtt_bridge import MqttBridge


def msg(topic, payload):
    return types.SimpleNamespace(topic=topic, payload=payload)


class StartWithBrokerDown(unittest.TestCase):
    def test_start_does_not_raise_when_broker_unreachable(self):
        """The app and the broker race to boot after a power blink; start() must
        keep retrying in the background instead of failing once and staying dead."""
        b = MqttBridge(host="127.0.0.1", port=59999)   # nothing listening
        try:
            b.start()   # would raise ConnectionRefusedError before the fix
        finally:
            b.stop()


class WorkerDispatch(unittest.TestCase):
    def setUp(self):
        self.results = []
        self.done = threading.Event()
        self.bridge = MqttBridge(
            host="127.0.0.1", port=59999,
            command_handler=self._on_command,
            trigger_handler=self._on_trigger,
        )
        self.bridge.start()

    def tearDown(self):
        self.bridge.stop()

    def _on_command(self, device_id, command):
        self.results.append(("command", device_id, command, threading.current_thread().name))
        self.done.set()

    def _on_trigger(self, topic, payload):
        self.results.append(("trigger", topic, payload, threading.current_thread().name))
        self.done.set()

    def test_command_parsed_and_handled_off_the_network_thread(self):
        self.bridge._on_message(None, None, msg("honeywell/DEV1/set/heat", b"70"))
        self.assertTrue(self.done.wait(timeout=5), "command handler never ran")
        kind, did, command, thread_name = self.results[0]
        self.assertEqual((kind, did), ("command", "DEV1"))
        self.assertEqual(command, {"heatSetpoint": 70.0,
                                   "thermostatSetpointStatus": "TemporaryHold"})
        self.assertEqual(thread_name, "mqtt-work",
                         "handler must run on the worker, not the caller/network thread")

    def test_trigger_dispatch_preserves_arrival_order(self):
        self.bridge.sync_trigger_topics({"facility/generator/status"})
        self.bridge._on_message(None, None, msg("facility/generator/status", b"on"))
        self.bridge._on_message(None, None, msg("facility/generator/status", b"off"))
        deadline = threading.Event()
        for _ in range(50):
            if len(self.results) >= 2:
                break
            deadline.wait(0.1)
        payloads = [r[2] for r in self.results if r[0] == "trigger"]
        self.assertEqual(payloads, ["on", "off"], "trigger order must be preserved")

    def test_malformed_command_payload_is_dropped_not_fatal(self):
        self.bridge._on_message(None, None, msg("honeywell/DEV1/set/heat", b"not-a-number"))
        self.bridge._on_message(None, None, msg("honeywell/DEV1/set/mode", b"Heat"))
        self.assertTrue(self.done.wait(timeout=5))
        kinds = [(r[0], r[2]) for r in self.results]
        self.assertEqual(kinds, [("command", {"mode": "Heat"})],
                         "bad payload must be skipped, later commands still handled")


if __name__ == "__main__":
    unittest.main()
