#!/usr/bin/env python3
"""Day-route toolbox for the trip-planner skill (stdlib only, Python 3.9+).

Input: a plan JSON — the SAME file render_plan.py consumes, so the map links, the
KML and the written plan can never drift apart:
{
  "trip": "hangzhou-apr",
  "tz": "Asia/Shanghai",                   # optional, IANA name; days may override
  "days": [
    {"date": "2026-04-11", "label": "西湖东线", "tz": "Asia/Shanghai",
     "stops": [
       {"name": "断桥残雪", "query": "断桥残雪, 杭州市"},
       {"name": "雷峰塔", "lat": 30.2318, "lon": 120.1489, "mode": "transit"}
     ]}
  ]
}
"query" defaults to "name"; a stop with lat/lon pre-filled skips geocoding; a day
with no stops (a travel day) still passes geocode/check, but plan_lint --strict
requires one stop and a sun on every day.

Optional stop "mode" describes the hop INTO that stop and overrides the distance
guess. Values: "walk" | "transit" | "fly" | "drive" | "boat" | "train" | "bus".
Set it whenever the traveller rides a short hop or walks a long one, because it
decides both the walking total and which directions the tappable link opens. Model
a long stroll as a stop at its midpoint, otherwise its kilometres never reach the
walking total. A hop with NO mode is GUESSED from straight-line distance
(<=1.6 km walk, else transit); `check` reports guessed and declared totals apart
and flags as SUSPICIOUS (exit 2) an undeclared hop >12 km or a declared walk/transit
hop >60 km (a city ride does not cross 60 km: fix the mis-geocoded stop, or say
train/bus/boat/drive/fly if a vehicle really runs it) — a declared fly/drive/boat/train/bus
hop of any length is a normal itinerary shape, never a failure.

Timeline hop rows and map links (links --write): hop rows (kind:"hop", map != false)
correspond 1:1, IN ORDER, with the stop-to-stop hops of that day. Two shapes break
that silently and MUST be handled in the plan: the FIRST hop of a day (lodging ->
first stop) and the LAST hop (-> lodging) either carry "map": false, or the lodging
itself is written into stops[0] / stops[-1].
Rail / flight / boat rows — the ONE rule ("map": false depends on the stops list,
not on the vehicle):
  * both ends ARE in that day's stops[] (e.g. stops: [..., 西安北站, 北京西站, ...])
    -> leave the row mappable (NO "map": false). It is one of the day's hops, so it
       keeps the count aligned; `links --write` writes NO link onto it (long leg,
       kept for alignment) and says so.
  * the leg is covered only by legs[] and its stations are NOT in stops[]
    (e.g. stops jump from a Rome museum straight to a Florence piazza)
    -> mark the row "map": false, so it sits outside the stop-to-stop pairing.
  Getting this wrong is silent-ish: N rows vs N+1 hops -> the whole day is parked in
  day["hop_links"] (a WARN tells you), or every later pairing shifts by one.
`links --write` prints the origin -> destination place names next to the row text
it wrote to, refuses a row that names a different stop of the day (weak name check;
the URL is parked in day["hop_links"] instead), and never emits a transit deep link
for a hop >100 km or a declared fly/boat hop (those are covered by legs[]/the
operator's link) — such rows are counted as "long legs kept unlinked (expected)".
The name check (Mexico F2, Turkey F3): only the text around the arrow counts; a
match is case/accent-insensitive and, for Latin words, whole-word ("Mural" does not
hit "Muralismo"); LONGEST FIRST — another stop's name that is a substring of one of
this hop's endpoint names ("格雷梅" inside "格雷梅露天博物馆", "centro" inside
"酒店 · 杭州湖滨银泰") is not a hit, but a longer other-stop name that CONTAINS
the endpoint's name is; a token shared by two differently-named stops of the day
("centro", "Museo") is generic and never a hit on its own. Parking is the designed
fallback and keeps exit 0, but whenever parked > 0 the run ends with a loud stderr
WARN block listing every parked row (day, row number, first 40 chars) — read it.

Map provider (links --provider amap|baidu, default amap; overridable with env
TRIP_MAP_PROVIDER). 本 skill 仅支持境内行程,不再提供 google/apple 链接:
  amap    https://uri.amap.com/navigation?from=lon,lat,name&to=lon,lat,name
          &mode=walk|bus|car&src=trip-planner   (keyless; 高德)
  baidu   https://api.map.baidu.com/direction?origin=lat,lon&destination=lat,lon
          &mode=walking|transit|driving&coord_type=wgs84&output=html&src=trip-planner
          (keyless; 百度地图; per-hop only)
Coordinates in the plan are WGS84; the amap/baidu builders convert mainland-China
points to GCJ-02 on the fly (standard correction), so pins land on the right
building in 高德/百度 without corrupting the plan's own data or the KML.
Whole-day DAY CHAIN: amap via= (drive overview, <=3 waypoints per chain); baidu has none and
per-hop links and no day_map.

Name the file plan.geo.json from the start: geocode then edits it in place and every
later command reads the one file that has everything.

Subcommands:
  geocode plan.geo.json       -> resolves stops in place (+ geocache.json); order:
                                 preset coords > geocache > 高德 restapi (needs
                                 AMAP_KEY / --amap-key, GCJ-02 auto-converted back
                                 to WGS84) > 百度 geocoding v3 (needs
                                 BAIDU_MAP_AK / --baidu-ak); existing coordinates
                                 are PRESERVED
  check   plan.geo.json       -> hop distances (straight-line), walk/transit
                                 estimates marked (est), on-foot vs ridden totals
                                 split into declared / guessed; warns (stderr) when a
                                 day has 0 km on foot but >=2 anchor rows; exits 2
                                 on an undeclared hop >12 km, a declared walk/transit
                                 hop >60 km, or a hop with missing coordinates;
                                 --live swaps (est) for real 高德 driving/transit
                                 minutes (needs AMAP_KEY; routecache.json cache)
  routes  plan.geo.json [--write] [--strategy 0] [--ttl 86400]
                              -> optional structured walk/transit/drive routes:
                                 metrics, explicit costs, segments and WGS84 paths.
                                 Dry-run writes nothing; --write atomically updates
                                 arriving stops' route fields + TTL routecache.json.
                                 AMAP_KEY environment/.env only (no key CLI here).
                                 check --live and its cross-city fallback stay old.
  links   plan.geo.json [--write] [--provider amap|baidu]
                              -> per-hop map links;
                                 --write injects them into the plan's hop rows and
                                 ends with a "wrote / parked / suspicious" summary
  kml     plan.geo.json -o trip.kml  -> numbered pins + day route lines
                                        (import into Organic Maps / 奥维互动地图等)
  poi     plan.geo.json --at 止名|纬度,经度 [--kw 地铁站|餐饮] [--radius 1000]
                              -> 高德 place/around 周边搜索 (needs AMAP_KEY;
                                 结果为 WGS84 坐标，可直接填回 plan)
  sun     plan.geo.json [--write] [--tz auto|Area/City] [--only DATE,...] [--lang zh|en]
                              -> per-day civil dawn / sunrise / sunset computed
                                 LOCALLY (NOAA solar model, no network, no API —
                                 境内直连默认) (city-level: the day's first
                                 stop with coordinates; on a MOVING day — travel_day
                                 true, or first and last stop >150 km apart — the LAST
                                 stop with coordinates, because that is where the
                                 evening is spent; day["sun_stop"] = a stop's name or
                                 0-based index in stops[] overrides that pick — e.g.
                                 a "日出在 c 城,随后动身去 d 城"
                                 day whose timeline hangs on the FIRST city — the
                                 pick reason then prints "sun_stop=…"; unknown or
                                 coordinate-less -> WARN + old rule), cross-checked
                                 against a local solar model before anything is
                                 written; --write sets day["sun"] to the canonical string
                                 "天亮 HH:MM · ☀ HH:MM / 🌇 HH:MM · TZ · 本地天文计算"
                                 (--lang en, or plan.lang / plan.meta.lang == "en":
                                 "dawn HH:MM · ☀ HH:MM / 🌇 HH:MM · TZ · NOAA local solar model")
                                 (renderers take the first HH:MM after 🌇 — keep a
                                 space after every time). Cached in suncache.json next
                                 to the plan, so re-runs on the same day are free.

Time zone for `sun`: day["tz"] > --tz Area/City > plan["tz"] / plan["meta"]["tz"] >
longitude-derived UTC offset (approximate, no DST — printed but NEVER written; pass
--tz or set tz in the plan to write those days).

Sanity guards in `sun` (why they exist: an API once returned "equator, today" with
status OK and nearly shipped as Norwegian October data): sunrise/sunset must agree
with the independent UTC-step model within 12 min; day length must match within 20
min; a polar day/night is reported, never written. Any failure -> that day is
reported on stderr and NOT written, and the command exits 3. Every day that ends up
NOT written (no ISO date, no coordinates, approximate tz, rejected)
gets its own WARN line on stderr and the closing summary lists them by date — do not
count the written days yourself (a day with no stops at all is informational, not
counted). Exit codes of `sun`: 0 = every day handled; 1 = at
least one day NOT written (the others ARE written — re-run `sun --write --only
DATE[,DATE]` for the listed dates; a missing date / coordinates / approximate tz
needs the plan fixed); 3 = a day was REJECTED by the sanity checks (look before
retrying — a polar day/night rejection is never retried, PLN-11). Non-zero always
means "some day has no sun value from this run" — the file may still have been
updated.

geocode also warns when a hit's display_name does not contain the query's head token
(first Latin word of >=4 letters, or the first 2 CJK characters) — geocoders happily
return a guesthouse named after the street, or a bus stop 600 m from the station;
that WARN means "open the coordinates and look before trusting them".

Change log (2026-08-15, after the AU / Nordic test runs):
- links --write: per-link origin -> destination names printed against the row text;
  weak stop-name mismatch check; no transit link >100 km or for fly/boat; summary.
- check: declared vs guessed totals; 0-km-on-foot warning; new long-hop modes do not
  trip exit 2; transit >20 km prints "use the operator timetable" instead of a 4-10x
  overestimated range; (est) markers; warnings on stderr.
- new `sun` subcommand with cache + sanity checks (Nordic F1, AU F7).
Change log (2026-08-15 b, after the China / Italy / Japan runs):
- one consistent rule for "map": false on rail/flight rows (docstring, WARNs, --help
  used to contradict each other — China F2, Italy F2).
- sun: moving days use the last stop; every unwritten day is a WARN + listed (Italy F5,
  China #9).
- links: "long legs kept unlinked (expected)" wording (Japan F11); --provider
  amap|baidu (境内-only build; google/apple 已移除).
Change log (2026-08-15 c, i18n):
- sun: --lang zh|en (default plan.lang > plan.meta.lang > zh) picks the dawn word
  ("天亮" / "dawn"); the renderers accept either spelling. zh output unchanged.
- geocode: head-token WARN on geocoder hits (Japan F5).
Change log (2026-08-16, after the Mexico / Morocco / Turkey / Vietnam runs):
- links: name check is whole-word + folded for Latin, longest-first / substring-of-
  endpoint rule for CJK, generic shared tokens ignored (历史测试 F2, F3);
  parked > 0 ends with a loud stderr WARN listing the parked rows, exit still 0
  (历史测试 F6); the mainland-China WARN fires only for links actually printed /
  written into China, and the China test is a coarse polygon instead of a box that
  swallowed neighbouring capitals (历史测试 F8).
- sun: day["sun_stop"] (name or 0-based index) overrides the first/last-stop pick
  (历史测试 F5); exit 1 whenever a day was skipped / failed, after writing the good
  days (Vietnam F6); a request failure is a skip (exit 1), not a "rejection" (3).
"""
import argparse
import datetime as _dt
import json
import math
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

UA = {"User-Agent": "trip-planner-skill/1.3 (personal trip planning script)"}
WALK_MIN_PER_KM = 14          # tourist pace incl. lights/photos
MAX_WALK_KM = 1.6             # beyond this, guess transit
DAY_WALK_FLAG_KM = 8.0
SUSPICIOUS_KM = 12.0          # undeclared hop longer than this: same day? same city?
DECLARED_CITY_MAX_KM = 60.0   # a DECLARED transit/walk hop longer than this is a
                              # mis-geocoded stop (or a train/bus/boat/drive/fly in disguise),
                              # never a city ride — 2026-09 test: a Nairobi hotel geocoded
                              # to Kisumu, 257 km, and "mode": "transit" silenced check
TRANSIT_EST_MAX_KM = 20.0     # beyond this a straight-line minute range is fiction
LINK_MAX_KM = 100.0           # beyond this no Google Maps deep link at all
SUN_MOVE_KM = 150.0           # first->last stop farther than this: the day moved city
LONG_MODES = ("fly", "drive", "boat", "train", "bus")
KNOWN_MODES = ("walk", "transit") + LONG_MODES
PROVIDERS = ("amap", "baidu")
DEFAULT_PROVIDER = os.environ.get("TRIP_MAP_PROVIDER", "amap").strip().lower()
if DEFAULT_PROVIDER not in PROVIDERS:
    DEFAULT_PROVIDER = "amap"


def wgs84_to_gcj02(lat, lon):
    """WGS-84 -> GCJ-02 (火星坐标). Standard public correction; only applied to
    mainland-China points (HK/MO/overseas pass through unchanged). Accuracy ~1 m,
    far below the pin-placement tolerance of a deep link."""
    if not in_mainland_china((lat, lon)):
        return lat, lon
    a = 6378245.0
    ee = 0.00669342162296594323

    def _transform_lat(x, y):
        ret = (-100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y
               + 0.2 * math.sqrt(abs(x)))
        ret += (20.0 * math.sin(6.0 * x * math.pi)
                + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
        ret += (20.0 * math.sin(y * math.pi)
                + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
        ret += (160.0 * math.sin(y / 12.0 * math.pi)
                + 320.0 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
        return ret

    def _transform_lon(x, y):
        ret = (300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y
               + 0.1 * math.sqrt(abs(x)))
        ret += (20.0 * math.sin(6.0 * x * math.pi)
                + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
        ret += (20.0 * math.sin(x * math.pi)
                + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
        ret += (150.0 * math.sin(x / 12.0 * math.pi)
                + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
        return ret

    dlat = _transform_lat(lon - 105.0, lat - 35.0)
    dlon = _transform_lon(lon - 105.0, lat - 35.0)
    radlat = lat / 180.0 * math.pi
    magic = math.sin(radlat)
    magic = 1 - ee * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((a * (1 - ee)) / (magic * sqrtmagic) * math.pi)
    dlon = (dlon * 180.0) / (a / sqrtmagic * math.cos(radlat) * math.pi)
    return lat + dlat, lon + dlon


def gcj02_to_wgs84(lat, lon):
    """GCJ-02 -> WGS-84, iterated inverse of wgs84_to_gcj02 (converges <1e-5 in
    3 passes). Used on 高德 geocode results so the plan always stores WGS84 —
    one datum everywhere, or `check` distances and the KML would drift."""
    if not in_mainland_china((lat, lon)):
        return lat, lon
    wlat, wlon = lat, lon
    for _ in range(3):
        glat, glon = wgs84_to_gcj02(wlat, wlon)
        wlat += lat - glat
        wlon += lon - glon
    return wlat, wlon
# Mainland China outline as a coarse (lat, lon) polygon, minus HK / Macau / Taiwan
# boxes. Coarse on purpose (~10-30 km at the borders): it only decides whether to
# print one WARN. A polygon, not a bounding box, because a bounding box would swallow
# neighbouring countries (a wider box once classified foreign stops as "mainland CN").
_CN_POLY = (
    (39.4, 73.6), (40.4, 75.0), (41.0, 76.9), (42.0, 80.2), (43.1, 80.4),
    (44.6, 80.0), (45.2, 82.4), (46.0, 82.4), (47.3, 82.9), (48.4, 85.6),
    (49.2, 87.4), (48.5, 89.5), (46.9, 90.7), (45.0, 91.0), (44.0, 95.5),
    (42.8, 96.4), (42.6, 100.0), (42.4, 101.8), (41.7, 104.5), (42.4, 107.5),
    (42.5, 109.5), (43.7, 111.9), (44.9, 113.7), (45.4, 115.8), (46.4, 117.4),
    (46.7, 119.9), (47.7, 117.6), (49.9, 116.7), (51.5, 120.0), (53.3, 121.5),
    (53.6, 123.7), (52.7, 126.5), (50.3, 127.6), (49.4, 129.5), (48.4, 135.0),
    (47.7, 134.6), (45.3, 133.5), (44.5, 131.3), (43.4, 131.3), (42.4, 130.7),
    (42.0, 128.4), (41.8, 127.1), (41.2, 126.4), (40.6, 124.9), (40.1, 124.4),
    (38.7, 121.2), (37.4, 122.8), (36.0, 120.6), (34.6, 119.5), (32.2, 121.9),
    (30.5, 122.4), (28.3, 121.9), (26.3, 120.0), (24.6, 118.8), (23.5, 117.5),
    (22.6, 114.9), (21.9, 113.1), (21.4, 111.5), (20.3, 110.5), (19.8, 111.2),
    (18.0, 110.4), (18.1, 108.5), (20.9, 109.4), (21.4, 108.0), (21.55, 107.4),
    (21.95, 106.65), (22.6, 106.7), (22.9, 106.3), (23.0, 105.5), (22.9, 104.5),
    (22.6, 103.9), (22.6, 103.0), (22.4, 102.2), (21.5, 101.75), (21.15, 101.7),
    (21.45, 101.15), (21.7, 100.1), (22.2, 99.4), (23.4, 98.9), (23.95, 97.6),
    (24.3, 97.6), (25.0, 98.2), (26.0, 98.4), (27.7, 98.4), (28.3, 97.5),
    (29.4, 96.1), (28.9, 94.9), (28.3, 93.0), (27.9, 92.0), (28.3, 91.5),
    (28.0, 89.6), (28.1, 88.9), (27.9, 88.1), (27.9, 86.6), (28.2, 85.4),
    (28.7, 84.4), (29.3, 84.0), (30.2, 81.0), (31.2, 79.2), (32.5, 78.5),
    (33.4, 78.9), (34.5, 78.6), (35.5, 78.0), (35.9, 76.6), (37.0, 74.9),
    (38.5, 74.0))
_NOT_MAINLAND = ((22.15, 22.60, 113.80, 114.45),     # Hong Kong
                 (22.10, 22.22, 113.52, 113.62),     # Macau
                 (21.80, 25.40, 119.30, 122.10))     # Taiwan
DAY_COLORS = ["ff0000ff", "ffff0000", "ff00aa00",
              "ff00aaff", "ffaa00aa", "ff777777"]   # KML aabbggrr
SHAPE = ('Expected: {"days":[{"date":"...","label":"...",'
         '"stops":[{"name":"...","query":"..."}]}]}')


def warn(msg):
    """All warnings go to stderr so stdout stays a clean report."""
    print("WARN: " + msg, file=sys.stderr)


def read_json(path, what="file"):
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as ex:
        sys.exit("Cannot read {} ({}): {}".format(path, what, ex))
    except UnicodeDecodeError as ex:
        sys.exit("{} is not UTF-8: {}".format(path, ex))
    try:
        return json.loads(raw)
    except ValueError as ex:
        sys.exit("{} is not valid JSON: {}\n{}".format(path, ex, SHAPE))


def load_plan(path):
    plan = read_json(path, "plan")
    if not isinstance(plan, dict) or not isinstance(plan.get("days"), list):
        sys.exit('{} has no "days" list.\n{}'.format(path, SHAPE))
    problems = []
    for i, day in enumerate(plan["days"], 1):
        if not isinstance(day, dict):
            sys.exit("days[{}] must be an object.\n{}".format(i, SHAPE))
        # Days with no mapped stops are normal (travel day, rest day) — tolerate.
        day.setdefault("stops", [])
        if not isinstance(day["stops"], list):
            sys.exit('days[{}]["stops"] must be a list.\n{}'.format(i, SHAPE))
        where = "day {} ({!r}), stop {}"
        for j, s in enumerate(day["stops"], 1):
            if not isinstance(s, dict):
                problems.append((where.format(i, day.get("label", ""), j),
                                 "must be an object, got {!r}".format(s)))
                continue
            s.setdefault("name", s.get("query") or "stop {}".format(j))
            for k in ("lat", "lon"):
                if s.get(k) is None:
                    continue
                try:
                    s[k] = float(s[k])
                except (TypeError, ValueError):
                    problems.append((
                        where.format(i, day.get("label", ""), j),
                        "{}={!r} is not a number (use decimal degrees, "
                        "e.g. 34.9949)".format(k, s[k])))
            m = s.get("mode")
            if m is not None and m not in KNOWN_MODES:
                warn("{}: unknown mode {!r} — treated as undeclared (known: {})"
                     .format(where.format(i, day.get("label", ""), j), m,
                             "|".join(KNOWN_MODES)))
                s["mode"] = None
    if problems:
        # Report every bad stop at once so the file gets fixed in one pass.
        sys.exit("\n".join("{}: {}".format(w, m) for w, m in problems))
    return plan


def coords(stop):
    if stop.get("lat") is not None and stop.get("lon") is not None:
        return stop["lat"], stop["lon"]
    return None


def haversine_km(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def r5(x):
    return int(round(x / 5.0)) * 5


def hop_estimate(km, mode=None):
    """Classify one hop. Returns (cat, declared, verdict, dur):
      cat      'walk' | 'transit' | one of LONG_MODES | 'suspicious'
      declared True when the plan said so, False when guessed from distance
      verdict  human label, always carrying (declared)/(guessed)
      dur      duration string marked (est), or None when we refuse to guess
    Distance alone cannot know whether a traveller rides a 1.4 km hop or walks it,
    and that single fact decides both the walking total and which directions the
    tappable link opens — so when the plan knows, it says so. Transit <=20 km gets a
    RANGE (a straight line cannot know the line, the headway, or the walk to the
    platform); transit >20 km gets NO minutes at all — airport expresses, tunnels
    and dedicated lines were being overestimated 4-10x, which is worse than nothing."""
    declared = mode is not None
    tag = " (declared)" if declared else " (guessed)"
    if mode in LONG_MODES:
        return mode, True, mode.upper() + tag, None
    if declared and km > DECLARED_CITY_MAX_KM:
        return ("suspicious", True,
                "SUSPICIOUS (>{:.0f} km declared {} — a city hop does not cross {:.0f} km: "
                "a stop is probably mis-geocoded (read geocache.json display_name), or "
                "the ride is a train/bus/boat/drive/fly and must say so; never swap in a "
                "vehicle mode just to silence this)".format(DECLARED_CITY_MAX_KM, mode,
                                               DECLARED_CITY_MAX_KM), None)
    if not declared and km > SUSPICIOUS_KM:
        return ("suspicious", False,
                "SUSPICIOUS (>{:.0f} km straight-line, no mode — does a vehicle really "
                "run this hop? then declare its mode fly/drive/boat/train/bus on the "
                "arriving stop and give it its legs[] row (add one if the leg has none); "
                "nothing runs it? then the day is "
                "mis-clustered or a stop is mis-geocoded — fix the stop, not the mode)"
                .format(SUSPICIOUS_KM), None)
    walking = mode == "walk" or (not declared and km <= MAX_WALK_KM)
    if walking:
        return ("walk", declared, "walk" + tag,
                "~{} min (est)".format(max(5, r5(km * WALK_MIN_PER_KM))))
    if km > TRANSIT_EST_MAX_KM:
        return ("transit", declared, "TRANSIT" + tag,
                "{:.0f} km · use the operator timetable".format(km))
    mins = km * 6 + 8              # ~6 min/km in-vehicle + access/wait allowance
    return ("transit", declared, "TRANSIT" + tag,
            "~{}-{} min (est)".format(max(15, r5(mins * 0.85)), r5(mins * 1.25)))


def _ll(c):
    return "{:.6f},{:.6f}".format(*c)


def dir_url(provider, o, d, mode, waypoints=None, names=None):
    """One directions deep link. mode is the walking/transit/driving vocabulary:
      amap   mode   walk | bus | car   (uri.amap.com/navigation, keyless; from/to
             carry lon,lat,NAME — 高德 puts the name on the pin). Multi-point:
             via= 途经参数仅驾车(car)且最多 3 个途经点(URI API 限制)——超出或
             非驾车模式抛 ValueError，调用方退回逐跳链接。⚡ via 链接首次使用
             建议浏览器实测一次。
      baidu  mode   walking | transit | driving (api.map.baidu.com/direction,
             keyless; coord_type=wgs84 tells Baidu the input datum — the response
             and pins are handled server-side, no client conversion needed)
             — 无 keyless 多途经形式。
    Mainland-CN points are converted WGS84->GCJ-02 for amap (Baidu does the datum
    shift itself via coord_type) so pins land on the right building."""
    if provider == "amap":
        na, nb = (names or ("起点", "终点"))
        amode = {"walking": "walk", "transit": "bus", "driving": "car"}[mode]
        ga = wgs84_to_gcj02(*o)
        gb = wgs84_to_gcj02(*d)
        url = ("https://uri.amap.com/navigation?from={},{}&to={},{}&mode={}"
               "&src=trip-planner-cn".format(
                   "{:.6f},{:.6f}".format(ga[1], ga[0]),
                   urllib.parse.quote(str(na)[:40], safe=""),
                   "{:.6f},{:.6f}".format(gb[1], gb[0]),
                   urllib.parse.quote(str(nb)[:40], safe=""), amode))
        if waypoints:
            if amode != "car":
                raise ValueError("amap via= 仅驾车模式支持多途经点")
            if len(waypoints) > 3:
                raise ValueError("amap via= 最多 3 个途经点(收到 {} 个)"
                                 .format(len(waypoints)))
            via = ";".join(
                "{:.6f},{:.6f}".format(g[1], g[0])
                for g in (wgs84_to_gcj02(*w) for w in waypoints))
            url += "&via={}".format(urllib.parse.quote(via, safe=""))
        return url
    if provider == "baidu":
        if waypoints:
            raise ValueError("baidu: no keyless multi-point navigation link")
        bmode = {"walking": "walking", "transit": "transit",
                 "driving": "driving"}[mode]
        p = [("origin", "{:.6f},{:.6f}".format(*o)),
             ("destination", "{:.6f},{:.6f}".format(*d)),
             ("mode", bmode),
             ("coord_type", "wgs84"),
             ("output", "html"),
             ("src", "trip-planner-cn")]
        return "https://api.map.baidu.com/direction?" + urllib.parse.urlencode(p)
    raise ValueError("unknown provider {!r}".format(provider))


def in_mainland_china(c):
    """Coarse point-in-polygon (ray casting) against _CN_POLY, then HK/MO/TW cut out."""
    lat, lon = c
    inside = False
    n = len(_CN_POLY)
    for i in range(n):
        y1, x1 = _CN_POLY[i]
        y2, x2 = _CN_POLY[(i + 1) % n]
        if (y1 > lat) != (y2 > lat):
            if x1 + (lat - y1) * (x2 - x1) / (y2 - y1) > lon:
                inside = not inside
    if not inside:
        return False
    return not any(b[0] <= lat <= b[1] and b[2] <= lon <= b[3] for b in _NOT_MAINLAND)


_LATIN_WORD = re.compile(r"[A-Za-zÀ-ɏ]{4,}")
_CJK_RUN = re.compile(r"[぀-ヿ㐀-鿿]{2,}")


def _fold(s):
    """Casefold + strip diacritics ('Gōra' -> 'gora') so a macron/accent difference
    between the query and the geocoder's display_name is not a false alarm."""
    import unicodedata
    return "".join(ch for ch in unicodedata.normalize("NFKD", str(s))
                   if not unicodedata.combining(ch)).casefold()


def head_token(query):
    """The query's head token: the first CJK run (its first 2 chars) if the query
    starts with CJK, else the first Latin word of >=4 letters. None when neither
    exists (pure digits, 3-letter names) — then no check is possible."""
    q = str(query).strip()
    m_cjk, m_lat = _CJK_RUN.search(q), _LATIN_WORD.search(q)
    if m_cjk and (not m_lat or m_cjk.start() < m_lat.start()):
        return m_cjk.group(0)[:2]
    if m_lat:
        return m_lat.group(0)
    return None


def head_token_missing(query, display_name):
    """True when display_name does not contain the query's head token (Japan F5:
    'Gōra Station' resolved to a point 600 m from the station and nothing said so).
    Only a hint — the address of the right place can legitimately lack the token."""
    tok = head_token(query)
    if not tok or not display_name:
        return False
    return _fold(tok) not in _fold(display_name)


def _load_dotenv():
    """Load KEY=VALUE lines from a git-ignored .env (skill root, then CWD) into
    os.environ WITHOUT overwriting anything already set — so AMAP_KEY can live
    next to the skill and never inside the repo (see .gitignore: `.env`)."""
    here = Path(__file__).resolve().parent.parent
    for p in (here / ".env", Path.cwd() / ".env"):
        try:
            if not p.is_file():
                continue
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
        except OSError:
            continue


def _geocode_amap(q, key):
    """One 高德 restapi geocode. Returns a WGS84 hit dict or None (no result).
    Raises on network/HTTP errors so the caller can fall back to 百度.
    The hit keeps the geocoder's citycode — `check --live` needs it to pick the
    real transit API for same-city hops (driving otherwise)."""
    url = ("https://restapi.amap.com/v3/geocode/geo?output=json&limit=1&key="
           + urllib.parse.quote(key) + "&address=" + urllib.parse.quote(q))
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA),
                                timeout=20) as r:
        data = json.loads(r.read().decode("utf-8"))
    if not isinstance(data, dict) or data.get("status") != "1":
        return None
    geos = data.get("geocodes") or []
    if not geos or not geos[0].get("location"):
        return None
    lon_s, lat_s = geos[0]["location"].split(",")
    wlat, wlon = gcj02_to_wgs84(float(lat_s), float(lon_s))
    return {"lat": wlat, "lon": wlon, "citycode": geos[0].get("citycode", ""),
            "display_name": "{}  [高德 GCJ02→WGS84]".format(
                geos[0].get("formatted_address", ""))}


def _geocode_baidu(q, ak):
    """One 百度 geocoding v3 call. Returns a WGS84 hit dict or None."""
    url = ("https://api.map.baidu.com/geocoding/v3/?output=json&ret_coordtype"
           "=gcj2ll&ak=" + urllib.parse.quote(ak) + "&address="
           + urllib.parse.quote(q))
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA),
                                timeout=20) as r:
        data = json.loads(r.read().decode("utf-8"))
    if not isinstance(data, dict) or data.get("status") != 0:
        return None
    res = data.get("result") or {}
    loc = res.get("location") or {}
    if not loc:
        return None
    wlat, wlon = gcj02_to_wgs84(float(loc["lat"]), float(loc["lon"]))
    return {"lat": wlat, "lon": wlon,
            "display_name": "{}  [百度 GCJ02→WGS84]".format(
                res.get("formatted_address", ""))}


def _amap_direction_minutes(ca, cb, mode, key, city=""):
    """Real minutes for one hop from the 高德 direction API (Web 服务 key).
    ca/cb are the plan's WGS84 coords; converted to GCJ-02 for the call.
    mode: 'drive' -> v3/direction/driving; 'transit' -> v3/direction/transit/
    integrated with city=origin citycode (跨城/未知 citycode 由调用方降级为
    driving 折算，此处不猜). Returns int minutes, or None when the API answers
    but offers no route. Raises on network/HTTP/API-status errors."""
    go, to = wgs84_to_gcj02(*ca), wgs84_to_gcj02(*cb)
    if mode == "transit":
        url = ("https://restapi.amap.com/v3/direction/transit/integrated"
               "?output=json&strategy=0&city=" + urllib.parse.quote(city)
               + "&origin=" + _ll((go[1], go[0]))
               + "&destination=" + _ll((to[1], to[0]))
               + "&key=" + urllib.parse.quote(key))
    else:
        url = ("https://restapi.amap.com/v3/direction/driving?output=json"
               "&origin=" + _ll((go[1], go[0]))
               + "&destination=" + _ll((to[1], to[0]))
               + "&key=" + urllib.parse.quote(key))
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA),
                                timeout=20) as r:
        data = json.loads(r.read().decode("utf-8"))
    if not isinstance(data, dict) or data.get("status") != "1":
        # New keys intermittently answer CUQPS_HAS_EXCEEDED_THE_LIMIT even to a
        # sequential caller — wait briefly and retry once before giving up.
        if "QPS" in str(data.get("info", "")):
            time.sleep(0.8)
            with urllib.request.urlopen(
                    urllib.request.Request(url, headers=UA), timeout=20) as r:
                data = json.loads(r.read().decode("utf-8"))
        if not isinstance(data, dict) or data.get("status") != "1":
            raise RuntimeError("amap direction status={!r} info={}".format(
                data.get("status"), data.get("info", "")))
    route = data.get("route") or {}
    if mode == "transit":
        transits = route.get("transits") or []
        for t in transits:
            try:
                return max(1, int(round(int(t.get("duration", 0)) / 60.0)))
            except (TypeError, ValueError):
                continue
        return None
    paths = route.get("paths") or []
    if not paths:
        return None
    try:
        return max(1, int(round(int(paths[0].get("duration", 0)) / 60.0)))
    except (TypeError, ValueError):
        return None


# Structured routes are an optional v2 layer. The minute-only API above stays
# untouched for `check --live`; this separate path never changes its selection or
# cross-city behaviour. No request URL is ever returned in a route record.
ROUTE_MODES = ("walk", "transit", "drive")
ROUTE_CACHE_PREFIX = "route-v2|"
ROUTE_PATH_MAX = 160
ROUTE_SEGMENT_PATH_MAX = 60


class RouteRequestError(RuntimeError):
    """A sanitised, classified request failure (never carries the request URL)."""
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


def _utc_now():
    return (_dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)
            .isoformat().replace("+00:00", "Z"))


def _amap_route_request(url, retries=1, timeout=15):
    """GET one route response; retry only transient failures, with a hard cap."""
    last = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA),
                                        timeout=timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except TimeoutError as ex:
            last = RouteRequestError("timeout", "amap direction request timed out")
            if attempt < retries:
                continue
            raise last from ex
        except urllib.error.HTTPError as ex:
            kind = "qps" if ex.code == 429 else "http"
            last = RouteRequestError(kind, "amap direction HTTP {}".format(ex.code))
            if attempt < retries and (ex.code == 429 or ex.code >= 500):
                time.sleep(0.8)
                continue
            raise last from ex
        except urllib.error.URLError as ex:
            reason = getattr(ex, "reason", None)
            kind = ("timeout" if "timed out" in str(reason).lower() else "network")
            last = RouteRequestError(kind, "amap direction {} error".format(kind))
            if attempt < retries:
                continue
            raise last from ex
        except (UnicodeDecodeError, ValueError) as ex:
            raise RouteRequestError(
                "invalid_response", "amap direction returned invalid JSON") from ex
        if not isinstance(data, dict):
            raise RouteRequestError(
                "invalid_response", "amap direction returned a non-object response")
        if data.get("status") != "1":
            info = str(data.get("info") or data.get("infocode") or "unknown")[:160]
            secret = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("key", [""])[0]
            if secret:
                info = info.replace(secret, "[redacted]")
            info = re.sub(r"https?://\S+", "[redacted URL]", info)
            kind = "qps" if "QPS" in info.upper() else "api"
            last = RouteRequestError(kind, "amap direction API error: " + info)
            if kind == "qps" and attempt < retries:
                time.sleep(0.8)
                continue
            raise last
        return data
    raise last or RouteRequestError("network", "amap direction request failed")


def _positive_number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _route_metrics(raw):
    distance = _positive_number(raw.get("distance"))
    seconds = _positive_number(raw.get("duration"))
    out = {}
    if distance is not None:
        out["distance_m"] = int(round(distance))
    if seconds is not None:
        out["duration_min"] = max(1, int(round(seconds / 60.0)))
    return out


def _station_count(value):
    if isinstance(value, bool):
        return None
    try:
        count = int(value)
    except (TypeError, ValueError):
        return None
    return count if count >= 0 else None


def _route_cost(value, kind):
    if isinstance(value, (list, tuple, dict)):
        return None
    try:
        amount = float(value)
        if not math.isfinite(amount) or amount < 0:
            return None
        if amount.is_integer():
            amount = int(amount)
    except (TypeError, ValueError):
        return None
    return {"amount": amount, "currency": "CNY", "kind": kind}


def _point_segment_distance(point, start, end):
    """Small equirectangular distance used only for path simplification."""
    lat, lon = point
    scale = math.cos(math.radians((start[0] + end[0] + lat) / 3.0))
    px, py = lon * scale, lat
    ax, ay = start[1] * scale, start[0]
    bx, by = end[1] * scale, end[0]
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) /
                     float(dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def simplify_route_path(points, max_points=ROUTE_PATH_MAX, protected=()):
    """Douglas-Peucker-style cap preserving endpoints and supplied key points."""
    pts = [(float(p[0]), float(p[1])) for p in points]
    max_points = max(2, int(max_points))
    if len(pts) <= max_points:
        return [[round(lat, 7), round(lon, 7)] for lat, lon in pts]
    lookup = {tuple(p): i for i, p in enumerate(pts)}
    mandatory = {0, len(pts) - 1}
    for point in protected:
        try:
            key = (float(point[0]), float(point[1]))
        except (TypeError, ValueError, IndexError):
            continue
        if key in lookup:
            mandatory.add(lookup[key])
    if len(mandatory) > max_points:
        ordered = sorted(mandatory)
        mandatory = {0, len(pts) - 1}
        for i in range(1, max_points - 1):
            mandatory.add(ordered[round(i * (len(ordered) - 1) / (max_points - 1))])
    keep = set(mandatory)
    while len(keep) < max_points:
        ordered = sorted(keep)
        best = None
        for left, right in zip(ordered, ordered[1:]):
            for idx in range(left + 1, right):
                distance = _point_segment_distance(pts[idx], pts[left], pts[right])
                if best is None or distance > best[0]:
                    best = (distance, idx)
        if best is None or best[0] <= 1e-14:
            break
        keep.add(best[1])
    return [[round(pts[i][0], 7), round(pts[i][1], 7)] for i in sorted(keep)]


def _route_polyline(raw):
    """AMap GCJ-02 'lon,lat;…' -> bounded WGS84 [[lat,lon],…]."""
    if not isinstance(raw, str) or not raw.strip():
        return []
    points = []
    for token in raw.split(";"):
        try:
            lon, lat = token.split(",", 1)
            point = gcj02_to_wgs84(float(lat), float(lon))
        except (TypeError, ValueError):
            continue
        if not points or point != points[-1]:
            points.append(point)
    return simplify_route_path(points, ROUTE_SEGMENT_PATH_MAX) if len(points) >= 2 else []


def _paths_from_steps(steps):
    if not isinstance(steps, list) or not steps:
        return [], False
    paths = []
    complete = True
    for step in steps:
        if not isinstance(step, dict):
            complete = False
            continue
        path = _route_polyline(step.get("polyline"))
        if path:
            paths.append(path)
        else:
            complete = False
    return paths, complete and bool(paths)


def _combine_route_paths(paths):
    if not paths or any(not path for path in paths):
        return []
    combined, protected = [], []
    for path in paths:
        if combined and path[0] == combined[-1]:
            combined.extend(path[1:])
        else:
            combined.extend(path)
        protected.extend((path[0], path[-1]))
    if len(combined) < 2:
        return []
    return simplify_route_path(combined, ROUTE_PATH_MAX, protected)


def _stop_name(raw):
    return str(raw.get("name")) if isinstance(raw, dict) and raw.get("name") else None


def _put_known(target, key, value):
    if value is not None and value != "":
        target[key] = value


def _transit_segments(raw_segments):
    segments = []
    if not isinstance(raw_segments, list):
        return segments, False
    path_complete = bool(raw_segments)
    for raw in raw_segments:
        if not isinstance(raw, dict):
            path_complete = False
            continue
        walking = raw.get("walking") or {}
        bus = raw.get("bus") or {}
        railway = raw.get("railway") or {}
        segment = None
        if isinstance(walking, dict) and walking:
            paths, complete = _paths_from_steps(walking.get("steps"))
            path = _combine_route_paths(paths) if complete else []
            segment = {"mode": "walk"}
            segment.update(_route_metrics(walking))
            if path:
                segment["path"] = path
        elif isinstance(bus, dict) and (bus.get("buslines") or []):
            lines = [line for line in (bus.get("buslines") or [])
                     if isinstance(line, dict)]
            if not lines:
                path_complete = False
                continue
            def line_rank(line):
                metrics = _route_metrics(line)
                return (0 if len(metrics) == 2 else 1,
                        metrics.get("duration_min", float("inf")),
                        metrics.get("distance_m", float("inf")),
                        str(line.get("name") or ""))
            line = sorted(lines, key=line_rank)[0]
            segment = {"mode": "transit"}
            segment.update(_route_metrics(line))
            _put_known(segment, "line", line.get("name"))
            # Direction is copied only when explicitly supplied; arrival_stop is
            # not repurposed as a guessed direction.
            _put_known(segment, "direction", line.get("direction") or bus.get("direction"))
            _put_known(segment, "from", _stop_name(line.get("departure_stop")))
            _put_known(segment, "to", _stop_name(line.get("arrival_stop")))
            _put_known(segment, "station_count", _station_count(line.get("via_num")))
            path = _route_polyline(line.get("polyline"))
            if path:
                segment["path"] = path
        elif isinstance(railway, dict) and railway:
            segment = {"mode": "transit"}
            railway_metrics = dict(railway)
            if "duration" not in railway_metrics and "time" in railway_metrics:
                railway_metrics["duration"] = railway_metrics["time"]
            segment.update(_route_metrics(railway_metrics))
            _put_known(segment, "line", railway.get("name"))
            _put_known(segment, "direction", railway.get("direction"))
            _put_known(segment, "from", _stop_name(railway.get("departure_stop")))
            _put_known(segment, "to", _stop_name(railway.get("arrival_stop")))
            railway_count = _station_count(railway.get("via_num"))
            if railway_count is None and isinstance(railway.get("via_stop"), list):
                railway_count = len(railway["via_stop"])
            _put_known(segment, "station_count", railway_count)
            path = _route_polyline(railway.get("polyline"))
            if path:
                segment["path"] = path
        if segment is None:
            path_complete = False
            continue
        if len(segment) > 1:
            segments.append(segment)
        if not segment.get("path") and len(segment) > 1:
            path_complete = False
    return segments, path_complete and bool(segments)


def _transit_metrics(candidate, segments):
    record = _route_metrics(candidate)
    # The integrated API normally omits total distance. Sum only when EVERY
    # retained segment has an explicit distance; otherwise leave it unknown.
    if "distance_m" not in record and segments and all(
            segment.get("distance_m") for segment in segments):
        record["distance_m"] = sum(segment["distance_m"] for segment in segments)
        record["distance_basis"] = "segments_sum"
    return record


def _candidate_rank(record, mode, strategy):
    complete = record.get("distance_m") and record.get("duration_min")
    primary = ()
    if mode == "transit" and strategy == 1:       # most economical
        cost = record.get("cost") or {}
        primary = (cost.get("amount", float("inf")),)
    elif mode == "transit" and strategy == 2:     # fewest transfers
        segments = record.get("segments")
        primary = (sum(1 for segment in segments
                       if segment.get("mode") == "transit") if segments
                   else float("inf"),)
    elif mode == "transit" and strategy == 3:     # least walking
        segments = record.get("segments")
        walking = [segment.get("distance_m") for segment in segments or []
                   if segment.get("mode") == "walk"]
        primary = (float("inf") if not segments
                   or any(distance is None for distance in walking) else sum(walking),)
    elif mode == "drive" and strategy in (1, 7, 14, 16, 17, 18):
        primary = ((record.get("cost") or {}).get("amount", float("inf")),)
    return ((0 if complete else 1,) + primary +
            (record.get("duration_min", float("inf")),
             record.get("distance_m", float("inf")),
             0 if record.get("path") else 1,
             json.dumps(record.get("segments") or [], ensure_ascii=False, sort_keys=True)))


def _amap_direction_route(ca, cb, mode, key, city="", strategy=0,
                          departure_date="", departure_time=""):
    """Return one structured AMap route for WGS84 endpoints.

    Candidates within the requested AMap strategy are ranked by complete metrics,
    duration and distance — never blindly first. All returned coordinates are
    WGS84. No-route is data; request failures raise RouteRequestError so the
    caller can isolate just this hop.
    """
    if mode not in ROUTE_MODES:
        raise ValueError("unsupported structured route mode {!r}".format(mode))
    go, to = wgs84_to_gcj02(*ca), wgs84_to_gcj02(*cb)
    params = [("output", "json"),
              ("origin", _ll((go[1], go[0]))),
              ("destination", _ll((to[1], to[0])))]
    if mode == "transit":
        endpoint = "https://restapi.amap.com/v3/direction/transit/integrated"
        params.extend((("strategy", str(strategy)), ("city", city)))
        if departure_date and departure_time:
            params.extend((("date", departure_date), ("time", departure_time)))
    elif mode == "walk":
        endpoint = "https://restapi.amap.com/v3/direction/walking"
    else:
        endpoint = "https://restapi.amap.com/v3/direction/driving"
        params.append(("strategy", str(strategy)))
    params.append(("key", key))
    data = _amap_route_request(endpoint + "?" + urllib.parse.urlencode(params))
    route = data.get("route") or {}
    records = []
    if mode == "transit":
        for candidate in route.get("transits") or []:
            if not isinstance(candidate, dict):
                continue
            segments, path_complete = _transit_segments(candidate.get("segments"))
            record = _transit_metrics(candidate, segments)
            cost = _route_cost(candidate.get("cost"), "transit_fare")
            if cost:
                record["cost"] = cost
            if segments:
                record["segments"] = segments
            if path_complete:
                path = _combine_route_paths([s["path"] for s in segments])
                if path:
                    record["path"] = path
            if any(field in record for field in ("distance_m", "duration_min", "segments", "path")):
                records.append(record)
    else:
        for candidate in route.get("paths") or []:
            if not isinstance(candidate, dict):
                continue
            record = _route_metrics(candidate)
            paths, path_complete = _paths_from_steps(candidate.get("steps"))
            if path_complete:
                path = _combine_route_paths(paths)
                if path:
                    record["path"] = path
            if mode == "drive":
                cost = _route_cost(candidate.get("tolls"), "road_tolls")
                if cost:
                    record["cost"] = cost
            if any(field in record for field in ("distance_m", "duration_min", "path")):
                records.append(record)
    base = {"mode": mode, "provider": "amap", "queried_at": _utc_now()}
    if not records:
        base.update({"status": "no_route", "source": "estimated",
                     "error": {"kind": "no_route",
                               "message": "provider returned no usable route"}})
        return base
    best = dict(sorted(records, key=lambda record: _candidate_rank(
        record, mode, strategy))[0])
    best.update(base)
    best["source"] = "live"
    missing = [key for key in ("distance_m", "duration_min") if key not in best]
    if missing:
        best["status"] = "partial"
        best["error"] = {"kind": "missing_fields", "fields": missing}
    else:
        best["status"] = "ok"
    return best


def _amap_around(c, kw, key, radius=1000, limit=10):
    """高德 place/around POI search. c is WGS84 (lat, lon); returns a list of
    {name, type, address, dist_m, lat, lon(WGS84)} sorted by the API's own
    distance order. Raises on network/HTTP/API-status errors."""
    g = wgs84_to_gcj02(*c)
    url = ("https://restapi.amap.com/v3/place/around?output=json&extensions=base"
           "&location=" + _ll((g[1], g[0]))
           + "&keywords=" + urllib.parse.quote(kw or "")
           + "&radius={}&offset={}&page=1".format(int(radius), min(25, max(1, limit)))
           + "&key=" + urllib.parse.quote(key))
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA),
                                timeout=20) as r:
        data = json.loads(r.read().decode("utf-8"))
    if not isinstance(data, dict) or data.get("status") != "1":
        if "QPS" in str(data.get("info", "")):   # new-key throttle; retry once
            time.sleep(0.8)
            with urllib.request.urlopen(
                    urllib.request.Request(url, headers=UA), timeout=20) as r:
                data = json.loads(r.read().decode("utf-8"))
        if not isinstance(data, dict) or data.get("status") != "1":
            raise RuntimeError("amap place/around status={!r} info={}".format(
                data.get("status"), data.get("info", "")))
    out = []
    for p in (data.get("pois") or [])[:limit]:
        loc = (p.get("location") or "").split(",")
        if len(loc) != 2:
            continue
        try:
            lat, lon = gcj02_to_wgs84(float(loc[1]), float(loc[0]))
        except ValueError:
            continue
        out.append({"name": p.get("name", ""), "type": p.get("type", ""),
                    "address": p.get("address", "") or "",
                    "dist_m": p.get("distance", ""), "lat": lat, "lon": lon})
    return out


def cmd_geocode(args):
    plan = load_plan(args.plan)
    src = Path(args.plan)
    out = (src if src.stem.lower().endswith(".geo")
           else src.with_name(src.stem + ".geo.json"))

    # Carry over coordinates already present in the output file. The NOT FOUND
    # message tells people to hand-fill lat/lon there; silently clobbering that
    # work on the next run would make the advice a trap.
    kept = 0
    carried = set()
    if out != src and out.exists():
        prev = read_json(out, "previous geocode output")
        known = {}
        for day in (prev.get("days") or []):
            for s in (day.get("stops") or []):
                if isinstance(s, dict) and s.get("lat") is not None:
                    known[s.get("query") or s.get("name")] = (s["lat"], s["lon"])
        for day in plan["days"]:
            for s in day["stops"]:
                key = s.get("query") or s.get("name")
                if coords(s) is None and key in known:
                    s["lat"], s["lon"] = known[key]
                    carried.add(id(s))
                    kept += 1
    if kept:
        print("kept {} hand-entered/previous coordinate(s) from {}".format(
            kept, out.name))

    cache_path = src.parent / "geocache.json"
    cache = read_json(cache_path, "geocache") if cache_path.exists() else {}
    misses = []
    def show(stop, source, detail=""):
        # Every stop prints its provenance, so a wrong-city hit is caught here rather
        # than by the >12 km heuristic in `check` three steps later.
        print("  {:22.22} {:.5f},{:.5f}  [{}] {}".format(
            stop["name"], stop["lat"], stop["lon"], source, detail[:60]))

    for day in plan["days"]:
        for stop in day["stops"]:
            if coords(stop):
                show(stop, "carried" if id(stop) in carried else "preset")
                continue
            q = stop.get("query") or stop["name"]
            hit = cache.get(q)
            from_cache = bool(hit)
            if not hit:
                amap_key = (getattr(args, "amap_key", None)
                            or os.environ.get("AMAP_KEY") or "").strip()
                baidu_ak = (getattr(args, "baidu_ak", None)
                            or os.environ.get("BAIDU_MAP_AK") or "").strip()
                if amap_key:
                    # 境内默认:高德 restapi first (fast and reliable; result GCJ-02,
                    # converted back to WGS84 above).
                    try:
                        hit = _geocode_amap(q, amap_key)
                    except Exception as ex:
                        print("  amap geocode failed on {!r}: {}".format(q, type(ex).__name__))
                        hit = None
                if hit is None and baidu_ak:
                    # 百度 geocoding v3 fallback (also GCJ-02 -> WGS84).
                    try:
                        hit = _geocode_baidu(q, baidu_ak)
                    except Exception as ex:
                        print("  baidu geocode failed on {!r}: {}".format(q, type(ex).__name__))
                        hit = None
                if hit is None:
                    # Negative results are NOT cached: a miss is almost always a bad
                    # query string, and the fix is to re-query — caching null would
                    # make the retry silently impossible.
                    if not amap_key and not baidu_ak:
                        misses.append(q + "  (未设置 AMAP_KEY — 免费申请三步见 "
                                        "references/cn-domestic.md §5: lbs.amap.com "
                                        "注册→支付宝实名→创建应用加 Web服务 key, 然后重跑 "
                                        "geocode; 不配置也可用百度回退/手工坐标)")
                    else:
                        misses.append(q)
                    continue
                cache[q] = hit
                cache_path.write_text(
                    json.dumps(cache, ensure_ascii=False, indent=1),
                    encoding="utf-8")
            stop["lat"], stop["lon"] = hit["lat"], hit["lon"]
            show(stop, "cache" if from_cache else "api",
                 hit.get("display_name", ""))
            if head_token_missing(q, hit.get("display_name", "")):
                warn("{!r} -> {!r}: the result does not mention {!r} — it may have "
                     "landed on the wrong place (a bus stop / guesthouse nearby). "
                     "Open {:.5f},{:.5f} on a map and check; hand-fill lat/lon if "
                     "off.".format(q, hit.get("display_name", "")[:60], head_token(q),
                                   hit["lat"], hit["lon"]))

    out.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    print("wrote", out)
    if misses:
        print("NOT FOUND — cheapest fix first: re-query with the local-language name "
              "and drop the neighborhood token (e.g. '八坂神社, 京都市東山区'). Only if "
              "that fails, open the place in Google Maps, copy the place-card "
              "coordinates, and hand-fill lat/lon into {} (re-running geocode keeps "
              "them):".format(out.name))
        for m in misses:
            print("  -", m)


def anchor_rows(day):
    return [it for it in (day.get("timeline") or [])
            if isinstance(it, dict) and it.get("kind") == "anchor"]


def _live_setup(args):
    """Resolve `check --live` prerequisites -> (key, routecache, cache_path,
    geocache). key is empty when --live is absent or no AMAP_KEY — the caller
    then silently keeps straight-line estimates."""
    if not getattr(args, "live", False):
        return "", {}, None, {}
    key = (getattr(args, "amap_key", None)
           or os.environ.get("AMAP_KEY") or "").strip()
    if not key:
        warn("--live 需要 AMAP_KEY（免费申请三步见 references/cn-domestic.md §5）"
             "或 --amap-key 传入；本次仍用直线估算")
        return "", {}, None, {}
    src = Path(args.plan)
    cp = src.parent / "routecache.json"
    rc = read_json(cp, "routecache") if cp.exists() else {}
    gp = src.parent / "geocache.json"
    geo = read_json(gp, "geocache") if gp.exists() else {}
    return key, rc, cp, geo


def _live_dur(ca, cb, cat, sa, sb, key, routecache, geo, cache_path):
    """Cached 高德 real minutes for one hop, rendered as a dur string, or None
    (caller keeps the straight-line estimate). transit: the real transit API
    only when BOTH endpoints' citycodes are known and EQUAL (a city ride — the
    integrated API is city-scoped); otherwise driving minutes ×1.25 labelled
    折算. drive: driving API as-is. Only successful lookups are cached
    (routecache.json, WGS84@4dp key) — failures are never negative-cached, so a
    transient network error is retried on the next run."""
    qa = sa.get("query") or sa.get("name") or ""
    qb = sb.get("query") or sb.get("name") or ""
    ckey = "{}|{:.4f},{:.4f}|{:.4f},{:.4f}".format(cat, ca[0], ca[1], cb[0], cb[1])
    cached = routecache.get(ckey)
    mins = tag = None
    if cached is not None:
        mins, tag = cached
    if mins is None:
        try:
            if cat == "transit":
                city_a = (geo.get(qa) or {}).get("citycode", "")
                city_b = (geo.get(qb) or {}).get("citycode", "")
                if city_a and city_a == city_b:
                    mins = _amap_direction_minutes(ca, cb, "transit", key,
                                                   city=city_a)
                    tag = "公交实时"
                else:
                    mins = _amap_direction_minutes(ca, cb, "drive", key)
                    if mins is not None:
                        mins = r5(mins * 1.25)
                    tag = "驾车折算"
            else:
                mins = _amap_direction_minutes(ca, cb, "drive", key)
                tag = "驾车实时"
        except Exception as ex:
            warn("高德实时时距调用失败({}) — 本跳回退直线估算".format(
                type(ex).__name__))
            return None
        if mins is None:
            return None
        routecache[ckey] = [mins, tag]
        if cache_path is not None:
            try:
                cache_path.write_text(
                    json.dumps(routecache, ensure_ascii=False, indent=1),
                    encoding="utf-8")
            except OSError:
                pass
    if not mins:
        return None
    return "~{} min (高德{})".format(mins, tag)


def cmd_check(args):
    plan = load_plan(args.plan)
    bad_days = 0
    live_key, routecache, cache_path, geo = _live_setup(args)
    for i, day in enumerate(plan["days"], 1):
        print("\nDay {} {} — {}".format(i, day.get("date", "?"),
                                        day.get("label", "")))
        pts = [(s, coords(s)) for s in day["stops"]]
        if len(pts) < 2:
            print("  (no hops to check — {} mapped stop(s))".format(len(pts)))
            continue
        walk = {"declared": 0.0, "guessed": 0.0}
        ride = {"declared": 0.0, "guessed": 0.0}
        rides = 0
        long_hops = []
        worst = "OK"
        for (a, ca), (b, cb) in zip(pts, pts[1:]):
            if not ca or not cb:
                print("  {} -> {}: missing coords — run geocode or fill by hand"
                      .format(a["name"], b["name"]))
                worst = "BROKEN"
                continue
            km = haversine_km(ca, cb)
            cat, declared, verdict, dur = hop_estimate(km, b.get("mode"))
            if live_key and cat in ("transit", "drive"):
                live = _live_dur(ca, cb, cat, a, b, live_key, routecache, geo,
                                 cache_path)
                if live:
                    dur = live
            bucket = "declared" if declared else "guessed"
            if cat == "suspicious":
                worst = "SUSPICIOUS"
            elif cat == "walk":
                walk[bucket] += km
            else:
                ride[bucket] += km
                rides += 1
                if cat in LONG_MODES:
                    long_hops.append("{} {:.0f} km".format(cat, km))
            print("  {:22.22} -> {:22.22} {:6.1f} km (straight-line)  {}{}".format(
                a["name"], b["name"], km, verdict,
                "  " + dur if dur else ""))
        note = []
        if worst != "OK":
            note.append(worst + " HOPS PRESENT")
            bad_days += 1
        walk_km = walk["declared"] + walk["guessed"]
        if walk_km * 1.3 > DAY_WALK_FLAG_KM:
            note.append("ALREADY OVER {:.0f} km ON FOOT — re-cluster".format(
                DAY_WALK_FLAG_KM))
        print("  on foot: {:.1f} km (declared {:.1f} · guessed {:.1f}) → ≈{:.1f} km "
              "with real streets (est)".format(walk_km, walk["declared"],
                                               walk["guessed"], walk_km * 1.3))
        if rides:
            print("  ridden:  {:.1f} km over {} hop(s) (declared {:.1f} · guessed "
                  "{:.1f}) — not walking{}".format(
                      ride["declared"] + ride["guessed"], rides, ride["declared"],
                      ride["guessed"],
                      "  [long legs: " + ", ".join(long_hops) + "]"
                      if long_hops else ""))
        n_anchor = len(anchor_rows(day))
        if walk_km == 0 and n_anchor >= 2:
            if ride["guessed"] == 0:
                # Every hop mode is explicitly declared — nothing was guessed, so
                # 0 km on foot is a deliberate all-rides day, not the AU F2 trap.
                print("  Day {} {}: 0 km on foot, {} anchor rows — all rides "
                      "declared — fine if intended.".format(
                          i, day.get("date", "?"), n_anchor))
            else:
                # AU F2: two 2 km coastal-walk hops were guessed as transit and the day
                # showed 0 km on foot without a word. Anchors imply walking between them.
                warn("Day {} {}: 0 km on foot but {} anchor rows — a walking hop is "
                     "probably guessed as transit; declare \"mode\": \"walk\" on the "
                     "stop it walks INTO (or the day really is all rides — then fine)."
                     .format(i, day.get("date", "?"), n_anchor))
        print("  + in-venue walking and strolls: add your own; the {:.0f} km cap is "
              "on the SUM — {}".format(DAY_WALK_FLAG_KM,
                                       "; ".join(note) if note else "OK so far"))
    print("\nNote: distances are straight-line; real streets add ~20-30%. Every "
          "duration is an estimate (est) — browser-verify the load-bearing hops; "
          "transit >{:.0f} km and fly/drive/boat/train/bus hops get no minutes here, "
          "use the operator timetable / legs[].".format(TRANSIT_EST_MAX_KM))
    if live_key:
        print("Note: (高德公交实时/驾车实时/驾车折算) durations came from the 高德 "
              "direction API and are cached in routecache.json; 折算 = driving "
              "minutes ×1.25 (transit API is city-scoped — cross-city hops fall "
              "back to it).")
    if bad_days:
        sys.exit(2)


def _atomic_write_json(path, value):
    """Write JSON beside its destination, then atomically replace the old file."""
    path = Path(path)
    tmp_name = None
    try:
        with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=str(path.parent),
                prefix=path.name + ".", suffix=".tmp", delete=False) as tmp:
            tmp_name = tmp.name
            json.dump(value, tmp, ensure_ascii=False, indent=1)
            tmp.write("\n")
        os.replace(tmp_name, path)
    finally:
        if tmp_name and os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def _structured_route_cache(path, what="结构化路线缓存"):
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as ex:
        warn("{}不可读，忽略并重新查询 ({})".format(what, type(ex).__name__))
        return {}
    if not isinstance(value, dict):
        warn("{}不是对象，忽略并重新查询".format(what))
        return {}
    return value


def _route_cache_key(mode, ca, cb, city_a, city_b, strategy, time_semantics):
    identity = {
        "mode": mode,
        "origin": [round(ca[0], 6), round(ca[1], 6)],
        "destination": [round(cb[0], 6), round(cb[1], 6)],
        "city_a": city_a or "", "city_b": city_b or "",
        "strategy": int(strategy), "time": time_semantics or "",
    }
    return ROUTE_CACHE_PREFIX + json.dumps(
        identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _cached_structured_route(cache, key, ttl, now=None):
    if ttl <= 0:
        return None
    entry = cache.get(key)
    if not isinstance(entry, dict) or not isinstance(entry.get("route"), dict):
        return None
    try:
        cached_at = float(entry["cached_at"])
    except (KeyError, TypeError, ValueError):
        return None
    now = time.time() if now is None else now
    if now < cached_at - 300 or now - cached_at >= ttl:
        return None
    route = json.loads(json.dumps(entry["route"]))
    if route.get("status") not in ("ok", "partial"):
        return None
    route["source"] = "cache"
    return route


def _cache_structured_route(cache, key, route, now=None):
    if route.get("status") not in ("ok", "partial"):
        return False
    stored = json.loads(json.dumps(route))
    stored["source"] = "live"
    cache[key] = {"cached_at": time.time() if now is None else now,
                  "route": stored}
    return True


def _route_citycode(stop, geocache):
    direct = str(stop.get("citycode") or "").strip()
    if direct:
        return direct
    query = stop.get("query") or stop.get("name") or ""
    return str((geocache.get(query) or {}).get("citycode") or "").strip()


def _route_rows(day, hop_count):
    rows = [item for item in (day.get("timeline") or [])
            if isinstance(item, dict) and item.get("kind") == "hop"
            and item.get("map") is not False]
    return rows if len(rows) == hop_count else []


def _route_departure(day, row):
    """Use only a valid plan date plus an explicit row-start HH:MM, never a guess."""
    try:
        date = _dt.date.fromisoformat(str(day.get("date") or "")).isoformat()
    except ValueError:
        return "", ""
    match = re.match(r"\s*((?:[01]\d|2[0-3]):[0-5]\d)", str(row.get("t") or ""))
    return (date, match.group(1)) if match else ("", "")


def _merge_route_result(existing, result):
    """Keep manual/custom fields; on failure also retain old data for inspection.

    Renderers only consume path/details for ok/partial records, so retained data
    under an error status is never presented as a fresh successful lookup.
    """
    if not isinstance(existing, dict):
        return result
    if result.get("status") not in ("ok", "partial"):
        merged = dict(existing)
        merged.update(result)
        return merged
    standard = {"mode", "distance_m", "distance_basis", "duration_min", "cost", "segments",
                "path", "provider", "queried_at", "status", "error", "source",
                "from", "to"}
    merged = {key: value for key, value in existing.items() if key not in standard}
    merged.update(result)
    return merged


def _route_failure(mode, kind, message):
    return {"mode": mode, "provider": "amap", "queried_at": _utc_now(),
            "status": "error" if kind != "cross_city_transit" else "degraded",
            "source": "estimated",
            "error": {"kind": kind, "message": message}}


def cmd_routes(args):
    """Fetch optional structured walk/transit/drive routes, hop by hop."""
    plan = load_plan(args.plan)
    key = (os.environ.get("AMAP_KEY") or "").strip()
    if not key:
        warn("routes 需要 AMAP_KEY 环境变量；本次不查询、不写回，原估算和深链保持不变")
        return
    src = Path(args.plan)
    cache_path = src.parent / "routecache.json"
    cache = _structured_route_cache(cache_path)
    geocache_path = src.parent / "geocache.json"
    geocache = _structured_route_cache(geocache_path, "地理编码缓存")
    ttl = max(0, int(args.ttl))
    strategy = int(args.strategy)
    cache_dirty = False
    counts = {"ok": 0, "cache": 0, "failed": 0, "skipped": 0}
    for day_index, day in enumerate(plan["days"], 1):
        mapped = [stop for stop in day["stops"] if coords(stop)]
        hop_count = max(0, len(mapped) - 1)
        rows = _route_rows(day, hop_count)
        print("\nDay {} {} — {}".format(day_index, day.get("date", "?"),
                                          day.get("label", "")))
        for hop_index, (origin, destination) in enumerate(zip(mapped, mapped[1:])):
            ca, cb = coords(origin), coords(destination)
            km = haversine_km(ca, cb)
            cat, _, _, estimate = hop_estimate(km, destination.get("mode"))
            if cat not in ROUTE_MODES:
                counts["skipped"] += 1
                continue
            row = rows[hop_index] if rows else {}
            time_semantics = "{}|{}".format(day.get("date", ""), row.get("t", ""))
            city_a = city_b = ""
            if cat == "transit":
                city_a = _route_citycode(origin, geocache)
                city_b = _route_citycode(destination, geocache)
            departure_date, departure_time = (_route_departure(day, row)
                                                 if cat == "transit" else ("", ""))
            cache_key = _route_cache_key(cat, ca, cb, city_a, city_b, strategy,
                                         time_semantics)
            record = _cached_structured_route(cache, cache_key, ttl)
            if record is not None:
                counts["cache"] += 1
                counts["ok"] += 1
            elif cat == "transit" and (not city_a or city_a != city_b):
                # Same degradation boundary as check --live. Unlike the legacy
                # minute display, do not relabel driving data as a transit route.
                record = _route_failure(
                    cat, "cross_city_transit",
                    "transit is city-scoped; missing/different citycodes, original estimate kept")
                counts["failed"] += 1
            else:
                try:
                    record = _amap_direction_route(
                        ca, cb, cat, key, city=city_a, strategy=strategy,
                        departure_date=departure_date, departure_time=departure_time)
                    if record.get("status") == "no_route":
                        counts["failed"] += 1
                    else:
                        counts["ok"] += 1
                        if ttl > 0:
                            cache_dirty = (_cache_structured_route(cache, cache_key, record)
                                           or cache_dirty)
                except RouteRequestError as ex:
                    record = _route_failure(cat, ex.kind, str(ex))
                    counts["failed"] += 1
                except Exception as ex:
                    # A malformed single response must not expose its URL/key or
                    # prevent later hops from being queried.
                    record = _route_failure(
                        cat, "invalid_response",
                        "route parsing failed ({})".format(type(ex).__name__))
                    counts["failed"] += 1
            record["from"] = origin.get("name") or ""
            record["to"] = destination.get("name") or ""
            if args.write:
                destination["route"] = _merge_route_result(
                    destination.get("route"), record)
            metrics = []
            if record.get("distance_m"):
                metrics.append("{} m".format(record["distance_m"]))
            if record.get("duration_min"):
                metrics.append("{} min".format(record["duration_min"]))
            error = record.get("error") or {}
            detail = error.get("kind") or " · ".join(metrics)
            print("  {} -> {}: {} {} [{}]{}".format(
                origin["name"], destination["name"], record["status"],
                record["source"], cat, " — " + str(detail) if detail else ""))
            if estimate and record.get("status") not in ("ok", "partial"):
                print("    original estimate kept: {}".format(estimate))
    if args.write:
        if cache_dirty:
            try:
                _atomic_write_json(cache_path, cache)
            except OSError as ex:
                warn("结构化路线缓存写回失败，不影响计划写回 ({})".format(
                    type(ex).__name__))
        _atomic_write_json(src, plan)
        print("\nupdated {} in place".format(src))
    else:
        print("\ndry run only — pass --write to update route fields and routecache.json")
    print("routes: {} usable ({} from cache), {} degraded/failed, {} skipped".format(
        counts["ok"], counts["cache"], counts["failed"], counts["skipped"]))


def cmd_poi(args):
    """POI 周边搜索: --at a stop name (plan coordinates) or 'lat,lon' (WGS84);
    prints nearby POIs from the 高德 place/around API with WGS84 coords, so a
    found metro entrance / restaurant can be pasted straight into the plan."""
    plan = load_plan(args.plan)
    key = (getattr(args, "amap_key", None)
           or os.environ.get("AMAP_KEY") or "").strip()
    if not key:
        warn("poi 需要 AMAP_KEY（免费申请三步见 references/cn-domestic.md §5: "
             "lbs.amap.com 注册→支付宝实名→创建应用加 Web 服务 key），"
             "或用 --amap-key 传入")
        sys.exit(1)
    at = str(args.at).strip()
    m = re.fullmatch(r"(-?\d+(?:\.\d+)?)[,，]\s*(-?\d+(?:\.\d+)?)", at)
    if m:
        c = (float(m.group(1)), float(m.group(2)))
    else:
        stops = [s for day in plan["days"] for s in day["stops"]]
        fat = _fold(at)
        def _stop_keys(s):
            return {_fold(str(s[k])) for k in ("name", "query")
                    if s.get(k) and str(s[k]).strip()}
        exact = [s for s in stops if fat in _stop_keys(s)]
        sub = [s for s in stops if fat and any(fat in f for f in _stop_keys(s))]
        pick = exact or sub
        if not pick:
            warn("plan 里找不到 stop {!r} — 先跑 geocode，或用 --at 纬度,经度"
                 .format(at))
            sys.exit(1)
        if len(pick) > 1:
            print("  {!r} 命中 {} 个 stop，用第一个：{!r}".format(
                at, len(pick), pick[0].get("name")))
        s = pick[0]
        c = coords(s)
        if not c:
            warn("{!r} 还没有坐标 — 先跑 geocode".format(s.get("name")))
            sys.exit(1)
    if not in_mainland_china(c):
        warn("该坐标疑似境外，高德 POI 仅覆盖中国大陆")
    try:
        pois = _amap_around(c, args.kw, key, radius=args.radius, limit=args.limit)
    except Exception as ex:
        warn("周边搜索失败: {}".format(type(ex).__name__))
        sys.exit(1)
    print("POI 周边 «{}» @ {:.5f},{:.5f} (WGS84) 半径 {}m:".format(
        args.kw or "(不限关键词)", c[0], c[1], args.radius))
    if not pois:
        print("  (无结果 — 换个关键词或加大 --radius；关键词可用 | 分隔多个，"
              "如 地铁站|地铁出入口)")
    for p in pois:
        print("  {:>7}m  {:30.30} {:26.26} {:.5f},{:.5f}  {}".format(
            str(p["dist_m"]), p["name"], p["type"].replace(";", "·")[:26],
            p["lat"], p["lon"], str(p["address"])[:30]))


_SPLIT = re.compile(r"\s*[·/()()\[\]【】,,]\s*|\s+")
# Generic place words in the languages the test runs hit (en/es/it/fr/pt): a token
# from this list can name half the stops of a day ("centro", "mercado", "museo"),
# so it is never evidence that a hop row points at a different stop (Mexico F2).
_STOP_TOKENS = {"the", "and", "beach", "station", "market", "park", "street",
                "hotel", "terminal", "museum", "airport", "wharf", "point", "gorge",
                "river", "lake", "bay", "hall", "tower", "bridge", "square", "road",
                "centro", "centre", "center", "downtown", "plaza", "piazza", "place",
                "mercado", "mercato", "marche", "museo", "musee", "palacio",
                "palazzo", "palace", "catedral", "cathedral", "church", "iglesia",
                "chiesa", "temple", "shrine", "mosque", "castle", "castillo",
                "castello", "garden", "gardens", "jardin", "parque", "parco",
                "port", "puerto", "porto", "harbour", "harbor", "city", "town",
                "north", "south", "east", "west", "island", "gare", "estacion",
                "stazione", "bahnhof", "aeropuerto", "aeroporto", "cafe", "hostel",
                "riad", "medina", "house", "casa", "villa", "monte", "mount"}
_CJK_CHAR = re.compile(r"[぀-ヿ㐀-鿿]")


def name_keys(stop):
    """Weak matching keys for a stop: full name, query, and its meaningful pieces
    (CJK runs of >=2 chars, Latin words of >=4 chars not in a generic stoplist)."""
    full, toks = name_keys2(stop)
    return full | toks


def name_keys2(stop):
    """(full_keys, token_keys): the stop's full name / query, and its pieces
    (CJK runs of >=2 chars, ASCII words of >=4 letters not in _STOP_TOKENS)."""
    full, toks = set(), set()
    for raw in (stop.get("name"), stop.get("query")):
        if not raw:
            continue
        raw = str(raw).strip()
        if len(raw) >= 2:
            full.add(raw)
        for tok in _SPLIT.split(raw):
            tok = tok.strip()
            if not tok:
                continue
            if re.fullmatch(r"[぀-ヿ㐀-鿿]{2,}", tok):
                toks.add(tok)
            elif (len(tok) >= 4 and tok.isascii()
                  and _fold(tok) not in _STOP_TOKENS):
                toks.add(tok)
    return full, toks - full


_KEY_RE = {}


def key_regex(key):
    """Compiled matcher for one key on FOLDED text (see _fold): case-insensitive,
    accent-insensitive, and Latin ends must sit on a word boundary — so the key
    "Mural" no longer hits "Muralismo" (Mexico F2). CJK has no spaces, so an end
    that is a CJK character gets no boundary ("格雷梅" still matches inside a run)."""
    pat = _KEY_RE.get(key)
    if pat is None:
        fk = _fold(key)
        body = re.escape(fk)
        if fk and not _CJK_CHAR.match(fk[0]):
            body = r"(?<!\w)" + body
        if fk and not _CJK_CHAR.match(fk[-1]):
            body = body + r"(?!\w)"
        pat = _KEY_RE[key] = re.compile(body)
    return pat


def foreign_stops(ctx, sa, sb, stops):
    """Names of the OTHER stops of the day that the hop row text `ctx` mentions,
    given the hop's real endpoints sa -> sb. Rules (Mexico F2, Turkey F3):
      1. matching is folded + Latin word-boundary (key_regex);
      2. longest first: a key that is a substring of an endpoint's own name/query is
         NOT a hit — "格雷梅" inside "格雷梅露天博物馆", "centro" inside "Hotel ·
         Oaxaca centro". The other way round (another stop's name CONTAINS the
         endpoint's name and appears in the text) IS a hit;
      3. a token shared by two differently-named stops of the day is generic and
         never counts (only that stop's full name/query can then name it)."""
    fctx = _fold(ctx)
    end_names = {_fold(x) for s in (sa, sb)
                 for x in (s.get("name"), s.get("query")) if x}
    own = {_fold(k) for k in name_keys(sa) | name_keys(sb)}
    tok_owner = {}
    for s in stops:
        for k in name_keys2(s)[1]:
            tok_owner.setdefault(_fold(k), set()).add(_fold(s.get("name")))
    hits = set()
    for s in stops:
        if s is sa or s is sb:
            continue
        full, toks = name_keys2(s)
        for k in full | toks:
            fk = _fold(k)
            if fk in own or any(fk in n for n in end_names):
                continue                                          # rule 2
            if k in toks and len(tok_owner.get(fk, ())) > 1:
                continue                                          # rule 3
            if key_regex(k).search(fctx):
                hits.add(s["name"])
                break
    return sorted(hits)


_ARROW = re.compile(r"\s*(?:→|->|➜|⇒)\s*")
_CTX_CUT = re.compile(r"\s·\s|[((]|\d+(?:\.\d+)?\s*(?:km|分|min)")


def arrow_context(what):
    """The part of a hop row's text that names its endpoints: the segments right
    before and after the arrow ("… · 中央车站→Fish Market 站 · 下车…" -> "中央车站",
    "Fish Market 站"). Rows without an arrow yield the whole text. Restricting the
    mismatch check to this context keeps a street name in a trailing remark
    ("(沿 Esplanade)") from colliding with a stop that happens to carry that word."""
    m = _ARROW.search(what)
    if not m:
        return what
    left = _CTX_CUT.split(what[:m.start()])[-1]
    right = _CTX_CUT.split(what[m.end():])[0]
    return left + " " + right


def cmd_links(args):
    plan = load_plan(args.plan)
    provider = getattr(args, "provider", None) or DEFAULT_PROVIDER
    n_wrote = n_parked = n_susp = n_long = 0
    parked_log = []      # human lines for the closing WARN (Morocco F6)
    print("map provider: {}".format(provider))
    for i, day in enumerate(plan["days"], 1):
        print("\nDay {} {} — {}".format(i, day.get("date", "?"),
                                        day.get("label", "")))
        mapped = [s for s in day["stops"] if coords(s)]
        pts = [(s["name"], coords(s)) for s in mapped]
        skipped = len(day["stops"]) - len(pts)
        if skipped:
            print("  ({} stop(s) skipped: no coords)".format(skipped))
        hops = []   # {a, b, url|None, note, km, mode}
        for (na, ca), (nb, cb), sa, sb in zip(pts, pts[1:], mapped, mapped[1:]):
            km = haversine_km(ca, cb)
            cat, declared, verdict, dur = hop_estimate(km, sb.get("mode"))
            url, note = None, None
            if km > LINK_MAX_KM or cat in ("fly", "boat"):
                # AU F1: a 1971 km travelmode=transit deep link was written into a
                # hop row without a word. Long legs live in legs[] / the operator's
                # own link. The row stays in the count (so the day's alignment
                # holds) — it just gets no link. This is the EXPECTED shape for a
                # rail/flight row whose two stations are in stops[]; only a row
                # whose ends are NOT in stops[] takes map:false (see docstring).
                note = ("long leg ({:.0f} km, {}) — no map link written; row kept "
                        "for alignment (expected, NOT an error — do not add "
                        "map:false), book it via legs[]/operator link".format(km, cat))
                n_long += 1
            else:
                mode = ("walking" if cat == "walk" else
                        "driving" if cat == "drive" else "transit")
                url = dir_url(provider, ca, cb, mode, names=(na, nb))
            hops.append({"a": na, "b": nb, "sa": sa, "sb": sb, "url": url,
                         "note": note, "km": km, "verdict": verdict, "dur": dur})
            print("  {} -> {}  [{} {:.1f} km {}]\n    {}".format(
                na, nb, verdict, km, dur or "", url or note))
        if args.write and hops:
            # Hop rows correspond 1:1, in order, with the stop-to-stop hops — the
            # stops-mirror-the-timeline invariant. A flight/rail row whose stations
            # are NOT in stops[] (covered only by legs[]) carries "map": false and
            # sits outside that invariant (a day with such a flight plus 3 ground
            # hops has 4 hop rows but only 3 mapped hops). A rail row whose two
            # stations ARE in stops[] stays mappable: it is hop N of the day, gets
            # no link (long leg) but keeps the count aligned. Position pairing is
            # still weak, so every write is printed with its place names and
            # checked against the other stops' names (AU F1).
            rows = [it for it in (day.get("timeline") or [])
                    if isinstance(it, dict) and it.get("kind") == "hop"
                    and it.get("map") is not False]
            parked = []
            if len(rows) == len(hops):
                for idx, (row, h) in enumerate(zip(rows, hops), 1):
                    what = str(row.get("what", ""))
                    if h["url"] is None:
                        row.pop("link", None)
                        print("  ✎ row {} “{}” ← {} → {}: {}".format(
                            idx, what[:48], h["a"], h["b"], h["note"]))
                        continue
                    foreign = foreign_stops(arrow_context(what), h["sa"], h["sb"],
                                            mapped)
                    if foreign:
                        n_susp += 1
                        parked.append(h["url"])
                        parked_log.append("Day {} row {} “{}” (names {}; hop is "
                                          "{} → {})".format(
                                              i, idx, what[:40], ", ".join(foreign),
                                              h["a"], h["b"]))
                        row.pop("link", None)
                        warn("Day {} row {} “{}” names {} but the hop in this "
                             "position is {} → {} — SUSPICIOUS mismatch, link parked "
                             "in hop_links, not written. Fix: give every stop-to-stop "
                             "transition its own hop row in order; \"map\": false "
                             "only on lodging rows and on rail/flight rows whose "
                             "stations are NOT in stops[] (both ends in stops[] -> "
                             "keep the row mappable)."
                             .format(i, idx, what[:48], ", ".join(foreign),
                                     h["a"], h["b"]))
                        continue
                    row["link"] = h["url"]
                    n_wrote += 1
                    print("  ✎ row {} “{}” ← {} → {}".format(
                        idx, what[:48], h["a"], h["b"]))
            else:
                parked.extend(h["url"] for h in hops if h["url"])
                if parked:
                    parked_log.append("Day {}: {} mappable hop row(s) vs {} mapped "
                                      "hop(s) — all {} link(s) of the day parked"
                                      .format(i, len(rows), len(hops), len(parked)))
                for h in hops:
                    if h["url"] is None:
                        print("  ✎ {} → {}: {}".format(h["a"], h["b"], h["note"]))
                warn("Day {}: {} mappable hop rows vs {} mapped hops — links parked "
                     "in day['hop_links'] (render_plan shows them as a hop-by-hop maps row). "
                     "To place them on rows: give every stop-to-stop transition its "
                     "own hop row (a rail/flight row whose two stations are in "
                     "stops[] IS one of them — keep it mappable, it just gets no "
                     "link); \"map\": false ONLY on a flight/rail row whose stations "
                     "are not in stops[], and on the lodging->first / last->lodging "
                     "rows (or put the lodging into stops[0]/stops[-1])."
                     .format(i, len(rows), len(hops)))
            n_parked += len(parked)
            # hop_links is script-owned: it holds exactly what this run could not
            # place on a row, so a stale list from an earlier run does not survive.
            if parked:
                day["hop_links"] = parked
            else:
                day.pop("hop_links", None)
        if len(pts) < 2:
            continue
        if provider == "baidu":
            # 百度 keyless URI 无多途经形式。
            print("  DAY CHAIN not available for baidu — per-hop links only")
            continue
        if provider == "amap":
            # via= 多途经深链：驾车模式、每链 ≤3 个途经点（按 4 站一段切链）。
            # 这是整日概览（车行视角），不是步行路线——步行请用上方逐跳链接。
            for s in range(0, len(pts) - 1, 4):
                seg = pts[s:s + 5]
                if len(seg) < 2:
                    break
                seg_hops = [haversine_km(a[1], b[1]) for a, b in zip(seg, seg[1:])]
                if max(seg_hops) > SUSPICIOUS_KM:
                    print("  DAY CHAIN suppressed: a {:.0f} km hop makes a chained "
                          "link meaningless — use the per-hop links above."
                          .format(max(seg_hops)))
                    continue
                mids = [c for _, c in seg[1:-1]]
                try:
                    url = dir_url("amap", seg[0][1], seg[-1][1], "driving",
                                  mids or None)
                except ValueError as exc:
                    print("  DAY CHAIN not available ({}) — per-hop links only"
                          .format(exc))
                    continue
                print("  DAY CHAIN (drive overview) {} -> {} ({} stops):\n    {}"
                      .format(seg[0][0], seg[-1][0], len(seg), url))
                if args.write and s == 0:
                    day["day_map"] = url
            continue
        # Whole-day overview chains. (本境内版仅 amap/baidu, chain 分支保留用于
        # 兼容自定义 provider;google 的 waypoint 行为说明已随 google 移除。)
        for s in range(0, len(pts) - 1, 10):
            seg = pts[s:s + 11]
            if len(seg) < 2:
                break
            seg_hops = [haversine_km(a[1], b[1]) for a, b in zip(seg, seg[1:])]
            if max(seg_hops) > SUSPICIOUS_KM:
                print("  DAY CHAIN suppressed: a {:.0f} km hop makes a chained link "
                      "meaningless — use the per-hop links above.".format(
                          max(seg_hops)))
                continue
            label = ("DAY CHAIN" if max(seg_hops) <= MAX_WALK_KM else
                     "DAY CHAIN (overview only — walking mode, NOT a walking route)")
            url = dir_url(provider, seg[0][1], seg[-1][1], "walking",
                          [c for _, c in seg[1:-1]] or None)
            print("  {} {} -> {} ({} stops):\n    {}".format(
                label, seg[0][0], seg[-1][0], len(seg), url))
            if args.write and s == 0:
                day["day_map"] = url
    if args.write:
        Path(args.plan).write_text(
            json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
        # n_long is NOT a failure counter: those rows are rail/flight/boat hops
        # whose ends are in stops[] — kept for alignment, deliberately unlinked.
        print("\nupdated {} in place — wrote {} link(s) onto hop rows / parked {} in "
              "hop_links / suspicious {}; long legs kept unlinked (expected): {}"
              .format(args.plan, n_wrote, n_parked, n_susp, n_long))
        if n_susp or n_parked:
            print("Read the WARN lines on stderr before rendering.")
        if n_parked:
            # Morocco F6: 13 parked links and the command was "green". Exit code
            # stays 0 (parking is the designed fallback), but it must be visible.
            warn("==== links --write: {} link(s) PARKED in day['hop_links'], NOT on "
                 "hop rows — the page degrades to one hop-by-hop maps row per such day ===="
                 .format(n_parked))
            for line in parked_log:
                warn("  parked: " + line)


def esc(t):
    return (str(t).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def cmd_kml(args):
    plan = load_plan(args.plan)
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>',
             "<name>{}</name>".format(esc(plan.get("trip", "trip")))]
    total, dropped = 0, []
    for i, day in enumerate(plan["days"], 1):
        color = DAY_COLORS[(i - 1) % len(DAY_COLORS)]
        parts.append("<Folder><name>Day {} {} — {}</name>".format(
            i, esc(day.get("date", "")), esc(day.get("label", ""))))
        line, miss = [], []
        for j, s in enumerate(day["stops"], 1):
            c = coords(s)
            if not c:
                miss.append(s["name"])
                continue
            parts.append(
                "<Placemark><name>{}. {}</name>"
                "<Point><coordinates>{:.6f},{:.6f},0</coordinates></Point>"
                "</Placemark>".format(j, esc(s["name"]), c[1], c[0]))
            line.append("{:.6f},{:.6f},0".format(c[1], c[0]))
            total += 1
        if len(line) > 1:
            parts.append(
                "<Placemark><name>Day {} route</name><Style><LineStyle>"
                "<color>{}</color><width>3</width></LineStyle></Style>"
                "<LineString><coordinates>{}</coordinates></LineString>"
                "</Placemark>".format(i, color, " ".join(line)))
        parts.append("</Folder>")
        if miss:
            dropped.append("Day {}: {}".format(i, ", ".join(miss)))
    parts.append("</Document></kml>")
    # Path.open, not write_text(newline=...) — that kwarg is 3.10+.
    with Path(args.out).open("w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(parts))
    print("wrote {} — {} pin(s) — import into Organic Maps / 奥维互动地图等支持 "
          "KML 的应用(收藏点)".format(args.out, total))
    for d in dropped:
        print("  omitted (no coords) — {}".format(d))
    if total == 0:
        print("  WARNING: no pins written — run geocode first.")


# ---------------------------------------------------------------- sun -----------

def solar_events_utc(lat, lon, date, zenith=90.833):
    """NOAA-style sunrise/sunset (UTC datetimes) for one date; ~1-2 min accuracy.
    Used ONLY to sanity-check the API answer, never as the published value.
    zenith 90.833 = sunrise/sunset, 96 = civil twilight. Returns (rise, set) or
    (None, None) for polar day/night."""
    n = date.toordinal() - _dt.date(2000, 1, 1).toordinal() + 0.5 - lon / 360.0
    def _pass(rising):
        # Iterate twice: the second pass uses the improved transit time.
        jd = 2451545.0 + n
        for _ in range(2):
            t = (jd - 2451545.0) / 36525.0
            L0 = (280.46646 + t * (36000.76983 + 0.0003032 * t)) % 360
            M = math.radians((357.52911 + t * (35999.05029 - 0.0001537 * t)) % 360)
            C = ((1.914602 - t * (0.004817 + 0.000014 * t)) * math.sin(M)
                 + (0.019993 - 0.000101 * t) * math.sin(2 * M)
                 + 0.000289 * math.sin(3 * M))
            lam = math.radians(L0 + C - 0.00569
                               - 0.00478 * math.sin(math.radians(125.04 - 1934.136 * t)))
            eps = math.radians(23.439291 - 0.0130042 * t
                               + 0.00256 * math.cos(math.radians(125.04 - 1934.136 * t)))
            dec = math.asin(math.sin(eps) * math.sin(lam))
            y = math.tan(eps / 2) ** 2
            e = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
            L0r = math.radians(L0)
            eqt = 4 * math.degrees(
                y * math.sin(2 * L0r) - 2 * e * math.sin(M)
                + 4 * e * y * math.sin(M) * math.cos(2 * L0r)
                - 0.5 * y * y * math.sin(4 * L0r) - 1.25 * e * e * math.sin(2 * M))
            cosH = ((math.cos(math.radians(zenith))
                     - math.sin(math.radians(lat)) * math.sin(dec))
                    / (math.cos(math.radians(lat)) * math.cos(dec)))
            if cosH < -1 or cosH > 1:
                return None
            H = math.degrees(math.acos(cosH))
            minutes = 720 - 4 * (lon + (H if rising else -H)) - eqt   # NOAA sign
            jd = 2451545.0 + n - 0.5 + minutes / 1440.0
        base = _dt.datetime(date.year, date.month, date.day, tzinfo=_dt.timezone.utc)
        return base + _dt.timedelta(minutes=minutes)
    return _pass(True), _pass(False)


def resolve_tz(day, plan, cli_tz, lon):
    """(ZoneInfo|fixed tzinfo, label, approx:bool). See docstring for the order."""
    name = day.get("tz")
    if not name and cli_tz and cli_tz != "auto":
        name = cli_tz
    if not name:
        name = plan.get("tz") or (plan.get("meta") or {}).get("tz")
    if not name and cli_tz == "auto" and in_mainland_china((35.0, lon)):
        # 境内零配置默认:行程点在中国大陆且未指定任何时区 -> 统一北京时间。
        name = "Asia/Shanghai"
    if name:
        # 境内-only 内置兜底:全国统一北京时间(新疆官方亦用北京时间),无需 tzdata
        # — Windows 缺 tzdata 包时 sun 也能直接写值(境外带 DST 的时区仍走 ZoneInfo)。
        if name in ("Asia/Shanghai", "Asia/Urumqi", "PRC", "Asia/Chongqing",
                    "Asia/Harbin"):
            return (_dt.timezone(_dt.timedelta(hours=8), "CST"), "CST/UTC+8", False)
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(name), name, False
        except Exception as ex:                       # unknown name / no tzdata
            warn("tz {!r} not usable ({}); falling back to a longitude offset"
                 .format(name, ex))
    off = int(round(lon / 15.0))
    return (_dt.timezone(_dt.timedelta(hours=off)),
            "UTC{:+d} (approx from longitude, no DST — pass --tz)".format(off), True)


SUN_DAWN = {"zh": "天亮", "en": "dawn"}   # the renderers accept both spellings


def plan_lang(plan, arg=None):
    """--lang > plan.lang > plan.meta.lang > zh (same order as the theme renderers)."""
    for cand in (arg, plan.get("lang"), (plan.get("meta") or {}).get("lang")):
        if cand in SUN_DAWN:
            return cand
    return "zh"


def cmd_sun(args):
    plan = load_plan(args.plan)
    dawn_word = SUN_DAWN[plan_lang(plan, getattr(args, "lang", None))]
    src = Path(args.plan)
    only = set(x.strip() for x in (args.only or "").split(",") if x.strip())
    failures, written = [], 0   # failures = sanity-check rejections
    skipped = []         # (date, why) — every day this run did NOT write (Italy F5)
    def skip(day_date, head, why):
        skipped.append((day_date or "?", why))
        warn("{}: skipped — {}".format(head, why))
    for i, day in enumerate(plan["days"], 1):
        date = day.get("date")
        head = "Day {} {} — {}".format(i, date or "?", day.get("label", ""))
        if only and date not in only:
            continue
        if not date or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(date)):
            print("{}\n  skipped: no ISO date".format(head))
            skip(date, head, "no ISO date")
            continue
        with_c = [s for s in day["stops"] if coords(s)]
        if not day["stops"]:
            # A day with NO stops at all (pure travel/rest day, e.g. the kyoto
            # sample's day 2) has nothing to compute from — informational, not
            # counted as "not written" (no exit 1 for it).
            print("{}\n  no stops (travel/rest day) — nothing to compute, not "
                  "counted here; add one city-level stop (the airport on a move day): "
                  "plan_lint --strict fails a stop-less day".format(head))
            continue
        if not with_c:
            print("{}\n  skipped: no stop with lat/lon — set one city-level stop (the "
                  "airport on a move day) and re-run --only DATE; plan_lint --strict "
                  "fails the day until it has one".format(head))
            skip(date, head, "no stop with lat/lon — set one city-level stop")
            continue
        # A moving day (travel_day, or the day's first and last stop are more than
        # SUN_MOVE_KM apart) spends its evening at the LAST stop, so the sunset that
        # matters is there (China #9: Xi'an's 17:41 was written for a day that ended
        # in Beijing at 16:59). Ordinary days keep the first stop (city-level).
        span_km = haversine_km(coords(with_c[0]), coords(with_c[-1]))
        moving = bool(day.get("travel_day")) or span_km > SUN_MOVE_KM
        pick = with_c[-1] if moving else with_c[0]
        pick_why = ("last stop — moving day{}".format(
            ", first->last {:.0f} km".format(span_km) if span_km > SUN_MOVE_KM
            else " (travel_day)") if moving else "first stop")
        # Morocco F5: D9 "sunrise in Chefchaouen, then fly to Casablanca" is a moving
        # day whose whole timeline hangs on the FIRST city's sunrise. day["sun_stop"]
        # (a stop's name, or its 0-based index in stops[]) overrides the automatic
        # pick; missing / unknown / no coordinates -> WARN and the old rule applies.
        chosen = day.get("sun_stop")
        if chosen is not None:
            override = None
            if isinstance(chosen, bool):
                pass
            elif isinstance(chosen, int):
                if 0 <= chosen < len(day["stops"]):
                    override = day["stops"][chosen]
            else:
                want = str(chosen).strip()
                for s in day["stops"]:
                    if str(s.get("name", "")).strip() == want:
                        override = s
                        break
                else:
                    for s in day["stops"]:
                        if _fold(s.get("name", "")).strip() == _fold(want):
                            override = s
                            break
            if override is None:
                warn("{}: sun_stop={!r} matches no stop of the day (use a stop's "
                     "exact name or its 0-based index) — falling back to the {}"
                     .format(head, chosen, pick_why))
            elif not coords(override):
                warn("{}: sun_stop={!r} -> {!r} has no lat/lon — falling back to "
                     "the {}".format(head, chosen, override.get("name"), pick_why))
            else:
                pick = override
                pick_why = "sun_stop={!r}{}".format(
                    chosen, " (would have been the {})".format(pick_why)
                    if override is not (with_c[-1] if moving else with_c[0]) else "")
        lat, lon = coords(pick)
        tz, tzlabel, approx = resolve_tz(day, plan, args.tz, lon)
        d = _dt.date.fromisoformat(date)
        problems = []
        rise = sset = dawn = None
        # 境内-only 默认且唯一路径:本地 NOAA 天文模型直接发布值 — 零网络、
        # 无署名要求、无限流,精度 ±1-2 min,远低于排程容差。
        src_tag = ("NOAA local solar model"
                   if plan_lang(plan, getattr(args, "lang", None)) == "en"
                   else "本地天文计算")
        rise_u, set_u = solar_events_utc(lat, lon, d)
        dawn_u, _dusk = solar_events_utc(lat, lon, d, 96.0)
        if rise_u is None or set_u is None or dawn_u is None:
            problems.append("polar day/night at {:.2f}° lat — model has no "
                            "sunrise; remove this day's sun key (absent — no null, "
                            "no \"\") and note it (KNOWN-ISSUES PLN-11)".format(lat))
        else:
            rise = rise_u.astimezone(tz)
            sset = set_u.astimezone(tz)
            dawn = dawn_u.astimezone(tz)
        if rise is not None:
            # the model computes the requested date directly; verify the local
            # date matches what was asked (paranoia against tz bugs)
            for what, t in (("sunrise", rise), ("sunset", sset)):
                if t.date() != d:
                    problems.append("{} dated {} but asked {}".format(
                        what, t.date(), d))
        print(head)
        print("  city point: {} ({:.4f},{:.4f}; {}) · tz {} · local model".format(
            pick["name"], lat, lon, pick_why, tzlabel))
        if problems:
            failures.append("{}: {}".format(head, "; ".join(problems)))
            print("  REJECTED: " + "; ".join(problems))
            skip(date, head, "rejected by sanity checks (see below)")
            continue
        value = "{} {} · ☀ {} / 🌇 {} · {}".format(
            dawn_word, dawn.strftime("%H:%M"), rise.strftime("%H:%M"), sset.strftime("%H:%M"),
            src_tag)
        print("  sun: {}".format(value))
        if day.get("sun") and day["sun"] != value:
            print("  (was: {})".format(day["sun"]))
        if args.write:
            if approx:
                skip(date, head, "tz approximate — NOT written; pass --tz Area/City "
                     "or set day.tz / plan.tz")
                continue
            day["sun"] = value
            written += 1
    if args.write and written:
        src.write_text(json.dumps(plan, ensure_ascii=False, indent=1),
                       encoding="utf-8")
    if args.write:
        print("\n{} day(s) written{}".format(
            written,
            " to " + str(src) if written else " — file untouched"))
    else:
        print("\nprinted only (add --write to update the plan)")
    if skipped:
        # Named, not counted: "6 written" out of 7 is only visible if you count.
        line = "skipped/failed ({}): {}".format(
            len(skipped), ", ".join("{} ({})".format(d, w.split(" — ")[0])
                                     for d, w in skipped))
        print(line)
        warn("sun " + line)
    if failures:
        for f in failures:
            warn("sun REJECTED — " + f)
        warn("{} day(s) rejected by sanity checks — nothing about them was written."
             .format(len(failures)))
        sys.exit(3)
    if skipped:
        # Vietnam F6: one TLS failure out of ten used to end with exit 0 and a
        # summary line nobody reads. Non-zero = at least one day has no sun value
        # from this run; the successful days ARE written above. Re-run with
        # --only DATE[,DATE] for the listed dates (a request failure retries;
        # no-date / no-coordinates / approximate-tz days need the plan fixed).
        warn("sun: {} day(s) NOT written — exit 1; fix/re-run with --only {}".format(
            len(skipped), ",".join(d for d, _ in skipped)))
        sys.exit(1)


def _ics_escape(t):
    return (str(t).replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\\n").replace("\n", "\\n"))


def _ics_fold(line):
    """RFC 5545 folding at 75 octets, never inside a UTF-8 sequence."""
    b, out, first = line.encode("utf-8"), [], True
    while b:
        limit = 75 if first else 74
        chunk = b[:limit]
        while 0 < len(chunk) < len(b) and (b[len(chunk)] & 0xC0) == 0x80:
            chunk = chunk[:-1]
        out.append(("" if first else " ") + chunk.decode("utf-8"))
        b, first = b[len(chunk):], False
    return "\r\n".join(out)


def cmd_ics(args):
    """gates .ics from the checklist: one VEVENT per row whose deadline carries an ISO
    date or a T-N marker (T-14 = departure - 14 days; departure = days[0].date), floating
    local time (no Z / TZID), two VALARMs (-P1D, -PT30M), stable UID per row, SEQUENCE
    from --sequence (bump it on every plan change)."""
    import hashlib
    plan = load_plan(args.plan)
    rows = plan.get("checklist") or []
    if not isinstance(rows, list):
        sys.exit("checklist must be a list of rows")
    dep = None
    try:
        dep = _dt.date.fromisoformat(str(plan["days"][0].get("date", ""))[:10])
    except (ValueError, IndexError, KeyError):
        pass
    hh, mm = (args.hour.split(":") + ["00"])[:2]
    trip = str(plan.get("trip", "trip"))
    slug = re.sub(r"[^A-Za-z0-9]+", "-", trip).strip("-").lower()
    if len(slug) < 3:                      # CJK trip names strip to nothing useful
        slug = "trip"
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    events, skipped = [], []
    for i, r in enumerate(rows, 1):
        if not isinstance(r, dict):
            skipped.append((i, "not an object"))
            continue
        item = str(r.get("item", "")).strip()
        deadline = str(r.get("deadline", ""))
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})", deadline)
        day = None
        if m:
            try:
                day = _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                day = None
        if day is None:
            t = re.search(r"\bT-(\d+)\b", deadline)
            if t and dep:
                day = dep - _dt.timedelta(days=int(t.group(1)))
        if day is None:
            skipped.append((i, item[:40] + " — deadline {!r} has no ISO date or T-N marker"
                            .format(deadline[:40])))
            continue
        uid = "{}-{}@trip-planner".format(slug, hashlib.sha1(item.encode("utf-8")).hexdigest()[:10])
        desc = "{}:{} local, wherever you are.\nDEADLINE: {}".format(hh, mm, deadline)
        if r.get("note"):
            desc += "\nDO: {}".format(r["note"])
        if r.get("link"):
            desc += "\nLINK: {}".format(r["link"])
        if r.get("price"):
            desc += "\nPRICE: {}".format(r["price"])
        start = _dt.datetime(day.year, day.month, day.day, int(hh), int(mm))
        end = start + _dt.timedelta(hours=1)
        ev = ["BEGIN:VEVENT", "UID:" + uid, "DTSTAMP:" + stamp,
              "SEQUENCE:{}".format(args.sequence),
              "DTSTART:" + start.strftime("%Y%m%dT%H%M%S"),
              "DTEND:" + end.strftime("%Y%m%dT%H%M%S"),
              "SUMMARY:" + _ics_escape(item), "DESCRIPTION:" + _ics_escape(desc),
              "BEGIN:VALARM", "ACTION:DISPLAY",
              "DESCRIPTION:" + _ics_escape("Tomorrow: " + item), "TRIGGER:-P1D", "END:VALARM",
              "BEGIN:VALARM", "ACTION:DISPLAY",
              "DESCRIPTION:" + _ics_escape("Now: " + item), "TRIGGER:-PT30M", "END:VALARM",
              "END:VEVENT"]
        events.append((day, ev))
    events.sort(key=lambda e: e[0])
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0",
             "PRODID:-//trip-planner//{}-gates//EN".format(slug), "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", "X-WR-CALNAME:" + _ics_escape(trip + " — booking gates")]
    for _, ev in events:
        lines.extend(ev)
    lines.append("END:VCALENDAR")
    Path(args.out).write_text("\r\n".join(_ics_fold(l) for l in lines) + "\r\n",
                              encoding="utf-8")
    for i, why in skipped:
        warn("ics: checklist row {} skipped — {}".format(i, why))
    print("ics: {} event(s) written to {} ({} row(s) skipped, SEQUENCE={})".format(
        len(events), args.out, len(skipped), args.sequence))
    if not events:
        sys.exit(1)


def main():
    _load_dotenv()
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in [("geocode", cmd_geocode), ("check", cmd_check),
                     ("routes", cmd_routes), ("links", cmd_links), ("kml", cmd_kml), ("sun", cmd_sun),
                     ("ics", cmd_ics), ("poi", cmd_poi)]:
        p = sub.add_parser(name, description={
            "check": "Straight-line hop distances, (est) durations, on-foot vs "
                     "ridden totals split declared/guessed. Exit 2 for an "
                     "undeclared >12 km hop, a DECLARED transit/walk hop >60 km "
                     "(a mis-geocoded stop — read geocache.json — or a train/bus/boat/"
                     "drive/fly that must say so) or missing coordinates; declared "
                     "fly/drive/boat/train/bus hops are fine. --live: real 高德 "
                     "driving/transit minutes replace the (est) for transit/drive "
                     "hops (needs AMAP_KEY; cached in routecache.json; cross-city "
                     "transit falls back to driving ×1.25 折算).",
            "routes": "Optional structured 高德 walk/transit/driving routes with "
                       "WGS84 paths, segments, explicit costs and TTL caching. "
                       "Dry-run by default; --write atomically adds route fields to "
                       "arriving stops and updates routecache.json. AMAP_KEY is read "
                       "from the environment only. Each hop fails independently; "
                       "cross-city transit keeps its estimate and old check --live "
                       "behaviour is unchanged.",
            "ics": "The gates .ics from the checklist: one VEVENT per row whose "
                   "deadline carries an ISO date or a T-N marker (T-14 = days[0].date "
                   "- 14), floating local time at --hour (default 09:00), DTEND +1 h, "
                   "two VALARMs (-P1D, -PT30M), stable UID per row, SEQUENCE from "
                   "--sequence (bump on every plan change), 75-octet folding. Rows "
                   "without a parseable deadline are listed as WARN. Exit 1 when no "
                   "row produced an event.",
            "geocode": "Resolve stops via 高德/百度 geocoding in place "
                       "(+ geocache.json); "
                       "preset/hand-filled coordinates are kept. WARNs when a hit's "
                       "display_name lacks the query's head token (possible wrong "
                       "place — look at it).",
            "links": "Per-hop map links (amap | baidu). --write prints "
                     "origin -> destination names per row, refuses mismatched rows "
                     "and >100 km / fly / boat links, and summarises wrote / parked / "
                     "suspicious + 'long legs kept unlinked (expected)'. The row "
                     "name check is whole-word (Latin) / longest-first (CJK: another "
                     "stop's name that is a substring of this hop's endpoint is not "
                     "a mismatch); a token shared by two stops of the day is ignored. "
                     "parked > 0 -> exit still 0, but a loud stderr WARN lists every "
                     "parked row (day, row, text) — read it. Rail/flight row rule: "
                     "both stations in stops[] -> keep the row mappable (no link, "
                     "count aligned); stations NOT in stops[] (legs[] only) "
                     "-> \"map\": false.",
            "sun": "Civil dawn / sunrise / sunset per day, computed LOCALLY by the "
                   "NOAA solar model (zero network); writes day['sun'] only with "
                   "--write. "
                   "Point: the day's first stop, or the LAST stop on a moving day "
                   "(travel_day, or first->last >150 km); day['sun_stop'] (a stop's "
                   "name or 0-based index in stops[]) overrides that pick and is "
                   "echoed as 'sun_stop=…' (unknown -> WARN + old rule). Every "
                   "unwritten day is a WARN and listed at the end. Exit 0 = all "
                   "days handled; 1 = some day NOT written (others are; re-run "
                   "--write --only DATE,... for the listed dates); 3 = a day was "
                   "REJECTED by the sanity checks (look before retrying — a polar "
                   "day/night rejection is never retried, PLN-11).",
            "poi": "周边 POI 搜索 via 高德 place/around (needs AMAP_KEY). --at is "
                   "either a stop name/query from the plan (uses its geocoded "
                   "coords) or 'lat,lon' in WGS84; --kw keywords (| separates "
                   "alternatives, e.g. '地铁站|地铁出入口'), --radius metres "
                   "(default 1000), --limit rows (default 10). Results carry "
                   "WGS84 coords ready to paste into the plan. Exit 1 without a "
                   "key / on API error.",
        }.get(name), formatter_class=argparse.RawDescriptionHelpFormatter)
        p.add_argument("plan", help="plan JSON path")
        if name == "kml":
            p.add_argument("-o", "--out", default="trip.kml")
        if name == "ics":
            p.add_argument("-o", "--out", default="gates.ics")
            p.add_argument("--hour", default="09:00", help="floating local time of every gate (HH:MM)")
            p.add_argument("--sequence", type=int, default=0,
                           help="SEQUENCE for every VEVENT — bump it on each plan change")
        if name == "routes":
            p.add_argument("--write", action="store_true",
                           help="atomically write days[].stops[].route and the TTL "
                                "cache; without it only print results")
            p.add_argument("--strategy", type=int, default=0,
                           help="AMap route strategy (default 0); included in cache keys")
            p.add_argument("--ttl", type=int, default=86400,
                           help="successful route cache TTL in seconds (default 86400; "
                                "0 disables structured-route caching)")
        if name == "links":
            p.add_argument("--write", action="store_true",
                           help="inject the URLs into the plan's hop rows and "
                                "day_map, in place, instead of only printing them; "
                                "prints each link's origin -> destination against "
                                "the row text and a wrote/parked/suspicious summary")
            p.add_argument("--provider", choices=PROVIDERS, default=DEFAULT_PROVIDER,
                           help="deep-link provider: amap (uri.amap.com, keyless — "
                                "DEFAULT; 境内直连), baidu (api.map.baidu.com, "
                                "keyless). Env override: TRIP_MAP_PROVIDER")
        if name == "geocode":
            p.add_argument("--amap-key", default=None,
                           help="高德 Web 服务 API key（地理编码）；默认读环境变量 "
                                "AMAP_KEY（或 skill 根目录 .env）。结果自动 "
                                "GCJ02→WGS84")
            p.add_argument("--baidu-ak", default=None,
                           help="百度地图服务端 AK（地理编码 v3 回退）；默认读环境"
                                "变量 BAIDU_MAP_AK。结果自动 GCJ02→WGS84")
        if name == "check":
            p.add_argument("--live", action="store_true",
                           help="用高德 direction API 的真实驾车/公交分钟数替代 "
                                "(est)（需 AMAP_KEY；缓存 routecache.json；跨城"
                                "公交降级为驾车×1.25 折算）")
            p.add_argument("--amap-key", default=None,
                           help="高德 Web 服务 key；默认读 AMAP_KEY 环境变量或 .env")
        if name == "poi":
            p.add_argument("--at", required=True,
                           help="中心点：plan 里的 stop 名（用其已 geocode 坐标）"
                                "或 '纬度,经度'（WGS84）")
            p.add_argument("--kw", default="",
                           help="关键词，| 分隔多个（如 '地铁站|地铁出入口'）；"
                                "留空 = 不限")
            p.add_argument("--radius", type=int, default=1000,
                           help="搜索半径（米，默认 1000，上限 50000）")
            p.add_argument("--limit", type=int, default=10,
                           help="返回条数（默认 10，上限 25）")
            p.add_argument("--amap-key", default=None,
                           help="高德 Web 服务 key；默认读 AMAP_KEY 环境变量或 .env")
        if name == "sun":
            p.add_argument("--write", action="store_true",
                           help="write day['sun'] in place (default: print only); "
                                "exit 1 if any day was skipped/failed (the good "
                                "days are still written — re-run with --only for "
                                "the dates listed), 3 on a sanity-check rejection")
            p.add_argument("--tz", default="auto",
                           help="IANA zone (Area/City) for all days lacking day.tz; "
                                "'auto' = day.tz > plan.tz > longitude offset "
                                "(approx, never written)")
            p.add_argument("--only", default="",
                           help="comma-separated ISO dates to (re)compute, e.g. "
                                "2026-10-01,2026-10-04")
            p.add_argument("--lang", default=None, choices=sorted(SUN_DAWN),
                           help="language of the written string's dawn word "
                                "(zh '天亮' / en 'dawn'); default: plan.lang > "
                                "plan.meta.lang > zh")
        p.set_defaults(fn=fn)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
