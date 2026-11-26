import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import urllib.error
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "themes"))

import plan_lint
import qc
import render_plan
import route_tools
import theme_common


def settings_patch(values):
    """Point route_tools' shared settings at a fixed dict (no env vars)."""
    return mock.patch.object(
        route_tools.trip_config, "get",
        side_effect=lambda key, default=None: values.get(key, default))


class FakeResponse:
    def __init__(self, value, raw=False):
        self.raw = value if raw else json.dumps(value).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.raw


def success_route(mode="walk", minutes=8):
    return {
        "mode": mode,
        "distance_m": 800,
        "duration_min": minutes,
        "path": [[39.9042, 116.4074], [39.9050, 116.4080]],
        "provider": "amap",
        "queried_at": "2026-01-02T03:04:05Z",
        "status": "ok",
        "source": "live",
    }


def plan_with_stops(modes):
    stops = []
    rows = []
    for index, mode in enumerate(["walk"] + list(modes)):
        stops.append({
            "name": "站{}".format(index),
            "query": "站{}, 北京".format(index),
            "lat": 39.9042 + index * 0.001,
            "lon": 116.4074 + index * 0.001,
            "mode": mode,
        })
        if index:
            rows.append({
                "t": "09:0{}-09:1{}".format(index, index),
                "what": "人工路线 站{}→站{}".format(index - 1, index),
                "kind": "hop",
                "link": "https://uri.amap.com/navigation?manual={}".format(index),
                "price": "人工价格{}".format(index),
                "verify": "est",
            })
    return {
        "trip": "离线测试",
        "days": [{
            "date": "2026-01-02",
            "timeline": rows,
            "stops": stops,
        }],
    }


class RouteParsingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures = json.loads(
            (ROOT / "tests/fixtures/amap_routes.json").read_text(encoding="utf-8"))
        cls.ca = (39.9042, 116.4074)
        cls.cb = (39.9142, 116.4174)

    def parse(self, mode, fixture, city="010"):
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse(fixture)) as request:
            result = route_tools._amap_direction_route(
                self.ca, self.cb, mode, "test-only", city=city, strategy=0)
        return result, request.call_args[0][0].full_url

    def test_coordinate_roundtrip(self):
        for point in (self.ca, self.cb, (30.2318, 120.1489), (22.3193, 114.1694)):
            converted = route_tools.wgs84_to_gcj02(*point)
            restored = route_tools.gcj02_to_wgs84(*converted)
            self.assertAlmostEqual(point[0], restored[0], places=5)
            self.assertAlmostEqual(point[1], restored[1], places=5)

    def test_walking_selects_best_within_strategy_not_first(self):
        result, url = self.parse("walk", self.fixtures["walking"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["duration_min"], 10)
        self.assertEqual(result["distance_m"], 1200)
        self.assertEqual(result["source"], "live")
        self.assertGreaterEqual(len(result["path"]), 2)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        expected = route_tools.wgs84_to_gcj02(*self.ca)
        self.assertEqual(query["origin"][0], "{:.6f},{:.6f}".format(expected[1], expected[0]))
        self.assertNotIn("strategy", query)

    def test_driving_uses_only_explicit_tolls(self):
        result, url = self.parse("drive", self.fixtures["driving"])
        self.assertEqual(result["duration_min"], 15)
        self.assertEqual(result["distance_m"], 7000)
        self.assertEqual(result["cost"], {"amount": 5, "currency": "CNY", "kind": "road_tolls"})
        self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["strategy"], ["0"])

    def test_transit_transfer_lines_stations_cost_and_direction(self):
        result, url = self.parse("transit", self.fixtures["transit_transfer"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["duration_min"], 25)
        self.assertEqual(result["distance_m"], 5640)
        self.assertEqual(result["distance_basis"], "segments_sum")
        self.assertEqual(result["cost"]["amount"], 5)
        lines = [segment for segment in result["segments"] if segment.get("line")]
        self.assertEqual([segment["line"] for segment in lines], ["地铁1号线", "地铁4号线"])
        self.assertEqual(lines[0]["direction"], "苹果园")
        self.assertNotIn("direction", lines[1])
        self.assertEqual((lines[0]["from"], lines[0]["to"]), ("天安门东", "西单"))
        self.assertEqual(lines[0]["station_count"], 2)
        self.assertLessEqual(len(result["path"]), route_tools.ROUTE_PATH_MAX)
        transfer = route_tools.gcj02_to_wgs84(39.9055, 116.4086)
        self.assertTrue(any(abs(p[0] - transfer[0]) < 1e-6 and abs(p[1] - transfer[1]) < 1e-6
                            for p in result["path"]))
        self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["city"], ["010"])

    def test_transit_strategy_and_explicit_departure(self):
        economic = copy.deepcopy(self.fixtures["transit_transfer"])
        economic["route"]["transits"][0]["cost"] = "1"
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse(economic)) as request:
            result = route_tools._amap_direction_route(
                self.ca, self.cb, "transit", "test-only", city="010", strategy=1,
                departure_date="2026-01-02", departure_time="09:05")
        self.assertEqual(result["cost"]["amount"], 1)
        self.assertEqual(result["duration_min"], 50)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(
            request.call_args[0][0].full_url).query)
        self.assertEqual(query["date"], ["2026-01-02"])
        self.assertEqual(query["time"], ["09:05"])

    def test_no_route_zero_and_missing_fields(self):
        no_route, _ = self.parse("walk", self.fixtures["no_route"])
        zero, _ = self.parse("walk", self.fixtures["zero_route"])
        partial, _ = self.parse("walk", self.fixtures["missing_fields"])
        self.assertEqual(no_route["status"], "no_route")
        self.assertEqual(zero["status"], "no_route")
        self.assertNotIn("path", no_route)
        self.assertNotIn("duration_min", zero)
        self.assertEqual(partial["status"], "partial")
        self.assertEqual(partial["error"], {"kind": "missing_fields", "fields": ["duration_min"]})
        self.assertEqual(partial["distance_m"], 1200)

    def test_qps_and_timeout_have_one_bounded_retry(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=[FakeResponse(self.fixtures["qps"]),
                                     FakeResponse(self.fixtures["walking"])]) as request, \
             mock.patch.object(route_tools.time, "sleep", return_value=None):
            result = route_tools._amap_direction_route(self.ca, self.cb, "walk", "test-only")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(request.call_count, 2)

        with mock.patch("urllib.request.urlopen",
                        side_effect=[TimeoutError("fixture timeout"),
                                     FakeResponse(self.fixtures["walking"])]) as request:
            result = route_tools._amap_direction_route(self.ca, self.cb, "walk", "test-only")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(request.call_count, 2)

        with mock.patch("urllib.request.urlopen", side_effect=TimeoutError("fixture timeout")) as request:
            with self.assertRaises(route_tools.RouteRequestError) as caught:
                route_tools._amap_direction_route(self.ca, self.cb, "walk", "test-only")
        self.assertEqual(caught.exception.kind, "timeout")
        self.assertEqual(request.call_count, 2)

    def test_api_error_is_classified_without_key_or_request_url(self):
        bad = {"status": "0", "info": "QPS test-only https://restapi.amap.com/v3/x?key=test-only"}
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse(bad)), \
             mock.patch.object(route_tools.time, "sleep", return_value=None):
            with self.assertRaises(route_tools.RouteRequestError) as caught:
                route_tools._amap_direction_route(self.ca, self.cb, "walk", "test-only")
        self.assertEqual(caught.exception.kind, "qps")
        self.assertNotIn("test-only", str(caught.exception))
        self.assertNotIn("restapi.amap.com", str(caught.exception))

    def test_legacy_minute_interface_is_unchanged(self):
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse(self.fixtures["driving"])):
            minutes = route_tools._amap_direction_minutes(self.ca, self.cb, "drive", "test-only")
        self.assertEqual(minutes, 30)  # the legacy function intentionally keeps its first path

    def test_path_cap_preserves_endpoints_and_key_point(self):
        points = [(39.9 + i * 0.0001, 116.4 + (i % 7) * 0.0002) for i in range(200)]
        result = route_tools.simplify_route_path(points, 20, protected=[points[77]])
        self.assertLessEqual(len(result), 20)
        for actual, expected in ((result[0], points[0]), (result[-1], points[-1])):
            self.assertAlmostEqual(actual[0], expected[0], places=7)
            self.assertAlmostEqual(actual[1], expected[1], places=7)
        self.assertTrue(any(abs(p[0] - points[77][0]) < 1e-7
                            and abs(p[1] - points[77][1]) < 1e-7 for p in result))

    def test_cache_identity_and_ttl(self):
        key = route_tools._route_cache_key("transit", self.ca, self.cb, "010", "010", 0, "2026-01-02|09:00")
        for args in (("walk", self.ca, self.cb, "010", "010", 0, "2026-01-02|09:00"),
                     ("transit", self.ca, self.cb, "021", "021", 0, "2026-01-02|09:00"),
                     ("transit", self.ca, self.cb, "010", "010", 1, "2026-01-02|09:00"),
                     ("transit", self.ca, self.cb, "010", "010", 0, "2026-01-03|09:00")):
            self.assertNotEqual(key, route_tools._route_cache_key(*args))
        cache = {}
        route_tools._cache_structured_route(cache, key, success_route("transit"), now=1000)
        cached = route_tools._cached_structured_route(cache, key, 50, now=1049)
        self.assertEqual(cached["source"], "cache")
        self.assertIsNone(route_tools._cached_structured_route(cache, key, 50, now=1050))
        self.assertIsNone(route_tools._cached_structured_route(cache, key, 0, now=1001))


class RouteCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.settings = settings_patch({"amapKey": "test-only"})

    def tearDown(self):
        self.settings.stop()
        self.temp.cleanup()

    def write_plan(self, plan):
        path = self.directory / "plan.geo.json"
        path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        return path

    def args(self, path, write=True):
        return SimpleNamespace(plan=str(path), write=write, ttl=3600, strategy=0)

    def test_cross_city_transit_degrades_without_request_and_keeps_manual_row(self):
        plan = plan_with_stops(["transit"])
        path = self.write_plan(plan)
        (self.directory / "geocache.json").write_text(json.dumps({
            "站0, 北京": {"citycode": "010"},
            "站1, 北京": {"citycode": "021"},
        }), encoding="utf-8")
        with self.settings, mock.patch.object(route_tools, "_amap_direction_route") as request, \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            route_tools.cmd_routes(self.args(path))
        request.assert_not_called()
        saved = json.loads(path.read_text(encoding="utf-8"))
        route = saved["days"][0]["stops"][1]["route"]
        self.assertEqual(route["status"], "degraded")
        self.assertEqual(route["error"]["kind"], "cross_city_transit")
        self.assertEqual(saved["days"][0]["timeline"], plan["days"][0]["timeline"])

    def test_single_failure_does_not_block_other_hops_or_clear_manual_fields(self):
        plan = plan_with_stops(["walk", "walk", "walk"])
        path = self.write_plan(plan)
        with self.settings, mock.patch.object(
                route_tools, "_amap_direction_route",
                side_effect=[success_route(minutes=7),
                             route_tools.RouteRequestError("timeout", "fixture timeout"),
                             success_route(minutes=9)]) as request, \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            route_tools.cmd_routes(self.args(path))
        self.assertEqual(request.call_count, 3)
        saved = json.loads(path.read_text(encoding="utf-8"))
        routes = [stop.get("route") for stop in saved["days"][0]["stops"]]
        self.assertIsNone(routes[0])
        self.assertEqual([route["status"] for route in routes[1:]], ["ok", "error", "ok"])
        self.assertEqual(routes[2]["error"]["kind"], "timeout")
        self.assertEqual(saved["days"][0]["timeline"], plan["days"][0]["timeline"])
        cache = json.loads((self.directory / "routecache.json").read_text(encoding="utf-8"))
        self.assertEqual(len([key for key in cache if key.startswith(route_tools.ROUTE_CACHE_PREFIX)]), 2)
        self.assertTrue(all("test-only" not in key for key in cache))

    def test_dry_run_does_not_write_plan_or_cache(self):
        plan = plan_with_stops(["walk"])
        path = self.write_plan(plan)
        before = path.read_bytes()
        with self.settings, mock.patch.object(route_tools, "_amap_direction_route", return_value=success_route()), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            route_tools.cmd_routes(self.args(path, write=False))
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse((self.directory / "routecache.json").exists())

    def test_missing_key_never_writes_even_with_write_flag(self):
        plan = plan_with_stops(["walk"])
        path = self.write_plan(plan)
        before = path.read_bytes()
        with settings_patch({}), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            route_tools.cmd_routes(self.args(path, write=True))
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse((self.directory / "routecache.json").exists())

    def test_check_live_does_not_write_plan(self):
        plan = plan_with_stops(["transit"])
        path = self.write_plan(plan)
        before = path.read_bytes()
        args = SimpleNamespace(plan=str(path), live=True, amap_key=None)
        with self.settings, mock.patch.object(route_tools, "_live_dur", return_value="~8 min (高德公交实时)"), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            route_tools.cmd_check(args)
        self.assertEqual(path.read_bytes(), before)

    def test_failed_refresh_preserves_old_route_data_but_marks_it_unusable(self):
        plan = plan_with_stops(["walk"])
        old = success_route()
        old["manual_note"] = "保留人工备注"
        plan["days"][0]["stops"][1]["route"] = old
        path = self.write_plan(plan)
        with self.settings, mock.patch.object(
                route_tools, "_amap_direction_route",
                side_effect=route_tools.RouteRequestError("qps", "fixture qps")), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            route_tools.cmd_routes(self.args(path))
        route = json.loads(path.read_text(encoding="utf-8"))["days"][0]["stops"][1]["route"]
        self.assertEqual(route["status"], "error")
        self.assertEqual(route["manual_note"], "保留人工备注")
        self.assertEqual(route["path"], old["path"])
        self.assertEqual(render_plan._route_path(route), [])


class RenderAndLintTests(unittest.TestCase):
    def setUp(self):
        theme_common.set_lang("zh")
        plan_lint.fails[:] = []
        plan_lint.warns[:] = []

    def route_plan(self):
        plan = plan_with_stops(["transit"])
        route = success_route("transit", 25)
        route.update({
            "from": "站0", "to": "站1",
            "cost": {"amount": 5, "currency": "CNY", "kind": "transit_fare"},
            "segments": [{
                "mode": "transit", "line": "地铁1号线", "from": "天安门东", "to": "西单",
                "station_count": 2, "duration_min": 10,
            }],
        })
        plan["days"][0]["stops"][1]["route"] = route
        return plan

    def test_old_plan_loads_lints_and_renders_without_route_fields(self):
        plan = plan_with_stops(["walk"])
        for stop in plan["days"][0]["stops"]:
            stop.pop("route", None)
        self.assertEqual(render_plan._hop_routes(plan["days"][0]), {})
        theme_common.norm_plan(plan)
        self.assertNotIn("_route", plan["days"][0]["timeline"][0])
        html = render_plan.render(plan)
        self.assertIn("站点连线示意", html)
        plan_lint.validate_routes(plan)
        self.assertEqual(plan_lint.fails, [])

    def test_route_details_paths_and_html_security(self):
        plan = self.route_plan()
        theme_common.norm_plan(plan)
        row = plan["days"][0]["timeline"][0]
        self.assertIn("地铁1号线", theme_common.route_detail(row["_route"]))
        html = render_plan.render(plan)
        self.assertIn("地铁1号线", html)
        self.assertIn('class="real"', html)
        self.assertNotIn("test-only", html)
        self.assertNotIn("restapi.amap.com", html)
        self.assertNotIn('<script src="https://', html)
        plan_lint.validate_routes(plan)
        self.assertEqual(plan_lint.fails, [])

    def test_misaligned_rows_hide_details_but_keep_old_link(self):
        plan = self.route_plan()
        plan["days"][0]["timeline"].insert(0, {"kind": "hop", "what": "其他行", "map": False})
        plan["days"][0]["timeline"].insert(1, {"kind": "hop", "what": "无法配对"})
        html = render_plan.render(plan)
        self.assertIn("无法配对", html)
        self.assertNotIn("route-data", html)

    def test_lint_rejects_zero_success_and_request_material(self):
        plan = self.route_plan()
        route = plan["days"][0]["stops"][1]["route"]
        route["distance_m"] = 0
        route["request_url"] = "https://restapi.amap.com/v3/direction/walking"
        with contextlib.redirect_stdout(io.StringIO()):
            plan_lint.validate_routes(plan)
        self.assertGreaterEqual(len(plan_lint.fails), 2)

    def test_static_map_partial_path_note_and_embedded_png(self):
        plan = self.route_plan()
        day = plan["days"][0]
        day["stops"].append({
            "name": "站2", "lat": 39.9162, "lon": 116.4194, "mode": "walk",
            "route": {"mode": "walk", "status": "error", "source": "estimated",
                      "provider": "amap", "queried_at": "2026-01-02T03:04:05Z",
                      "error": {"kind": "timeout"}},
        })
        theme_common._STATICMAP_CACHE.clear()
        with settings_patch({"amapKey": "test-only"}), \
             mock.patch("urllib.request.urlopen", return_value=FakeResponse(b"\x89PNG\r\nfixture", raw=True)) as request:
            uri = theme_common.day_embed_url(day)
        self.assertTrue(uri.startswith("data:image/png;base64,"))
        self.assertNotIn("test-only", uri)
        self.assertNotIn("restapi.amap.com", uri)
        self.assertIn("部分为直线示意", theme_common.day_map_note_html(day))
        url = request.call_args[0][0].full_url
        self.assertIn("|4,0x7A8A99,0.65,,", url)
        self.assertNotIn("|paths=", url)

    def test_static_map_without_path_retains_old_polyline_and_hides_without_key(self):
        day = plan_with_stops(["walk"])["days"][0]
        theme_common._STATICMAP_CACHE.clear()
        with settings_patch({"amapKey": "test-only"}), \
             mock.patch("urllib.request.urlopen", return_value=FakeResponse(b"\x89PNG\r\nfixture", raw=True)) as request:
            theme_common.day_embed_url(day)
        url = request.call_args[0][0].full_url
        self.assertIn("paths=8,0x1E6FFF,0.85,,", url)
        self.assertNotIn("|paths=", url)
        self.assertIn("站点连线示意", theme_common.day_map_note_html(day))
        with settings_patch({}):
            self.assertEqual(theme_common.day_embed_url(day), "")

    def test_static_map_error_log_does_not_include_key_or_url(self):
        day = plan_with_stops(["walk"])["days"][0]
        theme_common._STATICMAP_CACHE.clear()
        error = urllib.error.HTTPError(
            "https://restapi.amap.com/v3/staticmap?key=test-only", 500,
            "fixture", {}, None)
        stderr = io.StringIO()
        with settings_patch({"amapKey": "test-only"}), \
             mock.patch("urllib.request.urlopen", side_effect=error), contextlib.redirect_stderr(stderr):
            self.assertEqual(theme_common.day_embed_url(day), "")
        self.assertNotIn("test-only", stderr.getvalue())
        self.assertNotIn("restapi.amap.com", stderr.getvalue())

    def test_qc_rejects_request_url_and_external_map_script(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "page.html"
            path.write_text('<html><style>:focus-visible{}</style><h1>x</h1>'
                            '<p>https://restapi.amap.com/v3/staticmap?key=secret</p></html>')
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertGreater(qc.check(path), 0)
            path.write_text('<html><style>:focus-visible{}</style><h1>x</h1>'
                            '<script src="https://maps.example.invalid/map.js"></script></html>')
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertGreater(qc.check(path), 0)


if __name__ == "__main__":
    unittest.main()
