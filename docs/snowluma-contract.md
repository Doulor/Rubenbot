# SnowLuma 网关：44 项静态接口契约对照

**结论（2026-11-23）：44 个网关名字中，43 个在已安装的 SnowLuma 中注册，1 个未暴露（`set_group_dismiss`）。** 在这 43 个中，`set_group_anonymous`、`clean_cache` 只返回成功、不执行操作；`set_restart` 固定返回不支持。其余 40 个有处理逻辑，不代表账号有权限、请求成功或 QQ 端效果已经验收。另有 **165 个上游注册名**不在网关目录，含别名及隐藏入口。

最容易造成误判的差异是：群历史的锚点叫 `message_id`，不是 `message_seq`（传错仍可能拿到最新一页）；空间说说只传正文时默认**所有人可见**；贴表情必须是可解析出 QQ 权威序号的**群消息**。好友/入群申请均须等机主配合一个真实申请才能终验，本表不是审批回执。

## 版本、证据和读表方法

- 网关：[44 项注册表](../rubenbot/gateway/catalogs/snowluma.json)，基线提交 `40be9dbba5f3af6d60b8b6ba24b232edc8c9cbe8`，目录文件 Git blob `05a8774a003e38283a1dc0f75397227b1a684121`。
- 上游：只读复制 `snowluma` 容器的 `/app/runtime/index.mjs` 至独立空目录，未加载或执行该模块。原件与副本 SHA-256 均为 `e23ed3d4aa7e01e7f45400a7ffe6b0c4b67bb6d011f776f2fd04b959510435e6`（9,407,603 字节）。代码内置版本 `2.1.1`（L5892–5947），版本动作构造 `SnowLuma / 2.1.1-node / v11`（L7384–7390），不是在线查询值。未发 HTTP、未调用动作、未读取部署配置、令牌、日志或用户数据。
- 下表 **L 为该哈希文件中的行号**。沿 `ACTION_GROUPS` → `compileActionRegistry` → `registry.bind` → 按名分派核对（L16460–16545、16547–16607、16711–16725、16772–16779），展开 `name` 数组中的别名；不把内部函数、文档示例或相似名字算作注册。
- 输入列写**上游实际字段**：`x` 必填，`x?` 可省，`x=v` 可省且有默认；“至少一个”等运行条件另写。`group_id/user_id` 通常为正整数，`message_id` 通常为非零整数（可为负），例外单列；`message` 为字符串、消息段数组或对象。布尔、整数、字符串的兼容转换见 L6837–7110。`groupAction` 补必填 `group_id`，`groupUserAction` 再补必填 `user_id`（L7269–7291）。
- 差异列以**网关相对上游**为方向。“未列”不是“必定拒绝”：网关只校验声明的字段，必填值不能为 `null` 或空串，`string` 必须是字符串；`params:null` 是自由 JSON 对象，**不是无参**（[校验 L359–402](../rubenbot/gateway/service.py#L359-L402)）。上游只把自己声明的字段交给 `run`，多余键被忽略（L7181–7203）。「一致」指字段名与必填关系，不代表基础类型强制转换、值域或权限完全相同。
- **返回列是成功信封的 `data`**，不是模型最终看到的文本。网关目录 44 项均未声明返回 schema；[适配器 L243–251、275–289](../rubenbot/adapters/snowluma.py#L243-L289) 取 `data`，网关另做摘要。上游成功为 `{status:"ok",retcode:0,data}`，无参 `okResponse()` 的 `data` 为 `null`；失败为 `{status:"failed",retcode,data:null,wording}`（L6716–6735）。「未暴露」表示此版本注册表没有这个名字；「未知」表示从静态代码无法判断线上值或结果；`null` 两者都不是。

以下是**核对时的基线目录**，保留发现时的差异，避免把修复误记为当时就已对齐。本分支后续修复见 `49e9eb4`：消息段联合类型、群历史锚点、空间权限参数、资料卡点赞默认次数、群表情前提及若干误导性说明已更新；未宣称其余差异已消失。动态证据另见[逐动作验收](gateway-e2e-results.md)。

## 逐项对照

### 消息、互动与申请（1–11）

| # | 网关名 | 上游输入及必填条件 | 成功 `data` | 网关差异／前提 | 上游证据 L |
|---:|---|---|---|---|---|
| 1 | `send_private_msg` | `user_id, message, group_id?, auto_escape=false`；此处 `group_id` 为非负整数，正数用于临时会话 | `{message_id:数值}` | 未列 `group_id`；注明可发段数组却将 `message` 声明为 string，网关拒绝数组/对象 | 7496–7510 |
| 2 | `send_group_msg` | `group_id, message, auto_escape=false` | `{message_id:数值}` | 必填一致；同上，网关 string 校验拦截上游支持的段数组/对象 | 7511–7522 |
| 3 | `send_msg` | `message, message_type?, group_id?, user_id?, auto_escape=false`；指定 group 或有群号走群发，需群号；否则需用户号。显式 private 且两号都有走临时私聊；空串群号视为缺省 | `{message_id:数值}` | 未列 `auto_escape`、临时私聊及目标的条件必填；只接受 string 消息，网关整数校验也不接受空串群号 | 7431–7442、7475–7495 |
| 4 | `delete_msg` | `message_id` | `null` | 一致；找不到可撤回的消息元数据则失败 | 7543–7553 |
| 5 | `get_msg` | `message_id` | 消息事件对象：去 `post_type/self_id`，加 `real_id`；媒体地址可能刷新 | 一致；不是固定字段全集，未命中不是成功空对象 | 7523–7542 |
| 6 | `get_forward_msg` | `id?, message_id?`（字符串）；至少一个有效，优先 `id`，也可从 `message_id` 所指事件提取转发资源 ID | `{messages:节点对象数组}` | 网关只列并强制 `id`，不能单用 `message_id` | 12412–12453 |
| 7 | `send_like` | `user_id, times=1`；次数为非负整数 | `null` | 网关写默认 10、上限 20；上游默认 **1**，动作与序列化层未设 20 上限，QQ 实际限额未知。这是资料卡点赞，不是戳一戳 | 10152–10166、218109–218119 |
| 8 | `set_msg_emoji_like` | `message_id, emoji_id`（非空字符串），`set=true` | `null` | 字段一致；只接受已知群消息且须有权威 QQ 序号，私聊不支持；返回无计数或可见效果证明 | 10648–10660、33087–33096 |
| 9 | `fetch_ptt_text` | `message_id` | `{text:字符串}` | 一致；是主注册名（另有两个别名），需要未撤回、含 record 且有媒体快照或缓存的消息 | 10518–10539、32870–32904 |
| 10 | `set_friend_add_request` | `flag`（非空字符串），`approve=true` | `null` | 网关强制 `approve`；目录中的 `remark` 不被读取，成功不代表已设备注。须真实申请 | 9534–9544 |
| 11 | `set_group_add_request` | `flag`（非空字符串），`sub_type?, type?, approve=true, reason=""`；动作优先选 `sub_type`，其次 `type`，否则 `add` | `null` | 网关强制 `sub_type, approve`，未列 `type`；本构建的桥接忽略 `_subType`，按 `flag` 解析申请。须真实申请 | 9545–9560、32945–32973、33006 起、33075 |

### 资料、历史与群管理（12–27）

| # | 网关名 | 上游输入及必填条件 | 成功 `data` | 网关差异／前提 | 上游证据 L |
|---:|---|---|---|---|---|
| 12 | `get_stranger_info` | `user_id` | 资料对象，至少 `user_id,nickname,remark,sex,age,long_nick`；其它账号、等级、会员字段视资料可用性 | 网关另列 `no_cache`，上游忽略；兜底空资料不证明查到此人 | 7594–7732 |
| 13 | `get_friend_list` | 无 | 好友数组，每项含 `user_id,nickname,remark`；可为空 | 网关自由对象；上游无参 | 7558–7593 |
| 14 | `get_group_info` | `group_id, no_cache=false` | 群对象 G（见表下），可返回该群号加空/零字段 | 一致；兜底对象不证明群资料有效 | 8245–8298、8392–8417 |
| 15 | `get_group_list` | `no_cache=false` | G 数组，可为空 | 网关自由对象，未描述 `no_cache` | 8323–8391 |
| 16 | `get_group_member_info` | `group_id, user_id, no_cache=false` | 成员对象 M（见表下）；可用输入 ID 返回兜底资料 | 一致；兜底角色 `member` 不证明存在 | 8543–8696 |
| 17 | `get_group_member_list` | `group_id, no_cache=false` | M 数组，可为空 | 网关未列 `no_cache` | 8418–8542 |
| 18 | `get_group_honor_info` | `group_id, type?`；缺省或空串按 `all`，其它有效值 `talkative/performer/legend/emotion` | `group_id,current_talkative`（可 null）及 `talkative_list,performer_list,legend_list,emotion_list,strong_newbie_list` | 网关强制 `type`；上游 `strong_newbie_list` 初始化为空，无对应可选 type；部分取数失败也可能为空 | 8206–8213、8228–8241、8697–8713、255724–255748 |
| 19 | `get_group_msg_history` | `group_id, message_id=0, count=20, reverse_order=true`；`count≥0`，实际 0 取 20、最多 200；方向仅对非零锚点生效 | `{messages:消息事件对象数组}` | 网关的 `message_seq` 被忽略；未列 `message_id, reverse_order`。用前者翻页可能成功却重复最新页 | 10288–10310、31035–31061、31775–31781 |
| 20 | `set_group_kick` | `group_id, user_id, reject_add_request=false` | `null` | 一致 | 8800–8808 |
| 21 | `set_group_ban` | `group_id, user_id, duration=1800`（非负秒，0 解除） | `null` | 网关强制 `duration` | 7028–7036、8821–8829 |
| 22 | `set_group_whole_ban` | `group_id, enable=true` | `null` | 网关强制 `enable` | 8830–8838 |
| 23 | `set_group_admin` | `group_id, user_id, enable=true` | `null` | 网关强制 `enable` | 9053–9061 |
| 24 | `set_group_card` | `group_id, user_id, card=""` | `null` | 网关强制 `card` 且拒绝必填空串，目录所说“空串清空”无法经该校验 | 9062–9070 |
| 25 | `set_group_name` | `group_id, group_name=""` | `null` | 网关强制非空 `group_name`；上游动作允许缺省/空串，不代表 QQ 接受空群名 | 9071–9079 |
| 26 | `set_group_leave` | `group_id` | `null` | 网关的 `is_dismiss` 被忽略，上游只走 leave；不是可通过此字段选择的解散接口 | 9080–9087、172086–172089 |
| 27 | `set_group_special_title` | `group_id, user_id, special_title=""` | `null` | 网关强制非空 `special_title`；`duration` 不被读取，不能承诺有效期 | 9088–9096 |

G 的 schema 至少要求 `group_id,group_name,group_remark,member_count,max_member_count,group_all_shut`；另列 `group_create_time,group_level,group_memo,group_description`。M 至少要求 `group_id,user_id,nickname,role`；另有群名片、入群/最后发言时间、禁言截止、等级、头衔等。完整字段及占位值分别见 L8245–8298、8423–8534、8548–8656；列表与单条不是同一份存在性证明。

### 自身、媒体、空间及禁止项（28–44）

| # | 网关名 | 上游输入及必填条件 | 成功 `data` | 网关差异／前提 | 上游证据 L |
|---:|---|---|---|---|---|
| 28 | `get_login_info` | 无 | `{user_id:整数,nickname:字符串}` | 网关自由对象；上游无参，未读取账号值 | 7295–7321 |
| 29 | `get_status` | 无 | `{online:布尔,good:布尔,time:运行秒数}` | 网关自由对象；上游无参，在线值未知 | 7322–7357 |
| 30 | `get_version_info` | 无 | `{app_name,app_version,protocol_version}`，三个字符串的本构建值见版本栏 | 网关自由对象；上游无参 | 7358–7391 |
| 31 | `get_cookies` | `domain="qun.qq.com"`；空串也回退该域 | `{cookies:字符串}` | 网关强制非空 `domain`；仅核对字段，不取凭据 | 10540–10557 |
| 32 | `get_csrf_token` | 无 | `{token:整数}` | 网关自由对象；上游无参，仅核对字段 | 10558–10575 |
| 33 | `can_send_image` | 无 | `{yes:布尔}` | 网关自由对象；上游无参，缺能力回调时 false，不是发送回执 | 7393–7409 |
| 34 | `can_send_record` | 无 | `{yes:布尔}` | 同上，查询的是语音能力 | 7410–7426 |
| 35 | `get_image` | `file="", file_id=""`；至少一个非空字符串，优先 `file` | `{file,url,file_size,file_name}`；大小为字符串，URL 可为空 | 网关只列并强制 `file`，不能单用 `file_id`；未命中缓存报错 | 10472–10487、32906–32922 |
| 36 | `get_record` | `file="", file_id="", out_format?`；同一选择规则；格式限 `mp3/amr/wma/m4a/spx/ogg/wav/flac` | `{file,url,file_size,file_name}`；请求并成功转码时加 `out_format,base64` | 网关强制 `file`、未列 `file_id` 或格式全集；缓存未命中、取源或转码失败不返回成功 | 10488–10517、32923–32934 |
| 37 | `send_qzone_msg` | `content`（非空字符串），`images?`（非空字符串的数组），`ugc_right=1, target_uins?`（正整数数组）；权限限 1/4/16/64/128，16/128 时名单非空 | `{tid:字符串,time:数值}`；缺 tid 走失败，缺时间回落 0 | 网关只列正文。默认 1 所有人可见，4 好友、16 部分好友、64 仅自己、128 部分好友不可见；目录未列删除动作 | 13392–13417、253968–253976、254458–254466 |
| 38 | `set_group_portrait` | `group_id, file`（非空图片来源字符串：路径、URL 或 base64） | `null` | 字段与必填一致；网关说明只列路径/URL | 7001–7009、9109–9117 |
| 39 | `set_group_anonymous` | 无 | `null`（固定成功） | **占位**：不读网关强制的 `group_id, enable`，无匿名开关调用 | 9097–9102 |
| 40 | `delete_friend` | `user_id, block=false` | `null` | **网关禁止**，自由对象不是上游契约；上游有删除实现 | 7734–7745 |
| 41 | `set_group_dismiss` | 不适用 | 无处理器；普通分派未命中为 `failed/1404`，非成功 `data` | **网关禁止；本版上游未暴露**（主名、别名及隐藏名均无）；不可用退群替代 | 16460–16516、16547–16607、16772–16779 |
| 42 | `set_restart` | 无 | **无成功分支**；固定 `failed/100`，`wording="not supported"` | **网关禁止**；上游已注册但明确不支持，非“未暴露” | 6716–6721、10608–10615 |
| 43 | `clean_cache` | 无 | `null`（固定成功） | **网关禁止**；上游占位，无清理调用 | 10616–10623 |
| 44 | `set_online_status` | `status`（非零整数），`ext_status=0, battery_status=100`（均为非负整数） | `null` | **网关禁止**，目录自由对象未描述上述参数；上游有修改实现 | 10747–10759 |

## 反向差集：上游有名字，网关没有目录项

按 L16547–16597 的类别展开 13 个动作数组：**200 个声明主名 + 6 个别名 = 206 个声明注册名**。另加 L16598–16607 的 2 个隐藏分派名，共 208；减去交集 43，差 **165**。下表按**名字**计数，不把别名算成新的能力。这里只说明当前网关目录的边界，不是新增、放行或直接调用建议。

| 上游分组（定义起始行） | 目录外名字数 | 代表性缺项 |
|---|---:|---|
| 信息／好友／请求（7294、7557、9534） | 0 | 这些组的注册名均已覆盖；不代表参数全对齐 |
| 消息／群信息（7444、8299） | 1 + 2 | `send_custom_face`；`get_group_share_link`、`get_group_system_msg` |
| 群管理／群文件（8799、9125） | 11 + 12 | 群标签、批量踢人、成员权限；`upload_group_file`、`upload_private_file`、群文件夹增删改查等 |
| 扩展（10151） | 109 | `fetch_emoji_like`、`get_friend_msg_history`、`friend_poke`、已读、合并转发、群公告等；仅 `fetch_ptt_text` 的别名 `get_ptt_text`、`get_record_text` 也占其中两名 |
| 群相册／空间（12897、13196） | 9 + 9 | 群相册媒体读写；`get_qzone_msg_list`、`get_qzone_msg_detail`、`delete_qzone_msg`、`set_qzone_msg_right`、评论等。**网关有发表，无目录内删除或权限修改** |
| 系统表情／流式接口（16360、13940、14612） | 4 + 6 | 如 `fetch_sys_faces`、`search_sys_faces`；流式文件上传、下载、流临时文件清理等，不等同于 `clean_cache` |
| 隐藏原始入口（16598–16607、16711–16725） | 2 | `.handle_quick_operation` 及其 `_async` 别名，不属于声明式动作组 |

供独立验收定位：`delete_qzone_msg(tid)` 只删本账号空间的指定说说，`tid` 为非空字符串，成功 `data:null`（L13410–13417、254467–254470）。群内单表情读回 `get_emoji_likes(message_id, emoji_id)` 返回 `{emoji_like_list:[{user_id:字符串,nick_name:""}]}`（L11161–11196）；整条消息读回 `get_msg_emoji_likes(message_id)` 返回含 `emoji_id,emoji_type,count,last_reaction_time,users` 的数组（L11288–11339）。两种读回都不能当作 QQ 客户端截图的替代品：汇总从本地回应记录出发，远端用户列表不可用时可回落缓存（L33097–33145）。三者均不在本次网关目录内。

**静态核对的边界：** 名字未收录是网关目录差异，不等于适配器的其它功能完全不能使用该接口；上游注册也不等于该账号的媒体、权限、目标消息或审批事件可用。QQ 端可见效果、真实请求往返、审计记录及禁止项的实际拦截结果，均属于独立动态验收，不在本次结论内。
