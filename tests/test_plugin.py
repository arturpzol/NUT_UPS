"""
Tests for the NUT_UPS Domoticz plugin, without a real UPS and without Domoticz.

A fake NUT server (upsd) runs on localhost and can answer in small chunks, with "ERR ..." replies,
or not at all (to exercise timeouts). The Domoticz module is replaced by a mock.

Run from the repository root:  python3 -m unittest discover -s tests -v
"""
import os
import re
import shlex
import socket
import sys
import threading
import time
import types
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# --------------------------------------------------------------------------------------------------
# Domoticz mock (must be installed before plugin.py is imported)
# --------------------------------------------------------------------------------------------------
DEVICES = {}
LOG = {"error": [], "debug": [], "log": []}


class FakeDevice:
    def __init__(self, Name, Unit, TypeName, Used=0, Image=None, Options=None):
        self.Name = Name
        self.Unit = Unit
        self.TypeName = TypeName
        self.Used = Used
        self.Image = Image
        self.Options = Options
        self.ID = Unit
        self.nValue = 0
        self.sValue = ""
        self.TimedOut = 0
        self.LastLevel = 0
        self.updates = []

    def Create(self):
        DEVICES[self.Unit] = self

    def Update(self, nValue, sValue, TimedOut=None):
        self.nValue = nValue
        self.sValue = sValue
        if TimedOut is not None:
            self.TimedOut = TimedOut
        self.updates.append((nValue, sValue, TimedOut))


domoticz_mock = types.ModuleType("Domoticz")
domoticz_mock.Debug = lambda msg: LOG["debug"].append(msg)
domoticz_mock.Error = lambda msg: LOG["error"].append(msg)
domoticz_mock.Log = lambda msg: LOG["log"].append(msg)
domoticz_mock.Debugging = lambda level: None
domoticz_mock.Device = FakeDevice
sys.modules["Domoticz"] = domoticz_mock

sys.path.insert(0, REPO_ROOT)
import plugin  # noqa: E402

plugin.Devices = DEVICES


# --------------------------------------------------------------------------------------------------
# Fake upsd
# --------------------------------------------------------------------------------------------------
DEFAULT_VARS = {
    "battery.charge": "100",
    "battery.runtime": "1830",
    "battery.type": "PbAc",
    "device.mfr": "Eaton \"Ellipse\" \\ ECO",  # value with escaped quote and backslash
    "input.voltage": "231.0",
    "ups.load": "17",
    "ups.realpower": "52",
    "input.frequency": "50.0",
    "ups.status": "OL CHRG",
}


def quote(value):
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class FakeUPSD:
    """Tiny upsd look-alike. Set attributes before a poll to change its behaviour."""

    def __init__(self, ups="ups", variables=None):
        self.ups = ups
        self.variables = dict(DEFAULT_VARS if variables is None else variables)
        self.chunk_size = None  # send replies in chunks of this many bytes
        self.chunk_delay = 0.0  # pause between chunks
        self.users = None  # {username: password}; None = accept any credentials
        self.list_reply = None  # raw bytes to send instead of a normal LIST VAR reply
        self.hang_after = None  # bytes: send this for LIST VAR, then never finish the reply
        self.commands = []
        self.connections = 0
        self.disconnects = 0
        self.lock = threading.Lock()
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(5)
        self.port = self.server.getsockname()[1]
        self.running = True
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def stop(self):
        self.running = False
        try:
            self.server.close()
        except OSError:
            pass

    def wait_disconnects(self, count, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                if self.disconnects >= count:
                    return True
            time.sleep(0.01)
        return False

    def _accept_loop(self):
        while self.running:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            with self.lock:
                self.connections += 1
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _send(self, conn, data):
        if not self.chunk_size:
            conn.sendall(data)
            return
        for i in range(0, len(data), self.chunk_size):
            conn.sendall(data[i:i + self.chunk_size])
            if self.chunk_delay:
                time.sleep(self.chunk_delay)

    def _list_var(self, name):
        if name != self.ups:
            return b"ERR UNKNOWN-UPS\n"
        lines = ["BEGIN LIST VAR {}".format(name)]
        lines += ["VAR {} {} {}".format(name, key, quote(value)) for key, value in self.variables.items()]
        lines.append("END LIST VAR {}".format(name))
        return ("\n".join(lines) + "\n").encode()

    def _handle(self, conn):
        buffer = b""
        username = None
        try:
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buffer += chunk
                while b"\n" in buffer:
                    raw, buffer = buffer.split(b"\n", 1)
                    line = raw.decode().strip()
                    with self.lock:
                        self.commands.append(line)
                    try:
                        words = shlex.split(line)  # upsd accepts "quoted" arguments with \ escapes
                    except ValueError:
                        self._send(conn, b"ERR INVALID-ARGUMENT\n")
                        continue
                    if not words:
                        continue
                    if words[0] == "USERNAME" and len(words) == 2:
                        username = words[1]
                        self._send(conn, b"OK\n")
                    elif words[0] == "PASSWORD" and len(words) == 2:
                        password = words[1]
                        if self.users is None or self.users.get(username) == password:
                            self._send(conn, b"OK\n")
                        else:
                            self._send(conn, b"ERR ACCESS-DENIED\n")
                    elif words[0] in ("USERNAME", "PASSWORD"):
                        self._send(conn, b"ERR INVALID-ARGUMENT\n")
                    elif words[:2] == ["LIST", "VAR"] and len(words) == 3:
                        if self.hang_after is not None:
                            self._send(conn, self.hang_after)
                        elif self.list_reply is not None:
                            self._send(conn, self.list_reply)
                        else:
                            self._send(conn, self._list_var(words[2]))
                    elif words[0] == "LOGOUT":
                        self._send(conn, b"OK Goodbye\n")
                        return
                    else:
                        self._send(conn, b"ERR UNKNOWN-COMMAND\n")
        except OSError:
            pass
        finally:
            conn.close()
            with self.lock:
                self.disconnects += 1


def free_port():
    """A localhost port with nothing listening on it."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# --------------------------------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------------------------------
class NUTClientTests(unittest.TestCase):

    def setUp(self):
        self.upsd = FakeUPSD()

    def tearDown(self):
        self.upsd.stop()

    def client(self, read_timeout=2):
        nut = plugin.NUTClient("127.0.0.1", str(self.upsd.port), connect_timeout=2, read_timeout=read_timeout)
        nut.connect()
        return nut

    def test_list_vars_parses_all_variables(self):
        nut = self.client()
        try:
            values = nut.list_vars("ups")
        finally:
            nut.close()
        self.assertEqual(values, DEFAULT_VARS)

    def test_list_vars_one_byte_chunks(self):
        self.upsd.chunk_size = 1
        nut = self.client()
        try:
            values = nut.list_vars("ups")
        finally:
            nut.close()
        self.assertEqual(values, DEFAULT_VARS)

    def test_list_vars_slow_chunks_with_split_end_marker(self):
        # 7-byte chunks with a delay: the "END LIST VAR ups" marker arrives split over several recv() calls
        self.upsd.chunk_size = 7
        self.upsd.chunk_delay = 0.005
        nut = self.client()
        try:
            values = nut.list_vars("ups")
        finally:
            nut.close()
        self.assertEqual(values, DEFAULT_VARS)

    def test_read_until_keeps_data_after_marker(self):
        # both lines are sent in one sendall(), so "second" normally arrives in the same recv() as "first"
        self.upsd.list_reply = b"first\nsecond\n"
        nut = self.client()
        try:
            nut.write("LIST VAR ups")
            self.assertEqual(nut.read_until(b"\n"), b"first\n")
            self.assertEqual(nut.read_line(), "second")
            self.assertEqual(nut.buffer, b"")
        finally:
            nut.close()

    def test_err_reply_raises(self):
        nut = self.client()
        try:
            with self.assertRaises(plugin.NUTError) as ctx:
                nut.list_vars("other")
        finally:
            nut.close()
        self.assertEqual(str(ctx.exception), "ERR UNKNOWN-UPS")

    def test_timeout_raises_and_respects_deadline(self):
        self.upsd.hang_after = b"BEGIN LIST VAR ups\nVAR ups battery.charge \"100\"\n"
        nut = self.client(read_timeout=0.3)
        start = time.monotonic()
        try:
            with self.assertRaises(plugin.NUTError):
                nut.list_vars("ups")
        finally:
            nut.close()
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 1.5)
        self.assertTrue(self.upsd.wait_disconnects(1))
        self.assertIn("LOGOUT", self.upsd.commands)

    def test_connection_closed_by_server_raises(self):
        self.upsd.list_reply = b"BEGIN LIST VAR ups\n"
        nut = self.client()
        nut.write("LIST VAR ups")
        nut.read_line()
        nut.write("LOGOUT")  # fake server answers and closes the connection
        self.assertEqual(nut.read_line(), "OK Goodbye")
        with self.assertRaises(plugin.NUTError):
            nut.read_line()
        nut.close()

    def test_malformed_line_raises(self):
        self.upsd.list_reply = b"BEGIN LIST VAR ups\nVAR ups battery.charge 100\nEND LIST VAR ups\n"
        nut = self.client()
        try:
            with self.assertRaises(plugin.NUTError):
                nut.list_vars("ups")
        finally:
            nut.close()

    def test_crlf_line_endings(self):
        self.upsd.list_reply = (b"BEGIN LIST VAR ups\r\nVAR ups battery.charge \"99\"\r\n"
                                b"VAR ups ups.status \"OL\"\r\nEND LIST VAR ups\r\n")
        nut = self.client()
        try:
            self.assertEqual(nut.list_vars("ups"), {"battery.charge": "99", "ups.status": "OL"})
        finally:
            nut.close()

    def test_escaped_closing_quote_rejected(self):
        self.upsd.list_reply = b'BEGIN LIST VAR ups\nVAR ups device.mfr "abc\\"\nEND LIST VAR ups\n'
        nut = self.client()
        try:
            with self.assertRaises(plugin.NUTError):
                nut.list_vars("ups")
        finally:
            nut.close()

    def test_unescaped_inner_quote_rejected(self):
        self.upsd.list_reply = b'BEGIN LIST VAR ups\nVAR ups device.mfr "a"b"\nEND LIST VAR ups\n'
        nut = self.client()
        try:
            with self.assertRaises(plugin.NUTError):
                nut.list_vars("ups")
        finally:
            nut.close()

    def test_unquote(self):
        self.assertEqual(plugin.NUTClient.unquote('"a \\"b\\" \\\\ c"'), 'a "b" \\ c')
        self.assertEqual(plugin.NUTClient.unquote('""'), "")
        for bad in ['', '"', 'abc', '"abc', '"abc\\"', '"a"b"']:
            with self.assertRaises(ValueError, msg=bad):
                plugin.NUTClient.unquote(bad)

    def test_buffer_limit_does_not_reject_complete_response(self):
        reply = b"BEGIN LIST VAR ups\n"
        self.upsd.list_reply = reply
        nut = self.client()
        nut.MAX_BUFFER = len(reply) - 5  # the whole reply arrives in one chunk, larger than the limit
        try:
            nut.write("LIST VAR ups")
            self.assertEqual(nut.read_line(), "BEGIN LIST VAR ups")
        finally:
            nut.close()

    def test_buffer_limit_rejects_endless_response(self):
        self.upsd.hang_after = b"x" * 5000  # no newline, never ends
        nut = self.client()
        nut.MAX_BUFFER = 1000
        try:
            nut.write("LIST VAR ups")
            with self.assertRaises(plugin.NUTError) as ctx:
                nut.read_line()
        finally:
            nut.close()
        self.assertIn("too large", str(ctx.exception))

    def test_quote(self):
        self.assertEqual(plugin.NUTClient.quote("monuser"), "monuser")
        self.assertEqual(plugin.NUTClient.quote("my secret"), '"my secret"')
        self.assertEqual(plugin.NUTClient.quote('a"b\\c'), '"a\\"b\\\\c"')
        self.assertEqual(plugin.NUTClient.quote(""), '""')

    def test_close_sends_logout(self):
        nut = self.client()
        nut.list_vars("ups")
        nut.close()
        self.assertTrue(self.upsd.wait_disconnects(1))
        self.assertEqual(self.upsd.commands, ["LIST VAR ups", "LOGOUT"])
        self.assertIsNone(nut.sock)
        nut.close()  # closing twice is harmless

    def test_newline_injection_rejected(self):
        nut = self.client()
        try:
            with self.assertRaises(plugin.NUTError):
                nut.login("admin\nLIST VAR ups", "x")
        finally:
            nut.close()


class PluginTests(unittest.TestCase):

    def setUp(self):
        DEVICES.clear()
        for messages in LOG.values():
            messages.clear()
        self.upsd = FakeUPSD()
        plugin.Parameters = {
            "Address": "127.0.0.1",
            "Port": str(self.upsd.port),
            "Username": "",
            "Password": "",
            "Mode1": "ups",
            "Mode6": "Normal",
        }
        self.plugin = plugin.BasePlugin()
        self.plugin.readtimeout = 0.5  # keep the timeout tests fast
        self.plugin.onStart()

    def tearDown(self):
        self.upsd.stop()

    def poll(self):
        self.plugin.nextpoll = datetime.now()
        self.plugin.onHeartbeat()

    def test_first_and_second_poll(self):
        self.assertEqual(list(DEVICES), [1])
        self.assertEqual(DEVICES[1].Name, "UPS Status Mode")
        self.assertEqual(DEVICES[1].TypeName, "Alert")

        self.poll()  # creates the child devices, updates the status device
        self.assertEqual(LOG["error"], [])
        self.assertEqual((DEVICES[1].nValue, DEVICES[1].sValue), (1, "ONLINE CHARGING"))
        self.assertFalse(DEVICES[1].TimedOut)
        expected = {
            2: ("UPS Charge", "%", 1),
            3: ("UPS Backup Time", "s", 1),
            4: ("UPS AC Input", "V", 0),
            5: ("UPS Load", "%", 0),
            6: ("UPS Power", "W", 0),
            7: ("UPS AC Frequency", "Hz", 0),
        }
        self.assertEqual(sorted(DEVICES), [1, 2, 3, 4, 5, 6, 7])
        for unit, (name, unit_label, used) in expected.items():
            device = DEVICES[unit]
            self.assertEqual(device.Name, name)
            self.assertEqual(device.TypeName, "Custom")
            self.assertEqual(device.Image, 17)
            self.assertEqual(device.Options, {"Custom": "1;{}".format(unit_label)})
            self.assertEqual(device.Used, used)
            self.assertEqual(device.updates, [])  # created now, updated on the next poll

        self.poll()  # updates the child devices
        values = {unit: DEVICES[unit].sValue for unit in expected}
        self.assertEqual(values, {2: "100", 3: "1830", 4: "231.0", 5: "17", 6: "52", 7: "50.0"})
        self.assertTrue(self.upsd.wait_disconnects(2))
        self.assertEqual(self.upsd.commands.count("LOGOUT"), 2)

    def test_poll_with_chunked_responses(self):
        self.upsd.chunk_size = 3
        self.poll()
        self.poll()
        self.assertEqual(LOG["error"], [])
        self.assertEqual(DEVICES[3].sValue, "1830")

    def test_missing_variable_does_not_create_device(self):
        del self.upsd.variables["battery.runtime"]
        self.poll()
        self.poll()
        self.assertNotIn(3, DEVICES)
        self.assertEqual(DEVICES[2].sValue, "100")

    def test_authentication_success(self):
        plugin.Parameters["Username"] = "monuser"
        plugin.Parameters["Password"] = "secret"
        self.upsd.users = {"monuser": "secret"}
        self.poll()
        self.assertEqual(LOG["error"], [])
        self.assertEqual(self.upsd.commands[:3], ["USERNAME monuser", "PASSWORD secret", "LIST VAR ups"])
        self.assertEqual(DEVICES[1].sValue, "ONLINE CHARGING")

    def test_authentication_with_special_characters(self):
        plugin.Parameters["Username"] = "mon user"
        plugin.Parameters["Password"] = 'my "secret" \\ pw'
        self.upsd.users = {"mon user": 'my "secret" \\ pw'}
        self.poll()
        self.assertEqual(LOG["error"], [])
        self.assertEqual(self.upsd.commands[:2], ['USERNAME "mon user"', 'PASSWORD "my \\"secret\\" \\\\ pw"'])
        self.assertEqual(DEVICES[1].sValue, "ONLINE CHARGING")

    def test_authentication_failure(self):
        plugin.Parameters["Username"] = "monuser"
        plugin.Parameters["Password"] = "wrong"
        self.upsd.users = {"monuser": "secret"}
        self.poll()
        self.assertEqual(len(LOG["error"]), 1)
        self.assertIn("Authentication error", LOG["error"][0])
        self.assertIn("ERR ACCESS-DENIED", LOG["error"][0])
        self.assertEqual(DEVICES[1].sValue, "COMMUNICATION ERROR")
        self.assertTrue(DEVICES[1].TimedOut)
        self.assertNotIn("LIST VAR ups", self.upsd.commands)
        self.assertTrue(self.upsd.wait_disconnects(1))
        self.assertEqual(self.upsd.commands[-1], "LOGOUT")

    def test_connection_refused(self):
        plugin.Parameters["Port"] = str(free_port())
        self.poll()
        self.assertEqual(len(LOG["error"]), 1)
        self.assertIn("Cannot communicate with NUT Server", LOG["error"][0])
        self.assertEqual(DEVICES[1].sValue, "COMMUNICATION ERROR")
        self.assertTrue(DEVICES[1].TimedOut)

    def test_err_reply_after_good_poll(self):
        self.poll()
        self.poll()
        before = {unit: list(DEVICES[unit].updates) for unit in DEVICES if unit != 1}
        plugin.Parameters["Mode1"] = "wrongname"
        self.poll()
        self.assertEqual(LOG["error"], ["Error reading UPS variables: ERR UNKNOWN-UPS"])
        self.assertEqual(DEVICES[1].sValue, "COMMUNICATION ERROR")
        self.assertTrue(DEVICES[1].TimedOut)
        after = {unit: list(DEVICES[unit].updates) for unit in DEVICES if unit != 1}
        self.assertEqual(before, after)  # no value device was updated
        self.assertTrue(self.upsd.wait_disconnects(3))

        plugin.Parameters["Mode1"] = "ups"  # recovery clears the error state
        self.poll()
        self.assertEqual(DEVICES[1].sValue, "ONLINE CHARGING")
        self.assertFalse(DEVICES[1].TimedOut)

    def test_error_on_first_poll_flags_status_device(self):
        plugin.Parameters["Mode1"] = "wrongname"
        self.poll()
        self.assertEqual(LOG["error"], ["Error reading UPS variables: ERR UNKNOWN-UPS"])
        self.assertEqual(DEVICES[1].sValue, "COMMUNICATION ERROR")
        self.assertTrue(DEVICES[1].TimedOut)
        self.assertEqual(sorted(DEVICES), [1])  # no value devices created from a failed poll

    def test_partial_response_does_not_update_devices(self):
        self.poll()
        self.poll()
        self.upsd.variables["battery.charge"] = "42"
        self.upsd.hang_after = b"BEGIN LIST VAR ups\nVAR ups battery.charge \"42\"\n"
        start = time.monotonic()
        self.poll()
        self.assertLess(time.monotonic() - start, 2.0)
        self.assertEqual(len(LOG["error"]), 1)
        self.assertIn("Timeout", LOG["error"][0])
        self.assertEqual(DEVICES[2].sValue, "100")  # the partial "42" was not applied
        self.assertEqual(DEVICES[1].sValue, "COMMUNICATION ERROR")
        self.assertTrue(self.upsd.wait_disconnects(3))
        self.assertEqual(self.upsd.commands[-1], "LOGOUT")

    def test_malformed_response_does_not_update_devices(self):
        self.poll()
        self.poll()
        self.upsd.list_reply = (b"BEGIN LIST VAR ups\nVAR ups battery.charge \"42\"\n"
                                b"garbage line\nEND LIST VAR ups\n")
        self.poll()
        self.assertEqual(len(LOG["error"]), 1)
        self.assertEqual(DEVICES[2].sValue, "100")
        self.assertEqual(DEVICES[1].sValue, "COMMUNICATION ERROR")

    def test_unknown_status_flag_is_reported_as_is(self):
        self.upsd.variables["ups.status"] = "OB LB XYZ"
        self.poll()
        self.assertEqual((DEVICES[1].nValue, DEVICES[1].sValue), (4, "ONBATTERY LOWBATTERY XYZ"))


class PluginDefinitionTests(unittest.TestCase):
    """Guards backward compatibility of the plugin definition shown in the Domoticz hardware page."""

    def test_plugin_xml(self):
        with open(os.path.join(REPO_ROOT, "plugin.py"), encoding="utf-8") as f:
            source = f.read()
        xml = re.search(r"<plugin .*?</plugin>", source, re.S).group(0)
        root = ET.fromstring(xml)
        self.assertEqual(root.get("key"), "NUT_UPS")
        self.assertEqual(root.get("name"), "UPS Monitor")
        self.assertEqual(root.get("version"), "0.3.0")
        fields = [(p.get("field"), p.get("default")) for p in root.iter("param")]
        self.assertEqual(fields, [("Address", "127.0.0.1"), ("Port", "3493"), ("Username", ""),
                                  ("Password", ""), ("Mode1", "ups"), ("Mode6", None)])
        self.assertIsNone(re.search(r"^\s*(import|from)\s+telnetlib", source, re.M))


if __name__ == "__main__":
    unittest.main()
