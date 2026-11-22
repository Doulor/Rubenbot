"""离线验证实测台的布景、证据和判分，不把模拟回包作为真实回放结果。"""
import asyncio
import json

import httpx
import pytest

from tools import e2e_live as live


@pytest.mark.parametrize('number,basis,speak,reply,error', [
    (9, 'guess', False, '', None),
    (9, 'settled', True, '我也用过，上瘾一样', '仍作熟悉的跟评'),
    (10, 'common', True, '用 yt-dlp 加视频链接。', None),
    (10, 'common', True, '搬去哪个平台啊？', '未直接回答'),
    (10, 'common', None, '用 yt-dlp。', None),  # 策划日志有效且实际直答，不因原始 speak 缺报误判绕答。
    (11, 'guess', False, '', None),
    (11, 'common', True, '前端强就够了', '仍插入'),
    (12, 'settled', True, '不能访问。', None),
    (12, 'guess', True, '', None),  # 模型原本想说，依据门实际压住。
    (12, 'ledger', True, '就是卡在收验证邮件。', '未按机主定论'),
    (12, 'settled', True, '不是不能访问，是可以访问。', '未按机主定论'),
    (12, None, False, '', '未取得有效策划'),  # 模型漏报不能冒充自报 guess。
])
def test_replay(tmp_path, monkeypatch, number, basis, speak, reply, error):
    def respond(request):
        payload = json.loads(request.content)
        plan = {'intent': '补充新信息' if speak is not False else '沉默', 'increment': '说一点有用的信息'}
        if speak is not None:
            plan['speak'] = speak
        if basis:
            plan['basis'] = basis
        text = json.dumps(plan) if 'response_format' in payload else reply
        return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': text}}]})

    client_class = live.LLMClient
    monkeypatch.setattr(live, 'LLMClient', lambda cfg, **kw: client_class(cfg, transport=httpx.MockTransport(respond), **kw))

    async def run():
        cfg = live.LLMConfig(base_url='https://relay.invalid/v1', chat_model=live.MODEL)
        async with live.instance(tmp_path, cfg, number) as ctx:
            scenario = live.SCENARIOS[number - 1][1]
            if error:
                with pytest.raises(AssertionError, match=error) as exc:
                    await scenario(ctx)
                assert '出站=' in str(exc.value)
            else:
                evidence = await scenario(ctx)
                assert '出站=' in evidence and 'source=fast' in evidence
                if basis == 'guess' and speak:
                    assert 'guess/True' in evidence and '落库=guess/False' in evidence

    asyncio.run(run())
