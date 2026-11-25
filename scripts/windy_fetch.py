#!/usr/bin/env python3
"""Windy free-data fetcher for the trip-planner skill (stdlib only, Python 3.9+).

A keyless, loginless weather source reverse-engineered from the Windy Android
client (51.3.2) — full endpoint catalogue & compliance notes: docs/windy.md. All data comes from https://node.windy.com JSON
endpoints; no map tiles are fetched (the skill already uses 高德/百度 for maps).

Endpoints used (verified 2026-11-23, see plan.md §3):
  /metadata/v1.0/forecast/{model}/minifest.json     model run "ref" time
  /forecast/point/{model}/v3.0/{lat}/{lon}          point forecast (needs refTime)
  /forecast/point/now/{model}/v1.0/{lat}/{lon}      current conditions
  /forecast/airq/{model}/v3.0/{lat}/{lon}           air quality (cams-global)
  /forecast/archive/{model}/v3.0/{lat}/{lon}        ~5 days of past hourly values
  /capalerts/{lat}/{lon}?detailed=true              CAP weather warnings (204 = none)
  /tc/v2/storms                                     active tropical cyclones
  /services/elevation/{lat}/{lon}                   single-point elevation (m)
  /rplanner/v1/elevation/{lat,lon;lat,lon;...}      route elevation profile
  /rplanner/v1/forecast/{mode}/{lat,lon;...}?dst=YYYY/MM/DD/HH
                                                    weather ALONG a route
                                                    (mode: car|boat|vfr|ifr)
  /pois/v2/stations/{lat}/{lon}                     nearby stations w/ live obs
  /obs/measurement/v3/{source}/{id}/{n}/1           station obs history
                                                    (source: ad|radiosonde|airq|
                                                     pws|wmo; 204 = no data)
  /search/v4.1/{lat}/{lon}/{query}?lang=&size=&types=...
                                                    place/station search
                                                    (size >= 10, server-enforced;
                                                     types: airport | station,
                                                     radiosonde | webcam | kite,
                                                     surf | pg; omitted = all)

Units: Windy returns temperature in K, pressure in Pa, wind in m/s, precip in mm.
This script converts to °C / hPa and keeps m/s plus a km/h helper field.

Stamps: every output carries meta.source like "Windy gfs 2026-11-22T12:00:00Z"
because data-sources.md requires every weather line to be stamped with mode+date.

Environment:
  WINDY_BASE   base URL, default https://node.windy.com (set to a proxy prefix
               such as https://your-proxy/https://node.windy.com when 境内直连
               is slow or blocked)
  WINDY_CACHE  cache file (minifest + responses), default windycache.json in CWD;
               minifest TTL 30 min, forecasts TTL 60 min, alerts/elevation 10 min.
               Set WINDY_CACHE="" to disable.

Subcommands:
  forecast  --lat L --lon L [--model gfs] [--days 5] [--step 3] [--raw]
            -> hourly forecast; default models: gfs (fallback chain tries
               icon-global, ecmwf-hres when a model 404s or errors, unless
               --model is given explicitly). NOTE: each model serves its
               native cadence (3 h for the global models) — --step 1 still
               returns 3-hourly rows; meta.effective_step_hours records what
               you actually got.
  now       --lat L --lon L [--model gfs]        -> current conditions
  aqi       --lat L --lon L [--model cams-global] -> PM2.5/PM10/AQI-US etc.
  archive   --lat L --lon L [--hours 120] [--model gfs] [--raw]
                                                    -> past hourly values
                (temp_c/wind_kmh/gust_kmh/pressure_hpa converted; raw kept)
  alerts    --lat L --lon L [--lang zh-cn]       -> CAP warnings ([] when none;
               a 204 response is NOT an error)
  storms    [--count]                            -> active tropical cyclones
  elevation --lat L --lon L  |  --route "lat,lon;lat,lon[;...]"
                                                   -> single value / profile
  stations  --lat L --lon L                      -> nearby stations & airports
                (wind fields: raw m/s plus wind_kmh/gust_kmh normalized)
  obs       --source ad --id ZBAA [--count 10] [--raw]
                                                   -> station obs history (also:
               radiosonde | airq | pws | wmo; --source radiosonde --id 54511)
               (temp_c/dew_point_c/wind_kmh/gust_kmh/pressure_hpa converted)
  search    --query 北京 --lat L --lon L [--types station,radiosonde]
                                                   -> place/station search
               (size >= 10 enforced by server; types omitted = all)
  route     --route "39.90,116.41;39.12,117.20" [--mode car] [--dst 2026/11/24/02]
                                                   -> weather along the route
               (modes: car | boat | vfr | ifr; dst defaults to next UTC hour;
                temp_c/wind_kmh/gust_kmh converted, --raw to disable)
  celestial --lat L --lon L [--tz-hours 8]       -> sunrise/sunset/dusk; adds a
               "local" block in the target timezone (originals stay UTC)
  minifest  [--model gfs]                        -> debug: current model run
  all       --lat L --lon L [--model gfs] [--lang zh-cn] [--tz-hours 8]
            -> one JSON: forecast + now + aqi + alerts + storms + celestial,
               units converted

Exit codes: 0 = data returned (possibly an empty alerts list);
            1 = fetch/parse/network error (stderr has the reason);
            2 = usage error; 3 = model has no data for this point (e.g. a
            regional model outside its bounds — try gfs / icon-global).

Politeness: responses are cached, requests are sequential, timeouts are short
(20 s) and one retry with 2 s backoff. Do not script tight loops against Windy.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
import urllib.error
import urllib.parse
import urllib.request

BASE = (os.environ.get("WINDY_BASE") or "https://node.windy.com").rstrip("/")
CACHE_PATH = os.environ.get("WINDY_CACHE", "windycache.json")
CACHE_TTL = {"minifest": 1800, "forecast": 3600, "now": 600, "aqi": 3600,
             "archive": 3600, "alerts": 600, "storms": 600, "elevation": 600,
             "stations": 600, "obs": 600, "search": 600, "route": 600, "celestial": 3600,
             "lightning": 300, "airport": 900, "webcams": 900,
             "pois": 600, "reverse": 86400, "avalanche": 3600}
TIMEOUT = 20
RETRIES = 1
UA = "trip-planner-cn/1.0 (+windy public data; contact: repo maintainer)"

OBS_SOURCES = ("ad", "radiosonde", "airq", "pws", "wmo")
ROUTE_MODES = ("car", "boat", "vfr", "ifr")

# model -> run cadence in hours (used only when minifest is unreachable)
RUN_HOURS = {"gfs": 6, "gfs-wave": 6, "ecmwf-hres": 6, "icon-global": 6,
             "cams-global": 12, "ecmwf-wam": 12}
FORECAST_MODELS = ["gfs", "icon-global", "ecmwf-hres"]  # global, cover 境内
WAVE_MODELS = ["gfs-wave", "ecmwf-wam"]


def warn(msg):
    print("WARN windy: {}".format(msg), file=sys.stderr)


# ---------------------------------------------------------------- caching --
def _load_cache():
    if not CACHE_PATH:
        return {}
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save_cache(cache):
    if not CACHE_PATH:
        return
    try:
        with open(CACHE_PATH, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, ensure_ascii=False, sort_keys=True)
    except OSError as ex:
        warn("cache write failed ({}): {}".format(CACHE_PATH, ex))


def cache_get(kind, key):
    entry = _load_cache().get(kind, {}).get(key)
    if entry and time.time() - entry[0] < CACHE_TTL.get(kind, 600):
        return entry[1]
    return None


def cache_put(kind, key, value):
    if not CACHE_PATH:
        return
    cache = _load_cache()
    bucket = cache.setdefault(kind, {})
    bucket[key] = [time.time(), value]
    _save_cache(cache)


# ---------------------------------------------------------------- fetching --
def fetch_json(path, kind=None, cache_key=None, ttl_override=None):
    """GET {BASE}{path} -> parsed JSON. Raises WindyError on failure."""
    if kind and cache_key is not None:
        hit = cache_get(kind, cache_key)
        if hit is not None:
            return hit
    url = BASE + path
    last_err = None
    for attempt in range(1 + RETRIES):
        if attempt:
            time.sleep(2)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                       "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read().decode("utf-8", "replace")
                status = resp.getcode()
            if status == 204 or not raw.strip():
                data = None  # e.g. capalerts with no active warning
            else:
                data = json.loads(raw)
            if kind and cache_key is not None:
                cache_put(kind, cache_key, data)
            return data
        except urllib.error.HTTPError as ex:
            if ex.code == 404:
                raise WindyError("no data for this point/model (404)", code=3)
            if ex.code == 204:
                return None
            last_err = "HTTP {}".format(ex.code)
        except (urllib.error.URLError, OSError, ValueError) as ex:
            last_err = str(ex)
    raise WindyError("{} -> {}".format(url, last_err), code=1)


class WindyError(Exception):
    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------- refTime --
def reftime_for(model):
    """Latest run time for a model, from minifest; falls back to UTC cadence."""
    mkey = model
    hit = cache_get("minifest", mkey)
    if hit:
        return hit.get("ref")
    try:
        mani = fetch_json("/metadata/v1.0/forecast/{}/minifest.json".format(model),
                          kind="minifest", cache_key=mkey)
        return mani.get("ref")
    except WindyError as ex:
        warn("minifest failed ({}); guessing run time from cadence".format(ex))
        hours = RUN_HOURS.get(model, 6)
        now = time.time() - hours * 3600  # use a run that is surely published
        stamp = time.strftime("%Y-%m-%dT%H:00:00Z", time.gmtime(now))
        return stamp


# ------------------------------------------------------------ unit helpers --
def k_to_c(k):
    return None if k is None else round(k - 273.15, 1)


def pa_to_hpa(p):
    return None if p is None else round(p / 100.0, 1)


def ms_to_kmh(ms):
    return None if ms is None else round(ms * 3.6, 1)


def _norm_point(data):
    """Convert one point-forecast payload to human units (in place)."""
    if not isinstance(data, dict):
        return data
    d = data.get("data")
    if not isinstance(d, dict):
        return data
    out = {}
    for key, values in d.items():
        if not isinstance(values, list):
            out[key] = values
            continue
        if key == "temperature":
            out["temp_c"] = [k_to_c(v) for v in values]
        elif key == "feelTemperature":
            out["feels_c"] = [k_to_c(v) for v in values]
        elif key == "pressure":
            out["pressure_hpa"] = [pa_to_hpa(v) for v in values]
        elif key == "wind":
            out["wind_ms"] = values
            out["wind_kmh"] = [ms_to_kmh(v) for v in values]
        elif key == "windGust":
            out["gust_ms"] = values
            out["gust_kmh"] = [ms_to_kmh(v) for v in values]
        else:
            out[key] = values
    data["data"] = out
    return data


def _norm_now(payload):
    if not isinstance(payload, dict):
        return payload
    if "temp" in payload:
        payload["temp_c"] = k_to_c(payload.get("temp"))
        payload["dewpoint_c"] = k_to_c(payload.get("dewPoint"))
    if "pressure" in payload:
        payload["pressure_hpa"] = pa_to_hpa(payload.get("pressure"))
    for src, dst in (("wind", "wind_kmh"), ("gust", "gust_kmh")):
        if payload.get(src) is not None:
            payload[dst] = ms_to_kmh(payload[src])
    return payload


DEFAULT_TZ_HOURS = 8  # 境内统一北京时间; celestial --tz-hours 可覆盖


def celestial_local(data, tz_hours=DEFAULT_TZ_HOURS):
    """Add a 'local' block with tz-aware ISO strings for the celestial fields.

    Windy /services/celestial returns epoch-ms stamps plus UTC ISO strings
    only ("night": "…T10:58:29Z"), but plans are written in the destination's
    wall time — provide both, keep the original fields untouched."""
    tz = timezone(timedelta(hours=tz_hours))
    out = {}
    for key in ("sunriseTs", "sunsetTs", "duskTs", "dawnTs"):
        ts = data.get(key)
        if isinstance(ts, (int, float)):
            out[key[:-2]] = datetime.fromtimestamp(ts / 1000.0, tz).isoformat()
    night = data.get("night")
    if isinstance(night, str):
        try:
            out["night"] = datetime.fromisoformat(
                night.replace("Z", "+00:00")).astimezone(tz).isoformat()
        except ValueError:
            pass
    out["tz_hours"] = tz_hours
    return out


def normalize_station(st):
    """Add wind_kmh/gust_kmh to one /pois/v2/stations entry.

    Station obs report wind in m/s while point-forecast output already carries
    km/h columns; expose both so model-vs-observation cross-checks compare
    like with like. Original fields are left untouched."""
    if not isinstance(st, dict):
        return st
    wind = st.get("wind")
    if isinstance(wind, (int, float)):
        st["wind_kmh"] = round(wind * 3.6, 1)
    gust = st.get("gust")
    if isinstance(gust, (int, float)):
        st["gust_kmh"] = round(gust * 3.6, 1)
    return st


def _map_units(series, fn):
    if not isinstance(series, list):
        return series
    return [None if v is None else fn(v) for v in series]


def normalize_archive(data):
    """Convert /forecast/archive values to human units (in place).

    Same schema as the point forecast but the raw endpoint returns K / Pa /
    m/s and cmd_archive skipped the conversion; add *_c / *_hpa / *_kmh
    columns and keep the raw arrays untouched."""
    if not isinstance(data, dict):
        return data
    d = data.get("data")
    if not isinstance(d, dict):
        return data
    if isinstance(d.get("temperature"), list):
        d["temp_c"] = _map_units(d["temperature"], k_to_c)
    for src, fn, dst in (("wind", ms_to_kmh, "wind_kmh"),
                         ("windGust", ms_to_kmh, "gust_kmh"),
                         ("pressure", pa_to_hpa, "pressure_hpa"),
                         ("feelTemperature", k_to_c, "feels_c")):
        if isinstance(d.get(src), list):
            d[dst] = _map_units(d[src], fn)
    return data


def normalize_route(data):
    """Convert /rplanner/v1/forecast values to human units (in place).

    temp K -> temp_c, wind/gust m/s -> *_kmh, visibility m (as returned)."""
    if not isinstance(data, dict):
        return data
    d = data.get("data")
    if not isinstance(d, dict):
        return data
    if isinstance(d.get("temp"), list):
        d["temp_c"] = _map_units(d["temp"], k_to_c)
    for src, dst in (("wind", "wind_kmh"), ("gust", "gust_kmh")):
        if isinstance(d.get(src), list):
            d[dst] = _map_units(d[src], ms_to_kmh)
    return data


def normalize_obs(data):
    """Convert /obs/measurement/v3 values to human units (in place).

    temp/dewPoint K -> *_c, wind/gust m/s -> *_kmh, pressure Pa -> pressure_hpa;
    raw arrays stay untouched for traceability."""
    if not isinstance(data, dict):
        return data
    d = data.get("data")
    if not isinstance(d, dict):
        return data
    for src, fn, dst in (("temp", k_to_c, "temp_c"),
                         ("dewPoint", k_to_c, "dew_point_c"),
                         ("wind", ms_to_kmh, "wind_kmh"),
                         ("gust", ms_to_kmh, "gust_kmh"),
                         ("pressure", pa_to_hpa, "pressure_hpa")):
        if isinstance(d.get(src), list):
            d[dst] = _map_units(d[src], fn)
    return data


def forecast_step_note(data, requested_step):
    """Record the effective sampling step (coarsest gap measured from the
    returned timestamps) in meta, with a note when the server quantized the
    requested step.

    Windy serves each model at its native cadence (3 h for gfs / icon-global /
    ecmwf-hres detail), so --step 1 silently comes back 3-hourly; make that
    visible instead of implying hourly data. The coarsest gap is what a
    consumer must plan around, so irregular spacing (a dropped row) also
    surfaces."""
    try:
        meta = data.setdefault("meta", {})
        ts = (data.get("data") or {}).get("ts") or []
        if len(ts) >= 2:
            step_ms = max(ts[i + 1] - ts[i] for i in range(len(ts) - 1))
            hours = step_ms / 3600000.0
            meta["effective_step_hours"] = round(hours, 2)
            if requested_step and hours > requested_step:
                meta["note"] = (
                    "server returned up to {:g} h sampling for --step {} "
                    "(model-native cadence)".format(hours, requested_step))
    except (AttributeError, TypeError, ValueError):
        pass
    return data


def _stamp(model, ref):
    return "Windy {} {}".format(model, ref or "")


# ------------------------------------------------------------ subcommands --
def cmd_forecast(args):
    models = [args.model] if args.model else FORECAST_MODELS
    errors = []
    for model in models:
        ref = reftime_for(model)
        path = "/forecast/point/{}/v3.0/{:.4f}/{:.4f}".format(model, args.lat, args.lon)
        qs = urllib.parse.urlencode({"refTime": ref, "days": args.days,
                                     "step": args.step})
        if args.source:
            qs += "&source=" + args.source
        try:
            data = fetch_json("{}?{}".format(path, qs), kind="forecast",
                              cache_key="{}|{}|{}|{}".format(model, args.lat,
                                                              args.lon, args.step))
        except WindyError as ex:
            errors.append("{}: {}".format(model, ex))
            continue
        if data is None:
            errors.append("{}: empty".format(model))
            continue
        if not args.raw:
            data = _norm_point(data)
        data.setdefault("meta", {})["source"] = _stamp(model, ref)
        data["meta"]["model"] = model
        forecast_step_note(data, args.step)
        return data
    raise WindyError("; ".join(errors) or "no data", code=3 if errors else 1)


def cmd_now(args):
    model = args.model or "gfs"
    ref = reftime_for(model)
    path = "/forecast/point/now/{}/v1.0/{:.4f}/{:.4f}?refTime={}".format(
        model, args.lat, args.lon, urllib.parse.quote(ref))
    data = fetch_json(path, kind="now", cache_key="{}|{}|{}".format(
        model, args.lat, args.lon))
    data = _norm_now(data)
    data.setdefault("meta", {})["source"] = _stamp(model, ref)
    return data


def cmd_aqi(args):
    model = args.model or "cams-global"
    ref = reftime_for(model)
    path = "/forecast/airq/{}/v3.0/{:.4f}/{:.4f}".format(model, args.lat, args.lon)
    qs = urllib.parse.urlencode({"refTime": ref, "days": args.days, "step": args.step})
    data = fetch_json("{}?{}".format(path, qs), kind="aqi",
                      cache_key="{}|{}|{}".format(model, args.lat, args.lon))
    if isinstance(data, dict):
        data.setdefault("meta", {})["source"] = _stamp(model, ref)
    return data


def cmd_archive(args):
    model = args.model or "gfs"
    ref = reftime_for(model)
    path = "/forecast/archive/{}/v3.0/{:.4f}/{:.4f}".format(model, args.lat, args.lon)
    qs = urllib.parse.urlencode({"archiveHours": args.hours, "source": args.source})
    data = fetch_json("{}?{}".format(path, qs), kind="archive",
                      cache_key="{}|{}|{}".format(model, args.lat, args.hours))
    if not args.raw:
        data = normalize_archive(data)
    if isinstance(data, dict):
        data.setdefault("meta", {})["source"] = _stamp(model, ref)
    return data


def cmd_alerts(args):
    path = "/capalerts/{:.4f}/{:.4f}".format(args.lat, args.lon)
    qs = urllib.parse.urlencode({"detailed": "true", "lang": args.lang})
    data = fetch_json("{}?{}".format(path, qs), kind="alerts",
                      cache_key="{:.4f}|{:.4f}|{}".format(args.lat, args.lon, args.lang))
    # 204 / empty body -> None -> no active warnings; that is success
    return {"alerts": data or [], "meta": {"source": "Windy capalerts",
                                           "fetched_at": time.strftime(
                                               "%Y-%m-%dT%H:%M:%SZ", time.gmtime())}}


def cmd_storms(args):
    """Active tropical cyclones; one storm's detail via --id (e.g. xavier)."""
    if args.id:
        return fetch_json("/tc/v2/storms/{}".format(args.id), kind="storms",
                          cache_key="detail|" + args.id)
    if args.count:
        data = fetch_json("/tc/v2/storms/active/count", kind="storms",
                          cache_key="count")
        return {"active_count": data}
    data = fetch_json("/tc/v2/storms", kind="storms", cache_key="all")
    return {"storms": (data or {}).get("storms", []), "meta": {"source": "Windy tc/v2"}}


def cmd_celestial(args):
    """Sunrise/sunset/dusk/night for a point (complements the local NOAA calc)."""
    path = "/services/celestial/{:.4f}/{:.4f}".format(args.lat, args.lon)
    data = fetch_json(path, kind="celestial",
                      cache_key="{:.4f}|{:.4f}".format(args.lat, args.lon))
    if not isinstance(data, dict):
        raise WindyError("celestial returned no parsable data", code=1)
    data["local"] = celestial_local(data, args.tz_hours)
    data.setdefault("meta", {})["source"] = "Windy services/celestial"
    return data


def cmd_lightning(args):
    """Recent lightning: /blitz/v3/hot (dense strike cells, stable) or
    /blitz/v3/60mins (last-hour strikes — flaky, sometimes 404)."""
    which = "hot" if not args.minutes60 else "60mins"
    data = fetch_json("/blitz/v3/{}".format(which), kind="lightning",
                      cache_key=which)
    if data is None:
        return {"strikes": [], "meta": {"source": "Windy blitz/v3/" + which}}
    strikes = data if isinstance(data, list) else data.get("data", data)
    return {"strikes": strikes,
            "meta": {"source": "Windy blitz/v3/{}".format(which)}}


def cmd_airport(args):
    """Aviation weather for an airport: METAR (current), TAF (forecast),
    NOTAMs and aerodrome info. Verified: ZBAA all four 200."""
    parts = {}
    wanted = args.what.split(",") if args.what != "all" else \
        ["metar", "taf", "notams", "adinfo"]
    for what in wanted:
        path = "/airports/{}/{}".format(what, args.icao)
        data = fetch_json(path, kind="airport",
                          cache_key="{}|{}".format(args.icao, what))
        parts[what] = data
    return {"icao": args.icao, "data": parts,
            "meta": {"source": "Windy airports/*", "icao": args.icao}}


def cmd_webcams(args):
    """Nearby webcams (list), freshness (last-updated-on) or the hourly day
    archive of one webcam (photo timestamps, not the images themselves)."""
    if args.id:
        if args.archive:
            data = fetch_json("/webcams/v2.0/archive/hourly/{}".format(args.id),
                              kind="webcams", cache_key="archive|" + args.id)
            return {"webcamId": args.id, "archive": data,
                    "meta": {"source": "Windy webcams/v2.0/archive/hourly"}}
        data = fetch_json(
            "/webcams/v1.0/webcams/last-updated-on?webcamIds={}".format(args.id),
            kind="webcams", cache_key="updated|" + args.id)
        return {"webcamId": args.id, "lastUpdated": data,
                "meta": {"source": "Windy webcams/v1.0/last-updated-on"}}
    # list: nearby lat,lon RAW commas (server rule), radius separate, no imageSize
    path = "/webcams/v2.0/list?nearby={:.4f},{:.4f}&radius={}&order=popularity&limit={}&lang={}".format(
        args.lat, args.lon, args.radius, args.limit, args.lang)
    data = fetch_json(path, kind="webcams",
                      cache_key="{:.4f}|{:.4f}|{}".format(args.lat, args.lon, args.radius))
    return {"cams": (data or {}).get("cams", []),
            "meta": {"source": "Windy webcams/v2.0/list",
                     "radius_km": args.radius}}


POI_LIST_IDENTS = ("stations", "ad", "tide", "airq", "radiosonde",
                   "pgspots", "kitespots", "surfspots")


def cmd_pois(args):
    """Nearest POIs by ident. List-style idents (verified): stations, ad
    (airports), tide, airq, radiosonde; spot idents share the same shape.
    firespots uses the mesh-tile form /pois/v2/firespots/tiles/{z}/{x}/{y}
    (raw arrays: id / tileX / tileY / spotRank — sub-grid coords, returned
    as-is with the tile URL in meta)."""
    if args.ident == "firespots":
        import math
        z = 7
        x = int((args.lon + 180.0) / 360.0 * (1 << z))
        lat_rad = math.radians(args.lat)
        y = int((1 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2 * (1 << z))
        path = "/pois/v2/firespots/tiles/{}/{}/{}".format(z, x, y)
        data = fetch_json(path, kind="pois",
                          cache_key="firespots|{}|{}|{}".format(z, x, y))
        return {"firespots": data,
                "meta": {"source": "Windy pois/v2/firespots (mesh tile)",
                         "tile": "{}/{}/{}".format(z, x, y),
                         "note": "tileX/tileY are sub-grid coords inside the tile"}}
    if args.ident not in POI_LIST_IDENTS:
        raise WindyError("ident must be one of {}, or firespots".format(
            ",".join(POI_LIST_IDENTS)), code=2)
    path = "/pois/v2/{}/{:.4f}/{:.4f}".format(args.ident, args.lat, args.lon)
    data = fetch_json(path, kind="pois",
                      cache_key="{}|{:.4f}|{:.4f}".format(args.ident, args.lat, args.lon))
    return {"pois": data or [],
            "meta": {"source": "Windy pois/v2/" + args.ident}}


def cmd_reverse(args):
    """Reverse geocoding with local-language place names (verified zh-cn)."""
    path = "/reverse/v3/{:.4f}/{:.4f}/{}?lang={}".format(
        args.lat, args.lon, args.zoom, args.lang)
    data = fetch_json(path, kind="reverse",
                      cache_key="{:.4f}|{:.4f}|{}".format(args.lat, args.lon, args.lang))
    if not isinstance(data, dict):
        raise WindyError("reverse returned no parsable data", code=1)
    data.setdefault("meta", {})["source"] = "Windy reverse/v3"
    return data


def cmd_avalanche(args):
    """Avalanche danger: regions.json (full micro-region registry w/ borders)
    or overview/{date}.json (global danger levels keyed by region id).
    Verified: regions 200·529KB, overview/2026-11-23.json 200·126KB.
    Coverage is mostly Europe/US mountain areas — China not included."""
    if args.regions:
        data = fetch_json("/avalanches/regions.json", kind="avalanche",
                          cache_key="regions")
        return {"regions": data,
                "meta": {"source": "Windy avalanches/regions.json"}}
    date = args.date or time.strftime("%Y-%m-%d", time.gmtime())
    data = fetch_json("/avalanches/overview/{}.json".format(date),
                      kind="avalanche", cache_key="overview|" + date)
    return {"date": date, "dangerByRegion": data,
            "meta": {"source": "Windy avalanches/overview"}}


def cmd_elevation(args):
    if args.route:
        pts = []
        for pair in args.route.split(";"):
            lat_s, _, lon_s = pair.partition(",")
            try:
                pts.append((float(lat_s), float(lon_s)))
            except ValueError:
                raise WindyError("bad route point {!r} (want lat,lon)".format(pair), code=2)
        path_pts = ";".join("{:.4f},{:.4f}".format(lat, lon) for lat, lon in pts)
        data = fetch_json("/rplanner/v1/elevation/{}".format(path_pts),
                          kind="elevation", cache_key=path_pts)
        return data
    path = "/services/elevation/{:.4f}/{:.4f}".format(args.lat, args.lon)
    data = fetch_json(path, kind="elevation",
                      cache_key="{:.4f}|{:.4f}".format(args.lat, args.lon))
    try:
        return {"elevation_m": int(data), "meta": {"source": "Windy services/elevation"}}
    except (TypeError, ValueError):
        return data


def cmd_stations(args):
    path = "/pois/v2/stations/{:.4f}/{:.4f}".format(args.lat, args.lon)
    data = fetch_json(path, kind="stations",
                      cache_key="{:.4f}|{:.4f}".format(args.lat, args.lon))
    stations = [normalize_station(st) for st in (data or [])]
    return {"stations": stations,
            "meta": {"source": "Windy pois/v2/stations",
                     "wind_units": "m/s raw + km/h normalized"}}


def cmd_obs(args):
    """Station obs history: /obs/measurement/v3/{source}/{id}/{n}/1.

    source=id conventions (verified against the app's station plugin):
      ad {ICAO}            airport METAR history (e.g. ZBAA), incl. summary
      radiosonde {wmo-id}  sounding + 4D balloon trajectory (e.g. 54511 北京)
      airq {numeric}       air-quality station (ids from search/pois)
      pws {hex}            personal weather station (204 = not reporting)
      wmo {wmo-id}         SYNOP station (mostly non-China)
    204 / None -> station exists but has no measurements; reported as empty.
    """
    if args.source not in OBS_SOURCES:
        raise WindyError("source must be one of {}".format(",".join(OBS_SOURCES)),
                         code=2)
    path = "/obs/measurement/v3/{}/{}/{}/1?properties={}".format(
        args.source, args.id, args.count,
        urllib.parse.quote(args.properties, safe=""))
    data = fetch_json(path, kind="obs",
                      cache_key="{}|{}|{}".format(args.source, args.id, args.count))
    if data is None:
        return {"station": "{}-{}".format(args.source, args.id),
                "measurements": [], "note": "no measurements (204)",
                "meta": {"source": "Windy obs/measurement/v3"}}
    if not getattr(args, "raw", False):
        data = normalize_obs(data)
    return data


def cmd_search(args):
    """Place/station search. Server enforces size >= 10; types omitted = all."""
    size = max(10, args.size)
    qs = urllib.parse.urlencode({"lang": args.lang, "size": size})
    if args.types:
        qs += "&types=" + urllib.parse.quote(args.types, safe=",")
    query = urllib.parse.quote(args.query.replace("/", " "), safe="")
    path = "/search/v4.1/{:.4f}/{:.4f}/{}?{}".format(args.lat, args.lon, query, qs)
    data = fetch_json(path, kind="search",
                      cache_key="{}|{}|{}".format(args.query, args.types, args.lang))
    if not isinstance(data, dict):
        raise WindyError("search returned no parsable data", code=1)
    return {"results": data.get("data", []),
            "meta": {"source": "Windy search/v4.1", "size": size}}


def cmd_route(args):
    """Weather along a route: /rplanner/v1/forecast/{mode}/{points}?dst=…

    points: "lat,lon;lat,lon;..." (commas/semicolons raw in the path, as the
    app sends them). dst is the departure time as YYYY/MM/DD/HH (UTC, hour
    aligned to the ECMWF calendar); defaults to the next UTC hour.
    """
    if args.mode not in ROUTE_MODES:
        raise WindyError("mode must be one of {}".format(",".join(ROUTE_MODES)),
                         code=2)
    pts = []
    for pair in args.route.split(";"):
        lat_s, _, lon_s = pair.partition(",")
        try:
            pts.append((float(lat_s), float(lon_s)))
        except ValueError:
            raise WindyError("bad route point {!r} (want lat,lon)".format(pair),
                             code=2)
    if len(pts) < 2:
        raise WindyError("route needs at least two lat,lon points", code=2)
    if args.dst:
        dst = args.dst.strip()
    else:
        nxt = time.gmtime(time.time() + 3600)
        dst = time.strftime("%Y/%m/%d/%H", nxt)
    path_pts = ";".join("{:.4f},{:.4f}".format(lat, lon) for lat, lon in pts)
    path = "/rplanner/v1/forecast/{}/{}?dst={}".format(
        args.mode, path_pts, urllib.parse.quote(dst, safe=""))
    data = fetch_json(path, kind="route",
                      cache_key="{}|{}|{}".format(args.mode, path_pts, dst))
    if not getattr(args, "raw", False):
        data = normalize_route(data)
    if isinstance(data, dict):
        data.setdefault("meta", {})["source"] = "Windy rplanner/v1"
        data["meta"]["mode"] = args.mode
        data["meta"]["dst"] = dst
    return data


def cmd_minifest(args):
    model = args.model or "gfs"
    return fetch_json("/metadata/v1.0/forecast/{}/minifest.json".format(model),
                      kind="minifest", cache_key=model)


def cmd_all(args):
    out = {"meta": {"source": "Windy node.windy.com (keyless)",
                    "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}}
    ns = lambda **kw: argparse.Namespace(
        lat=args.lat, lon=args.lon, lang=args.lang, days=args.days,
        step=args.step, source=args.source or None, raw=False, count=False,
        id=None, model=args.model, hours=None, tz_hours=args.tz_hours, **kw)
    sections = [
        ("forecast", lambda: cmd_forecast(ns())),
        ("now", lambda: cmd_now(ns())),
        ("aqi", lambda: cmd_aqi(ns())),
        ("alerts", lambda: cmd_alerts(ns())),
        ("storms", lambda: cmd_storms(ns())),
        ("celestial", lambda: cmd_celestial(ns())),
    ]
    failed = []
    for name, fn in sections:
        try:
            out[name] = fn()
        except WindyError as ex:
            failed.append("{}: {}".format(name, ex))
            out[name] = None
    if failed:
        warn("partial: " + "; ".join(failed))
    return out


# --------------------------------------------------------------------- CLI --
def main(argv=None):
    # JSON output must survive redirection on CJK Windows consoles (GBK
    # cannot encode webcams titles etc.); reconfigure stdout/stderr to
    # UTF-8 instead of relying on PYTHONIOENCODING being set.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except (OSError, ValueError):
                pass
    ap = argparse.ArgumentParser(
        prog="windy_fetch.py",
        description="Windy keyless weather source for trip-planner (see docs/windy.md)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_point(sp, with_model=True):
        sp.add_argument("--lat", type=float, required=True)
        sp.add_argument("--lon", type=float, required=True)
        if with_model:
            sp.add_argument("--model", default=None)

    p = sub.add_parser("forecast", help="hourly point forecast")
    add_point(p)
    p.add_argument("--days", type=int, default=5)
    p.add_argument("--step", type=int, default=3)
    p.add_argument("--source", default=None)
    p.add_argument("--raw", action="store_true", help="no unit conversion")

    p = sub.add_parser("now", help="current conditions")
    add_point(p)

    p = sub.add_parser("aqi", help="air quality (PM2.5/PM10/AQI-US)")
    add_point(p)
    p.add_argument("--days", type=int, default=4)
    p.add_argument("--step", type=int, default=3)

    p = sub.add_parser("archive", help="past ~5 days of hourly values")
    add_point(p)
    p.add_argument("--hours", type=int, default=120)
    p.add_argument("--source", default="station")
    p.add_argument("--raw", action="store_true", help="no unit conversion")

    p = sub.add_parser("alerts", help="CAP weather warnings ([] when none)")
    add_point(p, with_model=False)
    p.add_argument("--lang", default="zh-cn")

    p = sub.add_parser("storms", help="active tropical cyclones")
    p.add_argument("--count", action="store_true", help="count only")
    p.add_argument("--id", default=None, help="storm id from the list, e.g. xavier")

    p = sub.add_parser("elevation", help="single point or route elevation")
    p.add_argument("--lat", type=float)
    p.add_argument("--lon", type=float)
    p.add_argument("--route", default=None, help='"lat,lon;lat,lon;..."')

    p = sub.add_parser("stations", help="nearby stations with live obs")
    add_point(p, with_model=False)

    p = sub.add_parser("obs", help="station obs history (obs/measurement/v3)")
    p.add_argument("--source", default="ad", help="ad|radiosonde|airq|pws|wmo")
    p.add_argument("--id", required=True, help="ICAO / wmo id / numeric id")
    p.add_argument("--count", type=int, default=10,
                   help="measurements to fetch back from latest (default 10)")
    p.add_argument("--properties", default="data,summary,segments")
    p.add_argument("--raw", action="store_true", help="no unit conversion")

    p = sub.add_parser("search", help="place/station search (search/v4.1)")
    p.add_argument("--query", required=True)
    p.add_argument("--lat", type=float, default=39.9042,
                   help="search centre lat (default Beijing)")
    p.add_argument("--lon", type=float, default=116.4074)
    p.add_argument("--types", default=None,
                   help="airport | station,radiosonde | webcam | kite,surf | pg")
    p.add_argument("--lang", default="zh-cn")
    p.add_argument("--size", type=int, default=10, help=">=10 (server rule)")

    p = sub.add_parser("route", help="weather along a route (rplanner/v1)")
    p.add_argument("--route", required=True, help='"lat,lon;lat,lon;..."')
    p.add_argument("--mode", default="car", help="car|boat|vfr|ifr")
    p.add_argument("--dst", default=None, help="departure YYYY/MM/DD/HH (UTC)")
    p.add_argument("--raw", action="store_true", help="no unit conversion")

    p = sub.add_parser("celestial", help="sunrise/sunset/dusk (services/celestial)")
    add_point(p, with_model=False)
    p.add_argument("--tz-hours", type=int, default=DEFAULT_TZ_HOURS,
                   help="target timezone offset for the local block "
                        "(default +8, 境内统一北京时间)")

    p = sub.add_parser("lightning", help="recent lightning (blitz/v3)")
    p.add_argument("--minutes60", action="store_true",
                   help="last-60-min strikes (flaky) instead of dense cells")

    p = sub.add_parser("airport", help="METAR/TAF/NOTAMs/adinfo (airports)")
    p.add_argument("--icao", required=True, help="e.g. ZBAA")
    p.add_argument("--what", default="all",
                   help="all | comma list of metar,taf,notams,adinfo")

    p = sub.add_parser("webcams", help="nearby webcams / freshness / archive")
    p.add_argument("--lat", type=float, default=39.9042)
    p.add_argument("--lon", type=float, default=116.4074)
    p.add_argument("--radius", type=int, default=250, help="km (default 250)")
    p.add_argument("--limit", type=int, default=3)
    p.add_argument("--lang", default="zh-cn")
    p.add_argument("--id", default=None, help="webcam id (freshness mode)")
    p.add_argument("--archive", action="store_true",
                   help="with --id: hourly day archive timestamps")

    p = sub.add_parser("pois", help="nearest POIs by ident (pois/v2)")
    p.add_argument("--ident", required=True,
                   help="stations|ad|tide|airq|radiosonde|pgspots|kitespots|"
                        "surfspots|firespots")
    p.add_argument("--lat", type=float, default=39.9042)
    p.add_argument("--lon", type=float, default=116.4074)

    p = sub.add_parser("reverse", help="reverse geocoding (reverse/v3)")
    p.add_argument("--lat", type=float, default=39.9042)
    p.add_argument("--lon", type=float, default=116.4074)
    p.add_argument("--zoom", type=int, default=10)
    p.add_argument("--lang", default="zh-cn")

    p = sub.add_parser("avalanche", help="avalanche danger (regions/overview)")
    p.add_argument("--regions", action="store_true",
                   help="full micro-region registry instead of today's danger")
    p.add_argument("--date", default=None, help="YYYY-MM-DD (default today)")

    p = sub.add_parser("minifest", help="debug: current model run info")
    p.add_argument("--model", default=None)

    p = sub.add_parser("all", help="forecast+now+aqi+alerts+storms in one JSON")
    add_point(p)
    p.add_argument("--lang", default="zh-cn")
    p.add_argument("--days", type=int, default=5)
    p.add_argument("--step", type=int, default=3)
    p.add_argument("--source", default=None)
    p.add_argument("--tz-hours", type=int, default=DEFAULT_TZ_HOURS,
                   help="celestial local-block timezone offset (default +8)")

    args = ap.parse_args(argv)
    handlers = {"forecast": cmd_forecast, "now": cmd_now, "aqi": cmd_aqi,
                "archive": cmd_archive, "alerts": cmd_alerts, "storms": cmd_storms,
                "elevation": cmd_elevation, "stations": cmd_stations,
                "obs": cmd_obs, "search": cmd_search, "route": cmd_route,
                "celestial": cmd_celestial, "lightning": cmd_lightning,
                "airport": cmd_airport, "webcams": cmd_webcams,
                "pois": cmd_pois, "reverse": cmd_reverse,
                "avalanche": cmd_avalanche, "minifest": cmd_minifest,
                "all": cmd_all}
    if args.cmd == "elevation" and not args.route and (args.lat is None or args.lon is None):
        ap.error("elevation needs --lat/--lon or --route")
    try:
        result = handlers[args.cmd](args)
    except WindyError as ex:
        warn(str(ex))
        sys.exit(ex.code)
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
