# Trip Planner Skill（境内优化版）

> 一句旅行需求，变成逐小时、信息核实过、可以直接照着订的行程，交付为双击即开的精美 HTML 页面。

![npm](https://img.shields.io/npm/v/dsh-trip-planner-cn)
![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-3776AB.svg)
![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)

一个面向**中国大陆境内旅行**深度适配的 [Agent Skill](https://github.com/anthropics/skills)：装进 Claude Code、Codex、Gemini CLI、Cursor、WorkBuddy、CodeBuddy、DeepSeek Harness 等编码智能体后，直接用自然语言提需求即可。

**它能帮你做的事：**

- 🗺️ **长假多日游**：得到一份逐小时、能直接照着订的完整行程
- 🌆 **周末一日游**：排出一套不赶路的玩法
- ⏱️ **2 小时空档**：在陌生城市给出步行可达的去处
- 🌧️ **临场重排**：赶不上车、下暴雨，几分钟重排当天

所有营业时间、价格、预约政策都用工具核实而不靠猜，每一项预订都递给你链接；**不代订、不付款、不填个人信息**。

![成品页封面](docs/screenshots/cover.jpg)

## 安装

### 方式一：npm 安装（DeepSeek Harness）

先装好 [DSH](https://www.npmjs.com/package/@deepseek-ai/dsh-skill) 与 pnpm，然后：

```bash
dsh plugin --profile web add dsh-trip-planner-cn
```

装完重启 DSH，技能目录里会出现 `trip-planner-cn`。卸载：

```bash
dsh plugin --profile web remove dsh-trip-planner-cn
```

不想走 npm 也可以直接从 GitHub 仓库装：

```bash
dsh plugin --profile web add github:ChenYiming-aaa/better-trip-planner
```

每个正式版本（GitHub Release）都会附带 CI 构建的安装包
`dsh-trip-planner-cn-<version>.tgz`（含 sha256 校验文件），下载后可直接本地安装：

```bash
dsh plugin --profile web add ./dsh-trip-planner-cn-0.2.0.tgz
```

### 方式二：其他宿主（Claude Code / WorkBuddy / CodeBuddy 等）

下载本仓库，放到宿主的 skills 目录并命名为 `trip-planner-cn`：

| 宿主 | 位置 |
|---|---|
| Claude Code | `~/.claude/skills/trip-planner-cn` |
| WorkBuddy | `~/.workbuddy/skills/trip-planner-cn` |
| CodeBuddy | `~/.codebuddy/skills/trip-planner-cn` |

运行环境只需 **Python 3.9+ 标准库**；可选安装 Pillow 用于素材流水线（`pip install pillow`）。

## 三十秒验证

不需要任何密钥，在 skill 目录执行：

```bash
python themes/render_clay2.py examples/china-2026/china.geo.json -o china-clay.html
python themes/qc.py china-clay.html    # 退出码 0 即通过
```

> 未配置 `AMAP_KEY` 时渲染会提示静态地图隐藏的 WARN，属预期降级，退出码仍为 0。

## 使用

装好后，在智能体里用自然语言提需求即可，旅行类请求会自动触发本 skill：

```text
云南 8 天，10 月出发，中等预算，自然风光和古城，日期前后可挪 2 天
```

**使用提示**：如果已经订好了机票、车票或酒店（航班号/车次、酒店名与入住日期），尽量一并提供——智能体会把这些预订直接编进行程、围绕它们反推每天的动线，得到的计划比"从零推荐"更贴合你的实际安排。

智能体会按问法自动选择四种工作模式：

| 模式 | 触发示例 | 执行内容 |
|---|---|---|
| 整趟旅行 | 帮我规划云南 8 天 | 意图收集 → 行前简报 → 路线骨架 → 城际交通 → 每日计划与酒店 → 自检交付 |
| 单日规划 | 我们在杭州有一天 | 节假日检查 + 单日计划 + 自检 |
| 空档填充 | 我在 X 附近有 2 小时空 | 15 分钟半径内 2–3 个选项 |
| 临场重排 | 高铁没赶上 / 下暴雨了 | 只重建受影响的那一天 |

### 渲染成品页

计划定稿后，渲染成设计版页面（默认插画版）：

```bash
python themes/render_theme2.py plan.geo.json --art plan.art.json -o trip-illustrated.html
python themes/qc.py trip-illustrated.html
```

七种主题任选，同一份行程可以每种都渲染一遍再挑：

| 主题 | 风格 |
|---|---|
| `theme2` | 插画（水彩风，默认） |
| `clay2` | 黏土 |
| `noir2` | 夜航（黑白） |
| `glass2` | 玻璃 |
| `journal` | 手账 |
| `zine` | 杂志 |
| `splash` | 闪屏 |

成品页完全自包含：双击即开、离线可看，每一天可展开高德路线地图，不泄漏任何密钥。可选写入真实路线后，地图按分段使用真实 path；缺失分段以不同线型注明“部分为直线示意”，无 path 时仍注明“站点连线示意”。

![高德路线地图](docs/screenshots/map-day2.jpg)

更多主题与行程气质的搭配示例（重庆 · 山城霓虹、上海 · 梧桐区漫步、沪京西 8 天多城），见 [`examples/`](examples/README.md)。

## 核心特性

- **逐小时每日计划**：营业时间、闭馆日、预约政策全部用工具核实后写入，每一跳都有高德/百度地图链接。
- **跨城交通深链 + 可选机票/酒店/铁路实时查询**：机票/火车票日期网格翻译成携程 / Trip.com / 去哪儿 / 12306 的直达链接，价格点开链接核对，不编造；配置飞猪 flyai CLI 与 `FLYAI_API_KEY` 后可查询机票/酒店只读参考候选（价格标"查询时参考价·单源"，遮蔽价绝不猜测），`train_live.py` 零依赖直连 12306 公共端点可查直达、席别余票/票价、中转与经停（无 MCP/Node/Key/登录），失败均完整回退深链。
- **住宿与预算**：按片区给出酒店候选（带日期深链）、人民币预算汇总、按截止日排序的预订清单。
- **行前简报**：应急号码、气象预警、假日调休、支付分层、离线地图准备，全部境内数据源。
- **图片三级策略**：内置素材库优先 → 模型原生生图 → 境内生图 key（阿里云百炼 / 硅基流动），任何一档下页面都完整交付。

## 配置（可选）

所有密钥均为可选；不配置任何密钥也能完成规划与渲染，只是相应能力降级。

| 环境变量 | 作用 | 缺失时的行为 |
|---|---|---|
| `AMAP_KEY` | 高德 Web 服务 key | 静态地图隐藏，真实路线不查询，时距用估算值 |
| `BAIDU_MAP_AK` | 百度地理编码回退 | 跳过回退 |
| `DASHSCOPE_API_KEY` / `SILICONFLOW_API_KEY` | 境内生图 | 图片走素材库或原生能力 |
| `TRIP_MAP_PROVIDER` | 深链服务商，`amap`（默认）或 `baidu` | 用默认高德 |
| `TRAIN_LIVE_CACHE_DIR` | 12306 车站表/查询路径缓存目录（默认临时目录） | 缓存写在系统临时目录 |
| `FLYAI_API_KEY` | 飞猪 flyai CLI 只读机票/酒店查询 | 机票/酒店仅用深链与价格区间 |
| `FLYAI_CLI` | flyai CLI 不在 PATH 时的绝对路径 | 按 PATH 上的 `flyai` 探测 |

**申请高德 key**：在高德开放平台（lbs.amap.com）注册并实名认证后创建应用，key 类型选「Web 服务」，个人开发者免费额度充足。拿到 key 后，在 skill 根目录建一个 `.env` 文件（参照 [.env.example](.env.example)）写入 `AMAP_KEY=你的key` 即可，脚本会自动读取。

## 命令行工具

`scripts/route_tools.py` 统一处理计划文件（`python scripts/route_tools.py <子命令> <plan.json> [参数]`）：

| 子命令 | 功能 |
|---|---|
| `geocode` | 高德/百度解析 stop 坐标并写回 |
| `check --live` | 用高德实时时距替换估算值（旧行为不变，不写计划） |
| `routes [--write]` | 可选真实步行/公交/驾车路线、分段和 WGS84 path；默认干跑 |
| `links --write` | 为每一跳生成高德/百度导航深链 |
| `kml` | 导出 Organic Maps 可用的离线 KML |
| `sun` | 每日日出日落（本地天文模型，零网络） |
| `ics` | 从预订清单生成带提醒的日历文件 |
| `poi` | 高德周边 POI 搜索 |

`routes` 只从环境变量或 `.env` 读取 `AMAP_KEY`，不提供密钥命令行参数。`--write` 原子写回到达站点的可选 `route` 字段并更新带 TTL 的 `routecache.json`；`--ttl 0` 禁用结构化缓存，`--strategy` 会进入缓存键。每跳独立失败，原交通描述、价格和深链不变。跨城公交在新结构化数据中标记降级，不把驾车路线冒充公交；旧 `check --live` 的驾车 ×1.25 折算保持不变。

```bash
python scripts/route_tools.py routes plan.geo.json           # 只查询和打印，不写文件
python scripts/route_tools.py routes plan.geo.json --write   # 显式写回路线与缓存
python scripts/flight_scan.py --from 杭州 --to 青岛 --depart 2026-10-26 --nights 4   # 机票/火车票深链网格
python scripts/train_live.py direct --from 北京南 --to 上海虹桥 --date 2026-12-01  # 可选铁路查询/深链回退
python scripts/plan_lint.py plan.geo.json --strict    # 渲染前内容门禁

# 实时铁路的上游核验、显式本地安装、状态与 fixtures（未实测真实 12306）见：
# references/train-live.md
```

## 数据来源

境内直连、免密钥优先；页面中的深链是价格与票务的真源。明细与回落链见 [references/data-sources.md](references/data-sources.md)。

| 数据源 | 用途 |
|---|---|
| 高德 / 百度开放平台 | 坐标解析、实时时距、周边 POI、静态路线图 |
| 12306 / 携程 / 去哪儿 / Trip.com | 机票与火车票深链；可选 12306 公共端点直连铁路查询 |
| timor.tech + 国务院放假安排 | 法定节假日与调休 |
| Open-Meteo / 中央气象台 | 天气预报与气象预警 |
| 本地 NOAA 天文模型 | 日出日落（零网络） |

## 致谢

- [Caveat](https://fonts.google.com/specimen/Caveat)（SIL OFL 1.1）与 [Lucide](https://lucide.dev/)（ISC），许可证全文见 [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md)
- 高德开放平台、百度地图开放平台、Open-Meteo、timor.tech
- 图片生成：阿里云百炼通义万相、硅基流动 Kolors

## 许可证

[MIT](LICENSE)
