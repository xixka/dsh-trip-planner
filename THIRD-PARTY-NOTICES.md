# Third-party notices

The skill's own code and documentation are MIT-licensed (see [LICENSE](LICENSE)).
The repository also **redistributes** the following third-party assets, each under
its own licence, which travels with them:

| What | Where in this repo | Licence | Notice text |
|---|---|---|---|
| **Caveat** variable font (embedded as a data-URI `@font-face` in the journal / zine pages) | `themes/assets/caveat-vf.woff2` | SIL Open Font License 1.1 — Copyright 2014 The Caveat Project Authors | [themes/assets/OFL-Caveat.txt](themes/assets/OFL-Caveat.txt) |
| **Lucide** icon paths (inlined as SVG sprites by the themed renderers) | `themes/lucide-icons.json` | ISC — Copyright (c) Lucide Icons and Contributors; some icons derive from Feather (MIT, Cole Bemis) | [themes/LICENSE-lucide.txt](themes/LICENSE-lucide.txt) |

Generated images (`themes/assets/*.webp`) were produced for this project with
`openai/gpt-image-2` (via OpenRouter, upstream) and are published under the repository's
MIT licence. Their prompts, parameters and cost are recorded in
`themes/assets/manifest.json`（境内-only 改造后新图一律走阿里云百炼/硅基流动生成；
原 portal 视频资产已随视频能力整体移除）.

Runtime data sources (高德/百度地图开放平台, Open-Meteo, timor.tech, 中央气象台
nmc.cn, 中国地震台网 ceic.ac.cn) are queried live and are **not** redistributed; their
terms — including Amap/Baidu platform quotas and attribution rules — are honoured in the
scripts and printed on the rendered pages where required. See
`references/data-sources.md`.（原 Nominatim / sunrise-sunset.org / Nager.Date /
frankfurter.dev / Google Flights 等境外数据源已整体移除。）

Optional `scripts/train_live.py` queries the 12306 public web endpoints directly over
HTTPS (anonymous session cookie only; no login, no MCP, no bundled dependencies).
Its transport layer is a clean-room reimplementation of the endpoint handling in
[php737/train-12306](https://github.com/php737/train-12306) (MIT, Copyright (c) 2026
php737), itself an independent reimplementation inspired by Joooook/12306-mcp
(MIT, Copyright (c) 2025 Jok). No upstream source code is bundled or copied; the
upstream is referenced for lineage only. Neither licence asserts 12306 endorsement
or service terms. See `references/train-live.md`.
Optional live flight/hotel candidates (飞猪 flyai) come from a **user-installed flyai
CLI** with the user's own `FLYAI_API_KEY` (see `references/flyai.md`). The CLI is not
bundled, not auto-installed and not accessed in CI; 飞猪/Fliggy 商标与服务条款归阿里
巴巴集团所有，本仓库与飞猪无从属或授权关系，返回的参考价仅为"查询时参考价·单源"，
不构成报价或库存承诺。无 CLI/无 key/体验模式/失败时完整回退深链。CLI 契约未实测
（编写时环境中无 flyai CLI），首次使用前须以 `flyai --help` 与
`scripts/flyai_provider.py doctor` 核对。
