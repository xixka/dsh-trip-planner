#!/usr/bin/env python3
"""Optional 12306 live rail queries through a local stdio MCP server.

No third-party Python packages, credentials, scraping, login, booking, or passenger
information are used. Without --mcp-command (or TRAIN_LIVE_MCP_COMMAND), the
result is ``unavailable`` plus the existing 12306/Ctrip deep links.

Examples:
  python3 scripts/train_live.py direct --from 北京南 --to 上海虹桥 --date 2026-12-01
  python3 scripts/train_live.py direct --from 北京南 --to 上海虹桥 --date 2026-12-01 \
      --mcp-command 'node /opt/12306-mcp/build/index.js' --transfer auto
  python3 scripts/train_live.py route --train G1 --date 2026-12-01 \
      --mcp-command 'node /opt/12306-mcp/build/index.js'
  python3 scripts/train_live.py direct --from 北京南 --to 上海虹桥 --date 2026-12-01 \
      --fixture tests/fixtures/train_live/direct-live.json

Only a schema-valid ``live`` result can be merged into plan.legs[]. Fixtures are
always ``sample`` and can never overwrite a plan. See references/train-live.md.
"""

import argparse
import copy
import json
import math
import os
import queue
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from flight_scan import rail_links

SCHEMA_VERSION = 1
UPSTREAM_REPOSITORY = "Joooook/12306-mcp"
UPSTREAM_VERSION = "0.3.10"
UPSTREAM_COMMIT = "ff6439da6f63d7d72181abea4568abd69878c600"
BEIJING_TZ = timezone(timedelta(hours=8), name="CST")
TIMEZONE_NAME = "Asia/Shanghai"
OFFICIAL_HOME = "https://www.12306.cn/index/"
LIVE_NOTICE = "查询时参考，购票以 12306 为准"
SAMPLE_NOTICE = "注明日期的离线样本，非实时数据"
STATUSES = {
    "live", "not_on_sale", "sold_out", "no_service", "sample",
    "unavailable", "unverified",
}
STATUS_PRIORITY = {"unverified": 0, "unavailable": 0, "deep_link": 1,
                   "not_on_sale": 1, "no_service": 1, "sold_out": 1,
                   "sample": 2, "live": 3}
TOOL_ARGUMENTS = {
    "get-tickets": ("date", "fromStation", "toStation", "format"),
    "get-interline-tickets": ("date", "fromStation", "toStation", "format"),
    "get-train-route-stations": ("trainCode", "departDate", "format"),
}


class QueryError(Exception):
    """Sanitized, classified query failure; raw server stderr is never exposed."""

    def __init__(self, error_class, message, status="unavailable", retryable=False):
        super().__init__(message)
        self.error_class = error_class
        self.message = message
        self.status = status
        self.retryable = retryable


def now_beijing():
    return datetime.now(BEIJING_TZ).isoformat(timespec="seconds")


def parse_iso_date(value):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise QueryError("invalid_date", "date must be YYYY-MM-DD", "unverified")


def station_input(name):
    value = (name or "").strip()
    if not value or len(value) > 40 or any(ord(c) < 32 for c in value):
        raise QueryError("invalid_station", "station name is empty or invalid", "unverified")
    return value


def validate_train_code(value):
    code = (value or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{1,12}", code):
        raise QueryError("invalid_train", "train code is invalid", "unverified")
    return code


def argparse_date(value):
    try:
        parse_iso_date(value)
        return value
    except QueryError as error:
        raise argparse.ArgumentTypeError(error.message)


def argparse_station(value):
    try:
        return station_input(value)
    except QueryError as error:
        raise argparse.ArgumentTypeError(error.message)


def argparse_train(value):
    try:
        return validate_train_code(value)
    except QueryError as error:
        raise argparse.ArgumentTypeError(error.message)


def train_flags(value):
    flags = (value or "").strip().upper()
    if not re.fullmatch(r"[GDZTKOFS]{0,8}", flags):
        raise argparse.ArgumentTypeError("train flags must match [GDZTKOFS]{0,8}")
    return flags


def expected_check_date(travel_date, presale_days):
    if presale_days <= 0:
        return None
    return (travel_date - timedelta(days=presale_days - 1)).isoformat()


def fallback_links(orig, dest, depart, adults=1):
    """Reuse the existing deep-link generator verbatim as the complete fallback."""
    text = rail_links(orig, dest, depart, None, adults)
    urls = re.findall(r"https?://[^\s]+", text)
    official = next((u for u in urls if "12306.cn" in u), OFFICIAL_HOME)
    ctrip = next((u for u in urls if "ctrip.com" in u), None)
    return {
        "official": official,
        "official_booking": OFFICIAL_HOME,
        "ctrip_check": ctrip,
        "text": text,
        "booking_notice": "官方购票仅使用 12306；携程链接只用于人工核对",
    }


def base_result(kind, args, source_mode="live"):
    orig = getattr(args, "orig", None)
    dest = getattr(args, "dest", None)
    links = (fallback_links(orig, dest, args.travel_date, args.adults)
             if orig and dest else {
                 "official": OFFICIAL_HOME,
                 "official_booking": OFFICIAL_HOME,
                 "ctrip_check": None,
                 "text": "火车·12306  " + OFFICIAL_HOME,
                 "booking_notice": "官方购票仅使用 12306",
             })
    result = {
        "schema_version": SCHEMA_VERSION,
        "kind": kind,
        "status": "unverified",
        "source": {
            "name": "12306",
            "transport": "local_stdio_mcp" if source_mode == "live" else "offline_fixture",
            "server": UPSTREAM_REPOSITORY,
            "server_version": None,
            "adapter_verified_version": UPSTREAM_VERSION,
            "adapter_verified_commit": UPSTREAM_COMMIT,
        },
        "queried_at": now_beijing() if source_mode == "live" else None,
        "timezone": TIMEZONE_NAME,
        "travel_date": args.travel_date,
        "candidates": [],
        "recommendations": {"primary": None, "backup": None},
        "links": links,
        "notice": LIVE_NOTICE if source_mode == "live" else SAMPLE_NOTICE,
        "attempts": 0,
    }
    if orig and dest:
        result["query"] = {
            "from": orig, "to": dest, "train_flags": args.train_flags,
            "earliest": args.earliest, "latest": args.latest,
        }
        result["query_key"] = "{}:{}:{}:{}:{}:{}-{}".format(
            kind, args.travel_date, orig, dest, args.train_flags,
            args.earliest, args.latest,
        )
    return result


def error_result(result, error):
    result["status"] = error.status
    result["error"] = {"class": error.error_class, "message": error.message}
    result["recommendations"] = {"primary": None, "backup": None}
    result["notice"] = ("尚未开售；预计关注日期按本地预售期配置估算，12306 公告为准"
                         if error.status == "not_on_sale" else
                         "实时结果不可用或未核验；保留人工结果并点开深链核对")
    return result


def classify_text(text):
    lower = text.lower()
    if any(x in lower for x in ("429", "rate limit", "too many requests", "限流", "频繁")):
        return QueryError("rate_limited", "12306 or MCP rate limit", "unavailable")
    if any(x in text for x in ("暂未发售", "尚未开售", "预售期", "not_on_sale")):
        return QueryError("not_on_sale", "tickets are not on sale", "not_on_sale")
    if any(x in lower for x in ("timeout", "timed out")):
        return QueryError("timeout", "MCP query timed out", "unavailable", True)
    if any(x in text for x in ("Station not found", "未检索到城市", "未检索到车站")):
        return QueryError("station_not_found", "station was not resolved", "unverified")
    if text.startswith("Error:") or "parse tickets info failed" in text:
        return QueryError("upstream_query", "MCP server could not query or parse 12306",
                          "unavailable", True)
    return None


def mcp_text(payload):
    if not isinstance(payload, dict):
        raise QueryError("malformed_response", "MCP response is not an object", "unverified")
    if payload.get("isError"):
        text = " ".join(str(c.get("text", "")) for c in payload.get("content", [])
                        if isinstance(c, dict))
        raise classify_text(text) or QueryError(
            "mcp_tool_error", "MCP tool returned an error", "unavailable", True)
    content = payload.get("content")
    if not isinstance(content, list) or not content:
        raise QueryError("empty_response", "MCP response has no content", "unavailable")
    texts = [c.get("text") for c in content
             if isinstance(c, dict) and c.get("type") == "text"]
    if len(texts) != 1 or not isinstance(texts[0], str):
        raise QueryError("malformed_response", "expected exactly one MCP text item", "unverified")
    text = texts[0].strip()
    if not text:
        raise QueryError("empty_response", "MCP response text is empty", "unavailable")
    classified = classify_text(text)
    if classified:
        raise classified
    return text


def parse_json_list(payload):
    text = mcp_text(payload)
    if (text in ("没有查询到相关车次信息", "未查询到相关车次信息。") or
            "未查到相关的列车余票" in text or "未查询到对应车次" in text):
        return []
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        raise QueryError("malformed_response", "MCP text is not valid JSON", "unverified")
    if not isinstance(value, list):
        raise QueryError("schema_mismatch", "MCP JSON payload is not a list", "unverified")
    return value


def normalize_availability(raw):
    if raw is None:
        return {"status": "unknown", "available": None, "count": None, "raw": None}
    value = str(raw).strip()
    if re.fullmatch(r"\d+", value):
        count = int(value)
        return {"status": "available" if count else "sold_out",
                "available": count > 0, "count": count, "raw": value}
    if value in ("有", "充足"):
        return {"status": "available", "available": True, "count": None, "raw": value}
    if value == "无":
        return {"status": "sold_out", "available": False, "count": 0, "raw": value}
    if value == "候补":
        return {"status": "waitlist", "available": False, "count": None, "raw": value}
    if value in ("", "--", "—", "未知", "*"):
        return {"status": "unknown", "available": None, "count": None, "raw": value}
    return {"status": "unknown", "available": None, "count": None, "raw": value}


def valid_datetime(day, clock):
    if not isinstance(day, str) or not isinstance(clock, str):
        raise ValueError("missing date/time")
    if not re.fullmatch(r"\d{2}:\d{2}", clock):
        raise ValueError("invalid clock")
    return datetime.combine(date.fromisoformat(day),
                            datetime.strptime(clock, "%H:%M").time(), BEIJING_TZ)


def normalize_ticket(raw):
    if not isinstance(raw, dict):
        raise ValueError("ticket is not an object")
    required = ("start_train_code", "start_date", "start_time", "arrive_date",
                "arrive_time", "from_station", "to_station",
                "from_station_telecode", "to_station_telecode")
    if any(not isinstance(raw.get(k), str) or not raw.get(k) for k in required):
        raise ValueError("ticket misses required fields")
    train_code = raw["start_train_code"].upper()
    if not re.fullmatch(r"[A-Z0-9]{1,12}", train_code):
        raise ValueError("train code is malformed")
    departure = valid_datetime(raw["start_date"], raw["start_time"])
    arrival = valid_datetime(raw["arrive_date"], raw["arrive_time"])
    if arrival <= departure:
        raise ValueError("arrival is not after departure")
    prices = raw.get("prices", [])
    if not isinstance(prices, list):
        raise ValueError("prices is not a list")
    seats = []
    for item in prices:
        if not isinstance(item, dict) or not isinstance(item.get("seat_name"), str):
            raise ValueError("seat is malformed")
        price = item.get("price")
        if (isinstance(price, bool) or not isinstance(price, (int, float)) or
                not math.isfinite(price) or price < 0):
            price = None
        seats.append({
            "name": item["seat_name"],
            "code": item.get("short") if isinstance(item.get("short"), str) else None,
            "price_cny": price,
            "availability": normalize_availability(item.get("num")),
        })
    known = [s["availability"] for s in seats]
    if known and all(a["available"] is False for a in known):
        availability = "sold_out"
    elif any(a["available"] is True for a in known):
        availability = "available"
    else:
        availability = "unknown"
    return {
        "train_code": train_code,
        "from": {"name": raw["from_station"], "code": raw["from_station_telecode"]},
        "to": {"name": raw["to_station"], "code": raw["to_station_telecode"]},
        "departure": {"date": raw["start_date"], "time": raw["start_time"]},
        "arrival": {"date": raw["arrive_date"], "time": raw["arrive_time"]},
        "duration": raw.get("lishi") if isinstance(raw.get("lishi"), str) else None,
        "seats": seats,
        "availability": availability,
        "_departure_dt": departure,
        "_arrival_dt": arrival,
    }


def public_candidate(candidate):
    return {k: v for k, v in candidate.items() if not k.startswith("_")}


def filter_train(candidate, flags, earliest, latest):
    code = candidate["train_code"].upper()
    if flags:
        matched = False
        for flag in flags:
            if flag == "G" and code.startswith(("G", "C")):
                matched = True
            elif flag == "D" and code.startswith("D"):
                matched = True
            elif flag in "ZTK" and code.startswith(flag):
                matched = True
            elif flag == "O" and not code.startswith(("G", "C", "D", "Z", "T", "K")):
                matched = True
            elif flag == "F" or flag == "S":
                # These flags depend on upstream dw_flag filtering; do not infer them.
                matched = True
        if not matched:
            return False
    hour = int(candidate["departure"]["time"][:2])
    return earliest <= hour < latest


def normalize_direct(payload, args):
    rows = parse_json_list(payload)
    result = base_result("direct", args)
    if not rows:
        result["status"] = "no_service"
        result["reason"] = "valid_empty_result_not_sold_out"
        return result
    candidates = []
    malformed = 0
    for raw in rows:
        try:
            candidate = normalize_ticket(raw)
            if candidate["departure"]["date"] != args.travel_date:
                raise ValueError("ticket date differs from requested travel date")
            if filter_train(candidate, args.train_flags, args.earliest, args.latest):
                candidates.append(candidate)
        except (KeyError, TypeError, ValueError):
            malformed += 1
    if not candidates:
        result["status"] = "unverified" if malformed else "no_service"
        result["reason"] = "all_rows_malformed" if malformed else "no_rows_match_filters"
        result["malformed_rows"] = malformed
        return result
    # sold_out requires affirmative unavailable evidence for every seat on every train.
    if all(c["availability"] == "sold_out" for c in candidates):
        result["status"] = "sold_out" if malformed == 0 else "unverified"
    else:
        result["status"] = "live"
    rank = {"available": 0, "unknown": 1, "sold_out": 2}
    candidates = sorted(enumerate(candidates), key=lambda p: (rank[p[1]["availability"]], p[0]))
    candidates = [c for _, c in candidates[:args.limit]]
    result["candidates"] = [public_candidate(c) for c in candidates]
    usable = [i for i, c in enumerate(candidates) if c["availability"] != "sold_out"]
    if result["status"] == "live" and usable:
        result["recommendations"] = {
            "primary": usable[0], "backup": usable[1] if len(usable) > 1 else None,
        }
    if malformed:
        result["malformed_rows"] = malformed
        result["warnings"] = ["{} malformed row(s) isolated".format(malformed)]
    return result


def minutes_between(later, earlier):
    return int((later - earlier).total_seconds() // 60)


def normalize_transfer(payload, args):
    rows = parse_json_list(payload)
    result = base_result("transfer", args)
    if not rows:
        result["status"] = "no_service"
        result["reason"] = "valid_empty_result_not_sold_out"
        return result
    candidates = []
    malformed = 0
    for raw in rows:
        try:
            if not isinstance(raw, dict) or not isinstance(raw.get("ticketList"), list):
                raise ValueError("transfer ticketList missing")
            segments = [normalize_ticket(t) for t in raw["ticketList"]]
            if len(segments) != 2:
                raise ValueError("only two-segment transfers are supported")
            first, second = segments
            if first["departure"]["date"] != args.travel_date:
                raise ValueError("transfer date differs from requested travel date")
            if first["to"]["code"] != second["from"]["code"] and raw.get("same_station") is True:
                raise ValueError("same_station conflicts with segment stations")
            if first["to"]["code"] == second["from"]["code"] and raw.get("same_station") is False:
                raise ValueError("cross_station flag conflicts with segment stations")
            buffer_minutes = minutes_between(second["_departure_dt"], first["_arrival_dt"])
            if buffer_minutes < 0:
                raise ValueError("negative transfer buffer")
            same_station = raw.get("same_station") if isinstance(raw.get("same_station"), bool) else None
            # Unknown same/cross-station relations use the more conservative buffer.
            required = args.same_station_buffer if same_station is True else args.cross_station_buffer
            buffer_ok = buffer_minutes >= required
            if any(s["availability"] == "sold_out" for s in segments):
                availability = "sold_out"
            elif all(s["availability"] == "available" for s in segments):
                availability = "available"
            else:
                availability = "unknown"
            candidates.append({
                "segments": [public_candidate(s) for s in segments],
                "transfer": {
                    "arrival_station": first["to"],
                    "departure_station": second["from"],
                    "same_station": same_station,
                    "buffer_minutes": buffer_minutes,
                    "planning_buffer_minutes": required,
                    "planning_buffer_source": "configurable_planning_default_not_official",
                    "buffer_ok": buffer_ok,
                },
                "last_arrival": public_candidate(second)["arrival"],
                "availability": availability,
            })
        except (KeyError, TypeError, ValueError):
            malformed += 1
    if not candidates:
        result["status"] = "unverified"
        result["reason"] = "all_transfer_rows_malformed"
        result["malformed_rows"] = malformed
        return result
    result["status"] = ("sold_out" if malformed == 0 and
                        all(c["availability"] == "sold_out" for c in candidates) else
                        "unverified" if all(c["availability"] == "sold_out" for c in candidates)
                        else "live")
    rank = {"available": 0, "unknown": 1, "sold_out": 2}
    candidates.sort(key=lambda c: (0 if c["transfer"]["buffer_ok"] else 1,
                                   rank[c["availability"]],
                                   c["last_arrival"]["date"], c["last_arrival"]["time"]))
    result["candidates"] = candidates[:args.limit]
    usable = [i for i, c in enumerate(result["candidates"])
              if c["transfer"]["buffer_ok"] and c["availability"] != "sold_out"]
    if result["status"] == "live" and usable:
        result["recommendations"] = {
            "primary": usable[0], "backup": usable[1] if len(usable) > 1 else None,
        }
    if malformed:
        result["malformed_rows"] = malformed
        result["warnings"] = ["{} malformed transfer row(s) isolated".format(malformed)]
    return result


def normalize_route(payload, args):
    rows = parse_json_list(payload)
    result = base_result("route", args)
    result["train_code"] = args.train
    if not rows:
        result["status"] = "no_service"
        result["reason"] = "valid_empty_route_result"
        return result
    stops = []
    for index, raw in enumerate(rows, 1):
        if not isinstance(raw, dict) or not isinstance(raw.get("station_name"), str):
            raise QueryError("schema_mismatch", "route station is malformed", "unverified")
        for field in ("arrive_time", "start_time"):
            value = raw.get(field)
            if not isinstance(value, str) or (value not in ("", "--") and
                                               not re.fullmatch(r"\d{2}:\d{2}", value)):
                raise QueryError("schema_mismatch", "route time is malformed", "unverified")
        stops.append({
            "sequence": index,
            "station_name": raw["station_name"],
            "train_code": raw.get("station_train_code") or args.train,
            "arrive_time": raw.get("arrive_time") if raw.get("arrive_time") not in ("", "--") else None,
            "start_time": raw.get("start_time") if raw.get("start_time") not in ("", "--") else None,
            "arrive_day": raw.get("arrive_day_str") or None,
            "elapsed": raw.get("lishi") or None,
        })
    result["status"] = "live"
    result["stops"] = stops
    return result


class MCPClient:
    """Small JSON-RPC 2.0 stdio client with per-request deadlines."""

    def __init__(self, command, timeout):
        self.command = command
        self.timeout = timeout
        self.process = None
        self.messages = queue.Queue()
        self.stderr_lines = []
        self.next_id = 1
        self.server_info = {}

    def __enter__(self):
        try:
            self.process = subprocess.Popen(
                self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                bufsize=1, shell=False,
            )
        except (OSError, ValueError):
            raise QueryError("mcp_start_failed", "local MCP server could not start",
                             "unavailable", True)
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        try:
            initialized = self.request("initialize", {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "dsh-trip-planner-train-live", "version": "1"},
            })
            server_info = initialized.get("serverInfo", {})
            self.server_info = server_info if isinstance(server_info, dict) else {}
            self.notify("notifications/initialized", {})
        except QueryError:
            self.close()
            raise
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def _read_stdout(self):
        try:
            for line in self.process.stdout:
                try:
                    self.messages.put(json.loads(line))
                except json.JSONDecodeError:
                    self.messages.put({"_invalid": True})
        finally:
            self.messages.put({"_eof": True})

    def _read_stderr(self):
        # Drain to prevent deadlock; never print or persist upstream stderr.
        for line in self.process.stderr:
            if len(self.stderr_lines) < 20:
                self.stderr_lines.append(line[:500])

    def _send(self, payload):
        if not self.process or self.process.poll() is not None:
            raise QueryError("mcp_closed", "local MCP server exited", "unavailable", True)
        try:
            self.process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError):
            raise QueryError("mcp_closed", "local MCP server pipe closed", "unavailable", True)

    def notify(self, method, params):
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method, params):
        request_id = self.next_id
        self.next_id += 1
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise queue.Empty
                message = self.messages.get(timeout=remaining)
                if message.get("_eof"):
                    raise QueryError("mcp_closed", "local MCP server closed stdout",
                                     "unavailable", True)
                if message.get("_invalid"):
                    raise QueryError("malformed_response", "MCP emitted invalid JSON", "unverified")
                if message.get("id") != request_id:
                    continue
                if "error" in message:
                    text = str(message["error"].get("message", "")) if isinstance(message["error"], dict) else ""
                    raise classify_text(text) or QueryError(
                        "mcp_rpc_error", "MCP JSON-RPC returned an error", "unavailable", True)
                payload = message.get("result")
                if not isinstance(payload, dict):
                    raise QueryError("malformed_response", "MCP result is missing", "unverified")
                return payload
        except queue.Empty:
            raise QueryError("timeout", "MCP query exceeded {:.1f}s".format(self.timeout),
                             "unavailable", True)

    def check_tool(self, name):
        listed = self.request("tools/list", {})
        tools = listed.get("tools")
        if not isinstance(tools, list):
            raise QueryError("schema_mismatch", "MCP tools/list is malformed", "unverified")
        tool = next((t for t in tools if isinstance(t, dict) and t.get("name") == name), None)
        if not tool:
            raise QueryError("tool_missing", "required MCP tool is unavailable", "unverified")
        schema = tool.get("inputSchema", {})
        properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
        if any(field not in properties for field in TOOL_ARGUMENTS[name]):
            raise QueryError("schema_mismatch", "MCP tool schema does not match the adapter", "unverified")

    def call(self, name, arguments):
        self.check_tool(name)
        return self.request("tools/call", {"name": name, "arguments": arguments})

    def close(self):
        if not self.process:
            return
        try:
            if self.process.stdin:
                self.process.stdin.close()
        except OSError:
            pass
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        for stream in (self.process.stdout, self.process.stderr):
            try:
                if stream:
                    stream.close()
            except OSError:
                pass


def parse_command(value):
    if not value:
        raise QueryError("mcp_not_configured",
                         "set --mcp-command or TRAIN_LIVE_MCP_COMMAND; deep links remain available",
                         "unavailable")
    try:
        command = shlex.split(value, posix=os.name != "nt")
    except ValueError:
        raise QueryError("mcp_not_configured", "MCP command cannot be parsed", "unavailable")
    if not command:
        raise QueryError("mcp_not_configured", "MCP command is empty", "unavailable")
    return command


def call_mcp(tool, arguments, args):
    payload = None
    last_error = None
    for attempt in range(args.retries + 1):
        try:
            with MCPClient(parse_command(args.mcp_command), args.timeout) as client:
                payload = client.call(tool, arguments)
                return payload, dict(client.server_info), attempt + 1
        except QueryError as error:
            last_error = error
            if not error.retryable or attempt >= args.retries:
                raise
    raise last_error


def load_fixture(path, expected_tool):
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
        error_class = raw.get("class", "upstream_query")
        status = raw.get("status", "unavailable")
        if status not in STATUSES or status in ("live", "sample"):
            status = "unavailable"
        raise QueryError(error_class, "offline fixture simulated " + error_class,
                         status, bool(raw.get("retryable")))
    sample_date = envelope.get("sample_date")
    try:
        parse_iso_date(sample_date)
    except QueryError:
        raise QueryError("fixture_invalid", "sample fixture requires sample_date", "unverified")
    return envelope.get("result"), sample_date


def tool_arguments(tool, args):
    if tool == "get-train-route-stations":
        return {"trainCode": args.train, "departDate": args.travel_date, "format": "json"}
    value = {
        "date": args.travel_date, "fromStation": args.orig, "toStation": args.dest,
        "format": "json", "trainFilterFlags": args.train_flags,
        "earliestStartTime": args.earliest, "latestStartTime": args.latest,
        "sortFlag": "startTime", "sortReverse": False,
    }
    if tool == "get-tickets":
        value["limitedNum"] = max(10, args.limit)
    else:
        value.update({"middleStation": args.middle_station, "showWZ": False,
                      "limitedNum": 10})
    return value


def execute_tool(tool, args):
    if args.fixture:
        payload, sample_date = load_fixture(args.fixture, tool)
        return payload, None, 0, sample_date
    payload, server_info, attempts = call_mcp(tool, tool_arguments(tool, args), args)
    return payload, server_info, attempts, None


def finalize_live_metadata(result, server_info, attempts):
    result["attempts"] = attempts
    if server_info:
        result["source"]["server_version"] = server_info.get("version")
        result["source"]["server_name"] = server_info.get("name")
    return result


def mark_sample(result, sample_date):
    result["status"] = "sample"
    result["queried_at"] = None
    result["sample_date"] = sample_date
    result["notice"] = SAMPLE_NOTICE + "；样本日期 " + sample_date
    result["source"]["name"] = "offline_fixture"
    result["source"]["transport"] = "offline_fixture"
    result["source"]["queried_at"] = None
    result["recommendations"] = {"primary": None, "backup": None}
    return result


def run_direct(args):
    travel = parse_iso_date(args.travel_date)
    today = datetime.now(BEIJING_TZ).date()
    result = base_result("direct", args)
    if travel < today and not args.fixture:
        return error_result(result, QueryError("past_date", "travel date is in the past", "unverified"))
    if not args.fixture and args.presale_days > 0 and travel > today + timedelta(days=args.presale_days - 1):
        result["status"] = "not_on_sale"
        result["reason"] = "outside_configured_presale_window_no_request_sent"
        result["expected_check_date"] = expected_check_date(travel, args.presale_days)
        result["presale_days"] = args.presale_days
        result["presale_notice"] = "按本地预售期配置估算，12306 公告为准"
        return result
    try:
        payload, server_info, attempts, sample_date = execute_tool("get-tickets", args)
        result = normalize_direct(payload, args)
        finalize_live_metadata(result, server_info, attempts)
        if sample_date:
            mark_sample(result, sample_date)
    except QueryError as error:
        result = error_result(result, error)
        if (error.status == "not_on_sale" and args.presale_days > 0 and
                travel > today + timedelta(days=args.presale_days - 1)):
            result["expected_check_date"] = expected_check_date(travel, args.presale_days)
            result["presale_notice"] = "按本地预售期配置估算，12306 公告为准"
    should_transfer = args.transfer == "always" or (args.transfer == "auto" and result["status"] == "sold_out")
    if should_transfer:
        transfer_args = copy.copy(args)
        transfer_args.fixture = args.transfer_fixture
        if args.fixture and not args.transfer_fixture:
            transfer = error_result(base_result("transfer", transfer_args), QueryError(
                "fixture_missing", "offline direct query needs an explicit transfer fixture",
                "unverified"))
        else:
            transfer = run_transfer(transfer_args, presale_guard=False)
        result["transfer"] = transfer
    if args.route_train:
        route_args = copy.copy(args)
        route_args.train = validate_train_code(args.route_train)
        route_args.fixture = args.route_fixture
        if args.fixture and not args.route_fixture:
            route = error_result(base_result("route", route_args), QueryError(
                "fixture_missing", "offline direct query needs an explicit route fixture",
                "unverified"))
        else:
            route = run_route(route_args, presale_guard=False)
        result["route"] = route
    return result


def run_transfer(args, presale_guard=True):
    result = base_result("transfer", args)
    try:
        payload, server_info, attempts, sample_date = execute_tool("get-interline-tickets", args)
        result = normalize_transfer(payload, args)
        finalize_live_metadata(result, server_info, attempts)
        if sample_date:
            mark_sample(result, sample_date)
    except QueryError as error:
        result = error_result(result, error)
    return result


def run_route(args, presale_guard=True):
    result = base_result("route", args)
    try:
        payload, server_info, attempts, sample_date = execute_tool("get-train-route-stations", args)
        result = normalize_route(payload, args)
        finalize_live_metadata(result, server_info, attempts)
        if sample_date:
            mark_sample(result, sample_date)
    except QueryError as error:
        result = error_result(result, error)
    return result


def selected_candidate(result):
    if not isinstance(result, dict) or result.get("status") != "live":
        return None
    recommendations = result.get("recommendations", {})
    candidates = result.get("candidates", [])
    if not isinstance(recommendations, dict) or not isinstance(candidates, list):
        return None
    index = recommendations.get("primary")
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(candidates):
        return None
    return candidates[index] if isinstance(candidates[index], dict) else None


def merge_target(result):
    """A live direct train wins; a live transfer is used only when direct is not live."""
    if not isinstance(result, dict):
        return None
    if selected_candidate(result) is not None:
        return result
    transfer = result.get("transfer")
    if isinstance(transfer, dict) and selected_candidate(transfer) is not None:
        return transfer
    return None


def select_seat(ticket, preference=None):
    seats = ticket.get("seats", [])
    if preference:
        seats = [s for s in seats if s.get("name") == preference or s.get("code") == preference]
    if not seats:
        return None
    return sorted(seats, key=lambda s: (
        0 if s["availability"]["available"] is True else 1 if s["availability"]["available"] is None else 2,
        0 if s["price_cny"] is not None else 1,
    ))[0]


def money(value):
    if value is None:
        return None
    return "¥{:.2f}".format(value).rstrip("0").rstrip(".")


def availability_text(seat):
    if not seat:
        return "余票未知"
    availability = seat["availability"]
    if availability["count"] is not None and availability["available"]:
        return "余票 {}".format(availability["count"])
    return {"available": "有票", "sold_out": "无票", "waitlist": "候补"}.get(
        availability["status"], "余票未知")


def station_relation(value):
    return "同站" if value is True else "异站" if value is False else "同/异站未知"


def candidate_summary(candidate):
    if "segments" in candidate:
        segments = candidate["segments"]
        transfer = candidate["transfer"]
        return "{} → {} · {}–{} · {} 换乘 {} 分钟 · 末段 {} {} 到".format(
            " + ".join(s["train_code"] for s in segments),
            transfer["departure_station"]["name"], segments[0]["departure"]["time"],
            segments[-1]["arrival"]["time"],
            station_relation(transfer["same_station"]),
            transfer["buffer_minutes"], candidate["last_arrival"]["date"],
            candidate["last_arrival"]["time"],
        )
    return "{} · {}→{} · {} {}–{} {}".format(
        candidate["train_code"], candidate["from"]["name"], candidate["to"]["name"],
        candidate["departure"]["date"], candidate["departure"]["time"],
        candidate["arrival"]["time"], availability_text(select_seat(candidate)),
    )


def live_leg_fields(result, seat_preference=None):
    candidate = selected_candidate(result)
    if candidate is None:
        raise ValueError("only a recommended live candidate can become a leg")
    backup_index = result["recommendations"].get("backup")
    backup = (candidate_summary(result["candidates"][backup_index])
              if isinstance(backup_index, int) else "暂无可靠备选；点开 12306 核对")
    note = "12306 live · 查询 {} · {}".format(result["queried_at"], LIVE_NOTICE)
    if result["kind"] == "transfer":
        segments = candidate["segments"]
        selected = [select_seat(s, seat_preference) for s in segments]
        prices = [s["price_cny"] if s else None for s in selected]
        price = ("合计 " + money(sum(prices)) + "（12306 live）"
                 if all(p is not None for p in prices) else "—，见 12306 深链核对")
        transfer = candidate["transfer"]
        note += " · {} · 换乘 {} 分钟（规划缓冲 {} 分钟，非官方最低时限） · 末段 {} {} 到".format(
            station_relation(transfer["same_station"]) + "换乘",
            transfer["buffer_minutes"], transfer["planning_buffer_minutes"],
            candidate["last_arrival"]["date"], candidate["last_arrival"]["time"],
        )
        note += " · " + " / ".join(availability_text(s) for s in selected)
        return {
            "type": "rail (transfer)", "date": result["travel_date"],
            "carrier": " → ".join(s["train_code"] for s in segments),
            "from": segments[0]["from"]["name"], "to": segments[-1]["to"]["name"],
            "dep": segments[0]["departure"]["time"], "arr": segments[-1]["arrival"]["time"],
            "price": price, "bags": "铁路行李按 12306 超规与托运规则核验",
            "link": result["links"]["official"], "note": note, "backup": backup,
            "refund_change": "按 12306 官方车次、席别与退改规则核验",
        }
    seat = select_seat(candidate, seat_preference)
    price = ("{} {}（12306 live）".format(seat["name"], money(seat["price_cny"]))
             if seat and seat["price_cny"] is not None else "—，见 12306 深链核对")
    note += " · {} {}".format(seat["name"] if seat else "席别", availability_text(seat))
    return {
        "type": "high-speed rail" if candidate["train_code"].startswith(("G", "D", "C")) else "rail",
        "date": candidate["departure"]["date"], "carrier": candidate["train_code"],
        "from": candidate["from"]["name"], "to": candidate["to"]["name"],
        "dep": candidate["departure"]["time"], "arr": candidate["arrival"]["time"],
        "price": price, "bags": "铁路行李按 12306 超规与托运规则核验",
        "link": result["links"]["official"], "note": note, "backup": backup,
        "refund_change": "按 12306 官方车次、席别与退改规则核验",
    }


def existing_priority(row):
    live = row.get("rail_live")
    if isinstance(live, dict):
        return STATUS_PRIORITY.get(live.get("status"), 0)
    return STATUS_PRIORITY["deep_link"] if row.get("link") else STATUS_PRIORITY["unverified"]


def merge_plan(plan, response, seat_preference=None):
    """Return (new_plan, metadata); input plan is never mutated."""
    updated = copy.deepcopy(plan)
    info = {"written": False, "reason": "no_recommended_live_candidate"}
    target = merge_target(response)
    if target is None:
        return updated, info
    if not isinstance(updated, dict) or not isinstance(updated.get("legs", []), list):
        info["reason"] = "invalid_plan_legs"
        return updated, info
    legs = updated.setdefault("legs", [])
    key = target.get("query_key")
    managed = next((r for r in legs if isinstance(r, dict) and
                    isinstance(r.get("rail_live"), dict) and
                    r["rail_live"].get("query_key") == key and
                    r.get("_train_live_managed") is True), None)
    manual = next((r for r in legs if isinstance(r, dict) and
                   r.get("date") == target["travel_date"] and
                   r.get("_train_live_managed") is not True and
                   r.get("from") in (target["query"]["from"],) and
                   r.get("to") in (target["query"]["to"],)), None)
    if ((managed is not None and existing_priority(managed) > STATUS_PRIORITY["live"]) or
            (manual is not None and existing_priority(manual) > STATUS_PRIORITY["live"])):
        info["reason"] = "higher_priority_result_kept"
        return updated, info
    fields = live_leg_fields(target, seat_preference)
    if managed is not None:
        managed.update(fields)
        managed["rail_live"] = copy.deepcopy(target)
        info.update({"written": True, "reason": "updated_managed_leg",
                     "leg_index": legs.index(managed)})
    elif manual is not None:
        # Preserve every human-entered legacy field; only add the optional enhancement.
        manual["rail_live"] = copy.deepcopy(target)
        info.update({"written": True, "reason": "augmented_manual_leg_without_field_changes",
                     "leg_index": legs.index(manual)})
    else:
        row = dict(fields)
        row["_train_live_managed"] = True
        row["rail_live"] = copy.deepcopy(target)
        legs.append(row)
        info.update({"written": True, "reason": "appended_live_leg", "leg_index": len(legs) - 1})
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


def response_to_text(result):
    lines = ["status: {} · {} · {}".format(result["status"], result["kind"], result["travel_date"])]
    if result.get("sample_date"):
        lines.append("样本日期: {}（非实时）".format(result["sample_date"]))
    if result.get("expected_check_date"):
        lines.append("预计关注日期: {}（12306 公告为准）".format(result["expected_check_date"]))
    if result.get("error"):
        lines.append("error: {} · {}".format(result["error"]["class"], result["error"]["message"]))
    show_values = result["status"] in ("live", "sample")
    if show_values:
        for candidate in result.get("candidates", []):
            lines.append("- " + candidate_summary(candidate))
            tickets = candidate.get("segments", [candidate])
            for ticket in tickets:
                for seat in ticket.get("seats", []):
                    price = money(seat["price_cny"]) if seat["price_cny"] is not None else "票价未知"
                    lines.append("  {} · {} · {}".format(ticket["train_code"], seat["name"], price)
                                 + " · " + availability_text(seat))
        for stop in result.get("stops", []):
            lines.append("- {} · {} 到 / {} 发 · {}".format(
                stop["station_name"], stop["arrive_time"] or "—",
                stop["start_time"] or "—", stop["arrive_day"] or "日期未知"))
    lines.append(result.get("notice", LIVE_NOTICE))
    lines.append(result["links"]["text"])
    if isinstance(result.get("transfer"), dict):
        lines.append("transfer status: " + result["transfer"]["status"])
        if result["transfer"]["status"] in ("live", "sample"):
            for candidate in result["transfer"].get("candidates", []):
                lines.append("- transfer " + candidate_summary(candidate))
    if isinstance(result.get("route"), dict):
        lines.append("route status: " + result["route"]["status"])
    return "\n".join(lines)


def add_common_arguments(parser, rail=True):
    parser.add_argument("--date", dest="travel_date", required=True, type=argparse_date,
                        help="YYYY-MM-DD，北京时间")
    parser.add_argument("--mcp-command", default=os.environ.get("TRAIN_LIVE_MCP_COMMAND"),
                        help="本地 stdio MCP 启动命令；不默认使用 npx 联网安装")
    parser.add_argument("--timeout", type=float, default=20.0, help="每个 MCP 请求超时秒数")
    parser.add_argument("--retries", type=int, default=1, choices=range(0, 3), help="网络/超时有限重试")
    parser.add_argument("--fixture", help="显式离线 MCP fixture；有效数据强制标为 sample")
    parser.add_argument("--output", choices=("json", "text"), default="json")
    if rail:
        parser.add_argument("--from", dest="orig", required=True, type=argparse_station,
                            help="具体出发站或电报码，不合并同城车站")
        parser.add_argument("--to", dest="dest", required=True, type=argparse_station)
        parser.add_argument("--train-flags", default="", type=train_flags,
                            help="上游车型筛选标志 [GDZTKOFS]{0,8}，默认不限")
        parser.add_argument("--earliest", type=int, default=0, choices=range(0, 24))
        parser.add_argument("--latest", type=int, default=24, choices=range(1, 25))
        parser.add_argument("--limit", type=int, default=3, choices=range(1, 6))
        parser.add_argument("--adults", type=int, default=1, choices=range(1, 10), help="仅用于深链人数")
        parser.add_argument("--seat", help="旧 legs.price 展示的偏好席别；缺失保持未知")
        parser.add_argument("--presale-days", type=int, default=15, choices=range(0, 31),
                            help="本地预售期 guard；0 禁用，官方公告为准")
        parser.add_argument("--middle-station", default="", help="可选中转站，仅在查询中转时使用")
        parser.add_argument("--same-station-buffer", type=int, default=45,
                            choices=range(0, 721), help="同站规划缓冲分钟，非官方最低时限")
        parser.add_argument("--cross-station-buffer", type=int, default=120,
                            choices=range(0, 721), help="异站或关系未知时的保守规划缓冲分钟")


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    direct = sub.add_parser("direct", help="直达查询；auto 仅在直达确认无票时查中转")
    add_common_arguments(direct)
    direct.add_argument("--transfer", choices=("auto", "always", "never"), default="auto")
    direct.add_argument("--transfer-fixture", help="中转离线 fixture（仍需 --fixture 提供直达样本）")
    direct.add_argument("--route-train", type=argparse_train,
                        help="按需同时查询经停，单点失败不影响直达")
    direct.add_argument("--route-fixture", help="经停离线 fixture")
    direct.add_argument("--plan", help="可选 plan.geo.json；只有 live 可增量合并")
    direct.add_argument("--write", action="store_true", help="原子写回 --plan；非 live 不写")
    route = sub.add_parser("route", help="按需经停查询")
    add_common_arguments(route, rail=False)
    route.add_argument("--train", required=True, type=argparse_train)
    merge = sub.add_parser("merge", help="合并已保存的规范化 JSON 响应；不访问网络")
    merge.add_argument("--plan", required=True)
    merge.add_argument("--response", required=True)
    merge.add_argument("--write", action="store_true")
    merge.add_argument("--seat")
    return parser


def emit(result, output, plan_info=None):
    if plan_info is not None:
        result["plan_merge"] = plan_info
    if output == "text":
        print(response_to_text(result))
        if plan_info is not None:
            print("plan merge: " + json.dumps(plan_info, ensure_ascii=False))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command == "merge":
        try:
            response = json.loads(Path(args.response).read_text(encoding="utf-8"))
            original_text = Path(args.plan).read_text(encoding="utf-8")
            plan = json.loads(original_text)
            updated, info = merge_plan(plan, response, args.seat)
            if args.write and info["written"]:
                atomic_write_plan(args.plan, updated)
            elif args.write:
                info["reason"] += "_plan_unchanged"
        except (OSError, json.JSONDecodeError, ValueError, KeyError, TypeError, AttributeError) as error:
            print(json.dumps({"status": "unverified", "error": {
                "class": "merge_failed", "message": str(error),
            }}, ensure_ascii=False, indent=2))
            return 2
        emit(updated if args.write else response, "json", info)
        return 0
    if hasattr(args, "earliest") and args.earliest >= args.latest:
        build_parser().error("--earliest must be earlier than --latest")
    if args.timeout <= 0 or args.timeout > 120:
        build_parser().error("--timeout must be > 0 and <= 120")
    if args.command == "route":
        result = run_route(args)
        emit(result, args.output)
        return 0
    result = run_direct(args)
    target = merge_target(result)
    plan_info = None
    if args.plan:
        try:
            plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
            updated, plan_info = merge_plan(plan, target or result, args.seat)
            if args.write and plan_info["written"]:
                atomic_write_plan(args.plan, updated)
            elif args.write:
                plan_info["reason"] += "_plan_unchanged"
        except (OSError, json.JSONDecodeError, ValueError, KeyError, TypeError, AttributeError) as error:
            plan_info = {"written": False, "reason": "merge_failed", "message": str(error)}
            emit(result, args.output, plan_info)
            return 2
    emit(result, args.output, plan_info)
    return 0


if __name__ == "__main__":
    sys.exit(main())
