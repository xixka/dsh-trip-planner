# 飞猪 flyai 机票与酒店查询（可选增强·官方 CLI 已实测）

`scripts/flyai_provider.py` 为 Phase 3 增加机票与酒店的只读实时候选。它是**可选**
的飞猪官方 flyai CLI 适配器：无 CLI 或查询失败时，完整回退 `flight_scan.py` 的机票
深链与酒店价格区间（估算），旧计划、旧字段、人工核验和旧渲染完全不依赖本能力。

边界：不自动安装 CLI，不登录，不启动浏览器，不抓取搜索页，不下单，不收集乘客
信息；密钥只经环境（含项目 `.env` 规则：`os.environ.setdefault`，不覆盖已有值）
传递，绝不进入命令行参数、日志、fixture、缓存或 HTML。

## CLI 来源与契约（2026-11-25 真机核验）

| 项 | 已核验值 |
|---|---|
| 上游 | `alibaba-flyai/flyai-skill`（阿里飞猪官方组织，MIT，1297★） |
| CLI | npm `@fly-ai/flyai-cli` 1.0.16（maintainer `open-flyai <flyai@alibaba-inc.com>`） |
| 安装 | `npm i -g @fly-ai/flyai-cli`（Node 18+；一条命令，官方发行渠道） |
| 传输 | streamable_http；stdout 单行 JSON，错误/提示走 stderr |
| API Key | **可选**——无 key 时 CLI 以体验模式运行（真实报价，部分结果受限） |

适配器内的命令模板/字段映射在 2026-11-25 对照真实 `--help` 与真机输出逐项核验：

| 项 | 真实契约 |
|---|---|
| 机票 | `flyai search-flight --origin … --destination … --dep-date YYYY-MM-DD [--back-date] [--journey-type 1\|2] [--seat-class-name economy,business,first] [--dep-hour-start/--dep-hour-end] [--max-price] [--sort-type 1-8]` |
| 酒店 | `flyai search-hotel --dest-name … [--check-in-date] [--check-out-date] [--key-words] [--poi-name] [--hotel-stars 1-5] [--hotel-bed-types 大床房,双床房,多床房] [--max-price] [--sort distance_asc\|rate_desc\|price_asc\|price_desc\|no_rank]` |
| 其他 | `search-train` / `search-poi` / `keyword-search` / `ai-search` / 万豪×2（本适配器暂未接入） |
| 信封 | `{"data": {"itemList": [...]}, "message": str, "status": 0, "systemMessage": str}`；无 `--format` 旗标（输出恒为 JSON） |
| 机票行 | `journeys[].segments[]`（`depDateTime`/`arrDateTime` = `YYYY-MM-DD HH:MM:SS`、`depStationCode/Name`、`marketingTransportName/No`、`duration` 分钟字符串、`seatClassName` 经济舱/商务舱/头等舱、`quantity` 余座、经停走 `stopInfos`）+ 信封 `ticketPrice`（`"450.00"`）+ `jumpUrl` |
| 酒店行 | `name/shId/star/rate/price/latitude/longitude/detailUrl/mainPic/address/brandName/interestsPoi`；**体验模式下酒店价格是数字遮蔽**（`"¥1xx"`/`"¥8x"`） |

注意：`--version` 旗标不存在（doctor 改用 `--help` 核验签名）。

## 安装与 doctor

```bash
npm i -g @fly-ai/flyai-cli          # 官方 CLI（可选）
python3 scripts/flyai_provider.py doctor   # 只读体检：CLI 路径/签名/Key 存在性
```

doctor 不发任何查询，只打印 key 是否存在（绝不打印值）；签名不符时 capabilities
照常报告，但 notes 提示先人工核对 `flyai --help` 再修订模板常量。

## 体验模式（无 Key）的诚实语义

无 `FLYAI_API_KEY` 时 CLI 正常工作但 `systemMessage` 会声明体验模式。适配器区分
两种情况：

- **真实报价 + 体验声明**（真机默认形态）：状态仍为 `live`、候选照常规范化，
  附加 `experience_mode: true` 与 `experience_mode_notice`（部分结果受限说明）；
- **声明"非真实报价"**（示例数据）：状态强制 `demo`，候选清空，绝不写计划。

酒店价格在体验模式下是数字遮蔽（`¥1xx`），适配器如实标记 `masked`，
**绝不按数量级猜测**；masked/缺失候选不参与分档，保留原样供人工核对。

## 统一返回结构

每个查询（机票/酒店）都返回同一信封（与 train_live 同构）：

```json
{
  "schema_version": 1, "kind": "flights|hotels",
  "source": {"name", "transport", "cli_path", "cli_version",
             "api_key_present", "adapter_verified_cli"},
  "queried_at": "北京时间 ISO8601 或 null（样本）",
  "timezone": "Asia/Shanghai",
  "status": "live|sample|demo|no_service|unavailable|unverified",
  "experience_mode": true|false,
  "system_message": "来自 CLI 的 systemMessage（透传）",
  "query": {...}, "query_key": "...",
  "offers": [...], "recommendations": {"primary": 0, "backup": 1},
  "notice": "...", "attempts": 1, "error": {"class", "message"},
  "links": {"text": "flight_scan 深链", "ctrip_check": "...", "booking_notice": "..."}
}
```

状态表（优先级同数字越小越高；非 live 一律不写计划）：

| status | 含义 | 写入计划 |
|---|---|---|
| live | CLI 成功、JSON 合法、至少一条合规候选（含无 key 体验模式的真实报价） | 可 |
| sample | 离线 fixture（必须带 sample_date，永不冒充 live） | 否 |
| demo | CLI 明示"非真实报价"（示例数据） | 否 |
| no_service | 查询成功但无候选（明确空列表） | 否 |
| unavailable | CLI 缺失、超时、非零退出、限流 | 否 |
| unverified | 输出畸形、schema 不符、日期为过去 | 否 |

错误分类与重试：超时重试一次（`--retries`，默认 1）；限流/429 不重试；单点失败
隔离——任一 offer 行畸形只计入 `malformed_rows`，其余照常规范化。stdout 噪声用
边界解析定位 JSON（整段 → 逐行 → 首尾大括号切片），找不到才判畸形。Windows 差异
由 `shutil.which`（PATH/PATHEXT 解析）与 `text=True, encoding="utf-8",
errors="replace"` 覆盖。

## 机票

筛选：日期、城市名/机场三字码、`--adults` 人数（适配器本地筛选与深链）、
`--cabin` 舱等（映射 CLI 的 `--seat-class-name`）、`--earliest/--latest`
出发时段（映射 `--dep-hour-start/end`）、`--direct-only`（映射 `--journey-type 1`）、
`--limit`（默认 3，≤5）。规范化输出航班号、航司、机场/航站楼、起降日期时间
（含次日到）、历时、舱等、余座数、人民币参考价、`jumpUrl` 深链。

排序**不只看低价**：直飞优先、历时其次、价格第三；价格未知（遮蔽/缺失）的候选保留
在列表末位，绝不丢弃、绝不猜测。中转方案的信封价直接采用（CLI 已给出整程价），
不自行加总。

## 酒店

CLI 返回酒店级每晚价。适配器输出酒店名、shId、档次（`star`）、地址、品牌、
附近 POI、评分、坐标（WGS84 由高德核验后使用）、`detailUrl`；`mainPic` 仅作
元数据保留、绝不渲染进计划（未核验授权）。`guests/--rooms` 只影响本地展示与
合并上限，不透传给 CLI（真实契约无此旗标）。

按价格组织 2–3 档：masked/缺失价不参与分档；档内与档间都按价格升序展示。

## 计划合并（仅 live）

- 机票：已有同日同航线人工行 → 只加 `flight_live` 子对象，人工字段原样保留；
  无匹配 → 追加 `_flyai_managed` 行（含全部旧渲染字段）。
- 酒店：`--base` 匹配 `hotels[].base` → 增量扩展 `options[]`（名字去重）；
  不匹配 → 追加受管行。
- sample/demo/unavailable/unverified 一律不写文件；原子写回只发生在成功合并后。

## 离线 fixture 与 CI

所有测试均不调用 CLI、不访问飞猪、不读真实 key。fixtures 已按真实 CLI 信封
schema 重写（`tests/fixtures/flyai/`），覆盖：直达与主备、中转两段、次日到、
masked 价、体验模式两种形态（真实报价 vs 非真实报价）、空列表、畸形输出、
非零退出、stdout 噪声、缺字段隔离、旧计划合并与旧渲染兼容。

## 真机验证记录

2026-11-25 23:37–23:48（北京时间，只读查询，无 key 体验模式）：

- `search-flight` 上海→昆明 2026-12-02：live，MU2508+MU5479 ¥560（8 座）、
  MU9744 直飞 ¥523（3 座），detailUrl 均为 router.feizhu.com 可跳转；
- `search-hotel` 杭州 2026-12-02~04：live，10 家候选，价格全部数字遮蔽
  （`¥1xx` 形态）→ 如实标 masked，坐标/detailUrl 完整；
- doctor：CLI 路径、签名（search-flight/search-hotel/search-train）全过。
