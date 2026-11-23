#!/usr/bin/env python3
"""Offline tests for scripts/flyai_provider.py; never starts the flyai CLI,
never accesses Fliggy, never reads a real key."""

import contextlib
import io
from datetime import datetime, timedelta
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import flyai_provider as fp  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "flyai"
OLD_PLAN = json.loads((FIXTURES / "old-plan.json").read_text(encoding="utf-8"))
QUERIED_AT = "2026-11-23T12:00:00+08:00"


def payload(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))["result"]


def flights_args(**overrides):
    argv = ["flights", "--from", "PVG", "--to", "KMG", "--date", "2026-12-01"]
    fixture_name = overrides.pop("fixture_name", None)
    if fixture_name:
        argv += ["--fixture", str(FIXTURES / fixture_name)]
    for key, value in overrides.items():
        option = "--" + key.replace("_", "-")
        if isinstance(value, bool):
            if value:
                argv.append(option)
        elif value is not None:
            argv += [option, str(value)]
    return fp.build_parser().parse_args(argv)


def hotels_args(**overrides):
    argv = ["hotels", "--city", "乌镇", "--checkin", "2026-12-01",
            "--checkout", "2026-12-03", "--guests", "2"]
    fixture_name = overrides.pop("fixture_name", None)
    if fixture_name:
        argv += ["--fixture", str(FIXTURES / fixture_name)]
    for key, value in overrides.items():
        option = "--" + key.replace("_", "-")
        if isinstance(value, bool):
            if value:
                argv.append(option)
        elif value is not None:
            argv += [option, str(value)]
    return fp.build_parser().parse_args(argv)


def live_flights(fixture_name="flights-live.json", **overrides):
    result = fp.normalize_flights(payload(fixture_name), flights_args(**overrides))
    result["queried_at"] = QUERIED_AT
    return result


def live_hotels(fixture_name="hotels-live.json", **overrides):
    result = fp.normalize_hotels(payload(fixture_name), hotels_args(**overrides))
    result["queried_at"] = QUERIED_AT
    return result


def result_query_key(result):
    return result["query_key"]


class FlightStatusTests(unittest.TestCase):
    def test_live_direct_small_candidate_set_and_one_backup(self):
        result = live_flights()
        self.assertEqual(result["status"], "live")
        self.assertLessEqual(len(result["offers"]), 3)
        self.assertEqual(result["recommendations"]["primary"], 0)
        self.assertEqual(result["recommendations"]["backup"], 1)
        offer = result["offers"][0]
        self.assertEqual(offer["flight_no"], "MU5117")
        self.assertEqual(offer["from"]["airport"], "PVG")
        self.assertEqual(offer["from"]["terminal"], "T1")
        self.assertEqual(offer["dep"]["time"], "08:00")
        self.assertEqual(offer["duration_minutes"], 205)
        self.assertEqual(offer["price"], {"amount_cny": 850.0, "status": "ok"})
        self.assertEqual(offer["detail_url"][:8], "https://")
        # baggage / refund_change stay unknown — never invented
        for segment in offer["segments"]:
            self.assertIsNone(segment["baggage"])
            self.assertIsNone(segment["refund_change"])

    def test_sample_is_dated_and_cannot_impersonate_live(self):
        args = flights_args(fixture_name="flights-live.json")
        result = fp.run_flights(args)
        self.assertEqual(result["status"], "sample")
        self.assertEqual(result["sample_date"], "2026-11-01")
        self.assertIsNone(result["queried_at"])
        self.assertIsNone(result["recommendations"]["primary"])
        self.assertIn("禁止写入计划", result["notice"])

    def test_masked_price_is_flagged_not_guessed(self):
        result = live_flights("flights-masked.json")
        statuses = [o["price"]["status"] for o in result["offers"]]
        self.assertIn("masked", statuses)
        for offer in result["offers"]:
            if offer["price"]["status"] == "masked":
                self.assertIsNone(offer["price"]["amount_cny"])
        # the one known price survives normalisation at its real value
        known = [o for o in result["offers"] if o["price"]["status"] == "ok"]
        self.assertEqual(known[0]["price"]["amount_cny"], 560.0)
        self.assertEqual(known[0]["flight_no"], "9C8851")

    def test_demo_mode_is_not_live_and_offers_are_dropped(self):
        result = live_flights("flights-demo.json")
        self.assertEqual(result["status"], "demo")
        self.assertEqual(result["offers"], [])
        self.assertIsNone(result["recommendations"]["primary"])
        self.assertIn("体验模式", result["notice"])

    def test_missing_field_rows_are_isolated(self):
        result = live_flights("flights-missing-fields.json")
        self.assertEqual(result["malformed_rows"], 2)
        self.assertEqual(len(result["offers"]), 1)
        self.assertEqual(result["offers"][0]["flight_no"], "MU5117")

    def test_valid_empty_list_is_no_service(self):
        result = live_flights("flights-empty.json")
        self.assertEqual(result["status"], "no_service")
        self.assertEqual(result["reason"], "valid_empty_result")

    def test_transfer_offer_is_two_segments_with_summed_price(self):
        result = live_flights("flights-transfer.json")
        transfer = next(o for o in result["offers"] if o["stops"] == 1)
        self.assertEqual(len(transfer["segments"]), 2)
        self.assertEqual(transfer["price"], {"amount_cny": 820.0, "status": "ok"})
        self.assertIn("MU5415 + 3U8633", transfer["flight_no"])

    def test_transfer_with_lower_price_still_ranks_below_direct(self):
        # 低价不等于推荐: 直飞优先,再历时,最后价格
        result = live_flights("flights-transfer.json")
        self.assertEqual(result["offers"][0]["stops"], 0)
        self.assertEqual(result["offers"][0]["price"]["amount_cny"], 990.0)

    def test_next_day_arrival_is_flagged(self):
        result = live_flights("flights-next-day.json")
        offer = result["offers"][0]
        self.assertTrue(offer["next_day_arrival"])
        self.assertEqual(offer["arr"]["date"], "2026-12-02")

    def test_noise_around_json_is_boundary_parsed(self):
        args = flights_args(fixture_name="flights-noise.json")
        result = fp.run_flights(args)
        self.assertEqual(result["status"], "sample")
        self.assertEqual(len(result["offers"]), 1)

    def test_malformed_stdout_is_unverified(self):
        args = flights_args(fixture_name="flights-malformed.txt")
        result = fp.run_flights(args)
        self.assertEqual(result["status"], "unverified")
        self.assertEqual(result["error"]["class"], "malformed_output")

    def test_nonzero_exit_is_unavailable(self):
        args = flights_args(fixture_name="flights-nonzero.json")
        result = fp.run_flights(args)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["error"]["class"], "cli_nonzero")
        self.assertIn("ctrip_check", result["links"])

    def test_departure_window_and_direct_only_filters(self):
        result = live_flights(earliest=9, latest=12)
        times = [o["dep"]["time"] for o in result["offers"]]
        self.assertTrue(all("09" <= t[:2] <= "12" for t in times))
        transfer = live_flights("flights-transfer.json", direct_only=True)
        self.assertTrue(all(o["stops"] == 0 for o in transfer["offers"]))

    def test_cabin_filter(self):
        result = live_flights(cabin="business")
        self.assertEqual(len(result["offers"]), 1)
        self.assertEqual(result["offers"][0]["segments"][0]["cabin"], "business")


class HotelStatusTests(unittest.TestCase):
    def test_price_bases_are_preserved_and_not_summed_when_unclear(self):
        result = live_hotels()
        by_name = {(o["hotel_name"], o["room_type"]): o for o in result["offers"]}
        self.assertEqual(
            by_name[("乌镇西栅精品客栈", "标准大床房")]["price"]["basis"],
            "per-night-per-room")
        self.assertEqual(
            by_name[("乌镇通安客栈", "高级双床房")]["price"]["basis"],
            "per-night-per-person")
        total = by_name[("乌镇枕水度假酒店", "临水景观大床房")]
        self.assertEqual(total["price"]["basis"], "total-per-room")
        unknown = by_name[("乌镇子夜大酒店", "豪华套房")]
        self.assertEqual(unknown["price"]["basis"], "unknown")

    def test_masked_hotel_price_is_flagged(self):
        result = live_hotels("hotels-masked.json")
        for offer in result["offers"]:
            self.assertEqual(offer["price"]["status"], "masked")
            self.assertIsNone(offer["price"]["amount_cny"])

    def test_room_not_meeting_occupancy_never_fills_a_tier(self):
        result = live_hotels()
        single = next(o for o in result["offers"] if o["room_type"] == "经济单人间")
        self.assertFalse(single["hard_requirements_ok"])
        self.assertIsNone(single["tier"])
        tiered = [o for o in result["offers"] if o["tier"]]
        self.assertTrue(all(o["hard_requirements_ok"] for o in tiered))

    def test_tiers_are_two_or_three_and_price_sorted(self):
        result = live_hotels()
        tiered = sorted((o for o in result["offers"] if o["tier"]),
                        key=lambda o: o["tier"])
        self.assertIn(max(o["tier"] for o in tiered), (2, 3))
        self.assertLessEqual(min(o["tier"] for o in tiered), 1)
        amounts = [o["price"]["amount_cny"] for o in tiered]
        self.assertEqual(amounts, sorted(amounts))
        # unknown-basis and broken rows are excluded from tiers
        unranked = [o for o in result["offers"] if not o["tier"]]
        self.assertTrue(all(
            o["price"]["basis"] not in ("per-night-per-room", "per-night-per-person")
            or not o["hard_requirements_ok"] for o in unranked))

    def test_unknown_basis_is_never_summed(self):
        result = live_hotels()
        offer = next(o for o in result["offers"] if o["price"]["basis"] == "unknown")
        row = fp.hotel_option_fields(offer, result, nights=2)
        self.assertIn("口径未知", row["band"])
        self.assertNotIn("晚合计", row["band"])

    def test_per_night_per_room_band_shows_same_basis_total(self):
        result = live_hotels()
        offer = next(o for o in result["offers"]
                     if o["price"]["basis"] == "per-night-per-room"
                     and o["hard_requirements_ok"])
        row = fp.hotel_option_fields(offer, result, nights=2)
        self.assertIn("¥320/晚·每间", row["band"])
        self.assertIn("2 晚合计 ¥640", row["band"])

    def test_main_pic_is_dropped(self):
        result = live_hotels()
        for offer in result["offers"]:
            self.assertNotIn("main_pic", offer)
            self.assertNotIn("mainPic", offer)
            self.assertNotIn("image", offer)

    def test_demo_hotels_are_not_live(self):
        result = live_hotels("hotels-demo.json")
        self.assertEqual(result["status"], "demo")
        self.assertEqual(result["offers"], [])

    def test_enrichment_fields_are_recorded(self):
        result = live_hotels()
        offer = next(o for o in result["offers"]
                     if o["hotel_name"] == "乌镇西栅精品客栈")
        self.assertEqual(offer["occupancy"]["max_guests"], 2)
        self.assertEqual(offer["taxes_fees"], "included")
        self.assertEqual(offer["breakfast"], "含双早")
        self.assertEqual(offer["cancellation"], "入住前1天18:00前免费取消")
        self.assertEqual(offer["rating"], 4.7)
        self.assertEqual(offer["location"]["lat"], 30.7461)
        row = fp.hotel_option_fields(offer, result, nights=2)
        self.assertEqual(row["flyai"]["queried_at"], QUERIED_AT)


class FallbackAndSafetyTests(unittest.TestCase):
    def test_no_key_degrades_without_spawning_a_process(self):
        with mock.patch.object(fp.subprocess, "run") as run:
            args = flights_args()
            result = fp.run_flights(args)
        run.assert_not_called()
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["error"]["class"], "key_missing")
        for url in ("ctrip_check", "booking_notice"):
            self.assertIn(url, result["links"])
        self.assertIn("携程", result["links"]["text"])
        self.assertIn("去哪儿", result["links"]["text"])

    def test_missing_cli_is_unavailable_with_links(self):
        with mock.patch.dict("os.environ", {"FLYAI_API_KEY": "k"}):
            with mock.patch.object(fp.shutil, "which", return_value=None):
                args = flights_args()
                result = fp.run_flights(args)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["error"]["class"], "cli_not_found")
        self.assertIn("ctrip_check", result["links"])

    def test_key_travels_via_env_only_and_argv_is_an_array(self):
        completed = subprocess.CompletedProcess(
            [], 0, stdout=json.dumps({"demoMode": False, "offers": []}),
            stderr="")
        with mock.patch.dict("os.environ", {"FLYAI_API_KEY": "secret-key-123"}):
            with mock.patch.object(fp.shutil, "which",
                                   return_value="/usr/local/bin/flyai"):
                with mock.patch.object(fp.subprocess, "run",
                                       return_value=completed) as run:
                    args = flights_args()
                    fp.run_flights(args)
        argv = run.call_args[0][0]
        self.assertIsInstance(argv, list)
        self.assertEqual(argv[0], "/usr/local/bin/flyai")
        for element in argv:
            self.assertNotIn("secret-key-123", str(element))
        self.assertEqual(run.call_args[1]["shell"], False)

    def test_timeout_retries_once_and_rate_limit_does_not(self):
        with mock.patch.dict("os.environ", {"FLYAI_API_KEY": "k"}):
            with mock.patch.object(fp.shutil, "which", return_value="flyai"):
                with mock.patch.object(
                        fp.subprocess, "run",
                        side_effect=subprocess.TimeoutExpired("flyai", 1)) as run:
                    args = flights_args(retries=1, timeout=1)
                    result = fp.run_flights(args)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["error"]["class"], "timeout")
        self.assertEqual(result["attempts"], 2)

        rate_limited = subprocess.CompletedProcess(
            [], 429, stdout="", stderr="HTTP 429 Too Many Requests")
        with mock.patch.dict("os.environ", {"FLYAI_API_KEY": "k"}):
            with mock.patch.object(fp.shutil, "which", return_value="flyai"):
                with mock.patch.object(fp.subprocess, "run",
                                       return_value=rate_limited) as run2:
                    args = flights_args(retries=1, timeout=1)
                    result = fp.run_flights(args)
        self.assertEqual(run2.call_count, 1)
        self.assertEqual(result["error"]["class"], "rate_limited")

    def test_error_never_leaks_stderr_or_key(self):
        broken = subprocess.CompletedProcess(
            [], 1, stdout="", stderr="boom secret-key-123")
        with mock.patch.dict("os.environ", {"FLYAI_API_KEY": "secret-key-123"}):
            with mock.patch.object(fp.shutil, "which", return_value="flyai"):
                with mock.patch.object(fp.subprocess, "run", return_value=broken):
                    args = flights_args()
                    result = fp.run_flights(args)
        dumped = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("secret-key-123", dumped)
        self.assertNotIn("boom", dumped)
        self.assertNotIn("stderr", dumped)


class PlanCompatibilityTests(unittest.TestCase):
    def test_live_augments_manual_leg_without_changing_legacy_fields(self):
        plan = json.loads(json.dumps(OLD_PLAN))
        updated, info = fp.merge_flights_plan(plan, live_flights())
        self.assertTrue(info["written"])
        flight_leg = next(r for r in updated["legs"] if r.get("type") == "flight")
        self.assertEqual(flight_leg["price"], "—, 点链接核对")  # manual price kept
        self.assertEqual(flight_leg["note"], "人工核验行,价格不得被覆盖")
        self.assertEqual(flight_leg["backup"], "HO1121 09:30 出发")
        self.assertNotIn("_flyai_managed", flight_leg)  # stays a manual row
        live = flight_leg["flight_live"]
        self.assertEqual(live["status"], "live")
        self.assertEqual(live["query_key"], result_query_key(live_flights()))
        self.assertIn("MU5117", live["offers"][0]["flight_no"])

    def test_new_live_appends_managed_leg_with_unknown_fields_marked(self):
        plan = json.loads(json.dumps(OLD_PLAN))
        plan["legs"] = [r for r in plan["legs"] if r.get("type") != "flight"]
        updated, info = fp.merge_flights_plan(plan, live_flights())
        self.assertTrue(info["written"])
        self.assertEqual(info["reason"], "leg_appended")
        leg = next(r for r in updated["legs"] if r.get("_flyai_managed") is True)
        self.assertIn("查询时参考价", leg["price"])
        self.assertIn("单源", leg["price"])
        self.assertIn("未实测", leg["bags"])
        self.assertIn("未实测", leg["refund_change"])
        self.assertIn(QUERIED_AT, leg["note"])
        self.assertIn("fliggy", leg["link"])
        self.assertIn(leg["from"], ("PVG", "PVG T1"))
        self.assertIn("backup", leg)

    def test_every_non_live_status_leaves_plan_unchanged(self):
        sample = fp.mark_sample(live_flights(), "2026-11-01")
        cases = (
            sample,
            live_flights("flights-demo.json"),
            live_flights("flights-empty.json"),
            fp.error_result(fp.base_result("flights", flights_args()),
                            fp.QueryError("timeout", "t", "unavailable")),
        )
        for response in cases:
            plan = json.loads(json.dumps(OLD_PLAN))
            updated, info = fp.merge_flights_plan(plan, response)
            self.assertEqual(updated, OLD_PLAN, response.get("status"))
            self.assertFalse(info["written"])

    def test_failed_write_preserves_file_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plan.geo.json"
            path.write_text(json.dumps(OLD_PLAN, ensure_ascii=False),
                            encoding="utf-8")
            before = path.read_bytes()
            argv = ["flights", "--from", "PVG", "--to", "KMG",
                    "--date", "2026-12-01",
                    "--fixture", str(FIXTURES / "flights-live.json"),
                    "--plan", str(path), "--write"]
            with mock.patch.object(fp, "merge_flights_plan",
                                   side_effect=ValueError("boom")):
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = fp.main(argv)
            self.assertEqual(rc, 2)
            self.assertEqual(path.read_bytes(), before)

    def test_sample_result_never_writes_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plan.geo.json"
            path.write_text(json.dumps(OLD_PLAN, ensure_ascii=False),
                            encoding="utf-8")
            before = path.read_bytes()
            argv = ["flights", "--from", "PVG", "--to", "KMG",
                    "--date", "2026-12-01",
                    "--fixture", str(FIXTURES / "flights-live.json"),
                    "--plan", str(path), "--write"]
            with contextlib.redirect_stdout(io.StringIO()):
                rc = fp.main(argv)
            self.assertEqual(rc, 0)
            self.assertEqual(path.read_bytes(), before)

    def test_budget_rows_are_never_duplicated_or_mutated(self):
        plan = json.loads(json.dumps(OLD_PLAN))
        for merge in (fp.merge_flights_plan(plan, live_flights()),
                      fp.merge_hotels_plan(plan, live_hotels(), "乌镇")):
            self.assertEqual(merge[0]["budget"], OLD_PLAN["budget"])
        updated = fp.merge_hotels_plan(plan, live_hotels(), "乌镇")[0]
        cats = [b["cat"] for b in updated["budget"]]
        self.assertEqual(len(cats), len(set(cats)))

    def test_live_hotels_extend_options_additively(self):
        plan = json.loads(json.dumps(OLD_PLAN))
        updated, info = fp.merge_hotels_plan(plan, live_hotels(), "乌镇")
        self.assertTrue(info["written"])
        self.assertEqual(info["reason"], "extended_options")
        row = next(h for h in updated["hotels"] if "乌镇" in h.get("base", ""))
        manual = row["options"][0]
        self.assertEqual(manual["name"], "Property 1")  # manual option untouched
        self.assertEqual(manual["band"], "PLACEHOLDER/night")
        added = row["options"][1:]
        self.assertGreaterEqual(len(added), 2)
        for option in added:
            self.assertIn("flyai", option)  # additive object
            self.assertTrue((option.get("link") or "").startswith("https://"))
            flyai = option["flyai"]
            self.assertEqual(flyai["status"], "live")
            self.assertEqual(flyai["source"], "flyai (飞猪)")
            self.assertIn("¥", option["band"])

    def test_hotel_merge_skips_duplicate_names(self):
        plan = json.loads(json.dumps(OLD_PLAN))
        response = live_hotels()
        first, _info = fp.merge_hotels_plan(plan, response, "乌镇")
        row = next(h for h in first["hotels"] if "乌镇" in h.get("base", ""))
        names = [o["name"] for o in row["options"]]
        second, info2 = fp.merge_hotels_plan(first, response, "乌镇")
        row2 = next(h for h in second["hotels"] if "乌镇" in h.get("base", ""))
        self.assertEqual([o["name"] for o in row2["options"]], names)
        self.assertFalse(info2["written"])
        self.assertGreaterEqual(info2["options_skipped_duplicate"], 1)

    def test_hotel_merge_without_base_match_appends_managed_row(self):
        plan = json.loads(json.dumps(OLD_PLAN))
        plan["hotels"] = [{"base": "杭州 1 nights", "area": "x", "why": "y",
                           "options": []}]
        updated, info = fp.merge_hotels_plan(plan, live_hotels(), "乌镇")
        self.assertTrue(info["written"])
        self.assertEqual(info["reason"], "hotel_appended")
        row = updated["hotels"][1]
        self.assertTrue(row["_flyai_managed"])
        self.assertIn("乌镇", row["base"])
        self.assertTrue(row["options"])

    def test_old_renderer_accepts_augmented_plan(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import render_plan
        updated = fp.merge_flights_plan(
            fp.merge_hotels_plan(json.loads(json.dumps(OLD_PLAN)),
                                 live_hotels(), "乌镇")[0],
            live_flights())[0]
        html = render_plan.render(updated)
        self.assertIn("flyai", html)
        self.assertIn("hotels", html)

    def test_theme_common_hotel_option_extra_is_empty_for_legacy(self):
        sys.path.insert(0, str(ROOT / "themes"))
        import theme_common
        self.assertEqual(theme_common.hotel_option_extra(
            {"name": "Property 1", "band": "PLACEHOLDER/night"}), "")
        result = live_hotels()
        option = fp.hotel_option_fields(
            next(o for o in result["offers"]
                 if o["price"]["basis"] == "per-night-per-room"
                 and o["hard_requirements_ok"]),
            result, nights=2)
        extra = theme_common.hotel_option_extra(option)
        self.assertIn("¥320", extra)
        self.assertIn("每间·每晚", extra)
        self.assertIn("含双早", extra)
        self.assertIn("参考价", extra)
        self.assertNotIn("<script", extra)


class DoctorTests(unittest.TestCase):
    def test_no_cli_degrades_with_notes_and_no_network(self):
        with mock.patch.object(fp.shutil, "which", return_value=None):
            args = fp.build_parser().parse_args(["doctor"])
            report = fp.run_doctor(args)
        self.assertEqual(report["cli"]["found"], False)
        self.assertEqual(report["exit_code"], 2)
        self.assertIn("深链", json.dumps(report, ensure_ascii=False))
        self.assertFalse(report["env"]["FLYAI_API_KEY"])

    def test_doctor_reports_key_presence_without_the_value(self):
        with mock.patch.dict("os.environ", {"FLYAI_API_KEY": "secret-key-123"}):
            with mock.patch.object(fp.shutil, "which", return_value=None):
                args = fp.build_parser().parse_args(["doctor"])
                report = fp.run_doctor(args)
        dumped = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("secret-key-123", dumped)
        self.assertTrue(report["env"]["FLYAI_API_KEY"])

    def test_signature_mismatch_is_reported_not_silent(self):
        completed = subprocess.CompletedProcess(
            [], 0, stdout="flyai 1.2.3\nusage: flyai [--config C]\n", stderr="")
        with mock.patch.dict("os.environ", {"FLYAI_API_KEY": "k"}):
            with mock.patch.object(fp.shutil, "which", return_value="flyai"):
                with mock.patch.object(fp.subprocess, "run",
                                       return_value=completed):
                    args = fp.build_parser().parse_args(["doctor"])
                    report = fp.run_doctor(args)
        self.assertEqual(report["cli"]["version"], "flyai 1.2.3")
        self.assertFalse(report["cli"]["signature_check"]["flights"])
        self.assertFalse(report["cli"]["signature_check"]["hotels"])
        self.assertFalse(report["capabilities"]["flights"] == "live")
        self.assertIn("签名", json.dumps(report, ensure_ascii=False))


class RendererDiffsTests(unittest.TestCase):
    """The seven themes render the augmented hotel options through the shared
    theme_common.hotel_option_extra helper; legacy plans stay unchanged."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT / "themes"))
        cls.plan = fp.merge_hotels_plan(
            json.loads(json.dumps(OLD_PLAN)), live_hotels(), "乌镇")[0]
        cls.row = next(h for h in cls.plan["hotels"]
                       if "乌镇" in h.get("base", ""))

    def test_hotels_section_helpers_render_the_enrichment(self):
        for theme in ("render_journal", "render_splash", "render_theme2",
                      "render_zine"):
            module = __import__(theme)
            html = module.render_hotels(self.plan["hotels"])
            self.assertIn("含双早", html, theme)
            self.assertIn("参考价", html, theme)

    def test_appendix_helpers_render_the_enrichment(self):
        clay = __import__("render_clay2")
        glass = __import__("render_glass2")
        titles = {"emergency": "应急", "safety": "安全"}
        self.assertIn("含双早", clay.appendix(self.plan, "", titles))
        self.assertIn("含双早", glass.appendix(self.plan, "", None, titles))

    def test_legacy_plan_hotels_unchanged_in_every_theme(self):
        legacy_html = {}
        for theme in ("render_journal", "render_splash", "render_theme2",
                      "render_zine"):
            module = __import__(theme)
            legacy_html[theme] = module.render_hotels(OLD_PLAN["hotels"])
            self.assertNotIn("含双早", legacy_html[theme])

    def test_noir2_wires_the_shared_helper(self):
        source = (ROOT / "themes" / "render_noir2.py").read_text(encoding="utf-8")
        self.assertIn("hotel_option_extra(o)", source)


if __name__ == "__main__":
    unittest.main()
