# RubenBot 规格说明（协调者：Ruben / 决策者：多萝）

## 项目定位

RubenBot 是一个以 Muse 式架构为核心的**拟人社交聊天 bot 框架**：

- 场景锁定：社交平台群聊/私聊，体感对标 MaiBot（像个住在群里的人，不是问答助手）
- 核心卖点：**记忆**。按人建档、跨会话跨群共享、定期归纳提炼，目标是媲美 Muse 的长期记忆体感
- 架构原则：尽可能轻量。Python 3.12，依赖越少越好；核心只用标准库 + httpx + websockets + pyyaml 级别的轻依赖
- 平台接入走**适配器模式**，先做 SnowLuma（OneBot v11）适配器
- 支持**插件**：目录式插件 + 钩子
- 带 WebUI（第二阶段，仿 FlClash 的界面语言）
- 许可证：MIT。记忆层的设计借鉴 nanoMuse 的思路（已研读其 store/consolidate 实现），**代码全部自己写，不复制 GPL 代码**

## 目录结构（目标）

```
rubenbot/
  pyproject.toml
  rubenbot/
    __init__.py
    config.py        # 配置加载（YAML）：模型、适配器、人格、记忆参数
    persona.py       # 人格文件加载（SOUL.md 风格的 Markdown）
    llm.py           # OpenAI 兼容的 LLM 客户端（chat / extract / consolidate 可分别配模型）
    core.py          # 消息处理主循环：触发判断 → 上下文拼装 → 生成 → 拆条发送
    memory/
      __init__.py
      store.py       # SQLite 存储：memories / memory_log / people / raw_messages
      retrieve.py    # IDF 稀有词加权检索（中文按字 bigram），可选 embedding 融合位
      extract.py     # 从消息流批量抽取事实
      consolidate.py # 定期归纳：模型提议 merge/drop，代码校验后执行
    adapters/
      __init__.py
      base.py        # BaseAdapter + 统一事件模型
      snowluma.py    # OneBot v11：WS 事件 + HTTP API
    plugins/
      __init__.py
      loader.py      # 插件发现与加载
  plugins/           # 用户插件目录（示例插件放这里）
  data/              # 运行时数据（sqlite、人格文件示例）
  tests/
```

## 记忆层设计（核心，必须按这个做）

### 数据模型

一条记忆 = 一句关于某个人的短事实。表结构要点：

- `memories`：id、person_id（关于谁）、scope_type（global / group）、scope_id（group 时为群 id，global 为空）、content、category、source（extracted / user / system）、source_ref（来源消息或会话标识）、created_at、updated_at
- `people`：person_id、platform、platform_uid、display_name、first_seen、last_seen。同一人在不同平台暂时视为不同 person（跨平台合并留作后续）
- `raw_messages`：消息流水（platform、chat_id、sender、text、ts），抽取的原料，保留最近一段即可
- `memory_log`：每次变更（merge / drop / forget / restore）记录 before / after 全文 + reason，可撤销

### 写入（extract）

- 不靠主对话自觉记。消息先落 `raw_messages`；每攒够 N 条（默认 30）或会话静默一段时间，对每个活跃发送者跑一次抽取：让模型从流水里提炼「关于这个人的耐久事实」，输出 JSON 列表
- 抽取规则写进 prompt：只要耐久事实（身份、偏好、习惯、关系、经历、承诺）；一次性请求、当天天气、纯情绪语气词不要
- 入库前查重：与该人已有记忆算 token 重合度（Jaccard），≥0.8 视为已有，跳过；精确重复直接跳过

### 检索（retrieve）

- 无 embedding 也能跑：token 化（字母词 + 中文字 bigram），按 IDF 加权打分 `log(N/df)`，稀有词权重高；整句原文命中加 bonus
- 回复时拼上下文：当前说话人的相关记忆（优先）+ 本群 scope 的相关记忆 + 最近记忆兜底，总量有上限（默认注入 ≤20 条）
- 预留 embedding 融合接口（有端点时启用），v1 可不实现

### 归纳（consolidate）

设计原则：**模型只许提议，代码负责裁决。**

- 按人分组跑（一个人一次），输入该人最近 ≤150 条记忆
- 模型只能输出两种操作（JSON 数组）：`merge`（多条合成一条，矛盾时留新事实并注明变化，如「住在北京，2026 年从上海搬来」）、`drop`（删掉不是耐久事实的）
- 代码校验，任何一条不过就跳过该提议：
  1. 合并出的新句子**不许出现原文没有的词**（逐词核对；中文逐字核对；允许少量连接词）——防编造
  2. `source = user` 的记忆不许 drop
  3. 一轮最多移除 20%（且至少允许 4 条），超出的留到下一轮
  4. 不许跨人合并（按人分组天然保证）
- 每次变更写 `memory_log`，提供 restore 撤销
- 触发：新增记忆攒够阈值（默认 50）或距上次归纳超过间隔（默认 24h），由主程序的后台任务检查

## 主循环（core）

- 统一事件进来后：落 raw_messages → 更新 people → 触发判断 → 拼上下文 → LLM 生成 → 拆条发送
- 触发判断（拟人关键，参考 MaiBot 的体感）：
  - 私聊：都回
  - 群聊：被 @ / 被回复到 bot 时必回；普通消息按可配置的活跃度概率决定是否插话（默认低，如 0.08）；连续对话上下文里适当提高
- 上下文拼装：人格（persona）+ 检索到的记忆 + 当前会话最近 K 条消息（默认 30）+ 群/说话人信息
- 发送：长回复拆成 2~3 条短消息（按句/长度），条间加短延迟，模拟真人节奏；可配置
- 稳健性：任何单个环节异常不能把主循环带死（MaiBot 的 planner 一次异常永久停摆是反面教材）；记录日志后继续

## 适配器

`BaseAdapter` 接口：

- `async start(on_event)`：启动并把统一事件交给回调
- `async send(chat_id, segments)`：发送消息（segments 支持纯文本 v1 先行，预留图片/表情结构）
- `async stop()`
- 统一事件 `MessageEvent`：platform、chat_type（private/group）、chat_id、sender_id、sender_name、text、reply_to_id、mentions_bot、raw

SnowLuma 适配器（OneBot v11）：

- 正向 WebSocket 连 SnowLuma 的 WS 服务收事件（地址、access_token 可配置）
- HTTP API 发消息（send_group_msg / send_private_msg）
- 心跳与断线重连

另做一个 `ConsoleAdapter`（stdin/stdout）用于本地调试和测试，不依赖任何真实平台。

## 插件系统

- `plugins/<name>/plugin.py`，暴露 `register(ctx)`；ctx 提供钩子注册：`on_message`（事件预处理）、`on_before_reply`、`on_after_reply`、命令注册 `command(name, handler)`
- 插件可启用/禁用（配置里控制）；插件异常不能拖垮主循环
- 附一个示例插件：`plugins/hello`（命令 /hello 回复 + 演示 on_message 钩子）

## 配置

单个 YAML（`config.yaml`，仓库给 `config.example.yaml`）：

- llm：base_url、api_key、chat_model、extract_model、consolidate_model（后两者缺省跟随 chat）
- persona：人格文件路径（默认 `data/SOUL.md`，给一个默认 Ruben 人格示例）
- memory：extract_every_n、inject_limit、consolidate_new_threshold、consolidate_interval_hours
- chat：history_size、split_max_parts、拆条与历史上下文参数
- decision：意愿系统参数（合并窗口、基础值/阈值、动量半衰期、发言占比抑制、冷却、否决标记、每群覆盖）
- adapters：启用的适配器及各自参数（snowluma 的 ws_url、http_url、access_token）

## 第一阶段交付标准（本次任务）

1. 上述目录与模块全部实现，`pip install -e .` 可装
2. 单元测试覆盖：记忆 store CRUD + 变更日志/撤销、检索打分、归纳的校验逻辑（重点测「不许新词」「20% 上限」「user 来源不可删」）、抽取去重
3. 用 ConsoleAdapter + 假 LLM（mock）跑通端到端：发消息 → 落流水 → 抽取 → 再发消息时能引用记忆
4. SnowLuma 适配器实现完整（真实联调在下一阶段，不阻塞）
5. README：架构图（文字版即可）+ 快速开始

## 回复决策（何时开口）——核心机制，多萝 2026-11-19 特别指出

与 Muse 的根本区别：Muse 是一对一助手，每条必回；群聊 bot 绝不能每条都回，必须自己判断该不该开口。core 里现有的平坦概率只是占位，按下面实现**意愿系统**（新模块 `rubenbot/decision.py`）：

1. **消息合并窗口**：同一发送者短时间（默认 4 秒）连发的多条先攒着、合并成一轮再决策，避免逐条回复碎片消息。私聊、群聊都适用；硬触发消息走短窗口或立即处理
2. **硬触发（直接进入生成）**：私聊；群聊里 @bot、回复 bot 的消息、消息里叫了 bot 的名字/别名
3. **意愿评分（群聊普通消息）**：每群基础值（可配，默认低）+ 动态修正——
   - 对话动量：bot 刚参与过这个会话、对方还在继续说 → 加分，随时间衰减（半衰期可配）
   - 话题相关性：记忆检索命中分数高（这话题 bot 有积累）→ 加分
   - 发言占比抑制：bot 在本群最近 N 条消息中占比超阈值（默认 20%）→ 大幅减分直至静默
   - 发言后冷却：刚发过言的一小段时间（默认 30 秒）内普通消息意愿压低；硬触发不受冷却影响
   - 评分过阈值才进入生成，阈值附近带随机性，避免机械感
4. **生成后否决**：生成环节允许模型输出「不回复」标记，core 解析后静默丢弃——评分过了、但结合上下文没必要接话时，它可以自己闭嘴
5. **可观测**：每次决策记录日志（回/不回、分数、主要因子），供调参和 WebUI 展示
6. **每群可配**：活跃度倍率、静默时段

测试要求：合并窗口、动量衰减、占比抑制、冷却、硬触发直通、生成否决，各有单测；用假时钟/可控随机源保证确定性。

## 第二阶段：WebUI（意愿引擎完成后做）

- 技术：FastAPI + uvicorn（作为可选 extra 依赖，不进核心依赖）；前端**不用构建链**，原生 HTML/CSS/JS 单页，静态文件由 FastAPI 直接托管
- 界面语言仿 FlClash：左侧导航栏、卡片式内容、圆角、明/暗两色、克制的配色；用已装的 frontend-design / design-taste-frontend / minimalist-ui 三个 skill 把关审美，不许做成默认模板脸
- 页面：
  - **概览**：运行状态、消息/回复计数、记忆总量、最近决策动态
  - **记忆**：按人浏览、全文搜索、手动新增/编辑/删除、变更日志查看与撤销（对接 memory_log）
  - **人物**：人物列表与档案（平台、首次/最近出现、记忆条数）
  - **群组与意愿**：每群活跃度倍率与静默时段配置、最近决策日志（分数与因子）查看
  - **插件**：列表、启用/禁用
  - **日志**：运行日志尾部查看
  - **设置**：config.yaml 查看与编辑（保存后提示需重启或热加载，v1 先做提示重启）
- API：REST + JSON，前缀 `/api`；WebUI 与 bot 同进程运行（bot 启动时按配置决定是否拉起 UI 服务，默认开，端口可配，默认 8765）
- 安全底线：默认只绑 127.0.0.1；绑非本地地址时必须设访问口令（配置项），无口令拒绝启动外网绑定

## 第三阶段：真实 SnowLuma 联调
