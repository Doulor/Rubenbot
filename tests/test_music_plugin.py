"""点歌插件：触发识别、搜索结果解析、冷却、音乐段与降级路径。"""

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx

from rubenbot.adapters.base import MessageEvent
from rubenbot.adapters.snowluma import segment_to_onebot
from rubenbot.config import Config
from rubenbot.plugins.loader import PluginManager, load_manifest

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugins" / "music"
PLUGIN_FILE = PLUGIN_DIR / "plugin.py"
LOGGER_NAME = "music_plugin"


def load_plugin_module():
    """按 loader 的方式从文件加载 plugin.py，拿到独立的模块对象。"""
    spec = importlib.util.spec_from_file_location(LOGGER_NAME, PLUGIN_FILE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[LOGGER_NAME] = module
    spec.loader.exec_module(module)
    return module


plugin = load_plugin_module()


# ----------------------------------------------------------------------
# 假对象
# ----------------------------------------------------------------------


class RecordingAdapter:
    """记录每次 send 的会话与消息段；``fail_music`` 模拟协议端不支持音乐段。"""

    def __init__(self, name="snowluma", *, fail_music=False):
        self.name = name
        self.fail_music = fail_music
        self.sent: list[tuple[str, str, list]] = []

    async def send(self, chat_id, segments, *, chat_type="group"):
        if self.fail_music and any(segment["type"] == "music" for segment in segments):
            raise RuntimeError("协议端未实现音乐段")
        self.sent.append((chat_id, chat_type, list(segments)))

    @property
    def texts(self):
        return ["".join(segment.render() for segment in segments) for _, _, segments in self.sent]


class FakeClock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def runtime(adapter) -> SimpleNamespace:
    return SimpleNamespace(adapters={adapter.name: adapter})


def event(text="点歌 晴天", *, sender_id="u1", chat_id="g1", platform="snowluma") -> MessageEvent:
    return MessageEvent(
        platform=platform,
        chat_type="group",
        chat_id=chat_id,
        sender_id=sender_id,
        sender_name="阿花",
        text=text,
    )


SEARCH_HIT = {
    "result": {
        "songs": [
            {
                "id": 186016,
                "name": "晴天",
                "ar": [{"name": "周杰伦"}],
                "al": {"picUrl": "https://p1.music.126.net/cover.jpg"},
            }
        ]
    }
}


def make_player(*, response=None, status=200, adapter=None, config=None, clock=None):
    """构造插件实例；HTTP 全部走 MockTransport。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if status >= 400:
            return httpx.Response(status, json={"code": status})
        payload = SEARCH_HIT if response is None else response
        return httpx.Response(200, json=payload)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    player = plugin.MusicPlayer(config or {}, client=client, clock=clock or FakeClock())
    return player, adapter or RecordingAdapter()


# ----------------------------------------------------------------------
# 触发识别
# ----------------------------------------------------------------------


def test_parse_trigger_basic():
    triggers = ("点歌", "我想听")
    assert plugin.parse_trigger("点歌 晴天", triggers) == "晴天"
    assert plugin.parse_trigger("我想听 晴天", triggers) == "晴天"
    assert plugin.parse_trigger("点歌：稻香", triggers) == "稻香"
    assert plugin.parse_trigger("我想听《起风了》", triggers) == "《起风了》"
    # 只有触发词没带歌名 -> 空串
    assert plugin.parse_trigger("点歌", triggers) == ""
    assert plugin.parse_trigger("点歌   ", triggers) == ""
    # 不相关消息不触发
    assert plugin.parse_trigger("今天天气不错", triggers) is None
    # 「我想听歌」只表达想听、没点具体哪首 -> 空串（回一句提示，不拿「歌」去搜）
    assert plugin.parse_trigger("我想听歌", triggers) == ""
    assert plugin.parse_trigger("点歌 一首歌", triggers) == ""
    assert plugin.parse_trigger("", triggers) is None


def test_parse_trigger_longest_first():
    assert plugin.parse_trigger("点歌单 晴天", ("点歌", "点歌单")) == "晴天"


def test_parse_search_response():
    song = plugin.parse_search_response(SEARCH_HIT)
    assert song is not None
    assert song.song_id == "186016"
    assert song.name == "晴天"
    assert song.artists == ("周杰伦",)
    assert song.cover_url == "https://p1.music.126.net/cover.jpg"
    assert song.summary() == "晴天 — 周杰伦"
    assert song.link == "https://music.163.com/#/song?id=186016"


def test_parse_search_response_variants():
    # 空结果 / 结构不对
    assert plugin.parse_search_response({"result": {"songs": []}}) is None
    assert plugin.parse_search_response({"result": {}}) is None
    assert plugin.parse_search_response({"code": 200}) is None
    assert plugin.parse_search_response(None) is None
    # 顶层 songs（部分镜像实现）
    song = plugin.parse_search_response({"songs": [{"id": 1, "name": "无题"}]})
    assert song is not None and song.name == "无题"
    assert song.artists == () and song.artist_text == "未知歌手"
    # 缺 id 的条目跳过第一首（视为无效）
    assert plugin.parse_search_response({"result": {"songs": [{"name": "x"}]}}) is None


def test_config_parsing_and_defaults():
    cfg = plugin.MusicConfig.from_mapping(None)
    assert cfg.triggers == plugin.DEFAULT_TRIGGERS
    assert cfg.cooldown_seconds == 60
    assert cfg.source == "netease"
    cfg = plugin.MusicConfig.from_mapping(
        {"triggers": ["来一首"], "cooldown_seconds": 0, "source": "kkbox"}
    )
    assert cfg.triggers == ("来一首",)
    assert cfg.cooldown_seconds == 0
    assert cfg.source == "netease"  # 不支持的音源回落到 netease
    # 类型不对的项退回默认值
    cfg = plugin.MusicConfig.from_mapping({"triggers": "点歌", "cooldown_seconds": "abc"})
    assert cfg.triggers == ("点歌",) and cfg.cooldown_seconds == 60


def test_shipped_config_example_is_valid():
    cfg = plugin.read_config(PLUGIN_DIR)
    assert cfg.triggers == ("点歌", "我想听")
    assert cfg.cooldown_seconds == 60


def test_manifest_is_market_compliant():
    manifest = load_manifest(PLUGIN_DIR)
    assert manifest.id == "music"
    assert set(manifest.permissions) == {"read_messages", "send_messages", "network"}
    assert manifest.raw.get("config_schema")
    assert {item["key"] for item in manifest.config_schema} == {
        "triggers", "cooldown_seconds", "source"
    }


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------


def test_song_uses_music_segment_on_snowluma():
    player, adapter = make_player()
    asyncio.run(player.on_message(runtime(adapter), event("点歌 晴天")))

    assert len(adapter.sent) == 1
    chat_id, chat_type, segments = adapter.sent[0]
    assert (chat_id, chat_type) == ("g1", "group")
    assert segments == [{"type": "music", "data": {"type": "netease", "id": "186016"}}]
    # 转成 OneBot 报文后是标准的 music 段
    assert segment_to_onebot(segments[0]) == {
        "type": "music",
        "data": {"type": "netease", "id": "186016"},
    }


def test_non_snowluma_falls_back_to_text_and_cover():
    adapter = RecordingAdapter(name="telegram")
    player, _ = make_player(adapter=adapter)

    asyncio.run(player.on_message(runtime(adapter), event("我想听 晴天", platform="telegram")))

    assert len(adapter.sent) == 1
    segments = adapter.sent[0][2]
    assert segments[0]["type"] == "text"
    assert "晴天 — 周杰伦" in segments[0]["data"]["text"]
    assert "https://music.163.com/#/song?id=186016" in segments[0]["data"]["text"]
    assert segments[1] == {
        "type": "image",
        "data": {"url": "https://p1.music.126.net/cover.jpg"},
    }


def test_music_segment_failure_degrades_to_text(caplog):
    adapter = RecordingAdapter(fail_music=True)
    player, _ = make_player(adapter=adapter)

    asyncio.run(player.on_message(runtime(adapter), event("点歌 晴天")))

    # 第一次发音乐段抛异常，降级再发一次文字 + 封面
    assert len(adapter.sent) == 1
    segments = adapter.sent[0][2]
    assert segments[0]["type"] == "text" and "晴天 — 周杰伦" in segments[0]["data"]["text"]


def test_no_results_replies_not_found():
    player, adapter = make_player(response={"result": {"songs": []}})

    asyncio.run(player.on_message(runtime(adapter), event("点歌 不存在的歌")))

    assert adapter.texts == ["没找到《不存在的歌》，换个关键词试试"]
    assert all(
        segment["type"] == "text" for _, _, segments in adapter.sent for segment in segments
    )


def test_http_error_replies_search_failed_with_log(caplog):
    caplog.set_level("WARNING", logger=LOGGER_NAME)
    player, adapter = make_player(status=503)

    asyncio.run(player.on_message(runtime(adapter), event("点歌 晴天")))

    assert adapter.texts == ["搜不到，换个关键词试试"]
    assert any("搜索" in record.message for record in caplog.records)


def test_timeout_replies_search_failed(monkeypatch):
    def handler(request):
        raise httpx.ReadTimeout("超时", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    player = plugin.MusicPlayer({}, client=client, clock=FakeClock())
    adapter = RecordingAdapter()

    asyncio.run(player.on_message(runtime(adapter), event("点歌 晴天")))
    assert adapter.texts == ["搜不到，换个关键词试试"]


def test_trigger_without_song_name_asks_for_name():
    player, adapter = make_player()
    asyncio.run(player.on_message(runtime(adapter), event("点歌")))
    assert adapter.texts == ["想听哪首歌？说「点歌 歌名」就好"]


def test_unrelated_message_is_not_intercepted():
    player, adapter = make_player()
    result = asyncio.run(player.on_message(runtime(adapter), event("今天天气不错")))
    assert result is None
    assert adapter.sent == []


# ----------------------------------------------------------------------
# 冷却
# ----------------------------------------------------------------------


def test_cooldown_blocks_second_request():
    clock = FakeClock()
    player, adapter = make_player(clock=clock)

    asyncio.run(player.on_message(runtime(adapter), event("点歌 晴天", sender_id="u1")))
    assert len(adapter.sent) == 1

    # 冷却内：不再搜索，只回一句剩余等待
    clock.now += 20
    asyncio.run(player.on_message(runtime(adapter), event("点歌 稻香", sender_id="u1")))
    assert len(adapter.sent) == 2
    assert adapter.texts[-1] == "刚点过啦，40 秒后再点吧"

    # 其他人不受影响
    asyncio.run(player.on_message(runtime(adapter), event("点歌 稻香", sender_id="u2")))
    assert adapter.sent[-1][2] == [
        {"type": "music", "data": {"type": "netease", "id": "186016"}}
    ]

    # 冷却结束（默认 60 秒）后可以再点
    clock.now += 40
    asyncio.run(player.on_message(runtime(adapter), event("点歌 稻香", sender_id="u1")))
    assert adapter.sent[-1][2][0]["type"] == "music"


def test_cooldown_starts_even_if_search_fails():
    clock = FakeClock()
    player, adapter = make_player(status=500, clock=clock)

    asyncio.run(player.on_message(runtime(adapter), event("点歌 晴天")))
    assert adapter.texts == ["搜不到，换个关键词试试"]

    clock.now += 10
    asyncio.run(player.on_message(runtime(adapter), event("点歌 晴天")))
    assert adapter.texts[-1] == "刚点过啦，50 秒后再点吧"


# ----------------------------------------------------------------------
# 注册与宿主集成
# ----------------------------------------------------------------------


class StubCtx:
    def __init__(self, path: Path):
        self.extras = {}
        self.plugin = SimpleNamespace(path=path)
        self.message_hooks = []
        self.stop_hooks = []

    def on_message(self, handler):
        self.message_hooks.append(handler)
        return handler

    def on_stop(self, handler):
        self.stop_hooks.append(handler)
        return handler


def test_register_wires_hooks():
    ctx = StubCtx(PLUGIN_DIR)
    plugin.register(ctx)
    assert len(ctx.message_hooks) == 1
    assert len(ctx.stop_hooks) == 1
    assert isinstance(ctx.extras["music"], plugin.MusicPlayer)


def test_loader_loads_music_plugin():
    config = Config()
    config.plugins.dir = str(PLUGIN_DIR.parent)
    config.plugins.enabled = ["music"]
    manager = PluginManager(config, store=None)
    loaded = manager.load_all()
    assert [item.name for item in loaded] == ["music"]
    assert manager.plugins["music"].valid


def test_beginner_smoke_search_via_real_client_shape():
    """搜索请求带上 UA / Referer 与 query，接口返回体按 cloudsearch 结构解析。"""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["ua"] = request.headers.get("user-agent")
        seen["referer"] = request.headers.get("referer")
        return httpx.Response(200, json=SEARCH_HIT)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    player = plugin.MusicPlayer({}, client=client)

    song = asyncio.run(player.search("晴天"))
    assert song is not None and song.name == "晴天"
    assert "cloudsearch/pc" in seen["url"]
    assert "s=%E6%99%B4%E5%A4%A9" in seen["url"]
    assert "Mozilla" in seen["ua"] and "music.163.com" in seen["referer"]
    asyncio.run(client.aclose())


def test_aclose_releases_owned_client():
    player = plugin.MusicPlayer({})
    asyncio.run(player.aclose())  # 没建过客户端也不报错
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    player = plugin.MusicPlayer({}, client=client)
    asyncio.run(player.aclose())
    assert client.is_closed is False  # 注入的客户端不归插件关
    asyncio.run(client.aclose())


def test_snowluma_music_segment_roundtrip():
    """段转换是纯函数，不依赖真实客户端。"""
    from rubenbot.adapters.base import Segment

    data = segment_to_onebot(Segment("music", {"type": "netease", "id": 123}))
    assert data == {"type": "music", "data": {"type": "netease", "id": "123"}}
    # 未知字段不会带进报文
    data = segment_to_onebot(Segment("music", {"type": "qq", "id": 1, "junk": "x"}))
    assert data == {"type": "music", "data": {"type": "qq", "id": "1"}}


def test_snowluma_render_placeholder():
    from rubenbot.adapters.base import Segment

    assert Segment("music", {"type": "netease", "id": "1"}).render() == "[音乐]"


# ----------------------------------------------------------------------
# 宿主链路：handle_event → 插件钩子 → 发送
# ----------------------------------------------------------------------


def test_end_to_end_via_host_message_loop(tmp_path):
    """走真实的宿主主循环：点歌消息被插件拦下并发音乐段，不惊动模型。"""
    from rubenbot.core import RubenBot
    from rubenbot.memory.store import MemoryStore
    from rubenbot.persona import Persona

    from conftest import RecordingAdapter, ScriptedLLM

    class SnowlumaRecordingAdapter(RecordingAdapter):
        """名字是 snowluma 的假适配器：走音乐段而非文字降级。"""

        name = "snowluma"

    root = tmp_path / "plugins"
    plugin_dir = root / "music"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.py").write_text(PLUGIN_FILE.read_text(encoding="utf-8"), encoding="utf-8")
    (plugin_dir / "manifest.json").write_text(
        (PLUGIN_DIR / "manifest.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (plugin_dir / "config.yaml").write_text("cooldown_seconds: 0\n", encoding="utf-8")

    config = Config()
    config.plugins.dir = str(root)
    config.plugins.enabled = ["music"]
    config.reply.segment_delay_enabled = False
    config.decision.merge_window_seconds = 0
    config.decision.hard_merge_window_seconds = 0
    manager = PluginManager(config, MemoryStore(":memory:"))
    manager.load_all()
    llm = ScriptedLLM({"chat": ["不该出现"]})
    bot = RubenBot(config, manager.store, llm, Persona("Ruben", "你是 Ruben"), plugins=manager)
    adapter = SnowlumaRecordingAdapter()
    bot.register_adapter(adapter)
    manager.extras["bot"] = bot

    # 插件里的 HTTP 客户端换成 MockTransport（不碰真实网络）
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=SEARCH_HIT)))
    manager.extras["music"]._client = client

    message = MessageEvent(
        platform="snowluma",
        chat_type="private",
        chat_id="u1",
        sender_id="u1",
        sender_name="阿花",
        text="我想听 晴天",
    )
    asyncio.run(bot.handle_event(message))

    assert llm.calls == []
    assert len(adapter.sent) == 1
    assert adapter.sent[0][2] == [
        {"type": "music", "data": {"type": "netease", "id": "186016"}}
    ]
    asyncio.run(client.aclose())
