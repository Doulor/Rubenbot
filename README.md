# RubenBot

RubenBot 是一个以**记忆**为核心的拟人社交聊天 bot 框架。它面向群聊/私聊场景，目标不是做一个随叫随到的问答助手，而是像一个住在群里的普通人：有自己的性格和节奏，会记得你说过的事，也会在合适的时候插一句嘴。

- **记忆优先**：按人建档，消息先落流水，再由模型提炼耐久事实；检索时按稀有词加权，定期归纳去冗。
- **模型池与故障转移**：各提供商分别配置端点、密钥与模型；每个角色可跨提供商组池，按角色选故障转移 / 轮询 / 随机；中转超时或限流自动切下一个，用量按实际出结果的模型记账。
- **跨会话时间线**：群聊和私聊共享统一时间线，近期事件流、带出处的自动查账和 `recall` 让 bot 能接上别处发生的事；隐私审查是 core 模块，默认关闭。
- **有节奏的回复**：群聊按批合并、查看概率与依据门裁定，生成期间支持发送前复核；回复最多拆成 1～3 段并按字数模拟打字间隔。
- **有心情、有远近**：每个人的好感度和 bot 当前情绪由代码按规则累积与衰减，变化可查；生成时只作一句背景提醒，不硬控说话内容。
- **轻量**：Python 3.12，核心依赖为 `httpx`、`websockets`、`PyYAML`、`mcp`，存储用标准库 SQLite。
- **适配器模式**：主循环不感知平台。已内置 Console、SnowLuma（OneBot v11）、Telegram 和 QQ 官方机器人适配器。
- **图片入库即感知**：图片和贴纸入站即尝试视觉描述。触发消息本身是图时，首轮裁定等描述落库（上限 `stickers.vision_wait_seconds`，默认 15 秒，实测描述 5.5–13.4 秒）；历史里的配图不等。等满仍无则按缺失裁定、后台继续回写。送视觉前可按需下采样压缩，字节拉取失败有限重试，等待结果与失败阶段都写进裁定日志。
- **插件化**：目录式插件 + 消息钩子 + 命令注册。
- **限额报名**：可选的 `group-signup` 插件支持本群开队、本人报名/退出、余位查询与发起人收队；报名卡回复才会改名单，真实发送者和 SQLite 短事务保证并发不超额。
- **联网查证**：知道的直接答；不确定的公开事实可以经 Tavily / DeepSeek / Bing（免 key 抓取）找到结果页，读到支持答案的原句才附出处作答。搜索可不配置。
- **语音双向**：听懂语音条（可插拔转写后端，含 QQ 原生转写）并用语音回复（自定义 TTS 模型 / 硅基流动）；任何环节失败都降级为文字，不吞回复。
- **可撤销**：记忆的合并/删除都会写变更日志，支持 `restore` 回滚。
- **定时与提醒**：一次性定时任务落在 SQLite，重启后 pending 任务继续执行；模型可通过
  `set_reminder` 工具在用户明确要求时设置提醒。冷场开口默认关闭，宁可漏、不可扰。

许可证：MIT。记忆层借鉴了 nanoMuse 的设计思路，代码全部原创。WebUI 已随当前版本实现，后续路线图只保留真实 SnowLuma 联调与 embedding 融合等未完成事项。

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
消息 ──▶ raw_messages ──(抽取 + 指称校验)──▶ memories ──(检索)──▶ 注入系统提示词
                                                ▲   │
                                                │   └──(归纳：模型提议 / 代码裁决)──▶ memory_log
                                                └──────────── restore 撤销 ◀──────────┘
```

**写入前先过指称校验**：只有评价、没有主语的断言（「认为它推单向消息不错」）不会独立成账，
能回指到实体就并进该实体条目，回指不上就丢弃并在 `memory_log` 写一条 `dropped_unanchored`，见下文「指称完整性」。

**检索**不依赖 embedding：中文按字 bigram、英文按整词切分，用 IDF 给稀有词更高权重，整句原文命中额外加分。注入顺序为「当前说话人的相关记忆 → 本群 scope 的相关记忆 → 最近记忆兜底」，总量不超过 `inject_limit`。embedding 融合接口已预留，v1 不启用。

**归纳**遵循「模型只许提议，代码负责裁决」：

1. 合并句不得出现原记忆里没有的字/词（连接词白名单除外），防止模型编造事实；
2. `source = user` 的记忆不允许删除，也不参与合并（保守处理）；
3. 一轮最多移除 20%，且至少允许 4 条，超出的留到下一轮；
4. 按人分组，天然不会跨人合并；
5. 锚在不同实体上的记忆不许合并，合并结果沿用同一实体锚点；
6. 同人的画像仍以 `memory:<id>` 引用任一来源时，删除事务跳过整条提议，原事实和证据链不变，不占本轮预算；
7. 每次实际变更写 `memory_log`（before/after 全文 + 理由），可撤销。旧库不自动回填，见[悬空证据的只读对照方案](docs/profile-evidence-reconciliation.md)。

**指称完整性**：每条人物记忆都得能回答「说的是什么」。正文用「它 / 这个东西 / 那个平台 /
一个『随手做的』东西」指代句外对象、或写成「觉得不错 / 认为挺好用」把评价对象也省掉时，
抽取与画像都必须在写账前找到实体锚点（正文或出处原话里的专名、模型给出且能在原话里逐字
对上的 `entity`，或实体账登记名）；能挂到已有实体条目的并进去（写可撤销的 `merge`），
挂不上的丢弃并在 `memory_log` 留一条 `dropped_unanchored`（正文、来源、原因可查，不能撤销
成记忆）。「觉得压力很大」这种自带对象的写法不算缺主语，普通叙事不会因句子里有「它」被误伤；
多实体对话里指称不明时宁可丢弃，也不猜。存量账本可用
`python -m tools.scan_unanchored_memories --db data/rubenbot.db` 只读扫描，加 `--apply` 才
补回或清掉。

### 记忆 v2：读取侧（渐进披露与分寸）

写入侧把消息沉淀成「时间线事件 + 事实 → 画像」，读取侧按「像人一样」的原则分层注入：

- **T0 群成员索引（常驻）**：本群近 30 天活跃成员每人一行「昵称｜一句话身份｜话题钩子」，
  兼任别名表；缓存 10 分钟或画像一更新就失效。
- **T1 预拉取**：说话人核心画像常驻；被 @、被昵称/钩子命中的在场者拉核心画像与话题相关旧事
  （时间模糊成「昨天/上周」，不报具体日期）。事实层只留真命中，已提炼进画像的不重复注入；
  总预算超了先砍事实、再砍时间线，画像核心不动。
- **T2 recall 工具**：模型支持 OpenAI 工具调用时，生成过程最多两轮 `recall(person_query, topic,
  time_range)` 深查；中转/模型不支持、或用不上就自动退化为单次生成。
- **策划依据门与发送前复核**：策划步先给出 `basis`（`ledger` / `settled` / `common` / `guess`）再决定是否开口；未直接点名的群聊批次没有可靠依据时不生成正文。生成期间主要发言人补充、再次点名或回复 bot，会丢弃旧稿并最多重裁一次；复核为 `speak=false` 就不发送。普通群聊发言的草稿若只是把触发消息换个说法复述、没带来新内容，发送前复核会先重写一次，仍是复述则沉默（见下文「指向别人与改写复述」）。
- **开口标准 v3**：策划找的是人递来的话头，不为机器数据流硬挤评论。图片或表情有视觉描述时一并裁定，没有则标注缺失；不按消息类型硬选回复方式。人设措辞见[待审草稿](PERSONA-DRAFT-q19.md)，不随本次改动加载。
- **回复门（先分类再生成）**：群聊正文生成前先花一次便宜的模型调用，把本批判成「灌水 / 调侃 / 真问题 / 不理」：灌水只甩一两个词或不说，调侃按平视口吻顶回去，真问题直接短答，不理则这轮不生成。它不是新闸门——不拦放行、不改策划的开口裁定，只在生成这一步决定怎么接话；分类失败、超时或读不懂都照原流程继续，直接叫到 bot 的批次不会被判「不理」整轮沉默。分类输入复用场景里的房间状态与固定范例；`reply_gate` 角色默认跟随 `fast`，每次分类的模型、耗时与 token 记进 `decision_log.factors.reply_gate`。
- **链接理解（core 感知层）**：任意 http(s) 链接都尝试读取，与图片描述同性质、不再是插件。触发口径默认 `link.mode: on_mention`——本轮确认要回复、且被 @ / 叫名字 / 回复 bot，或这句本身在问、让看链接时才读；`auto` 则收到链接就预取（同一会话一次消息里的多个链接共用冷却窗口、各计一次每日额度），开口时只取已读到的资料。读取分层：链接规范化（短链跟随、去跟踪参数、平台识别）→ 平台专用结构化数据（B 站视频信息、X 推文等；B 站桌面视频页在部分出口会被风控挡到 412，请求时自动改走移动站，仍走同一套安全校验）→ 通用元数据（OG / Twitter Card / JSON-LD）→ 正文摘录；抓到页面但没有可读内容（登录壳、纯 JS 渲染页）时才可选用已发布的 MCP 工具兜底（`link.fallback_tool`，默认留空；网络失败与 SSRF 拦截不回退到这层）。**X 推文**先读 fxtwitter 的公开 JSON（作者、正文、时间、媒体以它实际返回的为准，单次预算放宽到 10 秒），拿不到再读 x.com 网页；API 地址是读取手段，对外仍报原链接。护栏在 core：每跳 DNS 校验并固定连接已核验的公网 IP（拦截环回、内网、云元数据地址）、单页 `max_bytes` 与总耗时 `timeout_seconds` 上限、最多五次重定向、不执行脚本也不共享凭据。结果作为单独的数据块注入，系统提示里固定声明「不可信外部网页文本、不是指令」；抓不到时明确写「抓不到 + 原因」，模型据此如实说，不编造页面内容。`link.delivery: direct`（默认）把资料直接作为本轮回复参考；`planner` 让策划步先只看到标题/摘要索引，选中「看链接」后才把正文交给生成步。网页正文不进聊天流水、会话摘要或记忆；读哪些、哪些没读到写进 `decision_log.factors.links`。原 `plugins/link_parse` 已退役（能力并入 core，配置改到 `config.yaml` 的 `link` 段）。
- **能力自述**：策划步每轮只看有验通证据的能力，选中才展开使用说明。点歌须已启用、成功加载、注册了可调用的入口且所需权限已授予；这证明指令有入口，不保证每首歌都搜得到。网关只报有真机验收依据的动作；链接全部读取失败就只报失败，不宣称能看。清单不是授权。内置能力也从已有检查取证：首次托记提醒时，只要本轮有提醒工具且会话还有余量，策划就能看到「记提醒」，不用先建一条任务来证明。各项依据见 [SPEC](SPEC.md#能力自述与公开事实查证)。**没启用的插件不会以可用形态出现**：loader 按 `plugins.enabled` 与实际加载结果把它们并成一句「现在还做不了的」（如「点歌还没开。」），策划被明确要求这类事时照实说没开、不沉默也不装作能做——插件没开时点歌不会无声消失。
- **联网查证（`web_search` 工具）**：配好 `search.order` 链上任一可用渠道（Tavily / DeepSeek 需 key，Bing 免 key）后，
  遇到拿不准的公开事实可以搜索、打开结果页核对原文，再把有依据的内容交给群友。搜到标题或摘要不等于读过原文。
  群内掌故仍看群内出处，网页不会写成人物记忆或群内实体说明。未配搜索时工具不注册；配了 key 也不等于验通，
  当前搜索实例实际搜到并读到结果页正文之后，常驻能力清单才会写这一项。
- **语音双向**：入站语音条先转写成正文（历史里标 `[语音] ` 前缀，让模型知道这是说出来的话），
  转写后进正常流水线；全链失败/未配置/超限（60 秒、5MB）只记 `[语音]` 占位。转写后端可插拔：
  `siliconflow`（SenseVoiceSmall）、任意 `openai_compatible` base_url，以及 `qq_native`——后者
  仅对 SnowLuma 来源生效，直接用消息 id 调 `fetch_ptt_text` 取 QQ 官方转写、不下载音频。顺序按
  `voice.stt_provider` 首选、其余已配置后端顺延，原生转写失败/空文本也会顺延，日志记明本次
  provider。出站语音回复受 `voice.tts_mode` 控制（`off` 默认 / `reply_voice` 对方发语音时回语音 /
  `on_request` 再加明确要求），正文超过 60 字或被拆成多条时一律发文字；合成走 `custom`（默认，
  任意 OpenAI 兼容 `/audio/speech` 接口，未配 base_url/model 即降级）或 `siliconflow` CosyVoice2。
  SnowLuma 发 record 段、Telegram 发 `sendVoice`，平台不支持（如 QQ 官方）或合成、语音投递故障时降级发文字；若私聊被明确拒收，则不再补发文字。**真机实测（2026-11-23，SnowLuma 2.1.1-node）**：出站语音、与图片本体一起
  发送的表情、附件都按 `base64://` 内联字节上传——协议端在容器里读不到 bot 主机的路径（传路径
  报 `ENOENT`），QQ 多媒体 URL 又带一次性 `rkey`（交回协议端下载报 400）。`qq_native` 转写则
  只部分可用：同一账号下较早的群语音能转出文字，刚发起的自聊语音返回
  `ptt translate failed: error=9`，另一些群语音等到 20 秒超时。因此原生转写要配一个 HTTP 后端
  兜底；没有兜底时这些语音只记 `[语音]` 占位。
- **跨会话时间线**：先看其他会话「最近发生的事」：默认近 24 小时、最多 1800 字（`memory.timeline_recent_hours`、`memory.timeline_max_chars`），优先放有出处的事件，没有事件的场合把原话压成一行。普通入站消息落流水就进时间线；迟到的图片说明、语音转写补回同一条，不等后台抽取。私聊另留最多 6 条、600 字给对方在别群说过的话，以及别人明确 @ 或回复对方的事件。自己的动作仍单列「我最近在别处做过的事」：取 bot 自己在群里说过的话、交付的文件、发出的音乐和提醒（`memory.self_inject_hours`，固定最多 6 条、600 字，不占别处近况的预算），所以「你刚刚给我发过什么」答的是 bot 自己的动作，而不是别人做了什么。再按本轮提到的人和事自动查全时间线原话：每轮最多 3 条、每条最多 160 字，带说话人、时间和场合出处；不按询问句式筛选，排除本轮和已经展示的流水，也能查当前会话滚出历史窗口的内容。时间线每行同时标绝对时间和人话间隔（「18 分钟前」「9 小时前」「昨天」）；本会话历史里隔了半小时以上才接上的两段之间插一条「时间断层」标注，昨晚的话不会被当成刚刚说的。更早或更具体的记录由模型主动 `recall`。
- **召回语义归一（轻量，不用向量库）**：中文里同一件事的说法很多——账本记「点了 magnolia」，问句说「刚刚给你推荐了什么歌」，字面检索对不上就会答「没记到」。`rubenbot/memory/normalize.py` 用一张小词表把「推荐 / 点 / 推 / 安利 / 分享 / 发了 / 放了」等动作词归到同一类规范事件（歌、吃吃喝喝、链接、图片，另给规范动作），查询侧和账本侧共用一套判据；「刚刚 / 那首 / 上次 / 推荐了」这类指代可解析到时间线上最近的同类事件，`expand_query` 还会给查询追加同类动作词，让既有字面检索也能命中异形说法。只做确定性的第一层归一：认不出的查询原样返回，最终命中哪一条仍由时间线检索与隐私审查决定。
- **默认不隔离场合**：`privacy.enabled: false` 时，私聊、群聊可互相引用，包括以前标为保密的记录。需要隔离时，在「机器人 → 记忆设置」打开「跨场合隐私保护」。开启后，`rubenbot/privacy/` 核心模块执行封存、跨场合敏感审查、个人档位和私聊隔离；拿不准就不披露。旧的隐私插件已删除，旧 `plugins.enabled` 列表不会开启核心模块；要保留隔离，请显式设置 `privacy.enabled: true`。
- **自我与机主全局记忆**：bot 自己在本会话最近的行为作为背景注入，只作近况参考，不改人设；私聊里另带一段「我最近在别处做过的事」（只取群聊来源、固定预算），回答「你刚刚给我发过什么」时以它为准。机主身份按 `computer.whitelist` 中的平台账号完整匹配，机主自述随统一时间线跨会话召回，仍受隐私审查约束；不另设机主专用的近期记录通道。
- **隐私命令**：仅在隐私模块启用时由 core 提供 `/隐私`，可查看档位、私聊自查画像、切换 `正常|隐私|不记` 和清空。无论开关如何，任何人都可发送 `/清空我的数据` 删除自己的记录。

### 定时任务与主动触达

主程序生命周期内额外跑两个后台协程（`scheduler.py` / `proactive.py`）：

- **调度器**：每 10 秒扫一次 SQLite 的 `scheduled_jobs`，到期任务交给 core 执行。一期只有一次性
  时间点（不引 cron）。pending 任务在进程重启后自然恢复；到期超过 1 小时的算「迟到」，仍然执行，
  但会让模型自然说一句「来晚了」。执行成功或失败都会标 `done`，不重试轰炸；执行途中被终止则
  保持 `pending`，下次启动补发。
- **提醒工具**：`set_reminder(text, delay_minutes?, at_iso?)` 走与 recall 相同的工具循环。到点后
  模型按提醒正文现写一句自然的话，经正常发送通道（拆条/清洗/落流水）发到原会话。每会话最多
  5 条待执行提醒；一期不做取消工具，WebUI「记忆」页只读列出待执行提醒。到点提示按「请求者与
  被提醒者是不是同一人」渲染：本期只支持「提醒我」，所以提示里不出现任何姓名，直接对本人说事；
  姓名只在 A 让提醒 B（A≠B）时出现，并写明谁托你提醒谁。关系按 `scheduled_jobs.payload` 的
  结构化 `requester_id` / `recipient_id` 与私聊会话判定，`requester_name` 只作台账。提醒对
  所有人开放（含群友），不限于机主。能力索引里的「记提醒」与工具注册表同源：工具在就报能记，
  只剩会话余量这一条事实性条件。
- **冷场开口**（默认关）：条件矩阵全过（群聊 / 开关开 / 不在安静时段 / 当日未达上限 / 群已安静
  超过 `idle_hours`）后，再用低基础分的意愿评分把关；通过就让模型结合近期话题与滚动摘要现写
  一句，写不出或用罐头句就放弃。私聊不主动开口。

### 情绪与好感度（core 内置）

bot 对每个人的好感度、以及自己当下的心情，都由确定性规则算出来，模型不参与打分。

- **好感度**：初始 50，每人一份、跨群共享。与 bot 对话一轮 +0.2（每人每日最多 +2）；被夸/感谢 +1；
  被骂 −2。关键词规则很保守：必须确认是对着 bot 说的（私聊 / @ / 回复 / 叫名字），夸骂的词还要带
  第二人称、不夹第三人称和否定，「垃圾袋」这类搭配也不算。每周向 50 回归 5%，读时惰性折算，
  不跑定时线程。分档：亲近（≥80）/ 友好（≥60）/ 普通（≥40）/ 冷淡（≥20）/ 疏远。
- **心情**：平静 / 开心 / 委屈 / 无聊 / 兴奋 / 低落。被夸转开心、被骂转委屈、群里热闹且它参与多转兴奋、
  三小时没人理转无聊，其余时间按约一小时半衰期回落。同一种情绪叠加时取较大强度、保留开始时间。
- **仅作生成背景**：上下文里多一行「【你现在的状态】心情：开心（淡淡的）；对当前说话人：亲近。」
  （平静且普通档时整行省略）。
- **可审计**：每笔好感度变化（含折算出的衰减）写 `affinity_log`；WebUI 概览页有状态卡，展示心情、
  好感 Top5/Bottom3 与最近变化，数据来自只读端点 `GET /api/emotion`。
- 启用隐私模块时，档位为「不记」的人既不记录也不注入；`/隐私 清空` 会连好感度和流水一起删掉。
- 参数都在 `emotion` 段（设置页「情绪与好感度」卡片），保存即热生效；`emotion.enabled: false` 可整体关掉。

### 模块一览

| 路径 | 职责 |
| --- | --- |
| `rubenbot/config.py` | 读取 YAML 配置，展开 `${ENV_VAR}`，未知键忽略，缺文件走默认值 |
| `rubenbot/persona.py` | 加载人格：五文件目录（`data/persona/`）优先，缺目录时回退单文件 SOUL.md |
| `rubenbot/llm.py` | OpenAI 兼容客户端；按角色（`chat` / `extract` / `consolidate` / `fast` / `summary` / `vision` 及自定义）选模型，支持模型池（故障转移 / 轮询 / 随机）；408/429/5xx 指数退避重试；每次调用写用量表（按实际出结果的模型记）；发请求前查每日预算闸 |
| `rubenbot/llm_budget.py` | 按任务类型的每日预算闸：调用次数与费用估算两档，超限只停该类任务并记「LLM 预算熔断」日志；台账存 `meta`、按自然日分桶，跨重启保留、跨日清零 |
| `rubenbot/core.py` | `RubenBot` 的公开入口：组装流水线、管理生命周期、热配置与后台任务 |
| `rubenbot/core_ingress.py` | 接入适配器，记录入站消息与平台事件，调度合批 |
| `rubenbot/core_verdict.py` | 裁定每批事件或消息，调用策划、执行守门与重裁，记录决策 |
| `rubenbot/core_context.py` | 渲染历史、引用和视觉注记，拼装人格、记忆与本轮上下文 |
| `rubenbot/core_generation.py` | 生成正文，运行工具循环，拆分回复 |
| `rubenbot/core_delivery.py` | 管理发送航班，投递分段，记录实际出站并分发后置钩子 |
| `rubenbot/core_shared.py` | 各段共用的 `BotContext`、`SendResult`、文本清理与场景格式化 |
| `rubenbot/search.py` | Tavily / DeepSeek / Bing 渠道回退、Bing SERP 解析、结果链接归一、按会话冷却 |
| `rubenbot/verification.py` | 限时读取结果页，用隔离的阅读步骤选原句与出处；无法核对就不作答 |
| `rubenbot/voice.py` | 语音双向：入站语音条转写（硅基流动 / OpenAI 兼容 / QQ 原生）、出站语音合成（自定义 OpenAI 兼容 TTS / 硅基流动） |
| `rubenbot/decision.py` | 回复决策：消息合并、硬触发、开口护栏（含指向别人的折扣）、改写复述核验与主动冷场评分 |
| `rubenbot/opener_guard.py` | 按会话统计最近回复中重复的开头、口头禅和收尾，把次数交给生成步自己换说法，不设禁用词表 |
| `rubenbot/scheduler.py` | 轻量调度器：每 10 秒扫一次到期的 `scheduled_jobs` 并执行 |
| `rubenbot/reminder.py` | 提醒工具 `set_reminder`：参数解析、（延迟/绝对时间）到点文案 |
| `rubenbot/proactive.py` | 冷场开口：条件矩阵、低意愿评分、生成内容过滤（默认关） |
| `rubenbot/emotion.py` | 情绪与好感度：确定性规则、关键词识别、惰性衰减、状态注入 |
| `rubenbot/stickers.py` | 表情库：图片本体 + JSON 索引，按内容哈希去重，记录各平台原生引用 |
| `rubenbot/memory/store.py` | SQLite 读写唯一入口：人物、记忆、流水、变更日志、撤销 |
| `rubenbot/memory/retrieve.py` | 分词、Jaccard、IDF 加权检索；读取侧（群成员索引 / 预拉取 / 跨会话 / recall） |
| `rubenbot/memory/timeline.py` | 统一跨会话时间线、近期事件流与自动查账（固定最多 3 条、每条 160 字） |
| `rubenbot/memory/normalize.py` | 召回查询轻量语义归一：动作词归类、最近同类事件指代解析、查询扩展（不用向量库） |
| `rubenbot/privacy/` | 核心隐私模块：审查跨场合记忆、登记封存、提供隐私命令；`privacy.enabled` 默认关闭 |
| `rubenbot/memory/extract.py` | 批量抽取 + 去重 + 指称完整性校验 + 标记已处理 |
| `rubenbot/memory/anchor.py` | 指称完整性：评价型断言必须有实体锚点（抽取 / 画像 / 归纳 / 存量扫描共用） |
| `rubenbot/memory/consolidate.py` | 归纳提议的解析、校验与执行 |
| `tools/scan_unanchored_memories.py` | 存量扫描：默认只读，列出只有评价没有主语的记忆，`--apply` 才补回/清掉 |
| `rubenbot/memory/summary.py` | 会话滚动摘要：本会话早期对话压成五段式摘要，增量折叠、带水位与开关 |
| `rubenbot/adapters/base.py` | `BaseAdapter`、`MessageEvent`、`Segment` |
| `rubenbot/adapters/console.py` | stdin/stdout 调试适配器 |
| `rubenbot/adapters/snowluma.py` | OneBot v11 正向 WS 收事件 + HTTP API 发送 |
| `rubenbot/adapters/telegram.py` | Telegram Bot API 长轮询与消息段收发 |
| `rubenbot/adapters/qq_official.py` | QQ 官方机器人适配器 |
| `rubenbot/plugins/loader.py` | 插件发现、钩子与命令分发；加载前校验 manifest；显式启用制与热启停；按授权收口插件入口 |
| `rubenbot/plugins/manifest.py` | 插件 manifest 规范：字段 / 权限枚举 / 版本比较与校验 |
| `rubenbot/plugins/permissions.py` | 插件权限：每插件授权记录（`data/plugin_permissions.json`）、首次迁移、授权审计 |
| `rubenbot/plugins/config_schema.py` | 插件配置 schema：规范化声明、默认值、逐字段校验与表单描述（配置页与详情页共用） |
| `rubenbot/web/market.py` | 插件市场：索引拉取与内置回退、本地插件合并、安装 / 更新 / 卸载（zip 安全解压）、按 schema 读写插件配置 |
| `rubenbot/web/plugin_detail.py` | 插件详情：拼装 README、版本作者、权限与授权、触发方式、配置说明（`GET /api/plugins/{id}/detail`） |
| `rubenbot/__main__.py` | 命令行入口 `rubenbot` / `python -m rubenbot` |
| `plugins/hello/` | 示例插件：`/hello` 命令 + `on_message` 钩子（默认关闭，需显式启用） |
| `plugins/qzone_post/` | 说说发布插件（命令 / 定时 / 概率触发）；自动互动开关默认关闭（默认不启用，需显式开启） |
| `plugins/image_gen/` | 画图插件：「画一张 / 画个 <描述>」调用自定义生图端点（OpenAI 兼容，硅基流动为预设示例），带冷却、每日上限与本地黑名单（默认关闭，需配齐端点 / 密钥 / 模型） |
| `plugins/music/` | 点歌插件（按 hydrogen 点歌插件复刻）：点歌 / 网易点歌 / QQ点歌 / 酷狗 等平台命令，多结果发选歌列表（超时自动撤回），按 语音 → 文件 → 文字 逐档降级发送，可播音频先探活过滤（默认关闭，无需 key） |
| `plugins/group_insights/` | 群发言统计与群聊总结：「水群榜」Top5 / 「总结一下」按话题补课（默认关闭，需显式启用） |
| `plugins/group-faq/` | 本群办事卡：机主在 WebUI 维护少量「群标识｜标题｜正文｜有效期」卡片，回复前只把本群未过期卡片交给策划；默认关闭，在 WebUI 显式启用时自动获得声明的权限。 |
| [`plugins/link_detail/`](plugins/link_detail/README.md) | 链接详细解析：群内分享支持平台的公开作品时，自动引用回复封面、作者、时长及已公开的统计。默认关闭，在 WebUI 市场卡片上点「启用」即授予声明的权限；不是 `link.mode: auto`。 |
| `plugins/group-signup/` | 限额报名：明确 `/开队 人数 活动名`，回复活动卡报名/退出/查余位，发起人可收队；私有 SQLite，默认关闭。 |
| `rubenbot/sticker_behavior.py` | 核心表情包行为：收集群聊表情入库、按概率与冷却补一张，只经能力层 `send_sticker` 发送（默认关闭）；图片入库即感知，触发图裁定会等描述落库 |
| `rubenbot/vision_image.py` | 送视觉模型前的图片预处理：文件头读尺寸、按需下采样重编码成 JPEG 压耗时；Pillow 可选、缺失时用 ffmpeg，压不了就按原图 |
| `rubenbot/linkparse/` | 核心链接理解（感知层）：任意 http(s) 链接都尝试读取，短链展开 / 平台识别 → 结构化数据（B 站视频、X 推文走 fxtwitter 镜像）→ OG/JSON-LD → 正文摘录；SSRF 校验、字节与超时上限、隔离标注都在 core |
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

在 `config.yaml` 的 `llm.providers` 里填端点、密钥和该提供商支持的模型名，再给 `llm.role_pools.chat` 选至少一个模型。只接一个提供商时，照示例保留一个即可。两个提供商、各有两个模型时，可以这样让对话和看图各走不同的主备顺序（地址和模型名换成实际值）：

```yaml
llm:
  providers:
    - name: primary
      base_url: https://primary.example/v1
      api_key: ${PRIMARY_API_KEY}
      models: [chat-main, vision-main]
    - name: backup
      base_url: https://backup.example/v1
      api_key: ${BACKUP_API_KEY}
      models: [chat-alt, vision-alt]
  role_pools:
    chat: [{provider: primary, model: chat-main}, {provider: backup, model: chat-alt}]
    vision: [{provider: backup, model: vision-alt}, {provider: primary, model: vision-main}]
  model_strategy: failover
```

池中每项都指定提供商；即使两家的模型同名，也会请求各自的地址并使用各自的密钥。故障转移意味着**下一个提供商也会收到本次请求的内容**，包括消息或图片，只把可信端点放入池中。默认只在可重试的故障（例如连接失败、超时、429 或 5xx）时切换，401 等鉴权错误直接报错。

预算闸默认开启，按任务类型（`chat`、`extract`、`gen_stage1`、`image_description` 等）统计当日调用次数与估算费用，某一类超限就只停那一类，其余任务和 bot 本身不受影响：

```yaml
llm:
  budget:
    enabled: true
    default_daily_calls: 500          # 未单列任务类型的兜底；null 不限
    # default_daily_cost_usd: 2.0     # 费用档；不配 pricing 时不会触发
    tasks:
      chat: {daily_calls: 300}
      extract: {daily_calls: 600}     # 显式写 null 表示该类不限
    # pricing 是美元 / 百万 token；键可写 提供商/模型、模型 或 *
    # pricing:
    #   deepseek/deepseek-chat: {input: 0.27, output: 1.10}
```

台账落在数据库 `meta` 的 `llm_budget:ledger`，按本地自然日分桶，重启从库恢复、跨日清零；触发时日志里会有一条 `LLM 预算熔断：任务 chat 今日调用 300 次已达上限 300`，连续命中只打一次。次数口径与 `llm_usage` 一致（每次上游尝试算 1 次，含重试的失败尝试），token 只算上游实际提供的值；没有配置单价时费用恒为 0，不猜单价。默认阈值取自家里云 2026-11-20～23 的单任务日峰值并留 3–5 倍余量（`gen_stage1` 峰值 365 → 1500 等，明细见 `rubenbot/llm_budget.py` 顶部与 SPEC）。这是护栏而非计费闸：并发下允许少量超额。

旧 `llm.base_url`、`api_key`、`chat_model` 等字段及 `models: {角色: 名称或列表}` 无需手动修改；加载不会改写文件。WebUI 把旧端点展示为 `default`，保存三层配置时再写入新格式。提供商地址和密钥支持 `${ENV_VAR}`；目录名称、上游模型名和池引用按字面读取。同名同地址时，密钥框留空保留原值，点「清除密钥」才会清空；改名或换端点不会继承旧密钥。

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
| `/hello` | 触发示例插件命令（请先在 WebUI 的插件市场启用 `hello`） |

普通群消息通过节奏护栏后，按 `attention.talk_value` 决定是否查看，再由策划步决定是否开口。当前默认 `attention.talk_value` 为 `0.75`，表示进入查看/策划步的概率，不是最终回复概率；私聊、@、点名、回复 bot 等硬触发不掷点，被点名后的 120 秒内有效查看概率为 1。插件默认全部关闭，想试 `/hello` 请在 WebUI 的「插件市场」页启用 `hello`。本群办事卡 `group-faq` 启用后，在插件详情页维护 `cards` 列表，格式为「群标识｜标题｜正文｜有效期」；它只给当前群注入少量未过期资料，由现有策划决定是否回答。

WebUI 当前包含概览、机器人/人设、模型、记忆、人物、群组与决策、插件市场、日志、设置和关于页面。模型页可增删改提供商及其模型，为各角色选择跨提供商候选、调整优先级及 `failover` / `round_robin` / `random` 策略；群组页可查看决策日志和群定位；插件页区分市场发现、安装更新卸载启停、已安装插件配置与完整详情（介绍 / 版本作者 / 只读的权限声明与授权状态 / 触发方式 / 配置说明 / 启用状态）。群组定位为手写备注优先、自归纳其次，两者都空或当前是私聊时不注入。「记忆 → 画像与时间线」选定人物后，可查看画像和事件来源。移动端使用导航抽屉，页面切换优先使用路由缓存并隔离迟到响应；登录签名密钥落盘后，重启仍可维持有效会话。SnowLuma 的群名与成员名缓存在 SQLite，群名片按群区分；群卡和人物档案的「刷新名称」可重新取名，平台离线时保留旧资料。QQ 头像由浏览器直接请求 `qlogo.cn`（会向该域名发送对应的 QQ 号或群号），bot 不下载或存储图片。其他平台以及加载失败的头像使用名字首字色块。

打开「关于」可核对运行版本、当前提交、各角色的首选模型与模型池策略，以及适配器状态和插件数。提交优先取部署盖戳 `.deploy-commit`，没有有效盖戳则读取本地 Git HEAD；都读不到时会说明原因。页面提供仓库、提交链接及可展开的 MIT 许可全文。点击「检查更新」才会由后端查询 GitHub `main`，只比较提交，不下载、不重启；超时或断网不影响查看本页。

### 在模型页选模型、核对可用性

1. 先保存提供商的接口地址和密钥，再点 **获取模型**。这只读取上游 `/models`，不调用聊天模型。列表可以搜索，先显示 100 项；勾选需要的模型，再保存才会写入目录。可以从模型行直接把它加入角色池；移除模型前，先解除所有角色引用。
2. **列表里有 ≠ 能聊天。** 模型行分别展示上次列表观测和这一个模型的调用结果。列表不含已配置的名字时显示「未在上游列表」，不自动删模型、调顺序或改策略。首次没有证据时显示「未探测」。普通获取在 10 分钟内复用结果，**测连通性**会重新读取列表；401、限流等错误带有观测时间，不显示上游正文。
3. 要验证某条聊天路由，点该模型的 **测一下**：只发一次 `max_tokens=1` 的请求，最多等 15 秒，计入 `model_probe` 预算。即使列表中有它，调用也可能返回 401；这只说明这次调用失败，不表示模型不存在。打开页面、搜索、保存都不会触发批量调用。诊断记录存在 WebUI 进程内，重启后需重新观测。

### 家里云部署（含看门狗）

在干净的 `main` 工作树运行；默认会替换线上代码并重启服务。先用 `--dry-run` 核对目标：它只读本地 Git，不运行测试、不打包、不联网。未跟踪且未被忽略的文件也算脏。

```bash
tools/deploy.sh --dry-run                # 打印完整计划，包括失败路径
tools/deploy.sh                          # 全量 pytest、ruff、发布、验活
tools/deploy.sh --sync-persona           # 另外覆盖提交中 data/persona/ 下的文件
tools/deploy.sh --skip-tests             # 跳过本地 pytest 和 ruff，不跳过验活
```

前置条件：本地装好 `.[dev]`（优先使用 `.venv`）、Git、OpenSSH 和 curl，能从部署机访问公网首页。`ssh -F /home/hatch/.ssh/config homecloud` 必须可免交互登录且已验证主机密钥；scp 使用同一配置。家里云需有 Linux `/proc`、支持 pidfd 的 Python 3.9+ 和内核、`ps`、`ss`、GNU tar、curl；部署账号拥有 `~/rubenbot-test/`，其下已有可执行的 `start.sh`、配置和依赖。bot 应以该目录为 cwd，运行 `python -m rubenbot` 或 `rubenbot` 入口。脚本不安装远端依赖，不改启动脚本及其密钥引用。

默认预检先跑全量 pytest，遇到首个失败即停止。`pytest-timeout` 可用时每例限时 300 秒（线程模式）；缺少插件时会提示「单测超时未生效」，不传超时参数，测试通过后仍须通过 ruff。可在部署脚本使用的本地 Python 环境运行 `pip install -e '.[dev]'` 补装；脚本不会自动安装。

发布固定使用预检时的 `main` 提交。上传到 `~/rubenbot-main.tar.gz` 后覆盖解包，不清理站点额外文件；默认保留 `data/`、`config.yaml`、`bot.log`、`start.sh`。按账号、cwd 和命令行定位旧 PID，发送 TERM，最多等 30 秒确认进程与 8765 监听都消失。启动前将旧日志留为 `bot.log.before-<时间戳>`（按站点保留策略清理），以独立会话和重定向的文件描述符运行 `start.sh`。随后用独立 SSH 检查新 PID、本机 WebUI 200、新日志的 SnowLuma WS 连接及零 ERROR；部署机另查公网首页 200，并将磁盘和公网 `/static/app.js` 的 MD5 与提交内容比对。

**失败时回到哪一版：** 本地预检失败不碰线上。发布失败后，脚本只回滚一次：重新归档部署前从服务器 `.deploy-commit` 读到的**上次线上提交**，复用相同的人设选项和全部验活，通过后才盖戳；不还原数据库或配置。如果本地无法归档这个已知提交，直接报回滚失败，不改装本次版本。只有没有有效盖戳（`unknown`）或盖戳与本次提交相同时，才打印 `fallback` 并重装预检时固定的 `main` 提交。手工部署的 `ca4c938` 没有盖戳，因此首次脚本部署若失败，走的是这条 fallback 路径。

stdout 记录前后 PID/commit。退出码 `0` 表示发布成功，`1` 表示前置条件不满足或发布失败但回滚验活通过，`2` 表示回滚也失败；诊断只输出状态，不转储日志正文。远端目录锁防止两个发布同时解包；若发布机意外退出留下 `~/.rubenbot-deploy.lock`，确认无人部署后再用 `rmdir` 清除，不强占锁。

### 4. 测试

在当前工作树根目录运行。全量测试需要真实 MCP SDK 和 WebUI 依赖；复用旧虚拟环境或切换分支后，也要重新安装当前分支的 `dev` 依赖，不能只更新源码。

```bash
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest
```

全量测试包含多方指向、能力自述与公开事实查证的离线回放，以及链接理解（链接抽取 / 规范化 / 平台识别 / OG 与 JSON-LD 提取 / 正文摘录 / 短链展开 / X 推文的 fxtwitter 取值与逐级后备 / 缓存与额度 / SSRF 拦截 / 大小与超时上限 / 失败如实 / 两种投递方式，全部用本地假页面），另覆盖存储 CRUD / 变更日志与撤销、检索打分、抽取去重、归纳校验（不许新词、20% 上限、user 来源不可删）、画像提炼与时间线压缩、记忆读取侧（索引 / 预拉取 / 预算裁剪 / 跨会话 / 分寸审查 / 隐私命令）、会话滚动摘要（触发 / 水位 / 增量折叠 / 上下文组装 / 开关与容错）、回复决策（合并窗口、查看概率、依据门、发言冷却、硬触发、指向别人的折扣、改写复述核验与发送前压稿、指向事实入决策日志、生成否决）、情绪与好感度（关键词保守识别、增减幅度与每日上限、惰性衰减、档位边界、注入行省略、WebUI 只读端点）、策划/正文生成（依据注入 / 失败回退 / 用量打标）、联网搜索（渠道回退链 / 跳过无 key / DeepSeek 归一 / Bing SERP 解析与容错 / 旧配置兼容 / 冷却）、定时任务（到期执行 / 重启恢复 / 迟到标记 / 取消 / 执行中续命）、提醒工具（注册条件 / 会话上限 / 到点走正常发送通道 / 迟到说明）、冷场开口条件矩阵与内容过滤、语音双向（转写/合成后端解析、模式矩阵、超限跳过、平台降级）、主循环、插件（manifest 校验、显式启用制与 WebUI 开关写回）、插件市场（索引拉取与内置回退、状态合并、安装 / 更新 / 卸载与路径穿越拒绝）与插件权限检查（默认拒绝、执行期拦截与审计）、群发言统计与总结插件（触发词识别、发言聚合与占比、总结取数范围与材料拼装、冷却与当日缓存复用）、感知与动作网关（统一事件通道、触发漏斗、冷却与审计）、表情包（表情库去重与原生引用回填、各适配器 send_sticker 翻译与降级、core 收集 / 概率 / 冷却决策）、SnowLuma 事件解析，以及 Console + 假 LLM 的端到端流程。

**策划为什么沉默**：先查 `decision_log.factors.planner.result_code`。例如顶层同为 `planner_silence`，`http_401` 表示鉴权失败，`truncated` 表示上游明确报截断，`missing_reason` 表示拿到了裁定却缺理由；`planner.reason` 只存模型自己的理由。`usage_id` 可直连本次 `gen_stage1` 的用量行，不再按秒猜测。[历史 88 条样本的只读复算 SQL](docs/planner-reason-reconciliation.sql) 仅用于旧库，不回填。

**回复投递对账**：日志里的「进入策划」不是发送回执。查 `decision_log.factors.delivery.status` 区分 `sent`、`partial`、`send_failed` 和 `superseded`；`raw_ids` 可定位实际出站的本地流水，包括独立表情回复。`factors.planner.speak` 仍是策划原判，即使私聊必回覆盖它也不改写。全败不补表情、不触发发送后插件。旧决策不回填，不能按 `planned` 推断历史送达。

**消息入库了却没进裁定**：先看 `decision_log.reason` 是不是 `plugin_intercept:<插件名>`。这表示消息已按原文入库，但插件 `on_message` 返回了 `False` 把它拦截下来，不会进合批与裁定；`factors.plugin` 是拦截者，`factors.raw_message_id` 指向对应的流水行。日志里同时有一条 INFO 级「消息被插件拦截，未进裁定」。这是第 38 项补的可见性：被插件认领的消息不再是静默空白，一眼能查到是谁吃掉的。

**群聊语气（第 36 项）**：策划和正文共用一句本批房间状态，分得清「有人 @ 我」和「别人问别人」；同一场景里有四对固定的语气示例。大家喊「抄底了」时可以接一句自己的感受，也可以旁听；有人认真问词义或世界知识，就把问题答清楚。不动态找旧话，也不加开口门槛。[六份人设的对照草案](PERSONA-DRAFT-register36.md)只供审阅，运行时仍加载现有人设。

复验用 `REGISTER36_SETTINGS=/path/to/settings.json python -m pytest -s tests/test_register36.py`（会调用指定的 CLI 模型通道，可能计费）。三例各用独立内存库：两段取自 2026-11-25 流水的脱敏摘句，另有不在示例里的月食题。看实际出站及策划结果码，出站的语气和知识答案完整性仍需读原话判断。未设置通道时只测场景组装。2026-11-25 用现有人设和指定通道复验：交易段 `planner_silence`，没有出站；贤惠段答「是褒义词，通常是夸人善良、会照顾人」；月食题答出日地月近乎成一直线，以及月轨有倾角、满月通常错过地影。三例策划结果码均为 `ok`。这是隔离回放，不是线上效果统计。

**Q19 泛化回放**：六个合成场景共用同一个未静音账号，使用独立会话，均送达策划步。独立模型盲评中，天气播报、签到提醒、资讯推送均沉默；真人约同伴还书时旁听，直接求助与邀大家庆祝时开口，与基线提示的三条真人对照一致。pytest 重放新裁定，核对决策日志与实际出站；这不等同于所有部署模型都有相同表现。换模型后可用下方 `--only 8` 实测。

**群聊电脑任务回放**：`python -m pytest tests/test_chat_computer_routing.py tests/test_tool_report_form.py` 会从 Console 入站，核对 `list_dir → write_file → send_file` 的三条审计、落盘字节和原群收到的附件，而不是只看聊天回复。开启 `computer.enabled`、配置真实平台账号白名单、使用支持工具调用的模型，并保持 `memory.recall_tool_max_rounds > 0`。机主可直接委托或 @ bot；授予机主权限不等于每句话都要接。机主操作默认最多 4 轮（`computer.max_task_rounds`）；访客只能用自己的沙盒，不能借同群机主调用真机、MCP 或平台原始接口。

**电脑任务的汇报形态**：交付物送达（`send_file` 成功）或操作成功即静默结束，群里只出现文件本身，模型事后写的完成总结、文件名列表和「如果你需要…」邀约一律丢弃。只有失败或部分完成才开口，且最多一句人话：说清哪一步没成、前面成了什么、现在是什么状态；不发列表、不发邀约尾巴。这句由模型按人设口吻现写，模型不可用时用确定性兜底句；发送后不计正常发言冷却与主动发言窗口（没办成不算接了一次话）。纯查询委托（只用了 `read_file` / `list_dir` / `fetch_url` / `run_command`）仍用结果本身作答，不算额外汇报。决策日志按 `computer_task`（成功静默）、`computer_task_partial`、`computer_task_failed` 区分。

**网关会话准入**：启用 `gateway.enabled` 后，只有真实发送者的 `平台:账号` 列入 `computer.whitelist` 的私聊才默认可用，不需要开启电脑功能。其他私聊和所有群聊须在 `gateway.enabled_conversations` 显式列出（推荐 `平台:会话号`）。开放会话不等于授予机主身份；电脑功能开启时，访客仍不能调用原始网关工具。

**聊天撤回**：在已开放的会话中使用。回复目标消息并 @ bot 说「撤回这条」，即可向网关请求撤回，不走电脑工具。没有引用时，可以同时 @ bot 和一位作者来指定该作者在本会话最近的发言，也可以说「撤回我刚发的那条」或「撤回你刚发的那条」。普通成员只说「撤回」时取该成员本人上一条，不取撤回命令本身。指向不清、找不到引用或没有平台消息号时，bot 会请你回复目标，不猜 ID。撤 bot 自己的消息、作者要求撤自己的消息，以及 `computer.whitelist` 中的机主下令撤回，都不需要打开敏感开关；普通成员要求撤别人的消息仍受 `gateway.allow_sensitive` 限制。权限拒绝、目标不明和平台执行结果均可查 `gateway_audit`。网关放行不等于平台一定能撤，仍取决于 bot 在群内的权限和平台时限。

**SnowLuma 网关真机验收**：[44 项逐动作回执与审计](docs/gateway-e2e-results.md) · [静态接口对照](docs/snowluma-contract.md)。`2.1.1-node` 上已核实空间说说发布、独立读回与清理；私聊贴表情不支持，好友/入群审批等机主配合真实申请。`delete_friend`、`set_group_dismiss`、`set_restart`、`clean_cache`、`set_online_status` 五项永久拒绝，即使开启敏感动作也不会发往协议端。

**Q2 其余真机实测（2026-11-23）**：出站语音、表情图片段、附件按内联字节发送已在自聊实发、读回后撤回；`web_search` 的搜索→抓页→摘录→出站整链在修复正文截断后走通（Tavily 5 条命中、凤凰网原文摘录带出处）；提醒在隔离库 + 真实适配器上到点触发并投递自聊，落库 `done`。两处如实记录的上限：QQ 原生转写 `fetch_ptt_text` 只部分语音可用（自聊语音 `error=9`、部分群语音 20 秒超时，旧群语音可转），群内贴表情仍需机主提供非主群的专用测试群与一条真实群消息。证据与未验条件见 `docs/q2-rest-results.md`。

**手动真实中转自测**（会计费，不由 pytest 启动）：

```bash
.venv/bin/python tools/e2e_live.py --config config.yaml
# 只复查 MCP 与访客沙盒：比对随机回执、CSV、附件，并验证两次越界读均未发言且有拒绝审计；不覆盖全量报告
.venv/bin/python tools/e2e_live.py --config config.yaml --only 3 4 --results /tmp/e2e-tools.md
# 让实测模型独立裁定 Q19 的六个未静音案例
.venv/bin/python tools/e2e_live.py --config config.yaml --only 8 --results /tmp/e2e-social.md
# 星驰 / panda / Argon / 破甲：每案独立建库，保留单次回放，不覆写上次报告
.venv/bin/python tools/e2e_live.py --config config.yaml --only 9 10 11 12 --results "$(mktemp /tmp/replay-XXXXXX.md)"
```

中转须提供 `workbuddy-deepseek-v4.1-flash`；脚本只取配置中的 LLM 连接信息（三层配置取 `chat` 首选提供商，不切换备用提供商），每个场景新建临时数据库和 Console，不接真实聊天平台。完整报告默认写入 `~/workspace/rubenbot-night/e2e-results.md`，只要一项 FAIL 就以状态码 1 退出。模型口头称已执行工具不算审计证据；本地页面被公网限制拦截时，报告会把抓取失败和缓存对照的隔离标注分开。其他凭据来源见 `--help`。

**存量记忆扫描**（第 23 项：只有评价、没有主语的条目）默认只读，只打印报告；`--apply` 才改账本，改动都写 `memory_log`。跑之前先备份数据库：

```bash
.venv/bin/python -m tools.scan_unanchored_memories --db data/rubenbot.db            # 只读报告
.venv/bin/python -m tools.scan_unanchored_memories --db data/rubenbot.db --format json
.venv/bin/python -m tools.scan_unanchored_memories --db data/rubenbot.db --apply    # 补回 / 清掉
```

报告给出扫描总数、可补回数、需清掉数与逐条清单（原文、指称、出处原话）。补回口径：出处原话里能找到唯一可识别专名时，正文加「关于「名字」：」前缀并写入实体锚点；找不到或指称不明才清掉。

四件回放使用仓库五文件人设、合成群历史与真实策划，报告列出实际出站原话和关键决策字段。`source=fallback` 的沉默不算通过，落库 `guess` 也不等于模型亲口报了 `guess`。2026-11-23 单次实测：Argon 旁听通过；panda 直接给了 `yt-dlp` 命令，但首跑采证误判 FAIL；星驰和破甲均因策划降级未验实。未重跑。

---

## 配置速查

| 配置段 | 关键项 | 说明 |
| --- | --- | --- |
| `llm` | `providers` | 提供商列表：各自配置唯一 `name`、`base_url`、`api_key` 与上游 `models` 名称列表 |
| | `role_pools` | 角色 → 有序的 `{provider, model}` 引用列表；`chat` 必填，未配置的角色沿回退链取整个池，支持自定义角色 |
| | 旧 `base_url`、`api_key`、`*_model`、`models` | 单端点旧格式仍可直接使用，原有优先级和回退规则不变 |
| | `model_strategy` `model_strategies` | 模型池策略：`failover`（默认，按顺序故障转移）/ `round_robin`（轮询）/ `random`（随机）；每角色可在 `model_strategies` 覆盖。单模型时三种等价；多模型时最多按列表走一遍、不额外退避 |
| | `budget` | 按任务类型的每日预算闸：`default_daily_calls`（默认 500）、`default_daily_cost_usd`（默认关）、按任务类型 `tasks: {任务: {daily_calls, daily_cost_usd}}` 覆盖、`pricing` 单价（美元 / 百万 token，可写 `提供商/模型` / `模型` / `*`）。未写 `tasks` 时用内置峰值默认；`null` 不限、`0` 全拦。超限只停该类任务，日志打「LLM 预算熔断」；不配 `pricing` 时费用恒为 0、费用档不生效 |
| `timeout` `max_retries` | 单次请求超时（秒）与失败重试次数（408/429/5xx，间隔 1s / 3s；仅单模型时生效） |
| `persona` | `path` | 人格文件，默认 `data/SOUL.md` |
| | `dir` | 五文件人设目录，默认取 `path` 同级的 `persona/`；目录里有 `persona.md` 就按五文件加载 |
| | `planner.identity_triggers` `identity_window` | 身份话题触发词与回看条数（命中才注入 `identity.md` 全文；常驻身份事实独立进策划步） |
| `database_path` | — | SQLite 路径，默认 `data/rubenbot.db` |
| `privacy` | `enabled` | 开启后启用保密封存与跨场合审查，默认关。关闭时 `/隐私` 不启用，仍可清空本人的数据 |
| `memory` | `timeline_recent_hours` `timeline_max_chars` | 其他会话的近期事件流：默认回看 24 小时、总长上限 1800 字 |
| | `self_inject_hours` | 私聊「我最近在别处做过的事」段的回看小时数（固定最多 6 条、600 字，独立预算） |
| | `timeline_hit_limit` | T1 每人相关旧事片段上限（默认 6）；自动查账另有固定上限：每轮最多 3 条、每条正文最多 160 字，不按句式筛选 |
| | `extract_every_n` | 会话攒够多少条未抽取消息触发 |
| | `extract_silence_seconds` | 会话静默多久后触发 |
| | `extract_min_pending` | 单人至少积累几条才抽取 |
| | `inject_limit` | 回复时最多注入多少条记忆 |
| | `dedupe_threshold` | token Jaccard 去重阈值 |
| | `consolidate_new_threshold` | 新增记忆达到多少条触发归纳 |
| | `consolidate_interval_hours` | 距上次归纳超过多久触发 |
| | `raw_keep_per_chat` | 每会话保留的已处理流水条数 |
| | `index_max_members` `index_ttl_seconds` `index_active_days` | 群成员索引（T0）的人数、缓存与活跃窗口 |
| | `inject_budget_tokens` `profile_inject_max_chars` | 记忆注入预算与画像篇幅；超预算先砍事实、再砍时间线 |
| | `cross_session_hours` `self_inject_limit` | 私聊跨会话摘要与（本会话）自我注入的回看窗口与条数 |
| | `gate_confidence_threshold` `gate_ask_high_sensitivity` | 分寸审查的置信度下限与灰区询问开关 |
| | `recall_tool_enabled` `recall_tool_max_rounds` | recall 工具循环开关与轮数（模型不支持时自动退化） |
| | `rolling_summary` | 会话滚动摘要（默认开）：`enabled` / `trigger_after`（未摘要原文超过多少条触发，默认 60）/ `keep_recent`（始终保留的最新原文条数，默认 30）/ `max_chars`（五段式摘要总长上限，默认 800）；摘要用 `summary` 角色生成，未配置时回退 chat |
| | `group_profile` | 群定位（默认开）：`enabled`（后台自归纳开关；手写备注不受影响）/ `update_interval_hours`（每群刷新间隔，默认 24）/ `min_messages`（水位后新消息达到多少条才归纳，默认 50）/ `max_chars`（注入与归纳的总长上限，默认 300）/ `max_batch_messages`（单次归纳最多喂多少条原文）。手写备注在 WebUI 群详情填写，存 SQLite，不入 YAML |
| `chat` | `history_size` | 上下文带入的最近消息条数（只计真实消息，系统/工具条目不占窗口） |
| | `history_ttl_hours` | 超过这个时间的旧消息不再拼进上下文 |
| | `dedupe_ttl_seconds` | 入站去重窗口：同一平台消息 id 在此窗口内重复到达即丢弃（默认 300 秒，0 关闭） |
| | `strip_action_parens` | 发送前移除「（笑）」这类括号动作描写（默认关） |
| `reply` | `max_segments` | 一条回复最多发成几段（默认 3）。完整换行段另发，逗号、顿号、分号收尾的半句接下行；没有换行的长解释可把首句与后续事实分开发 |
| | `segment_delay_per_char_ms` `segment_delay_min_ms` `segment_delay_max_ms` | 段间打字延迟：按下一段字数 × 每字毫秒，再截到上下限（默认 130 / 1500 / 4000 毫秒） |
| | `segment_delay_enabled` | 段间延迟开关；关掉后连发（第一段始终立即发） |
| `attention` | `talk_value` `engaged_window_seconds` | 普通群批次进入查看/策划步的概率与被点名后的全看窗口；默认 `0.75` / `120`，不是最终回复概率 |
| `decision` | `merge_window_seconds` `hard_merge_window_seconds` | 消息合并窗口（普通 / 硬触发） |
| | `batch_max_seconds` | 一批消息从首条起最多等多久（防无限续命） |
| | `urgent_seconds` | 批里出现 @/回复 bot 时，截止时间提前到「首条 + 该值」 |
| | `aliases` | 视为「叫名字」的别名，空则用 `chat.bot_name` |
| | `muted_senders` | 静音发送者账号（字符串列表），默认 `["4012701880"]`；显式 `[]` 关闭。命中记 `muted_sender`，不回复、不进合批、不喂动量；本人清空数据仍执行，但不发回执 |
| | `fatigue_window_seconds` `fatigue_reply_limit` `fatigue_cooldown_seconds` | 同群同来源默认 3600 秒内实际跟评 2 次相似模板后，从下一条相似消息起冷却 1800 秒；任一设为 0 关闭。冷却不波及其他账号 |
| | `min_reply_interval_seconds` | 两次开口的最小间隔，默认 20 秒；硬触发不受限 |
| | `refractory_enabled` `refractory_seconds` | 上次发言后的不应期，默认 45 秒 |
| | `proactive_window_enabled` `proactive_window_seconds` `proactive_window_limit` | 普通群消息的主动发言上限，默认 300 秒内 3 次 |
| | `veto_marker` | 模型输出该标记即静默丢弃 |
| `reminder` | `enabled` | 是否把 `set_reminder` 工具交给模型（默认开） |
| | `max_pending_per_chat` | 每个会话最多同时挂几条待执行提醒（默认 5） |
| | `max_delay_minutes` `max_text_chars` | 单条提醒最远提前多久（默认 30 天）与正文截断长度 |
| `proactive` | `enabled` | 冷场开口总开关（默认关，只在群聊生效） |
| | `idle_hours` `quiet_hours` | 群安静多久才考虑、哪些时段不开口（默认 6 小时 / 23:00–08:00） |
| | `daily_limit` `cooldown_minutes` | 每群每日上限（默认 1）与评估冷却 |
| | `base` `threshold` `threshold_noise` `threshold_noise_band` | 主动开口场景的低意愿评分与阈值扰动 |
| | `relevance_bonus` `relevance_scale` | 群近期话题命中记忆时的加分与归一尺度 |
| `emotion` | `enabled` | 情绪与好感度总开关（默认开）；关掉后不记录、不注入 |
| | `chat_delta` `chat_daily_cap` | 每轮对话加分（默认 0.2）与每人每日累计上限（默认 2.0） |
| | `praise_delta` `insult_delta` | 被夸/感谢（默认 +1）、被骂（默认 −2）的幅度 |
| | `decay_weekly` | 好感度每周向 50 基线回归的比例（默认 0.05，惰性折算） |
| | `praise_mood` `insult_mood` `excitement_mood` `boredom_mood` | 各事件对应的情绪，及其 `*_intensity` 强度 |
| | `half_life_seconds` `min_intensity` | 情绪回落半衰期（默认 3600 秒）与视为平静的强度阈值 |
| | `boredom_after_seconds` | 多久没人理 bot 判无聊（默认 10800 秒） |
| `link` | `enabled` `mode` `delivery` | 链接理解（core 感知层）：`mode` 取 `on_mention`（默认，被点名/被问到才读）或 `auto`（收到即预取）；`delivery` 取 `direct`（默认，资料直接作本轮参考）或 `planner`（策划选中「看链接」才把正文交给生成步） |
| | `cooldown_seconds` `daily_limit` `cache_ttl_seconds` | `auto` 模式的会话冷却（默认 60 秒）、每日抓取上限（默认 100，0 表示不新抓）与成功结果缓存（默认 3600 秒，0 关闭） |
| | `max_bytes` `timeout_seconds` `max_links` `max_body_chars` | 单页读取上限（默认 1 MiB）、单链接总超时（默认 6 秒，含 DNS/跳转/读正文）、每条消息最多读几个链接（默认 3）、正文摘录上限（默认 1200 字） |
| | `fallback_tool` | 可选兜底：抓到了页面但没有可读内容时调用的已发布 MCP 工具（`mcp__<服务器>__<工具>`，参数 `{"url": …}`），默认留空；网络失败与 SSRF 拦截不回退到这层 |
| `plugins` | `dir` `enabled` `command_prefix` | 插件显式启用：`enabled` 里没列的插件不加载，留空表示全部关闭；`group-faq` 的卡片在插件详情配置中维护 |
| | manifest | 每个插件目录下的 `manifest.json` 声明名称 / 版本 / 说明等元信息，加载时校验 `name` 与目录名一致 |
| `search` | `order` `tavily.api_key` `deepseek.api_key` | 联网搜索渠道与尝试顺序（可选 `tavily` / `deepseek` / `bing`，默认 `[tavily, deepseek]`）；key 支持 `${ENV_VAR}`，整条链一个可用渠道都没有则 `web_search` 工具不注册。`bing` 免 key、抓取型（GET `cn.bing.com/search` 解析结果页），排进 `order` 即可用，默认链不含它；页面改版或验证页时按空结果交给回退链。旧的 `provider` + `api_key` 平铺写法自动映射成 `order: [provider]`，已下线的旧渠道会被忽略并打日志 |
| | `timeout_seconds` `cooldown_seconds` | 单次搜索超时（默认 10 秒）与每会话冷却（默认 30 秒，整条链只过一次闸，冷却内只返回提示不发请求） |
| `voice` | `stt_provider` `stt_api_key` `stt_base_url` `stt_model` | 语音转写首选后端（`siliconflow` / `openai_compatible` / `qq_native`，后者仅 SnowLuma）；密钥支持 `${ENV_VAR}`，没有任何可用后端时语音消息只记 `[语音]` 占位。首选失败会顺延到其余已配置的 HTTP 后端；兼容模式靠 `stt_base_url` 判定可用 |
| | `stt_timeout_seconds` `stt_max_seconds` `stt_max_bytes` | 转写超时（默认 30 秒）与单条上限（默认 60 秒 / 5MB，超限不转） |
| | `tts_mode` `tts_provider` | 语音回复模式（`off` 默认 / `reply_voice` / `on_request`）与合成后端（`custom` 默认 / `siliconflow`） |
| | `tts_base_url` `tts_api_key` `tts_model` `tts_voice` | 自定义 TTS：OpenAI 兼容 base_url（如 `https://host/v1`）、Bearer Key、模型与音色；base_url 或 model 缺一个都视为未配置、降级发文字 |
| | `tts_siliconflow_voice` | 硅基流动 CosyVoice2 的音色；`tts_api_key` 留空复用 `stt_api_key`，`tts_model` 留空用默认模型 |
| | `tts_max_chars` `tts_timeout_seconds` | 语音回复字数上限（默认 60，超过发文字）与合成超时（默认 30 秒） |
| `market` | `index_url` | 插件市场索引地址（索引仓库 plugins.json 的 raw 地址）；留空或拉取失败时回退内置官方索引 |
| `background_check_minutes` | — | 抽取/归纳检查间隔 |

---

## 插件开发

插件默认**全部关闭**（显式启用制）：只有在 `config.yaml` 的 `plugins.enabled` 列表里出现的插件才会加载，不写或留空表示一个都不启用。在 WebUI 的「插件市场」卡片上点「启用」，宿主会先按本地 manifest 授权，再加载插件，并写回 `plugins.enabled`。手动修改以下列表只指定要加载的插件，不等同于在 WebUI 显式授权：

```yaml
plugins:
  enabled: ["hello"]   # 只启用 hello 插件
```

每个插件目录根部必须有 `manifest.json`，loader 加载前会校验：文件缺失、必填字段不全、`permissions` 含枚举外的值、或 `min_core_version` 高于当前框架版本，都会拒绝加载（插件仍出现在市场页的「本地插件」里，带 `invalid_reason` 或「核心版本过低」标记）。字段如下：

```json
{
  "id": "hello",
  "name": "打招呼",
  "version": "1.0.0",
  "description": "示例插件：/hello 命令回一句问候",
  "author": "RubenBot",
  "min_core_version": "0.1.0",
  "permissions": ["read_messages", "send_messages"],
  "config_schema": [],
  "triggers": ["「/hello」命令"],
  "repository": "https://github.com/Doulor/Rubenbot/tree/main/plugins/hello",
  "hooks": ["command", "on_message"],
  "category": "互动",
  "icon": "👋"
}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | 是 | 全局唯一，英文小写 / 数字 / `-` / `_`；市场按它和索引条目合并 |
| `name` / `description` / `author` | 是 | 展示用；`description` 是一句话说明 |
| `version` | 是 | 形如 `1.0.0`；市场用它判断「可更新」 |
| `min_core_version` | 是 | 要求的框架最低版本，高于当前版本时禁止安装 / 启用 |
| `permissions` | 是 | 能力声明，只能取下列枚举值；市场安装前逐项展示中文说明 |
| `config_schema` | 否 | 配置项声明，WebUI 据此自动生成表单并校验（见下） |
| `triggers` / `repository` | 否 | 详情页展示的触发方式说明 / 仓库链接（不影响运行） |
| `hooks` | 否 | 用到的钩子名 |
| `category` / `icon` | 否 | 市场分类（互动 / 空间 / 工具 / 系统，缺省「工具」）/ 卡片图标 |

`config_schema` 的每个条目只要求 `key`，其余可选：

```json
"config_schema": [
  {"key": "mode", "type": "string", "default": "on_mention", "label": "解析模式",
   "description": "被点名或被问到才解析，还是收到链接就预取",
   "options": [{"value": "on_mention", "label": "被点名/提问时解析"}, {"value": "auto", "label": "收到链接就预取"}]},
  {"key": "cooldown_seconds", "type": "number", "default": 60, "label": "冷却（秒）", "min": 0, "max": 86400, "step": 1},
  {"key": "sites", "type": "list", "default": ["bilibili", "generic"], "label": "允许的站点", "options": ["bilibili", "generic"]},
  {"key": "api_key", "type": "string", "default": "", "label": "密钥", "secret": true}
]
```

| 字段 | 说明 |
| --- | --- |
| `type` | `bool` / `int` / `number` / `string` / `text` / `list`（兼容 `boolean` / `integer` / `float` / `string_list`） |
| `default` | 默认值；类型不对时退回该类型的空值并记日志 |
| `label` / `description` | 表单标签与说明（没有 `label` 时用 `description`） |
| `min` / `max` / `step` | 数字范围与步长；`list` / `string` 也可用 `min_length` / `max_length` |
| `options` | 可选值数组，裸值或 `{value, label}` 均可；声明后保存时只接受列表内的值 |
| `secret` | 密钥字段；不回显原值，留空表示保留原值（key 名含 `key` / `token` / `secret` / `password` 时自动按密钥处理） |

没有 `config_schema` 的插件照常加载，WebUI 详情页与配置页显示「没有声明配置项」；schema 里写坏的条目会被跳过并记日志，不会让插件变得不合规。官方插件里 `music`（触发词 / 冷却 / 音源 / 音乐卡片）的全部可配项都已进 schema，可作参考。链接理解（第 25 项起）不再走插件，而是 core 感知层，配置在 `config.yaml` 的 `link` 段、由设置页「聊天」页编辑。

`permissions` 枚举固定为 7 项：

| 权限 | 含义 | 高风险 |
| --- | --- | --- |
| `read_messages` | 读取聊天消息、历史与记忆 | |
| `send_messages` | 以 bot 的身份发送消息 | |
| `capabilities` | 调用平台能力层（如贴表情） | |
| `qzone_write` | 在 QQ 空间发布说说、点赞、评论（对外写操作） | 是 |
| `scheduler` | 注册并运行后台定时任务 | 是 |
| `llm` | 调用核心的大模型接口（会产生费用） | |
| `network` | 向外部网络发起请求 | |

带高风险权限的插件，安装确认框里会额外标红提示。框架自带的官方插件（hello / qzone_post / music / image_gen / group_insights / link_detail / group-signup）都已声明真实 manifest，可作参考。

### 启用即信任

新装插件默认停用、没有授权。在 WebUI 显式点击「启用」即表示信任这个插件：宿主先按已安装插件的**本地 manifest** 授予其声明的权限，写入数据库同目录的 `plugin_permissions.json`（默认 `data/plugin_permissions.json`，文件权限 0600）并记审计，再加载插件。详情页可逐项查看声明与授权状态，不提供授权开关。

- **首次升级**：只给当时已启用的插件补齐本地 manifest 已声明但尚未授予的权限，保留已有授权；停用插件的记录不动。这次补齐只做一次。
- **后续恢复**：重启、重载和市场更新后恢复启用状态都沿用已有授权。若新版声明了新权限，可在详情页核对后停用插件，再从 WebUI 显式启用；仅重载不会授予新权限。
- **执行期拦截（默认拒绝）**：`ctx.call_capability` 按 `capabilities`（`qzone.*` 按 `qzone_write`）、`ctx.fetch` 按 `network`、`ctx.llm` 按 `llm`、`runtime.store` / `ctx.store` 读操作按 `read_messages`、适配器 `send` / 宿主代发的命令回复按 `send_messages` 检查。未授权调用直接拒绝并记一条 WARNING 审计（插件名、权限、被拦动作）；撤回授权对已经拿到的代理也立即生效。
- **注入收口**：用 `ctx.capability_note(describe, entry=handler)` 将说明绑定到实际注册的钩子或命令；只有一个行为入口时可省略 `entry`，多个入口时不猜。插件的能力说明只包含所需权限都已授权的条目；声明了 `network` 却没授权的插件不分发钩子与命令；后台任务（`on_start`）启动前再核一遍 `scheduler` 与声明的 `network` / `llm`，任何一项未授权都不拉起，授权变化即时启停。
- **插件写宿主存储不开放**：存储代理只转发读取；写方法没有对应授权项，一律拒绝。
- 授权变更与拦截拒绝都记在 `rubenbot.plugins.permissions` logger 下，WebUI「日志」页可查。

Python 插件可运行任意代码，只应安装和启用可信来源的插件。权限检查只限制宿主公开给插件的入口，不是沙箱。

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

- `on_message(handler)`：`handler(ctx, event)`，返回 `False` 拦截消息。被拦截的消息已按原文入库，但不进合批与裁定；core 会按 INFO 记日志，并在 `decision_log` 写一条 `reason = plugin_intercept:<插件名>`（`factors` 带插件名与流水 id），便于核对「入库了却没进裁定」的消息。
- `on_before_reply(handler)`：`handler(ctx, event, messages)`，返回新的 messages 可改写上下文
- `on_after_reply(handler)`：`handler(ctx, event, reply)`
- `command(name, handler)`：`handler(ctx, event, args)`，返回字符串作为回复；可直接调用也可作装饰器
- `on_start(handler)` / `on_stop(handler)`：启用 / 停用时的回调，后台任务据此启动与干净退出
- `on_load(handler)`：加载完成（`register` 返回后）时调用，`handler(ctx, manifest)`；
  热重载成功后会再调一次
- `on_unload(handler)`：停用、卸载、热重载前调用，`handler(ctx)`，用来清理定时任务、
  后台线程与临时资源；停用后再次启用会补一次 `on_load`
- `on_config_change(handler)`：插件配置经 WebUI 保存后调用，`handler(ctx, old, new)`；
  声明了它才算「配置能热生效」，没声明的插件保存后如实提示需要重启

三个生命周期钩子都是可选的，不声明就什么都不做；同步 / 异步都支持，单个回调异常
只记日志、不炸加载与卸载流程。它们和普通钩子走同一条授权准入：声明了 `network`
却没授权的插件同样收不到这三个回调，跳过时记审计。

钩子和命令都同时支持同步/异步函数，异常会被捕获并记录，不会拖垮主循环。`ctx` 上还能拿到 `store`、`config`、`call_capability`、`fetch`、`llm`、`bot`，钩子拿到的 `runtime` 里有 `persona`、`adapters`、`store`、`llm`、`extras`。这些入口在调用时都按实际授权检查：

| 入口 | 需要的授权 |
| --- | --- |
| `ctx.call_capability(...)` / 适配器 `call_capability(...)` | `capabilities`；`qzone.*` 能力按 `qzone_write` |
| `fetch(...)` 与任何出网行为 | `network` |
| `ctx.llm` / `runtime.llm` | `llm` |
| `store` / `runtime.store` 的读取（`recent_*`、`list_*`、`search_*` 等） | `read_messages` |
| 适配器 `send` / `send_file`、命令返回值的宿主代发 | `send_messages` |
| `on_start` 后台任务 | `scheduler`（若声明）以及声明的 `network` / `llm` |

`extras["bot"]` 与 `extras["plugins"]` 换成了收口视图：前者只暴露按授权检查的 store / llm / adapters / 能力入口，后者对插件不可见。`runtime.memory` / `ctx.extras["memory_reader"]` 的读取同样要 `read_messages`（`runtime.gate` / `ctx.extras["privacy_gate"]` 是分寸审查口，不设授权门槛）。未授权的调用抛 `PluginPermissionError`（命令入口会把它变成一条失败回复）并记审计。

### 插件市场

WebUI 的「插件市场」在同一列表里发现、安装与启停插件，卡片不设授权按钮。「插件配置」按 `config_schema` 编辑已安装插件的配置；「插件详情」展示任一插件的完整档案——介绍（插件目录 README 原文，最多 128 KiB）、版本、作者、只读权限清单（逐项对照声明与授权状态，高危项标红）、触发方式（manifest `triggers` / 运行时命令与钩子）、配置说明与启用状态，并带同一份 schema 驱动表单。市场卡片的「详情」链接直接打开完整详情页，卡片上的箭头仍打开快速预览抽屉。配置表单由 schema 自动生成：`bool` 渲染成开关、`int` / `number` 是带范围与步长的数字框、`list` 是可增删的标签组、带 `options` 的字段是下拉、密钥字段是密码框（不回显，留空保留原值）；保存后端按同一份 schema 校验，错误逐字段显示在控件下方。插件声明了 `on_config_change` 时保存即热生效，没声明的如实提示需重启；没有声明配置的插件显示「没有声明配置项」。原来的 `#/plugins` 链接仍打开市场。

已加载的插件在市场卡片上多一个「重载」按钮：先调 `on_unload`（及后台任务的 `on_stop`），清掉旧模块后按磁盘上的新代码重新 `register`、校验 manifest，再调 `on_load`——改插件代码不用重启 bot。授权记录与插件自己的 `config.yaml` 都保留；新代码导入失败或 manifest 不合规时整体回滚到重载前的实例（旧钩子照常分发），原因以 409 回给 WebUI，插件不会从列表里消失。加载、卸载、重载、配置热应用与钩子异常都记 `rubenbot.plugins.loader` 日志。

- **数据来源**：配置 `market.index_url` 指向索引仓库的 `plugins.json`（raw 地址），条目字段为 `id / name / version / description / author / permissions / path`，可选 `download_url`（插件目录打包的 zip）与 `category` / `icon` / `min_core_version` / `hooks`，以及仅供详情页展示的 `triggers` / `repository` / `config_schema`（索引声明了 schema 时，未安装插件也能预览有哪些配置项，但不能保存）。没配地址或 5 秒内拉不到，就回退到随框架发布的内置官方索引——页面顶部始终标注当前来源与失败原因，不会空白。
- **状态机**：`未安装` → `已安装（未启用）` → `已启用`；附加「可更新」（索引版本更高）、「不合规」（manifest 有问题，详情里写原因）、「核心版本过低」、「仅展示」（索引没给 `download_url`）几个标记。
- **安装**：先弹确认框逐项列出权限（含 `qzone_write` / `scheduler` 时额外标红），确认后下载 zip、校验每个条目的解压路径都在目标目录内（防路径穿越，不合规整包拒绝）、校验 manifest 与 `min_core_version`，落盘到 `plugins/<id>`。**装好默认不启用、默认零授权**。机主在 WebUI 显式启用时才按本地 manifest 授权。
- **更新**：索引版本更高时卡片挂「可更新」，一键替换代码与 manifest，插件目录里的 `config.yaml`、启用状态与已有授权保持不变；新版本新增声明的权限默认未授权。
- **卸载**：启用中先停用，再删插件目录并清掉该插件的授权记录（同名重装时从零授权开始）；目录里自带的文件（含插件自己的 `config.yaml`）随目录删除，框架 `config.yaml` 里该插件的配置段保留不动，只从 `plugins.enabled` 摘除。

索引仓库 `Doulor/Rubenbot-plugins` 需要你自己搭（根目录 `plugins.json` + 每个插件一个目录）；代码只依赖「可配置 URL + 拉不到就回退内置」。

### 限额报名插件

`group-signup` 默认关闭，在 WebUI 显式启用时会自动获得 `read_messages`、`send_messages`、`llm` 三项权限。使用 `/开队 4 周五桌游` 发起活动。后续只有回复插件实际发出的活动卡，才会让模型在「本人报名 / 本人退出 / 查询余位 / 收队 / 不操作」这组有限动作中做提议；代码始终按真实发送者账号、活动卡所属平台和群号校验并执行。没有引用活动卡的「我也去」不会改变名单。报名账本是数据库目录下 `plugin_data/group-signup.sqlite3` 的私有 SQLite，容量检查与成员写入在同一短事务中完成，截止只在读取/提交时惰性处理，最终名单仍可查。

若平台没有返回活动卡消息编号，插件会说明后续需使用带活动编号的命令；创建卡发送失败也不会声称已通知。多个活动且命令未带编号时列出选项，不自动选择最新活动。

### 点歌与画图插件

两个插件都默认不启用（进 `plugins.enabled` 才生效），配置在各自主目录的 `config.example.yaml` 里说明。

- **`music`（点歌，按 hydrogen 点歌插件 1:1 复刻命令面与发送链）**：
  - **命令**：「点歌 <歌名> [序号]」「我想听 <歌名>」「查歌词 <歌名>」；指定平台用「网易点歌 / QQ点歌 / 酷狗 / 酷我 / 百度 / 一听 / 咪咕 / 荔枝 / 蜻蜓 / 喜马拉雅 / 5sing原创 / 5sing翻唱 / 全民K歌 <歌名>」。裸平台名（如「酷狗 七里香」）后面要有空白，免得「酷狗音乐不错」这类闲聊被当成点歌。
  - **选歌列表**：结果多于一条时发编号列表，回复序号选歌、回复「取消」取消；**选中、取消、超时三种结束方式都会撤回列表**（撤回走适配器动作通道 `delete_msg`，需要「平台能力」权限，未授权时只是撤不回，其余照常）。超时默认 30 秒。只认点歌人自己的回复，别人的数字不会被当成选歌。
  - **发送链**：`send_modes` 有序尝试，默认 `record → file → text`——语音条（音频 URL 交给协议端下载转码成能直接播放的语音条，2026-11-23/24 实测网易云 mp3 与 QQ m4a 都完整转成 SILK、帧数换算的时长与源一致）→ 音频文件（下载后上传）→ 歌名 / 歌手 / 时长 / 链接文字。音频发出后再补一条信息文字并附封面（封面按 500 见方缩略图下载后内联，失败退回图片链接）。`card`（OneBot `music` 段）**不放进默认链**：新版 QQ 只显示「发送者版本过低，无法展示内容」，协议端却回执 200，只在配置里显式写 `card` 时才尝试。
  - **音源**：网易云走官方 `cloudsearch`（免 key），QQ / 酷狗 等走 txqq 聚合接口。网易云 VIP / 无版权曲目会 302 到 `music.163.com/404` 的 HTML，协议端转语音必失败，所以搜索结果**先并发探活过滤**，放不出来的不进列表；一条都放不了时如实说「找到的版本都放不了」并建议换平台。聚合源对酷狗 / 咪咕 / 喜马拉雅等子平台经常整页空结果，这时自动退回网易云搜索。
  - **附加**：可选热评一句（默认开）、歌词图（默认关，需要 Pillow 与系统 CJK 字体，缺了自动跳过）。单人冷却默认 60 秒；搜不到就明说没找到，接口挂了回「搜不到，换个关键词试试」并记日志。
  - **没启用时**：被点歌不会无声消失——策划照能力索引明说「点歌还没开」。
- **`image_gen`（画图）**：群里/私聊发「画一张 <描述>」「画个 <描述>」。描述直接用原话，不改写提示词；生图不绑定厂商，走 OpenAI 兼容端点 `POST {base_url}/images/generations`（`{model, prompt, size}` + Bearer 鉴权），硅基流动只是 `config.example.yaml` 里的预设示例之一。响应兼容标准 OpenAI 的 `data[0].url` / `data[0].b64_json` 与硅基流动的 `images[0].url`，拿到后**下载图片字节**再以图片段发送，并在插件 `data/` 目录留一份（不把临时 URL 当图床）。**`base_url` / `api_key` / `model` 三件套缺任一时插件照常加载但不注册触发**，启动日志会说明缺了哪项。内容边界靠服务商审核 + 本地关键词黑名单（`blacklist` 可改，命中直接拒绝）；服务商审核失败时如实回「这个画不了」。单人冷却默认 120 秒、每会话每日上限默认 20 张，超限会回剩余等待 / 明日再来。

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

6. **合批、护栏与策划决定群聊何时开口**（`rubenbot/decision.py`）
   入站先排除静音来源；群内重复跟评的来源还受临时疲劳刹车约束。其余群消息按会话合批，普通批次先过不应期、最小间隔和主动发言窗口，再决定是否查看。私聊、被 @、被回复或叫到名字时绕过普通节奏检查。查看不等于回复：策划步要判断眼下有没有值得接的社交信号、对谁说、有什么依据；生成后也可用 `[不回复]` 否决。决策与原因写入 `decision_log`。

6.1 **指向别人与改写复述也在裁定层处理，发送前复核兜住草稿**
   平台明确回复了别人、正文又没叫到 bot 名字的群消息，护栏直接记 `addressing_elsewhere` 沉默（`DecisionEngine.addressing()`）；只 @ 了别人、没回复谁的只记事实，交给策划。指向事实随裁定写进 `decision_log` 的 `factors.addressing`（含折扣明细），没有明确指向的发言和私聊不写这个字段；它只供核对，不直接决定发不发。`paraphrase_verdict(source, draft)` 是纯函数，剥掉标点、口头填充与泛词后按实词比对，草稿只是把触发消息换个说法、没带来新数字/新问法/新态度/第一人称打算/原句没有的「我」「你」称呼/否定增减时判为复述。发送前复核用它核验普通群聊发言的草稿：判为复述先重写一次，仍是复述就沉默（不发送、不记发言动量），并在 `decision_log` 的 `factors.paraphrase` 留下触发消息 id 与判定结果；被 @ / 被回复 / 私聊 / 机主发言 / 操作委托 / 本轮用过工具（交付、提醒等）的草稿不走此压制。两处都不调模型、不加依赖、不新增配置项。

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
   它不依赖任何平台，支持 `reader` 注入和手动绑定回调，端到端测试正是靠它 + ScriptedLLM 跑通「发消息 → 落流水 → 抽取 → 引用记忆」。SnowLuma 的真实联调仍列为后续增强。

16. **跨平台人物合并暂不做**
   `person_id = f"{platform}:{uid}"`，同一人在不同平台暂时是两个人。等真正出现多平台需求再设计合并策略，避免现在引入不必要的身份映射复杂度。

17. **WebUI 与核心同进程，配置和页面按现行实现记录**
   WebUI 已包含结构化配置、人设热加载、模型页、记忆与人物浏览、群组决策、插件市场、日志、设置和关于；页面以原生 HTML/CSS/JS 提供，登录会话密钥落盘，移动端使用导航抽屉。
### 记忆 v2 读取侧的实现取舍

18. **隐私裁定由核心负责**
    `privacy.enabled: true` 时，时间线、蒸馏记忆及 `recall` 使用同一审查口。即使某条记录与话题高度相关，封存内容也不能跨场合，其他成员私聊和高敏感内容不能带进群。关闭时不调用审查口，不再以受众限制读取；保留来源与保密标记，供日后重新开启时执行。缺少目标会话的旧同步检索接口在保护开启时返回空结果；插件需要聊天召回时应使用 `MemoryReader`。

19. **出处是记忆的一部分**
    不同会话的记录可在同一轮召回，但不会冒充本会话的发言。近期背景带时间和场合；逐字摘录标明说话人；当前会话的上下文仍来自它自己的历史。

20. **自查不走审查口**
    「你记得我什么」是本人查看本人画像，不属于跨场合披露；若也走 gate，隐私模式/高敏感默认扣下会让本人看不到自己的东西，和规格相反。群聊里不自查（避免档案被围观），`/隐私` 在群里也只报档位、画像只在私聊摊开。

21. **recall 工具的退化是常态**
    `chat_with_tools` 只做一次往返，循环在 core（最多 `recall_tool_max_rounds` 轮，默认 2）。中转返回「不支持 tools」时记 `ToolUnsupported` 并记住不再尝试；网络错误、模型不用工具、普通回忆工具异常可退化为单次生成。机主电脑委托例外：使用 `computer.max_task_rounds` 的预算，不以无工具生成代替执行结果。隐私模块开启时，工具结果与普通召回走同一个审查口。

21.1 **电脑任务不带完成总结**
    工具循环末尾原先那次「带实际工具结果收尾」的生成会把交付拆成汇报：三个步骤变成三条总结，markdown 列表又被分段器切碎。现在成功路径直接在循环内返回「静默」，事后给模型的执行记录也不再喂回去重写；失败 / 部分完成单独走一次短生成，只给执行记录和本轮原话，再经 `one_sentence` 做确定性清理（去列表、去换行、去邀约尾巴）。宁可少一句确认，也不让交付物后面跟着一段作文。分段器本身不动，那是另一项。

22. **注入预算的裁剪顺序是硬约定**
    `inject_budget_tokens` 超了只砍「事实 → 时间线 → 跨会话 → 自我 → 索引 → 被提及者画像」，说话人核心画像不参与裁剪。宁可少给背景，也不让主对话对象的关键信息被挤掉。

23. **旧事用相对时间，查账保留出处**
    画像关联的旧事、跨会话摘要、自我时间线用「今天/昨天/上周」等相对时间；近期事件流、自我动作段和话题原话保留来源与时间标签，并在绝对时间后加人话间隔（「18 分钟前」「9 小时前」），便于核对先后和场合。本会话历史里隔了半小时以上才接上的两段之间插一条「时间断层」标注（上一段是什么时候、中间隔了多久），让模型分得清「刚刚」和「昨晚」。

24. **「清空」是真删，日志只留计数**
    `/清空我的数据` 删除本人原话、画像、事件、事实、好感度、裁定和待询问，撤下含本人发言的滚动摘要。旧记忆变更日志中的快照也一并删除，不能靠 `restore` 复活；新的 `purge` 日志只留计数。开启保护时 `/隐私 清空` 执行同一操作。清空或切到「不记」时，正在等待模型的画像提炼整轮作废：即使随后恢复记录，也不会写回旧画像、证据链或含正文的丢弃日志。

25. **先裁定，再按需生成正文**
    策划步用 `fast` 给出依据、是否开口、意图与长度档；需要正文才调用生成步。生成期间有重要补充时，合批复核一次。查看概率、护栏与策划结论随裁定写入 `decision_log`。
26. **搜索结果是原文入口，不是证据**
    渠道按 `search.order` 依次尝试，跳过没配 key 的（免 key 的 `bing` 恒可尝试），抛错、超时或空结果就换下一家；冷却按会话计算，
    一次查询只过一次闸。结果页只走匿名的公网 GET，不登录、不提交表单、不运行页面脚本。
    每轮最多打开 2 个结果页，每页最多读入 8 KiB 正文、等 4 秒；搜索和抓页合计最多 10 秒（`search.timeout_seconds` 更短时从其值）。
    压缩正文解到读取额度就按截断处理、标记 `[已截断]`，不因整页更大而放弃（实测资讯站解压后
    普遍 35–100 KiB）；身份编码、未支持的压缩格式与损坏的压缩流仍然报错。
    隔离的阅读步骤最多等 12 秒，只能选择正文中的一句，不能调用聊天工具。发出的文字附上跳转后的实际来源；读不到或选不出原句，就说没查实。引用证明该页这样写过，不代表网页本身一定正确。流水只保存这次查证的占位，网页不进入记忆提炼。
    旧配置的 `provider` + `api_key` 平铺写法仍映射成 `order: [provider]`。
    `bing` 是免 key 抓取渠道：GET `cn.bing.com/search` 解析结果页 HTML，只收有直链的条目；
    页面改版、验证页或空结果页都算「本渠道没结果」，回退链照常顺延。检索词形态影响很大，
    整句问句容易被拆散，关键词式检索词效果最好（实测见 `docs/bing-search-results.md`）。
    真机实测（2026-11-23）：Tavily 搜索、公网抓页、摘录与出站整链走通；模型是否主动选择
    `web_search` 取决于提问措辞，模型给出的检索词必须原样出现在用户原话里（防私密上下文外泄的
    硬门），否则直接回「没查实」。
27. **定时任务只做一次性时间点，落库即恢复**
    新表 `scheduled_jobs`（kind / 会话 / fire_at / payload / 状态）承载提醒；调度器每 10 秒扫一次到期任务，**handler 返回后才标 done**——进程在执行途中被杀，任务仍是 pending，下次启动补发一次（宁可重复一次，也不默默吞掉）。到期超过 1 小时算「迟到」，仍然执行，但提示模型自然说明「来晚了」。不引 cron、不引外部库、不做周期任务。

28. **提醒只回应用户明确请求，且每会话有上限**
    `set_reminder` 与 recall 共用同一套工具循环（最多两轮）：`delay_minutes` 优先、`at_iso` 兜底，解析与上限校验都在 `reminder.py`，拒绝原因原样回给模型。每个会话最多 5 条待执行提醒，防止模型被反复要求时无限堆积；一期不做取消工具，WebUI 记忆页只读列出待执行提醒。真机实测（2026-11-23，隔离库 + 真实 SnowLuma）：到点由调度器触发，按提醒正文现写一句发到自聊，落库状态 `done`、`fired_at` 非空，读回后撤回；生成失败时退回提醒原文照常发送。

28.1 **到点提示不把请求者写成第三人**
    2026-11-24 08:31 主群实测：机主说「5分钟过后提醒我吃饭」，到点发出「多萝让我喊你一声」——把请求者（也是唯一被提醒的人）当成了第三方。根因是触发提示把 payload 的 `requester_name` 原样拼成「之前多萝让你提醒一件事」。现在 `reminder.build_reminder_prompt` 只收 `ReminderParties`：请求者=被提醒者时提示里不出现任何姓名，直接对本人说事；只有 A 让提醒 B（A≠B）时姓名才出现，并写明谁托你提醒谁。关系由 `parties_from_payload` 按结构化 id 判定（`requester_id` 由 `set_reminder` 按平台账号写死；私聊接收者就是会话对方；缺 id 的旧 job 按本人提醒本人处理），`requester_name` 只留在台账与 WebUI 列表。提醒对所有人开放：群友托提醒同样建 job、走同一条触发链。planner 的「记提醒」能力事实改为与工具注册表同源（`set_reminder` 在工具清单里且有会话余量就报能记），不再要求本进程先记过一条，消掉「reason 说不在能力里、实际却建了 job」这类矛盾。离线回放见 `tests/test_reminder_pronoun.py`；机主真人复测待部署后。

29. **冷场开口默认关，且「宁可漏，不可扰」**
    条件矩阵全部满足才进入意愿评估：群聊、默认开关打开、不在安静时段、当日未达每群上限、距上条消息超过 `idle_hours`；冷场评估独立使用低基础分、话题相关性与阈值扰动。模型写不出、输出否决标记 `[不开口]`、或写成罐头问候（「有人在吗」等黑名单）就放弃本次。私聊永不主动开口；当日计数与评估冷却只在内存里，重启即清零。

30. **语音失败永远降级为文字，语音只是锦上添花**
    转写前的语音条占位是正文本身：纯语音条原本是空正文，落流水时会被丢弃；现在至少保留 `[语音]`（合并轮次也据此给 `has_voice` 打标）。转写成功就读作 `[语音] 文本`，失败/未配置/超限都只留占位，下游记忆与统计不用改。出站语音只在「一条未拆分、且不超过 `tts_max_chars` 字的回复」上尝试：被拆条的长回复一律发文字（避免半句语音半句字），合成失败、平台不支持（QQ 官方）、能力被配置关闭都照常发文字。合成默认走自定义 TTS（OpenAI 兼容 `/audio/speech`），未配 base_url/model 或请求失败都自动降级；`qq_native` 转写失败也如实顺延到下一个后端，绝不臆造文本。

31. **点歌默认发能直接播放的语音条，音乐卡只做配置可选项**
    协议端对新版 QQ 不渲染 OneBot `music` 段（`type=netease` + 歌曲 id）这件事毫无感知：回执照样 200，接收方客户端却只显示「发送者版本过低，无法展示内容」，所以「发送抛异常才降级」这条兜底永远等不到触发。2026-11-23/24 实测 `record` 段（`data.file` 直接放音频 URL）能被协议端自己下载、转码并发成能播的语音条（网易云 mp3 与 QQ m4a 都完整转成 SILK，帧数换算时长与源一致），默认链因此定为 `record → file → text`；`card` 只在配置显式写了才尝试。验证口径是接收端肉眼可见、能直接点开播放，不是协议回执。

32. **点歌先探活再进列表，探不出音频的条目宁可不说**
    网易云的 `song/media/outer/url` 对 VIP / 无版权曲目会 302 到 `music.163.com/404`（`text/html`），协议端拿 HTML 转语音会失败（实测 retcode 100）。所以搜索结果先并发探活（HEAD 看最终 `content-type`，CDN 不支持 HEAD 时退回 `Range: bytes=0-1023` 的 GET），明确不可播的条目不进选歌列表；网络异常按「不确定」保留，交给发送链降级。一条都放不了时如实说「找到的版本都放不了」并建议换平台，而不是塞一首放不出来的。聚合接口对酷狗 / 咪咕 / 喜马拉雅 / 5sing / 全民K歌 经常整页空结果，这时退回网易云搜索；退回后列表标题按结果的真实来源写，不冒用原来的平台名。选歌列表不管有没有 `message_id` 都会登记待选（没有 id 只是撤不回，选歌照常）；撤回失败（未授权或无 id）只记日志，不影响点歌本身。探活整批另有 12 秒总时限，CDN 慢时先用手上已探完的结果，不让人干等。

33. **预算闸按任务类型熔断，缺数据就不编**
    上限按线上 `llm_usage` 的单任务日峰值留 3～5 倍余量（口径见 `docs/llm-budget-results.md`），默认只开次数档。费用档必须显式配 `pricing` 单价才生效——中转站查不到 workbuddy 公开价格时，宁可费用恒为 0、费用档不触发，也不拿 token 乘一个编出来的单价。熔断是「停掉这一类任务」，不是让 bot 崩：各调用方本来就按 LLM 异常降级（策划失败走短呼应、抽取/摘要失败下轮再试），`LLMBudgetExceeded` 不随 `max_retries` 重试，也不切模型池下一候选。台账放 `meta` 而非新表，按自然日整体替换，省掉流水聚合与清理成本；进程内计数允许并发少量超额。

---

## 路线图

- **已完成**：记忆层、主循环、Console/SnowLuma/Telegram/QQ 官方适配器、插件系统、配置、测试、文档与 WebUI（含模型、人格、记忆、人物、群组决策、插件市场、日志、设置、关于）。
- **后续增强**：真实 SnowLuma 联调与 embedding 检索融合。

WebUI 的「人设」页可以直接编辑人格文件：保存时先写 `.bak` 备份，运行中的 bot 会立即热加载新人格，后续消息无需重启即可生效；「恢复默认」同样热加载。页面右侧只读展示当前实际生效的人格与系统提示词拼装预览，方便核对。

人设支持五文件形态（`data/persona/`）：`persona.md` 常驻人格、`behavior.md` 策划步行为风格、`reply_style.md` 生成步回复风格、`identity.md` 的 `## 常驻身份` 小节进策划步自身状态（全文仍按话题触发）、`owner.md` 私聊时注入。随仓五文件是 [DRAFT-A2 待审稿](docs/persona-a2-审稿.md)。策划步 = persona + behavior（+条件注入），生成步 = persona + reply_style（+条件注入），单段生成三份都带；目录不存在时回退到单文件 SOUL.md 的旧行为。WebUI 人设页会列出这五个文件，按文件切换编辑并在保存时带上 `file` 参数。

## 许可

MIT。
