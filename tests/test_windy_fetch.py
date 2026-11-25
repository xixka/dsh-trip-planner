#!/usr/bin/env python3
"""Offline tests for scripts/windy_fetch.py helpers; never touches the network."""

from datetime import datetime, timezone, timedelta
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import windy_fetch  # noqa: E402


class CelestialLocalTest(unittest.TestCase):
    """Wind returns UTC ISO + epoch-ms only; the local block must mirror it
    in the destination wall time without touching original fields."""

    def test_local_block_offsets_epoch_fields(self):
        # 1795560251286 ms == 2026-11-24 22:44:11 UTC == 北京 11-25 06:44:11
        # (日出前于当天 06:44, 与深圳 11-25 实测一致)
        sunrise_ms = 1795560251286
        data = {"sunriseTs": sunrise_ms, "sunsetTs": 1795599598889,
                "duskTs": 1795601036906,
                "night": "2026-11-25T10:58:29.593Z"}
        original = dict(data)
        local = windy_fetch.celestial_local(data, 8)
        self.assertEqual(local["sunrise"], "2026-11-25T06:44:11.286000+08:00")
        self.assertEqual(local["sunset"], "2026-11-25T17:39:58.889000+08:00")
        self.assertEqual(local["dusk"], "2026-11-25T18:03:56.906000+08:00")
        self.assertEqual(local["night"], "2026-11-25T18:58:29.593000+08:00")
        self.assertEqual(local["tz_hours"], 8)
        # original fields untouched
        for key, value in original.items():
            self.assertEqual(data[key], value)

    def test_other_offset_and_missing_fields(self):
        local = windy_fetch.celestial_local({"sunriseTs": 1795560251286}, -5)
        self.assertEqual(local["sunrise"], "2026-11-24T17:44:11.286000-05:00")
        self.assertEqual(local["tz_hours"], -5)
        # nothing usable -> only the tz record, no crash
        self.assertEqual(windy_fetch.celestial_local({}, 8), {"tz_hours": 8})
        # bad night string is tolerated silently
        self.assertEqual(windy_fetch.celestial_local({"night": "oops"}, 8),
                         {"tz_hours": 8})

    def test_dawn_ts_supported(self):
        local = windy_fetch.celestial_local({"dawnTs": 1795560251286}, 8)
        self.assertIn("dawn", local)


class NormalizeStationTest(unittest.TestCase):
    """/pois/v2/stations reports wind in m/s; expose km/h alongside."""

    def test_converts_wind_and_gust(self):
        st = windy_fetch.normalize_station(
            {"name": "GW7927 SHENZHEN CN", "wind": 4.4704, "gust": 5.3645,
             "temp": 22.78, "dist": 1.739})
        self.assertAlmostEqual(st["wind_kmh"], 16.1, places=1)
        self.assertAlmostEqual(st["gust_kmh"], 19.3, places=1)
        # raw fields kept
        self.assertEqual(st["wind"], 4.4704)
        self.assertEqual(st["gust"], 5.3645)

    def test_missing_or_null_wind_left_alone(self):
        st = windy_fetch.normalize_station({"name": "TA KWU LING",
                                            "gust": None, "wind": 3})
        self.assertIn("wind_kmh", st)
        self.assertNotIn("gust_kmh", st)
        # non-dict passthrough
        self.assertEqual(windy_fetch.normalize_station("x"), "x")
        self.assertIsNone(windy_fetch.normalize_station(None))


class UnitNormalizationTest(unittest.TestCase):
    """archive / route / obs endpoints return K / Pa / m/s raw; the CLI must
    expose converted columns without destroying the raw arrays."""

    def test_normalize_archive(self):
        data = {"data": {"temperature": [286.24], "wind": [3.58],
                         "windGust": [5.0], "pressure": [101900.0],
                         "feelTemperature": [283.0]}}
        windy_fetch.normalize_archive(data)
        self.assertEqual(data["data"]["temp_c"], [13.1])
        self.assertEqual(data["data"]["wind_kmh"], [12.9])
        self.assertEqual(data["data"]["gust_kmh"], [18.0])
        self.assertEqual(data["data"]["pressure_hpa"], [1019.0])
        self.assertEqual(data["data"]["feels_c"], [9.9])
        # raw arrays untouched
        self.assertEqual(data["data"]["temperature"], [286.24])

    def test_normalize_route(self):
        data = {"data": {"temp": [276.5], "wind": [1.0], "gust": [3.05],
                         "vis": [20295]}}
        windy_fetch.normalize_route(data)
        self.assertEqual(data["data"]["temp_c"], [3.4])
        self.assertEqual(data["data"]["wind_kmh"], [3.6])
        self.assertEqual(data["data"]["gust_kmh"], [11.0])
        self.assertEqual(data["data"]["vis"], [20295])  # metres, untouched

    def test_normalize_obs(self):
        data = {"data": {"temp": [270.15], "dewPoint": [264.15],
                         "wind": [1.0], "gust": [None], "pressure": [102900.0]}}
        windy_fetch.normalize_obs(data)
        self.assertEqual(data["data"]["temp_c"], [-3.0])
        self.assertEqual(data["data"]["dew_point_c"], [-9.0])
        self.assertEqual(data["data"]["wind_kmh"], [3.6])
        self.assertIsNone(data["data"]["gust_kmh"][0])
        self.assertEqual(data["data"]["pressure_hpa"], [1029.0])

    def test_normalize_helpers_ignore_bad_payloads(self):
        for payload in (None, "x", {"data": None}, {"data": "x"}, {}):
            windy_fetch.normalize_archive(payload)
            windy_fetch.normalize_route(payload)
            windy_fetch.normalize_obs(payload)  # must not raise


class ForecastStepNoteTest(unittest.TestCase):
    """--step 1 silently returns the model-native cadence; meta must say so."""

    def test_meta_records_effective_step_and_note(self):
        base = 1795543200000  # ms
        data = {"data": {"ts": [base, base + 10800000, base + 21600000]},
                "meta": {"source": "Windy gfs x"}}
        windy_fetch.forecast_step_note(data, 1)
        self.assertEqual(data["meta"]["effective_step_hours"], 3.0)
        self.assertIn("3 h sampling", data["meta"]["note"])
        self.assertIn("--step 1", data["meta"]["note"])

    def test_matching_step_has_no_note(self):
        base = 1795543200000
        data = {"data": {"ts": [base, base + 10800000]}, "meta": {}}
        windy_fetch.forecast_step_note(data, 3)
        self.assertEqual(data["meta"]["effective_step_hours"], 3.0)
        self.assertNotIn("note", data["meta"])

    def test_malformed_payload_is_ignored(self):
        for payload in ({"data": None}, {"data": {}}, {"data": {"ts": []}},
                        {"data": {"ts": [1]}}, "not-a-dict", None):
            windy_fetch.forecast_step_note(payload, 1)  # must not raise

    def test_irregular_steps_report_coarsest_gap(self):
        base = 1795543200000
        data = {"data": {"ts": [base, base + 10800000, base + 25200000]},
                "meta": {}}
        windy_fetch.forecast_step_note(data, 3)
        self.assertEqual(data["meta"]["effective_step_hours"], 4.0)
        self.assertIn("note", data["meta"])  # coarsest gap 4 h > requested 3 h


class CliWiringTest(unittest.TestCase):
    """Parser must accept the new flags so cmd_all's Namespace stays valid."""

    def test_celestial_help_mentions_tz_hours(self):
        import subprocess
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "windy_fetch.py"),
             "celestial", "--help"],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("--tz-hours", proc.stdout)

    def test_all_help_mentions_tz_hours_and_celestial(self):
        import subprocess
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "windy_fetch.py"),
             "all", "--help"],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("--tz-hours", proc.stdout)

    def test_default_tz_is_beijing(self):
        self.assertEqual(windy_fetch.DEFAULT_TZ_HOURS, 8)


if __name__ == "__main__":
    unittest.main()
