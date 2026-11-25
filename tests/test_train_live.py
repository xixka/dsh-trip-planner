#!/usr/bin/env python3
"""Offline tests for scripts/train_live.py; never accesses the real 12306."""

import contextlib
import copy
from datetime import datetime, timedelta
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import train_live  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "train_live"


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def payload(name):
    return fixture(name)["result"]


def dated_payload(name, travel_date):
    value = copy.deepcopy(payload(name))

    def update_dates(item):
        if isinstance(item, dict):
            if "start_date" in item:
                item["start_date"] = travel_date
            if "arrive_date" in item:
                item["arrive_date"] = travel_date
            for child in item.values():
                update_dates(child)
        elif isinstance(item, list):
            for child in item:
                update_dates(child)

    rows = json.loads(value["content"][0]["text"])
    update_dates(rows)
    value["content"][0]["text"] = json.dumps(rows, ensure_ascii=False)
    return value


def args_for(**overrides):
    travel_date = overrides.pop("travel_date", "2026-08-15")
    argv = [
        "direct", "--from", "北京南", "--to", "上海虹桥", "--date", travel_date,
        "--presale-days", "0",
    ]
    fixture_name = overrides.pop("fixture_name", None)
    if fixture_name:
        argv += ["--fixture", str(FIXTURES / fixture_name)]
    # The MCP transport is gone; tests pass mcp_command="node unused" to mean
    # "live mode, network mocked" — drop the key instead of passing a flag.
    overrides.pop("mcp_command", None)
    for key, value in overrides.items():
        option = "--" + key.replace("_", "-")
        if isinstance(value, bool):
            if value:
                argv.append(option)
        elif value is not None:
            argv += [option, str(value)]
    return train_live.build_parser().parse_args(argv)


def live_result():
    result = train_live.normalize_direct(payload("direct-live.json"), args_for())
    result["queried_at"] = "2026-08-01T08:00:00+08:00"
    result["source"]["server_version"] = train_live.UPSTREAM_VERSION
    result["attempts"] = 1
    return result


class DirectStatusTests(unittest.TestCase):
    def test_live_direct_has_small_candidate_set_and_one_backup(self):
        result = live_result()
        self.assertEqual(result["status"], "live")
        self.assertEqual(len(result["candidates"]), 3)
        primary = result["candidates"][result["recommendations"]["primary"]]
        backup = result["candidates"][result["recommendations"]["backup"]]
        self.assertEqual(primary["train_code"], "G3")
        self.assertEqual(backup["train_code"], "G5")
        self.assertEqual(primary["from"]["name"], "北京南")
        self.assertEqual(primary["to"]["name"], "上海虹桥")
        seats = {s["name"]: s for s in primary["seats"]}
        self.assertEqual(seats["二等座"]["price_cny"], 553)
        self.assertEqual(seats["二等座"]["availability"]["count"], 12)
        self.assertEqual(result["timezone"], "Asia/Shanghai")
        self.assertIn("12306", result["links"]["official"])
        self.assertIn("ctrip.com", result["links"]["ctrip_check"])

    def test_sample_is_dated_and_cannot_impersonate_live(self):
        result = train_live.run_direct(args_for(fixture_name="direct-live.json"))
        self.assertEqual(result["status"], "sample")
        self.assertEqual(result["sample_date"], "2026-08-01")
        self.assertIsNone(result["queried_at"])
        self.assertIsNone(result["recommendations"]["primary"])
        self.assertIn("样本日期: 2026-08-01（非实时）", train_live.response_to_text(result))
        self.assertNotIn("status: live", train_live.response_to_text(result))

    def test_sold_out_requires_affirmative_unavailable_seats(self):
        result = train_live.normalize_direct(payload("direct-sold-out.json"), args_for())
        self.assertEqual(result["status"], "sold_out")
        self.assertIsNone(result["recommendations"]["primary"])
        self.assertNotIn("¥553", train_live.response_to_text(result))

    def test_unknown_dash_inventory_is_not_sold_out(self):
        result = train_live.normalize_direct(payload("direct-unknown-availability.json"), args_for())
        self.assertEqual(result["status"], "live")
        self.assertEqual(result["candidates"][0]["availability"], "unknown")
        self.assertEqual(result["candidates"][0]["seats"][0]["availability"]["status"], "unknown")

    def test_date_mismatch_and_partial_malformed_rows_cannot_become_live_or_sold_out(self):
        mismatch = train_live.normalize_direct(payload("direct-live.json"), args_for(travel_date="2026-08-16"))
        self.assertEqual(mismatch["status"], "unverified")
        partial = copy.deepcopy(payload("direct-sold-out.json"))
        rows = json.loads(partial["content"][0]["text"])
        rows.append({"unexpected": "row"})
        partial["content"][0]["text"] = json.dumps(rows, ensure_ascii=False)
        result = train_live.normalize_direct(partial, args_for())
        self.assertEqual(result["status"], "unverified")
        self.assertNotEqual(result["status"], "sold_out")
        self.assertEqual(result["malformed_rows"], 1)

    def test_full_upstream_train_flag_combination_is_accepted(self):
        args = args_for(train_flags="GDZTKOFS")
        self.assertEqual(args.train_flags, "GDZTKOFS")
        self.assertEqual(train_live.normalize_direct(payload("direct-live.json"), args)["status"], "live")

    def test_valid_empty_list_is_no_service_not_sold_out(self):
        result = train_live.normalize_direct(payload("direct-no-service.json"), args_for())
        self.assertEqual(result["status"], "no_service")
        self.assertEqual(result["reason"], "valid_empty_result_not_sold_out")

    def test_empty_transport_response_is_unavailable_not_no_service(self):
        result = train_live.run_direct(args_for(fixture_name="direct-empty-response.json"))
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["error"]["class"], "empty_response")
        self.assertNotEqual(result["status"], "no_service")

    def test_malformed_json_is_unverified(self):
        result = train_live.run_direct(args_for(fixture_name="direct-malformed-response.json"))
        self.assertEqual(result["status"], "unverified")
        self.assertEqual(result["error"]["class"], "malformed_response")

    def test_timeout_and_rate_limit_have_distinct_error_classes(self):
        timeout = train_live.run_direct(args_for(fixture_name="direct-timeout.json"))
        limited = train_live.run_direct(args_for(fixture_name="direct-rate-limited.json"))
        self.assertEqual(timeout["status"], "unavailable")
        self.assertEqual(timeout["error"]["class"], "timeout")
        self.assertEqual(limited["status"], "unavailable")
        self.assertEqual(limited["error"]["class"], "rate_limited")
        self.assertNotEqual(timeout["status"], "no_service")
        self.assertNotEqual(limited["status"], "no_service")

    def test_explicit_not_on_sale_fixture(self):
        result = train_live.run_direct(args_for(fixture_name="direct-not-on-sale.json"))
        self.assertEqual(result["status"], "not_on_sale")

    def test_presale_guard_sends_no_request_and_calculates_check_date(self):
        today = datetime.now(train_live.BEIJING_TZ).date()
        travel = today + timedelta(days=30)
        args = args_for(travel_date=travel.isoformat(), presale_days=15, mcp_command="node unused")
        with mock.patch.object(train_live, "execute_tool") as execute:
            result = train_live.run_direct(args)
        execute.assert_not_called()
        self.assertEqual(result["status"], "not_on_sale")
        self.assertEqual(result["expected_check_date"], (travel - timedelta(days=14)).isoformat())
        self.assertEqual(result["attempts"], 0)

    def test_missing_mcp_keeps_complete_deep_link_fallback(self):
        travel = datetime.now(train_live.BEIJING_TZ).date() + timedelta(days=1)
        args = args_for(travel_date=travel.isoformat())
        with mock.patch.object(
                train_live.Rail12306Http, "bootstrap",
                side_effect=train_live.QueryError(
                    "network_error", "12306 unreachable", "unavailable", False)):
            result = train_live.run_direct(args)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["error"]["class"], "network_error")
        self.assertIn("火车·12306", result["links"]["text"])
        self.assertIn("火车·携程", result["links"]["text"])


class TransferAndRouteTests(unittest.TestCase):
    def test_transfer_computes_same_cross_station_buffer_and_last_arrival(self):
        result = train_live.normalize_transfer(payload("transfer-live.json"), args_for())
        self.assertEqual(result["status"], "live")
        primary = result["candidates"][result["recommendations"]["primary"]]
        self.assertTrue(primary["transfer"]["same_station"])
        self.assertEqual(primary["transfer"]["buffer_minutes"], 60)
        self.assertTrue(primary["transfer"]["buffer_ok"])
        self.assertEqual(primary["last_arrival"], {"date": "2026-08-15", "time": "13:00"})
        cross = next(c for c in result["candidates"] if c["transfer"]["same_station"] is False)
        self.assertEqual(cross["transfer"]["arrival_station"]["name"], "南京南")
        self.assertEqual(cross["transfer"]["departure_station"]["name"], "南京")
        self.assertEqual(cross["transfer"]["buffer_minutes"], 45)
        self.assertFalse(cross["transfer"]["buffer_ok"])

    def test_auto_transfer_only_runs_after_confirmed_direct_sold_out(self):
        today = datetime.now(train_live.BEIJING_TZ).date() + timedelta(days=1)
        args = args_for(travel_date=today.isoformat(), mcp_command="node unused")

        def live_only(tool, _args):
            self.assertEqual(tool, "get-tickets")
            return dated_payload("direct-live.json", today.isoformat()), {"version": "0.3.10"}, 1, None

        with mock.patch.object(train_live, "execute_tool", side_effect=live_only) as execute:
            result = train_live.run_direct(args)
        self.assertEqual(result["status"], "live")
        self.assertNotIn("transfer", result)
        self.assertEqual(execute.call_count, 1)

        calls = []

        def sold_then_transfer(tool, _args):
            calls.append(tool)
            name = "direct-sold-out.json" if tool == "get-tickets" else "transfer-live.json"
            return dated_payload(name, today.isoformat()), {"version": "0.3.10"}, 1, None

        with mock.patch.object(train_live, "execute_tool", side_effect=sold_then_transfer):
            result = train_live.run_direct(args)
        self.assertEqual(result["status"], "sold_out")
        self.assertEqual(result["transfer"]["status"], "live")
        self.assertEqual(calls, ["get-tickets", "get-interline-tickets"])
        self.assertEqual(train_live.merge_target(result)["kind"], "transfer")

    def test_route_is_on_demand_and_failure_is_isolated(self):
        route_args = train_live.build_parser().parse_args([
            "route", "--train", "G3", "--date", "2026-08-15",
        ])
        route = train_live.normalize_route(payload("route-live.json"), route_args)
        self.assertEqual(route["status"], "live")
        self.assertEqual(len(route["stops"]), 3)
        self.assertEqual(route["stops"][-1]["station_name"], "上海虹桥")

        today = datetime.now(train_live.BEIJING_TZ).date() + timedelta(days=1)
        args = args_for(travel_date=today.isoformat(), mcp_command="node unused", route_train="G3")

        def direct_ok_route_timeout(tool, _args):
            if tool == "get-tickets":
                return dated_payload("direct-live.json", today.isoformat()), {"version": "0.3.10"}, 1, None
            raise train_live.QueryError("timeout", "route timeout", "unavailable", True)

        with mock.patch.object(train_live, "execute_tool", side_effect=direct_ok_route_timeout):
            result = train_live.run_direct(args)
        self.assertEqual(result["status"], "live")
        self.assertEqual(result["route"]["status"], "unavailable")
        self.assertIsNotNone(train_live.merge_target(result))


class RetryTests(unittest.TestCase):
    def test_timeout_retries_once_and_rate_limit_does_not_retry(self):
        args = args_for(retries=1)
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise train_live.QueryError("timeout", "timeout", "unavailable", True)
            return "ok"

        value, attempts = train_live.http_get_with_retry(flaky, args)
        self.assertEqual(value, "ok")
        self.assertEqual(attempts, 2)
        self.assertEqual(len(calls), 2)

        limited_calls = []

        def limited():
            limited_calls.append(1)
            raise train_live.QueryError("rate_limited", "limited", "unavailable")

        args = args_for(retries=2)
        with self.assertRaises(train_live.QueryError):
            train_live.http_get_with_retry(limited, args)
        self.assertEqual(len(limited_calls), 1)


class PlanCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.old_plan = fixture("old-plan.json")

    def test_live_augments_manual_leg_without_changing_legacy_fields(self):
        before = copy.deepcopy(self.old_plan)
        updated, info = train_live.merge_plan(self.old_plan, live_result())
        self.assertTrue(info["written"])
        self.assertEqual(info["reason"], "augmented_manual_leg_without_field_changes")
        self.assertEqual(self.old_plan, before, "input plan must not be mutated")
        row = updated["legs"][0]
        for key, value in before["legs"][0].items():
            self.assertEqual(row[key], value, key)
        self.assertEqual(row["rail_live"]["status"], "live")
        self.assertEqual(updated["legs"][1], before["legs"][1])
        self.assertEqual(updated["unverified"], before["unverified"])

    def test_new_live_appends_old_compatible_leg_and_backup(self):
        plan = {"trip": "new", "lang": "zh", "legs": [], "days": []}
        updated, info = train_live.merge_plan(plan, live_result())
        self.assertEqual(info["reason"], "appended_live_leg")
        row = updated["legs"][0]
        for key in ("type", "date", "carrier", "from", "to", "dep", "arr", "price", "bags", "link", "note", "backup"):
            self.assertIn(key, row)
        self.assertEqual(row["carrier"], "G3")
        self.assertIn("G5", row["backup"])
        self.assertIn("12306.cn", row["link"])
        self.assertNotIn("ctrip", row["link"])
        self.assertIn(train_live.LIVE_NOTICE, row["note"])

    def test_live_replaces_dated_sample_metadata_but_preserves_manual_fields(self):
        plan = copy.deepcopy(self.old_plan)
        plan["legs"][0]["rail_live"] = {"status": "sample", "sample_date": "2026-07-01"}
        updated, info = train_live.merge_plan(plan, live_result())
        self.assertTrue(info["written"])
        self.assertEqual(updated["legs"][0]["rail_live"]["status"], "live")
        self.assertEqual(updated["legs"][0]["price"], "人工核对 ¥600")
        self.assertEqual(updated["legs"][0]["note"], "人工结果不得覆盖")

    def test_every_non_live_status_leaves_plan_unchanged(self):
        for status in ("sample", "unavailable", "unverified", "not_on_sale", "sold_out", "no_service"):
            with self.subTest(status=status):
                response = live_result()
                response["status"] = status
                updated, info = train_live.merge_plan(self.old_plan, response)
                self.assertFalse(info["written"])
                self.assertEqual(updated, self.old_plan)

    def test_failed_write_preserves_file_bytes(self):
        sample = train_live.run_direct(args_for(fixture_name="direct-live.json"))
        with tempfile.TemporaryDirectory() as temporary:
            plan_path = Path(temporary) / "plan.geo.json"
            response_path = Path(temporary) / "response.json"
            original = json.dumps(self.old_plan, ensure_ascii=False, indent=4).encode("utf-8")
            plan_path.write_bytes(original)
            response_path.write_text(json.dumps(sample, ensure_ascii=False), encoding="utf-8")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = train_live.main([
                    "merge", "--plan", str(plan_path), "--response", str(response_path), "--write",
                ])
            self.assertEqual(code, 0)
            self.assertEqual(plan_path.read_bytes(), original)
            self.assertIn("plan_unchanged", stdout.getvalue())

    def test_managed_row_updates_only_after_another_live_query(self):
        plan = {"legs": [], "days": []}
        first, _ = train_live.merge_plan(plan, live_result())
        second_response = live_result()
        second_response["queried_at"] = "2026-08-01T09:00:00+08:00"
        second, info = train_live.merge_plan(first, second_response)
        self.assertEqual(info["reason"], "updated_managed_leg")
        self.assertEqual(len(second["legs"]), 1)
        self.assertEqual(second["legs"][0]["rail_live"]["queried_at"], "2026-08-01T09:00:00+08:00")

    def test_old_renderer_accepts_augmented_plan(self):
        updated, _ = train_live.merge_plan({"trip": "render", "lang": "zh", "legs": [], "days": []}, live_result())
        with tempfile.TemporaryDirectory() as temporary:
            plan_path = Path(temporary) / "plan.geo.json"
            output_path = Path(temporary) / "trip.html"
            plan_path.write_text(json.dumps(updated, ensure_ascii=False), encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "render_plan.py"),
                 str(plan_path), "--out", str(output_path)],
                check=False, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            html = output_path.read_text(encoding="utf-8")
            self.assertIn("G3", html)
            self.assertIn(train_live.LIVE_NOTICE, html)
            self.assertNotIn("WARN", completed.stderr)


class InstallerTests(unittest.TestCase):
    def test_transport_metadata_matches_direct_https_lineage(self):
        self.assertEqual(train_live.UPSTREAM_REPOSITORY, "php737/train-12306")
        self.assertEqual(train_live.UPSTREAM_VERSION, "1.2.0")
        self.assertIn("MIT", (ROOT / "THIRD-PARTY-NOTICES.md").read_text(encoding="utf-8"))

    def test_direct_live_end_to_end_offline(self):
        """Fixture rows flow through the same normalizer as live HTTP rows."""
        rows = json.loads(payload("direct-live.json")["content"][0]["text"])
        args = args_for()
        result = train_live.normalize_direct(
            {"content": [{"type": "text", "text": json.dumps(rows, ensure_ascii=False)}]},
            args)
        self.assertEqual(result["status"], "live")
        self.assertEqual(len(result["candidates"]), 3)


if __name__ == "__main__":
    unittest.main()
