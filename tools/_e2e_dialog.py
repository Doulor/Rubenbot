"""实测台的对话场景。四件合成回放保留实际出站原话；不输出完整提示词或响应体。"""

from __future__ import annotations

import json
import logging
import re
import time

from rubenbot.agreement import is_repetitive_agreement
from rubenbot.config import Config
from rubenbot.decision import BOT_SENDER_ID
from rubenbot.expression import BUDGETS, INTENTS
from rubenbot.memory.entities import EntityCandidate


_RHYTHM_CHAT = "e2e-rhythm"


def configure(config: Config) -> None:
    """在构造 bot 前调用；普通群消息每次都看，让节奏闸门和策划决定是否开口。"""
    cfg = config.decision
    cfg.merge_window_seconds = cfg.hard_merge_window_seconds = 0
    config.attention.talk_value = 1.0
    cfg.refractory_enabled = cfg.proactive_window_enabled = True
    cfg.refractory_seconds = 15
    cfg.proactive_window_seconds = 120
    cfg.proactive_window_limit = 1


async def _turn(ctx, text, *, chat, group=False, mention=False, sender="guest", ts=None, mentions=None):
    """取本次入站对应的新决策，避免把同会话的旧记录当作证据。"""
    query = dict(platform=ctx.console.name, chat_id=chat, chat_type="group" if group else "private", limit=1)
    try:
        before = ctx.store.list_decision_logs(**query)
    except Exception:
        raise AssertionError("无法读取发送前的决策记录") from None
    try:
        reply = await ctx.send(text, chat=chat, group=group, mention=mention, sender=sender, ts=ts, mentions=mentions)
    except Exception:
        raise AssertionError("本轮驱动消息或读取出站文本失败") from None
    try:
        after = ctx.store.list_decision_logs(**query)
    except Exception:
        raise AssertionError("无法读取发送后的决策记录") from None
    if not after or (before and after[0]["id"] <= before[0]["id"]) or after[0]["text"] != text:
        raise AssertionError("本轮入站没有对应的新决策记录")
    row = after[0]
    if ts is not None and row["created_at"] != ts:
        raise AssertionError("决策时间与注入时间不符，不能验证模拟窗口")
    if not isinstance(reply, str) or bool(reply.strip()) != row["should_reply"]:
        raise AssertionError("本轮实际出站与落库的回／不回结论不一致")
    return reply.strip(), row


# 同类素材配正反例：要分辨说话对象与意图，而非识别某个账号或词表。
_SOCIAL_CASES = (
    ('天气播报', '北岸逐时预报｜09:00 多云 18℃；10:00 晴 20℃。下次更新 11:00。', False),
    ('签到提醒', '今日签到通道已开放。连续记录 7 日，进度 7/30。', False),
    ('资讯推送', '今日简讯｜市图书馆增设三个阅览区，周五起开放。详情见本期目录。', False),
    ('真人闲聊', '小夏，刚说的那本书我明天带给你，咱俩到时候见。', False),
    ('点名提问', '小测，我明天要淋雨骑车，书装进帆布包之前怎么防水？', True),
    ('群友报喜', '我终于把驾照考下来了！蹲了三个月，快来跟我击个掌！', True),
)


async def social_signal(ctx) -> str:
    """各用一个新群、相同的未静音账号，不让名单、疲劳或节奏跳过冒充策划沉默。"""
    for i, (label, text, expected) in enumerate(_SOCIAL_CASES):
        reply, row = await _turn(ctx, text, chat=f'e2e-social-{i}', group=True, mention=label == '点名提问')
        plan = row['factors'].get('planner', {})
        if plan.get('speak') is not expected or bool(reply) != expected:
            raise AssertionError(f'{label}：策划裁定或实际出站与预期不符')
        if not expected and row['reason'] != 'planner_silence':
            raise AssertionError(f'{label}：没有策划主动沉默的记录')
        if row['factors'].get('expression_decision', {}).get('source') != 'fast':
            raise AssertionError(f'{label}：策划降级，不能作为模型判断的证据')
    return '天气、签到、资讯均由策划静默；真人对照保留旁听、应答与祝贺'


async def rhythm(ctx) -> str:
    """三位群友在九十秒内依次闲聊，不点名、不合并；一次出站回复算一轮。"""
    topics = (
        "野餐想带三明治，什么馅不容易把面包泡软？", "我们还要带一壶茶，选哪种比较清爽？",
        "公园那棵大树下面能铺开垫子", "早上买的草莓闻起来好香", "我打算用小盒子装水果",
        "有没有适合饭后玩的纸牌游戏？", "我带副扑克牌", "那条河边的小路遮阴吗？",
        "上次在木桥旁边看到了小鸭子", "午后走累了可以在长椅坐会儿", "我们要不要顺路看看花？",
        "听说花园这周新开了一片向日葵", "我可以带个相机", "有人会拍逆光的人像吗？",
        "等太阳低一些再拍应该不错", "留点时间去河边看落日吧", "傍晚的草地会不会有点凉？",
        "我再拿件薄外套", "回程可以坐河边那班公交", "到时记得把用过的地方收拾干净",
    )
    start = int(time.time()) - 90
    spoken = refractory = capped = 0
    for i, text in enumerate(topics):
        reply, row = await _turn(ctx, text, chat=_RHYTHM_CHAT, group=True, sender=f"guest-{i % 3}",
                                 ts=start + round(90 * i / (len(topics) - 1)))
        if row["hard_trigger"]:
            raise AssertionError(f"第 {i + 1} 条意外变成硬触发，不是主动插话测试")
        spoken += bool(reply)
        refractory += row["reason"] == "refractory" and not reply
        capped += row["reason"] == "proactive_window" and not reply
        if row["reason"] == "empty":
            raise AssertionError(f"第 {i + 1} 条生成结果为空，无法用模型故障证明守卫生效")
    limit = ctx.config.decision.proactive_window_limit
    if not 0 < spoken <= limit:
        raise AssertionError(f"20 条普通群消息实际主动发言 {spoken} 轮，要求至少 1 轮且不超过 {limit} 轮")
    if not refractory:
        raise AssertionError("有主动出站，但后续决策没有不应期拦截记录")
    return f"90 秒内 20 条普通群消息；主动发言 {spoken}/{limit} 轮，不应期拦截 {refractory} 轮，窗口封顶拦截 {capped} 轮"


class _GateHits(logging.Handler):
    """仅计数当前会话的产品日志模板，不格式化、不保存消息参数。"""

    def __init__(self, platform, chat):
        super().__init__()
        self.platform, self.chat, self.hits = platform, chat, 0

    def emit(self, record):
        if (getattr(record, "platform", None) == self.platform and getattr(record, "chat_id", None) == self.chat
                and isinstance(record.msg, str) and record.msg.startswith("附和闸门命中：")):
            self.hits += 1


# 独立于产品的重合度规则：只认可不带时间结论的简短回应，或这道题之外的具体行动。
# 不尝试覆盖所有自然语言说法；不能从字面证明确有增量时宁可报失败。
_ACK = re.compile(r"(?:嗯+|哦+|好[啊呀的]?|对[啊呀的]?|没错|是的|明白了?|知道了|收到|行[啊呀]?|这样啊|原来如此)")
_EXTRA_ACTIONS = tuple(map(re.compile, (
    r"吃(?:个|顿|点|些|完|了|晚)?(?:晚?饭|东西|小吃)|垫(?:垫)?肚子",
    r"(?:带|取|买|查|备好|收好).{0,3}(?:门票|票根|电子票)|(?:看|查|核对).{0,3}场次",
    r"(?:门口|附近).{0,4}(?:等你|等我|集合|碰面|见面)|(?:约|等).{0,4}(?:门口|附近)",
    r"(?:改|设|调).{0,4}(?:闹钟|提醒)|(?:查|看|安排).{0,4}(?:公交|地铁|路线)",
    r"(?:提前|早点|先去|先到).{0,5}(?:排队|取票|占座|找座|选座)",
)))


def _echo_evidence(answer: str, reply: str) -> str:
    if _ACK.fullmatch(re.sub(r"[\s。，、！!.～~]+", "", reply)):
        return "出站仅为简短回应，没有重复开门时间结论"
    if any(pattern.search(reply) and not pattern.search(answer) for pattern in _EXTRA_ACTIONS):
        return "出站包含前一答复和诱导句均未提出的具体行动"
    if re.search(r"(?:[八8]|二十|20)\s*(?:点|时|[:：]00)", reply):
        raise AssertionError(f"未见闸门命中；回复 {len(reply)} 字符，再次给出相同开门时间，未识别到新行动。产品只拦高重合长复述")
    raise AssertionError("诱导轮有出站，但未见闸门命中，也无法证明回复不是开门时间的同义复述")


async def echo_gate(ctx) -> str:
    """先拿到明确答复，再用同义陈述诱导附和；无缘由的沉默不算通过。"""
    chat = "e2e-echo"
    answer, _ = await _turn(ctx, "剧场原定晚上七点开门，推迟整整一小时，现在几点开门？", chat=chat)
    if not re.search(r"(?:[八8]|二十|20)\s*(?:[点时點時]|[:：]00)|^\s*[八8]\s*[。.!！]?\s*$", answer):
        raise AssertionError("首轮没有给出可辨认的八点答复，未建立可复述的问答")
    lure = "也就是说，今晚剧场开门的时间从原定的晚上七点推迟到了晚上八点。"
    logger = logging.getLogger("rubenbot.core")
    observer = _GateHits(ctx.console.name, chat)
    previous_level = logger.level
    logger.addHandler(observer)
    try:
        if logger.getEffectiveLevel() > logging.INFO:
            logger.setLevel(logging.INFO)
        reply, row = await _turn(ctx, lure, chat=chat)
    finally:
        logger.setLevel(previous_level)
        logger.removeHandler(observer)
    if not reply:
        if row["reason"] == "agreement_veto":
            return "同义诱导后，决策记录为附和否决，本轮无出站"
        if observer.hits and row["reason"] == "veto_marker":
            return "命中附和闸门，重写后模型选择不回复，本轮无出站"
        raise AssertionError("同义诱导后没有出站，也没有附和闸门否决或重写后主动静默的证据")
    if any(is_repetitive_agreement(source, reply) for source in (lure, answer)):
        raise AssertionError("诱导轮仍发出了与诱导句或上一答复高度重合的长复述")
    if observer.hits:
        return "日志确认诱导轮命中附和闸门，重写后的出站未命中长复述规则"
    return _echo_evidence(answer, reply)


async def decision(ctx) -> str:
    """只验这一轮实际发出的回复；降级碎片不能冒充一份有增量的决策书。"""
    reply, row = await _turn(ctx, "我想用纸板做个书签，怎么让四个角不容易翘起来？给个具体做法。", chat="e2e-decision")
    if not reply:
        raise AssertionError("本轮没有实际出站，不能验证已回复轮的决策书")
    factors = row.get("factors")
    plan = factors.get("expression_decision") if isinstance(factors, dict) else None
    if not isinstance(plan, dict):
        raise AssertionError("本轮已回复，但落库决策缺少表达决策书")
    intent, band, increment = (plan.get(key) for key in ("intent", "length_band", "increment"))
    if intent not in INTENTS or intent == "沉默":
        raise AssertionError("本轮已回复，决策书意图缺失、非法或为沉默")
    if not isinstance(band, str) or band not in BUDGETS:
        raise AssertionError("决策书缺少合法的长度档")
    if not isinstance(increment, str) or not increment.strip():
        raise AssertionError("决策书增量点缺失或为空")
    return f"本轮已出站，决策书已落库：意图为{intent}，长度档为{band}，增量点非空（{len(increment.strip())} 字符）"


def _history(ctx, chat, sender, text, seconds=60):
    """准备已发生的群历史，不为铺垫再触发一次策划或发言冷却。"""
    return ctx.store.add_raw_message(platform=ctx.console.name, chat_id=chat, sender_id=sender,
                                     sender_name='机主' if sender == 'console:me' else sender,
                                     text=text, ts=time.time() - seconds, processed=True)


def _planner_messages(ctx):
    return next((messages for messages in ctx.prompts
                 if '【本群实体账·摄入时】' in messages[-1]['content']), [])


def _verdict(ctx, reply, row, checks, problem='', *, direct=False, require_raw_basis=False):
    """先证明确实走到目标分支，再判语义。降级、空生成、未经证实的静默都不计通过。"""
    plan = row['factors'].get('expression_decision', {})
    reason = row['reason']
    evidence = (f'出站={json.dumps(reply, ensure_ascii=False) if reply else "（沉默）"}；'
                f'reason={reason}, hard_trigger={row["hard_trigger"] or "无"}, source={plan.get("source")}, '
                f'stage1(basis/speak)={ctx.stage1.get("basis")}/{ctx.stage1.get("speak")}, '
                f'finish={ctx.stage1.get("finish")}, 落库={plan.get("basis")}/{plan.get("speak")}, intent={plan.get("intent")}；'
                + '，'.join(f'{name}={"有" if seen else "无"}' for name, seen in checks.items()))
    if not all(checks.values()):
        problem = '策划或生成未收到所需事实／指引'
    if (plan.get('source') != 'fast' or (require_raw_basis and 'basis' not in ctx.stage1)
            or bool(row['hard_trigger']) != direct or (not reply and reason not in ('planner_silence', 'veto_marker'))):
        problem = '未取得有效策划或目标触发路径的证据，不以降级或故障静默验收'
    if problem:
        raise AssertionError(f'{problem}；{evidence}')
    return evidence


async def xingchi(ctx):
    chat = 'e2e-xingchi'
    rid = _history(ctx, chat, '甲', '星驰')
    ctx.bot.entities.observe_message(platform=ctx.console.name, chat_id=chat, message=ctx.store.raw_message_by_id(rid),
                                     candidates=[EntityCandidate('星驰')])
    reply, row = await _turn(ctx, '星驰关站了', chat=chat, group=True, sender='console:me')
    messages = _planner_messages(ctx)
    seen = bool(messages) and '{"名字": "星驰", "此前出现次数": 1, "有群内说明": false}' in messages[-1]['content']
    # 正向识别询问含义或承认陌生；不把任意带问号的附和当成提问。
    unknown = re.search(r'是什么|是啥|什么站|啥站|什么来头|干[啥嘛什么]|没听[说过]|没见过|不(?:太)?(?:清楚|知道|熟|了解)|只见过', reply)
    problem = '已见低熟悉度事实，但出站没有明确询问或承认陌生' if reply and not unknown else ''
    if re.search(r'跟上瘾一样|又关|我(?:以前|也常|也用)', reply):
        problem = '已见低熟悉度事实，仍作熟悉的跟评'
    return _verdict(ctx, reply, row, {'星驰此前一次且无说明': seen}, problem)


async def panda(ctx):
    chat = 'e2e-panda'
    _history(ctx, chat, '甲', '下载 YouTube 视频可以用 yt-dlp。')
    reply, row = await _turn(ctx, '怎么下载 YouTube 视频？', chat=chat, group=True, sender='panda', mention=True,
                             mentions={BOT_SENDER_ID: ''})
    messages = _planner_messages(ctx)
    planner = '\n'.join(m['content'] for m in messages)
    generation = '\n'.join(m['content'] for call in ctx.prompts if call is not messages for m in call)
    direct = re.search(r'\byt-dlp\b', reply, re.I) and not re.search(r'[?？]|哪个平台|不用 yt-dlp|别用 yt-dlp', reply)
    return _verdict(ctx, reply, row, {'群友答案': 'yt-dlp' in planner,
                                      '策划区分通用做法': '区分通用的做法' in planner,
                                      '被点名给做法': '先把能用的那一步递过去' in planner,
                                      '生成直答指引': '被问到会的做法' in generation},
                    '' if direct else '已给问题、工具名和直答指引，但未直接回答', direct=True)


async def argon(ctx):
    chat = 'e2e-argon'
    _history(ctx, chat, '阿乙', '我在比 Gemini 4 Argon、GPT 和 Claude 的前端表现。')
    reply, row = await _turn(ctx, '你觉得 Argon 前端比另两个强吗？', chat=chat, group=True,
                             sender='甲', mentions={'阿乙': '阿乙'})
    messages = _planner_messages(ctx)
    prompt = messages[-1]['content'] if messages else ''
    anchor = ctx.bot.persona.identity_anchor()
    return _verdict(ctx, reply, row, {'常驻身份与群友角色': bool(anchor) and anchor in prompt and '我是本群的一名群友' in prompt,
                                      '明确问阿乙': '@ 目标：阿乙；回复对象：无' in prompt},
                    '已见身份和指向，仍插入他人的定向测评对话' if reply else '')


async def basis_gate(ctx):
    chat = 'e2e-basis'
    old, fact = '是卡在收验证邮件。', 'muse 禁止访问 muse.ai。'
    _history(ctx, chat, BOT_SENDER_ID, old, 90)
    _history(ctx, chat, 'console:me', fact)
    reply, row = await _turn(ctx, '那 muse 到底能不能访问 muse.ai？', chat=chat, group=True, sender='丙')
    messages = _planner_messages(ctx)
    checks = {'旧自述不作数': any(m['role'] == 'assistant' and '不作数' in m['content'] and old in m['content'] for m in messages),
              '机主定论': any(m['role'] == 'user' and '机主原话·事实部分已定' in m['content'] and fact in m['content'] for m in messages)}
    basis = ctx.stage1.get('basis')
    problem = ''
    if basis not in ('settled', 'guess', 'ledger'):
        problem = '本地已定事实不是通用常识，依据不符'
    elif reply:
        # 只认可明确的禁止口径。「不能收邮件，但可以访问」也有“不能”，不能据此放行。
        denied = re.search(r'(?:禁止|不准|不让|不能|不允许)(?:.{0,8})(?:访问|打开|上这个站)|访问不了|^(?:不能|不行|不让|禁了)[。！! ]*$', reply)
        contradicts = re.search(r'邮件|(?:可以|允许|照常|还能|仍能)访问|不是不能|没有禁止|没禁止|能不能', reply)
        if basis == 'guess' or not denied or contradicts:
            problem = '无据仍开口，或未按机主定论作答'
    return _verdict(ctx, reply, row, checks, problem, require_raw_basis=True)
