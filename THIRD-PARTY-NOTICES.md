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

Optional `scripts/train_live.py` can start a user-installed `Joooook/12306-mcp` server;
the upstream source and dependencies are **not bundled**. The installer pins commit
`ff6439da6f63d7d72181abea4568abd69878c600` (`12306-mcp` 0.3.10), preserves its MIT
license (Copyright (c) 2025 Jok), applies only the recorded Axios-timeout patch, and
writes license/patch hashes into the local installation record. This licence does not
assert 12306 endorsement or service terms. See `references/train-live.md`.
