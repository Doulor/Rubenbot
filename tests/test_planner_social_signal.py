"""重放六份真实策划请求的独立盲评输出；手动台场景 8 可换部署模型复验。"""

import asyncio
import io
import json
from pathlib import Path

from conftest import ScriptedLLM
from rubenbot.adapters.console import ConsoleAdapter
from rubenbot.core import GUIDANCE_TASK_PROMPT, RubenBot
from rubenbot.persona import Persona
from tools import _e2e_dialog, e2e_live


# 按案例对齐，保留盲评的依据、理由与增量。不是按正文关键词编写的替身分类器。
_RECORDED = (
    ('settled', False, '沉默', '碎片', '', '只是逐时天气播报'),
    ('settled', False, '沉默', '短句', '', '只是签到进度通知'),
    ('settled', False, '沉默', '碎片', '', '只有简讯，没人发问'),
    ('settled', False, '沉默', '碎片', '', '访客在和小夏约见面'),
    ('common', True, '补充新信息', '短句', '先把书装进密封袋，压紧封口再放进帆布包。', '点名问书怎么防水'),
    ('settled', True, '短附和', '碎片', '击掌！恭喜拿证！', '拿证报喜，邀大家击掌'),
)


def test_social_signal_replay(config, store, tmp_path: Path):
    config.chat.bot_name = '小测'
    plans = [json.dumps(dict(basis=basis, speak=speak, intent=intent, length_band=band,
                             increment=increment, reason=reason, avoid_repetition=[]), ensure_ascii=False)
             for basis, speak, intent, band, increment, reason in _RECORDED]
    llm = ScriptedLLM({'fast': plans, 'chat': ['先装进密封袋，压紧袋口。', '击掌！恭喜！']})
    persona = Persona('小测', '你是小测，一个群友。问题有用就回答，需要工具时实际调用，不编造结果。')
    bot = RubenBot(config, store, llm, persona)
    console = ConsoleAdapter(stdout=io.StringIO(), bot_name='小测')
    bot.register_adapter(console)
    console.bind(bot.handle_event)
    ctx = e2e_live.Context(tmp_path, config, bot, console, store)
    asyncio.run(_e2e_dialog.social_signal(ctx))

    calls = [c for c in llm.calls if c['task'] == 'gen_stage1']
    assert len(calls) == 6
    for call, (_, text, _) in zip(calls, _e2e_dialog._SOCIAL_CASES, strict=True):
        prompt = call['messages'][-1]['content']
        assert '有没有值得回应的人和社交信号' in prompt and text in prompt
        assert 'intent=表情回应' not in prompt  # 未启用表情时不建议不可执行的回应。
    for trap in ('天气', '签到', '资讯'):
        assert trap not in GUIDANCE_TASK_PROMPT
