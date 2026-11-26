#!/usr/bin/env python3
"""Optional 12306 live rail queries over the public web endpoints (direct HTTPS).

No third-party Python packages, credentials, login, booking, or passenger
information are used: the transport talks to the same anonymous public query
endpoints the 12306 web app uses (session cookie + hidden query path only).
When the network is unavailable, times out, or rate-limits, the result is
``unavailable`` plus the existing 12306/Ctrip deep links.

Transport lineage: a clean-room reimplementation of the endpoint handling in
php737/train-12306 (MIT, verified 2026-11-14), which itself is an independent
reimplementation inspired by Joooook/12306-mcp. No upstream source is copied.

Examples:
  python3 scripts/train_live.py direct --from 北京南 --to 上海虹桥 --date 2026-12-01
  python3 scripts/train_live.py direct --from 北京南 --to 上海虹桥 --date 2026-12-01 --transfer auto
  python3 scripts/train_live.py route --train G1 --date 2026-12-01
  python3 scripts/train_live.py direct --from 北京南 --to 上海虹桥 --date 2026-08-15 \
      --fixture tests/fixtures/train_live/direct-live.json

Only a schema-valid ``live`` result can be merged into plan.legs[]. Fixtures are
always ``sample`` and can never overwrite a plan. See references/train-live.md.
"""

import argparse
import copy
import json
import math
import os
import re
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from flight_scan import rail_links

import trip_config

SCHEMA_VERSION = 1
UPSTREAM_REPOSITORY = "php737/train-12306"
UPSTREAM_VERSION = "1.2.0"
UPSTREAM_COMMIT = "2026-11-14"
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

API_BASE = "https://kyfw.12306.cn"
WEB_URL = "https://www.12306.cn/index/"
SEARCH_API_BASE = "https://search.12306.cn"
LEFT_TICKET_INIT = API_BASE + "/otn/leftTicket/init"
LCQUERY_INIT = API_BASE + "/otn/lcQuery/init"
STATION_JS_FALLBACK = API_BASE + "/otn/resources/js/framework/station_name.js"
TRAIN_ROUTE_PATH = "/otn/queryTrainInfo/query"
# Hidden query paths must be discovered from the init pages; these fallbacks
# were re-verified against the live site on 2026-11-25 (train-12306 1.2.0).
FALLBACK_PATHS = {
    "ticket_path": "/otn/leftTicket/queryG",
    "transfer_path": "/lcquery/queryG",
}
# /otn/ prefix rules differ per endpoint; never "normalize" them into one form:
# a /otn/ prefix on the transfer path redirects to the passport login page.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
BASE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": LEFT_TICKET_INIT,
    "X-Requested-With": "XMLHttpRequest",
}
STATION_KEYS = ("station_id", "station_name", "station_code", "station_pinyin",
                "station_short", "station_index", "code", "city", "r1", "r2")
SEAT_TYPES = {
    "9": ("商务座", "swz"), "P": ("特等座", "tz"), "M": ("一等座", "zy"),
    "D": ("优选一等座", "zy"), "O": ("二等座", "ze"), "S": ("二等包座", "ze"),
    "6": ("高级软卧", "gr"), "A": ("高级动卧", "gr"), "4": ("软卧", "rw"),
    "I": ("一等卧", "rw"), "F": ("动卧", "rw"), "3": ("硬卧", "yw"),
    "J": ("二等卧", "yw"), "2": ("软座", "rz"), "1": ("硬座", "yz"),
    "W": ("无座", "wz"), "WZ": ("无座", "wz"), "H": ("其他", "qt"),
}
# yp_info_new fixed-width chunks: 10 chars per seat.
PRICE_CHUNK_LEN = 10
DISCOUNT_CHUNK_LEN = 5
# Result-array positional fields (47+ pipe-separated columns; position-sensitive).
TICKET_FIELD_INDEX = {
    "train_no": 2, "station_train_code": 3, "from_station_telecode": 6,
    "to_station_telecode": 7, "start_time": 8, "arrive_time": 9,
    "lishi": 10, "canWebBuy": 11, "yp_info_new": 39, "dw_flag": 46,
    "start_train_date": 13, "seat_discount_info": 54,
}
JUNK_LISHI = ("99:59", "24:00", "--:--")
MAX_DURATION_MIN = 48 * 60
STATION_CACHE_HOURS = 7 * 24
PATHS_CACHE_HOURS = 7 * 24
MISSING_STATIONS = [
    {"station_id": "@cdd", "station_name": "成都东", "station_code": "WEI",
     "station_pinyin": "chengdudong", "station_short": "cdd",
     "station_index": "", "code": "1707", "city": "成都", "r1": "", "r2": ""},
]


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
            "transport": "direct_https" if source_mode == "live" else "offline_fixture",
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
    lower = (text or "").lower()
    if any(x in lower for x in ("429", "rate limit", "too many requests", "限流", "频繁")):
        return QueryError("rate_limited", "12306 rate limit", "unavailable")
    if any(x in text for x in ("暂未发售", "尚未开售", "预售期", "not_on_sale")):
        return QueryError("not_on_sale", "tickets are not on sale", "not_on_sale")
    if any(x in lower for x in ("timeout", "timed out")):
        return QueryError("timeout", "query timed out", "unavailable", True)
    if any(x in text for x in ("Station not found", "未检索到城市", "未检索到车站")):
        return QueryError("station_not_found", "station was not resolved", "unverified")
    if text.startswith("Error:") or "parse tickets info failed" in text:
        return QueryError("upstream_query", "12306 query could not be parsed",
                          "unavailable", True)
    return None


# ---------------------------------------------------------------------------
# Direct HTTPS transport (no MCP, no subprocess, no third-party packages)
# ---------------------------------------------------------------------------


class Rail12306Http:
    """Anonymous session against the public 12306 web endpoints.

    Bootstrap visits the two init pages first (cookie + hidden query paths);
    bare queries without an active session get 302'd or return empty.
    """

    def __init__(self, timeout=15.0, cache_dir=None, verbose=False):
        self.timeout = timeout
        self.verbose = verbose
        self.cache_dir = Path(cache_dir) if cache_dir else self._default_cache_dir()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ssl.create_default_context()))
        self.cookies = {}
        self.paths = {}
        self._stations = None

    @staticmethod
    def _default_cache_dir():
        base = trip_config.get("trainLiveCacheDir", "").strip()
        if base:
            return Path(base).expanduser()
        return Path(tempfile.gettempdir()) / "dsh-trip-planner-12306"

    def _log(self, message):
        if self.verbose:
            print("[12306] " + message, file=sys.stderr)

    # ── low-level HTTP ──

    def _request(self, url, params=None, headers=None, tries=3):
        query = ("?" + urllib.parse.urlencode(params)) if params else ""
        last = None
        for attempt in range(tries):
            merged = dict(BASE_HEADERS)
            if self.cookies:
                merged["Cookie"] = "; ".join(
                    "{}={}".format(k, v) for k, v in self.cookies.items())
            merged.update(headers or {})
            request = urllib.request.Request(url + query, headers=merged)
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    for value in response.headers.get_all("Set-Cookie") or []:
                        first = value.split(";")[0].strip()
                        if "=" in first:
                            name, _, val = first.partition("=")
                            self.cookies[name.strip()] = val
                    return response.read().decode("utf-8", errors="replace")
            except urllib.error.HTTPError as error:
                last = error
                if error.code == 429:
                    raise QueryError("rate_limited", "12306 rate limit", "unavailable")
                self._log("HTTP {} on {} (attempt {}/{})".format(
                    error.code, url, attempt + 1, tries))
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                last = error
                self._log("network error on {} (attempt {}/{}): {}".format(
                    url, attempt + 1, tries, error.__class__.__name__))
            if attempt + 1 < tries:
                time.sleep(1.2 * (attempt + 1))
        raise QueryError("network_error", "12306 request failed: {}".format(
            last.__class__.__name__), "unavailable", True)

    # ── bootstrap: cookie + hidden query paths ──

    def bootstrap(self):
        if self.paths:
            return
        cached = self._read_cache("paths.json", PATHS_CACHE_HOURS)
        paths = dict(cached) if cached else {}
        for url in (LEFT_TICKET_INIT, LCQUERY_INIT):
            try:
                self._request(url)
            except QueryError as error:
                raise QueryError("network_error",
                                 "cannot reach 12306: " + error.error_class,
                                 "unavailable", True)
        if "ticket_path" not in paths:
            html = self._request(LEFT_TICKET_INIT)
            match = re.search(r"var CLeftTicketUrl = '(.+?)'", html)
            paths["ticket_path"] = self._norm_ticket_path(
                match.group(1) if match else None)
        if "transfer_path" not in paths:
            html = self._request(LCQUERY_INIT)
            match = re.search(r"var lc_search_url = '(.+?)'", html)
            paths["transfer_path"] = self._norm_transfer_path(
                match.group(1) if match else None)
        self.paths = paths
        self._write_cache("paths.json", paths)
        self._log("paths: " + json.dumps(paths))

    @staticmethod
    def _norm_ticket_path(value):
        value = (value or "").strip()
        if not value:
            return FALLBACK_PATHS["ticket_path"]
        if not value.startswith("/"):
            value = "/" + value
        if not value.startswith("/otn/"):
            value = "/otn" + value
        return value

    @staticmethod
    def _norm_transfer_path(value):
        value = (value or "").strip()
        if not value:
            return FALLBACK_PATHS["transfer_path"]
        return value if value.startswith("/") else "/" + value

    # ── cache helpers ──

    def _read_cache(self, name, ttl_hours):
        path = self.cache_dir / name
        try:
            if time.time() - path.stat().st_mtime < ttl_hours * 3600:
                return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
        return None

    def _write_cache(self, name, value):
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            (self.cache_dir / name).write_text(
                json.dumps(value, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    # ── station table ──

    def stations(self):
        if self._stations is not None:
            return self._stations
        cached = self._read_cache("stations.json", STATION_CACHE_HOURS)
        if cached:
            self._stations = cached
            return self._stations
        raw = None
        try:
            text = self._request(STATION_JS_FALLBACK)
            if "station_names" in text:
                raw = text
        except QueryError as error:
            self._log("station_name.js failed: " + error.error_class)
        if raw is None:
            html = self._request(WEB_URL, headers={"Referer": WEB_URL})
            match = re.search(r"(/script/core/common/station_name.+?\.js)", html)
            if not match:
                raise QueryError("schema_mismatch",
                                 "cannot locate station_name.js", "unverified")
            raw = self._request(API_BASE + match.group(1))
        table = parse_station_js(raw)
        self._stations = table
        self._write_cache("stations.json", table)
        return table

    def resolve_station(self, value):
        """Chinese name / city name / telecode → telecode, or None."""
        key = (value or "").strip()
        if key.endswith("站"):
            key = key[:-1]
        table = self.stations()
        if re.fullmatch(r"[A-Z]{2,4}", key) and key in table:
            return key
        for station in table.values():
            if station.get("station_name") == key:
                return station["station_code"]
        for station in table.values():
            if station.get("city") == key:
                return station["station_code"]
        return None

    def station_name(self, telecode):
        return self.stations().get(telecode, {}).get("station_name", telecode)

    # ── query endpoints ──

    def query_json(self, url, params):
        text = self._request(url, params=params)
        if text.lstrip().startswith("<"):
            raise QueryError("redirected_to_login",
                             "12306 returned HTML; query path may have changed",
                             "unavailable", True)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            raise QueryError("malformed_response", "12306 returned invalid JSON",
                             "unverified")


def parse_station_js(raw):
    body = raw.replace("var station_names ='", "").replace("';", "").strip()
    flat = body.split("|")
    table = {}
    for index in range(len(flat) // 10):
        group = flat[index * 10: index * 10 + 10]
        station = dict(zip(STATION_KEYS, group))
        if station.get("station_code"):
            table[station["station_code"]] = station
    for station in MISSING_STATIONS:
        table.setdefault(station["station_code"], station)
    return table


def rail_client(args):
    return Rail12306Http(timeout=args.timeout, cache_dir=args.cache_dir,
                         verbose=args.verbose)


def http_get_with_retry(tool_call, args):
    """Unified retry wrapper; mirrors the old call_mcp retry semantics."""
    last_error = None
    for attempt in range(args.retries + 1):
        try:
            return tool_call(), attempt + 1
        except QueryError as error:
            last_error = error
            if not error.retryable or attempt >= args.retries:
                raise
    raise last_error


def fetch_direct(args):
    client = rail_client(args)
    client.bootstrap()
    origin = client.resolve_station(args.orig)
    destination = client.resolve_station(args.dest)
    if not origin or not destination:
        raise QueryError("station_not_found",
                         "station was not resolved: {}→{} {}".format(
                             args.orig, origin, args.dest, destination),
                         "unverified")
    data = client.query_json(API_BASE + client.paths["ticket_path"], {
        "leftTicketDTO.train_date": args.travel_date,
        "leftTicketDTO.from_station": origin,
        "leftTicketDTO.to_station": destination,
        "purpose_codes": "ADULT",
    })
    if not data.get("status"):
        messages = data.get("messages")
        raise classify_text(json.dumps(messages, ensure_ascii=False)) or QueryError(
            "upstream_query", "12306 rejected the query", "unavailable", True)
    rows = (data.get("data") or {}).get("result") or []
    return [parse_ticket_row(row, client) for row in rows]


def fetch_transfer(args):
    client = rail_client(args)
    client.bootstrap()
    origin = client.resolve_station(args.orig)
    destination = client.resolve_station(args.dest)
    middle = client.resolve_station(args.middle_station) if args.middle_station else ""
    if not origin or not destination or (args.middle_station and not middle):
        raise QueryError("station_not_found", "station was not resolved", "unverified")
    params = {
        "train_date": args.travel_date,
        "from_station_telecode": origin,
        "to_station_telecode": destination,
        "middle_station": middle or "",
        "result_index": "0",
        "can_query": "Y",
        "isShowWZ": "Y",
        "purpose_codes": "00",
        "channel": "E",
    }
    plans = []
    for _ in range(10):
        data = client.query_json(API_BASE + client.paths["transfer_path"], params)
        payload = data.get("data")
        if isinstance(payload, str):
            text = json.dumps(data, ensure_ascii=False)
            raise classify_text(text) or QueryError(
                "upstream_query", "12306 transfer query rejected", "unavailable", True)
        plans += payload.get("middleList") or []
        if payload.get("can_query") == "N":
            break
        params["result_index"] = str(payload.get("result_index", 0))
    return plans


def fetch_route(args):
    client = rail_client(args)
    client.bootstrap()
    train_no = resolve_train_no(client, args.train, args.travel_date)
    data = client.query_json(API_BASE + TRAIN_ROUTE_PATH, {
        "leftTicketDTO.train_no": train_no,
        "leftTicketDTO.train_date": args.travel_date,
        "rand_code": "",
    })
    return (data.get("data") or {}).get("data") or []


def resolve_train_no(client, train_code, travel_date):
    """Display train code → internal train_no via the search endpoint."""
    search = client.query_json(SEARCH_API_BASE + "/search/v1/train/search", {
        "keyword": train_code, "date": travel_date.replace("-", "")})
    rows = search.get("data") or []
    for row in rows:
        if isinstance(row, dict) and (row.get("station_train_code") or "").upper() == train_code:
            return row["train_no"]
    raise QueryError("no_service", "train code was not found on that date",
                     "no_service")


def parse_ticket_row(raw, client):
    """Pipe-separated 12306 result row → adapter ticket dict (raises on junk)."""
    values = raw.split("|")
    fields = {key: values[index] if index < len(values) else ""
              for key, index in TICKET_FIELD_INDEX.items()}
    if fields["lishi"] in JUNK_LISHI:
        raise ValueError("placeholder junk row")
    if fields["start_time"] == fields["arrive_time"]:
        raise ValueError("placeholder junk row")
    try:
        hours, minutes = (int(x) for x in fields["lishi"].split(":"))
        if hours * 60 + minutes > MAX_DURATION_MIN:
            raise ValueError("implausible duration")
    except ValueError:
        raise ValueError("malformed duration")
    base = datetime.strptime(fields["start_train_date"], "%Y%m%d")
    departure_clock = datetime.strptime(fields["start_time"], "%H:%M")
    if departure_clock.hour >= 24:
        base += timedelta(days=departure_clock.hour // 24)
        departure_clock = departure_clock.replace(
            hour=departure_clock.hour % 24)
    departure = base.replace(hour=departure_clock.hour,
                             minute=departure_clock.minute, tzinfo=BEIJING_TZ)
    arrival = departure + timedelta(hours=hours, minutes=minutes)
    start_date = departure.strftime("%Y-%m-%d")
    start_time = departure.strftime("%H:%M")
    arrive_date = arrival.strftime("%Y-%m-%d")
    arrive_time = fields["arrive_time"]
    return {
        "start_train_code": fields["station_train_code"],
        "start_date": start_date,
        "start_time": start_time,
        "arrive_date": arrive_date,
        "arrive_time": arrive_time,
        "lishi": fields["lishi"],
        "from_station": client.station_name(fields["from_station_telecode"]),
        "to_station": client.station_name(fields["to_station_telecode"]),
        "from_station_telecode": fields["from_station_telecode"],
        "to_station_telecode": fields["to_station_telecode"],
        "prices": extract_prices(fields["yp_info_new"],
                                 fields["seat_discount_info"]),
        "dw_flag": fields["dw_flag"],
    }


def parse_transfer_plan(raw, client):
    """12306 middleList entry → two MCP-shaped ticket dicts (adapter format)."""
    legs = raw.get("fullList") or []
    if len(legs) != 2:
        raise ValueError("transfer plan must have exactly two legs")
    tickets = []
    for leg in legs:
        start_date = ""
        try:
            base = datetime.strptime(leg.get("start_train_date", ""), "%Y%m%d")
            clock = datetime.strptime(leg.get("start_time", ""), "%H:%M")
            if clock.hour >= 24:
                base += timedelta(days=clock.hour // 24)
                clock = clock.replace(hour=clock.hour % 24)
            start_date = base.strftime("%Y-%m-%d")
            hours, minutes = (int(x) for x in (leg.get("lishi") or "0:0").split(":"))
            arrival = base.replace(hour=clock.hour, minute=clock.minute) + \
                timedelta(hours=hours, minutes=minutes)
            arrive_date = arrival.strftime("%Y-%m-%d")
        except (ValueError, KeyError):
            raise ValueError("transfer leg has malformed dates")
        tickets.append({
            "start_train_code": leg.get("station_train_code", ""),
            "start_date": start_date,
            "start_time": leg.get("start_time", ""),
            "arrive_date": arrive_date,
            "arrive_time": leg.get("arrive_time", ""),
            "lishi": leg.get("lishi", ""),
            "from_station": leg.get("from_station_name", ""),
            "to_station": leg.get("to_station_name", ""),
            "from_station_telecode": leg.get("from_station_telecode", ""),
            "to_station_telecode": leg.get("to_station_telecode", ""),
            "prices": extract_prices(leg.get("yp_info"),
                                     leg.get("seat_discount_info")),
            "dw_flag": leg.get("dw_flag", ""),
        })
    return {
        "ticketList": tickets,
        "same_station": raw.get("same_station") == "0",
        "wait_time": raw.get("wait_time", ""),
        "middle_station": raw.get("middle_station_name", ""),
    }


def extract_prices(yp_info, seat_discount_info):
    """yp_info_new fixed-width chunks → MCP-shaped price list."""
    discounts = {}
    text = seat_discount_info or ""
    for index in range(len(text) // DISCOUNT_CHUNK_LEN):
        chunk = text[index * DISCOUNT_CHUNK_LEN:(index + 1) * DISCOUNT_CHUNK_LEN]
        try:
            discounts[chunk[0]] = int(chunk[1:])
        except ValueError:
            pass
    prices = []
    text = yp_info or ""
    for index in range(len(text) // PRICE_CHUNK_LEN):
        chunk = text[index * PRICE_CHUNK_LEN:(index + 1) * PRICE_CHUNK_LEN]
        try:
            total = int(chunk[6:10])
        except ValueError:
            continue
        if total >= 3000:
            code = "W"
        elif chunk[0] not in SEAT_TYPES:
            code = "H"
        else:
            code = chunk[0]
        name, short = SEAT_TYPES.get(code, ("其他", "qt"))
        try:
            price = int(chunk[1:6]) / 10
        except ValueError:
            continue
        num = extract_seat_num(short, chunk)
        prices.append({"seat_name": name, "short": short,
                       "num": num, "price": price, "discount": discounts.get(code)})
    return prices


def extract_seat_num(short, chunk):
    """Seat count uses the raw string so 有/无/候补 survive normalization."""
    return chunk[6:10].lstrip("0") or "0"


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


def dumps_rows(rows):
    """Serialize ticket rows as the JSON text a fixture payload would carry."""
    return json.dumps(rows, ensure_ascii=False)


def execute_tool(tool, args):
    """Returns (ticket_rows, transport_metadata, attempts, sample_date).

    With --fixture, rows come straight from the stored fixture payload.
    Live mode maps the HTTP payloads into the same row shape so every
    downstream normalizer keeps working unchanged.
    """
    if args.fixture:
        payload, sample_date = load_fixture(args.fixture, tool)
        return payload, None, 0, sample_date
    if tool == "get-tickets":
        rows, attempts = http_get_with_retry(lambda: fetch_direct(args), args)
    elif tool == "get-interline-tickets":
        plans, attempts = http_get_with_retry(lambda: fetch_transfer(args), args)
        client = rail_client(args)
        rows = []
        for plan in plans:
            try:
                rows.append(parse_transfer_plan(plan, client))
            except (ValueError, KeyError, TypeError):
                continue
    elif tool == "get-train-route-stations":
        rows, attempts = http_get_with_retry(lambda: fetch_route(args), args)
    else:
        raise QueryError("tool_missing", "unknown tool", "unverified")
    payload = {"content": [{"type": "text", "text": dumps_rows(rows)}]}
    info = {"name": UPSTREAM_REPOSITORY, "version": UPSTREAM_VERSION}
    return payload, info, attempts, None


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
        if malformed:
            result["warnings"] = ["{} malformed row(s) isolated".format(malformed)]
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
            # Live data uses "----" (and "--") for the no-show direction on
            # first/last stops; fixtures historically use "" / "--".
            if not isinstance(value, str) or (value not in ("", "--", "----") and
                                               not re.fullmatch(r"\d{2}:\d{2}", value)):
                raise QueryError("schema_mismatch", "route time is malformed", "unverified")
        stops.append({
            "sequence": index,
            "station_name": raw["station_name"],
            "train_code": raw.get("station_train_code") or args.train,
            "arrive_time": raw.get("arrive_time") if raw.get("arrive_time") not in ("", "--", "----") else None,
            "start_time": raw.get("start_time") if raw.get("start_time") not in ("", "--", "----") else None,
            "arrive_day": raw.get("arrive_day_str") or None,
            "elapsed": raw.get("running_time") or raw.get("lishi") or None,
        })
    result["status"] = "live"
    result["stops"] = stops
    return result


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
    parser.add_argument("--timeout", type=float, default=15.0, help="每个 HTTP 请求超时秒数")
    parser.add_argument("--retries", type=int, default=1, choices=range(0, 3), help="网络/超时有限重试")
    parser.add_argument("--fixture", help="显式离线 fixture；有效数据强制标为 sample")
    parser.add_argument("--output", choices=("json", "text"), default="json")
    parser.add_argument("--verbose", action="store_true", help="stderr 输出 HTTP 调试日志")
    parser.add_argument("--cache-dir", dest="cache_dir", default=None,
                        help="车站表/查询路径缓存目录（默认临时目录，可用设置里的 "
                             "trainLiveCacheDir 覆盖）")
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
