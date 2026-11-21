"""表情包改造（2026-11-21）新增行为的固定测试。

覆盖五步改法里新增的部分：
1. 收集存本体：文件头嗅探、大小上限、URL 下载、失败退回纯引用；
2. 存量回填工具：URL 优先、get_image 兜底、成功/失败/过期计数；
3. 描述字段：向后兼容、vision 未配置不调用、配置后后台写回；
4. 两级选择：有本地文件才参选、embedding 阈值、Fast 角色编号选择、
   解析失败回落标签匹配、发送成功后 uses+1；
5. WebUI 列表带 description / emotions / uses。
"""

from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace

import pytest

from conftest import ScriptedLLM
from rubenbot.adapters.base import MessageEvent, Segment
from rubenbot.config import Config
from rubenbot.memory.store import MemoryStore
from rubenbot.sticker_behavior import MAX_SELECT_CANDIDATES, StickerBehavior
from rubenbot.stickers import Sticker, StickerStore, sniff_image_suffix
from tools import backfill_sticker_files as backfill_tool
from tools.backfill_sticker_files import Attempt, backfill

PNG = b"\x89PNG\r\n\x1a\n"
JPG = b"\xff\xd8\xff\xe0"
GIF = b"GIF89a"
WEBP = b"RIFF\x00\x00\x00\x00WEBP"


# ----------------------------------------------------------------------
# 夹具与假件
# ----------------------------------------------------------------------


def make_store(tmp_path) -> StickerStore:
    return StickerStore(tmp_path / "stickers")


class FixedRng:
    def random(self):
        return 0.0

    def choice(self, values):
        return values[0]


class Clock:
    def __call__(self):
        return 1000.0


def event(text="你好", *, chat_id="g1", chat_type="group", segments=None):
    return MessageEvent(
        platform="snowluma", chat_type=chat_type, chat_id=chat_id,
        sender_id="u1", sender_name="阿花", text=text, segments=list(segments or []),
    )


class Bot:
    def __init__(self, store, adapters=None, *, config=None, llm=None):
        self.stickers = store
        self.adapters = {item.name: item for item in (adapters or [StickerAdapter()])}
        self.config = config or Config()
        self.llm = llm
        self.calls = []

    async def call_capability(self, adapter, name, **kwargs):
        self.calls.append((adapter, name, kwargs))
        return True, "ok"


class StickerAdapter:
    name = "snowluma"
    capabilities = frozenset({"send_sticker"})
    capability_config_keys = {"send_sticker": "sticker"}
    capability_defaults = {}


def behavior(store, *, downloader=None, embed_fn=None, **values) -> StickerBehavior:
    cfg = SimpleNamespace(
        enabled=True, collect=True, send_probability=1.0, cooldown_seconds=300.0,
        max_file_bytes=8 * 1024 * 1024, embedding_threshold=0.3, embedding_refresh_seconds=0.0,
        embedding_base_url="", embedding_model="",
    )
    for key, value in values.items():
        setattr(cfg, key, value)
    return StickerBehavior(
        cfg, store_getter=lambda: store, rng=FixedRng(), clock=Clock(),
        downloader=downloader or _no_download, embed_fn=embed_fn,
    )


async def _no_download(url, limit):
    return None


def runtime(bot, store: MemoryStore | None = None):
    return SimpleNamespace(extras={"bot": bot}, config=bot.config, store=store,
                           llm=getattr(bot, "llm", None))


def collect(behavior, bot, *, text="看图", segments, store=None):
    asyncio.run(behavior.on_message(runtime(bot, store), event(text, segments=segments)))


def local_sticker(store, payload=b"cat", *, tags=("猫猫",), description="猫咪大笑", emotions=("开心",)):
    sticker = store.add(PNG + payload, tags=tags)
    if description or emotions:
        store.update_metadata(sticker.id, description=description, emotions=emotions)
    return store.get(sticker.id)


# ----------------------------------------------------------------------
# 1. 收集存本体
# ----------------------------------------------------------------------


@pytest.mark.parametrize("raw, expected", [
    (PNG + b"x", ".png"), (JPG + b"x", ".jpg"), (GIF + b"x", ".gif"),
    (WEBP + b"x", ".webp"), (b"not-an-image", None), (b"", None), (None, None),
])
def test_sniff_image_suffix(raw, expected):
    assert sniff_image_suffix(raw) == expected


def test_add_rejects_unknown_format_but_keeps_ref(tmp_path):
    store = make_store(tmp_path)
    sticker = store.add(b"not-an-image", native_refs={"snowluma": {"kind": "image", "data": {"file": "a"}}})
    assert sticker.file == "" and store.path_of(sticker) is None
    with pytest.raises(ValueError):
        store.add(b"not-an-image")


def test_add_rejects_oversized_image(tmp_path):
    store = make_store(tmp_path)
    assert len(store.find_by_tags(None)) == 0
    with pytest.raises(ValueError):
        store.add(PNG + b"x" * 100, max_file_bytes=50)


def test_add_upgrades_ref_only_entry_with_bytes(tmp_path):
    store = make_store(tmp_path)
    ref = {"kind": "image", "data": {"file": "abc.jpg"}}
    ref_only = store.add(native_refs={"snowluma": ref})
    upgraded = store.add(PNG + b"same", native_refs={"snowluma": ref})
    assert upgraded.id == ref_only.id
    assert upgraded.file and store.path_of(upgraded) is not None
    assert len(store.find_by_tags(None)) == 1


def test_collect_downloads_url_and_stores_file(tmp_path):
    store = make_store(tmp_path)
    seen: list[str] = []

    async def downloader(url, limit):
        seen.append(url)
        return JPG + b"downloaded"

    segment = Segment("image", {"file": "a.jpg", "url": "https://cdn.example/a.jpg", "summary": "[动画表情]"})
    collect(behavior(store, downloader=downloader), Bot(store), segments=[segment])
    item = store.find_by_tags(None)[0]
    assert seen == ["https://cdn.example/a.jpg"]
    assert item.file.endswith(".jpg") and store.path_of(item) is not None
    assert item.native_refs["snowluma"]["data"]["url"] == "https://cdn.example/a.jpg"


def test_collect_download_failure_keeps_ref_only(tmp_path, caplog):
    store = make_store(tmp_path)

    async def broken(url, limit):
        raise RuntimeError("网络断了")

    segment = Segment("image", {"url": "https://cdn.example/gone.jpg", "summary": "[动画表情]"})
    with caplog.at_level("WARNING"):
        collect(behavior(store, downloader=broken), Bot(store), segments=[segment])
    item = store.find_by_tags(None)[0]
    assert item.file == ""
    assert "下载失败" in caplog.text


def test_collect_download_oversize_keeps_ref_only(tmp_path):
    store = make_store(tmp_path)

    async def huge(url, limit):
        return PNG + b"x" * (limit + 1)

    segment = Segment("image", {"url": "https://cdn.example/huge.png", "summary": "[动画表情]"})
    collect(behavior(store, downloader=huge), Bot(store), segments=[segment])
    assert store.find_by_tags(None)[0].file == ""


def test_collect_mface_native_url_is_downloaded(tmp_path):
    """mface 段的 data.url 是 QQ 多媒体地址：收集时也当场下载。"""
    store = make_store(tmp_path)

    async def downloader(url, limit):
        assert url.startswith("https://multimedia.nt.qq.com.cn/")
        return GIF + b"mface"

    segment = Segment.sticker(native_ref={
        "kind": "mface", "data": {"emoji_id": "1", "url": "https://multimedia.nt.qq.com.cn/a.gif"},
    })
    collect(behavior(store, downloader=downloader), Bot(store), segments=[segment])
    item = store.find_by_tags(None)[0]
    assert item.file.endswith(".gif")
    assert item.native_refs["snowluma"]["kind"] == "mface"


def test_collect_base64_first_skips_download(tmp_path):
    store = make_store(tmp_path)
    seen: list[str] = []

    async def downloader(url, limit):
        seen.append(url)
        return PNG

    segment = Segment("image", {
        "base64": base64.b64encode(PNG + b"inline").decode(),
        "url": "https://cdn.example/a.png",
        "summary": "[动画表情]",
    })
    collect(behavior(store, downloader=downloader), Bot(store), segments=[segment])
    assert seen == []
    assert store.find_by_tags(None)[0].file


# ----------------------------------------------------------------------
# 2. 描述字段与视觉后台任务
# ----------------------------------------------------------------------


def test_sticker_fields_are_backward_compatible(tmp_path):
    root = tmp_path / "stickers"
    root.mkdir()
    old = {
        "version": 1,
        "stickers": [{
            "id": "st_old", "file": "", "tags": ["猫"], "native_refs": {},
            "content_hash": "x", "created_at": 1.0,
        }],
    }
    (root / "index.json").write_text(json.dumps(old), encoding="utf-8")
    sticker = StickerStore(root).get("st_old")
    assert sticker is not None
    assert sticker.description == "" and sticker.emotions == () and sticker.uses == 0


def test_update_metadata_and_record_use(tmp_path):
    store = make_store(tmp_path)
    sticker = store.add(PNG + b"x")
    assert store.update_metadata(sticker.id, description=" 大笑 ", emotions=["开心", "Happy"]) is True
    got = store.get(sticker.id)
    assert got.description == "大笑" and got.emotions == ("开心", "happy")
    assert store.record_use(sticker.id) is True
    assert store.get(sticker.id).uses == 1
    assert store.update_metadata("st_missing", description="x") is False
    assert store.record_use("st_missing") is False
    reopened = StickerStore(tmp_path / "stickers").get(sticker.id)
    assert reopened.description == "大笑" and reopened.uses == 1


def test_description_generated_via_fallback_without_vision_role(tmp_path):
    # 没单独配 vision 角色时走框架回退链（落到 chat 模型），照样生成描述
    store = make_store(tmp_path)
    llm = ScriptedLLM({"vision": ['{"description": "猫咪大笑", "emotions": ["开心"]}']})
    bot = Bot(store, llm=llm)

    async def scenario():
        b = behavior(store)
        await b.on_message(runtime(bot), event(segments=[Segment("image", {
            "base64": base64.b64encode(PNG + b"x").decode(),
            "summary": "[动画表情]",
        })]))
        await asyncio.gather(*b._bg_tasks)

    asyncio.run(scenario())
    assert llm.calls != []
    assert store.find_by_tags(None)[0].description == "猫咪大笑"


def test_description_skipped_without_llm(tmp_path, caplog):
    store = make_store(tmp_path)
    bot = Bot(store, llm=None)
    with caplog.at_level("INFO"):
        collect(behavior(store), bot, segments=[Segment("image", {
            "base64": base64.b64encode(PNG + b"x").decode(),
            "summary": "[动画表情]",
        })])
    assert store.find_by_tags(None)[0].description == ""
    assert "无 LLM 客户端" in caplog.text


def test_description_written_back_when_vision_configured(tmp_path):
    store = make_store(tmp_path)
    cfg = Config()
    cfg.llm.models["vision"] = "m-vision"
    llm = ScriptedLLM({"vision": ['{"description": "猫咪大笑，用来接梗", "emotions": ["开心", "大笑"]}']})
    bot = Bot(store, config=cfg, llm=llm)

    async def scenario():
        b = behavior(store)
        await b.on_message(runtime(bot), event(segments=[Segment("image", {
            "base64": base64.b64encode(PNG + b"x").decode(),
            "summary": "[动画表情]",
        })]))
        await asyncio.gather(*b._bg_tasks)

    asyncio.run(scenario())
    item = store.find_by_tags(None)[0]
    assert item.description == "猫咪大笑，用来接梗"
    assert item.emotions == ("开心", "大笑")
    assert llm.calls[0]["role"] == "vision"
    # 视觉请求带 data URL 的图片
    content = llm.calls[0]["messages"][0]["content"]
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_description_failure_does_not_break_collection(tmp_path):
    store = make_store(tmp_path)
    cfg = Config()
    cfg.llm.models["vision"] = "m-vision"

    class Broken:
        async def complete(self, *args, **kwargs):
            raise RuntimeError("视觉模型挂了")

    bot = Bot(store, config=cfg, llm=Broken())

    async def scenario():
        b = behavior(store)
        await b.on_message(runtime(bot), event(segments=[Segment("image", {
            "base64": base64.b64encode(PNG + b"x").decode(),
            "summary": "[动画表情]",
        })]))
        await asyncio.gather(*b._bg_tasks, return_exceptions=True)

    asyncio.run(scenario())
    item = store.find_by_tags(None)[0]
    assert item.file and item.description == ""


# ----------------------------------------------------------------------
# 4. 两级选择
# ----------------------------------------------------------------------


def test_reschedule_descriptions_backfills_existing_entries(tmp_path):
    store = make_store(tmp_path)
    has_file = local_sticker(store, b"a", description="")
    store.add(native_refs={"snowluma": {"kind": "mface", "data": {"file": "x"}}})
    cfg = Config()
    cfg.llm.models["vision"] = "m-vision"
    llm = ScriptedLLM({"vision": ['{"description": "静态图", "emotions": ["平静"]}']})
    bot = Bot(store, config=cfg, llm=llm)

    async def scenario():
        b = behavior(store)
        assert await b.reschedule_descriptions(runtime(bot)) == 1
        await asyncio.gather(*b._bg_tasks)

    asyncio.run(scenario())
    assert store.get(has_file.id).description == "静态图"


def test_reschedule_descriptions_is_noop_without_llm(tmp_path):
    store = make_store(tmp_path)
    local_sticker(store, b"a", description="")
    bot = Bot(store, llm=None)
    b = behavior(store)

    async def scenario():
        return await b.reschedule_descriptions(runtime(bot))

    assert asyncio.run(scenario()) == 0


def test_embedding_channel_skips_sticker_deleted_mid_flight(tmp_path):
    """语义命中的条目若已没有本地文件，不能拿去发送。"""
    store = make_store(tmp_path)
    sticker = local_sticker(store, b"a", tags=("猫猫",), description="开心", emotions=())

    async def embed_fn(texts):
        return [[1.0, 0.0] for _ in texts]

    b = behavior(store, embed_fn=embed_fn)
    store.delete(sticker.id)
    assert asyncio.run(b._pick_by_embedding(store, [sticker], "开心")) is None


def test_candidates_without_local_file_are_ignored(tmp_path):
    store = make_store(tmp_path)
    store.add(native_refs={"snowluma": {"kind": "mface", "data": {"file": "x"}}})
    bot = Bot(store)
    asyncio.run(behavior(store).on_after_reply(runtime(bot), event(), "嗯"))
    assert bot.calls == []


def test_unparseable_llm_choice_falls_back_to_tags(tmp_path):
    store = make_store(tmp_path)
    cat = local_sticker(store, b"cat", tags=("猫猫",))
    local_sticker(store, b"dog", tags=("狗狗",))

    class LLM:
        calls = []

        async def complete(self, messages, **kwargs):
            self.calls.append(kwargs)
            return "我觉得第三张不错"

    bot = Bot(store, llm=LLM())
    asyncio.run(behavior(store).on_after_reply(runtime(bot), event("猫猫在干嘛"), "嗯"))
    assert bot.calls[0][2]["sticker"]["id"] == cat.id
    assert bot.llm.calls[0]["role"] == "fast"


def test_llm_choice_out_of_range_falls_back(tmp_path):
    store = make_store(tmp_path)
    local_sticker(store, b"cat", tags=("猫猫",))

    class LLM:
        async def complete(self, messages, **kwargs):
            return '{"index": 99}'

    bot = Bot(store, llm=LLM())
    asyncio.run(behavior(store).on_after_reply(runtime(bot), event("无关话题"), "嗯"))
    # 无标签命中时退回任意一张，不能因为越界就一条不发
    assert len(bot.calls) == 1


def test_llm_choice_selects_numbered_candidate(tmp_path):
    store = make_store(tmp_path)
    local_sticker(store, b"cat", tags=("猫猫",), description="猫咪大笑")
    second = local_sticker(store, b"dog", tags=("狗狗",), description="狗狗摇头")

    class LLM:
        async def complete(self, messages, **kwargs):
            return '{"index": 2}'

    bot = Bot(store, llm=LLM())
    asyncio.run(behavior(store).on_after_reply(runtime(bot), event("今天好累"), "嗯"))
    assert bot.calls[0][2]["sticker"]["id"] == second.id


def test_llm_prompt_lists_emotions_and_description_and_emotion_hint(tmp_path):
    store = make_store(tmp_path)
    local_sticker(store, b"cat", tags=("猫猫",), description="猫咪大笑", emotions=("开心",))

    class LLM:
        messages = None

        async def complete(self, messages, **kwargs):
            self.messages = messages
            return '{"index": 1}'

    llm = LLM()
    bot = Bot(store, llm=llm)
    behavior_obj = behavior(store)
    asyncio.run(behavior_obj.try_send(runtime(bot), event("今天好累"), probability=1.0,
                                      emotion_hint="接梗：猫猫梗"))
    prompt = llm.messages[-1]["content"]
    assert "接梗：猫猫梗" in prompt and "猫咪大笑" in prompt and "开心" in prompt
    assert "1. " in prompt


def test_llm_prompt_candidate_cap(tmp_path):
    store = make_store(tmp_path)
    for index in range(MAX_SELECT_CANDIDATES + 5):
        local_sticker(store, str(index).encode(), tags=("t",), description=f"第{index}张")

    class LLM:
        messages = None

        async def complete(self, messages, **kwargs):
            self.messages = messages
            return '{"index": 1}'

    llm = LLM()
    bot = Bot(store, llm=llm)
    asyncio.run(behavior(store).try_send(runtime(bot), event(), probability=1.0))
    numbered = [line for line in llm.messages[-1]["content"].splitlines() if line[:1].isdigit()]
    assert len(numbered) == MAX_SELECT_CANDIDATES


def test_embedding_hit_above_threshold_skips_llm(tmp_path):
    store = make_store(tmp_path)
    # 两条的描述向量分别指向 x 轴与 y 轴；查询向量贴 x 轴
    local_sticker(store, b"a", tags=("猫猫",), description="开心", emotions=())
    local_sticker(store, b"b", tags=("狗狗",), description="伤心", emotions=())

    async def embed_fn(texts):
        return [[1.0, 0.0] if "开心" in text else [0.0, 1.0] if "伤心" in text else [1.0, 0.0]
                for text in texts]

    class LLM:
        calls = 0

        async def complete(self, messages, **kwargs):
            self.calls += 1
            return '{"index": 1}'

    llm = LLM()
    bot = Bot(store, llm=llm)
    picked = asyncio.run(behavior(store, embed_fn=embed_fn).try_send(
        runtime(bot), event("开心"), probability=1.0, emotion_hint="短附和：开心",
    ))
    assert picked is True
    assert llm.calls == 0
    assert bot.calls[0][2]["sticker"]["tags"] == ["猫猫"]
    cache = json.loads((tmp_path / "stickers" / "embeddings.json").read_text(encoding="utf-8"))
    assert cache["version"] == 1 and cache["items"]


def test_embedding_below_threshold_falls_through_to_llm(tmp_path):
    store = make_store(tmp_path)
    local_sticker(store, b"a", tags=("猫猫",), description="开心", emotions=())

    async def embed_fn(texts):
        # 查询与描述正交：余弦 0 < 默认阈值 0.3
        return [[1.0, 0.0] if "开心" in text else [0.0, 1.0] for text in texts]

    class LLM:
        calls = 0

        async def complete(self, messages, **kwargs):
            self.calls += 1
            return '{"index": 1}'

    llm = LLM()
    bot = Bot(store, llm=llm)
    asyncio.run(behavior(store, embed_fn=embed_fn).try_send(
        runtime(bot), event("难过"), probability=1.0, emotion_hint="难过",
    ))
    assert llm.calls == 1


def test_embedding_cache_reused_within_refresh_window(tmp_path):
    store = make_store(tmp_path)
    local_sticker(store, b"a", tags=("猫猫",), description="开心", emotions=())
    calls: list[list[str]] = []

    async def embed_fn(texts):
        calls.append(list(texts))
        return [[1.0, 0.0] if "开心" in text else [1.0, 0.0] for text in texts]

    b = behavior(store, embed_fn=embed_fn, embedding_refresh_seconds=3600.0)
    bot = Bot(store)
    asyncio.run(b.try_send(runtime(bot), event("开心"), probability=1.0))
    asyncio.run(b.try_send(runtime(bot), event("开心"), probability=1.0))
    # 第一轮：描述 + 查询；第二轮缓存命中，只把查询文本送去向量化
    assert sum(text.count("猫猫") for call in calls for text in call) == 1
    assert len(calls) == 2


def test_embedding_failure_falls_through_to_tags(tmp_path):
    store = make_store(tmp_path)
    local_sticker(store, b"a", tags=("猫猫",), description="开心", emotions=())

    async def embed_fn(texts):
        raise RuntimeError("向量服务挂了")

    class LLM:
        async def complete(self, messages, **kwargs):
            return "not json"

    bot = Bot(store, llm=LLM())
    cfg_behavior = behavior(store, embed_fn=embed_fn)
    assert asyncio.run(cfg_behavior.try_send(runtime(bot), event("猫猫"), probability=1.0)) is True
    assert bot.calls[0][2]["sticker"]["tags"] == ["猫猫"]


def test_successful_send_increments_uses(tmp_path):
    store = make_store(tmp_path)
    sticker = local_sticker(store, b"a", tags=("猫猫",))
    bot = Bot(store)

    async def no_llm(messages, **kwargs):
        return "x"

    bot.llm = SimpleNamespace(complete=no_llm)
    asyncio.run(behavior(store).try_send(runtime(bot), event("猫猫"), probability=1.0))
    assert store.get(sticker.id).uses == 1


def test_failed_send_does_not_increment_uses(tmp_path):
    store = make_store(tmp_path)
    sticker = local_sticker(store, b"a", tags=("猫猫",))

    class FailingBot(Bot):
        async def call_capability(self, adapter, name, **kwargs):
            self.calls.append((adapter, name, kwargs))
            return False, "平台失败"

    asyncio.run(behavior(store).try_send(runtime(FailingBot(store)), event("猫猫"), probability=1.0))
    assert store.get(sticker.id).uses == 0


def test_missing_embedding_config_means_no_embedding_channel():
    cfg = SimpleNamespace(embedding_base_url="", embedding_model="")
    b = StickerBehavior(cfg, store_getter=lambda: None)
    assert b.embedding_configured() is False
    cfg.embedding_base_url = "https://api.example/v1"
    cfg.embedding_model = "text-embedding"
    assert b.embedding_configured() is True


# ----------------------------------------------------------------------
# 2. 存量回填工具
# ----------------------------------------------------------------------


def ref_only(store, *, url="", file=""):
    data = {key: value for key, value in (("url", url), ("file", file)) if value}
    return store.add(native_refs={"snowluma": {"kind": "image", "data": data}})


def test_backfill_prefers_index_url(tmp_path):
    store = make_store(tmp_path)
    target = ref_only(store, url="https://cdn.example/a.png", file="abc.image")
    seen: list[str] = []

    async def fetch(url):
        seen.append(url)
        return Attempt(data=PNG + b"downloaded")

    async def fetch_image(name):
        raise AssertionError("URL 成功时不应调用 get_image")

    counts = asyncio.run(backfill(store, fetch=fetch, fetch_image=fetch_image))
    assert counts.as_dict() == {"success": 1, "failed": 0, "expired": 0, "skipped": 0}
    assert seen == ["https://cdn.example/a.png"]
    filled = store.get(target.id)
    assert filled.file.endswith(".png") and store.path_of(filled).read_bytes().startswith(PNG)


def test_backfill_falls_back_to_get_image(tmp_path):
    store = make_store(tmp_path)
    target = ref_only(store, file="A1B2.image")
    names: list[str] = []

    async def fetch(url):
        return Attempt(expired=True, reason="HTTP 404")

    async def fetch_image(name):
        names.append(name)
        return Attempt(data=GIF + b"from-onebot")

    counts = asyncio.run(backfill(store, fetch=fetch, fetch_image=fetch_image))
    assert counts.success == 1 and names == ["A1B2.image"]
    assert store.get(target.id).file.endswith(".gif")


def test_backfill_counts_expired_failed_skipped(tmp_path):
    store = make_store(tmp_path)
    ref_only(store, url="https://cdn.example/gone.png")
    ref_only(store, url="https://cdn.example/boom.png")
    ref_only(store, file="only-name.image")

    async def fetch(url):
        if "gone" in url:
            return Attempt(expired=True, reason="HTTP 410")
        return Attempt(reason="连接重置")

    async def fetch_image(name):
        return Attempt(reason="协议端错误")

    counts = asyncio.run(backfill(store, fetch=fetch, fetch_image=fetch_image))
    # gone：URL 已过期、get_image 也失败，整体记过期；boom 的 URL 与
    # get_image 都是普通失败；第三条没有 URL，只试 get_image 失败。
    assert counts.expired == 1
    assert counts.failed == 2
    assert counts.skipped == 0
    assert all(not item.file for item in store.find_by_tags(None))


def test_backfill_without_image_fetcher_skips_entries_without_url(tmp_path):
    store = make_store(tmp_path)
    ref_only(store, file="only-name.image")

    async def fetch(url):  # pragma: no cover - 不该被调用
        raise AssertionError

    counts = asyncio.run(backfill(store, fetch=fetch))
    assert counts.as_dict()["skipped"] == 1


def test_backfill_skips_entries_that_already_have_files(tmp_path):
    store = make_store(tmp_path)
    store.add(PNG + b"x")
    calls: list[str] = []

    async def fetch(url):
        calls.append(url)
        return Attempt(data=PNG)

    counts = asyncio.run(backfill(store, fetch=fetch))
    assert counts.as_dict()["success"] == 0 and calls == []


def test_backfill_dry_run_does_not_write(tmp_path):
    store = make_store(tmp_path)
    target = ref_only(store, url="https://cdn.example/a.png")

    async def fetch(url):
        return Attempt(data=PNG + b"x")

    counts = asyncio.run(backfill(store, fetch=fetch, dry_run=True))
    assert counts.success == 1
    assert store.get(target.id).file == ""
    assert not (tmp_path / "stickers" / "files").exists()


def test_backfill_rejects_bad_format_bytes(tmp_path):
    store = make_store(tmp_path)
    target = ref_only(store, url="https://cdn.example/a.png")

    async def fetch(url):
        return Attempt(data=b"<html>not an image</html>")

    counts = asyncio.run(backfill(store, fetch=fetch))
    assert counts.failed == 1 and store.get(target.id).file == ""


def test_url_candidates_and_native_names():
    sticker = Sticker(id="st_x", file="", native_refs={
        "snowluma": {"kind": "mface", "data": {
            "url": "https://cdn.example/a.gif", "file": "abc.image", "emoji_id": "1",
        }},
        "telegram": {"file_id": "F"},
    })
    assert backfill_tool.url_candidates(sticker) == ["https://cdn.example/a.gif"]
    assert backfill_tool.native_file_names(sticker) == ["abc.image"]


def test_onebot_fetcher_reads_local_file(tmp_path):
    image = tmp_path / "a.png"
    image.write_bytes(PNG + b"local")
    fetcher = backfill_tool.OneBotImageFetcher("http://127.0.0.1:3000")
    attempt = asyncio.run(fetcher._read_target(str(image)))
    assert attempt.data == PNG + b"local"
    assert asyncio.run(fetcher._read_target(str(tmp_path / "missing.png"))).expired is True
    asyncio.run(fetcher.close())


def test_onebot_fetcher_maps_failed_payload_to_expired(tmp_path):
    class StubClient:
        is_closed = False

        async def post(self, path, json):
            assert path == "/get_image" and json == {"file": "abc.image"}
            return SimpleNamespace(status_code=200, json=lambda: {"status": "failed", "message": "文件已过期"})

    fetcher = backfill_tool.OneBotImageFetcher("http://127.0.0.1:3000")
    fetcher._client = StubClient()
    attempt = asyncio.run(fetcher.get_image("abc.image"))
    assert attempt.expired is True and "过期" in attempt.reason


def test_onebot_fetcher_requires_file_target(tmp_path):
    class StubClient:
        is_closed = False

        async def post(self, path, json):
            return SimpleNamespace(status_code=200, json=lambda: {"status": "ok", "data": {}})

    fetcher = backfill_tool.OneBotImageFetcher("http://127.0.0.1:3000")
    fetcher._client = StubClient()
    assert asyncio.run(fetcher.get_image("abc.image")).expired is True


def test_public_host_guard():
    assert backfill_tool._public_host("https://cdn.example/a.png") is True
    assert backfill_tool._public_host("http://127.0.0.1/a.png") is False
    assert backfill_tool._public_host("http://192.168.1.2/a.png") is False
    assert backfill_tool._public_host("ftp://example.com/a.png") is False


def test_run_uses_config_dir_and_snowluma_url(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "stickers:\n  dir: \"{dir}\"\nadapters:\n  snowluma:\n    http_url: http://127.0.0.1:3999\n".format(
            dir=(tmp_path / "stickers").as_posix()
        ),
        encoding="utf-8",
    )
    store = StickerStore(tmp_path / "stickers")
    target = ref_only(store, file="abc.image")
    created: list[str] = []

    class FakeFetcher:
        def __init__(self, http_url, *, access_token=""):
            created.append(http_url)

        async def get_image(self, file):
            return Attempt(data=PNG + b"x")

        async def close(self):
            pass

    monkeypatch.setattr(backfill_tool, "OneBotImageFetcher", FakeFetcher)
    counts = asyncio.run(backfill_tool.run(config_path))
    assert counts.success == 1 and created == ["http://127.0.0.1:3999"]
    assert StickerStore(tmp_path / "stickers").get(target.id).file


def test_main_prints_counts(tmp_path, capsys):
    store = StickerStore(tmp_path / "stickers")
    store.add(PNG + b"x")
    assert backfill_tool.main(["--dir", str(tmp_path / "stickers")]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"success": 0, "failed": 0, "expired": 0, "skipped": 0}


# ----------------------------------------------------------------------
# 5. WebUI
# ----------------------------------------------------------------------

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from rubenbot.web.app import create_app  # noqa: E402
from rubenbot.web.state import WebState  # noqa: E402


def test_webui_sticker_list_includes_new_fields(tmp_path):
    store = make_store(tmp_path)
    sticker = store.add(PNG + b"x", tags=["猫"])
    store.update_metadata(sticker.id, description="猫咪大笑", emotions=["开心"])
    store.record_use(sticker.id)
    cfg = Config()
    cfg.stickers.dir = str(tmp_path / "stickers")
    cfg.webui.password = ""
    state = WebState(config=cfg, store=MemoryStore(":memory:"), config_path=tmp_path / "config.yaml",
                     bot=SimpleNamespace(stickers=store))
    client = TestClient(create_app(state))
    response = client.get("/api/stickers")
    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    assert item["description"] == "猫咪大笑"
    assert item["emotions"] == ["开心"]
    assert item["uses"] == 1
    assert item["image_url"]


def test_webui_sticker_list_legacy_entry_has_empty_new_fields(tmp_path):
    """旧索引条目（没有新字段）在列表里读出空值，不报错。"""
    root = tmp_path / "stickers"
    root.mkdir()
    (root / "index.json").write_text(json.dumps({
        "version": 1,
        "stickers": [{
            "id": "st_legacy", "file": "", "tags": ["猫"], "native_refs": {},
            "content_hash": "x", "created_at": 1.0,
        }],
    }), encoding="utf-8")
    cfg = Config()
    cfg.stickers.dir = str(root)
    cfg.webui.password = ""
    state = WebState(config=cfg, store=MemoryStore(":memory:"), config_path=tmp_path / "config.yaml")
    client = TestClient(create_app(state))
    item = client.get("/api/stickers").json()["items"][0]
    assert item["description"] == "" and item["emotions"] == [] and item["uses"] == 0
    assert item["image_url"] is None


def test_sticker_config_defaults_keep_old_behavior():
    cfg = Config()
    s = cfg.stickers
    assert s.max_file_bytes == 8 * 1024 * 1024
    assert s.embedding_base_url == "" and s.embedding_model == ""
    assert s.embedding_threshold == 0.3 and s.embedding_refresh_seconds == 21600.0


def test_sticker_config_embedding_fields_parse_and_clamp():
    cfg = Config.from_dict({"stickers": {
        "embedding_base_url": " https://api.example/v1 ",
        "embedding_model": "text-embedding-3-small",
        "embedding_threshold": 2,
        "embedding_refresh_seconds": -5,
        "max_file_bytes": 0,
    }})
    s = cfg.stickers
    assert s.embedding_base_url == "https://api.example/v1"
    assert s.embedding_model == "text-embedding-3-small"
    assert s.embedding_threshold == 1.0
    assert s.embedding_refresh_seconds == 0.0
    assert s.max_file_bytes == 8 * 1024 * 1024


def test_structured_config_masks_embedding_key_and_keeps_it_on_empty_save():
    from rubenbot.web.config_schema import structured_config, validate_structured

    cfg = Config()
    cfg.stickers.embedding_api_key = "sk-secret"
    view = structured_config(cfg)
    assert view["stickers"]["embedding_api_key"] == ""
    assert view["secrets"]["stickers.embedding_api_key"] is True
    # 表单原样回传空值：保留原 key，不写空串
    update = validate_structured({"stickers": {
        "embedding_base_url": "https://api.example/v1",
        "embedding_api_key": "",
    }})
    assert "embedding_api_key" not in update["stickers"]
    assert update["stickers"]["embedding_base_url"] == "https://api.example/v1"


def test_collect_one_bad_segment_does_not_block_others(tmp_path):
    store = make_store(tmp_path)

    class FlakyStore:
        """第一张图入库时抛错，其余照常。"""

        def __init__(self, inner):
            self.inner = inner
            self.failed = False
            self.root = inner.root

        def add(self, data=None, **kwargs):
            if not self.failed and data and data.endswith(b"bad"):
                self.failed = True
                raise RuntimeError("库炸了")
            return self.inner.add(data, **kwargs)

        def path_of(self, sticker):
            return self.inner.path_of(sticker)

        def find_by_tags(self, tags=None, **kwargs):
            return self.inner.find_by_tags(tags, **kwargs)

    flaky = FlakyStore(store)
    bad = Segment("image", {"base64": base64.b64encode(PNG + b"bad").decode(), "summary": "[动画表情]"})
    good = Segment("image", {"base64": base64.b64encode(PNG + b"good").decode(), "summary": "[动画表情]"})
    collect(behavior(flaky), Bot(flaky), segments=[bad, good])
    assert flaky.failed is True
    items = store.find_by_tags(None)
    assert len(items) == 1 and store.path_of(items[0]) is not None


def test_reschedule_descriptions_batches_and_serializes(tmp_path):
    store = make_store(tmp_path)
    for index in range(12):
        local_sticker(store, str(index).encode(), tags=("t",), description="")
    cfg = Config()
    cfg.llm.models["vision"] = "m-vision"
    llm = ScriptedLLM({"vision": ['{"description": "静态图", "emotions": ["平静"]}']})
    bot = Bot(store, config=cfg, llm=llm)

    async def scenario():
        b = behavior(store)
        first = await b.reschedule_descriptions(runtime(bot))
        # 一批补完会自动续排下一批，直到全部补完
        while b._bg_tasks:
            await asyncio.gather(*list(b._bg_tasks))
        second = await b.reschedule_descriptions(runtime(bot))
        return first, second

    first, second = asyncio.run(scenario())
    assert (first, second) == (10, 0)
    assert len(llm.calls) == 12


class _FakeResponse:
    def __init__(self, status=200, headers=None, chunks=()):
        self.status_code = status
        self.headers = headers or {}
        self._chunks = list(chunks)

    async def aiter_raw(self):
        for chunk in self._chunks:
            yield chunk


class _FakeStream:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *exc):
        return False


class _FakeClient:
    """伪造 httpx.AsyncClient.stream：按 URL 返回预置响应，并记录请求。"""

    responses: dict[str, _FakeResponse] = {}
    requests: list[str] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, method, url, headers=None):
        _FakeClient.requests.append(url)
        return _FakeStream(_FakeClient.responses[url])


def _patch_default_download(monkeypatch, responses):
    from rubenbot import sticker_behavior

    _FakeClient.responses = responses
    _FakeClient.requests = []
    monkeypatch.setattr(sticker_behavior.httpx, "AsyncClient", _FakeClient)

    async def allow(url):
        return True

    monkeypatch.setattr(sticker_behavior, "_public_url", allow)
    return sticker_behavior


def test_default_download_decompresses_gzip_body(monkeypatch):
    import gzip

    raw = PNG + b"gzipped"
    sb = _patch_default_download(monkeypatch, {
        "https://cdn.example/a.png": _FakeResponse(
            headers={"content-encoding": "gzip"}, chunks=[gzip.compress(raw)],
        ),
    })
    assert asyncio.run(sb._default_download("https://cdn.example/a.png", 1024)) == raw


def test_default_download_rejects_oversized_body(monkeypatch):
    sb = _patch_default_download(monkeypatch, {
        "https://cdn.example/big.png": _FakeResponse(chunks=[PNG + b"x" * 100]),
    })
    with pytest.raises(ValueError, match="上限"):
        asyncio.run(sb._default_download("https://cdn.example/big.png", 20))


def test_default_download_follows_one_redirect(monkeypatch):
    sb = _patch_default_download(monkeypatch, {
        "https://cdn.example/old.png": _FakeResponse(
            status=302, headers={"location": "https://cdn.example/new.png"},
        ),
        "https://cdn.example/new.png": _FakeResponse(chunks=[PNG + b"new"]),
    })
    assert asyncio.run(sb._default_download("https://cdn.example/old.png", 1024)) == PNG + b"new"


def test_default_download_rejects_private_address(monkeypatch):
    from rubenbot import sticker_behavior

    monkeypatch.setattr(sticker_behavior, "DOWNLOAD_TIMEOUT", 0.1)
    with pytest.raises(ValueError, match="公网"):
        asyncio.run(sticker_behavior._default_download("http://127.0.0.1/a.png", 1024))


def test_llm_selection_skipped_when_no_descriptions_exist(tmp_path):
    """候选全无描述/情绪时编号列表没有信息量：不为注定无效的选择多打一轮模型。"""
    store = make_store(tmp_path)
    cat = local_sticker(store, b"cat", tags=("猫猫",), description="", emotions=())

    class LLM:
        calls = 0

        async def complete(self, messages, **kwargs):
            self.calls += 1
            return '{"index": 1}'

    llm = LLM()
    bot = Bot(store, llm=llm)
    asyncio.run(behavior(store).try_send(runtime(bot), event("猫猫"), probability=1.0))
    assert llm.calls == 0
    assert bot.calls[0][2]["sticker"]["id"] == cat.id
