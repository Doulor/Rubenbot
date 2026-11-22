# SnowLuma 网关真机验收 · 2026-11-23（北京时间）

**未全通。** 44 项中，23 项收到真实 OneBot 成功回执并达到表中注明的验收范围；3 项收到真实失败回执；5 项按要求只验禁止闸门；2 项等真实申请；11 项未做群写操作。**HTTP 200 不等于业务成功，业务成功也不一定证明 QQ 客户端可见。**

## 如何取得证据

- 家里云回环端口：未认证探活 HTTP 401；认证后 `get_status` 在线且健康，`get_version_info` = SnowLuma `2.1.1-node`，`get_login_info` 与指定测试小号一致。令牌只在内存中用于本机请求，未写入文件、命令行参数或报告。
- 将已安装的 `/app/runtime/index.mjs` 与基线 `40be9db` 的目录逐名比对：[44 项参数、必填条件、返回值及反向差集](snowluma-contract.md)。43 个同名注册；`set_group_dismiss` 未暴露，另有两个固定成功占位、一个固定不支持。静态存在不作动态通过。
- 真机调用走 `GatewayService.call → SnowLumaAdapter → OneBot HTTP`。独立验收进程的网关服务、闸门、注册表、适配器四个文件与基线 SHA-256 一致。每次先查本次 SQLite `gateway_audit` 的动作名、来源、成败，再从 HTTP 响应单独取状态码、`retcode`、数据形状。**行号属于每批独立的内存 SQLite，不是生产审计 ID**；只保留下表脱敏摘录。生产库只读查看时 `gateway_audit`、`gateway_state` 均无行；未修改线上配置/数据，未部署或重启。
- 工具入口另测：结构化 `adapter_actions(get_status)` → `adapter_call(get_status)` 经 core 模型工具分派，`tool_call_id` 匹配，返回 `online`，HTTP 200/0，审计 T4 `source=model,ok=1`。这是实际分派，不是线上聊天模型自主选择动作。另用配置模型生成同一只读调用，在独立进程收到 `LLMResponseError`；模型配置引用环境变量，未取用线上进程密钥。**不声称生产模型自由决策全链已通过**。
- 表中 `R` = 只读/闸门批，`P` = 私聊批，`F` = 自聊媒体，`V` = 有声转写复测，`Z` = 空间，`T` = 工具/翻页对照，`D` = 历史检查，`D′` = 按消息 ID 撤回复核，`L` = 自赞复核。数字为该批审计行号。`通过` 均有 **HTTP 200、status=ok、retcode=0、ok=1**；`失败` 均有 **HTTP 200、status=failed、retcode=100、ok=0**；`闸门通过` 是拒绝成功、HTTP **零出站**。不提供账号列表、消息原文、文件地址、Cookie、CSRF 值或申请标识。

## 逐动作结果（按注册表顺序）

| # | 动作 | 结论 | 证据或未验原因 |
|---:|---|---|---|
| 1 | `send_private_msg` | 通过 | P4 返回 `message_id`；P5 按 id 读回本号发送者与唯一测试正文。 |
| 2 | `send_group_msg` | 未验 | 小号所在 13 群无单人群，生产群外未识别到专用测试群；未向群友发测试消息。 |
| 3 | `send_msg` | 通过（私聊） | P9 指定 private；P10 核对发送者和第二条测试标记，P11 撤回。群发分支未验。 |
| 4 | `delete_msg` | 通过（按 ID 读回） | 仅撤回本轮自发消息。D′5 按同一 id 确认小号与唯一正文，D′6 撤回返回 `null`；1 秒后 D′7 再查同一 id 得 `failed/100: message not found`。D 批的私聊历史在撤回前就未命中，不用它作证；无客户端截图。 |
| 5 | `get_msg` | 通过 | P5/P10 读到匹配的发送者和测试正文；F5 取得语音消息段。 |
| 6 | `get_forward_msg` | 通过 | F8 使用本号自聊合并转发的真实资源 ID，`messages` 中找到预设节点文字；F10 撤回样本。 |
| 7 | `send_like` | 失败（自赞） | P12/L4 本号给本号点 1 次；OIDB 1001，协议明确「禁止给自己点赞」。未找无关好友替代。 |
| 8 | `set_msg_emoji_like` | 失败（自聊）；群效果未验 | P6 用 P4 的真实消息，返回 `not supported`；P7 无表情字段。实现仅支持带权威序号的群消息，无专用群，未在主群贴表情；没有 QQ 端可见证据。 |
| 9 | `fetch_ptt_text` | 失败 | F7 静音及 V7 合成有声样本均失败；后者 `ptt translate failed: error=9`，未返回文字。V6 同一语音可读；未修改网关绕开转写。 |
| 10 | `set_friend_add_request` | 等机主配合 | 假适配器已验工具分派、显式审批参数、敏感开关关/开、拒绝/成功各一行审计；未制造或提交真实 flag。 |
| 11 | `set_group_add_request` | 等机主配合 | 同上，带 `sub_type=add`；事件裁定保持只通知、不自动审批。没有使用他人的待办申请。 |
| 12 | `get_stranger_info` | 通过 | R7 查询小号，资料对象含 `user_id,nickname,sex,qq_level` 等字段。 |
| 13 | `get_friend_list` | 通过 | R6 返回 62 项，条目含 `user_id,nickname,remark`。 |
| 14 | `get_group_info` | 通过 | R11 使用 R10 中的真实群，返回群号、名称及人数等字段。 |
| 15 | `get_group_list` | 通过 | R10 返回 13 个群对象；只读选择取样群，未公布名称及 ID。 |
| 16 | `get_group_member_info` | 通过 | R12 查询该群中小号自身，返回成员、名片、角色字段。 |
| 17 | `get_group_member_list` | 通过 | R13 返回 454 个成员对象，未输出成员列表。 |
| 18 | `get_group_honor_info` | 通过 | R14 `type=talkative`，返回群号、当前龙王及荣誉列表结构。 |
| 19 | `get_group_msg_history` | 通过；旧目录有误 | R15 返回 `messages`；T5–7 对同一页取锚点，旧 `message_seq` 返回相同页，真正的 `message_id` 返回不同页。本分支已改目录。 |
| 20 | `set_group_kick` | 未验 | 无专用群及同意被移出的测试成员，不踢真人。 |
| 21 | `set_group_ban` | 未验 | 无已授权的群/成员，不改变他人发言权限。 |
| 22 | `set_group_whole_ban` | 未验 | 会影响全群，未发请求。 |
| 23 | `set_group_admin` | 未验 | 无获准变更角色的测试群及成员。 |
| 24 | `set_group_card` | 未验 | 改自己名片也会在真实群留下可见变化；无专用群。 |
| 25 | `set_group_name` | 未验 | 不改线上群名。 |
| 26 | `set_group_leave` | 未验 | 会改变小号真实群关系；不退群，也不以退群替代解散。 |
| 27 | `set_group_special_title` | 未验 | 无可安全变更头衔的群与成员。 |
| 28 | `get_login_info` | 通过 | R3 账号与指定小号一致，核对后才做可见动作。 |
| 29 | `get_status` | 通过 | R1 `online=true,good=true`；T4 另外验证 core 工具返回与 `source=model` 审计。 |
| 30 | `get_version_info` | 通过 | R2 返回 `app_name,app_version,protocol_version`，实现/版本见上。 |
| 31 | `get_cookies` | 通过（只验形状） | R8 指定域，`cookies` 字段非空，不展示值。基线会将结果写进审计，探针只用内存库；分支已修以后审计的留存。 |
| 32 | `get_csrf_token` | 通过（只验形状） | R9 返回 `token` 字段；未输出或另行消费值。 |
| 33 | `can_send_image` | 通过 | R4 `yes=true`，是能力查询，不是图片送达回执。 |
| 34 | `can_send_record` | 通过 | R5 `yes=true`；F4/V4 另有自聊语音样本。 |
| 35 | `get_image` | 通过 | R16 用历史中真实图片段的文件标识，返回 `file,url,file_size,file_name`。 |
| 36 | `get_record` | 通过 | F6/V6 使用真实自聊语音段，返回文件路径、大小等四字段，未请求转码。 |
| 37 | `send_qzone_msg` | 通过；已清理 | Z4 返回 `tid,time`；用独立空间 feeds 按本号作者和随机正文匹配。删除前再次精确匹配唯一帖子，仅对其 `tid` 调上游 `delete_qzone_msg`（不在网关目录，清理专用），HTTP 200/0；删除后同一 feeds 匹配数为 0。 |
| 38 | `set_group_portrait` | 未验 | 无专用群，不改真实群头像。 |
| 39 | `set_group_anonymous` | 未验；上游占位 | 此版本静态实现直接返回 `null`，不读参数、不切换匿名；不拿成功包冒充效果。 |
| 40 | `delete_friend` | 闸门通过 | R17 明确禁止、`ok=0`、无 HTTP；绝不真删好友。 |
| 41 | `set_group_dismiss` | 闸门通过 | R18 同上；上游另无同名注册，拒绝发生在分派前。 |
| 42 | `set_restart` | 闸门通过 | R19 同上；未向协议端发送重启。 |
| 43 | `clean_cache` | 闸门通过 | R20 同上；未向协议端发送清理。 |
| 44 | `set_online_status` | 闸门通过 | R21 同上；未改变小号在线状态。 |

## 复验与遗留

`allow_sensitive=true` 的隔离配置也不能打开五个硬禁项。`tests/test_gateway.py` 可离线复核五项均不可发现/不可分派且各有一条拒绝审计，以及两类申请的敏感开关与分派；`tests/test_gateway_credentials.py` 检查凭据不会留在新审计或日志。全量 `pytest`：**2150 passed / 1 skipped**（基线 2124/1）；`ruff check .` 和 `git diff --check` 通过。回归测试不替代上表任何一项真机证据。

优先补验条件：提供非主群的专用测试群和一条真实群消息，在 QQ 客户端核对表情；提供同意配合的点赞目标。有声转写的上游错误仍待定位。

**等机主配合一个真实申请：好友申请、入群申请各一例。** 核对事件、审批参数、OneBot 回执、QQ 侧结果和对应审计后才能终验；两项当前均未通过真机审批验收。
