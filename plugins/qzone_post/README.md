# qzone_post：QQ 空间说说插件

把「什么时候发说说、发什么」留在插件里，发布动作只走核心能力入口
`qzone.publish`（SnowLuma 的 `send_qzone_msg`），插件不碰任何 OneBot 协议。

## 触发方式

触发方式可以组合，全部由插件配置控制；所有**自动模式**都强制经过「节制三件套」
（冷却 `cooldown_minutes`、每日上限 `daily_cap`、安静时段 `quiet_hours`）。
命令是明确指令，不走草稿也不受节制限制。

| 方式 | 怎么开 | 行为 |
| --- | --- | --- |
| 命令 | 默认可用 | 聊天里发 `/说说 内容` 直接发布；只发 `/说说` 则由 LLM 按当前人格生成一条再发 |
| 间隔定时 | `interval_hours: 8` | 后台每 8 小时生成一条并发布 |
| 时间点定时 | `schedule_mode: times`<br>`times: ["08:30", "21:00"]` | 每天在固定时间点发，每点带 ±`jitter_minutes`（默认 10）随机抖动；每个槽位一天只触发一次 |
| 概率触发 | `prob: true`<br>`post_chance: 0.3` | bot 在任意会话完成一轮回复后按概率掷骰；后台检查 tick 也会掷一次 |
| LLM 触发 | `llm_decide: true` | 后台每 `check_interval_minutes`（默认 60）分钟把人格 + 最近群聊热点/记忆喂给模型，问它「现在有没有值得发的事」；模型返回 `{"post": bool, "content": str}`，`post` 为真才发 |

`schedule_mode` 取 `interval` / `times` / `both`，决定间隔定时与时间点定时怎么并存；
`llm_then_prob`（默认开）让 LLM 判断说「发」之后再过一次 `post_chance` 概率门，
和概率触发叠加。

## 草稿确认（默认开）

`draft_confirm: true` 时，自动模式（概率 / LLM / 定时）生成的内容不会直接发出去，
而是先私聊发给主人：

```
【说说草稿】自动模式下想发这条：
<内容>

回「发」就发出去；回「算了」或其他内容作废。30 分钟内有效。
```

主人回「发」才真正调用 `qzone.publish`；回其他内容或超时（`draft_timeout_minutes`）
作废。主人用 `owner_ids` 识别；不配置时退化为发给第一个活跃私聊会话，并在日志里注明。
草稿只存在内存里，插件重启即丢弃；一条草稿待确认期间不会再生成新的自动说说。

## 配置

复制 `config.example.yaml` 为 `qzone_post/config.yaml`，或把同结构的 dict 放进
`ctx.extras["qzone_post_config"]`（嵌入式/测试用）。

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `interval_hours` | `0` | 间隔定时（小时）；`0` 关闭 |
| `schedule_mode` | `"interval"` | `interval` / `times` / `both` |
| `times` | `[]` | 每天的时间点，如 `["08:30", "21:00"]`；也可写 `"08:30,21:00"` |
| `jitter_minutes` | `10` | 每个时间点的随机抖动（±分钟） |
| `check_interval_minutes` | `60` | 后台检查 tick 间隔（分钟），供 `prob` / `llm_decide` 使用 |
| `prob` | `false` | 打开概率触发 |
| `post_chance` | `0.3` | 概率门（0~1） |
| `llm_decide` | `false` | 打开 LLM 判断触发 |
| `llm_then_prob` | `true` | LLM 说发之后是否再过概率门 |
| `cooldown_minutes` | `480` | 两次自动发布之间的最短间隔 |
| `daily_cap` | `2` | 每日自动发布上限；`0` 不限 |
| `quiet_hours` | `"23:00-07:00"` | 安静时段不发；空串关闭 |
| `draft_confirm` | `true` | 自动内容先私聊发给主人确认 |
| `draft_timeout_minutes` | `30` | 草稿有效期（分钟） |
| `command` | `"说说"` | 命令名，实际触发是 `/说说`（前缀由 `plugins.command_prefix` 决定） |
| `owner_only` | `false` | 只允许 `owner_ids` 里的人用命令 |
| `owner_ids` | `[]` | 主人 QQ / sender_id；命令权限与草稿确认都用它 |
| `max_chars` | `60` | 生成内容的最大字数（超出按句末标点截断） |
| `topic_window_hours` | `6` | 取题材时回看群聊的小时数 |
| `topic_limit` | `20` | 一次取多少条群聊消息当题材 |

## 还需要打开平台能力

`qzone.publish` 是对外发布内容，SnowLuma 适配器默认关闭。要真发出去，配置里必须有：

```yaml
adapters:
  snowluma:
    capabilities:
      qzone: true
```

没打开、适配器不支持、或调用失败时，命令会直接回复原因；自动路径只记日志并等下一轮，
不会把主循环或后台任务带死。

## 生命周期

- 后台任务在插件启用后、宿主处理第一条消息时懒启动（宿主同步加载插件时还没有事件循环）。
- 插件被禁用或 bot 停机（`bot._stop`）后任务会自行退出；宿主禁用插件时
  `PluginManager.set_enabled` 会同步调用插件的 `on_stop`，任务立即清掉。
- 定时任务里的异常全部被捕获记录，单次失败不影响下一轮。

## 测试

`tests/test_qzone_post.py` 用假 capability 入口与假 LLM 覆盖命令路径、间隔/时间点定时、
概率门拦截、LLM 判断返回 `post: false` 不发、冷却 / 每日上限 / 安静时段拦截、
草稿确认的批准与作废（含超时、非主人、群消息）、能力不可用、停用后任务退出。
