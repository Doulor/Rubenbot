"""RSS 订阅插件测试：解析、去重、指令、合并转发、轮询（全部 mock，不联网）。"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from rubenbot.adapters.base import MessageEvent

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugins" / "rss"

_PKG = "rss_plugin_test"
_pkg = types.ModuleType(_PKG)
_pkg.__path__ = [str(PLUGIN_DIR)]
sys.modules[_PKG] = _pkg


def _load(name: str):
    """照生产 loader 的方式加载（submodule_search_locations 让相对导入生效）。"""
    full = f"{_PKG}.{name}"
    spec = importlib.util.spec_from_file_location(
        full,
        PLUGIN_DIR / f"{name}.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[full] = module
    spec.loader.exec_module(module)
    return module


store_mod = _load("store")
fetcher_mod = _load("fetcher")
plugin_mod = _load("plugin")

RSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
<title>测试博客</title>
<link>https://example.com/</link>
<description>测试</description>
<item>
<title>第一篇</title>
<link>https://example.com/1</link>
<guid>guid-1</guid>
<pubDate>Sat, 28 Nov 2026 10:00:00 GMT</pubDate>
<description><![CDATA[<p>这是<b>第一篇</b>的正文。</p><script>alert(1)</script>]]></description>
</item>
<item>
<title>第二篇</title>
<link>https://example.com/2</link>
<guid>guid-2</guid>
<pubDate>Sat, 28 Nov 2026 11:00:00 GMT</pubDate>
<description>第二篇纯文本正文</description>
</item>
</channel>
</rss>
"""

RSS_XML_NEW = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
<title>测试博客</title>
<link>https://example.com/</link>
<item>
<title>第三篇</title>
<link>https://example.com/3</link>
<guid>guid-3</guid>
<pubDate>Sat, 28 Nov 2026 12:00:00 GMT</pubDate>
<description>第三篇正文</description>
</item>
<item>
<title>第二篇</title>
<link>https://example.com/2</link>
<guid>guid-2</guid>
<pubDate>Sat, 28 Nov 2026 11:00:00 GMT</pubDate>
<description>第二篇纯文本正文</description>
</item>
<item>
<title>第一篇</title>
<link>https://example.com/1</link>
<guid>guid-1</guid>
<pubDate>Sat, 28 Nov 2026 10:00:00 GMT</pubDate>
<description>第一篇正文</description>
</item>
</channel>
</rss>
"""

RSS_XML_NO_GUID = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
<title>无 GUID 源</title>
<item>
<title>只有链接</title>
<link>https://example.com/noguid</link>
<pubDate>Sat, 28 Nov 2026 10:00:00 GMT</pubDate>
<description>正文</description>
</item>
</channel>
</rss>
"""

ATOM_XML = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<title>Atom 测试</title>
<link href="https://example.com/"/>
<entry>
<title>Atom 第一篇</title>
<link href="https://example.com/a1"/>
<id>atom-1</id>
<updated>2026-11-28T10:00:00Z</updated>
<content type="html">&lt;p&gt;Atom 正文&lt;/p&gt;</content>
</entry>
</feed>
"""

FEED_URL = "https://example.com/feed.xml"


class FakeAdapter:
    name = "snowluma"
    self_id = "99999"

    def __init__(self):
        self.actions: list[tuple[str, dict]] = []
        self.member_roles: dict[tuple[int, int], str] = {}

    async def call_action(self, name, params=None):
        self.actions.append((name, dict(params or {})))
        if name == "get_group_member_info":
            role = self.member_roles.get((params["group_id"], params["user_id"]), "member")
            return {"role": role}
        return {"message_id": "123"}

    def forward_calls(self, action):
        return [params for name, params in self.actions if name == action]


def make_fetch(mapping):
    """mapping: url -> xml 文本；缺失或 None 表示抓取失败。"""

    async def fetch(url, timeout=None):
        if url not in mapping or mapping[url] is None:
            raise RuntimeError("fetch failed")
        return SimpleNamespace(text=mapping[url])

    return fetch


def make_service(tmp_path, fetch, config=None):
    ctx = SimpleNamespace(
        extras={},
        config=SimpleNamespace(database_path=str(tmp_path / "rubenbot.db")),
        runtime=None,
    )
    cfg = config or plugin_mod.RssConfig()
    store = store_mod.RssStore(tmp_path / "rss.sqlite3")
    service = plugin_mod.RssService(ctx, cfg, store, fetch_page=fetch)
    return service, store


def make_runtime(adapter):
    return SimpleNamespace(adapters={"snowluma": adapter})


def make_event(text, *, chat_type="private", chat_id="u1", sender="u1", role=None):
    raw = {}
    if role is not None:
        raw = {"sender": {"role": role}}
    return MessageEvent(
        platform="snowluma",
        chat_type=chat_type,
        chat_id=chat_id,
        sender_id=sender,
        sender_name="测试用户",
        text=text,
        raw=raw,
    )


# ----------------------------------------------------------------------
# 解析
# ----------------------------------------------------------------------


def test_parse_rss2_fields_and_html_stripped():
    title, items = fetcher_mod.parse_feed(RSS_XML)
    assert title == "测试博客"
    assert len(items) == 2
    # 按发布时间倒序：第二篇在前
    assert items[0]["guid"] == "guid-2"
    assert items[0]["title"] == "第二篇"
    assert items[0]["link"] == "https://example.com/2"
    assert items[0]["content"] == "第二篇纯文本正文"
    # HTML 描述洗成纯文本，script 内容丢掉
    assert items[1]["guid"] == "guid-1"
    assert items[1]["content"] == "这是第一篇的正文。"
    assert "alert" not in items[1]["content"]
    assert "<" not in items[1]["content"]


def test_parse_atom_and_no_guid_uses_link():
    title, items = fetcher_mod.parse_feed(ATOM_XML)
    assert title == "Atom 测试"
    assert items[0]["guid"] == "atom-1"
    assert items[0]["content"] == "Atom 正文"
    title2, items2 = fetcher_mod.parse_feed(RSS_XML_NO_GUID)
    assert title2 == "无 GUID 源"
    assert items2[0]["guid"] == "https://example.com/noguid"


def test_parse_invalid_feed_raises():
    with pytest.raises(ValueError):
        fetcher_mod.parse_feed("这不是 feed")


def test_html_to_text_drops_script_and_collapses():
    text = fetcher_mod.html_to_text("<div><p>你好</p><script>var x = 1;</script><p>世界</p></div>")
    assert text == "你好\n世界"
    assert fetcher_mod.html_to_text("纯文本  多空格") == "纯文本 多空格"


# ----------------------------------------------------------------------
# 去重
# ----------------------------------------------------------------------


def test_new_entries_empty_water_returns_nothing():
    _, items = fetcher_mod.parse_feed(RSS_XML)
    assert fetcher_mod.new_entries(items, "", 5) == []


def test_new_entries_only_newer_than_water():
    _, items = fetcher_mod.parse_feed(RSS_XML_NEW)
    # 水位在 guid-2：只推更新的 guid-3
    fresh = fetcher_mod.new_entries(items, "guid-2", 5)
    assert [item["guid"] for item in fresh] == ["guid-3"]
    # 水位在最新：什么都不推
    assert fetcher_mod.new_entries(items, "guid-3", 5) == []


def test_new_entries_respects_max_items():
    _, items = fetcher_mod.parse_feed(RSS_XML_NEW)
    fresh = fetcher_mod.new_entries(items, "guid-1", 1)
    assert [item["guid"] for item in fresh] == ["guid-3"]


# ----------------------------------------------------------------------
# 指令：私聊
# ----------------------------------------------------------------------


def test_private_subscribe_and_list_and_unsubscribe(tmp_path):
    service, store = make_service(tmp_path, make_fetch({FEED_URL: RSS_XML}))
    adapter = FakeAdapter()
    runtime = make_runtime(adapter)
    event = make_event("/rss 订阅 https://example.com/feed.xml", sender="100")

    reply = asyncio.run(service.command(runtime, event, "订阅 https://example.com/feed.xml"))
    assert "已订阅" in reply and "测试博客" in reply
    row = store.get(FEED_URL, "private", "100")
    assert row is not None
    # 首次订阅水位设到最新一条，不推旧内容
    assert row["last_seen_guid"] == "guid-2"

    reply = asyncio.run(service.command(runtime, event, "列表"))
    assert "测试博客" in reply and "私聊" in reply

    reply = asyncio.run(service.command(runtime, event, "取消 https://example.com/feed.xml"))
    assert reply == "已取消订阅"
    assert store.get(FEED_URL, "private", "100") is None


def test_private_subscribe_to_group_by_number(tmp_path):
    service, store = make_service(tmp_path, make_fetch({FEED_URL: RSS_XML}))
    runtime = make_runtime(FakeAdapter())
    event = make_event("/rss 订阅 x", sender="100")
    reply = asyncio.run(service.command(runtime, event, "订阅 https://example.com/feed.xml 12345"))
    assert "已订阅" in reply
    assert store.get(FEED_URL, "group", "12345") is not None


def test_invalid_feed_not_subscribed(tmp_path):
    service, store = make_service(tmp_path, make_fetch({}))
    runtime = make_runtime(FakeAdapter())
    event = make_event("/rss 订阅 https://example.com/feed.xml", sender="100")
    reply = asyncio.run(service.command(runtime, event, "订阅 https://example.com/feed.xml"))
    assert reply == "这个源抓不到"
    assert store.list() == []


def test_private_check_pushes_new_items_as_forward(tmp_path):
    mapping = {FEED_URL: RSS_XML}
    service, store = make_service(tmp_path, make_fetch(mapping))
    adapter = FakeAdapter()
    runtime = make_runtime(adapter)
    event = make_event("/rss 订阅 x", sender="100")
    asyncio.run(service.command(runtime, event, "订阅 https://example.com/feed.xml"))
    # 源里多了一篇新的
    mapping[FEED_URL] = RSS_XML_NEW
    reply = asyncio.run(service.command(runtime, event, "检查"))
    assert reply == "已检查，新推 1 条"
    calls = adapter.forward_calls("send_private_forward_msg")
    assert len(calls) == 1
    params = calls[0]
    assert params["user_id"] == 100
    nodes = params["messages"]
    assert len(nodes) == 1
    node = nodes[0]
    assert node["type"] == "node"
    assert node["data"]["name"] == "测试博客"
    content = node["data"]["content"]
    assert "第三篇" in content and "第三篇正文" in content
    assert "https://example.com/3" in content
    # 水位已推到最新
    assert store.get(FEED_URL, "private", "100")["last_seen_guid"] == "guid-3"


def test_check_twice_second_pushes_nothing(tmp_path):
    mapping = {FEED_URL: RSS_XML_NEW}
    service, store = make_service(tmp_path, make_fetch(mapping))
    adapter = FakeAdapter()
    runtime = make_runtime(adapter)
    event = make_event("/rss 订阅 x", sender="100")
    asyncio.run(service.command(runtime, event, "订阅 https://example.com/feed.xml"))
    # 订阅时水位已是 guid-3；再检查一遍什么都不推
    reply = asyncio.run(service.command(runtime, event, "检查"))
    assert reply == "已检查，新推 0 条"
    assert adapter.forward_calls("send_private_forward_msg") == []


# ----------------------------------------------------------------------
# 指令：群聊管理员
# ----------------------------------------------------------------------


def test_group_admin_can_subscribe_member_refused(tmp_path):
    service, store = make_service(tmp_path, make_fetch({FEED_URL: RSS_XML}))
    adapter = FakeAdapter()
    runtime = make_runtime(adapter)
    admin_event = make_event(
        "/rss 订阅 x", chat_type="group", chat_id="777", sender="u1", role="admin"
    )
    reply = asyncio.run(service.command(runtime, admin_event, "订阅 https://example.com/feed.xml"))
    assert "已订阅" in reply
    assert store.get(FEED_URL, "group", "777") is not None

    member_event = make_event(
        "/rss 订阅 x", chat_type="group", chat_id="777", sender="u2", role="member"
    )
    reply = asyncio.run(service.command(runtime, member_event, "订阅 https://example.com/feed.xml"))
    assert reply == "只有群管理员能订阅"


def test_group_admin_checked_via_api_when_role_missing(tmp_path):
    service, store = make_service(tmp_path, make_fetch({FEED_URL: RSS_XML}))
    adapter = FakeAdapter()
    adapter.member_roles[(777, 42)] = "admin"
    runtime = make_runtime(adapter)
    # 事件里没有 role：走 get_group_member_info 查
    event = make_event("/rss 订阅 x", chat_type="group", chat_id="777", sender="42", role=None)
    reply = asyncio.run(service.command(runtime, event, "订阅 https://example.com/feed.xml"))
    assert "已订阅" in reply
    assert any(name == "get_group_member_info" for name, _ in adapter.actions)

    # 查出来是 member：拒绝
    adapter2 = FakeAdapter()
    runtime2 = make_runtime(adapter2)
    event2 = make_event("/rss 订阅 x", chat_type="group", chat_id="777", sender="43", role=None)
    reply = asyncio.run(service.command(runtime2, event2, "订阅 https://example.com/feed.xml"))
    assert reply == "只有群管理员能订阅"


def test_group_owner_can_subscribe_and_list_open_to_all(tmp_path):
    service, store = make_service(tmp_path, make_fetch({FEED_URL: RSS_XML}))
    runtime = make_runtime(FakeAdapter())
    owner_event = make_event(
        "/rss 订阅 x", chat_type="group", chat_id="777", sender="u1", role="owner"
    )
    assert "已订阅" in asyncio.run(
        service.command(runtime, owner_event, "订阅 https://example.com/feed.xml")
    )
    member_event = make_event(
        "/rss 列表", chat_type="group", chat_id="777", sender="u2", role="member"
    )
    reply = asyncio.run(service.command(runtime, member_event, "列表"))
    assert "测试博客" in reply


# ----------------------------------------------------------------------
# 合并转发结构
# ----------------------------------------------------------------------


def test_forward_packs_multiple_items_in_one_message(tmp_path):
    # 订阅时源只有两篇；随后源里一次多出两篇新的，包成一条转发两个节点。
    old_xml = RSS_XML
    # 构造“两篇新”的源：guid-4、guid-3 在 guid-2 之上
    two_new = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>测试博客</title>
<item><title>第四篇</title><link>https://example.com/4</link><guid>guid-4</guid>
<pubDate>Sat, 28 Nov 2026 13:00:00 GMT</pubDate><description>第四篇正文</description></item>
<item><title>第三篇</title><link>https://example.com/3</link><guid>guid-3</guid>
<pubDate>Sat, 28 Nov 2026 12:00:00 GMT</pubDate><description>第三篇正文</description></item>
<item><title>第二篇</title><link>https://example.com/2</link><guid>guid-2</guid>
<pubDate>Sat, 28 Nov 2026 11:00:00 GMT</pubDate><description>第二篇正文</description></item>
</channel></rss>
"""
    mapping = {FEED_URL: old_xml}
    service, _ = make_service(tmp_path, make_fetch(mapping))
    adapter = FakeAdapter()
    runtime = make_runtime(adapter)
    event = make_event("/rss 订阅 x", sender="100")
    asyncio.run(service.command(runtime, event, "订阅 https://example.com/feed.xml"))
    mapping[FEED_URL] = two_new
    reply = asyncio.run(service.command(runtime, event, "检查"))
    assert reply == "已检查，新推 2 条"
    calls = adapter.forward_calls("send_private_forward_msg")
    assert len(calls) == 1, "多条新文章只发一条合并转发"
    nodes = calls[0]["messages"]
    assert len(nodes) == 2
    contents = [node["data"]["content"] for node in nodes]
    assert "第三篇" in contents[0] and "https://example.com/3" in contents[0]
    assert "第四篇" in contents[1] and "https://example.com/4" in contents[1]
    assert all(node["data"]["name"] == "测试博客" for node in nodes)


def test_single_item_still_uses_forward(tmp_path):
    mapping = {FEED_URL: RSS_XML}
    service, _ = make_service(tmp_path, make_fetch(mapping))
    adapter = FakeAdapter()
    runtime = make_runtime(adapter)
    event = make_event("/rss 订阅 x", sender="100")
    asyncio.run(service.command(runtime, event, "订阅 https://example.com/feed.xml"))
    mapping[FEED_URL] = RSS_XML_NEW
    asyncio.run(service.command(runtime, event, "检查"))
    assert len(adapter.forward_calls("send_private_forward_msg")) == 1
    # 没走普通 send_group_msg / send_private_msg
    assert not [n for n, _ in adapter.actions if n in ("send_group_msg", "send_private_msg")]


def test_group_forward_uses_group_action(tmp_path):
    mapping = {FEED_URL: RSS_XML}
    service, _ = make_service(tmp_path, make_fetch(mapping))
    adapter = FakeAdapter()
    runtime = make_runtime(adapter)
    event = make_event("/rss 订阅 x", chat_type="group", chat_id="777", sender="u1", role="admin")
    asyncio.run(service.command(runtime, event, "订阅 https://example.com/feed.xml"))
    mapping[FEED_URL] = RSS_XML_NEW
    asyncio.run(service.command(runtime, event, "检查"))
    calls = adapter.forward_calls("send_group_forward_msg")
    assert len(calls) == 1
    assert calls[0]["group_id"] == 777


# ----------------------------------------------------------------------
# 轮询
# ----------------------------------------------------------------------


def test_poll_single_source_failure_does_not_block_others(tmp_path):
    bad_url = "https://example.com/bad.xml"
    mapping = {FEED_URL: RSS_XML, bad_url: None}
    service, store = make_service(tmp_path, make_fetch(mapping))
    adapter = FakeAdapter()
    runtime = make_runtime(adapter)
    event = make_event("/rss 订阅 x", sender="100")
    asyncio.run(service.command(runtime, event, "订阅 https://example.com/feed.xml"))
    # 坏源直接写进订阅表（绕过订阅验证）
    store.add(bad_url, "private", "100", feed_title="坏源")
    mapping[FEED_URL] = RSS_XML_NEW
    pushed = asyncio.run(service.check_all(runtime))
    assert pushed == 1
    assert store.get(bad_url, "private", "100")["fail_count"] == 1
    assert store.get(FEED_URL, "private", "100")["fail_count"] == 0


def test_poll_marks_failed_five_times_and_recovers(tmp_path):
    bad_url = "https://example.com/bad.xml"
    mapping = {bad_url: None}
    service, store = make_service(tmp_path, make_fetch(mapping))
    adapter = FakeAdapter()
    runtime = make_runtime(adapter)
    store.add(bad_url, "private", "100", feed_title="坏源")
    for _ in range(5):
        asyncio.run(service.check_all(runtime))
    row = store.get(bad_url, "private", "100")
    assert row["fail_count"] == 5
    # 订阅还在，没被删
    assert row is not None
    # 源恢复后自动续推：失败计数清零
    mapping[bad_url] = RSS_XML
    pushed = asyncio.run(service.check_all(runtime))
    assert store.get(bad_url, "private", "100")["fail_count"] == 0
    assert pushed == 0  # 首次恢复只设水位，不推旧文章


def test_poll_loop_sleeps_configured_interval(tmp_path):
    service, _ = make_service(
        tmp_path,
        make_fetch({}),
        config=plugin_mod.RssConfig(poll_interval_minutes=30),
    )
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        sleeps.append(delay)
        service._stopping = True

    async def run():
        import asyncio as _asyncio

        _asyncio.sleep = fake_sleep
        try:
            await service._poll_loop()
        finally:
            _asyncio.sleep = real_sleep

    asyncio.run(run())
    assert sleeps == [1800.0]


def test_poll_loop_stop_cancels_task(tmp_path):
    service, _ = make_service(tmp_path, make_fetch({}))

    async def run():
        service.start()
        assert service._poll_task is not None
        await service.stop()
        assert service._poll_task is None or service._poll_task.done()

    asyncio.run(run())


# ----------------------------------------------------------------------
# 订阅表
# ----------------------------------------------------------------------


def test_store_same_feed_multiple_targets(tmp_path):
    store = store_mod.RssStore(tmp_path / "rss.sqlite3")
    store.add(FEED_URL, "private", "u1", feed_title="测试博客")
    store.add(FEED_URL, "group", "777", feed_title="测试博客")
    assert len(store.list()) == 2
    assert len(store.list(target_type="group", target_id="777")) == 1
    assert store.remove(FEED_URL, "group", "777") is True
    assert store.remove(FEED_URL, "group", "777") is False
    assert len(store.list()) == 1


# ----------------------------------------------------------------------
# WebUI 订阅列表接口
# ----------------------------------------------------------------------


def _webui_client(tmp_path, *, enabled=True):
    """建一个挂了 RSS 插件管理器（或没挂）的 WebUI TestClient。"""
    from fastapi.testclient import TestClient

    from rubenbot.config import Config
    from rubenbot.memory.store import MemoryStore
    from rubenbot.plugins.loader import PluginManager
    from rubenbot.web.app import create_app
    from rubenbot.web.state import WebState

    cfg = Config()
    cfg.webui.host = "127.0.0.1"
    cfg.webui.port = 8765
    cfg.webui.password = ""
    cfg.persona_path = str(tmp_path / "SOUL.md")
    store = MemoryStore(":memory:")
    state = WebState(config=cfg, store=store, config_path=tmp_path / "config.yaml")
    if enabled:
        manager = PluginManager(cfg, store)
        manager.extras["rss_db_path"] = str(tmp_path / "rss.sqlite3")
        manager.config.plugins.enabled = ["rss"]
        manager.load_all()
        service = manager.extras["rss"]
        service.store.add(
            FEED_URL, "group", "123456", feed_title="测试博客", created_by="111"
        )
        state.plugins = manager
    return TestClient(create_app(state))


def test_webui_list_subscriptions(tmp_path):
    client = _webui_client(tmp_path)
    resp = client.get("/api/plugins/rss/subscriptions")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["feed_url"] == FEED_URL
    assert items[0]["target_type"] == "group"
    assert items[0]["target_id"] == "123456"


def test_webui_delete_subscription(tmp_path):
    client = _webui_client(tmp_path)
    resp = client.delete(
        "/api/plugins/rss/subscriptions",
        params={"feed_url": FEED_URL, "target_type": "group", "target_id": "123456"},
    )
    assert resp.status_code == 200
    assert client.get("/api/plugins/rss/subscriptions").json()["items"] == []


def test_webui_delete_missing_subscription(tmp_path):
    client = _webui_client(tmp_path)
    resp = client.delete(
        "/api/plugins/rss/subscriptions",
        params={"feed_url": "https://nope.example/x.xml", "target_type": "group", "target_id": "123456"},
    )
    assert resp.status_code == 404


def test_webui_list_when_plugin_disabled(tmp_path):
    client = _webui_client(tmp_path, enabled=False)
    assert client.get("/api/plugins/rss/subscriptions").status_code == 409
    assert (
        client.delete(
            "/api/plugins/rss/subscriptions",
            params={"feed_url": FEED_URL, "target_type": "group", "target_id": "123456"},
        ).status_code
        == 409
    )
