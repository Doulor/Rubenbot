"""点歌插件（hydrogen 复刻版）：命令面、选歌列表、探活过滤、发送链与超时撤回。"""

import asyncio
import base64
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from rubenbot.adapters.base import MessageEvent
from rubenbot.config import Config
from rubenbot.plugins.loader import PluginManager, load_manifest

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugins" / "music"
PLUGIN_FILE = PLUGIN_DIR / "plugin.py"
MODULE_NAME = "rubenbot_plugin_music_test"
LOGGER_NAME = "rubenbot.plugin.music"


def load_plugin_module():
    """按 loader 的方式加载插件包（相对导入需要 submodule_search_locations）。"""
    spec = importlib.util.spec_from_file_location(
        MODULE_NAME, PLUGIN_FILE, submodule_search_locations=[str(PLUGIN_DIR)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


plugin = load_plugin_module()


# ----------------------------------------------------------------------
# 假对象
# ----------------------------------------------------------------------


class RecordingAdapter:
    """记录 send / send_file / call_action；可按需模拟某一类段发送失败。"""

    def __init__(
        self,
        name="snowluma",
        *,
        fail_record=False,
        fail_file=False,
        fail_image=False,
        fail_music=False,
        fail_action=False,
    ):
        self.name = name
        self.fail_record = fail_record
        self.fail_file = fail_file
        self.fail_image = fail_image
        self.fail_music = fail_music
        self.fail_action = fail_action
        self.sent: list[tuple[str, str, list]] = []
        self.files: list[tuple[str, str, bytes]] = []
        self.actions: list[tuple[str, dict]] = []

    def set_platform_event_callback(self, callback):
        """宿主注册适配器时会调；点歌插件用不到事件通道，收下即可。"""
        self.platform_event_callback = callback

    async def send(self, chat_id, segments, *, chat_type="group"):
        types = {segment["type"] for segment in segments}
        if self.fail_record and "record" in types:
            raise RuntimeError("语音转码失败")
        if self.fail_music and "music" in types:
            raise RuntimeError("协议端未实现音乐段")
        if self.fail_image and "image" in types:
            raise RuntimeError("封面图上传失败")
        self.sent.append((chat_id, chat_type, list(segments)))
        return f"mid-{len(self.sent)}"

    async def send_file(self, chat_id, filename, content, *, chat_type):
        if self.fail_file:
            raise RuntimeError("文件上传失败")
        self.files.append((chat_id, filename, content))

    async def call_action(self, name, params=None):
        if self.fail_action:
            raise RuntimeError("动作未授权")
        self.actions.append((name, dict(params or {})))
        return {"message_id": 1}

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


def netease_payload(*songs) -> dict:
    return {
        "result": {
            "songs": [
                {
                    "id": song[0],
                    "name": song[1],
                    "ar": [{"name": song[2]}],
                    "al": {"picUrl": "https://p1.music.126.net/cover.jpg"},
                    "dt": 268000,
                }
                for song in songs
            ]
        }
    }


def txqq_payload(*rows) -> dict:
    return {
        "data": [
            {
                "songid": row[0],
                "title": row[1],
                "author": row[2],
                "url": row[3],
                "link": f"https://y.qq.com/n/yqq/song/{row[0]}.html",
                "pic": "https://y.gtimg.cn/music/photo_new/T002R300x300M000x.jpg",
            }
            for row in rows
        ]
    }


COVER_BYTES = b"\xff\xd8\xff\xe0" + b"fake-jpeg" * 8
AUDIO_BYTES = b"ID3\x03\x00\x00\x00" + b"audio-frame" * 16


class FakeNet:
    """一个假网络：搜索、探活、封面、音频下载都按 URL 分派。"""

    def __init__(
        self,
        *,
        netease=None,
        txqq=None,
        search_status=200,
        unplayable_ids=(),
        cover=None,
        probe_status=200,
        download_status=200,
    ):
        self.netease = netease if netease is not None else netease_payload((1, "晴天", "周杰伦"))
        self.txqq = txqq if txqq is not None else txqq_payload(
            ("qq-1", "晴天", "周杰伦", "http://aqqmusic.tc.qq.com/C4001.m4a")
        )
        self.search_status = search_status
        self.unplayable_ids = {str(item) for item in unplayable_ids}
        self.cover = cover
        self.probe_status = probe_status
        self.download_status = download_status
        self.calls: list[tuple[str, str]] = []

    @staticmethod
    def _song_id(url: str) -> str:
        for marker in ("id=", "/C400", "/C4000"):
            if marker in url:
                tail = url.split(marker, 1)[1]
                return tail.split(".", 1)[0].split("?", 1)[0]
        return ""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append((request.method, url))
        if "cloudsearch" in url:
            if self.search_status >= 400:
                return httpx.Response(self.search_status, json={"code": self.search_status})
            return httpx.Response(200, json=self.netease)
        if "music.txqq.pro" in url:
            if self.search_status >= 400:
                return httpx.Response(self.search_status, json={"code": self.search_status})
            return httpx.Response(200, json=self.txqq)
        if "song/media/outer/url" in url or "aqqmusic.tc.qq.com" in url:
            if self._song_id(url) in self.unplayable_ids:
                return httpx.Response(
                    200, content=b"<html>404</html>", headers={"content-type": "text/html"}
                )
            # 探活是 HEAD 或带 Range 的小 GET；整段下载才走 download_status
            probing = request.method == "HEAD" or "range" in request.headers
            status = self.probe_status if probing else self.download_status
            if status >= 400:
                return httpx.Response(status, content=b"nope")
            return httpx.Response(
                200,
                content=AUDIO_BYTES,
                headers={"content-type": "audio/mpeg", "content-length": str(len(AUDIO_BYTES))},
            )
        if "music.126.net" in url or "gtimg.cn" in url or "cover" in url:
            if callable(self.cover):
                return self.cover(request)
            if isinstance(self.cover, httpx.Response):
                return self.cover
            return httpx.Response(
                200,
                content=COVER_BYTES if self.cover is None else self.cover,
                headers={"content-type": "image/jpeg"},
            )
        if "comments" in url:
            return httpx.Response(200, json={"hotComments": [{"content": "热评一句"}]})
        return httpx.Response(404, content=b"not found")


def make_player(*, config=None, net=None, adapter=None, clock=None):
    net = net or FakeNet()
    client = httpx.AsyncClient(transport=httpx.MockTransport(net))
    settings = plugin.MusicConfig.from_mapping(
        {"enable_comments": False, **(config or {})}
    )
    player = plugin.MusicPlayer(settings, client=client, clock=clock or FakeClock())
    return player, adapter or RecordingAdapter(), net


def run(coro):
    return asyncio.run(coro)


# ----------------------------------------------------------------------
# 命令面
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "song", "index", "source", "platform_type"),
    [
        ("点歌 晴天", "晴天", 0, "netease", "netease"),
        ("点歌晴天", "晴天", 0, "netease", "netease"),
        ("点歌：稻香", "稻香", 0, "netease", "netease"),
        ("我想听 后来", "后来", 0, "netease", "netease"),
        ("网易点歌 起风了", "起风了", 0, "netease", "netease"),
        ("网易云 起风了", "起风了", 0, "netease", "netease"),
        ("QQ点歌 稻香", "稻香", 0, "txqq", "qq"),
        ("qq点歌 稻香 2", "稻香", 2, "txqq", "qq"),
        ("酷狗 七里香", "七里香", 0, "txqq", "kugou"),
        ("酷我点歌 夜曲", "夜曲", 0, "txqq", "kuwo"),
        ("百度 演员", "演员", 0, "txqq", "baidu"),
        ("一听 童话", "童话", 0, "txqq", "1ting"),
        ("咪咕 后来", "后来", 0, "txqq", "migu"),
        ("荔枝 故事", "故事", 0, "txqq", "lizhi"),
        ("蜻蜓 睡前", "睡前", 0, "txqq", "qingting"),
        ("喜马拉雅 三国", "三国", 0, "txqq", "ximalaya"),
        ("5sing原创 demo", "demo", 0, "txqq", "5singyc"),
        ("5sing翻唱 demo", "demo", 0, "txqq", "5singfc"),
        ("全民K歌 童话", "童话", 0, "txqq", "kg"),
    ],
)
def test_parse_platform_commands(text, song, index, source, platform_type):
    request = plugin.parse_request(text)
    assert request is not None
    assert (request.song, request.index, request.source, request.platform_type) == (
        song,
        index,
        source,
        platform_type,
    )


def test_parse_rejects_plain_chat_and_empty_song():
    # 裸平台名后面没有分隔，按闲聊处理
    assert plugin.parse_request("酷狗音乐不错") is None
    assert plugin.parse_request("今天天气不错") is None
    assert plugin.parse_request("") is None
    # 只说了平台没给歌名：返回空歌名，由调用方回一句提示
    for text in ("点歌", "点歌  ", "我想听", "我想听歌", "点歌 一首歌"):
        request = plugin.parse_request(text)
        assert request is not None and request.song == ""


def test_parse_longest_alias_wins():
    # 「酷狗点歌」不能被「酷狗」抢走，也不能被「点歌」抢走
    assert plugin.parse_request("酷狗点歌 晴天").platform_type == "kugou"
    assert plugin.parse_request("喜马拉雅点歌 三国").platform_type == "ximalaya"


def test_parse_uses_default_player():
    request = plugin.parse_request("点歌 晴天", default_player="QQ点歌")
    assert (request.source, request.platform_type) == ("txqq", "qq")
    # 平台显示名、带括号说明的写法都能认
    assert plugin.parse_request("点歌 晴天", default_player="QQ音乐").platform_type == "qq"
    assert (
        plugin.parse_request("点歌 晴天", default_player="酷狗点歌（默认）").platform_type
        == "kugou"
    )
    # 配置只写「点歌」= 默认网易云，不能被「喜马拉雅点歌」这类更长的别名抢走
    assert plugin.parse_request("点歌 晴天", default_player="点歌").platform_type == "netease"
    # 未知默认平台退回网易云
    request = plugin.parse_request("点歌 晴天", default_player="不存在的平台")
    assert request.source == "netease"


def test_parse_lyrics_request():
    assert plugin.parse_lyrics_request("查歌词 晴天") == "晴天"
    assert plugin.parse_lyrics_request("歌词：晴天") == "晴天"
    assert plugin.parse_lyrics_request("查歌词") == ""
    assert plugin.parse_lyrics_request("今天天气不错") is None


def test_config_defaults_and_normalization():
    config = plugin.MusicConfig.from_mapping(None)
    assert config.send_modes == ("record", "file", "text")
    assert config.default_player == "网易点歌"
    assert config.timeout == 30 and config.cooldown_seconds == 60
    assert config.real_song_limit == 5

    config = plugin.MusicConfig.from_mapping(
        {
            "send_modes": ["card(卡片模式)", "语音", "文件", "text"],
            "select_mode": "single",
            "song_limit": 100,
            "timeout": 1,
            "cooldown_seconds": "abc",
            "enable_comments": "yes",
        }
    )
    assert config.send_modes == ("card", "record", "file", "text")
    assert config.select_mode == "single" and config.real_song_limit == 1
    assert config.song_limit == 5  # 超出范围退回默认
    assert config.timeout == 30
    assert config.cooldown_seconds == 60
    assert config.enable_comments is True


def test_parse_send_modes_ignores_unknown():
    assert plugin.parse_send_modes(["record", "nope", "text"], ()) == ("record", "text")
    assert plugin.parse_send_modes([], ("record",)) == ("record",)
    assert plugin.parse_send_modes("语音", ()) == ("record",)


def test_shipped_config_example_is_valid():
    config = plugin.read_config(PLUGIN_DIR)
    assert config.send_modes == ("record", "file", "text")
    assert config.default_player == "网易点歌"
    assert config.timeout == 30


def test_manifest_is_market_compliant():
    manifest = load_manifest(PLUGIN_DIR)
    assert manifest.id == "music"
    assert set(manifest.permissions) == {
        "read_messages", "send_messages", "network", "capabilities"
    }
    assert {item["key"] for item in manifest.config_schema} == {
        "song_limit", "select_mode", "timeout", "send_modes", "enable_comments",
        "enable_lyrics", "timeout_recall", "cooldown_seconds", "default_player",
    }


# ----------------------------------------------------------------------
# 结果解析 / 探活
# ----------------------------------------------------------------------


def test_parse_netease_results():
    sources = sys.modules[f"{MODULE_NAME}.sources"]
    songs = sources.parse_netease_songs(netease_payload((1, "晴天", "周杰伦")), limit=5)
    assert len(songs) == 1
    song = songs[0]
    assert song.id == "1" and song.name == "晴天"
    assert song.artist_text == "周杰伦"
    assert song.duration_text == "04:28"
    assert song.audio_url == "http://music.163.com/song/media/outer/url?id=1.mp3"
    assert song.link == "https://music.163.com/#/song?id=1"
    # 结构不对 / 空结果
    assert sources.parse_netease_songs({"result": {}}) == []
    assert sources.parse_netease_songs(None) == []
    assert sources.parse_netease_songs({"result": {"songs": [{"name": "x"}]}}) == []


def test_parse_txqq_results():
    sources = sys.modules[f"{MODULE_NAME}.sources"]
    rows = txqq_payload(
        ("qq-1", "晴天", "周杰伦、温岚", "http://aqqmusic.tc.qq.com/C4001.m4a"),
        ("qq-2", "占位", "某人", "http://aqqmusic.tc.qq.com/"),
    )
    songs = sources.parse_txqq_songs(rows, limit=5, platform_type="qq")
    assert [song.id for song in songs] == ["qq-1", "qq-2"]
    assert songs[0].artists == ("周杰伦", "温岚")
    assert songs[0].platform_type == "qq" and songs[0].source == "txqq"
    # 只有域名没有路径的占位地址视作无音频
    assert songs[1].audio_url == ""
    assert sources.parse_txqq_songs({"unexpected": 1}) == []


def test_probe_audio_verdicts():
    async def main():
        def handler(request):
            url = str(request.url)
            if "html" in url:
                return httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"})
            if "audio" in url:
                return httpx.Response(200, content=b"x", headers={"content-type": "audio/mpeg"})
            raise httpx.ConnectError("boom", request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        sources = sys.modules[f"{MODULE_NAME}.sources"]
        assert await sources.probe_audio(client, "http://e/audio.mp3") is True
        assert await sources.probe_audio(client, "http://e/html.mp3") is False
        # 网络异常按「不确定」处理，不把整批结果扔掉
        assert await sources.probe_audio(client, "http://e/error.mp3") is None
        assert await sources.probe_audio(client, "not-a-url") is False
        await client.aclose()

    run(main())


def test_probe_audio_falls_back_when_head_unsupported():
    """不支持 HEAD 的 CDN 回 405：不能据此判死，要退回 Range GET 再问。"""

    async def main():
        seen = []

        def handler(request):
            seen.append(request.method)
            if request.method == "HEAD":
                return httpx.Response(405, content=b"")
            return httpx.Response(200, content=b"x", headers={"content-type": "audio/mpeg"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        sources = sys.modules[f"{MODULE_NAME}.sources"]
        assert await sources.probe_audio(client, "http://e/audio.mp3") is True
        assert seen == ["HEAD", "GET"]
        await client.aclose()

    run(main())


def test_netease_search_filters_unplayable_and_raises_when_all_dead():
    net = FakeNet(
        netease=netease_payload((1, "晴天", "周杰伦"), (2, "稻香", "周杰伦")),
        unplayable_ids={"1"},
    )
    player, _, _ = make_player(net=net)
    songs = run(player.search("晴天"))
    assert [song.id for song in songs] == ["2"]

    net = FakeNet(netease=netease_payload((1, "晴天", "周杰伦")), unplayable_ids={"1"})
    player, _, _ = make_player(net=net)
    with pytest.raises(plugin.SongsUnplayable):
        run(player.search("晴天"))


def test_txqq_non_netease_source_skips_probe():
    net = FakeNet(txqq=txqq_payload(("qq-1", "晴天", "周杰伦", "http://aqqmusic.tc.qq.com/C4001.m4a")))
    player, _, _ = make_player(net=net)
    songs = run(player.search("晴天", source="txqq", platform_type="qq"))
    assert [song.id for song in songs] == ["qq-1"]
    # 只有搜索这一次请求，没有探活
    assert [url for _, url in net.calls if "m4a" in url] == []


def test_txqq_placeholder_audio_is_dropped():
    """聚合源只有域名没有路径的占位地址发哪一档都放不出来，不进选歌列表。"""
    net = FakeNet(
        txqq=txqq_payload(
            ("qq-1", "晴天", "周杰伦", "http://aqqmusic.tc.qq.com/"),
            ("qq-2", "稻香", "周杰伦", "http://aqqmusic.tc.qq.com/C4002.m4a"),
        )
    )
    player, _, _ = make_player(net=net)
    songs = run(player.search("晴天", source="txqq", platform_type="qq"))
    assert [song.id for song in songs] == ["qq-2"]


def test_empty_platform_falls_back_to_netease():
    """酷狗 / 咪咕 等子平台整页空结果时退回网易云，不让对方白点一次。"""

    def handler(request):
        url = str(request.url)
        if "music.txqq.pro" in url:
            return httpx.Response(200, json={"data": "", "code": 404, "error": "没有找到相关信息"})
        return net(request)

    net = FakeNet()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = plugin.MusicConfig.from_mapping({"enable_comments": False})
    player = plugin.MusicPlayer(settings, client=client, clock=FakeClock())
    songs = run(player.search("晴天", source="txqq", platform_type="kugou"))
    assert [song.source for song in songs] == ["netease"]


def test_selection_list_title_follows_actual_source():
    """指定平台没结果退回网易云后，列表标题不能还写「酷狗音乐」。"""

    def handler(request):
        url = str(request.url)
        if "music.txqq.pro" in url:
            return httpx.Response(200, json={"data": "", "code": 404})
        return net(request)

    net = FakeNet(netease=netease_payload((1, "晴天", "周杰伦"), (2, "稻香", "周杰伦")))
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = plugin.MusicConfig.from_mapping({"enable_comments": False})
    player = plugin.MusicPlayer(settings, client=client, clock=FakeClock())
    adapter = RecordingAdapter()
    run(player.on_message(runtime(adapter), event("酷狗 晴天")))

    listing = adapter.sent[0][2][0]["data"]["text"]
    assert "【网易云音乐】" in listing
    assert "酷狗" not in listing
    pending = next(iter(player._pending.values()))
    assert pending.label == "网易云音乐"


# ----------------------------------------------------------------------
# 主流程：单结果直发
# ----------------------------------------------------------------------


def assert_cover_segment(segment, *, expected_bytes=COVER_BYTES):
    assert segment["type"] == "image"
    data = segment["data"]
    if "base64" in data:
        assert base64.b64decode(data["base64"]) == expected_bytes
        assert data["mime_type"] == "image/jpeg"
    else:
        assert data["url"] == "https://p1.music.126.net/cover.jpg"


def test_single_result_sends_record_then_info_and_cover():
    player, adapter, net = make_player()
    result = run(player.on_message(runtime(adapter), event("点歌 晴天")))

    assert result is False
    # 第一段是语音条，file 字段是音频直链
    assert adapter.sent[0][2] == [
        {"type": "record", "data": {"file": "http://music.163.com/song/media/outer/url?id=1.mp3"}}
    ]
    # 随后补信息文字 + 封面
    chat_id, chat_type, segments = adapter.sent[1]
    assert (chat_id, chat_type) == ("g1", "group")
    assert segments[0]["type"] == "text"
    assert "晴天 - 周杰伦" in segments[0]["data"]["text"]
    assert "04:28" in segments[0]["data"]["text"]
    assert "https://music.163.com/#/song?id=1" in segments[0]["data"]["text"]
    assert_cover_segment(segments[1])
    assert adapter.files == []


def test_unrelated_message_is_not_intercepted():
    player, adapter, _ = make_player()
    assert run(player.on_message(runtime(adapter), event("今天天气不错"))) is None
    assert adapter.sent == []


def test_trigger_without_song_name_asks_for_name():
    player, adapter, _ = make_player()
    run(player.on_message(runtime(adapter), event("点歌")))
    assert adapter.texts == ["想听哪首歌？说「点歌 歌名」就好"]


def test_search_failure_and_empty_result():
    net = FakeNet(search_status=503)
    player, adapter, _ = make_player(net=net)
    run(player.on_message(runtime(adapter), event("点歌 晴天")))
    assert adapter.texts == ["搜不到，换个关键词试试"]

    net = FakeNet(netease={"result": {"songs": []}})
    player, adapter, _ = make_player(net=net)
    run(player.on_message(runtime(adapter), event("点歌 不存在的歌")))
    assert adapter.texts == ["没找到《不存在的歌》，换个关键词试试"]


def test_unplayable_results_get_a_different_hint():
    net = FakeNet(netease=netease_payload((1, "晴天", "周杰伦")), unplayable_ids={"1"})
    player, adapter, _ = make_player(net=net)
    run(player.on_message(runtime(adapter), event("点歌 晴天")))
    assert "都放不了" in adapter.texts[0]
    assert "QQ点歌 晴天" in adapter.texts[0]


def test_index_argument_picks_that_song_directly():
    net = FakeNet(
        netease=netease_payload((1, "晴天", "周杰伦"), (2, "稻香", "周杰伦")),
    )
    player, adapter, _ = make_player(net=net)
    run(player.on_message(runtime(adapter), event("点歌 晴天 2")))
    assert adapter.sent[0][2][0]["type"] == "record"
    assert "id=2" in adapter.sent[0][2][0]["data"]["file"]


# ----------------------------------------------------------------------
# 选歌列表
# ----------------------------------------------------------------------


def test_selection_flow_reply_index_sends_chosen_song():
    net = FakeNet(
        netease=netease_payload((1, "晴天", "周杰伦"), (2, "稻香", "周杰伦")),
    )
    player, adapter, _ = make_player(net=net)
    chat = event("点歌 周杰伦")
    run(player.on_message(runtime(adapter), chat))

    assert adapter.sent[0][2][0]["type"] == "text"
    listing = adapter.sent[0][2][0]["data"]["text"]
    assert "【网易云音乐】" in listing
    assert "1. 晴天 - 周杰伦" in listing and "2. 稻香 - 周杰伦" in listing
    assert "回复「取消」" in listing
    assert "mid-1" == (player._pending[(("snowluma", "group", "g1"))].message_id)

    # 回复序号：先撤回列表，再发歌
    run(player.on_message(runtime(adapter), event("2", sender_id="u1")))
    assert adapter.actions == [("delete_msg", {"message_id": "mid-1"})]
    assert adapter.sent[-2][2][0]["type"] == "record"
    assert "id=2" in adapter.sent[-2][2][0]["data"]["file"]
    assert player._pending == {}


def test_cancel_recalls_list_and_clears_state():
    net = FakeNet(netease=netease_payload((1, "晴天", "周杰伦"), (2, "稻香", "周杰伦")))
    player, adapter, _ = make_player(net=net)
    run(player.on_message(runtime(adapter), event("点歌 周杰伦")))
    run(player.on_message(runtime(adapter), event("取消", sender_id="u1")))

    assert adapter.actions == [("delete_msg", {"message_id": "mid-1"})]
    assert adapter.texts[-1] == "已取消点歌"
    assert player._pending == {}


def test_out_of_range_index_keeps_pending():
    net = FakeNet(netease=netease_payload((1, "晴天", "周杰伦"), (2, "稻香", "周杰伦")))
    player, adapter, _ = make_player(net=net)
    run(player.on_message(runtime(adapter), event("点歌 周杰伦")))
    run(player.on_message(runtime(adapter), event("9", sender_id="u1")))

    assert adapter.texts[-1] == "序号超出范围，请输入 1-2 之间的数字"
    assert player._pending  # 列表还能继续选
    assert adapter.actions == []


def test_other_people_cannot_select_the_pending_list():
    net = FakeNet(netease=netease_payload((1, "晴天", "周杰伦"), (2, "稻香", "周杰伦")))
    player, adapter, _ = make_player(net=net)
    run(player.on_message(runtime(adapter), event("点歌 周杰伦")))

    # 别人发数字：不认领，放行给主流程（返回 None），待选状态不变
    assert run(player.on_message(runtime(adapter), event("2", sender_id="u2"))) is None
    assert player._pending
    assert adapter.actions == []


def test_timeout_recalls_list_and_notifies():
    net = FakeNet(netease=netease_payload((1, "晴天", "周杰伦"), (2, "稻香", "周杰伦")))
    player, adapter, _ = make_player(net=net)
    player.config = plugin.MusicConfig(timeout=0, enable_comments=False)
    player.sender.config = player.config

    async def main():
        await player.on_message(runtime(adapter), event("点歌 周杰伦"))
        pending = next(iter(player._pending.values()))
        await pending.task

    run(main())
    assert adapter.actions == [("delete_msg", {"message_id": "mid-1"})]
    assert adapter.texts[-1] == "点歌已超时，请重新点歌"
    assert player._pending == {}


def test_timeout_recall_off_still_clears_state():
    net = FakeNet(netease=netease_payload((1, "晴天", "周杰伦"), (2, "稻香", "周杰伦")))
    player, adapter, _ = make_player(net=net)
    player.config = plugin.MusicConfig(timeout=0, timeout_recall=False, enable_comments=False)
    player.sender.config = player.config

    async def main():
        await player.on_message(runtime(adapter), event("点歌 周杰伦"))
        pending = next(iter(player._pending.values()))
        await pending.task

    run(main())
    assert adapter.actions == []
    assert adapter.texts[-1] == "点歌已超时，请重新点歌"


def test_recall_without_message_id_or_permission_only_logs(caplog):
    caplog.set_level("WARNING", logger=LOGGER_NAME)
    net = FakeNet(netease=netease_payload((1, "晴天", "周杰伦"), (2, "稻香", "周杰伦")))
    player, adapter, _ = make_player(net=net, adapter=RecordingAdapter(fail_action=True))
    run(player.on_message(runtime(adapter), event("点歌 周杰伦")))
    run(player.on_message(runtime(adapter), event("取消", sender_id="u1")))

    assert adapter.texts[-1] == "已取消点歌"
    assert any("撤回失败" in record.message for record in caplog.records)


def test_selection_still_works_without_message_id(caplog):
    """适配器不回 message_id（如 Telegram）时只是撤不回，选歌照常可用。"""
    caplog.set_level("WARNING", logger=LOGGER_NAME)

    class NoIdAdapter(RecordingAdapter):
        async def send(self, chat_id, segments, *, chat_type="group"):
            await super().send(chat_id, segments, chat_type=chat_type)
            return None

    net = FakeNet(netease=netease_payload((1, "晴天", "周杰伦"), (2, "稻香", "周杰伦")))
    player, adapter, _ = make_player(net=net, adapter=NoIdAdapter())
    run(player.on_message(runtime(adapter), event("点歌 周杰伦")))

    assert any("message_id" in record.message for record in caplog.records)
    pending = next(iter(player._pending.values()))
    assert pending.message_id is None

    run(player.on_message(runtime(adapter), event("2", sender_id="u1")))
    assert adapter.actions == []  # 没有 id 就不调撤回
    assert adapter.sent[-2][2][0]["type"] == "record"
    assert "id=2" in adapter.sent[-2][2][0]["data"]["file"]
    assert player._pending == {}


# ----------------------------------------------------------------------
# 发送链
# ----------------------------------------------------------------------


def test_record_failure_falls_back_to_file():
    net = FakeNet()
    player, adapter, _ = make_player(net=net, adapter=RecordingAdapter(fail_record=True))
    run(player.on_message(runtime(adapter), event("点歌 晴天")))

    assert adapter.files and adapter.files[0][1].endswith(".mp3")
    assert adapter.files[0][2] == AUDIO_BYTES
    # 音频发出后再补信息文字 + 封面
    assert "晴天 - 周杰伦" in adapter.texts[-1] or adapter.texts[-1].startswith("🎵")


def test_file_failure_falls_back_to_text_with_link():
    net = FakeNet(download_status=500)
    adapter = RecordingAdapter()
    player, _, _ = make_player(net=net, adapter=adapter)
    run(player.on_message(runtime(adapter), event("点歌 晴天")))

    assert [segment["type"] for segment in adapter.sent[-1][2]][0] == "text"
    assert "https://music.163.com/#/song?id=1" in adapter.sent[-1][2][0]["data"]["text"]


def test_telegram_skips_record_and_uses_file():
    adapter = RecordingAdapter(name="telegram")
    player, _, _ = make_player(adapter=adapter)
    run(player.on_message(runtime(adapter), event("点歌 晴天", platform="telegram")))

    assert adapter.files  # 语音段只对 OneBot 有意义，其他平台走文件上传
    assert all(segment["type"] != "record" for _, _, segs in adapter.sent for segment in segs)


def test_card_only_when_configured_and_only_for_snowluma():
    net = FakeNet()
    adapter = RecordingAdapter()
    player, _, _ = make_player(net=net, adapter=adapter, config={"send_modes": ["card", "text"]})
    run(player.on_message(runtime(adapter), event("点歌 晴天")))
    assert adapter.sent[0][2][0] == {"type": "music", "data": {"type": "netease", "id": "1"}}

    net = FakeNet()
    adapter = RecordingAdapter(name="telegram")
    player, _, _ = make_player(net=net, adapter=adapter, config={"send_modes": ["card", "text"]})
    run(player.on_message(runtime(adapter), event("点歌 晴天", platform="telegram")))
    assert all(segment["type"] != "music" for _, _, segs in adapter.sent for segment in segs)


def test_default_chain_does_not_use_card():
    player, adapter, _ = make_player()
    run(player.on_message(runtime(adapter), event("点歌 晴天")))
    assert all(segment["type"] != "music" for _, _, segs in adapter.sent for segment in segs)


def test_cover_failure_still_sends_text(caplog):
    caplog.set_level("WARNING", logger=LOGGER_NAME)
    adapter = RecordingAdapter(fail_image=True)
    player, _, _ = make_player(adapter=adapter)
    run(player.on_message(runtime(adapter), event("点歌 晴天")))

    assert adapter.sent[-1][2][0]["type"] == "text"
    assert "晴天 - 周杰伦" in adapter.sent[-1][2][0]["data"]["text"]
    assert any("封面" in record.message for record in caplog.records)


def test_cover_download_failure_falls_back_to_url():
    net = FakeNet(cover=httpx.Response(404, content=b"nope"))
    player, adapter, _ = make_player(net=net)
    run(player.on_message(runtime(adapter), event("点歌 晴天")))
    assert adapter.sent[-1][2][1] == {
        "type": "image",
        "data": {"url": "https://p1.music.126.net/cover.jpg"},
    }


def test_cover_is_downloaded_as_thumbnail_for_netease():
    seen = {}

    def cover(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, content=COVER_BYTES, headers={"content-type": "image/jpeg"})

    net = FakeNet(cover=cover)
    player, adapter, _ = make_player(net=net)
    run(player.on_message(runtime(adapter), event("点歌 晴天")))
    assert "param=500y500" in seen["url"]


def test_hot_comment_appended_when_enabled():
    net = FakeNet()
    player, adapter, _ = make_player(net=net, config={"enable_comments": True})
    run(player.on_message(runtime(adapter), event("点歌 晴天")))
    assert any("热评" in text for text in adapter.texts)


# ----------------------------------------------------------------------
# 冷却
# ----------------------------------------------------------------------


def test_cooldown_blocks_second_request():
    clock = FakeClock()
    player, adapter, net = make_player(clock=clock, net=FakeNet())
    run(player.on_message(runtime(adapter), event("点歌 晴天", sender_id="u1")))
    count = len(adapter.sent)

    clock.now += 20
    searches = len(net.calls)
    run(player.on_message(runtime(adapter), event("点歌 稻香", sender_id="u1")))
    assert adapter.texts[-1] == "刚点过啦，40 秒后再点吧"
    assert len(net.calls) == searches  # 冷却内不再搜索

    # 别人不受影响
    run(player.on_message(runtime(adapter), event("点歌 稻香", sender_id="u2")))
    assert len(adapter.sent) > count + 1


def test_cooldown_starts_even_if_search_fails():
    clock = FakeClock()
    player, adapter, _ = make_player(clock=clock, net=FakeNet(search_status=500))
    run(player.on_message(runtime(adapter), event("点歌 晴天")))
    assert adapter.texts == ["搜不到，换个关键词试试"]

    clock.now += 10
    run(player.on_message(runtime(adapter), event("点歌 晴天")))
    assert adapter.texts[-1] == "刚点过啦，50 秒后再点吧"


# ----------------------------------------------------------------------
# 查歌词 / 注册 / 宿主集成
# ----------------------------------------------------------------------


def test_lyrics_request_without_name_and_not_found():
    player, adapter, _ = make_player()
    run(player.on_message(runtime(adapter), event("查歌词")))
    assert adapter.texts[-1].startswith("请输入歌曲名称")

    net = FakeNet(netease={"result": {"songs": []}})
    player, adapter, _ = make_player(net=net)
    run(player.on_message(runtime(adapter), event("查歌词 不存在的歌")))
    assert adapter.texts[-1] == "没找到《不存在的歌》，换个关键词试试"


def test_lyrics_falls_back_to_text_when_renderer_unavailable():
    class NoRenderer:
        available = False

    net = FakeNet()
    player, adapter, _ = make_player(net=net)
    player.renderer = NoRenderer()
    player.sender.renderer = NoRenderer()

    def handler(request):
        url = str(request.url)
        if "lyric" in url:
            return httpx.Response(200, json={"lrc": {"lyric": "[00:01.00]第一句\n[00:02.00]第二句"}})
        return net(request)

    player._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    run(player.on_message(runtime(adapter), event("查歌词 晴天")))
    assert "第一句" in adapter.texts[-1] and "第二句" in adapter.texts[-1]


class StubCtx:
    def __init__(self, path: Path):
        self.extras = {}
        self.plugin = SimpleNamespace(path=path)
        self.message_hooks = []
        self.stop_hooks = []
        self.notes = []

    def capability_note(self, describe):
        self.notes.append(describe)

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
    assert ctx.notes == [ctx.extras["music"].capability_note]
    assert isinstance(ctx.extras["music"], plugin.MusicPlayer)


def test_capability_note_mentions_selection():
    player, adapter, _ = make_player()
    note = player.capability_note(event(), adapter)
    assert note.title == "点歌"
    assert "序号" in note.detail


def test_loader_loads_music_plugin():
    config = Config()
    config.plugins.dir = str(PLUGIN_DIR.parent)
    config.plugins.enabled = ["music"]
    manager = PluginManager(config, store=None)
    loaded = manager.load_all()
    assert [item.name for item in loaded] == ["music"]
    assert manager.plugins["music"].valid


def test_aclose_cancels_tasks_and_releases_owned_client():
    net = FakeNet(netease=netease_payload((1, "晴天", "周杰伦"), (2, "稻香", "周杰伦")))
    run(plugin.MusicPlayer(plugin.MusicConfig()).aclose())  # 没建过客户端也不报错

    client = httpx.AsyncClient(transport=httpx.MockTransport(net))
    player = plugin.MusicPlayer(plugin.MusicConfig(), client=client)
    run(player.aclose())
    assert client.is_closed is False  # 注入的客户端不归插件关
    run(client.aclose())

    async def main():
        player = plugin.MusicPlayer(plugin.MusicConfig())
        player._client = httpx.AsyncClient(transport=httpx.MockTransport(net))
        owned = player._client
        await player.aclose()
        assert owned.is_closed is True

    run(main())


def test_end_to_end_via_host_message_loop(tmp_path):
    """走宿主主循环：点歌被插件拦下，发出语音条 + 信息文字 + 封面，不惊动模型。"""
    from rubenbot.core import RubenBot
    from rubenbot.memory.store import MemoryStore
    from rubenbot.persona import Persona

    from conftest import ScriptedLLM

    root = tmp_path / "plugins"
    plugin_dir = root / "music"
    plugin_dir.mkdir(parents=True)
    for name in ("plugin.py", "manifest.json", "sources.py", "sender.py", "renderer.py"):
        (plugin_dir / name).write_text(
            (PLUGIN_DIR / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    (plugin_dir / "config.yaml").write_text(
        "cooldown_seconds: 0\nenable_comments: false\n", encoding="utf-8"
    )

    config = Config()
    config.plugins.dir = str(root)
    config.plugins.enabled = ["music"]
    config.reply.segment_delay_enabled = False
    config.decision.merge_window_seconds = 0
    config.decision.hard_merge_window_seconds = 0
    store = MemoryStore(":memory:")
    manager = PluginManager(config, store)
    manager.load_all()
    manager.permissions.grant("music", "network")
    manager.permissions.grant("music", "send_messages")
    llm = ScriptedLLM({"chat": ["不该出现"]})
    bot = RubenBot(config, store, llm, Persona("Ruben", "你是 Ruben"), plugins=manager)
    adapter = RecordingAdapter(name="snowluma")
    bot.register_adapter(adapter)
    manager.extras["bot"] = bot

    net = FakeNet()
    client = httpx.AsyncClient(transport=httpx.MockTransport(net))
    manager.extras["music"]._client = client

    message = MessageEvent(
        platform="snowluma",
        chat_type="private",
        chat_id="u1",
        sender_id="u1",
        sender_name="阿花",
        text="我想听 晴天",
    )
    run(bot.handle_event(message))

    assert llm.calls == []
    assert adapter.sent[0][2][0]["type"] == "record"
    assert adapter.sent[1][2][0]["type"] == "text"
    assert "晴天 - 周杰伦" in adapter.sent[1][2][0]["data"]["text"]
    assert_cover_segment(adapter.sent[1][2][1])
    run(client.aclose())


def test_aclose_is_registered_on_stop():
    """停用回调真的能取消待选列表的超时任务，不停在后台空转。"""

    async def main():
        net = FakeNet(netease=netease_payload((1, "晴天", "周杰伦"), (2, "稻香", "周杰伦")))
        player, adapter, _ = make_player(net=net)
        await player.on_message(runtime(adapter), event("点歌 周杰伦"))
        pending = next(iter(player._pending.values()))
        await player.aclose()
        assert player._pending == {}
        assert pending.task.done()

    run(main())


# ----------------------------------------------------------------------
# 歌词渲染
# ----------------------------------------------------------------------


def test_clean_lyrics_strips_timestamps_and_keeps_gaps():
    renderer = sys.modules[f"{MODULE_NAME}.renderer"]
    lines = renderer.clean_lyrics("[00:01.00]第一句\n\n[00:05.12]第二句\n[00:09]第三句\n")
    assert lines == ["第一句", "", "第二句", "第三句"]


def test_renderer_unavailable_without_font_or_pillow(tmp_path):
    renderer_mod = sys.modules[f"{MODULE_NAME}.renderer"]
    instance = renderer_mod.LyricsRenderer(font_path=str(tmp_path / "missing.ttf"))
    assert instance.available is False
    assert instance.render("[00:01.00]第一句") is None


def test_renderer_draws_png_when_environment_allows(tmp_path):
    pytest.importorskip("PIL")
    renderer_mod = sys.modules[f"{MODULE_NAME}.renderer"]
    font = renderer_mod.find_font()
    if not font:
        pytest.skip("环境里没有 CJK 字体")
    instance = renderer_mod.LyricsRenderer(font_path=font)
    image = instance.render("[00:01.00]第一句\n[00:02.00]第二句")
    assert image and image.startswith(b"\x89PNG")
