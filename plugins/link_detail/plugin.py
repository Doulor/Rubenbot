"""群内作品详情：确定性读取与回复，不调用模型，也不下载视频。"""

import asyncio
import base64
import logging
import time
from collections import OrderedDict
from functools import partial

import yaml

from rubenbot.adapters.base import Segment
from rubenbot.decision import BOT_SENDER_ID
from rubenbot.linkparse.fetch import FetchError
from rubenbot.plugins.config_schema import effective_values, normalize_schema, validate_values

from .routing import NAMES, allowed, extractor, identify, links

logger = logging.getLogger(__name__)


class LinkDetail:
    def __init__(self, ctx):
        self.ctx = ctx
        self.schema = normalize_schema(ctx.plugin.manifest.config_schema)
        self.recent = OrderedDict()
        self.pending = {}
        self.slots = asyncio.Semaphore(6)
        self.load()

    def configure(self, raw):
        values, errors = validate_values(self.schema, raw if isinstance(raw, dict) else {})
        if errors:
            logger.warning("链接详情忽略不合规配置项：%s", [e['field'] for e in errors])
        self.options = effective_values(self.schema, values)
        self.enabled = [k for k in NAMES if self.options[f"enable_{k}"]]
        self.recent.clear()

    def load(self):
        path = self.ctx.plugin.path / "config.yaml"
        self.configure(yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {})

    async def preview(self, key, link):
        read = partial(self.ctx.fetch_public_page, allow_url=partial(allowed, key))
        try:
            async with asyncio.timeout(14):
                detail = await extractor(key).extract(key, link.normalized, read)
            if detail is not None:
                detail.url = link.url
                text = detail.render(NAMES[key])
                if text:
                    segments = []
                    if detail.cover:
                        try:
                            image = await self.ctx.fetch_public_cover(detail.cover)
                            segments.append(Segment("image", {"file": "base64://" + base64.b64encode(image).decode("ascii")}))
                        except FetchError:
                            text += "\n（封面抓不到）"
                    return [*segments, Segment.text(text)]
        except (FetchError, TimeoutError):
            pass
        except Exception as exc:
            # 不把网页内容、带签名的地址或外部异常正文贴进群或日志。
            logger.warning("%s 详情提取失败：%s", key, type(exc).__name__)
        return [Segment.text(f"【{NAMES[key]}】抓不到这条链接的公开详情。\n{link.url}")]

    async def deliver(self, adapter, event, key, link):
        token = (event.platform, event.chat_id, link.normalized)
        if self.recent.get(token, 0) > time.monotonic():
            return True
        if token in self.pending:
            return await asyncio.shield(self.pending[token])
        # 无排队容量时让通用层照常工作；不为不断涌入的新 URL 建无界等待队列。
        if len(self.pending) >= 32:
            return False
        done = self.pending[token] = asyncio.get_running_loop().create_future()
        sent = False
        try:
            async with self.slots:
                segments = await self.preview(key, link)
                if event.platform_msg_id:
                    segments.insert(0, Segment.reply(event.platform_msg_id))
                await adapter.send(event.chat_id, segments, chat_type=event.chat_type)
            sent = True
            if ttl := self.options['cooldown_seconds']:
                self.recent[token] = time.monotonic() + ttl
                self.recent.move_to_end(token)
                if len(self.recent) > 1024:
                    self.recent.popitem(last=False)
        except Exception as exc:
            logger.warning("%s 详情未送达：%s", key, type(exc).__name__)
        finally:
            self.pending.pop(token)
            done.set_result(sent)
        return sent

    async def on_message(self, runtime, event):
        if not event.is_group or not all(self.ctx.allowed(p) for p in ('read_messages', 'send_messages', 'network')):
            return
        adapter = runtime.adapters.get(event.platform)
        if adapter is None or event.sender_id in (BOT_SENDER_ID, getattr(adapter, 'self_id', None)):
            return
        seen = list(links(event.text))
        selected = {}
        for link in seen:
            if key := identify(link.normalized, self.enabled):
                selected.setdefault(link.normalized, (key, link))
                if len(selected) == self.options['max_links']:
                    break
        if not selected:
            return
        delivered = await asyncio.gather(*(self.deliver(adapter, event, k, link) for k, link in selected.values()))
        owned = {url for url, ok in zip(selected, delivered) if ok}
        if not owned:
            return
        if all(link.normalized in owned for link in seen):
            return False
        # 流水在分发插件前已按原文落库。混合消息仅遮掉本轮已回执链接，
        # 让其余网址按原顺序进入 core 的预取、合批和策划，不修改平台原始事件。
        parts, start = [], 0
        for link in seen:
            if link.normalized in owned:
                parts.extend((event.text[start:link.start], "〔已回复链接详情〕"))
                start = link.end
        event.text = "".join([*parts, event.text[start:]])


def register(ctx):
    plugin = LinkDetail(ctx)
    ctx.on_message(plugin.on_message)
    ctx.on_load(lambda context, manifest: plugin.load())
    ctx.on_config_change(lambda context, old, new: plugin.configure(new))
