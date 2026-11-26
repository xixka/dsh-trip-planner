#!/usr/bin/env python3
"""Offline tests for scripts/trip_config.py (shared JSON settings)."""

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import trip_config


class TripConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.file = Path(self.temp.name) / "settings.json"
        trip_config.set_settings_path(self.file)

    def tearDown(self):
        trip_config.set_settings_path(None)
        self.temp.cleanup()

    def test_missing_file_returns_defaults(self):
        self.assertEqual(trip_config.load(), trip_config.DEFAULTS)
        self.assertEqual(trip_config.get("amapKey", "fallback"), "fallback")

    def test_save_and_load_round_trip(self):
        saved = trip_config.save({
            "amapKey": "  key-1  ",
            "mapProvider": "baidu",
            "unknown": "dropped",
        })
        self.assertEqual(saved["amapKey"], "key-1")
        self.assertEqual(saved["mapProvider"], "baidu")
        self.assertNotIn("unknown", saved)
        self.assertEqual(trip_config.get("amapKey"), "key-1")
        self.assertEqual(json.loads(self.file.read_text(encoding="utf-8")),
                         trip_config.DEFAULTS | {"amapKey": "key-1", "mapProvider": "baidu"})

    def test_invalid_provider_falls_back_to_amap(self):
        data = trip_config.normalize({"mapProvider": "nope"})
        self.assertEqual(data["mapProvider"], "amap")

    def test_status_never_contains_secret_values(self):
        trip_config.save({"amapKey": "secret-amap", "flyaiApiKey": "secret-flyai"})
        summary = json.dumps(trip_config.status(), ensure_ascii=False)
        self.assertNotIn("secret-amap", summary)
        self.assertNotIn("secret-flyai", summary)
        self.assertTrue(trip_config.status()["configured"]["amapKey"])
        self.assertTrue(trip_config.status()["configured"]["flyaiApiKey"])

    def test_cli_set_does_not_echo_value(self):
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            code = trip_config.main(["set", "amapKey", "secret-value"])
        self.assertEqual(code, 0)
        self.assertNotIn("secret-value", out.getvalue())
        self.assertEqual(trip_config.get("amapKey"), "secret-value")

    def test_cli_get_and_status(self):
        trip_config.save({"amapKey": "key-1"})
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(trip_config.main(["get", "amapKey"]), 0)
        self.assertEqual(out.getvalue().strip(), "key-1")
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(trip_config.main(["status"]), 0)
        payload = json.loads(out.getvalue())
        self.assertTrue(payload["configured"]["amapKey"])
        self.assertEqual(payload["settingsPath"], str(self.file))


if __name__ == "__main__":
    unittest.main()
