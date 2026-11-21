"""表情库质量三修：内容去重、表情/图片分类隔离、WebUI 计数与描述上磁贴。

对应 2026-11-21 线上数据：140 条索引按 content_hash 只有 105 个唯一内容
（14 组重复、多 35 条）；69 条摘要为「[动画表情]」、71 条为「[图片]」。
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import ScriptedLLM  # noqa: F401 - 供后续视觉用例复用
from rubenbot.adapters.base import MessageEvent, Segment
from rubenbot.config import Config
from rubenbot.memory.store import MemoryStore
from rubenbot.sticker_behavior import StickerBehavior
from rubenbot.stickers import (
    KIND_PHOTO,
    KIND_STICKER,
    StickerStore,
    infer_kind,
)
from tools import dedupe_stickers as dedupe_tool

PNG = b"\x89PNG\r\n\x1a\n"


# ----------------------------------------------------------------------
# 夹具
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


class Adapter:
    name = "snowluma"
    capabilities = frozenset({"send_sticker"})
    capability_config_keys = {"send_sticker": "sticker"}
    capability_defaults = {}


class Bot:
    def __init__(self, store, *, config=None):
        self.stickers = store
        self.adapters = {Adapter.name: Adapter()}
        self.config = config or Config()
        self.calls = []

    async def call_capability(self, adapter, name, **kwargs):
        self.calls.append((adapter, name, kwargs))
        return True, "ok"


def behavior(store, *, downloader=None, **values) -> StickerBehavior:
    cfg = SimpleNamespace(
        enabled=True, collect=True, collect_photos=False,
        send_probability=1.0, cooldown_seconds=300.0,
    )
    for key, value in values.items():
        setattr(cfg, key, value)
    return StickerBehavior(
        cfg, store_getter=lambda: store, rng=FixedRng(), clock=Clock(),
        downloader=downloader or _no_download,
    )


async def _no_download(url, limit):
    return None


def runtime(bot):
    return SimpleNamespace(extras={"bot": bot}, config=bot.config, store=None, llm=None)


def event(text="你好", *, segments=None):
    return MessageEvent(
        platform="snowluma", chat_type="group", chat_id="g1",
        sender_id="u1", sender_name="阿花", text=text, segments=list(segments or []),
    )


def collect(store, *, segments, bot=None, **values):
    b = behavior(store, **values)
    asyncio.run(b.on_message(runtime(bot or Bot(store)), event(segments=segments)))


def image_segment(raw=PNG + b"x", summary="[动画表情]") -> Segment:
    data = {"base64": base64.b64encode(raw).decode()}
    if summary is not None:
        data["summary"] = summary
    return Segment("image", data)


def write_index(root: Path, stickers: list[dict]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.json").write_text(
        json.dumps({"version": 1, "stickers": stickers}, ensure_ascii=False), encoding="utf-8"
    )


def entry(sticker_id: str, *, content_hash: str, **values) -> dict:
    row = {
        "id": sticker_id, "file": "", "tags": [], "native_refs": {},
        "content_hash": content_hash, "created_at": 1.0,
        "description": "", "emotions": [], "uses": 0,
    }
    row.update(values)
    return row


# ----------------------------------------------------------------------
# 1. 种类推导与旧索引兼容
# ----------------------------------------------------------------------


def test_infer_kind_from_native_ref_signals():
    assert infer_kind({}) == KIND_STICKER
    assert infer_kind({"snowluma": {"kind": "mface", "data": {}}}) == KIND_STICKER
    assert infer_kind({"telegram": {"kind": "sticker", "file_id": "F"}}) == KIND_STICKER
    assert infer_kind({"snowluma": {"kind": "image", "data": {"summary": "[动画表情]"}}}) == KIND_STICKER
    assert infer_kind({"snowluma": {"kind": "image", "data": {"summary": "[图片]"}}}) == KIND_PHOTO
    assert infer_kind({"snowluma": {"kind": "image", "data": {"file": "a.jpg"}}}) == KIND_PHOTO
    # 多个平台引用里只要有一个表情信号就算表情
    assert infer_kind({
        "snowluma": {"kind": "image", "data": {"summary": "[图片]"}},
        "telegram": {"kind": "sticker", "file_id": "F"},
    }) == KIND_STICKER


def test_legacy_index_derives_kind_from_summary(tmp_path):
    """旧索引没有 kind：按 native_refs 的段类型与 summary 现场推导。"""
    root = tmp_path / "stickers"
    write_index(root, [
        entry("st_sticker", content_hash="a", native_refs={
            "snowluma": {"kind": "image", "data": {"summary": "[动画表情]"}},
        }),
        entry("st_photo", content_hash="b", native_refs={
            "snowluma": {"kind": "image", "data": {"summary": "[图片]"}},
        }),
        entry("st_mface", content_hash="c", native_refs={
            "snowluma": {"kind": "mface", "data": {"emoji_id": "1"}},
        }),
        entry("st_plain", content_hash="d"),
    ])
    store = StickerStore(root)
    assert store.get("st_sticker").kind == KIND_STICKER
    assert store.get("st_photo").kind == KIND_PHOTO
    assert store.get("st_mface").kind == KIND_STICKER
    assert store.get("st_plain").kind == KIND_STICKER
    assert [item.id for item in store.find_by_tags(None, kind=KIND_PHOTO)] == ["st_photo"]


def test_kind_roundtrips_through_index(tmp_path):
    store = make_store(tmp_path)
    photo = store.add(
        PNG + b"photo",
        native_refs={"snowluma": {"kind": "image", "data": {"summary": "[图片]"}}},
    )
    assert photo.kind == KIND_PHOTO
    reopened = StickerStore(tmp_path / "stickers")
    assert reopened.get(photo.id).kind == KIND_PHOTO
    # 显式传 kind 时优先于推导
    explicit = store.add(PNG + b"explicit", kind=KIND_STICKER)
    assert explicit.kind == KIND_STICKER


def test_same_content_collected_as_photo_then_sticker_merges_to_sticker(tmp_path):
    store = make_store(tmp_path)
    raw = PNG + b"same"
    photo = store.add(raw, native_refs={"snowluma": {"kind": "image", "data": {"summary": "[图片]"}}})
    merged = store.add(raw, native_refs={"telegram": {"kind": "sticker", "file_id": "F"}})
    assert merged.id == photo.id
    assert merged.kind == KIND_STICKER
    assert merged.native_refs["telegram"] == {"kind": "sticker", "file_id": "F"}


# ----------------------------------------------------------------------
# 2. 收集：photo 默认不入库
# ----------------------------------------------------------------------


def test_collect_skips_photo_by_default(tmp_path, caplog):
    store = make_store(tmp_path)
    with caplog.at_level("DEBUG"):
        collect(store, segments=[image_segment(summary="[图片]")])
    assert store.find_by_tags(None) == []
    assert "跳过普通图片" in caplog.text


def test_collect_photo_when_enabled_and_never_selected(tmp_path):
    store = make_store(tmp_path)
    collect(store, segments=[image_segment(summary="[图片]")], collect_photos=True)
    items = store.find_by_tags(None)
    assert len(items) == 1 and items[0].kind == KIND_PHOTO and items[0].file
    # 选图只看 sticker：库里只有 photo 时一张都不发
    bot = Bot(store)
    asyncio.run(behavior(store).on_after_reply(runtime(bot), event(), "嗯"))
    assert bot.calls == []


def test_collect_sticker_without_summary_from_mface(tmp_path):
    store = make_store(tmp_path)
    collect(store, segments=[Segment.sticker(native_ref={
        "kind": "mface", "data": {"emoji_id": "1"},
    })])
    items = store.find_by_tags(None)
    assert len(items) == 1 and items[0].kind == KIND_STICKER


def test_selection_prefers_sticker_over_photo(tmp_path):
    store = make_store(tmp_path)
    photo = store.add(PNG + b"photo", kind=KIND_PHOTO, tags=["猫猫"])
    sticker = store.add(PNG + b"sticker", kind=KIND_STICKER, tags=["猫猫"])
    bot = Bot(store)
    asyncio.run(behavior(store).on_after_reply(runtime(bot), event("猫猫"), "嗯"))
    assert len(bot.calls) == 1 and bot.calls[0][2]["sticker"]["id"] == sticker.id
    assert photo.id not in bot.calls[0][2]["sticker"]["id"]


def test_descriptions_never_generated_for_photos(tmp_path):
    """普通图片不参选，也不值得花视觉模型额度：补描述只排真表情。"""
    store = make_store(tmp_path)
    sticker = store.add(PNG + b"sticker", kind=KIND_STICKER)
    store.add(PNG + b"photo", kind=KIND_PHOTO)
    cfg = Config()
    cfg.llm.models["vision"] = "m-vision"
    llm = ScriptedLLM({"vision": ['{"description": "猫咪大笑", "emotions": ["开心"]}']})
    bot = Bot(store, config=cfg)
    bot.llm = llm
    behavior_obj = behavior(store)

    async def scenario():
        assert await behavior_obj.reschedule_descriptions(runtime(bot)) == 1
        await asyncio.gather(*behavior_obj._bg_tasks)

    asyncio.run(scenario())
    assert len(llm.calls) == 1
    assert store.get(sticker.id).description == "猫咪大笑"
    assert [item.description for item in store.find_by_tags(None, kind=KIND_PHOTO)] == [""]


# ----------------------------------------------------------------------
# 3. 按内容去重：合并规则与工具
# ----------------------------------------------------------------------


def test_merge_combines_fields_and_keeps_earliest(tmp_path):
    root = tmp_path / "stickers"
    (root / "files").mkdir(parents=True)
    (root / "files" / "aaaa.png").write_bytes(PNG + b"x")
    write_index(root, [
        entry("st_late", content_hash="hash", file="files/aaaa.png", created_at=20.0,
              tags=["晚", "猫"], native_refs={"telegram": {"file_id": "F"}},
              uses=2, kind=KIND_STICKER, description="后来的描述", emotions=["开心"]),
        entry("st_early", content_hash="hash", file="files/aaaa.png", created_at=10.0,
              tags=["早", "猫"], native_refs={"snowluma": {"kind": "mface", "data": {}}},
              uses=3, kind=KIND_PHOTO, description="", emotions=["大笑", "开心"]),
    ])
    store = StickerStore(root)
    merged = store.merge(["st_late", "st_early"])
    assert merged.id == "st_early" and merged.created_at == 10.0
    assert store.get("st_late") is None and store.get("st_early") is not None
    assert merged.tags == ("早", "猫", "晚")
    assert merged.native_refs == {
        "snowluma": {"kind": "mface", "data": {}},
        "telegram": {"file_id": "F"},
    }
    assert merged.uses == 5
    assert merged.kind == KIND_STICKER  # 任一方是表情即算表情
    assert merged.description == "后来的描述"
    assert merged.emotions == ("大笑", "开心")
    # 重新打开只看到合并后的一条
    reopened = StickerStore(root)
    assert [item.id for item in reopened.find_by_tags(None)] == ["st_early"]
    # 图片文件不动
    assert (root / "files" / "aaaa.png").is_file()


def test_merge_with_fewer_than_two_returns_existing(tmp_path):
    store = make_store(tmp_path)
    sticker = store.add(PNG + b"x")
    assert store.merge([sticker.id]) == sticker
    assert store.merge(["st_missing"]) is None


def test_add_dedupes_existing_content_without_new_entry(tmp_path):
    """新增入口遇到已有内容：合并进旧条目，不建新条目，id 与 created_at 不变。"""
    store = make_store(tmp_path)
    raw = PNG + b"same"
    first = store.add(raw, tags=["a"], now=100.0)
    merged = store.add(raw, tags=["b", "a"], now=200.0)
    assert merged.id == first.id and merged.created_at == 100.0
    assert merged.tags == ("a", "b")
    assert len(store.find_by_tags(None)) == 1


def test_dedupe_tool_merges_index_and_reports_counts(tmp_path):
    root = tmp_path / "stickers"
    (root / "files").mkdir(parents=True)
    (root / "files" / "aaaa.png").write_bytes(PNG + b"x")
    rows = [
        entry("st_a1", content_hash="h1", file="files/aaaa.png", created_at=1.0, tags=["甲"],
              native_refs={"snowluma": {"kind": "mface", "data": {}}}, uses=1),
        entry("st_a2", content_hash="h1", file="files/aaaa.png", created_at=2.0, tags=["乙"],
              native_refs={"telegram": {"file_id": "F"}}, uses=4),
        entry("st_a3", content_hash="h1", file="files/aaaa.png", created_at=3.0, uses=2),
        entry("st_b1", content_hash="h2", file="files/aaaa.png", created_at=1.0),
        entry("st_c1", content_hash="h3", file="files/aaaa.png", created_at=1.0),
        entry("st_c2", content_hash="h3", file="files/aaaa.png", created_at=2.0),
    ]
    write_index(root, rows)
    counts = dedupe_tool.dedupe(StickerStore(root))
    assert counts.as_dict() == {"before": 6, "after": 3, "groups": 2, "removed": 3}
    store = StickerStore(root)
    rows = store.find_by_tags(None)
    assert len(rows) == 3
    merged = store.get("st_a1")
    assert merged.tags == ("甲", "乙") and merged.uses == 7
    # 被合并条目的图片文件保留，不动磁盘
    assert (root / "files" / "aaaa.png").is_file()


def test_dedupe_tool_dry_run_does_not_write(tmp_path):
    root = tmp_path / "stickers"
    write_index(root, [
        entry("st_a1", content_hash="h1", created_at=1.0),
        entry("st_a2", content_hash="h1", created_at=2.0),
    ])
    before_text = (root / "index.json").read_text(encoding="utf-8")
    counts = dedupe_tool.dedupe(StickerStore(root), dry_run=True)
    assert counts.as_dict() == {"before": 2, "after": 1, "groups": 1, "removed": 1}
    assert (root / "index.json").read_text(encoding="utf-8") == before_text


def test_dedupe_tool_main_prints_counts(tmp_path, capsys):
    root = tmp_path / "stickers"
    write_index(root, [
        entry("st_a1", content_hash="h1", created_at=1.0),
        entry("st_a2", content_hash="h1", created_at=2.0),
    ])
    assert dedupe_tool.main(["--dir", str(root)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"before": 2, "after": 1, "groups": 1, "removed": 1}
    assert dedupe_tool.main(["--dir", str(tmp_path / "missing")]) == 1


def test_dedupe_tool_run_uses_config_dir(tmp_path):
    root = tmp_path / "stickers"
    write_index(root, [
        entry("st_a1", content_hash="h1", created_at=1.0),
        entry("st_a2", content_hash="h1", created_at=2.0),
    ])
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f'stickers:\n  dir: "{root.as_posix()}"\n', encoding="utf-8")
    counts = dedupe_tool.run(config_path)
    assert counts.removed == 1 and len(StickerStore(root).find_by_tags(None)) == 1


# ----------------------------------------------------------------------
# 4. WebUI：默认只返回 sticker + photo_count
# ----------------------------------------------------------------------

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from rubenbot.web.app import create_app  # noqa: E402
from rubenbot.web.state import WebState  # noqa: E402


def web_client(tmp_path, store):
    cfg = Config()
    cfg.stickers.dir = str(tmp_path / "stickers")
    cfg.webui.password = ""
    state = WebState(config=cfg, store=MemoryStore(":memory:"),
                     config_path=tmp_path / "config.yaml",
                     bot=SimpleNamespace(stickers=store))
    return TestClient(create_app(state))


def test_api_defaults_to_stickers_and_reports_photo_count(tmp_path):
    store = make_store(tmp_path)
    sticker = store.add(PNG + b"sticker", kind=KIND_STICKER, tags=["猫"])
    store.update_metadata(sticker.id, description="猫咪大笑", emotions=["开心"])
    store.add(PNG + b"photo1", kind=KIND_PHOTO)
    store.add(PNG + b"photo2", kind=KIND_PHOTO)
    client = web_client(tmp_path, store)
    payload = client.get("/api/stickers").json()
    assert payload["photo_count"] == 2
    assert [item["id"] for item in payload["items"]] == [sticker.id]
    assert payload["items"][0]["kind"] == KIND_STICKER
    assert payload["items"][0]["description"] == "猫咪大笑"
    assert payload["items"][0]["emotions"] == ["开心"]


def test_api_include_photos_returns_everything_with_kind(tmp_path):
    store = make_store(tmp_path)
    store.add(PNG + b"sticker", kind=KIND_STICKER)
    store.add(PNG + b"photo", kind=KIND_PHOTO)
    client = web_client(tmp_path, store)
    payload = client.get("/api/stickers?include_photos=1").json()
    assert payload["photo_count"] == 1
    assert sorted(item["kind"] for item in payload["items"]) == [KIND_PHOTO, KIND_STICKER]


def test_api_photo_count_zero_when_only_stickers(tmp_path):
    store = make_store(tmp_path)
    store.add(PNG + b"sticker", kind=KIND_STICKER)
    payload = web_client(tmp_path, store).get("/api/stickers").json()
    assert payload["photo_count"] == 0
