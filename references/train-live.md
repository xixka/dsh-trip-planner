# 12306 实时铁路查询（可选增强·直连 HTTP）

`scripts/train_live.py` 为 Phase 3 增加出行日直达、席别票价/余票、中转和按需经停查询。
它是**可选**的零依赖增强：直连 12306 网页版自己在用的公共查询端点（匿名会话 Cookie +
页面隐藏查询路径），**不需要 MCP、Node/npm、API Key、登录或任何凭据**。
网络不通、超时、限流或返回畸形时，仍完整输出 `flight_scan.py` 的 12306 + 携程深链。
旧计划、旧字段、估算、人工核验和旧渲染不依赖本能力。

边界：不爬取搜索页，不使用浏览器/登录/乘客信息，不下单，不自动订票；
不把 Key 放入命令行、日志、fixture、缓存或 HTML。本适配器不需要 Key，也不写查询缓存。
携程链接只用于人工核对，**官方购票入口仅为 12306**。

## 数据来源与血统

传输层是对 **php737/train-12306**（MIT，Copyright (c) 2026 php737）端点处理的
净室重写：该上游本身是受 Joooook/12306-mcp 启发的独立 Python 实现，本仓库没有复制
任何上游源码。接入前核验记录（2026-11-25，GitHub API）：

| 项 | 已核验值 |
|---|---|
| 传输参考 | `php737/train-12306` 1.2.0（最后推送 2026-11-14，MIT） |
| 直达端点 | `GET /otn/leftTicket/query*`（页面 `CLeftTicketUrl` 动态发现） |
| 中转端点 | `GET /lcquery/query*`（页面 `lc_search_url` 动态发现，**无 `/otn/` 前缀**） |
| 经停端点 | `GET /otn/queryTrainInfo/query`（需 `train_no`，经 search 接口解析） |
| 车次搜索 | `GET search.12306.cn/search/v1/train/search`（车次号 → train_no） |
| 车站表 | `station_name.js`（约 3000 站，缓存 7 天，站名/城市名/电报码互查） |
| 凭据 | 无登录；仅 init 页返回的匿名会话 Cookie 与浏览器式 UA |

上游数据来自 12306 公共查询端点；**MIT 是软件许可，不代表 12306 官方授权、服务可用性
承诺或数据质量保证**。使用者仍须遵守 12306 当前条款、预售期与公告；不得把第三方实现
当作官方 SLA。适配器每个 HTTP 请求有超时、有限重试和请求间隔。

### 开箱即用

查询脚本无第三方 Python 依赖（纯标准库 urllib），**没有安装步骤**：

```bash
python3 scripts/train_live.py direct \
  --from 北京南 --to 上海虹桥 --date 2026-12-01
```

车站表与查询路径缓存在临时目录（可用 `TRAIN_LIVE_CACHE_DIR` 或 `--cache-dir` 覆盖，
7 天有效）；12306 改版导致「重定向到登录页」报错时，删掉缓存目录即自动重新发现。
`--verbose` 在 stderr 打印 HTTP 调试日志（不会进入 JSON 结果）。

## 查询流程

### 直达、时段、车型和主备

```bash
python3 scripts/train_live.py direct \
  --from 北京南 --to 上海虹桥 --date 2026-12-01 \
  --train-flags GD --earliest 6 --latest 12 --limit 3 --transfer auto
```

- 日期与所有到发时间统一为 `Asia/Shanghai`（UTC+8）。输入应是**具体车站或电报码**；
  北京/北京南、南京/南京南等同城站不合并，返回中同时记录站名和 telecode。
  城市名（如 `北京`）会解析到代表站，结果里记录的是实际 boarding 站。
- 适配器解析 12306 管道分隔行（57 字段）与 `yp_info_new` 定长票价块，并过滤
  占位脏数据（`99:59`/`24:00` 历时、出发=到达等）；`24:xx` 深夜时刻按次日处理。
- 默认最多展示 3 个候选，并给 1 个 primary + 至多 1 个 backup；不为了凑备选保留无票行。
- 每条候选保留 `train_code`、到发站/日期/时间、历时、`seats[]`。席别记录名称、代码、
  `price_cny` 和 `{status, available, count, raw}`；缺字段或非数值票价保持 `null`。
- `有/充足`、数字、`无/0`、`候补`分别归一化；`--`、空值和未知文本保持 `unknown`，
  不按无票处理。只有所有车次的所有已知席别都明确表示无票/候补时才汇总为 `sold_out`。
- 每个 HTTP 请求默认 15 秒超时，网络/超时默认重试 1 次（`--retries 0..2`）；限流
  （HTTP 429）不自动重试。stderr 只用于调试日志，不写入结果。

### 预售期

本地 guard 默认沿用项目已有的 15 天规划口径：`travel_date > 北京今天 + 14 天` 时直接返回
`not_on_sale`，**不发起网络请求、不反复请求**，并给 `expected_check_date = 出行日 - 14 天`。
这是预计关注日期，不是承诺的开售时间点；放票日期、时刻和预售期始终以 12306 公告为准。
可用 `--presale-days N` 显式调整，`0` 禁用本地 guard。窗口内上游明确返回未开售时也标记
`not_on_sale`，但不编造一个新的关注日期。

### 中转（默认仅直达无票）

`--transfer auto` 只有在直达结果为 `sold_out` 时才调用中转端点；
`no_service`、空响应、超时、未开售、样本和未知余票都不会触发自动中转。用户明确要求时用
`--transfer always`，`never` 完全禁用。可选 `--middle-station` 指定中转站。

中转结果保留两段完整车次与席别，并计算：

- `buffer_minutes`：第二段实际出发时间减第一段实际到达时间（含跨日，不做猜测）；
- `same_station`：同站 `true`、异站 `false`、来源缺失 `null`，同时列出两段换乘到发站；
- `last_arrival`：末段实际到达日期/时间，不以总历时反推；
- `buffer_ok`：是否满足可配置规划缓冲。默认同站 45 分钟、异站或关系未知 120 分钟，
  分别用 `--same-station-buffer` / `--cross-station-buffer` 调整。

这些分钟数只是**保守规划默认值，不是车站官方最低换乘时限**。缓冲不足、负跨段时间、段数不是 2、
站码冲突或时间畸形的候选不推荐；单行畸形被隔离，不覆盖其他有效行。直达仍为 live 时优先直达；
直达无票且中转 live 时才把中转作为计划合并目标。

### 经停（按需）

```bash
python3 scripts/train_live.py route --train G3 --date 2026-12-01
```

也可在直达命令上加 `--route-train G3`。只有用户需要经停时才调用；经停失败放在独立 `route`
子结果中，直达状态和计划合并不受影响。首末站 `--` 表示该方向无到/发时刻，输出为 `null`。
经停需要内部 `train_no`：先经 search 接口按车次号+日期解析，查不到即 `no_service`。

## 状态、优先级与展示约束

| 状态 | 含义 | 可写 `legs[]` |
|---|---|---|
| `live` | 直连 HTTP 返回且通过 schema/时间校验的本次 12306 查询 | **仅此状态且有推荐候选时可写** |
| `not_on_sale` | 超出配置预售期或上游明确未开售 | 否；给预计关注日期（可确定时） |
| `sold_out` | 所有有效候选均有明确无票/候补证据 | 否；auto 可继续查一次中转 |
| `no_service` | 成功响应为有效空列表或明确无匹配车次 | 否；**空响应不等于无票** |
| `sample` | 显式 `--fixture` 的离线样本，必须展示 `sample_date` | 否；严禁冒充 live |
| `unavailable` | 超时、网络、限流、重定向到登录页或传输不可用 | 否；保留人工结果并给深链 |
| `unverified` | 畸形/schema 不符、车站/日期错误或证据不足 | 否；保持未知 |

数据优先级：`live > 注明日期的 sample > 深链人工核对 > unverified`。
低优先级结果不会替换高优先级结果；失败合并不清空旧值。`unavailable` 与 `unverified` 都不等于
`no_service`，有效空列表也不等于 `sold_out`。

只有 `live` 的旧渲染字段可以写实时价/余票，并必须带：
`12306 live · 查询 {queried_at} · 查询时参考，购票以 12306 为准`。
样本可以离线展示，但文本必须出现“样本日期 YYYY-MM-DD（非实时）”，价格前仍有 sample 身份。
其他状态只展示状态、错误分类、预计关注日期（如有）和人工深链，不展示所谓实时价/余票。

## 增量写入 `legs[]` 与旧计划兼容

查询命令加 `--plan path/plan.geo.json --write`，或对已保存的规范化响应执行：

```bash
python3 scripts/train_live.py merge --plan path/plan.geo.json \
  --response path/live-response.json --write
```

- 已有人工匹配行（同日、同原始到发站）：**只新增/更新 `rail_live` 子对象**，旧 `type/date/
  carrier/from/to/dep/arr/price/bags/link/note/backup` 等人工字段原样保留。
- 没有匹配行：追加一条含全部旧字段的可渲染 leg，并附 `_train_live_managed:true`、
  `rail_live` 和可选 `refund_change`。`backup` 是旧渲染可读文本，完整备选留在 `rail_live`。
- 同一 managed 查询再次得到 live 时更新该 managed 行，避免重复追加。人工增加的其他键保留。
- 样本、无票、无车次、未开售、超时、限流、空/畸形响应、未知状态均不写文件。
  原子写回只在成功合并后发生；读取/解析/合并失败保留原文件字节。
- `rail_live` 保存来源（传输参考版本）、`queried_at`、`travel_date`、query、候选、主备、
  换乘/末段信息和深链。新字段会被旧渲染器自然忽略。

合并命令输出的 `plan_merge.written` 表示“内存中产生了可写变更”；只有同时指定 `--write`
才写回文件。计划文件属于用户数据，不要把 live 响应、fixture 或个人计划提交到仓库。

## 离线 fixture 与 CI

所有测试均不访问真实 12306：

```bash
python3 -m unittest -v tests/test_train_live.py
# 或同时跑原插件测试和本套件
npm test
```

显式样本示例：

```bash
python3 scripts/train_live.py direct --from 北京南 --to 上海虹桥 \
  --date 2026-08-15 --fixture tests/fixtures/train_live/direct-live.json --output text
python3 scripts/train_live.py direct --from 北京南 --to 上海虹桥 \
  --date 2026-08-15 --fixture tests/fixtures/train_live/direct-sold-out.json \
  --transfer always --transfer-fixture tests/fixtures/train_live/transfer-live.json
```

`tests/fixtures/train_live/` 覆盖：直达与主备、明确无票、有效空列表无车次、未知 `--` 余票、
中转同/异站及缓冲、经停、未开售、空传输响应、畸形 JSON、超时、限流和旧计划。
重点断言状态不混淆、限流不重试、超时不等于无车次、样本不冒充 live、非 live 不写计划、
人工字段不变以及旧 `render_plan.py` 仍能输出新增 live 行。

## 验证边界

传输层重写于 2026-11-25 完成并做了真机只读验证（北京南→上海虹桥直达、深拉萨中转、
G1 经停）。12306 未见官方公开 API 承诺；查询路径、字段位置或条款变化都可能使 live 失效
（表现为 `unavailable` + 深链兜底，不会污染计划）。交付 Agent 必须在少量只读查询成功后再
使用 live，任何未实测/失败项保持深链人工核验。
