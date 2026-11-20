"""画图插件：触发识别、响应解析、冷却 / 每日上限、黑名单与降级路径。"""

import asyncio
import base64
import importlib.util
import json
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import httpx

from rubenbot.adapters.base import MessageEvent
from rubenbot.config import Config
from rubenbot.plugins.loader import PluginManager, load_manifest

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugins" / "image_gen"
PLUGIN_FILE = PLUGIN_DIR / "plugin.py"
LOGGER_NAME = "image_gen_plugin"

# 1x1 PNG（真实文件头，detect_suffix 认得出）
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def load_plugin_module():
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
    """记录每次 send 的会话与消息段；``fail_image`` 模拟协议端不收图片段。"""

    def __init__(self, name="snowluma", *, fail_image=False):
        self.name = name
        self.fail_image = fail_image
        self.sent: list[tuple[str, str, list]] = []

    async def send(self, chat_id, segments, *, chat_type="group"):
        if self.fail_image and any(segment["type"] == "image" for segment in segments):
            raise RuntimeError("协议端不收图片")
        self.sent.append((chat_id, chat_type, list(segments)))

    @property
    def texts(self):
        return ["".join(segment.render() for segment in segments) for _, _, segments in self.sent]


class FakeClock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeDate:
    def __init__(self, day: date):
        self.day = day

    def __call__(self) -> date:
        return self.day


def runtime(adapter) -> SimpleNamespace:
    return SimpleNamespace(adapters={adapter.name: adapter})


def event(text="画一张 一只橘猫", *, sender_id="u1", chat_id="g1") -> MessageEvent:
    return MessageEvent(
        platform="snowluma",
        chat_type="group",
        chat_id=chat_id,
        sender_id=sender_id,
        sender_name="阿花",
        text=text,
    )


def make_generator(
    *,
    api_status=200,
    api_json=None,
    image_bytes=PNG_BYTES,
    download_status=200,
    api_raises=None,
    config=None,
    adapter=None,
    clock=None,
    today=None,
    data_dir=None,
    requests=None,
):
    """构造插件实例；生图与下载都走 MockTransport。"""
    calls = requests if requests is not None else []
    body = api_json if api_json is not None else {"images": [{"url": "https://cdn.example/out.png"}]}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(
            {
                "method": request.method,
                "url": str(request.url),
                "headers": dict(request.headers),
                "json": _maybe_json(request),
            }
        )
        if request.method == "POST":
            if api_raises is not None:
                raise api_raises
            return httpx.Response(api_status, json=body)
        return httpx.Response(download_status, content=image_bytes)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cfg = {"api_key": "sk-test", "cooldown_seconds": 0, "daily_limit": 20}
    cfg.update(config or {})
    generator = plugin.ImageGenerator(
        cfg,
        client=client,
        clock=clock or FakeClock(),
        today=today or date.today,
        data_dir=data_dir,
    )
    return generator, adapter or RecordingAdapter(), calls


def _maybe_json(request):
    try:
        return json.loads(request.content or b"{}")
    except Exception:  # noqa: BLE001
        return None


# ----------------------------------------------------------------------
# 触发识别
# ----------------------------------------------------------------------


def test_parse_trigger_basic():
    triggers = plugin.DEFAULT_TRIGGERS
    assert plugin.parse_trigger("画一张 一只橘猫", triggers) == "一只橘猫"
    assert plugin.parse_trigger("画个 赛博朋克城市", triggers) == "赛博朋克城市"
    assert plugin.parse_trigger("画一幅：雪山日出", triggers) == "雪山日出"
    assert plugin.parse_trigger("帮我画 一只柴犬", triggers) == "一只柴犬"
    assert plugin.parse_trigger("生成图片 一只猫", triggers) == "一只猫"
    # 「画个一只猫」没空格也认得
    assert plugin.parse_trigger("画个一只猫", triggers) == "一只猫"
    # 只有触发词没带描述 -> 空串
    assert plugin.parse_trigger("画一张", triggers) == ""
    assert plugin.parse_trigger("画个", triggers) == ""
    # 不相关消息不触发
    assert plugin.parse_trigger("今天画了个图", triggers) is None
    assert plugin.parse_trigger("", triggers) is None


def test_parse_trigger_custom_and_longest_first():
    assert plugin.parse_trigger("来张图 一只猫", ("来张图", "来张")) == "一只猫"
    # 长词优先
    assert plugin.parse_trigger("画一张猫 一只猫", ("画一张", "画一张猫")) == "一只猫"


def test_parse_response_variants():
    assert plugin.parse_response({"images": [{"url": "https://x/a.png"}]}) == (
        "https://x/a.png",
        "",
    )
    # 兼容 OpenAI 风格的 data / b64_json
    assert plugin.parse_response({"data": [{"b64_json": "AAAA"}]}) == ("AAAA", ".png")
    assert plugin.parse_response({"images": []}) is None
    assert plugin.parse_response({}) is None
    assert plugin.parse_response(None) is None
    assert plugin.parse_response({"images": [{"nope": 1}]}) is None


def test_detect_suffix():
    assert plugin.detect_suffix(PNG_BYTES) == ".png"
    assert plugin.detect_suffix(b"\xff\xd8\xff\xe0" + b"0" * 10) == ".jpg"
    assert plugin.detect_suffix(b"RIFF0000WEBPVP8 ") == ".webp"
    assert plugin.detect_suffix(b"GIF89a....") == ".gif"
    assert plugin.detect_suffix(b"unknown") == ".png"


def test_is_review_failure():
    assert plugin.is_review_failure(400, {"message": "content policy violation"})
    assert plugin.is_review_failure(403, "sensitive content detected")
    assert plugin.is_review_failure(400, {"message": "内容审核未通过"})
    assert not plugin.is_review_failure(200, "content")
    assert not plugin.is_review_failure(500, "content")
    assert not plugin.is_review_failure(400, {"message": "invalid model"})


# ----------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------


def test_config_defaults_and_env(monkeypatch):
    monkeypatch.setenv("RUBENBOT_IMAGE_KEY", "sk-from-env")
    cfg = plugin.ImageGenConfig.from_mapping({"api_key": "${RUBENBOT_IMAGE_KEY}"})
    assert cfg.api_key == "sk-from-env"
    assert cfg.configured is True
    assert cfg.provider == "siliconflow"
    assert cfg.model == "Kwai-Kolors/Kolors"
    assert cfg.triggers == plugin.DEFAULT_TRIGGERS
    assert cfg.cooldown_seconds == 120
    assert cfg.daily_limit == 20
    assert cfg.blacklist == plugin.DEFAULT_BLACKLIST
    assert "血腥" in cfg.blacklist

    # 没配 / 未展开的占位符都算没配
    assert plugin.ImageGenConfig.from_mapping(None).configured is False
    monkeypatch.delenv("RUBENBOT_IMAGE_KEY", raising=False)
    assert plugin.ImageGenConfig.from_mapping({"api_key": "${RUBENBOT_IMAGE_KEY}"}).api_key == ""


def test_config_unsupported_provider_and_bad_types():
    cfg = plugin.ImageGenConfig.from_mapping(
        {
            "provider": "midjourney",
            "triggers": "画个",
            "cooldown_seconds": "abc",
            "daily_limit": -5,
            "blacklist": ["血腥"],
        }
    )
    assert cfg.provider == "siliconflow"
    assert cfg.triggers == ("画个",)
    assert cfg.cooldown_seconds == 120
    assert cfg.daily_limit == 0
    assert cfg.blacklist == ("血腥",)


def test_shipped_config_example_is_valid():
    cfg = plugin.read_config(PLUGIN_DIR)
    assert cfg.provider == "siliconflow"
    assert cfg.model == "Kwai-Kolors/Kolors"
    assert cfg.cooldown_seconds == 120
    assert cfg.daily_limit == 20
    assert "血腥" in cfg.blacklist
    # 示例里写的是 ${ENV} 占位；没设环境变量时视为未配置
    assert cfg.configured is False


def test_manifest_is_market_compliant():
    manifest = load_manifest(PLUGIN_DIR)
    assert manifest.id == "image_gen"
    assert set(manifest.permissions) == {"read_messages", "send_messages", "network"}
    keys = {item["key"] for item in manifest.config_schema}
    assert {
        "provider",
        "api_key",
        "model",
        "triggers",
        "cooldown_seconds",
        "daily_limit",
    } <= keys


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------


def test_generate_downloads_and_sends_image(tmp_path):
    data_dir = tmp_path / "data"
    generator, adapter, calls = make_generator(data_dir=data_dir)

    asyncio.run(generator.on_message(runtime(adapter), event("画一张 一只橘猫在晒太阳")))

    # 一次 POST 生成 + 一次 GET 下载
    assert [call["method"] for call in calls] == ["POST", "GET"]
    post = calls[0]
    assert post["url"].startswith("https://api.siliconflow.cn/v1/images/generations")
    assert post["headers"]["authorization"] == "Bearer sk-test"
    # 描述直接用用户原话，不做改写
    assert post["json"]["prompt"] == "一只橘猫在晒太阳"
    assert post["json"]["model"] == "Kwai-Kolors/Kolors"
    assert calls[1]["url"] == "https://cdn.example/out.png"

    # 下载后的图片以 base64 图片段发送
    assert len(adapter.sent) == 1
    chat_id, chat_type, segments = adapter.sent[0]
    assert (chat_id, chat_type) == ("g1", "group")
    image = segments[0]
    assert image["type"] == "image"
    assert base64.b64decode(image["data"]["base64"]) == PNG_BYTES
    assert image["data"]["mime_type"] == "image/png"
    assert "url" not in image["data"]  # 不把临时 URL 当图床引用

    # data 目录留了一份
    saved = list(data_dir.glob("*.png"))
    assert len(saved) == 1 and saved[0].read_bytes() == PNG_BYTES
    assert image["data"]["filename"] == saved[0].name


def test_generate_supports_b64_response(tmp_path):
    encoded = base64.b64encode(PNG_BYTES).decode("ascii")
    generator, adapter, calls = make_generator(
        api_json={"images": [{"b64_json": encoded}]}, data_dir=tmp_path
    )

    asyncio.run(generator.on_message(runtime(adapter), event("画个 一只猫")))

    assert [call["method"] for call in calls] == ["POST"]  # 不需要下载
    assert adapter.sent[0][2][0]["type"] == "image"


def test_review_failure_replies_honestly(tmp_path):
    generator, adapter, _ = make_generator(
        api_status=400,
        api_json={"code": 400, "message": "content policy violation"},
        data_dir=tmp_path,
    )

    asyncio.run(generator.on_message(runtime(adapter), event("画一张 不该画的东西")))

    assert adapter.texts == ["这个画不了，换个描述试试"]
    assert all(segment["type"] == "text" for _, _, segments in adapter.sent for segment in segments)


def test_api_error_replies_generic(tmp_path):
    generator, adapter, _ = make_generator(
        api_status=400, api_json={"message": "invalid model"}, data_dir=tmp_path
    )

    asyncio.run(generator.on_message(runtime(adapter), event("画一张 一只猫")))

    assert adapter.texts == ["生图服务出错了，稍后再试试"]


def test_network_error_degrades(tmp_path):
    generator, adapter, _ = make_generator(
        api_raises=httpx.ConnectError("拒绝连接"), data_dir=tmp_path
    )

    asyncio.run(generator.on_message(runtime(adapter), event("画一张 一只猫")))

    assert adapter.texts == ["生图服务连不上，稍后再试试"]


def test_empty_response_degrades(tmp_path):
    generator, adapter, _ = make_generator(api_json={"images": []}, data_dir=tmp_path)

    asyncio.run(generator.on_message(runtime(adapter), event("画一张 一只猫")))

    assert adapter.texts == ["这次没画出图，换个描述试试"]


def test_download_failure_degrades(tmp_path):
    generator, adapter, _ = make_generator(download_status=404, data_dir=tmp_path)

    asyncio.run(generator.on_message(runtime(adapter), event("画一张 一只猫")))

    assert adapter.texts == ["图画好了但取不回来，稍后再试试"]


def test_oversized_image_rejected(tmp_path):
    generator, adapter, _ = make_generator(
        image_bytes=b"x" * (plugin.MAX_IMAGE_BYTES + 1), data_dir=tmp_path
    )

    asyncio.run(generator.on_message(runtime(adapter), event("画一张 一只猫")))

    assert adapter.texts == ["这张图太大发不出去，换个描述试试"]


def test_image_send_failure_degrades_to_text(tmp_path):
    generator, adapter, _ = make_generator(
        adapter=RecordingAdapter(fail_image=True), data_dir=tmp_path
    )

    asyncio.run(generator.on_message(runtime(adapter), event("画一张 一只猫")))

    assert len(adapter.sent) == 1
    assert "图画好了但发不出来" in adapter.texts[0]
    assert str(tmp_path) in adapter.texts[0]


def test_concurrent_saves_do_not_overwrite(tmp_path):
    """同一秒内生成多张时，落盘文件名不能互相覆盖。"""
    generator, adapter, _ = make_generator(data_dir=tmp_path, clock=FakeClock(1000.0))

    asyncio.run(generator.on_message(runtime(adapter), event("画一张 猫", sender_id="u1")))
    asyncio.run(generator.on_message(runtime(adapter), event("画一张 狗", sender_id="u2")))

    saved = list(tmp_path.glob("*.png"))
    assert len(saved) == 2


def test_trigger_without_prompt_asks_for_description(tmp_path):
    generator, adapter, calls = make_generator(data_dir=tmp_path)

    asyncio.run(generator.on_message(runtime(adapter), event("画一张")))

    assert adapter.texts == ["想画什么？说「画一张 描述」就好"]
    assert calls == []


def test_unrelated_message_is_not_intercepted(tmp_path):
    generator, adapter, calls = make_generator(data_dir=tmp_path)

    result = asyncio.run(generator.on_message(runtime(adapter), event("今天天气不错")))

    assert result is None
    assert adapter.sent == [] and calls == []


# ----------------------------------------------------------------------
# 黑名单
# ----------------------------------------------------------------------


def test_blacklist_blocks_before_request(tmp_path, caplog):
    caplog.set_level("INFO", logger=LOGGER_NAME)
    generator, adapter, calls = make_generator(
        config={"blacklist": ["血腥", "裸露"]}, data_dir=tmp_path
    )

    asyncio.run(generator.on_message(runtime(adapter), event("画一张 血腥的战场")))

    assert adapter.texts == ["这个画不了，换个描述试试"]
    assert calls == []  # 本地就拦下，不打接口
    assert any("黑名单" in record.message for record in caplog.records)


def test_blacklist_case_insensitive(tmp_path):
    generator, adapter, calls = make_generator(
        config={"blacklist": ["NSFW"]}, data_dir=tmp_path
    )

    asyncio.run(generator.on_message(runtime(adapter), event("画一张 nsfw 图")))

    assert adapter.texts == ["这个画不了，换个描述试试"]
    assert calls == []


# ----------------------------------------------------------------------
# 冷却与每日上限
# ----------------------------------------------------------------------


def test_cooldown_blocks_second_request(tmp_path):
    clock = FakeClock()
    generator, adapter, calls = make_generator(
        config={"cooldown_seconds": 120}, clock=clock, data_dir=tmp_path
    )

    asyncio.run(generator.on_message(runtime(adapter), event("画一张 猫")))
    assert len(calls) == 2  # 生成 + 下载

    clock.now += 30
    asyncio.run(generator.on_message(runtime(adapter), event("画一张 狗")))
    assert adapter.texts[-1] == "刚画过啦，90 秒后再画吧"
    assert len(calls) == 2  # 冷却内不打接口

    # 别人不受影响
    asyncio.run(generator.on_message(runtime(adapter), event("画一张 狗", sender_id="u2")))
    assert adapter.sent[-1][2][0]["type"] == "image"

    # 冷却结束后恢复
    clock.now += 90
    asyncio.run(generator.on_message(runtime(adapter), event("画一张 猫", sender_id="u1")))
    assert adapter.sent[-1][2][0]["type"] == "image"


def test_cooldown_applies_even_if_request_fails(tmp_path):
    clock = FakeClock()
    generator, adapter, _ = make_generator(
        config={"cooldown_seconds": 60},
        api_status=500,
        api_json={"message": "boom"},
        clock=clock,
        data_dir=tmp_path,
    )

    asyncio.run(generator.on_message(runtime(adapter), event("画一张 猫")))
    assert adapter.texts == ["生图服务出错了，稍后再试试"]

    clock.now += 10
    asyncio.run(generator.on_message(runtime(adapter), event("画一张 猫")))
    assert adapter.texts[-1] == "刚画过啦，50 秒后再画吧"


def test_daily_limit_per_chat_and_resets_next_day(tmp_path):
    today = FakeDate(date(2026, 11, 20))
    generator, adapter, calls = make_generator(
        config={"daily_limit": 2}, today=today, data_dir=tmp_path
    )

    asyncio.run(generator.on_message(runtime(adapter), event("画一张 猫", sender_id="u1")))
    asyncio.run(generator.on_message(runtime(adapter), event("画一张 狗", sender_id="u2")))
    assert sum(1 for call in calls if call["method"] == "POST") == 2
    assert all(sent[2][0]["type"] == "image" for sent in adapter.sent)

    # 同群第三人：超上限，回复明日再来，不再打接口
    asyncio.run(generator.on_message(runtime(adapter), event("画一张 鸟", sender_id="u3")))
    assert adapter.texts[-1] == "今天的画用完了，明天再来吧"
    assert sum(1 for call in calls if call["method"] == "POST") == 2

    # 另一个群有自己的额度
    asyncio.run(
        generator.on_message(runtime(adapter), event("画一张 猫", sender_id="u9", chat_id="g2"))
    )
    assert adapter.sent[-1][2][0]["type"] == "image"

    # 第二天额度重置
    today.day = date(2026, 11, 21)
    asyncio.run(generator.on_message(runtime(adapter), event("画一张 鸟", sender_id="u3")))
    assert adapter.sent[-1][2][0]["type"] == "image"


def test_daily_limit_zero_means_unlimited(tmp_path):
    generator, adapter, calls = make_generator(
        config={"daily_limit": 0}, data_dir=tmp_path
    )

    for index in range(3):
        asyncio.run(
            generator.on_message(runtime(adapter), event(f"画一张 猫{index}", sender_id=f"u{index}"))
        )
    assert sum(1 for call in calls if call["method"] == "POST") == 3


def test_failed_generation_does_not_consume_daily_quota(tmp_path):
    """服务商故障不该耗掉当天的额度：失败后额度仍在，冷却照常生效。"""
    generator, adapter, calls = make_generator(
        config={"daily_limit": 1, "cooldown_seconds": 0},
        api_status=500,
        api_json={"message": "boom"},
        data_dir=tmp_path,
    )

    asyncio.run(generator.on_message(runtime(adapter), event("画一张 猫", sender_id="u1")))
    assert adapter.texts == ["生图服务出错了，稍后再试试"]

    # 同群换个人再画：额度没被失败请求占掉，照常请求接口
    asyncio.run(generator.on_message(runtime(adapter), event("画一张 狗", sender_id="u2")))
    assert sum(1 for call in calls if call["method"] == "POST") == 2
    assert adapter.texts[-1] == "生图服务出错了，稍后再试试"


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


def test_register_without_key_does_not_register_trigger(caplog, monkeypatch):
    caplog.set_level("INFO", logger=LOGGER_NAME)
    # 示例配置里的 key 是未展开的 ${ENV}；确保环境里也没有这个变量
    monkeypatch.delenv("RUBENBOT_IMAGE_KEY", raising=False)
    ctx = StubCtx(PLUGIN_DIR)
    plugin.register(ctx)
    assert ctx.message_hooks == []
    assert ctx.stop_hooks == []
    assert "image_gen" not in ctx.extras
    assert any("未配置" in record.message for record in caplog.records)


def test_register_with_key_registers_trigger(tmp_path):
    ctx = StubCtx(PLUGIN_DIR)
    ctx.extras["image_gen_config"] = {"api_key": "sk-test"}
    plugin.register(ctx)
    assert len(ctx.message_hooks) == 1
    assert len(ctx.stop_hooks) == 1
    generator = ctx.extras["image_gen"]
    assert isinstance(generator, plugin.ImageGenerator)
    # 图片落盘目录跟着插件目录走
    assert generator.data_dir == PLUGIN_DIR / "data"


def test_loader_skips_trigger_without_key_but_loads_plugin():
    config = Config()
    config.plugins.dir = str(PLUGIN_DIR.parent)
    config.plugins.enabled = ["image_gen"]
    manager = PluginManager(config, store=None)
    loaded = manager.load_all()
    assert [item.name for item in loaded] == ["image_gen"]
    plugin_obj = manager.plugins["image_gen"]
    assert plugin_obj.valid
    # 没配 key：插件加载了，但没有注册任何消息钩子
    assert plugin_obj.message_hooks == []


def test_aclose_keeps_injected_client_open(tmp_path):
    generator, _, _ = make_generator(data_dir=tmp_path)
    client = generator._client
    asyncio.run(generator.aclose())
    # 注入的客户端不归插件关
    assert client is not None and client.is_closed is False
    asyncio.run(client.aclose())


def test_data_dir_for_fallback():
    assert plugin.data_dir_for(None) == PLUGIN_DIR / "data"

    class Ctx:
        plugin = SimpleNamespace(path=Path("/tmp/whatever"))

    assert plugin.data_dir_for(Ctx()) == Path("/tmp/whatever/data")
