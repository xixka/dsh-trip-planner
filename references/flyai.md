# 飞猪 flyai 机票与酒店查询（可选增强）

`scripts/flyai_provider.py` 为 Phase 3 增加机票与酒店的只读实时候选。它是**可选**
的飞猪 flyai CLI 适配器：无 CLI、无 `FLYAI_API_KEY`、体验模式或查询失败时，完整
回退 `flight_scan.py` 的机票深链与酒店价格区间（估算），旧计划、旧字段、人工核验
和旧渲染完全不依赖本能力。

边界：不自动安装 CLI，不登录，不启动浏览器，不抓取搜索页，不下单，不收集乘客
信息；密钥只经环境（含项目 `.env` 规则：`os.environ.setdefault`，不覆盖已有值）
传递，绝不进入命令行参数、日志、fixture、缓存或 HTML。

## 重要：CLI 契约未实测

本适配器编写时环境中没有 flyai CLI，无法先运行 `flyai --help` 核对。脚本顶部的
`FLIGHTS_TEMPLATE`/`HOTELS_TEMPLATE`（子命令与旗标）是**假设契约**，包括：

| 项 | 假设值 |
|---|---|
| 版本探测 | `flyai --version` |
| 机票 | `flyai flights --from IATA --to IATA --date YYYY-MM-DD [--return-date …] [--adults N] [--cabin …] [--direct-only\|--max-transfers N] --format json` |
| 酒店 | `flyai hotels --city 名称 --checkin … --checkout … --guests N --rooms N --format json` |
| 输出 | stdout 一个 JSON 对象：`{"demoMode": bool, "systemMessage": str|null, "offers": [...]}` |
| 机票 offer | `flightNo/airline/depAirport/depTerminal/arrAirport/arrTerminal/depDate/depTime/arrDate/arrTime/durationMinutes/cabin/price/priceMasked/detailUrl`（中转放 `segments[]`，≤2 段） |
| 酒店 offer | `hotelName/hotelId/roomType/beds/maxGuests/price/priceMasked/priceBasis/taxesFees/breakfast/cancellation/rating/lat/lon/detailUrl/mainPic` |

**首次真实使用前必须先运行 `doctor`**：

```bash
python3 scripts/flyai_provider.py doctor
```

doctor 会运行 `flyai --help`/`--version`（带超时，只读），逐项核验假设旗标是否
出现在 help 中；签名不符时 capabilities 降级为不可用并提示先人工核对 `flyai --help`，
再按实际参数修改脚本顶部的模板常量。真机只做少量只读验证，未实测的一律明说。

## 统一返回结构

每个查询（机票/酒店）都返回同一信封：

```json
{
  "schema_version": 1, "kind": "flights|hotels",
  "source": {"name", "transport", "cli_path", "cli_version",
             "api_key_present", "adapter_verified_cli"},
  "queried_at": "北京时间 ISO8601 或 null（样本）",
  "timezone": "Asia/Shanghai",
  "status": "live|sample|demo|no_service|unavailable|unverified",
  "status_reason": "...", "system_message": "来自 CLI 的 systemMessage（透传）",
  "query": {...}, "query_key": "...",
  "offers": [...], "recommendations": {"primary": 0, "backup": 1},
  "notice": "...", "attempts": 1, "error": {"class", "message"},
  "links": {"text": "flight_scan 深链", "ctrip_check": "...", "booking_notice": "..."}
}
```

状态表（优先级同数字越小越高；非 live 一律不写计划）：

| status | 含义 | 写入计划 |
|---|---|---|
| live | CLI 成功、JSON 合法、非体验模式、至少一条合规候选 | 可 |
| sample | 离线 fixture（必须带 sample_date，永不冒充 live） | 否 |
| demo | 体验模式（`demoMode`/`demo` 或 systemMessage 提到 体验/演示/沙箱） | 否 |
| no_service | 查询成功但无候选（明确空列表） | 否 |
| unavailable | key 缺失、CLI 缺失、超时、非零退出、限流 | 否 |
| unverified | 输出畸形、schema 不符、日期为过去 | 否 |

错误分类与重试：超时重试一次（`--retries`，默认 1）；限流/429 不重试；单点失败
隔离——任一 offer 行畸形只计入 `malformed_rows`，其余照常规范化。stdout 噪声用
边界解析定位 JSON（整段 → 逐行 → 首尾大括号切片），找不到才判畸形。Windows 差异
由 `shutil.which`（PATH/PATHEXT 解析）与 `text=True, encoding="utf-8",
errors="replace"` 覆盖。

## 机票

筛选：日期、机场三字码/城市名、`--adults` 人数、`--cabin` 舱等、`--earliest/--latest`
出发时段、`--direct-only`/`--max-transfers`、`--limit`（默认 3，≤5）。规范化输出
航班号、航司、机场/航站楼、起降日期时间（含次日到）、历时、舱等、人民币参考价、
`detailUrl`；往返日期透传查询，但推荐按去程。

排序**不只看低价**：直飞优先、历时其次、价格第三；价格未知（遮蔽/缺失）的候选保留
在列表但排最后。遮蔽价 `price.status="masked"`、缺失 `"unavailable"`，amount 一律
null，绝不推测。行李额与退改规则默认未知：只写“未实测，点开核对”，绝不编造。

`--plan plan.geo.json --write`（仅 live）：手工行按 日期+from/to 匹配，只追加可选的
`flight_live` 对象，legacy 字段一字不动；没有匹配行时新增 `_flyai_managed` 行，
price 写“¥X（flyai 查询时参考价·单源）”、note 带 `queried_at` 与提醒，bags/
refund_change 标未实测。budget[] 永不改动：实时参考价只进 legs，估算仍留在 budget，
两者口径分开、不重复计。

## 酒店

按真实 `--checkin/--checkout/--guests/--rooms` 查询。每条 offer 记录：酒店名、房型、
床位、`occupancy.max_guests`、价格与**口径**（`priceBasis`：per-night-per-room /
per-night-per-person / total-per-room / total-per-person / unknown）、税费
（included/excluded/unknown）、早餐、取消政策、评分、坐标、来源时间与链接。

口径规则：`per-night-per-room` 且晚数明确时才给“N 晚合计”；口径未知一律标注
“口径未知,勿加总”，绝不加总。分档（`organize_tiers`）：只把**满足硬要求**
（maxGuests ≥ 人数，未知不淘汰但也不凑档）且价格口径明确的候选按价格升序组织
1–3 档；不满足硬要求的房型不拿来凑档，遮蔽/未知价的候选保留原样供人工核对。
`mainPic` 有意不读取：未明确对应房型且未核验授权时不放图。坐标留给高德人工核验；
无数据则标未实测。

`--plan … --base 乌镇 --write`（仅 live）：按 `hotels[].base` 子串匹配目标行，**增量
追加** `options[]`（legacy name/band/link 兼容 + 可选 `flyai` 对象含全部明细与
`queried_at`）；同名房型去重；无匹配时新增 `_flyai_managed` 酒店行（area/why 留给
人工补充）。已有手工 options 永不修改，budget 永不改动。

## 渲染

`themes/theme_common.py` 新增共用安全格式化函数 `hotel_option_extra(option)`：
读 `options[].flyai` 可选对象，全部值 esc 转义后拼一行 dim 文案（房型·床位·人数·
价格与口径·税费·早餐·取消·评分·档位·查询时间·参考价）；无 `flyai` 字段的旧
options 返回空串，七个主题（illustrated/clay/noir/glass/journal/zine/splash）与
`scripts/render_plan.py` 的旧输出 byte-identical（已用 china-2026 样例回归验证）。
遮蔽/未知价格只显示“价格遮蔽,点开核对”，绝不变造成数字。

## 安装与降级

flyai CLI 与 key 都由用户自行安装/申请（本仓库不自动安装任何依赖）：

```bash
# .env（已 gitignore）
FLYAI_API_KEY=你的飞猪开放平台key
# 可选:CLI 不在 PATH 时指定路径
FLYAI_CLI=/absolute/path/flyai
```

无 CLI/无 key/体验模式/失败 → `unavailable|demo` + 完整的 `flight_scan.py` 深链
（携程/Trip.com/去哪儿机票、携程酒店列表链接），价格区间估算不动，旧页面照常渲染。
`flight_scan.py` 保留为第二入口与降级路径，永不移除。

## 测试与 fixture

```bash
python3 -m unittest -v tests/test_flyai_provider.py
npm test          # 插件自检 + Python 套件（CI 不访问真实服务）
```

`tests/fixtures/flyai/` 覆盖：正常直飞/中转/次日到达、遮蔽价、体验模式、缺字段、
空结果、CLI 缺失（key_missing）、超时、限流、非零退出、噪声边界、畸形输出、酒店
多价格口径、酒店遮蔽价与旧计划。fixture 信封 `{"tool", "sample_date", "result"}`
（样本必须带 sample_date，永不冒充 live）；错误模拟 `{"tool", "error": {...}}`；
CLI 退出模拟 `{"tool", "exit": {"returncode", "stdout", "stderr"}}`（成功模拟同样
必须带 sample_date）。

重点断言：样本不冒充 live、demo 与 unavailable 不混淆、限流不重试、遮蔽价不猜、
口径未知不加总、人工字段不变、budget 不重复、旧渲染 byte-identical、key 不进
argv/输出。

## 验证边界

本次开发**未安装 flyai CLI、未申请 key、未访问飞猪真实服务、未做真机 live 验证**；
CLI 契约是假设的（见上表）。交付 Agent 首次使用前必须：运行 doctor 核对签名 →
少量只读查询成功 → 再把 live 结果写计划；任何未实测/失败项保持深链人工核验。
