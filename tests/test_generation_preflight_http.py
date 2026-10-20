"""No-key HTTP requests preserve requirements before template fallback."""
import json
import sys
import threading
import unittest
from pathlib import Path
from http.server import ThreadingHTTPServer
from urllib.request import urlopen
from urllib.parse import quote
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server
from core import config


class GenerationPreflightHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.patches = [patch.object(config, "API_KEY", ""),
                       patch.object(server.rate_limiter, "allow", return_value=True)]
        for item in cls.patches:
            item.start()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        for item in cls.patches:
            item.stop()

    def events(self, prompt):
        with urlopen(f"http://127.0.0.1:{self.httpd.server_port}/api/generate?q=" + quote(prompt), timeout=30) as response:
            raw = response.read().decode()
        result = []
        for block in raw.split("\n\n"):
            event = next((line[7:] for line in block.splitlines() if line.startswith("event: ")), None)
            data = next((line[6:] for line in block.splitlines() if line.startswith("data: ")), None)
            if event and data:
                result.append((event, json.loads(data)))
        return result

    def assert_rejected(self, prompt):
        events = self.events(prompt)
        self.assertNotIn("circuit", [event for event, data in events])
        done = next(data for event, data in events if event == "done")
        self.assertTrue(done.get("rejected"))
        self.assertFalse(done.get("accepted", False))
        return events

    def test_no_key_mcu_i2c_never_becomes_led(self):
        self.assert_rejected("3.3V ESP32连接BMP280走I2C，地址0x76，上拉3.3V，公共地")

    def test_partial_alias_cannot_drop_required_mcu(self):
        self.assert_rejected("5V供电ATmega328P驱动LED指示灯，用470欧电阻限流")

    def test_missing_rail_requires_clarification_before_fallback(self):
        events = self.assert_rejected("ATmega328P连接BMP280走I2C，地址0x76，供电电压未定")
        self.assertTrue(next(data for event, data in events if event == "fallback").get("questions"))

    def test_missing_address_requires_clarification_before_fallback(self):
        events = self.assert_rejected("3.3V ESP32连接BMP280走I2C，地址未定")
        self.assertTrue(next(data for event, data in events if event == "fallback").get("questions"))

    def test_unmatched_request_never_receives_default_led(self):
        self.assert_rejected("设计一个RC低通滤波器，R为1k，C为100nF")

    def test_explicit_template_still_preserves_parameters(self):
        handler = object.__new__(server.Handler)
        circuit, name = handler._pick_fallback("5V电池用470欧姆电阻串联红色LED做限流指示灯")
        resistor = next(p for p in circuit["parts"] if p["type"] == "resistor")
        self.assertIn("470", resistor["attrs"]["value"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
