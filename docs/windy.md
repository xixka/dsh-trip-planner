# Windy 免费数据源（逆向文档 + windy_fetch.py 手册）

> 逆向对象：Windy Android 51.3.2（com.windyty.android，APKMirror 4arch/6dpi 构建）
> 验证日期：2026-11-23 / 11-24（全部端点经代理实测，含北京坐标）
> 状态：**全部免费数据功能已逆向并实现，零遗留**（见 §1.1 功能覆盖矩阵）；
> 配套实现 `scripts/windy_fetch.py`（19 个子命令）
> 本文档是 Windy 数据源的权威参考；`references/data-sources.md` 的 Weather 章节只保留接入要点

---

## 1. 概述

Windy Android 是 **Capacitor 混合应用**：原生壳 + 内嵌完整 Web 端 JS
（`assets/public/v/51.3.2.mob.3c81/`，约 1655 个 JS 文件），全部数据端点与请求
逻辑都在 JS 里，可静态审计。免费用户可用的数据（点预报、AQI、CAP 气象预警、
台风、闪电、机场报文、站点观测、POI、搜索、逆地理、高程、航线沿线天气）经
`https://node.windy.com` 下发，**无需 API key、无需登录**。

本项目已排除地图瓦片类接口（有高德/百度地图），因此 ims/rdr/sat 瓦片域名不在
接入范围。

### 1.1 免费功能覆盖矩阵（核实于 2026-11-24）

| Windy 免费功能 | 端点 | windy_fetch 子命令 | 状态 |
|---|---|---|---|
| 多模型点预报（gfs/icon-global/ecmwf-hres/区域模型） | `/forecast/point/*` | `forecast` | ✅ 实现+实测 |
| 当前实况 | `/forecast/point/now/*` | `now` | ✅ |
| 空气质量 AQI | `/forecast/airq/cams-global/*` | `aqi` | ✅ |
| CAP 气象预警 | `/capalerts/{lat}/{lon}` | `alerts` | ✅ |
| 台风/热带气旋（目录+详情+计数） | `/tc/v2/*` | `storms` | ✅ |
| 闪电（热区/近 60 分钟） | `/blitz/v3/*` | `lightning` | ✅（60mins 波动，默认 hot） |
| 高程（单点+路线剖面） | `/services/elevation`、`/rplanner/v1/elevation` | `elevation` | ✅ |
| 航线沿线天气 | `/rplanner/v1/forecast/{mode}/*` | `route` | ✅ |
| 附近站点/机场 POI | `/pois/v2/{ident}/*` | `stations`、`pois` | ✅（ident: stations/ad/tide/airq/radiosonde） |
| 火点（NASA FIRMS 活跃火） | `/pois/v2/firespots/tiles/{z}/{x}/{y}` | `pois --ident firespots` | ✅（mesh 原始数据） |
| 站点观测历史（METAR/探空/AQI/PWS） | `/obs/measurement/v3/*` | `obs` | ✅ |
| 机场报文（METAR/TAF/NOTAM/信息） | `/airports/{metar,taf,notams,adinfo}/*` | `airport` | ✅ |
| 附近实景摄像头 | `/webcams/v2.0/list` 等 | `webcams` | ✅（图链 imgproxy 直可用） |
| 地点/站点搜索 | `/search/v4.1/*` | `search` | ✅（size≥10 硬校验） |
| 逆地理编码 | `/reverse/v3/*` | `reverse` | ✅ |
| 日出日落/暮光 | `/services/celestial/*` | `celestial` | ✅ |
| 雪崩危险等级（欧/美山区） | `/avalanches/{regions,overview}.json` | `avalanche` | ✅ |
| 近 5 天预报存档（历史比对） | `/forecast/archive/*` | `archive` | ✅ |
| 波浪/涌浪预报 | `gfs-wave` point（含 waves/swell 字段） | `forecast --model gfs-wave` | ✅ |
| 组合查询 | — | `all` | ✅ |
| **不可用（免费档拿不到）** | `/tides`（401 登录门禁） | — | ❌ 潮汐站列表免费（`pois --ident tide`），潮汐数值付费 |
| **不适用本项目** | 地图/图层瓦片（ims/rdr/sat） | — | ⛔ 有高德/百度，见 §3.3 |
| **登录态功能** | `/notif/v1/*`（自定义提醒）、`/users/*`（收藏同步）、dashboards | — | ⛔ 需账号，不接入 |

**说明**：`ecmwf-wam` 的 point 预报实测 404（海上点同样），波浪数据一律走
`gfs-wave`（已实测含 `waves/wavesDir/wavesPeriod/swell1/swell2` 完整字段）；
`/forecast/meteogram/*` v1.2 时通时不通（布拉格 200、北京/慕尼黑 404），
以 `forecast`（point v3.0）为准。

## 2. 请求规则（先读这个）

- **refTime 依赖**：`/forecast/*` 系列必须带模型批次的 `refTime`。先请求
  `GET /metadata/v1.0/forecast/{model}/minifest.json` 取 `ref` 字段（建议缓存
  ≥30 min），失败时回退"最近 6/12 h 整批次"推算值。
- **模型 ident 注意**：全球 ICON 的 ident 是 `icon-global`（不是 `icon`）；
  AQI 走 `cams-global`（12 h 更新）；波浪 `gfs-wave` / `ecmwf-wam`。
- **区域模型边界**：icon-eu / arome / hrrr 等区域模型在覆盖区外返回 404，
  属正常，应静默降级到全球模型。
- **204 语义**：`/capalerts/{lat}/{lon}` 无预警、`/obs/measurement/v3/pws/…`
  站点无上报时返回 204 + 空体——**不是错误**。
- **单位**：Windy 原始返回温度 K、气压 Pa、风速 m/s、降水 mm；
  `windy_fetch.py` 已自动换算为 °C / hPa / m/s(+km/h)。
- **数据标注**：按 `data-sources.md` 的既定规范，每条天气行都要盖来源戳
  ——`… · Windy gfs 2026-11-24T12:00:00Z`。
- **境内直连**：node.windy.com 大陆可直连性未验证；`WINDY_BASE` 环境变量可
  整体换前缀（如自建反代 `https://your-proxy/https://node.windy.com`）。
- **限流自律**：非官方端点，必须走缓存（模块自带 windycache.json），禁止并发
  批量抓取；User-Agent 已声明用途。

## 3. 端点总目录

### 3.1 ✅ 已实测（2026-11-23/24，免 key 免登录）

| # | 端点 | 用途 | 实测 |
|---|---|---|---|
| 1 | `GET /metadata/v1.0/forecast/{model}/minifest.json` | 模型批次 ref/日历 | 200（gfs/icon-global/icon-eu/ecmwf-hres/cams-global/gfs-wave/ecmwf-wam） |
| 2 | `GET /forecast/point/{model}/v3.0/{lat}/{lon}?refTime=&days=&step=&source=detail` | **点预报**（主接口） | 200（北京：gfs/icon-global/ecmwf-hres/gfs-wave） |
| 3 | `GET /forecast/point/now/{model}/v1.0/{lat}/{lon}?refTime=` | 当前实况 | 200·316B |
| 4 | `GET /forecast/airq/cams-global/v3.0/{lat}/{lon}?refTime=&days=&step=` | **AQI**（注意路径段是 `airq`） | 200（aqiUs/pm2p5/pm10/no2/go3…） |
| 5 | `GET /forecast/archive/{model}/v3.0/{lat}/{lon}?archiveHours=120&source=station` | 近 5 天逐时存档（临场重排比对） | 200·1.9KB |
| 6 | `GET /forecast/fragment/{model}/v3.0/{lat}/{lon}?days=&source=&refTime=` | 详情卡片段 | 200·1KB |
| 7 | `GET /capalerts/{lat}/{lon}?detailed=true&lang=zh-cn&source=hp&maxCount=6` | **CAP 气象预警点查询** | 瑞士 200（MeteoSwiss 强降雨全文）；无预警 204 |
| 8 | `GET /capalerts/tiles/{z}/{x}/{y}` | 预警多边形瓦片 | 200·112KB（欧洲 z2·2574 要素） |
| 9 | `GET /tc/v2/storms` | **台风/热带气旋目录** | 200（id/name/lat/lon/strength/windSpeed + 模型注册表） |
| 10 | `GET /tc/v2/storms/{id}` | 台风详情+历史路径 | 200·6KB（xavier 实测） |
| 11 | `GET /tc/v2/storms/active/count` | 活跃台风计数 | 200 |
| 12 | `GET /services/elevation/{lat}/{lon}` | 单点高程（米，裸数字） | 200（北京 → `53`） |
| 13 | `GET /rplanner/v1/elevation/{lat,lon;lat,lon;…}` | **路线高程剖面** | 200·8.7KB（distances[]+elevation[]） |
| 14 | `GET /rplanner/v1/forecast/{mode}/{lat,lon;lat,lon;…}?dst=YYYY/MM/DD/HH` | **航线沿线天气**（car/boat/vfr/ifr） | 200（京→津、京→沪） |
| 15 | `GET /obs/measurement/v3/{source}/{id}/{count}/1?properties=data,summary,segments` | **站点观测历史** | 200（ad-ZBAA·37.9KB / radiosonde-54511·14.8KB / airq·10.7KB） |
| 16 | `GET /search/v4.1/{lat}/{lon}/{query}?lang=zh-cn&size=13&types={types}` | 地点/站点搜索 | 200（"Beijing"）；**size≥10 硬校验** |
| 17 | `GET /pois/v2/{ident}/{lat}/{lon}` | 附近 POI（ident：stations/radiosonde/airq/…） | 200（stations/radiosonde/airq 实测） |
| 18 | `POST /pois/v2/search` | POI 搜索（body） | 静态证实，未逐一实测 |
| 19 | `GET /reverse/v3/{lat}/{lon}/{zoom}?lang=zh-cn` | 逆地理编码 | 200·216B（中文） |
| 20 | `GET /services/celestial/{lat}/{lon}` | 日出/日落/暮光（补 NOAA 本地计算） | 200·133B |
| 21 | `GET /airports/metar/{icao}`（`/taf/{icao}`、`/notams/{icao}`、`/adinfo/{icao}`） | 机场实况/预报/通告/信息 | 200（ZBAA 四项全测；**METAR 响应自带提示：官方免费源为 aviationweather.gov/dataserver，批量需求请走官方**） |
| 22 | `GET /blitz/v3/hot`（`/blitz/v3/60mins`） | 闪电热区 | 200·58KB（hot 稳定；**60mins 有波动，11-24 实测 404**） |
| 23 | `GET /webcams/v2.0/list?nearby={lat},{lon}&radius=250&order=popularity&limit=3&lang=zh-cn` | 附近实景摄像头 | 200（**radius 单独传参；勿传 imageSize**） |
| 24 | `GET /webcams/v1.0/webcams/last-updated-on?webcamIds={id}`、`/webcams/v2.0/archive/hourly/{id}` | 摄像头更新时间/逐时归档 | 200（归档·121KB） |
| 25 | `GET /avalanches/regions.json`、`GET /avalanches/overview/{YYYY-MM-DD}.json` | 雪崩区域注册/当日全球危险等级 | 200（regions·529KB；overview·126KB；**路径参数是日期**） |
| 26 | `GET https://rdr.windy.com/radar2/composite/coverage.json` | 全球雷达覆盖表（无 token） | 200·6.8KB |
| 27 | `GET /reverse/v3/radar-provider/{lat}/{lon}` | 雷达数据提供方 | 200（北京 → **国家气象中心 NMC**） |
| 28 | `GET /pois/v2/{ident}/{lat}/{lon}`（ident 泛化） | 附近 POI | 200（stations/ad/tide/airq/radiosonde 实测） |
| 29 | `GET /pois/v2/firespots/tiles/{z}/{x}/{y}` | 活跃火点（mesh：id/tileX/tileY/spotRank） | 200（四川实测） |

**obs/measurement 的 source=id 约定**（来自 station.js 调用链）：

| source | id 形态 | 说明 |
|---|---|---|
| `ad` | ICAO（如 `ZBAA`） | 机场 METAR 历史 + 逐日摘要 |
| `radiosonde` | WMO 站号（如 `54511` 北京） | 探空 + 气球 4D 轨迹；**中国 CMA 站点注册此前缀** |
| `airq` | 数字 id | 空气质量站 |
| `pws` | hex id | 个人气象站（204=无上报） |
| `wmo` | WMO 站号 | SYNOP（多为非中国站） |

**search/v4.1 的 types 枚举**：`airport` / `station,radiosonde` / `webcam` /
`kite,surf` / `pg`；省略 types = 全部。query 中的 `/` 会替换为空格。

**模型清单（中国坐标可用性实测）**：`gfs`（全球 22km/6h）、`icon-global`
（全球 13km）、`ecmwf-hres`（全球 9km/6h）、`cams-global`（AQI 40km/12h）、
`gfs-wave`/`ecmwf-wam`（波浪）✅；`icon-eu` 等区域模型 ❌（区域外 404）。
APK 中注册的其他区域模型：icon-d2/icon-ewam/arome*/hrrr-conus/hrrr-alaska/
bom-access*/can-hrdps/can-rdwps/cze-aladin/cams-eu/cmems 等。

### 3.2 ⚠ 静态发现（路径证实，未逐一实测）

| 端点 | 用途 |
|---|---|
| `GET /airports/airspaces/tiles/{z}/{x}/{y}`、`/points/{lat}/{lon}` | 空域（航空行程） |
| `GET /airports/notams/{icao}` | 航行通告 |
| `GET /webcams/v2.0/archive/hourly/{id}`、`/v1.0/webcams/image-url/{id}` | 摄像头归档/图链 |
| `GET /blitz/v4/sse`（EventSource） | 闪电实时流（长连接，CLI 不用） |
| `GET /rplanner/v1/trajectory/paths` | 轨迹规划（400=需 POST/复杂参数） |
| `GET /obs/measurement/v2/radiosonde/{id}/1/1` | 探空 v2 兼容路径 |
| `GET /citytile/v1.0/{model}/…` | 城市瓦片（本项目不用） |

### 3.3 ❌ 受限（不接入）

| 端点 | 现象 | 处置 |
|---|---|---|
| `ims.windy.com/im/v3.0/…` 数据瓦片 | 400 `Invalid request`（会话校验） | 本项目不需要瓦片，放弃 |
| `rdr.windy.com`/`sat.windy.com` 瓦片 | 需 `token2` 会话参数 | 同上 |
| `/tides/v1.0/tides/{lat}/{lon}` | 401 登录门禁 | 潮汐数值不接入；**潮汐站列表免费**（`pois --ident tide`） |
| `/notif/v1/*`、`/users/*`、`/imaker/*`、订阅支付类 | 需登录 | 不接入 |
| `node.windy.com/sedlina/*` | 客户端遥测上报 | 不接入（勿模仿） |

## 4. 关键响应 Schema（实测）

**点预报 `/forecast/point/gfs/v3.0/…?source=detail`**
`data`：`ts`（epoch ms）、`hour`、`isDay`、`icon`、`moonPhase`、
`precipAmount`/`precipSnowAmount`/`precipConvectiveAmount`（mm）、`precipType`、
`pressure`（Pa）、`temperature`/`feelTemperature`（K）、`wind`/`windGust`（m/s）、
`windDir`（°）。不同 `include` 可选 dew/cloud 等字段。

**当前实况 `/forecast/point/now/gfs/v1.0/…`**（扁平）
`ts`、`mm`、`rain`、`snowPrecip`、`convPrecip`、`temp`（K）、`dewPoint`、
`wind`、`windDir`、`gust`、`pressure`（Pa）、`cbase`、`visibility`、`fog`、
`rh-surface`、`rh-950h`…（各层相对湿度）。

**AQI `/forecast/airq/cams-global/v3.0/…`**
`ts`、`hour`、`isDay`、`aqiUs`、`pm2p5`、`pm10`、`no2`、`go3`、
`chemsCosc`、`chemsDustsm`、`chemsSo2sm`。

**CAP 预警 `/capalerts/{lat}/{lon}?detailed=true`**
`data[]`：`id`、`start`/`end`（epoch ms）、`ident`（国家/机构码）、`event`、
`severity`（M/S/…）、`type`、`areaDesc`、`languages`、`author`、`senderName`、
`info.{lang}.{event,headline,description,instruction}`（多语言预警全文）。

**站点观测 `/obs/measurement/v3/ad/ZBAA/10/1`**
`header`（站名/位置/更新时间/is_airport…）、`segments[]`、
`data`（`ts/temp/wind/windDir/gust/dewPoint/pressure/weathercode/visibility/category`
——**历史逐时实况**）、`calData`、`summary`（逐日摘要）、`celestial`。
radiosonde 变体含 `trajectory[]`（lon,lat,alt,ts 气球轨迹）。

**航线沿线 `/rplanner/v1/forecast/car/{points}?dst=…`**
`data`：`wind[]`、`gust[]`、`windDir[]`、`temp[]`（K）、`precip[]`、`vis[]`，
索引对应路线等分点。

**单位换算**：`°C = K − 273.15`；`hPa = Pa ÷ 100`；风速 m/s（×3.6=km/h）；
降水 mm；`ts` 一律 epoch 毫秒。

## 5. windy_fetch.py 使用手册

`scripts/windy_fetch.py`（Python 3.9+ 纯标准库）。环境变量：`WINDY_BASE`
（默认 `https://node.windy.com`，可换反代前缀）、`WINDY_CACHE`
（默认 `./windycache.json`）。

```
forecast   --lat 39.9042 --lon 116.4074 [--model gfs] [--days 5] [--step 3]
now        --lat --lon [--model gfs]
aqi        --lat --lon [--model cams-global]
archive    --lat --lon [--hours 120] [--model gfs]
alerts     --lat --lon [--lang zh-cn]          # [] = 无预警（204）
storms     [--count] [--id xavier]
lightning  [--minutes60]                       # 闪电（默认 hot 热区；60mins 波动）
airport    --icao ZBAA [--what metar,taf,notams,adinfo]   # 机场报文
elevation  --lat --lon  |  --route "lat,lon;lat,lon"
route      --route "39.90,116.41;31.23,121.47" [--mode car] [--dst 2026/11/25/02]
stations   --lat --lon                         # 附近站点/机场实况
pois       --ident tide --lat --lon            # stations/ad/airq/radiosonde/firespots…
obs        --source ad --id ZBAA [--count 10]  # radiosonde 54511 / airq / pws / wmo
search     --query "Beijing" --lat --lon [--types station,radiosonde]
reverse    --lat --lon [--lang zh-cn]          # 逆地理编码
webcams    --lat --lon [--radius 250]          # 附近摄像头（--id 查更新/归档）
celestial  --lat --lon                         # 日出日落（补 NOAA 本地计算）
avalanche  [--date 2026-11-23] [--regions]     # 雪崩危险（欧/美山区）
minifest   [--model gfs]                       # 调试：当前批次
all        --lat --lon [--model gfs]           # 组合单 JSON（预报+实况+AQI+预警+台风）
```

退出码：`0` 正常（含空预警列表）；`1` 网络/解析失败；`2` 参数错误；
`3` 端点有响应但无数据（区域模型越界 404 等）。所有输出带
`meta.source = "Windy {model} {refTime}"` 溯源戳。

示例：

```bash
python3 scripts/windy_fetch.py forecast --lat 30.25 --lon 120.17 --model icon-global --days 5
python3 scripts/windy_fetch.py route --route "30.25,120.17;31.23,121.47" --mode car
python3 scripts/windy_fetch.py obs --source radiosonde --id 54511
python3 scripts/windy_fetch.py all --lat 30.25 --lon 120.17 | jq '.forecast.data.temp_c'
```

## 6. 与项目链路的集成（要点）

详细数据源规范见 `references/data-sources.md` § Weather，此处仅摘要：

- **预报回退链**：Open-Meteo（默认）→ **Windy point v3.0**（gfs/icon-global/
  ecmwf-hres 三模型交叉校验）→ QWeather（中文预报）→ 高德（国内城市）。
  Windy 的独有价值：**多模型交叉验证**（行前简报可写"两模型一致认为…"）。
- **AQI**：现有链路空缺，`aqi` 子命令填补（cams-global 覆盖全国）。
- **预警**：境内仍以中央气象台 nmc.cn 为主（Windy 的中国 CAP 数据覆盖有限，
  北京/广州实测均 204）；`alerts` 作为境外/跨境扩展与全球补充。
- **台风季（5–11 月）**：`storms` 检查行程目的地附近活跃台风，写入
  emergency/safety 卡。
- **高程**：`elevation`（单点+路线）用于徒步/山地行程的累计爬升提示。
- **临场重排**：`archive` 取近 5 天逐时实况与此前预报比对，判断趋势。
- **标签规范**：所有 Windy 数据行盖 `· Windy {model} {date}` 戳，与
  Open-Meteo 行同等可追溯。

## 7. 合规与风险

- 端点为**未公开文档的免费接口**，形状可能随版本变化；升级客户端后按
  §8 流程重新校验。接口失效时模块自动降级退出（code 3），不影响主链路。
- ECMWF 数据许可：个人行程规划用途可接受；**不得再分发原始 ECMWF 数据**。
- 限流自律：全部请求走 windycache.json 缓存（minifest 30 min、预报 1 h、
  实况/预警 10 min），禁止并发抓取与批量爬取。
- 官方替代路径（如需商业化）：api.windy.com 的 Point Forecast API
  （免费 trial 返回混淆数据，正式版 €990/年，无 ECMWF）与
  Map Forecast API（免费地图嵌入）。
- **METAR/TAF 专项**：Windy 服务器在响应中明确提示"请勿抓取，官方免费源为
  https://www.aviationweather.gov/dataserver"——机场报文的批量/长期需求应改用
  官方 ADDS 源，`airport` 子命令仅作行程规划内的低频兜底。

## 8. 维护：Windy 版本升级后的再验证流程

1. 解包新 APK：`unzip base.apk "assets/public/v/*" -d out/`；
2. 检索端点：`rg -o '`/[A-Za-z0-9${}._/-]+`' -g '*.js' out/ | sort -u`；
3. 对照本文 §3 目录，差异项用 `windy_fetch.py minifest/forecast…` 快测；
4. 更新本文档与模块常量（模型 ident / 版本号）。

附：本次逆向的分析脚本与实测记录存于开发过程档案（scripts/windy_api_test*.sh
为一次性验证脚本，不入库）。
