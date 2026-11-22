"""即时原话与事实抽取共用的文本资格判断；账号和受众另由调用方核实。"""

from __future__ import annotations

import re
import unicodedata


from ..privacy.sealing import requests_secrecy

_LEADING_AT = re.compile(r"^(?:@[\w:.-]+\s+)+")
# 不把「我朋友……」「我听人说……」或引述的第一人称当成本人自述。
_INDIRECT = re.compile(
    r"[\"“”「」『』]|(?:朋友|同事|同学|家人|亲戚|室友|女友|男友|对象|同桌|老[婆公板]|"
    r"爸|妈|哥|姐|弟|妹|儿子|女儿|网友|别人|他|她)|听[说說闻聞]|据说|听.{1,8}[说說]|转[述发]|"
    r"(?:有人|群里|别人|[\w一-鿿]{1,8})(?:告诉我|跟我说|对我说)|"
    r"\b(?:he|she|they|friend|wife|husband|colleague|said|says|told|heard|quote[ds]?)\b", re.I,
)
# 只识别常见的「时间/语气 + 我 + 状态/行为」短句，不把「我邻居」的零标记领属当成主语。
_MODIFIER = r'(?:其实|說實話|说实话|本来|原来|刚刚?|最近|平时|经常|一直|已经|特别|最|不太|很|也|还|又|就|只|没|不|正在|目前|终于|今年|去年|这周|[今明昨前][天晚早夜]|今晚|早上|中午|晚上)'
_SELF = re.compile(rf'^(?:{_MODIFIER}[，, ]*)*(?:我|本人)(?:{_MODIFIER})*'
                   r'(?:吃|喝|买|卖|去|来|到|住|搬|在|是|叫|有|要|想|会|能|打算|准备|决定|'
                   r'计划|喜欢|爱|讨厌|偏爱|习惯|擅长|学|看|听|读|写|做|玩|用|拿|穿|带|订|点|选|养|收|跑|睡|工作|上班|下班|参加|负责|考|毕业|换|开始|完成)|'
                   r'^(?:我|本人)的(?:名字|生日|爱好|职业|晚餐|午餐|早餐|手机|电脑|计划|目标|工作)|'
                   r"^(?:i(?:\s+|['’](?:m|ve|ll|d)\b)|my\s+)", re.I)
_QUESTION = re.compile(r"[啥谁誰]|什么|甚麼|怎么|怎样|為什麼|为什么|哪[个里裡兒儿家天种種]|多少|几[点个岁月天]|是不是|有没有|能不能|要不要|\b(?:what|where|when|why|how|which|who)\b", re.I)


def is_owner_self_statement(text: str, *, allow_implicit: bool = False) -> bool:
    """只提升有本人主语、没有转述或保密标记的陈述；看不准的留在原场合。"""
    value = _LEADING_AT.sub('', unicodedata.normalize('NFKC', text).strip()).strip()
    if not value or requests_secrecy(value) or _INDIRECT.search(value):
        return False
    # 「我今晚吃的是鸭腿你知道吗」有陈述；「我今晚吃的啥你知道吗」没有。
    value = re.sub(r"[,，\s]*(?:你知道吗|你知道嗎|你记得吗|你記得嗎)[?？。!！\s]*$", '', value)
    if _QUESTION.search(value) or re.search(r'[?？]|[吗嗎呢么麼][。!！\s]*$', value):
        return False
    if _SELF.match(value):
        return True
    # 仅抽取模型已给出逐条自述证据时，接受「今晚吃了……」式省略主语。
    return bool(allow_implicit and re.match(r'(?:刚刚?|最近|今年|去年|这周|[今明昨前][天晚早夜]|早上|中午|晚上)', value)
                and _SELF.match('我' + value))
