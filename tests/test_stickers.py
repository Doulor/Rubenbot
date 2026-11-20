"""表情库：入库去重、标签检索、原生引用回填与索引落盘。"""

from __future__ import annotations

import json

from rubenbot.stickers import StickerStore


def make_store(tmp_path) -> StickerStore:
    return StickerStore(tmp_path / "stickers")


def test_add_writes_file_and_index(tmp_path):
    store = make_store(tmp_path)
    sticker = store.add(
        b"\x89PNG-fake",
        tags=["开心", "开心", " 猫 "],
        native_refs={"snowluma": {"kind": "mface", "data": {"emoji_id": "1"}}},
        file_name="cat.png",
        now=1234.0,
    )
    assert sticker.id.startswith("st_")
    assert sticker.tags == ("开心", "猫")
    assert sticker.content_hash
    assert sticker.created_at == 1234.0
    path = store.path_of(sticker)
    assert path is not None and path.read_bytes() == b"\x89PNG-fake"
    assert path.suffix == ".png"
    # 索引落盘，重新打开能读回来
    index = json.loads((tmp_path / "stickers" / "index.json").read_text(encoding="utf-8"))
    assert index["version"] == 1 and index["stickers"][0]["id"] == sticker.id
    reopened = StickerStore(tmp_path / "stickers")
    again = reopened.get(sticker.id)
    assert again is not None
    assert again.tags == ("开心", "猫")
    assert again.native_refs == sticker.native_refs


def test_add_dedupes_by_content_hash_and_merges_tags(tmp_path):
    store = make_store(tmp_path)
    first = store.add(b"same-bytes", tags=["a"])
    second = store.add(b"same-bytes", tags=["b", "a"])
    assert first.id == second.id
    assert second.tags == ("a", "b")
    # 不重复写文件、不重复建记录
    assert len(list((tmp_path / "stickers" / "files").iterdir())) == 1
    assert len(store.find_by_tags(None)) == 1


def test_dedupe_merges_new_platform_refs(tmp_path):
    store = make_store(tmp_path)
    store.add(b"same-bytes", native_refs={"snowluma": {"emoji_id": "1"}})
    merged = store.add(b"same-bytes", native_refs={"telegram": {"file_id": "abc"}})
    assert merged.native_refs == {
        "snowluma": {"emoji_id": "1"},
        "telegram": {"file_id": "abc"},
    }


def test_ref_only_entry_without_bytes(tmp_path):
    store = make_store(tmp_path)
    sticker = store.add(native_refs={"snowluma": {"kind": "mface", "data": {"emoji_id": "9"}}})
    assert sticker.file == ""
    assert store.path_of(sticker) is None
    # 同样的引用再来一次：按引用序列化去重
    assert store.add(native_refs={"snowluma": {"kind": "mface", "data": {"emoji_id": "9"}}}) is not None
    assert len(store.find_by_tags(None)) == 1
    # 既没有字节也没有引用：直接报错
    try:
        store.add()
    except ValueError as exc:
        assert "表情入库" in str(exc)
    else:  # pragma: no cover - 不应走到
        raise AssertionError("空引用应抛 ValueError")


def test_find_by_tags_and_limit(tmp_path):
    store = make_store(tmp_path)
    happy = store.add(b"1", tags=["开心"])
    store.add(b"2", tags=["难过"])
    store.add(b"3", tags=["开心", "猫"])
    assert {item.id for item in store.find_by_tags(["猫"])} == {store.find_by_tags(["猫"])[0].id}
    assert len(store.find_by_tags(["开心"])) == 2
    assert {item.id for item in store.find_by_tags(["开心", "难过"])} == {
        item.id for item in store.find_by_tags(None)
    }
    assert store.find_by_tags(None, limit=1) == [happy]
    assert store.find_by_tags(["不存在"]) == []


def test_record_native_ref(tmp_path):
    store = make_store(tmp_path)
    sticker = store.add(b"x", tags=["a"])
    assert store.record_native_ref(sticker.id, "telegram", {"file_id": "F1"}) is True
    got = store.get(sticker.id)
    assert got is not None and got.native_refs["telegram"] == {"file_id": "F1"}
    # 不存在的 id / 空引用都返回 False，不新增记录
    assert store.record_native_ref("st_missing", "telegram", {"file_id": "x"}) is False
    assert store.record_native_ref(sticker.id, "telegram", None) is False
    assert store.record_native_ref(sticker.id, "", "x") is False


def test_reference_shape(tmp_path):
    store = make_store(tmp_path)
    sticker = store.add(b"x", tags=["a"], native_refs={"telegram": {"file_id": "F"}})
    ref = store.reference(sticker)
    assert ref["id"] == sticker.id
    assert ref["tags"] == ["a"]
    assert ref["native_refs"] == {"telegram": {"file_id": "F"}}
    assert ref["file"] == str(store.path_of(sticker))


def test_delete_removes_index_and_file(tmp_path):
    store = make_store(tmp_path)
    sticker = store.add(b"delete-me", file_name="x.png")
    path = store.path_of(sticker)
    assert path is not None and path.exists()
    assert store.delete(sticker.id) is True
    assert not path.exists()
    assert store.get(sticker.id) is None
    assert StickerStore(tmp_path / "stickers").get(sticker.id) is None


def test_delete_reference_only_entry(tmp_path):
    store = make_store(tmp_path)
    sticker = store.add(native_refs={"snowluma": {"id": "1"}})
    assert store.delete(sticker.id) is True
    assert store.find_by_tags(None) == []


def test_broken_index_is_ignored(tmp_path):
    root = tmp_path / "stickers"
    root.mkdir()
    (root / "index.json").write_text("{ not json", encoding="utf-8")
    store = StickerStore(root)
    assert store.find_by_tags(None) == []
