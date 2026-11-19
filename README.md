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
                            │  3. 触发判断（私聊/被 @/概率）  │
                            │  4. 拼上下文（人格+记忆+历史）  │
                            │  5. LLM 生成 → 拆条发送        │
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

主循环之外还有一个后台任务（默认每 5 分钟检查一次），负责两件事：

- **抽取**：某个会话待处理消息攒够 `extract_every_n` 条，或会话静默超过 `extract_silence_seconds` 时，按发送者分别让模型从流水里提炼「关于这个人的耐久事实」。
- **归纳**：某人的新增记忆达到阈值，或距上次归纳超过间隔时，让模型提议 `merge` / `drop`，再由代码逐条校验执行。

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

### 模块一览

| 路径 | 职责 |
| --- | --- |
| `rubenbot/config.py` | 读取 YAML 配置，展开 `${ENV_VAR}`，未知键忽略，缺文件走默认值 |
| `rubenbot/persona.py` | 加载 SOUL.md 风格的 Markdown 人格；不存在时生成默认人格 |
| `rubenbot/llm.py` | OpenAI 兼容客户端；`chat` / `extract` / `consolidate` 可分模型；带退避重试 |
| `rubenbot/core.py` | 消息主循环、触发判断、上下文拼装、拆条发送、后台维护 |
| `rubenbot/memory/store.py` | SQLite 读写唯一入口：人物、记忆、流水、变更日志、撤销 |
| `rubenbot/memory/retrieve.py` | 分词、Jaccard、IDF 加权检索 |
| `rubenbot/memory/extract.py` | 批量抽取 + 去重 + 标记已处理 |
| `rubenbot/memory/consolidate.py` | 归纳提议的解析、校验与执行 |
| `rubenbot/adapters/base.py` | `BaseAdapter`、`MessageEvent`、`Segment` |
| `rubenbot/adapters/console.py` | stdin/stdout 调试适配器 |
| `rubenbot/adapters/snowluma.py` | OneBot v11 正向 WS 收事件 + HTTP API 发送 |
| `rubenbot/plugins/loader.py` | 插件发现、钩子与命令分发 |
| `rubenbot/__main__.py` | 命令行入口 `rubenbot` / `python -m rubenbot` |
| `plugins/hello/` | 示例插件：`/hello` 命令 + `on_message` 钩子 |
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
| `/hello` | 触发示例插件命令 |

群聊里普通消息默认只有 `group_talkativeness`（0.08）的概率被接话；想调试时可以在配置里临时调高，或直接发 `/hello`。

### 4. 测试

```bash
pytest
```

当前共 96 个测试，覆盖存储 CRUD / 变更日志与撤销、检索打分、抽取去重、归纳校验（不许新词、20% 上限、user 来源不可删）、主循环、插件、SnowLuma 事件解析，以及 Console + 假 LLM 的端到端流程。

---

## 配置速查

| 配置段 | 关键项 | 说明 |
| --- | --- | --- |
| `llm` | `base_url` `api_key` `chat_model` | 任意 OpenAI 兼容接口 |
| | `extract_model` `consolidate_model` | 留空则跟随 `chat_model` |
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
| `chat` | `group_talkativeness` | 群聊普通消息插话概率 |
| | `group_talkativeness_boost` | 刚聊过时的插话概率 |
| | `history_size` | 上下文带入的最近消息条数 |
| | `split_max_parts` `split_part_chars` `split_delay_seconds` | 长回复拆条策略 |
| `plugins` | `dir` `enabled` `command_prefix` | `enabled` 为空表示全部启用 |
| `background_check_minutes` | — | 抽取/归纳检查间隔 |

---

## 插件开发

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

钩子和命令都同时支持同步/异步函数，异常会被捕获并记录，不会拖垮主循环。`ctx` 上还能拿到 `store`、`config`，以及 `runtime` 中的 `persona`、`adapters`、`retriever`、`extractor`、`consolidator`。

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
   这样下一轮上下文能看到自己的话，对话连贯；同时它不会被当成「人物」，也不参与记忆抽取。

6. **群聊插话是概率行为，并带「刚聊过」加成**
   私聊必回、被 @ / 被回复必回；其余消息按 `group_talkativeness` 随机决定。最近刚回复过的会话在 `boost_window_seconds` 内提高概率，模拟「话题还没结束」。随机源可注入，测试里用固定种子。

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

14. **插件加载失败只跳过该插件**
   插件是可选增强，不能阻塞启动；插件目录不存在也算正常情况。`on_message` 返回 `False` 的语义定为「拦截」，与常见事件钩子习惯一致。

15. **ConsoleAdapter 是测试与本地调试的一等公民**
   它不依赖任何平台，支持 `reader` 注入和手动绑定回调，端到端测试正是靠它 + ScriptedLLM 跑通「发消息 → 落流水 → 抽取 → 引用记忆」。SnowLuma 的真实联调留到第二阶段。

16. **跨平台人物合并暂不做**
   `person_id = f"{platform}:{uid}"`，同一人在不同平台暂时是两个人。等真正出现多平台需求再设计合并策略，避免现在引入不必要的身份映射复杂度。

17. **WebUI 放到第二阶段**
   第一阶段先把核心链路和可观测性（日志、变更日志、插件列表接口）打牢；`PluginManager.list_plugins()` 等已为后续 WebUI 预留查询入口。

---

## 路线图

- **第一阶段（已完成）**：记忆层、主循环、Console/SnowLuma 适配器、插件系统、配置、测试与文档。
- **第二阶段**：FastAPI + 单页 WebUI（概览 / 记忆 / 人物 / 群组 / 插件 / 日志 / 设置），真实 SnowLuma 联调，embedding 检索融合。

## 许可

MIT。
