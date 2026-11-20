"""身份缓存、TTL 与 SnowLuma 名称刷新单测。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx

from rubenbot.adapters.snowluma import SnowLumaAdapter
from rubenbot.identity import IdentityService, avatar_url
from rubenbot.memory.store import MemoryStore


def test_identity_cache_is_platform_and_kind_scoped():
    store = MemoryStore(":memory:")
    try:
        store.upsert_identity("snowluma", "42", kind="user", name="群名片甲")
        store.upsert_identity("telegram", "42", kind="user", name="Telegram 甲")
        store.upsert_identity("snowluma", "42", kind="group", name="测试群")
        assert store.get_identity("snowluma", "42", "user")["name"] == "群名片甲"
        assert store.get_identity("telegram", "42", "user")["name"] == "Telegram 甲"
        assert store.get_identity("snowluma", "42", "group")["name"] == "测试群"
    finally:
        store.close()


def test_avatar_never_guesses_non_numeric_snowluma_ids():
    assert avatar_url("snowluma", "123", "user").startswith("https://q1.qlogo.cn/")
    assert avatar_url("snowluma", "group:123", "group") == ""
    assert avatar_url("snowluma", "qq-alias", "user") == ""
    assert avatar_url("telegram", "123", "user") == ""


def test_group_platforms_aggregates_only_real_group_sources():
    store = MemoryStore(":memory:")
    try:
        store.upsert_identity("telegram", "99", kind="group", name="TG 群")
        store.add_raw_message(platform="snowluma", chat_id="99", chat_type="group", sender_id="1", text="x")
        store.add_raw_message(platform="telegram", chat_id="99", chat_type="private", sender_id="1", text="x")
        assert store.group_platforms("99") == ["snowluma", "telegram"]
        assert store.group_platforms("404") == []
    finally:
        store.close()


def test_cold_directory_refresh_and_offline_fallback():
    store = MemoryStore(":memory:")
    try:
        calls = []

        class Adapter:
            async def refresh_identities(self, **kwargs):
                calls.append(kwargs)
                if len(calls) > 1:
                    raise OSError("离线")
                return [{"id": "42", "kind": "group", "name": "旧群名"}]

        state = SimpleNamespace(store=store, bot=SimpleNamespace(adapters={"snowluma": Adapter()}))
        service = IdentityService(state)
        assert asyncio.run(service.refresh(platform="snowluma"))["refreshed"] == 1
        assert asyncio.run(service.refresh(platform="snowluma"))["cached"]
        store.set_meta("group_directory:snowluma:updated_at", "0")
        assert not asyncio.run(service.refresh(platform="snowluma"))["ok"]
        assert service.chat("snowluma", "42")["name"] == "旧群名"
        assert len(calls) == 2
    finally:
        store.close()


def test_member_lookup_is_scoped_to_one_group_and_account():
    import json
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        action = request.url.path.rsplit("/", 1)[-1]
        params = json.loads(request.content)
        calls.append((action, params))
        profiles = {("get_group_list", 0): [{"group_id": 8, "group_name": "排球队"}],
                    ("get_group_info", 8): {"group_name": "排球队"},
                    ("get_group_member_info", 8): {"card": "队长", "nickname": "小周"},
                    ("get_group_member_info", 9): {"card": "班长", "nickname": "小周"},
                    ("get_stranger_info", 0): {"nickname": "小周"}}
        return httpx.Response(200, json={"status": "ok", "data": profiles[action, params.get("group_id", 0)]})

    async def check():
        async with httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(handler)) as client:
            adapter = SnowLumaAdapter(http_client=client)
            store = MemoryStore(":memory:")
            service = IdentityService(SimpleNamespace(store=store, bot=SimpleNamespace(adapters={"snowluma": adapter})))
            try:
                for params in [{}, {"chat_id": "8"}, {"person_id": "7"},
                               {"chat_id": "8", "person_id": "7"}, {"chat_id": "9", "person_id": "7"}]:
                    assert (await service.refresh(platform="snowluma", **params))["ok"]
                assert service.chat("snowluma", "8")["name"] == "排球队"
                assert service.person_id("snowluma:7", group_id="8")["name"] == "队长"
                assert service.person_id("snowluma:7", group_id="9")["name"] == "班长"
                assert service.chat("snowluma", "7", chat_type="private")["name"] == "小周"
                assert calls[-1] == ("get_group_member_info", {"group_id": 9, "user_id": 7})
                assert len(calls) == 5
            finally:
                store.close()
    asyncio.run(check())
