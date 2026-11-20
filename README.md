# RubenBot

RubenBot 是一个以**记忆**为核心的拟人社交聊天 bot 框架。它面向群聊/私聊场景，目标不是做一个随叫随到的问答助手，而是像一个住在群里的普通人：有自己的性格和节奏，会记得你说过的事，也会在合适的时候插一句嘴。

- **记忆优先**：按人建档，消息先落流水，再由模型提炼耐久事实；检索时按稀有词加权，定期归纳去冗。
- **轻量**：Python 3.12，核心依赖只有 `httpx`、`websockets`、`PyYAML`，存储用标准库 SQLite。
- **适配器模式**：主循环不感知平台。已内置 Console（本地调试）与 SnowLuma（OneBot v11）。
- **插件化**：目录式插件 + 消息钩子 + 命令注册。
- **可撤销**：记忆的合并/删除都会写变更日志，支持 `restore` 回滚。

许可证：MIT。记忆层借鉴了 nanoMuse 的设计思路，代码全部原创。

---

## 架构

```
                            ┌───────────────────────────────┐
  SnowLuma / QQ  ──WS──▶    │  adapters/snowluma.py         │
  终端 stdin     ───────▶   │  adapters/console.py          │
                            │  （统一转成 MessageEvent）      │
                            └───────────────┬───────────────┘
                                            │
                                            ▼
                            ┌───────────────────────────────┐
                            │  core.RubenBot                │
                            │  1. 落 raw_messages / people  │
                            │  2. 插件 on_message / 命令     │
                            │  3. 合并窗口 → 意愿决策        │
                            │  4. 拼上下文（人格+记忆+历史）  │
                            │  5. LLM 生成 → 否决检查 → 拆条 │
                            └──────┬───────────────┬────────┘
                                   │               │
                    ┌──────────────▼──┐   ┌────────▼──────────────┐
                    │ memory/         │   │ llm.LLMClient         │
                    │  store.py       │   │ OpenAI 兼容 /chat/    │
                    │  retrieve.py    │   │ completions           │
                    │  extract.py     │   └───────────────────────┘
                    │  consolidate.py │
                    └────────┬────────┘
                             │
                    ┌────────▼────────┐
                    │ SQLite (单文件) │
                    │ people /        │
                    │ memories /      │
                    │ raw_messages /  │
                    │ memory_log /    │
                    │ meta            │
                    └─────────────────┘
```

主循环之外还有一个后台任务（默认每 5 分钟检查一次），负责三件事：

- **抽取**：某个会话待处理消息攒够 `extract_every_n` 条，或会话静默超过 `extract_silence_seconds` 时，按发送者分别让模型从流水里提炼「关于这个人的耐久事实」。
- **归纳**：某人的新增记忆达到阈值，或距上次归纳超过间隔时，让模型提议 `merge` / `drop`，再由代码逐条校验执行。
- **滚动摘要**（默认开）：某个会话在摘要水位之后积累的原文超过 `trigger_after` 条时，把除最近 `keep_recent` 条之外的那批消息增量折叠进五段式摘要；之后拼上下文时只带「摘要块 + 水位之后的原文」。摘要只压缩本会话的对话历史，和跨会话的人物记忆各管各的。

### 记忆层数据流

```
消息 ──▶ raw_messages ──(抽取)──▶ memories ──(检索)──▶ 注入系统提示词
                                      ▲   │
                                      │   └──(归纳：模型提议 / 代码裁决)──▶ memory_log
                                      └──────────── restore 撤销 ◀──────────┘
```

**检索**不依赖 embedding：中文按字 bigram、英文按整词切分，用 IDF 给稀有词更高权重，整句原文命中额外加分。注入顺序为「当前说话人的相关记忆 → 本群 scope 的相关记忆 → 最近记忆兜底」，总量不超过 `inject_limit`。embedding 融合接口已预留，v1 不启用。

**归纳**遵循「模型只许提议，代码负责裁决」：

1. 合并句不得出现原记忆里没有的字/词（连接词白名单除外），防止模型编造事实；
2. `source = user` 的记忆不允许删除，也不参与合并（保守处理）；
3. 一轮最多移除 20%，且至少允许 4 条，超出的留到下一轮；
4. 按人分组，天然不会跨人合并；
5. 每次变更写 `memory_log`（before/after 全文 + 理由），可撤销。

### 记忆 v2：读取侧（渐进披露与分寸）

写入侧把消息沉淀成「时间线事件 + 事实 → 画像」，读取侧按「像人一样」的原则分层注入：

- **T0 群成员索引（常驻）**：本群近 30 天活跃成员每人一行「昵称｜一句话身份｜话题钩子」，
  兼任别名表；缓存 10 分钟或画像一更新就失效。
- **T1 预拉取**：说话人核心画像常驻；被 @、被昵称/钩子命中的在场者拉核心画像与话题相关旧事
  （时间模糊成「昨天/上周」，不报具体日期）。事实层只留真命中，已提炼进画像的不重复注入；
  总预算超了先砍事实、再砍时间线，画像核心不动。
- **T2 recall 工具**：模型支持 OpenAI 工具调用时，生成过程最多两轮 `recall(person_query, topic,
  time_range)` 深查；中转/模型不支持、或用不上就自动退化为单次生成。
- **跨会话接续**：私聊时把本人近 24h 在别处会话的要点压成背景摘要；群聊永不注入私聊来源。
- **分寸审查口（`memory/gate.py`，唯一）**：跨场合候选按「同场合/公开放行 → 三档隐私 → 封存 →
  已有裁定 → 高敏感扣下并待问 → 模型按准则权衡」判定；模型低置信或任何一步异常一律扣下
  （fail closed）。裁定落到 `rulings` 复用，同一项对同一类受众不再重复问。
- **自我**：bot 自己在本会话最近的行为（自我时间线）作为背景注入，只作近况参考，不改人设。
- **隐私命令**：`/隐私` 查看档位与「记得我什么」（画像只在私聊摊开），`/隐私 模式 正常|隐私|不记`
  切换，`/隐私 清空` 真删画像/事件/事实并留审计痕迹；灰区拿不准的事会在私聊里自然时机问本人一次。

### 模块一览

| 路径 | 职责 |
| --- | --- |
| `rubenbot/config.py` | 读取 YAML 配置，展开 `${ENV_VAR}`，未知键忽略，缺文件走默认值 |
| `rubenbot/persona.py` | 加载 SOUL.md 风格的 Markdown 人格；不存在时生成默认人格 |
| `rubenbot/llm.py` | OpenAI 兼容客户端；按角色（`chat` / `extract` / `consolidate` / `fast` / `summary` / `vision` 及自定义）选模型；408/429/5xx 指数退避重试；每次调用写用量表 |
| `rubenbot/core.py` | 消息主循环、合并窗口调度、决策、上下文拼装、拆条发送、后台维护 |
| `rubenbot/decision.py` | 回复意愿系统：消息合并、硬触发、意愿评分、生成否决标记 |
| `rubenbot/memory/store.py` | SQLite 读写唯一入口：人物、记忆、流水、变更日志、撤销 |
| `rubenbot/memory/retrieve.py` | 分词、Jaccard、IDF 加权检索；读取侧（群成员索引 / 预拉取 / 跨会话 / recall） |
| `rubenbot/memory/gate.py` | 唯一的分寸审查口：跨场合记忆的放行/扣下/待询问判断（fail closed） |
| `rubenbot/memory/extract.py` | 批量抽取 + 去重 + 标记已处理 |
| `rubenbot/memory/consolidate.py` | 归纳提议的解析、校验与执行 |
| `rubenbot/memory/summary.py` | 会话滚动摘要：本会话早期对话压成五段式摘要，增量折叠、带水位与开关 |
| `rubenbot/adapters/base.py` | `BaseAdapter`、`MessageEvent`、`Segment` |
| `rubenbot/adapters/console.py` | stdin/stdout 调试适配器 |
| `rubenbot/adapters/snowluma.py` | OneBot v11 正向 WS 收事件 + HTTP API 发送 |
| `rubenbot/plugins/loader.py` | 插件发现、钩子与命令分发；显式启用制与热启停 |
| `rubenbot/__main__.py` | 命令行入口 `rubenbot` / `python -m rubenbot` |
| `plugins/hello/` | 示例插件：`/hello` 命令 + `on_message` 钩子（默认关闭，需显式启用） |
| `plugins/privacy/` | 内置插件：`/隐私` 命令（档位 / 自查 / 清空）与灰区询问投递（默认关闭，需显式启用） |
| `plugins/qzone_post/` | 说说发布插件（命令 / 定时 / 概率触发）；自动互动开关默认关闭（默认不启用，需显式开启） |
| `plugins/emoji_react/` | 贴表情反应插件：跟随回复 / 关键词触发（默认关闭，需显式启用） |
| `data/SOUL.md` | 默认人格示例 |

---

## 快速开始

### 1. 安装

需要 Python 3.12：

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### 2. 配置

```bash
cp config.example.yaml config.yaml
export RUBENBOT_API_KEY="你的模型 API Key"
```

`config.yaml` 里至少要配好 `llm.base_url` 和 `llm.chat_model`。配置支持 `${ENV_VAR}` 展开，建议把密钥放环境变量而不是文件里。

只想本地试玩时，把 `adapters.snowluma` 整段注释掉，只保留：

```yaml
adapters:
  console: {}
```

接 SnowLuma 时则反过来，填好 OneBot v11 的 WS/HTTP 地址：

```yaml
adapters:
  snowluma:
    ws_url: "ws://127.0.0.1:3001"
    http_url: "http://127.0.0.1:3000"
    access_token: ""
    self_id: ""   # 可选；留空则从事件里的 self_id 自动取
```

### 3. 运行

```bash
rubenbot -c config.yaml
# 或
python -m rubenbot -c config.yaml
```

常用参数：

- `--console`：强制启用 ConsoleAdapter（即使配置里没写）
- `--init-persona`：把默认人格写到配置的路径后退出
- `--log-level DEBUG`：覆盖日志级别

ConsoleAdapter 的输入格式：

| 输入 | 含义 |
| --- | --- |
| `你好呀` | 私聊（必定回复） |
| `[摸鱼群] 今天好累` | 在名为「摸鱼群」的群聊里发言 |
| `[摸鱼群\|阿花] 今天好累` | 指定群名与昵称 |
| `/hello` | 触发示例插件命令（需先在 `plugins.enabled` 或 WebUI 里启用 `hello`） |

群聊里普通消息默认只拿 `decision.base`（0.10）的基础意愿，单靠它过不了阈值；被 @ / 被回复 / 叫到名字（`Ruben`）时必回。想调试时可以在配置里临时调高 `decision.base` / `decision.threshold`。插件默认全部关闭，想试 `/hello` 请在 WebUI 的「插件」页打开 `hello`，或在配置里写 `plugins.enabled: ["hello"]`。

### 4. 测试

```bash
pytest
```

当前共 723 个测试，覆盖存储 CRUD / 变更日志与撤销、检索打分、抽取去重、归纳校验（不许新词、20% 上限、user 来源不可删）、画像提炼与时间线压缩、记忆读取侧（索引 / 预拉取 / 预算裁剪 / 跨会话 / 分寸审查 / 隐私命令）、会话滚动摘要（触发 / 水位 / 增量折叠 / 上下文组装 / 开关与容错）、回复决策（合并窗口、动量衰减、占比抑制、冷却、硬触发、生成否决）、两段式生成（指导注入 / 失败回退 / 用量打标）、主循环、插件（显式启用制与 WebUI 开关写回）、SnowLuma 事件解析，以及 Console + 假 LLM 的端到端流程。

---

## 配置速查

| 配置段 | 关键项 | 说明 |
| --- | --- | --- |
| `llm` | `base_url` `api_key` `chat_model` | 任意 OpenAI 兼容接口 |
| | `extract_model` `consolidate_model` | 留空则跟随 `chat_model` |
| `models` | 可扩展的角色 → 模型映射（如 `fast` / `summary` / `vision`，未配的角色回退 `chat_model`），优先于上面两个旧字段 |
| `timeout` `max_retries` | 单次请求超时（秒）与失败重试次数（408/429/5xx，间隔 1s / 3s） |
| `persona` | `path` | 人格文件，默认 `data/SOUL.md` |
| `database_path` | — | SQLite 路径，默认 `data/rubenbot.db` |
| `memory` | `extract_every_n` | 会话攒够多少条未抽取消息触发 |
| | `extract_silence_seconds` | 会话静默多久后触发 |
| | `extract_min_pending` | 单人至少积累几条才抽取 |
| | `inject_limit` | 回复时最多注入多少条记忆 |
| | `dedupe_threshold` | token Jaccard 去重阈值 |
| | `consolidate_new_threshold` | 新增记忆达到多少条触发归纳 |
| | `consolidate_interval_hours` | 距上次归纳超过多久触发 |
| | `raw_keep_per_chat` | 每会话保留的已处理流水条数 |
| | `index_max_members` `index_ttl_seconds` `index_active_days` | 群成员索引（T0）的人数、缓存与活跃窗口 |
| | `inject_budget_tokens` `profile_inject_max_chars` | 记忆注入预算与画像篇幅；超预算先砍事实、再砍时间线 |
| | `cross_session_hours` `self_inject_hours` `self_inject_limit` | 私聊跨会话摘要与自我注入的回看窗口 |
| | `gate_confidence_threshold` `gate_ask_high_sensitivity` | 分寸审查的置信度下限与灰区询问开关 |
| | `recall_tool_enabled` `recall_tool_max_rounds` | recall 工具循环开关与轮数（模型不支持时自动退化） |
| | `rolling_summary` | 会话滚动摘要（默认开）：`enabled` / `trigger_after`（未摘要原文超过多少条触发，默认 60）/ `keep_recent`（始终保留的最新原文条数，默认 30）/ `max_chars`（五段式摘要总长上限，默认 800）；摘要用 `summary` 角色生成，未配置时回退 chat |
| `chat` | `history_size` | 上下文带入的最近消息条数（只计真实消息，系统/工具条目不占窗口） |
| | `history_ttl_hours` | 超过这个时间的旧消息不再拼进上下文 |
| | `dedupe_ttl_seconds` | 入站去重窗口：同一平台消息 id 在此窗口内重复到达即丢弃（默认 300 秒，0 关闭） |
| | `split_max_parts` `split_part_chars` `split_delay_seconds` | 长回复拆条策略 |
| `chat` | `history_size` | 上下文带入的最近消息条数 |
| | `split_max_parts` `split_part_chars` | 长回复拆条策略（按句拆，避开括号/引号/颜文字/URL） |
| | `type_speed` `type_delay_max_seconds` | 按字数估算打字停顿的全局倍率 / 单条上限 |
| | `strip_action_parens` | 发送前移除「（笑）」这类括号动作描写（默认关） |
| `decision` | `merge_window_seconds` `hard_merge_window_seconds` | 消息合并窗口（普通 / 硬触发） |
| | `batch_max_seconds` | 一批消息从首条起最多等多久（防无限续命） |
| | `urgent_seconds` | 批里出现 @/回复 bot 时，截止时间提前到「首条 + 该值」 |
| | `aliases` | 视为「叫名字」的别名，空则用 `chat.bot_name` |
| | `base` `threshold` | 每群基础意愿 / 开口阈值 |
| | `momentum_bonus` `momentum_half_life_seconds` | 对话动量加分与衰减半衰期 |
| | `relevance_bonus` `relevance_scale` | 话题相关性（记忆检索）加分 |
| | `ratio_window` `ratio_threshold` `ratio_silence` `ratio_penalty` | 发言占比抑制 |
| | `cooldown_seconds` `cooldown_penalty` | 发言后冷却 |
| | `veto_marker` | 模型输出该标记即静默丢弃 |
| | `groups` | 每群覆盖：`activity_multiplier` / `base` / `quiet_hours` |
| `generation` | `two_stage.enabled` | 两段式生成（默认关）：决定要回之后，先让 `fast` 模型写一段简短指导，再按指导写正文；第一段失败/超时/空内容自动回退单段直出。两段用量分记 `gen_stage1` / `gen_stage2` |
| `plugins` | `dir` `enabled` `command_prefix` | 插件显式启用：`enabled` 里没列的插件不加载，留空表示全部关闭 |
| `background_check_minutes` | — | 抽取/归纳检查间隔 |

---

## 插件开发

插件默认**全部关闭**（显式启用制）：只有在 `config.yaml` 的 `plugins.enabled` 列表里出现的插件才会加载，不写或留空表示一个都不启用。最省事的开启方式是启动后打开 WebUI 的「插件」页，逐个打开开关——保存后立即热生效（现场 `register` / 触发 `on_stop`），并写回 `plugins.enabled`；也可以直接改配置：

```yaml
plugins:
  enabled: ["hello"]   # 只启用 hello 插件
```

在插件目录下新建 `plugins/<name>/plugin.py`，暴露 `register(ctx)`：

```python
def register(ctx):
    @ctx.command("ping")
    async def ping(runtime, event, args):
        return "pong"

    @ctx.on_message
    async def preprocess(runtime, event):
        # 返回 False 拦截这条消息，返回 True/None 放行
        return True
```

可用钩子：

- `on_message(handler)`：`handler(ctx, event)`，返回 `False` 拦截消息
- `on_before_reply(handler)`：`handler(ctx, event, messages)`，返回新的 messages 可改写上下文
- `on_after_reply(handler)`：`handler(ctx, event, reply)`
- `command(name, handler)`：`handler(ctx, event, args)`，返回字符串作为回复；可直接调用也可作装饰器

钩子和命令都同时支持同步/异步函数，异常会被捕获并记录，不会拖垮主循环。`ctx` 上还能拿到 `store`、`config`，以及 `runtime` 中的 `persona`、`adapters`、`retriever`、`extractor`、`consolidator`；记忆读取侧与分寸审查口经 `ctx.extras["memory_reader"]` / `ctx.extras["privacy_gate"]`（或 `runtime.memory` / `runtime.gate`）取用。

---

## 决策记录

以下记录规格未写死、实现时按「轻量、简单、不过度设计」原则做出的选择。

1. **存储用同步 SQLite + 单连接 + 可重入锁**
   记忆规模是「一个人的几百条事实」，SQLite 完全够用。不引 ORM、不引异步驱动，所有读写收敛在 `MemoryStore` 一个入口，线程安全由一把 `RLock` 保证。

2. **记忆检索不用向量库，IDF 加权先跑起来**
   规格要求无 embedding 也能用。实际打分采用平滑 IDF `log(1 + N/(1+df))`，并乘以覆盖率系数 `0.5 + 0.5 * matched/len(query)`，避免长句偶尔命中一个词就排到前面；整句命中再加固定 bonus。`Retriever.embed_fn` 留作后续融合位。

3. **抽取按「平台 + 会话 + 发送者」聚合，而不是全局按人**
   同一个人在 A 群和 B 群的语境可能不同，按会话聚合能让 prompt 里的上下文更完整；person_id 仍是全局的 `platform:uid`，记忆跨群共享。

4. **抽取后无论是否产出事实都标记已处理**
   模型明确说「没有值得记录的事实」时，再反复喂同一批消息只会浪费调用。只有 LLM 调用失败时才保留 pending，下一轮重试。

5. **bot 自己的回复也写进 `raw_messages`，但用 `__bot__` 标记且 `processed=1`**
   这样下一轮上下文能看到自己的话，对话连贯；同时它不会被当成「人物」，也不参与记忆抽取。出站回执里的平台消息 id 一并落库，后续消息引用它时能解析出「[我] 说过什么」。

5.1 **历史按结构化格式渲染，消息 id 双向落库**（借鉴 MaiBot 研读 #1/#17 的最小版）
   `raw_messages` 新增 `platform_msg_id` / `reply_to_id` 两列（旧库启动时自动补列）。拼历史时别人的消息渲染成 `[HH:MM] [昵称] 正文`，bot 自己的消息只放干净正文、不加前缀（否则模型会模仿「[HH:MM] [我]」格式把前缀一起生成出来），发送侧还会剥掉回复开头意外出现的转录行前缀兜底；被引用的消息只在**本批历史**里按 `reply_to_id` 查，查到就在正文后附 `（引用 [昵称]：正文截断 50 字）`，查不到只标 `（引用了一条消息）`。本轮正在回的那条（回复目标）正文单独截断 300 字放进「上下文要求」区，不整段重复。`chat.history_size` 窗口只计真实消息：`sender_id` 以 `__` 开头的系统/工具条目不占名额。入站事件按 `(platform, platform_msg_id)` 在 `chat.dedupe_ttl_seconds`（默认 5 分钟）内去重，重复事件直接跳过。

6. **群聊开口由「意愿系统」决定，不是平坦概率**（`rubenbot/decision.py`）
   私聊、被 @ / 被回复 / 叫到名字（`aliases`，默认 `chat.bot_name`）是硬触发，直接生成。普通群消息先过合并窗口（同一发送者 4 秒内连发合成一轮，别人插话则提前放行），再算意愿分：`基础值 + 动量（半衰期衰减） + 相关性（记忆检索最高分归一化）`，减去 `发言占比惩罚`（最近 N 条里 bot 占比超过 20% 开始减，60% 直接静默）和 `发言后冷却`（默认 30 秒）。分数过 `threshold` 才生成；离阈值很近时加一点随机扰动，避免机械感。生成阶段模型可以只输出 `[不回复]` 自行否决。每次决策用 INFO 日志记录回/不回、分数和主要因子，`bot.last_decision` 保留最近结果供 WebUI 读取。所有时间与随机源都可注入，单测用假时钟/固定随机。

7. **每个会话一把 `asyncio.Lock`**
   同一会话的消息串行处理，避免并发回复、上下文互相覆盖；不同会话之间仍然并行。

8. **适配器独立监督、指数退避重启**
   单个适配器异常只影响自己，最多 30 秒退避后重连；主循环任何环节的异常都只记日志。SnowLuma 适配器自身也带 WS 重连。

9. **归纳的「不许新词」按字符/单词严格核对**
   中文逐字、英文/数字逐词，只放行一个小型连接词白名单。数字也必须在原文出现，防止模型编造年份。合并会删除源记忆，因此 `source = user` 的记忆同样禁止参与合并——比规格更保守，但避免用户手动写入的事实被间接抹掉。

10. **归纳预算按「净减少条数」计算**
    `drop` 记 N 条，`merge` 记 N-1 条；单轮上限 `max(4, int(total * 0.2))`。超预算的提议直接跳过并记入 `rejected`，不拆半执行。

11. **人格文件是自由格式 Markdown**
   不做结构化解析，整段作为系统提示词；名字仅从标题/「你是」句式尽力猜测，猜不到用 `Ruben`。这样用户改人格不需要学任何 schema。

12. **配置单文件、未知键静默忽略、缺文件用默认值**
   方便版本升级时旧配置继续可用；密钥支持 `${ENV_VAR}`，避免明文入库。值为 `null`/`false` 的适配器视为禁用，`{}` 视为启用。

13. **LLM 只实现 `/chat/completions` 一条路径**
   OpenAI 兼容接口已成为事实标准。`json_mode` 走 `response_format`，但解析器同时兼容 ```json 代码块和前后夹带废话的输出，降低模型不听话时的失败率。重试只针对网络错误和 4xx/5xx，采用 0.5s 起的指数退避。

14. **插件加载失败只跳过该插件；插件默认全部关闭**
   插件是可选增强，不能阻塞启动；插件目录不存在也算正常情况。插件采用显式启用制：只有 `plugins.enabled` 里列出的才加载，留空即全部关闭，避免升级或误配置时悄悄启用有对外写操作的插件。`on_message` 返回 `False` 的语义定为「拦截」，与常见事件钩子习惯一致。

15. **ConsoleAdapter 是测试与本地调试的一等公民**
   它不依赖任何平台，支持 `reader` 注入和手动绑定回调，端到端测试正是靠它 + ScriptedLLM 跑通「发消息 → 落流水 → 抽取 → 引用记忆」。SnowLuma 的真实联调留到第二阶段。

16. **跨平台人物合并暂不做**
   `person_id = f"{platform}:{uid}"`，同一人在不同平台暂时是两个人。等真正出现多平台需求再设计合并策略，避免现在引入不必要的身份映射复杂度。

17. **WebUI 放到第二阶段**
   第一阶段先把核心链路和可观测性（日志、变更日志、插件列表接口）打牢；`PluginManager.list_plugins()` 等已为后续 WebUI 预留查询入口。

### 记忆 v2 读取侧的实现取舍

18. **隐私判断只有 `gate.py` 一个口**
    其他模块（读取侧、recall、privacy 插件）只负责「把候选凑齐、把问题问出去」，不做任何「这个人能不能看」的判断。审查顺序固定：同场合/公开放行（不调模型）→ 三档隐私 → 封存 → 已有裁定 → 高敏感扣下并排队 → 模型按准则权衡。**任何一步异常，剩余跨场合项全部扣下**（fail closed）；同场合项不受异常影响。

19. **「群 → 私」不审查，「私 → 群」「群 A → 群 B」才审查**
    不对称原则：跟本人私聊时回看他自己在群里说过的话是自然的；反过来把私聊内容带到群里才是风险。历史数据没有 `audience` 时按同场合处理，保证旧记忆仍可用。

20. **自查不走审查口**
    「你记得我什么」是本人查看本人画像，不属于跨场合披露；若也走 gate，隐私模式/高敏感默认扣下会让本人看不到自己的东西，和规格相反。群聊里不自查（避免档案被围观），`/隐私` 在群里也只报档位、画像只在私聊摊开。

21. **recall 工具的退化是常态**
    `chat_with_tools` 只做一次往返，循环在 core（最多 `recall_tool_max_rounds` 轮，默认 2）。中转返回「不支持 tools」时记 `ToolUnsupported` 并记住不再尝试；网络错误、模型不用工具、工具结果异常都只是退化为原来的单次生成，不影响回复。工具结果与普通召回走同一个 gate。

22. **注入预算的裁剪顺序是硬约定**
    `inject_budget_tokens` 超了只砍「事实 → 时间线 → 跨会话 → 自我 → 索引 → 被提及者画像」，说话人核心画像不参与裁剪。宁可少给背景，也不让主对话对象的关键信息被挤掉。

23. **时间一律模糊化**
    注入的时间线、跨会话摘要、自我时间线都用 `format_relative_time` 压成「今天/昨天/前天/上周/上个月」，不出现精确时间戳和会话 id——人记得的本来就是「大概什么时候」。

24. **「清空」是真删，日志只留计数**
    `/隐私 清空` 删除画像、事件、事实、裁定、待询问；`memory_log` 只记 `purge` 动作与各类条数（正文不留），并直接标记 `reverted=1`、禁止 `restore`，否则「清空」就不算真删。

25. **两段式生成只管「决定要回之后怎么写」**（借鉴 MaiBot 研读 #4 的机制）
    开关 `generation.two_stage.enabled` 默认关；关着时完全走原来的单段直出。开着时第一段用 `fast` 角色（没配则经回退链落到 chat），喂与正文相同的上下文（插件改写后的那份）+ 一段「只写要点」的任务说明，指导插到历史之后、生成指令之前并标注为内部材料。第一段异常、超时（不超过 `llm.timeout` 也不超过 15 秒）、空内容、只回了否决标记，都会退回单段直出；指导超过 200 字按截断处理，防止模型把它写成草稿让第二段照抄。两段用量分别记 `gen_stage1` / `gen_stage2`，单段时仍是 `chat`。意愿引擎、否决标记、触发与合并逻辑一律不动。

---

## 路线图

- **第一阶段（已完成）**：记忆层、主循环、Console/SnowLuma 适配器、插件系统、配置、测试与文档。
- **第二阶段**：FastAPI + 单页 WebUI（概览 / 人设 / 记忆 / 人物 / 群组 / 插件 / 日志 / 设置），真实 SnowLuma 联调，embedding 检索融合。

WebUI 的「人设」页可以直接编辑人格文件：保存时先写 `.bak` 备份，运行中的 bot 会立即热加载新人格，后续消息无需重启即可生效；「恢复默认」同样热加载。页面右侧只读展示当前实际生效的人格与系统提示词拼装预览，方便核对。

## 许可

MIT。
