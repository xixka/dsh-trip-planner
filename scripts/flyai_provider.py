#!/usr/bin/env python3
"""Optional 飞猪 flyai CLI read-only flight and hotel queries.

No third-party Python packages, no login, no browser, no booking, no passenger
information. The flyai CLI is invoked read-only with a safe argument array
(never a shell string); FLYAI_API_KEY is passed through the environment only and
never appears in argv, logs, fixtures or rendered output. Without the CLI, the
key, on 体验模式 (demo mode), or on any query failure, the result is
``unavailable``/``demo`` plus the existing flight_scan deep links — the plan,
its estimates and its manual verification stay untouched.

IMPORTANT — 未实测: the flyai CLI was NOT available while this adapter was
written (no `flyai --help` could be run in the sandbox). The subcommand/flag
template below (FLIGHTS/CLI_TEMPLATE, HOTELS/CLI_TEMPLATE) is an ASSUMED
contract documented in references/flyai.md. Run `python3 scripts/flyai_provider.py
doctor` (it runs `flyai --help`/`--version` and checks the signature) before the
first real query; if the real CLI differs, adjust CLI_TEMPLATE or keep using the
flight_scan deep links. All offline tests use fixtures and never invoke the CLI.

Examples:
  python3 scripts/flyai_provider.py doctor
  python3 scripts/flyai_provider.py flights --from PVG --to KMG --date 2026-12-01
  python3 scripts/flyai_provider.py flights --from PVG --to KMG --date 2026-12-01 \
      --adults 2 --cabin economy --earliest 7 --latest 12 --plan plan.geo.json --write
  python3 scripts/flyai_provider.py hotels --city 杭州 --checkin 2026-12-01 \
      --checkout 2026-12-03 --guests 2 --base 乌镇 --plan plan.geo.json --write
  python3 scripts/flyai_provider.py flights --from PVG --to KMG --date 2026-12-01 \
      --fixture tests/fixtures/flyai/flights-live.json

Only a schema-valid ``live`` result can be merged into plan legs[] / hotels[].
Fixtures are always ``sample`` and can never write a plan. See
references/flyai.md.
"""

import argparse
import copy
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from flight_scan import flight_links

SCHEMA_VERSION = 1
CLI_NAME = "flyai"
CLI_ENV_OVERRIDE = "FLYAI_CLI"
CLI_KEY_ENV = "FLYAI_API_KEY"
BEIJING_TZ = timezone(timedelta(hours=8), name="CST")
TIMEZONE_NAME = "Asia/Shanghai"
LIVE_NOTICE = "查询时参考价（单源）；下单前点开 detailUrl/深链以航司或飞猪页面为准"
SAMPLE_NOTICE = "注明日期的离线样本，非实时数据，禁止写入计划"
DEMO_NOTICE = "flyai 体验模式：非真实报价，完整回退深链与价格区间"
STATUSES = {
    "live", "sample", "demo", "no_service", "unavailable", "unverified",
}
STATUS_PRIORITY = {"unverified": 0, "unavailable": 0, "deep_link": 1,
                   "no_service": 1, "demo": 2, "sample": 2, "live": 3}
CABINS = ("economy", "business", "first")
PRICE_BASES = (
    "per-night-per-room", "per-night-per-person",
    "total-per-room", "total-per-person", "unknown",
)
MASKED_MARKERS = ("*", "＊", "masked", "遮蔽", "****", "-----")

# ASSUMED flyai CLI contract (unverified — see module docstring and doctor).
FLIGHTS_TEMPLATE = ["flights", "--format", "json"]
HOTELS_TEMPLATE = ["hotels", "--format", "json"]
VERSION_TEMPLATE = ["--version"]
DOCTOR_TIMEOUT = 10.0
FLIGHTS_REQUIRED_KEYS = ("flightNo", "depDate", "depTime", "arrDate",
                         "arrTime", "depAirport", "arrAirport")


def now_beijing():
    return datetime.now(BEIJING_TZ).isoformat(timespec="seconds")


def parse_iso_date(value):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise QueryError("invalid_date", "date must be YYYY-MM-DD", "unverified")


def text_input(value, label, max_len=40):
    cleaned = (value or "").strip()
    if not cleaned or len(cleaned) > max_len or any(ord(c) < 32 for c in cleaned):
        raise QueryError("invalid_{}".format(label), "{} is empty or invalid".format(label),
                         "unverified")
    return cleaned


def argparse_date(value):
    try:
        parse_iso_date(value)
        return value
    except QueryError as error:
        raise argparse.ArgumentTypeError(error.message)


def argparse_text(label, max_len=40):
    def wrapper(value):
        try:
            return text_input(value, label, max_len)
        except QueryError as error:
            raise argparse.ArgumentTypeError(error.message)
    return wrapper


def _load_dotenv():
    """Load KEY=VALUE lines from a git-ignored .env (skill root, then CWD) into
    os.environ WITHOUT overwriting anything already set — mirrors
    route_tools._load_dotenv so FLYAI_API_KEY can live next to the skill and
    never inside the repo, the argv or a log line."""
    here = Path(__file__).resolve().parent.parent
    for path in (here / ".env", Path.cwd() / ".env"):
        try:
            if not path.is_file():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())
        except OSError:
            continue


def api_key_present():
    return bool(os.environ.get(CLI_KEY_ENV, "").strip())


def fallback_links_flights(orig, dest, depart, adults=1):
    """Reuse the existing deep-link generator verbatim as the complete fallback."""
    text = flight_links(orig, dest, depart, None, adults)
    urls = re.findall(r"https?://[^\s]+", text)
    ctrip = next((u for u in urls if "ctrip.com" in u), None)
    return {
        "text": text,
        "ctrip_check": ctrip,
        "booking_notice": "无真实报价时价格以点开深链核对为准，绝不猜价",
    }


def fallback_links_hotels(city, checkin, checkout):
    return {
        "text": "酒店·携程  https://hotels.ctrip.com/hotels/list?city={}&checkin={}"
                "&checkout={}".format(city, checkin, checkout),
        "ctrip_check": "https://hotels.ctrip.com/hotels/list?city={}&checkin={}"
                       "&checkout={}".format(city, checkin, checkout),
        "booking_notice": "无真实报价时价格与房型以点开深链核对为准",
    }


def base_result(kind, args, source_mode="live"):
    result = {
        "schema_version": SCHEMA_VERSION,
        "kind": kind,
        "status": "unverified",
        "source": {
            "name": "flyai (飞猪)",
            "transport": "cli" if source_mode == "live" else "offline_fixture",
            "cli_path": None,
            "cli_version": None,
            "api_key_present": api_key_present() if source_mode == "live" else None,
            "adapter_verified_cli": False,
        },
        "queried_at": now_beijing() if source_mode == "live" else None,
        "timezone": TIMEZONE_NAME,
        "offers": [],
        "recommendations": {"primary": None, "backup": None},
        "notice": LIVE_NOTICE if source_mode == "live" else SAMPLE_NOTICE,
        "attempts": 0,
    }
    return result


def error_result(result, error):
    result["status"] = error.status
    result["error"] = {"class": error.error_class, "message": error.message}
    result["recommendations"] = {"primary": None, "backup": None}
    result["notice"] = {
        "demo": DEMO_NOTICE,
        "no_service": "查询成功但无候选；保留人工结果并点开深链核对",
    }.get(error.status, "实时结果不可用或未核验；保留人工结果并点开深链核对")
    return result


def classify_text(text):
    lower = text.lower()
    if any(x in lower for x in ("429", "rate limit", "too many requests", "限流", "频繁")):
        return QueryError("rate_limited", "flyai CLI reported a rate limit", "unavailable")
    if any(x in lower for x in ("timeout", "timed out", "超时")):
        return QueryError("timeout", "flyai CLI timed out", "unavailable", True)
    return None


class QueryError(Exception):
    """Sanitized, classified query failure; raw CLI stderr is never exposed."""

    def __init__(self, error_class, message, status="unavailable", retryable=False):
        super().__init__(message)
        self.error_class = error_class
        self.message = message
        self.status = status
        self.retryable = retryable


def extract_json(stdout):
    """Boundary-tolerant JSON extraction: the CLI may print noise lines before or
    after the payload. Tries the whole text, then line by line, then the first
    balanced {...} slice. Never guesses values, only locates the payload."""
    text = (stdout or "").lstrip("﻿").strip()
    if not text:
        raise QueryError("empty_output", "flyai CLI produced no output", "unavailable")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith(("{", "[")):
            continue
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            continue
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    raise QueryError("malformed_output", "flyai CLI stdout is not valid JSON",
                     "unverified")


def run_cli(base_argv, args):
    """Run the flyai CLI once with a safe argv array. Returns parsed stdout JSON.
    The API key is only ever inherited through the environment; stderr is read
    for classification but never returned or printed."""
    executable = shutil.which(args.cli)
    if executable is None:
        raise QueryError("cli_not_found",
                         "flyai CLI was not found on PATH (set --cli or {})".format(CLI_ENV_OVERRIDE),
                         "unavailable")
    argv = [executable] + list(base_argv)
    try:
        completed = subprocess.run(
            argv, shell=False, cwd=None,
            timeout=args.timeout, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        raise QueryError("timeout", "flyai CLI exceeded {:.1f}s".format(args.timeout),
                         "unavailable", True)
    except (OSError, ValueError):
        raise QueryError("cli_failed", "flyai CLI could not be executed", "unavailable", True)
    stderr_text = completed.stderr or ""
    if completed.returncode != 0:
        classified = classify_text(stderr_text) or classify_text(completed.stdout or "")
        raise classified or QueryError(
            "cli_nonzero", "flyai CLI exited with code {}".format(completed.returncode),
            "unavailable")
    payload = extract_json(completed.stdout)
    if not isinstance(payload, dict):
        raise QueryError("schema_mismatch", "flyai JSON payload is not an object", "unverified")
    return payload


def load_fixture(path, expected_tool):
    """Fixture envelope: {"tool", "sample_date", "result"} for samples, or
    {"tool", "error"} / {"tool", "exit"} to simulate CLI failures offline."""
    try:
        envelope = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise QueryError("fixture_invalid", "offline fixture cannot be read", "unverified")
    if not isinstance(envelope, dict) or envelope.get("tool") != expected_tool:
        raise QueryError("fixture_invalid", "offline fixture tool does not match", "unverified")
    if "error" in envelope:
        raw = envelope["error"]
        if not isinstance(raw, dict):
            raise QueryError("fixture_invalid", "offline fixture error is malformed", "unverified")
        error_class = raw.get("class", "cli_failed")
        status = raw.get("status", "unavailable")
        if status not in STATUSES or status in ("live", "sample"):
            status = "unavailable"
        raise QueryError(error_class, "offline fixture simulated " + error_class,
                         status, bool(raw.get("retryable")))
    if "exit" in envelope:
        raw = envelope["exit"]
        if not isinstance(raw, dict):
            raise QueryError("fixture_invalid", "offline fixture exit is malformed", "unverified")
        stdout = raw.get("stdout", "")
        stderr = raw.get("stderr", "")
        returncode = raw.get("returncode", 1)
        if returncode == 0:
            payload = extract_json(stdout)
            if not isinstance(payload, dict):
                raise QueryError("schema_mismatch",
                                 "flyai JSON payload is not an object", "unverified")
            sample_date = envelope.get("sample_date")
            try:
                parse_iso_date(sample_date)
            except QueryError:
                raise QueryError(
                    "fixture_invalid",
                    "simulated CLI success requires sample_date (fixtures never "
                    "impersonate live)", "unverified")
            return payload, sample_date
        classified = classify_text(stderr) or classify_text(stdout)
        raise classified or QueryError(
            "cli_nonzero", "flyai CLI exited with code {}".format(returncode), "unavailable")
    sample_date = envelope.get("sample_date")
    try:
        parse_iso_date(sample_date)
    except QueryError:
        raise QueryError("fixture_invalid", "sample fixture requires sample_date", "unverified")
    return envelope.get("result"), sample_date


def execute_query(base_argv, args, expected_tool):
    """Fixture mode short-circuits the CLI (CI never touches the real service);
    the key check happens before any subprocess is spawned."""
    if args.fixture:
        payload, sample_date = load_fixture(args.fixture, expected_tool)
        return payload, 0, sample_date
    if not api_key_present():
        raise QueryError("key_missing",
                         "{} is not set (env or .env); deep links remain available"
                         .format(CLI_KEY_ENV), "unavailable")
    payload, attempts = None, 0
    last_error = None
    for _ in range(args.retries + 1):
        attempts += 1
        try:
            payload = run_cli(base_argv, args)
            return payload, attempts, None
        except QueryError as error:
            last_error = error
            last_error.attempts = attempts
            if not error.retryable or attempts > args.retries:
                raise
    raise last_error


def demo_flags(payload):
    """体验模式识别: explicit demoMode flag or a systemMessage mentioning it."""
    if payload.get("demoMode") is True or payload.get("demo") is True:
        return True, payload.get("systemMessage")
    message = payload.get("systemMessage")
    if isinstance(message, str) and any(
            token in message for token in ("体验", "演示", "demo", "Demo", "沙箱")):
        return True, message
    return False, message if isinstance(message, str) else None


def mark_sample(result, sample_date):
    result["status"] = "sample"
    result["queried_at"] = None
    result["sample_date"] = sample_date
    result["notice"] = SAMPLE_NOTICE + "；样本日期 " + sample_date
    result["source"]["name"] = "offline_fixture"
    result["source"]["transport"] = "offline_fixture"
    result["source"]["api_key_present"] = None
    result["recommendations"] = {"primary": None, "backup": None}
    return result


def normalize_price(raw, masked_flag=None):
    """价格遮蔽/缺失只标记,绝不推测: status ok|masked|unavailable."""
    if masked_flag is True:
        return {"amount_cny": None, "status": "masked"}
    if raw is None:
        return {"amount_cny": None, "status": "unavailable"}
    if isinstance(raw, bool):
        return {"amount_cny": None, "status": "unavailable"}
    if isinstance(raw, str):
        cleaned = raw.strip()
        if any(marker in cleaned for marker in MASKED_MARKERS):
            return {"amount_cny": None, "status": "masked"}
        if not cleaned or cleaned in ("-", "—", "n/a", "N/A", "未知"):
            return {"amount_cny": None, "status": "unavailable"}
        try:
            raw = float(cleaned)
        except ValueError:
            return {"amount_cny": None, "status": "unavailable"}
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        price = float(raw)
        if math.isfinite(price) and price >= 0:
            return {"amount_cny": price, "status": "ok"}
    return {"amount_cny": None, "status": "unavailable"}


def normalize_basis(raw):
    value = raw if isinstance(raw, str) else ""
    value = value.strip().lower().replace("_", "-").replace(" ", "-")
    aliases = {
        "pernightperroom": "per-night-per-room",
        "per-night/room": "per-night-per-room",
        "每晚每间": "per-night-per-room",
        "pernightperperson": "per-night-per-person",
        "per-night/person": "per-night-per-person",
        "每晚每人": "per-night-per-person",
        "totalperroom": "total-per-room",
        "total/room": "total-per-room",
        "总价每间": "total-per-room",
        "totalperperson": "total-per-person",
        "total/person": "total-per-person",
        "总价每人": "total-per-person",
    }
    if value in PRICE_BASES:
        return value
    compact = re.sub(r"[-/]", "", value)
    for key, mapped in aliases.items():
        if compact == re.sub(r"[-/]", "", key):
            return mapped
    return "unknown"


def valid_clock(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{1,2}:\d{2}", value):
        raise ValueError("invalid clock")
    hour, minute = value.split(":", 1)
    return "{:02d}:{:02d}".format(int(hour), int(minute))


def as_clock_or_none(value):
    try:
        return valid_clock(value)
    except ValueError:
        return None


def normalize_flight_segment(raw):
    """One flight segment (or a whole direct flight). Missing optional fields
    stay None — never guessed."""
    if not isinstance(raw, dict):
        raise ValueError("segment is not an object")
    for key in FLIGHTS_REQUIRED_KEYS:
        if not isinstance(raw.get(key), str) or not raw.get(key):
            raise ValueError("segment misses required field " + key)
    dep = valid_clock(raw["depTime"])
    arr = valid_clock(raw["arrTime"])
    dep_date = date.fromisoformat(raw["depDate"])
    arr_date = date.fromisoformat(raw["arrDate"])
    if arr_date < dep_date:
        raise ValueError("arrival date is before departure date")
    duration = raw.get("durationMinutes")
    if (isinstance(duration, bool) or not isinstance(duration, (int, float))
            or not math.isfinite(duration) or duration <= 0):
        duration = int((datetime.combine(arr_date, datetime.strptime(arr, "%H:%M").time())
                        - datetime.combine(dep_date, datetime.strptime(dep, "%H:%M").time())
                        ).total_seconds() // 60) or None
    price = normalize_price(raw.get("price"), raw.get("priceMasked"))
    return {
        "flight_no": raw["flightNo"].upper(),
        "airline": raw.get("airline") if isinstance(raw.get("airline"), str) else None,
        "from": {
            "airport": raw["depAirport"],
            "terminal": raw.get("depTerminal") if isinstance(raw.get("depTerminal"), str) else None,
        },
        "to": {
            "airport": raw["arrAirport"],
            "terminal": raw.get("arrTerminal") if isinstance(raw.get("arrTerminal"), str) else None,
        },
        "dep": {"date": raw["depDate"], "time": dep},
        "arr": {"date": raw["arrDate"], "time": arr},
        "next_day_arrival": arr_date > dep_date,
        "duration_minutes": int(duration) if duration else None,
        "cabin": raw.get("cabin") if raw.get("cabin") in CABINS else None,
        "price": price,
        "detail_url": raw.get("detailUrl") if isinstance(raw.get("detailUrl"), str) else None,
        # 未知保持未知: bag / refund rules are never invented.
        "baggage": None,
        "refund_change": None,
    }


def filter_flight(offer, args):
    dep_hour = int(offer["segments"][0]["dep"]["time"][:2])
    if not (args.earliest <= dep_hour < args.latest):
        return False
    cabin = offer["segments"][0]["cabin"]
    if args.cabin and cabin is not None and cabin != args.cabin:
        return False
    if args.direct_only and offer["stops"] > 0:
        return False
    if offer["stops"] > args.max_transfers:
        return False
    return True


def normalize_flights(payload, args):
    """Normalize the CLI payload into offers[] with 主/备 recommendations.
    Ranking is NOT price-only: direct first, then duration, then price; offers
    with an unknown price stay in the list, ranked last, never dropped."""
    demo, system_message = demo_flags(payload)
    result = base_result("flights", args)
    result["source"]["cli_version"] = payload.get("cliVersion") \
        if isinstance(payload.get("cliVersion"), str) else None
    result["query"] = {
        "from": args.orig, "to": args.dest, "date": args.travel_date,
        "return_date": args.return_date, "adults": args.adults,
        "cabin": args.cabin, "earliest": args.earliest, "latest": args.latest,
        "direct_only": args.direct_only, "max_transfers": args.max_transfers,
    }
    result["query_key"] = "flights:{}:{}:{}:{}-{}:{}:{}".format(
        args.travel_date, args.orig, args.dest, args.adults,
        args.earliest, args.latest, args.max_transfers)
    if system_message:
        result["system_message"] = system_message
    if demo:
        return error_result(result, QueryError("demo_mode", "flyai 体验模式输出", "demo"))
    rows = payload.get("offers")
    if rows is None:
        rows = payload.get("flights")
    if not isinstance(rows, list):
        raise QueryError("schema_mismatch", "flights payload has no offers list", "unverified")
    if not rows:
        result["status"] = "no_service"
        result["reason"] = "valid_empty_result"
        return result
    offers, malformed = [], 0
    for raw in rows:
        try:
            if not isinstance(raw, dict):
                raise ValueError("offer is not an object")
            segments_raw = raw.get("segments")
            if isinstance(segments_raw, list) and segments_raw:
                segments = [normalize_flight_segment(s) for s in segments_raw]
                if len(segments) > 2:
                    raise ValueError("more than two segments are unsupported")
            else:
                segments = [normalize_flight_segment(raw)]
            first, last = segments[0], segments[-1]
            if first["dep"]["date"] != args.travel_date:
                raise ValueError("offer date differs from requested travel date")
            offers.append({
                "segments": segments,
                "stops": len(segments) - 1,
                "flight_no": " + ".join(s["flight_no"] for s in segments),
                "from": first["from"], "to": last["to"],
                "dep": first["dep"], "arr": last["arr"],
                "next_day_arrival": last["next_day_arrival"],
                "duration_minutes": sum(s["duration_minutes"] or 0 for s in segments) or None,
                "price": (sum_price(segments) if len(segments) > 1
                          else segments[0]["price"]),
                "detail_url": last["detail_url"],
            })
        except (KeyError, TypeError, ValueError):
            malformed += 1
    result["malformed_rows"] = malformed
    offers = [offer for offer in offers if filter_flight(offer, args)]
    if not offers:
        result["status"] = "unverified" if malformed else "no_service"
        result["reason"] = "all_rows_malformed" if malformed else "no_rows_match_filters"
        return result
    result["status"] = "live"
    offers.sort(key=lambda o: (
        o["stops"],
        o["duration_minutes"] if o["duration_minutes"] is not None else 10 ** 6,
        o["price"]["amount_cny"] if o["price"]["status"] == "ok" else 10 ** 7,
    ))
    offers = offers[:args.limit]
    result["offers"] = offers
    result["recommendations"] = {
        "primary": 0 if offers else None,
        "backup": 1 if len(offers) > 1 else None,
    }
    if malformed:
        result["warnings"] = ["{} malformed offer(s) isolated".format(malformed)]
    return result


def sum_price(segments):
    """Multi-segment price = sum of known segment prices; any unknown/masked
    segment keeps the whole itinerary price unknown."""
    prices = [s["price"] for s in segments]
    if any(p["status"] != "ok" for p in prices):
        status = "masked" if any(p["status"] == "masked" for p in prices) else "unavailable"
        return {"amount_cny": None, "status": status}
    return {"amount_cny": sum(p["amount_cny"] for p in prices), "status": "ok"}


def run_flights(args):
    travel = parse_iso_date(args.travel_date)
    today = datetime.now(BEIJING_TZ).date()
    result = base_result("flights", args)
    if travel < today and not args.fixture:
        return error_result(result, QueryError(
            "past_date", "travel date is in the past", "unverified"))
    argv = list(FLIGHTS_TEMPLATE) + [
        "--from", args.orig, "--to", args.dest, "--date", args.travel_date,
        "--adults", str(args.adults),
    ]
    if args.return_date:
        argv += ["--return-date", args.return_date]
    if args.cabin:
        argv += ["--cabin", args.cabin]
    if args.direct_only:
        argv += ["--direct-only"]
    else:
        argv += ["--max-transfers", str(args.max_transfers)]
    try:
        payload, attempts, sample_date = execute_query(argv, args, "flights")
        result = normalize_flights(payload, args)
        result["attempts"] = attempts
        if sample_date and result.get("status") == "live":
            mark_sample(result, sample_date)
    except QueryError as error:
        result = error_result(result, error)
        result["attempts"] = getattr(error, "attempts", 0)
    result["links"] = fallback_links_flights(args.orig, args.dest,
                                             args.travel_date, args.adults)
    return result


def normalize_hotel_offer(raw, guests):
    if not isinstance(raw, dict):
        raise ValueError("offer is not an object")
    required = ("hotelName", "roomType")
    if any(not isinstance(raw.get(k), str) or not raw.get(k) for k in required):
        raise ValueError("offer misses hotelName/roomType")
    price = normalize_price(raw.get("price"), raw.get("priceMasked"))
    basis = normalize_basis(raw.get("priceBasis"))
    max_guests = raw.get("maxGuests") if isinstance(raw.get("maxGuests"), int) \
        and not isinstance(raw.get("maxGuests"), bool) else None
    rating = raw.get("rating")
    if (isinstance(rating, bool) or not isinstance(rating, (int, float))
            or not math.isfinite(rating) or not 0 <= rating <= 5):
        rating = None
    lat, lon = raw.get("lat"), raw.get("lon")
    location = None
    if (isinstance(lat, (int, float)) and isinstance(lon, (int, float))
            and not isinstance(lat, bool) and not isinstance(lon, bool)
            and math.isfinite(lat) and math.isfinite(lon)):
        location = {"lat": round(float(lat), 6), "lon": round(float(lon), 6)}
    hard_requirements_ok = max_guests is None or max_guests >= guests
    return {
        "hotel_name": raw["hotelName"],
        "hotel_id": raw.get("hotelId") if isinstance(raw.get("hotelId"), str) else None,
        "room_type": raw["roomType"],
        "beds": raw.get("beds") if isinstance(raw.get("beds"), str) else None,
        "occupancy": {"max_guests": max_guests,
                      "per_room": True if raw.get("perRoom") is not False else False},
        "price": dict(price, basis=basis),
        "taxes_fees": raw.get("taxesFees") if raw.get("taxesFees") in
                      ("included", "excluded") else "unknown",
        "breakfast": raw.get("breakfast") if isinstance(raw.get("breakfast"), str) else None,
        "cancellation": raw.get("cancellation") if isinstance(raw.get("cancellation"), str) else None,
        "rating": rating,
        "location": location,  # 高德核验由 agent 决定;无坐标则未实测
        "detail_url": raw.get("detailUrl") if isinstance(raw.get("detailUrl"), str) else None,
        "hard_requirements_ok": hard_requirements_ok,
        # mainPic 有意不读取:未明确对应房型且未核验授权时不放图。
    }


def organize_tiers(offers, guests, tier_count=None):
    """按价格组织 2–3 档;不满足硬要求(人数)的房型不凑档;口径未知或价格
    遮蔽/缺失的候选不参与分档,保留原样供人工核对。"""
    eligible = [o for o in offers if o["hard_requirements_ok"]
                and o["price"]["status"] == "ok"
                and o["price"]["basis"] in ("per-night-per-room", "per-night-per-person")]
    count = tier_count or min(3, max(1, len(eligible)))
    ranked = sorted(eligible, key=lambda o: o["price"]["amount_cny"])
    for offer in offers:
        offer["tier"] = None
    for index, offer in enumerate(ranked):
        offer["tier"] = index * count // max(1, len(ranked)) + 1
    return offers


def normalize_hotels(payload, args):
    demo, system_message = demo_flags(payload)
    result = base_result("hotels", args)
    result["source"]["cli_version"] = payload.get("cliVersion") \
        if isinstance(payload.get("cliVersion"), str) else None
    result["query"] = {
        "city": args.city, "checkin": args.checkin, "checkout": args.checkout,
        "guests": args.guests, "rooms": args.rooms,
    }
    result["query_key"] = "hotels:{}:{}:{}:{}:{}".format(
        args.city, args.checkin, args.checkout, args.guests, args.rooms)
    if system_message:
        result["system_message"] = system_message
    if demo:
        return error_result(result, QueryError("demo_mode", "flyai 体验模式输出", "demo"))
    rows = payload.get("offers")
    if rows is None:
        rows = payload.get("hotels")
    if not isinstance(rows, list):
        raise QueryError("schema_mismatch", "hotels payload has no offers list", "unverified")
    if not rows:
        result["status"] = "no_service"
        result["reason"] = "valid_empty_result"
        return result
    offers, malformed = [], 0
    for raw in rows:
        try:
            offers.append(normalize_hotel_offer(raw, args.guests))
        except (KeyError, TypeError, ValueError):
            malformed += 1
    if not offers:
        result["status"] = "unverified"
        result["reason"] = "all_rows_malformed"
        result["malformed_rows"] = malformed
        return result
    result["status"] = "live"
    result["malformed_rows"] = malformed
    result["offers"] = organize_tiers(offers, args.guests, args.tiers)
    if malformed:
        result["warnings"] = ["{} malformed offer(s) isolated".format(malformed)]
    return result


def run_hotels(args):
    checkin = parse_iso_date(args.checkin)
    checkout = parse_iso_date(args.checkout)
    if checkout <= checkin:
        return error_result(base_result("hotels", args), QueryError(
            "invalid_date", "checkout must be after checkin", "unverified"))
    argv = list(HOTELS_TEMPLATE) + [
        "--city", args.city, "--checkin", args.checkin, "--checkout", args.checkout,
        "--guests", str(args.guests), "--rooms", str(args.rooms),
    ]
    try:
        payload, attempts, sample_date = execute_query(argv, args, "hotels")
        result = normalize_hotels(payload, args)
        result["attempts"] = attempts
        if sample_date and result.get("status") == "live":
            mark_sample(result, sample_date)
    except QueryError as error:
        result = error_result(base_result("hotels", args), error)
        result["attempts"] = getattr(error, "attempts", 0)
    result["links"] = fallback_links_hotels(args.city, args.checkin, args.checkout)
    return result


def money(value):
    if value is None:
        return None
    return "¥{:.2f}".format(value).rstrip("0").rstrip(".")


BASIS_LABELS = {
    "per-night-per-room": "/晚·每间",
    "per-night-per-person": "/晚·每人",
    "total-per-room": "/全程·每间",
    "total-per-person": "/全程·每人",
}


def price_text(price, nights=None):
    if price["status"] == "ok":
        label = BASIS_LABELS.get(price.get("basis"), "")
        text = money(price["amount_cny"]) + label
        if price.get("basis") == "unknown":
            text += "（口径未知,勿加总）"
        if price.get("taxes_fees") == "excluded":
            text += "（税费另计）"
        elif price.get("taxes_fees") == "unknown":
            text += "（含税未知）"
        return text
    if price["status"] == "masked":
        return "价格遮蔽,点开核对"
    return "—,点开核对"


def flight_summary(offer):
    segments = offer["segments"]
    parts = [offer["flight_no"], "{}→{}".format(offer["from"]["airport"],
                                                offer["to"]["airport"])]
    for segment in segments:
        terminal_dep = segment["from"]["terminal"] or ""
        terminal_arr = segment["to"]["terminal"] or ""
        parts.append("{} {}{}–{}{}{}".format(
            segment["dep"]["date"], segment["dep"]["time"],
            " " + terminal_dep if terminal_dep else "",
            segment["arr"]["time"],
            " " + terminal_arr if terminal_arr else "",
            "（次日到）" if segment["next_day_arrival"] else ""))
    duration = offer["duration_minutes"]
    parts.append("{} 分钟".format(duration) if duration else "历时未知")
    parts.append(price_text(offer["price"]) if offer["price"]["status"] == "ok"
                 else {"masked": "价格遮蔽", "unavailable": "价格未知"}.get(
                     offer["price"]["status"], "价格未知"))
    return " · ".join(str(p) for p in parts)


def hotel_summary(offer):
    parts = ["{} · {}".format(offer["hotel_name"], offer["room_type"])]
    if offer["beds"]:
        parts.append(offer["beds"])
    if offer["occupancy"]["max_guests"] is not None:
        parts.append("≤{}人".format(offer["occupancy"]["max_guests"]))
    parts.append(price_text(offer["price"]))
    if offer["breakfast"]:
        parts.append(offer["breakfast"])
    if offer["cancellation"]:
        parts.append(offer["cancellation"])
    if offer["rating"] is not None:
        parts.append("评分 {:.1f}".format(offer["rating"]))
    if offer.get("tier"):
        parts.append("档位 {}".format(offer["tier"]))
    return " · ".join(str(p) for p in parts)


def response_to_text(result):
    lines = ["status: {} · {} · 查询 {}".format(
        result["status"], result["kind"], result["queried_at"] or "—")]
    if result.get("sample_date"):
        lines.append("样本日期: {}（非实时,不可写入计划）".format(result["sample_date"]))
    if result.get("error"):
        lines.append("error: {} · {}".format(result["error"]["class"],
                                             result["error"]["message"]))
    if result.get("system_message"):
        lines.append("systemMessage: {}".format(result["system_message"]))
    if result["status"] in ("live", "sample"):
        for offer in result.get("offers", []):
            summary = flight_summary(offer) if result["kind"] == "flights" \
                else hotel_summary(offer)
            lines.append("- " + summary)
        recommendations = result.get("recommendations", {})
        if result["kind"] == "flights" and recommendations.get("primary") is not None:
            lines.append("主选: 下标 {} · 备选: {}".format(
                recommendations.get("primary"), recommendations.get("backup")))
    lines.append(result.get("notice", LIVE_NOTICE))
    links = result.get("links") or {}
    if links.get("text"):
        lines.append(links["text"])
    return "\n".join(lines)


def selected_offer(result):
    if not isinstance(result, dict) or result.get("status") != "live":
        return None
    recommendations = result.get("recommendations", {})
    offers = result.get("offers", [])
    index = recommendations.get("primary") if isinstance(recommendations, dict) else None
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(offers):
        return None
    return offers[index] if isinstance(offers[index], dict) else None


def airport_label(airport):
    if not isinstance(airport, dict):
        return None
    label = airport.get("airport", "")
    terminal = airport.get("terminal")
    if terminal:
        label = "{} {}".format(label, terminal)
    return label or None


def live_leg_fields(result):
    """Only a recommended live offer becomes a leg. 未知字段保持未知:
    baggage/refund_change are never invented."""
    offer = selected_offer(result)
    if offer is None:
        raise ValueError("only a recommended live offer can become a leg")
    backup_index = result["recommendations"].get("backup")
    backup = (flight_summary(result["offers"][backup_index])
              if isinstance(backup_index, int) and 0 <= backup_index < len(result["offers"])
              else "暂无可靠备选；点开深链核对")
    note = "flyai live · 查询 {} · {}".format(result["queried_at"], LIVE_NOTICE)
    if offer["stops"] > 0:
        note += " · 中转 {} 次（换乘衔接以 detailUrl 为准）".format(offer["stops"])
    if offer["next_day_arrival"]:
        note += " · 次日 {} 到达".format(offer["arr"]["time"])
    price = (money(offer["price"]["amount_cny"]) + "（flyai 查询时参考价·单源）"
             if offer["price"]["status"] == "ok" else "—,见深链/detailUrl 核对")
    return {
        "type": "flight",
        "date": result["query"]["date"],
        "carrier": offer["flight_no"],
        "from": airport_label(offer["from"]) or result["query"]["from"],
        "to": airport_label(offer["to"]) or result["query"]["to"],
        "dep": offer["dep"]["time"],
        "arr": offer["arr"]["time"],
        "price": price,
        "bags": "行李额未实测,点开核对",
        "refund_change": "退改规则未实测,点开核对",
        "link": offer["detail_url"] or (result.get("links") or {}).get("ctrip_check"),
        "note": note,
        "backup": backup,
    }


def existing_priority(row):
    live = row.get("flight_live")
    if isinstance(live, dict):
        return STATUS_PRIORITY.get(live.get("status"), 0)
    return STATUS_PRIORITY["deep_link"] if row.get("link") else STATUS_PRIORITY["unverified"]


def merge_flights_plan(plan, response):
    """Return (new_plan, metadata); input plan is never mutated. Legacy manual
    fields are never changed — only the optional flight_live object is added."""
    updated = copy.deepcopy(plan)
    info = {"written": False, "reason": "no_recommended_live_offer"}
    offer = selected_offer(response)
    if offer is None:
        return updated, info
    if not isinstance(updated, dict) or not isinstance(updated.get("legs", []), list):
        info["reason"] = "invalid_plan_legs"
        return updated, info
    legs = updated.setdefault("legs", [])
    query = response.get("query", {})
    key = response.get("query_key")
    from_labels = {query.get("from"), airport_label(offer["from"])} - {None}
    to_labels = {query.get("to"), airport_label(offer["to"])} - {None}
    managed = next((r for r in legs if isinstance(r, dict) and
                    isinstance(r.get("flight_live"), dict) and
                    r["flight_live"].get("query_key") == key and
                    r.get("_flyai_managed") is True), None)
    manual = next((r for r in legs if isinstance(r, dict) and
                   r.get("_flyai_managed") is not True and
                   r.get("date") == query.get("date") and
                   r.get("from") in from_labels and r.get("to") in to_labels), None)
    if ((managed is not None and existing_priority(managed) > STATUS_PRIORITY["live"]) or
            (manual is not None and existing_priority(manual) > STATUS_PRIORITY["live"])):
        info["reason"] = "higher_priority_result_kept"
        return updated, info
    fields = live_leg_fields(response)
    if managed is not None:
        managed.update(fields)
        managed["flight_live"] = copy.deepcopy(response)
        info.update({"written": True, "reason": "updated_managed_leg",
                     "leg_index": legs.index(managed)})
    elif manual is not None:
        manual["flight_live"] = copy.deepcopy(response)
        info.update({"written": True, "reason": "augmented_manual_leg_without_field_changes",
                     "leg_index": legs.index(manual)})
    else:
        row = dict(fields)
        row["_flyai_managed"] = True
        row["flight_live"] = copy.deepcopy(response)
        legs.append(row)
        info.update({"written": True, "reason": "leg_appended",
                     "leg_index": len(legs) - 1})
    return updated, info


def hotel_option_fields(offer, response, nights):
    """New additive options[] entry: legacy name/band/link keys stay compatible;
    every enrichment lives in the optional flyai object."""
    name = "{} · {}".format(offer["hotel_name"], offer["room_type"])
    price = offer["price"]
    price_view = dict(price, taxes_fees=offer["taxes_fees"])
    band = price_text(price_view)
    if (price["status"] == "ok" and price.get("basis") == "per-night-per-room"
            and nights):
        band += " · {} 晚合计 {}（同口径加总）".format(
            nights, money(price["amount_cny"] * nights))
    return {
        "name": name,
        "band": band,
        "link": offer["detail_url"],
        "flyai": {
            "status": "live",
            "source": "flyai (飞猪)",
            "queried_at": response["queried_at"],
            "checkin": response["query"]["checkin"],
            "checkout": response["query"]["checkout"],
            "room_type": offer["room_type"],
            "beds": offer["beds"],
            "occupancy": offer["occupancy"],
            "price": price,
            "taxes_fees": offer["taxes_fees"],
            "breakfast": offer["breakfast"],
            "cancellation": offer["cancellation"],
            "rating": offer["rating"],
            "location": offer["location"],
            "location_checked": False,  # 高德核验由 agent 完成;默认未实测
            "tier": offer.get("tier"),
            "notice": LIVE_NOTICE,
        },
    }


def merge_hotels_plan(plan, response, base):
    """Extend hotels[].options[] additively. Existing options (manual or older
    flyai) are never modified; duplicate names are skipped; budget[] is never
    touched — live prices are hard spendings inside options, estimates stay
    separate."""
    updated = copy.deepcopy(plan)
    info = {"written": False, "reason": "response_not_live"}
    if not isinstance(response, dict) or response.get("status") != "live":
        info["reason"] = "response_not_live"
        return updated, info
    if not isinstance(updated, dict) or not isinstance(updated.get("hotels", []), list):
        info["reason"] = "invalid_plan_hotels"
        return updated, info
    hotels = updated.setdefault("hotels", [])
    query = response.get("query", {})
    checkin, checkout = query.get("checkin"), query.get("checkout")
    try:
        nights = (parse_iso_date(checkout) - parse_iso_date(checkin)).days
    except QueryError:
        nights = None
    base_needle = (base or query.get("city") or "").strip()
    target = next((h for h in hotels if isinstance(h, dict) and
                  base_needle and base_needle in str(h.get("base", ""))), None)
    if target is None:
        target = {
            "base": "{} {} nights（flyai 候选）".format(
                query.get("city"), nights or ""),
            "area": "待人工补充（flyai 未核验街区）",
            "why": "flyai live 候选；位置安全性与夜间动线需人工核验",
            "options": [],
            "_flyai_managed": True,
        }
        hotels.append(target)
        info["hotel_appended"] = True
        info["hotel_index"] = len(hotels) - 1
    else:
        info["hotel_index"] = hotels.index(target)
    existing_names = {o.get("name") for o in target.get("options", [])
                      if isinstance(o, dict)}
    added, skipped = 0, 0
    tier_order = {None: 9}
    for offer in sorted(response.get("offers", []),
                        key=lambda o: (tier_order.get(o.get("tier"), 9),
                                       o["hotel_name"])):
        row = hotel_option_fields(offer, response, nights)
        if row["name"] in existing_names:
            skipped += 1
            continue
        target.setdefault("options", []).append(row)
        existing_names.add(row["name"])
        added += 1
        if added >= (response.get("merge_limit") or 6):
            break
    if added:
        info.update({"written": True,
                     "reason": "hotel_appended" if info.get("hotel_appended")
                     else "extended_options",
                     "options_added": added, "options_skipped_duplicate": skipped})
    else:
        info.update({"written": False, "reason": "no_new_options",
                     "options_skipped_duplicate": skipped})
    return updated, info


def atomic_write_plan(path, plan):
    destination = Path(path)
    original_mode = destination.stat().st_mode
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(destination.parent), prefix=destination.name + ".",
        suffix=".tmp", delete=False,
    )
    try:
        with handle:
            json.dump(plan, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.chmod(handle.name, original_mode)
        os.replace(handle.name, destination)
    except Exception:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


def run_doctor(args):
    """Environment health check. Never sends a query, never prints the key,
    only its presence. Checks that the real CLI signature matches the adapter's
    assumed template (flights/hotels + --format json)."""
    _load_dotenv()
    report = {
        "schema_version": SCHEMA_VERSION,
        "python": "{}.{}.{}".format(*sys.version_info[:3]),
        "cli": {
            "name": CLI_NAME,
            "override": args.cli if args.cli != CLI_NAME else None,
            "found": False, "path": None, "version": None,
            "signature_check": {"flights": False, "hotels": False, "format_json": False},
            "help_head": None,
        },
        "env": {
            CLI_KEY_ENV: api_key_present(),
            "env_files": [str(p) for p in (Path(__file__).resolve().parent.parent / ".env",
                                           Path.cwd() / ".env") if p.is_file()],
        },
        "capabilities": {"flights": "unavailable", "hotels": "unavailable"},
        "notes": [],
    }
    executable = shutil.which(args.cli)
    report["cli"]["found"] = executable is not None
    report["cli"]["path"] = executable
    if executable is None:
        report["notes"].append(
            "flyai CLI 未安装:深链与价格区间不受影响;安装后再运行 doctor 复核签名")
    else:
        try:
            completed = subprocess.run(
                [executable] + VERSION_TEMPLATE, shell=False,
                timeout=args.timeout or DOCTOR_TIMEOUT, capture_output=True,
                text=True, encoding="utf-8", errors="replace")
            if completed.returncode == 0 and completed.stdout:
                report["cli"]["version"] = completed.stdout.strip().splitlines()[0][:200]
        except (subprocess.TimeoutExpired, OSError):
            report["notes"].append("flyai --version 超时或失败(不影响深链降级)")
        try:
            completed = subprocess.run(
                [executable, "--help"], shell=False, timeout=args.timeout or DOCTOR_TIMEOUT,
                capture_output=True, text=True, encoding="utf-8", errors="replace")
            help_text = (completed.stdout or "") + (completed.stderr or "")
        except (subprocess.TimeoutExpired, OSError):
            help_text = ""
            report["notes"].append("flyai --help 超时或失败(不影响深链降级)")
        if help_text:
            lines = [line.strip() for line in help_text.splitlines() if line.strip()]
            report["cli"]["help_head"] = lines[0][:200] if lines else None
            report["cli"]["signature_check"] = {
                "flights": any("flights" in line for line in lines),
                "hotels": any("hotels" in line for line in lines),
                "format_json": "--format" in help_text and "json" in help_text,
            }
            if not all(report["cli"]["signature_check"].values()):
                report["notes"].append(
                    "CLI 签名与适配器假设不一致:先运行 flyai --help 人工核对,"
                    "必要时修订 scripts/flyai_provider.py 的 CLI_TEMPLATE")
    if not api_key_present():
        report["notes"].append(
            "{} 未设置:查询保持 unavailable,深链/价格区间完整可用".format(CLI_KEY_ENV))
    live_capable = report["cli"]["found"] and api_key_present()
    if live_capable:
        report["capabilities"] = {"flights": "live-capable", "hotels": "live-capable"}
        report["notes"].append("doctor 只读检查,未发起任何真实查询;首次真机查询前先小量只读验证")
    report["summary"] = ("flyai 可用" if live_capable else
                         "flyai 不可用(降级:深链+价格区间,现有计划不受影响)")
    report["exit_code"] = 0 if live_capable else 2
    return report


def emit(result, output_format="json", plan_info=None):
    if plan_info is not None:
        result = dict(result)
        result["plan_merge"] = plan_info
    if output_format == "text":
        text = response_to_text(result)
        if plan_info is not None:
            text += "\nplan: {} · {}".format(plan_info.get("written"),
                                             plan_info.get("reason"))
        print(text)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def common(subparser):
        subparser.add_argument("--cli", default=os.environ.get(CLI_ENV_OVERRIDE, CLI_NAME),
                               help="flyai 可执行文件名或绝对路径(默认 flyai)")
        subparser.add_argument("--timeout", type=float, default=30.0,
                               help="每次 CLI 调用超时秒数")
        subparser.add_argument("--retries", type=int, default=1, choices=range(0, 3),
                               help="超时/失败有限重试(限流不重试)")
        subparser.add_argument("--fixture",
                               help="离线 fixture;有效数据强制标为 sample,不写计划")
        subparser.add_argument("--output", choices=("json", "text"), default="json")

    doctor = sub.add_parser("doctor", help="只读环境体检:CLI/Key/签名,不发查询")
    common(doctor)

    flights = sub.add_parser("flights", help="机票只读查询(降级:深链)")
    common(flights)
    flights.add_argument("--from", dest="orig", required=True,
                         type=argparse_text("from"),
                         help="出发机场三字码或城市名, e.g. PVG")
    flights.add_argument("--to", dest="dest", required=True, type=argparse_text("to"))
    flights.add_argument("--date", dest="travel_date", required=True, type=argparse_date)
    flights.add_argument("--return-date", type=argparse_date,
                         help="可选返程日期(往返查询透传,规范化按去程)")
    flights.add_argument("--adults", type=int, default=1, choices=range(1, 10),
                         help="人数(同时限制查询与深链)")
    flights.add_argument("--cabin", choices=CABINS, help="舱等筛选")
    flights.add_argument("--earliest", type=int, default=0, choices=range(0, 24),
                         help="出发时段下限(小时)")
    flights.add_argument("--latest", type=int, default=24, choices=range(1, 25),
                         help="出发时段上限(小时)")
    flights.add_argument("--direct-only", action="store_true", help="仅直飞")
    flights.add_argument("--max-transfers", type=int, default=1, choices=range(0, 3),
                         help="最大中转次数")
    flights.add_argument("--limit", type=int, default=3, choices=range(1, 6),
                         help="保留候选数量(主+备)")
    flights.add_argument("--plan", help="可选 plan.geo.json;仅 live 增量写入 legs[]")
    flights.add_argument("--write", action="store_true", help="原子写回 --plan;非 live 不写")

    hotels = sub.add_parser("hotels", help="酒店房型只读查询(降级:深链)")
    common(hotels)
    hotels.add_argument("--city", required=True, type=argparse_text("city", 60))
    hotels.add_argument("--checkin", required=True, type=argparse_date)
    hotels.add_argument("--checkout", required=True, type=argparse_date)
    hotels.add_argument("--guests", type=int, default=2, choices=range(1, 10))
    hotels.add_argument("--rooms", type=int, default=1, choices=range(1, 6))
    hotels.add_argument("--tiers", type=int, default=None, choices=range(1, 4),
                        help="按价格分档数(默认按候选数自动 1–3)")
    hotels.add_argument("--merge-limit", type=int, default=6, choices=range(1, 13),
                        help="写入计划时每个 base 最多新增的 options 数")
    hotels.add_argument("--plan", help="可选 plan.geo.json;仅 live 增量扩展 hotels[].options[]")
    hotels.add_argument("--base", help="匹配 hotels[].base 的子串;不匹配则追加受管行")
    hotels.add_argument("--write", action="store_true", help="原子写回 --plan;非 live 不写")

    merge = sub.add_parser("merge", help="把已保存的 live 响应增量合并进计划")
    common(merge)
    merge.add_argument("--plan", required=True)
    merge.add_argument("--response", required=True)
    merge.add_argument("--base", help="酒店响应匹配 hotels[].base 的子串")
    merge.add_argument("--write", action="store_true",
                       help="原子写回 --plan；非 live 不写")
    return parser


def main(argv=None):
    _load_dotenv()
    args = build_parser().parse_args(argv)
    if args.timeout is not None and not (0 < args.timeout <= 120):
        build_parser().error("--timeout must be > 0 and <= 120")
    if args.command == "doctor":
        report = run_doctor(args)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return report["exit_code"]
    if args.command == "flights":
        result = run_flights(args)
        plan_info = None
        if args.plan:
            try:
                plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
                updated, plan_info = merge_flights_plan(plan, result)
                if args.write and plan_info["written"]:
                    atomic_write_plan(args.plan, updated)
                elif args.write:
                    plan_info["reason"] += "_plan_unchanged"
            except (OSError, json.JSONDecodeError, ValueError, KeyError, TypeError,
                    AttributeError) as error:
                plan_info = {"written": False, "reason": "merge_failed", "message": str(error)}
                emit(result, args.output, plan_info)
                return 2
        return emit(result, args.output, plan_info)
    if args.command == "hotels":
        result = run_hotels(args)
        result["merge_limit"] = args.merge_limit
        plan_info = None
        if args.plan:
            try:
                plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
                updated, plan_info = merge_hotels_plan(plan, result, args.base)
                if args.write and plan_info["written"]:
                    atomic_write_plan(args.plan, updated)
                elif args.write:
                    plan_info["reason"] += "_plan_unchanged"
            except (OSError, json.JSONDecodeError, ValueError, KeyError, TypeError,
                    AttributeError) as error:
                plan_info = {"written": False, "reason": "merge_failed", "message": str(error)}
                emit(result, args.output, plan_info)
                return 2
        return emit(result, args.output, plan_info)
    # merge subcommand: rerun the normalisation on a saved response envelope.
    try:
        plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        response = json.loads(Path(args.response).read_text(encoding="utf-8"))
        if response.get("kind") == "flights":
            updated, plan_info = merge_flights_plan(plan, response)
        elif response.get("kind") == "hotels":
            updated, plan_info = merge_hotels_plan(plan, response, args.base)
        else:
            emit({"status": "unverified", "error": {
                "class": "unknown_kind", "message": "response kind not supported"}}, args.output)
            return 2
        if args.write and plan_info["written"]:
            atomic_write_plan(args.plan, updated)
        elif args.write:
            plan_info["reason"] += "_plan_unchanged"
        return emit(response, args.output, plan_info)
    except (OSError, json.JSONDecodeError, ValueError, KeyError, TypeError,
            AttributeError) as error:
        emit({"status": "unverified", "error": {
            "class": "merge_failed", "message": str(error)}}, args.output)
        return 2


if __name__ == "__main__":
    sys.exit(main())
