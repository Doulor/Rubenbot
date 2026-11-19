# qzone_post：QQ 空间说说插件

把「什么时候发说说、发什么」留在插件里，发布动作只走核心能力入口
`qzone.publish`（SnowLuma 的 `send_qzone_msg`），插件不碰任何 OneBot 协议。

## 触发方式

| 方式 | 怎么开 | 行为 |
| --- | --- | --- |
| 命令 | 默认可用 | 聊天里发 `/说说 内容` 直接发布；只发 `/说说` 则由 LLM 按当前人格生成一条再发 |
| 定时 | `interval_hours: 8` | 后台每 8 小时生成一条并发布（题材优先最近群聊，其次记忆，都没有写日常向） |

同一个插件还支持 `owner_only`（默认关）限定谁能用命令，以及自定义命令名
`command`（默认「说说」）。

## 配置

复制 `config.example.yaml` 为 `qzone_post/config.yaml`，或把同结构的 dict 放进
`ctx.extras["qzone_post_config"]`（嵌入式/测试用）。

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `interval_hours` | `0` | 定时间隔（小时）；`0` 关闭定时任务 |
| `command` | `"说说"` | 命令名，实际触发是 `/说说`（前缀由 `plugins.command_prefix` 决定） |
| `owner_only` | `false` | 只允许 `owner_ids` 里的人用命令 |
| `owner_ids` | `[]` | 主人 QQ / sender_id；`owner_only` 开启时必须配 |
| `max_chars` | `60` | 生成内容的最大字数（超出按句末标点截断） |
| `topic_window_hours` | `6` | 定时写说说时回看群聊的小时数 |
| `topic_limit` | `20` | 一次取多少条群聊消息当题材 |

## 还需要打开平台能力

`qzone.publish` 是对外发布内容，SnowLuma 适配器默认关闭。要真发出去，配置里必须有：

```yaml
adapters:
  snowluma:
    capabilities:
      qzone: true
```

没打开、适配器不支持、或调用失败时，命令会直接回复原因；定时任务只记日志并等下一轮，
不会把主循环或后台任务带死。

## 生命周期

- 后台任务在插件启用后、宿主处理第一条消息时懒启动（宿主同步加载插件时还没有事件循环）。
- 插件被禁用或 bot 停机（`bot._stop`）后任务会自行退出；宿主禁用插件时
  `PluginManager.set_enabled` 会同步调用插件的 `on_stop`，任务立即清掉。
- 定时任务里的异常全部被捕获记录，单次失败不影响下一轮。

## 测试

`tests/test_qzone_post.py` 用假 capability 入口与假 LLM 覆盖命令路径、定时路径、
能力不可用、`interval_hours=0` 不建任务、停用后任务退出。
