#!/usr/bin/env python3
"""手动实测（会产生模型费用）：在 worktree 中用项目虚拟环境运行 python tools/e2e_live.py。

优先用 --config CONFIG 或工作目录的 config.yaml（只取 llm 段，支持 ${ENV}）。
也可一起提供 RUBENBOT_BASE_URL、RUBENBOT_API_KEY。没有前两者时，仅在本机
Claude Code 的 settings.json 配的是计划中的同一模型时使用其中成对的中转地址与令牌。
不读取线上库、人格、聊天或插件配置，不落盘真实凭据、提示词或工具输出。
"""
from __future__ import annotations

import argparse
import asyncio
import io
import ipaddress
import json
import logging
import os
import random
import sys
import tempfile
import time
from collections import Counter
from contextlib import AsyncExitStack, asynccontextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # 共享虚拟环境可能 editable 安装的是另一个 worktree。
# 上面的 sys.path 注入必须先于项目内导入，因此这里只能放在顶部导入之后（E402）
from rubenbot.adapters.base import render_segments  # noqa: E402
from rubenbot.adapters.console import ConsoleAdapter  # noqa: E402
from rubenbot.config import Config, LLMConfig  # noqa: E402
from rubenbot.core import RubenBot  # noqa: E402
from rubenbot.llm import LLMClient, make_usage_recorder  # noqa: E402
from rubenbot.memory.store import MemoryStore  # noqa: E402
from rubenbot.persona import Persona  # noqa: E402
from rubenbot.plugins.loader import PluginManager  # noqa: E402
from tools import _e2e_computer, _e2e_dialog, _e2e_fixtures  # noqa: E402

MODEL = 'workbuddy-deepseek-v4.1-flash'
REPORT = Path.home() / 'workspace/rubenbot-night/e2e-results.md'
SCENARIOS = (
    ('节奏守卫', _e2e_dialog.rhythm), ('附和闸门', _e2e_dialog.echo_gate),
    ('MCP 真调用', _e2e_fixtures.mcp), ('访客沙盒', _e2e_computer.sandbox),
    ('白名单真机', _e2e_computer.host), ('链接插件', _e2e_fixtures.link),
    ('决策书落盘', _e2e_dialog.decision),
)


def relay_config(path: Path | None) -> LLMConfig:
    if path is not None or (ROOT / 'config.yaml').is_file():
        path = path or ROOT / 'config.yaml'
        if not path.is_file():
            raise AssertionError('指定的中转配置文件不存在')
        raw = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
        section = raw.get('llm', {})
        if not section.get('base_url'):
            raise AssertionError('配置缺少 llm.base_url')
        llm = Config.from_raw({'llm': section}).llm
    elif 'RUBENBOT_BASE_URL' in os.environ or 'RUBENBOT_API_KEY' in os.environ:
        llm = LLMConfig(base_url=os.getenv('RUBENBOT_BASE_URL', ''),
                        api_key=os.getenv('RUBENBOT_API_KEY', ''))
    else:
        settings = Path.home() / '.claude/settings.json'
        env = json.loads(settings.read_text()).get('env', {}) if settings.is_file() else {}
        if env.get('ANTHROPIC_MODEL') != MODEL:
            raise AssertionError('未找到计划模型的中转；请用 --config 指定项目配置')
        url = env.get('ANTHROPIC_BASE_URL', '').rstrip('/')
        if urlsplit(url).path not in ('', '/v1'):
            raise AssertionError('不能从该中转路径推断 OpenAI 兼容入口，请显式配置')
        llm = LLMConfig(base_url=url if url.endswith('/v1') else url + '/v1',
                        api_key=env.get('ANTHROPIC_AUTH_TOKEN', ''))
    parts = urlsplit(llm.base_url)
    if not llm.api_key or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise AssertionError('中转地址或凭据不完整')
    if parts.scheme != 'https' and not (parts.scheme == 'http' and parts.hostname in ('localhost', '127.0.0.1', '::1')):
        raise AssertionError('非本机中转必须使用 HTTPS')
    # 所有角色都走方案指定的模型，避免无意调用配置里的其他付费模型。
    llm.models = {'chat': MODEL, 'fast': MODEL}
    llm.chat_model = MODEL
    llm.gate_model = llm.extract_model = llm.consolidate_model = None
    llm.max_retries = 0
    return llm


def normalize_no_proxy():
    """httpx 0.28 会为 IPv6 再加一层括号；仅去重已有的等价 IP，不变更代理路由。"""
    for key in ('NO_PROXY', 'no_proxy'):
        if key not in os.environ:
            continue
        items = []
        for item in os.environ[key].split(','):
            item = item.strip()
            if item.startswith('[') and item.endswith(']'):
                try:
                    item = str(ipaddress.IPv6Address(item[1:-1]))
                except ValueError:
                    pass
            items.append(item)
        os.environ[key] = ','.join(dict.fromkeys(items))


@dataclass
class Context:
    root: Path
    config: Config
    bot: RubenBot
    console: ConsoleAdapter
    store: MemoryStore
    prompts: list = field(default_factory=list)
    statuses: list[int] = field(default_factory=list)

    async def request(self, req):
        if req.url.path.endswith('/chat/completions'):
            self.prompts.append(json.loads(req.content)['messages'])

    async def response(self, resp):
        self.statuses.append(resp.status_code)

    async def send(self, text, *, chat='e2e', sender='guest', group=False, mention=False, ts=None):
        event = self.console.parse_line(f'[{chat}|访客] {text}' if group else text)
        event.chat_id, event.sender_id = chat, sender
        event.sender_name = '机主' if sender == 'console:me' else sender
        event.mentions_bot = mention or not group
        start = len(self.console.sent)
        # 入站、合并、决策、发送使用同一个时间；不改变 asyncio、HTTP 或 MCP 的时钟。
        clock = SimpleNamespace(time=lambda: ts, localtime=time.localtime, strftime=time.strftime)
        with patch('rubenbot.core.time', clock) if ts is not None else nullcontext():
            await self.console.dispatch(event)
        return '\n'.join(render_segments(segments) for cid, _, segments in self.console.sent[start:] if cid == chat)


@asynccontextmanager
async def instance(root, llm_cfg, scenario):
    path = root / 'config.yaml'
    path.write_text(yaml.safe_dump({'adapters': {'console': {}}, 'database_path': str(root / 'e2e.sqlite'),
                                   'webui': {'enabled': False}, 'llm': {'chat_model': MODEL}}))
    cfg = Config.load(path)
    cfg.llm = llm_cfg
    cfg.chat.bot_name = '小测'
    cfg.chat.type_speed = 0
    cfg.chat.split_max_parts = 1
    cfg.decision.merge_window_seconds = cfg.decision.hard_merge_window_seconds = cfg.decision.urgent_seconds = 0
    cfg.memory.recall_tool_max_rounds = 4
    cfg.memory.recall_tool_enabled = False
    cfg.reminder.enabled = False
    cfg.stickers.dir = str(root / 'stickers')
    cfg.computer.enabled = scenario in (4, 5)
    _e2e_dialog.configure(cfg)
    _e2e_fixtures.configure(cfg, root, mcp=scenario == 3, link=scenario == 6)
    async with AsyncExitStack() as stack:
        store = MemoryStore(cfg.database_path)
        stack.callback(store.close)
        llm = LLMClient(cfg.llm, usage_recorder=make_usage_recorder(store))
        stack.push_async_callback(llm.close)
        plugins = PluginManager(cfg, store)
        plugins.load_all()
        bot = RubenBot(cfg, store, llm, Persona('小测', '你是小测，一个群友。问题有用就回答，需要工具时实际调用，不编造结果。'),
                       plugins=plugins, rng=random.Random(17))
        console = ConsoleAdapter(stdout=io.StringIO(), bot_name='小测')
        bot.register_adapter(console)
        console.bind(bot.handle_event)
        ctx = Context(root, cfg, bot, console, store)
        client = await llm._get_client()
        client.event_hooks['request'].append(ctx.request)
        client.event_hooks['response'].append(ctx.response)
        stack.push_async_callback(bot.shutdown)
        stack.push_async_callback(bot.mcp.stop)
        await bot.mcp.start()
        yield ctx


@dataclass
class Result:
    name: str
    passed: bool
    evidence: str
    reason: str = '—'

    def line(self):
        return f"{'PASS' if self.passed else 'FAIL'} {self.name}：{self.evidence}" + ('' if self.passed else f'；{self.reason}')


def safe_reason(exc):
    # 仅自测断言由我们控制。上游错误体、工具参数、异常 repr 均不得进入报告。
    origin = exc.__traceback__
    while origin and origin.tb_next:
        origin = origin.tb_next
    if isinstance(exc, AssertionError) and exc.args and origin:
        module = origin.tb_frame.f_globals.get('__name__')
        if module in {__name__, _e2e_computer.__name__, _e2e_dialog.__name__, _e2e_fixtures.__name__}:
            return str(exc)
    if isinstance(exc, TimeoutError):
        return '场景超时'
    return f'异常 {type(exc).__name__}（未记录原始错误正文）'


def save_report(path, results):
    passed = sum(r.passed for r in results)
    summary = f'{len(results)} 项，{passed} PASS，{len(results) - passed} FAIL'
    lines = ['# E2E 实测结果', '', f'运行：{datetime.now().astimezone().isoformat(timespec="seconds")}；计划模型：`{MODEL}`。',
             '', '| 场景 | 结论 | 证据摘要 | 失败原因 |', '|---|---|---|---|']
    for r in results:
        cells = (r.name, 'PASS' if r.passed else 'FAIL', r.evidence, r.reason)
        lines.append('| ' + ' | '.join(c.replace('|', r'\|').replace('\n', ' ') for c in cells) + ' |')
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, encoding='utf-8', delete=False) as f:
        tmp = Path(f.name)
        f.write('\n'.join([*lines, '', '**汇总：' + summary + '**', '']))
    try:
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)
    print(f'汇总：{summary}。报告：{path}', flush=True)


async def run(config_path, report, selected):
    results = []
    scenarios = [(i, name, fn) for i, (name, fn) in enumerate(SCENARIOS, 1) if i in selected]
    def record(result):
        print(result.line(), flush=True)
        results.append(result)
    try:
        normalize_no_proxy()
        llm_cfg = relay_config(config_path)
    except Exception as exc:
        for _, name, _ in scenarios:
            record(Result(name, False, '未进入场景', safe_reason(exc)))
    else:
        # 每项独立建库、装配、清理，单项复跑不受上一场景的记忆、随机数和工具清单影响。
        for i, name, fn in scenarios:
            ctx = None
            try:
                with tempfile.TemporaryDirectory(prefix='bot-e2e-') as tmp:
                    async with instance(Path(tmp), llm_cfg, i) as ctx, asyncio.timeout(240):
                        evidence = await fn(ctx)
                        if not any(200 <= s < 300 for s in ctx.statuses):
                            raise AssertionError('没有真实中转成功响应')
                record(Result(name, True, evidence))
            except Exception as exc:
                evidence = '未进入场景'
                if ctx:
                    codes = ', '.join(f'{s}×{n}' for s, n in sorted(Counter(ctx.statuses).items())) or '无'
                    evidence = f'中转请求 {len(ctx.prompts)} 次，HTTP 响应 {codes}'
                record(Result(name, False, evidence, safe_reason(exc)))
    save_report(report, results)
    return int(not all(r.passed for r in results))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--config', type=Path, help='仅取项目 YAML 的 llm 段')
    parser.add_argument('--results', type=Path, default=REPORT, help='Markdown 报告路径')
    parser.add_argument('--only', nargs='+', type=int, choices=range(1, 8), help='诊断时只跑指定场景，需另指定报告')
    args = parser.parse_args()
    if args.only and args.results.resolve() == REPORT.resolve():
        parser.error('单项诊断请用 --results 指定另一份报告，以免覆盖完整结果')
    if not __debug__:
        parser.error('不能使用 -O：会禁用场景断言')
    logging.getLogger().addHandler(logging.NullHandler())
    return asyncio.run(run(args.config, args.results, args.only or range(1, 8)))


if __name__ == '__main__':
    sys.exit(main())
