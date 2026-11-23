# LLM 预算闸默认阈值的数据口径（Q11）

本文记录 `rubenbot/llm_budget.py` 内置默认上限是怎么来的，便于以后复核或重算。
两个输入都只读线上数据，不改动生产库。

## 1. 线上日调用量

来源：家里云 `~/rubenbot-test/data/rubenbot.db` 的 `llm_usage` 表，只读方式
（`sqlite3` 以 `mode=ro` 打开），2026-11-23 取样。

每日总量：

| 日期 | 调用次数 | 成功 | prompt tokens | completion tokens |
| --- | ---: | ---: | ---: | ---: |
| 2026-11-20 | 264 | 244 | 285,513 | 170,185 |
| 2026-11-21 | 244 | 244 | 468,185 | 172,274 |
| 2026-11-22 | 1,186 | 1,183 | 3,240,595 | 588,569 |
| 2026-11-23 | 667 | 662 | 2,481,591 | 446,617 |

单任务日峰值（4 天内的最大值）：

| 任务类型 | 峰值日调用 | 峰值日 tokens |
| --- | ---: | ---: |
| gen_stage1 | 365 | 1,676,561 |
| gen_stage2 | 299 | 1,372,420 |
| extract | 180 | 337,711 |
| image_description | 100 | 126,141 |
| sticker_description | 81 | 61,991 |
| sticker_select | 57 | 152,846 |
| conversation_state | 52 | 87,638 |
| chat | 41 | 130,726 |
| rolling_summary | 40 | 141,202 |
| group_profile | 29 | 113,936 |
| consolidate | 23 | 68,702 |
| gate | 15 | 18,931 |
| distill | 14 | 62,499 |
| event_judge | 4 | 1,945 |

默认上限按峰值 × 3～5 倍取整（节假日、补数据或新群入群会高于样本峰值，留出余量
后仍是「明显异常才熔断」的量级）：

| 任务类型 | 峰值 | 默认 `daily_calls` |
| --- | ---: | ---: |
| gen_stage1 | 365 | 1500 |
| gen_stage2 | 299 | 1200 |
| extract | 180 | 600 |
| image_description | 100 | 400 |
| sticker_description / sticker_select / conversation_state / chat | 41～100 | 300 |
| rolling_summary | 40 | 200 |
| group_profile / consolidate | 23～29 | 150 |
| distill / gate | 14～15 | 100 |
| event_judge | 4 | 50 |

未列出的任务类型（插件自定义 `task` 等）使用 `llm.budget.default_daily_calls`，默认 500。

## 2. 模型单价

中转站 `api.heevcmlz.cn`（New API 部署）没有给出 `workbuddy-deepseek-v4.1-flash`
的公开单价：

- `GET https://api.heevcmlz.cn/api/pricing`（匿名与带线上 key 各试一次）返回
  `data: []`，只有分组倍率 `group_ratio: {"auto": 1}`，没有模型价格表；
- `GET /api/ratio_config` 返回 403「倍率配置接口未启用」；
- `GET /api/user/self` 用线上 key 也返回 401（该 key 不是控制台登录令牌）；
- 家里云的 `config.yaml` 及历次备份里只有 `base_url` / `chat_model`，没有单价记录。

因此**没有可信的 workbuddy 单价可用**，仓库不写死任何数字：

- `llm.budget.pricing` 默认为空；
- 费用档 `daily_cost_usd` 默认 `null`（不启用）；
- 没配任何单价时估算费用恒为 0，费用档不参与判断，只记一条启动 warning 提醒
  「配了费用上限但没配单价」。次数档照常生效。

需要费用档时，在 `config.yaml` 里按实际计费填 `pricing`（美元 / 百万 token），
例如 `deepseek/deepseek-chat: {input: 0.27, output: 1.10}`，或用 `*` 兜底。
