"""配置、人格、LLM 客户端。"""

import asyncio
import json

import httpx
import pytest

from rubenbot.config import Config, LLMConfig
from rubenbot.llm import LLMClient, LLMError, parse_json_payload
from rubenbot.persona import DEFAULT_PERSONA, load_persona


# ----------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------


def test_config_defaults():
    cfg = Config()
    assert cfg.llm.model_for("extract") == cfg.llm.chat_model
    assert cfg.llm.model_for("consolidate") == cfg.llm.chat_model
    assert cfg.memory.extract_every_n == 30
    assert cfg.memory.inject_limit == 20
    assert cfg.decision.merge_window_seconds == 2.5
    assert cfg.decision.hard_merge_window_seconds == 1.0
    assert cfg.decision.batch_max_seconds == 10.0
    assert cfg.decision.urgent_seconds == 2.0
    assert cfg.decision.cooldown_seconds == 30.0
    assert cfg.decision.ratio_threshold == 0.2
    assert cfg.decision.veto_marker == "[不回复]"
    assert cfg.decision.groups == {}
    assert cfg.reply.max_segments == 3
    assert cfg.reply.segment_delay_enabled is True
    assert cfg.reply.segment_delay_per_char_ms == 60.0
    assert cfg.reply.segment_delay_min_ms == 150.0
    assert cfg.reply.segment_delay_max_ms == 1500.0
    assert cfg.chat.strip_action_parens is False
    assert cfg.enabled_adapters == []


def test_attention_defaults_and_overrides():
    attention = Config().attention
    assert attention.talk_value == 0.75
    assert attention.engaged_window_seconds == 120
    for probability in (0, 0.35, 1):
        attention = Config.from_raw({"attention": {"talk_value": probability, "engaged_window_seconds": 0}}).attention
        assert attention.talk_value == probability
        assert attention.engaged_window_seconds == 0


def test_config_from_dict_and_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_KEY", "sk-test")
    path = tmp_path / "config.yaml"
    path.write_text(
        """
llm:
  base_url: "http://localhost:8000/v1"
  api_key: "${MY_KEY}"
  chat_model: "m-chat"
  extract_model: "m-extract"
persona:
  path: "data/P.md"
memory:
  extract_every_n: 7
  inject_limit: 9
chat:
  bot_name: "小R"
plugins:
  dir: "myplugins"
  enabled: ["hello"]
adapters:
  console: {}
  snowluma:
    ws_url: "ws://x"
database_path: "data/x.db"
""",
        encoding="utf-8",
    )
    cfg = Config.load(path)
    assert cfg.llm.api_key == "sk-test"
    assert cfg.llm.model_for("extract") == "m-extract"
    assert cfg.llm.model_for("consolidate") == "m-chat"
    assert cfg.persona_path == "data/P.md"
    assert cfg.memory.extract_every_n == 7
    assert cfg.chat.bot_name == "小R"
    assert cfg.plugins.enabled == ["hello"]
    assert cfg.enabled_adapters == ["console", "snowluma"]
    assert cfg.database_path == "data/x.db"


def test_config_missing_file_returns_defaults(tmp_path):
    cfg = Config.load(tmp_path / "nope.yaml")
    assert cfg.llm.chat_model == Config().llm.chat_model


def test_config_ignores_unknown_keys():
    cfg = Config.from_dict({"llm": {"chat_model": "x", "unknown": 1}, "whatever": True})
    assert cfg.llm.chat_model == "x"


def test_config_empty_adapter_is_disabled():
    cfg = Config.from_dict({"adapters": {"console": {}, "snowluma": None}})
    assert cfg.enabled_adapters == ["console"]


def test_config_decision_section():
    cfg = Config.from_dict(
        {
            "decision": {
                "base": 0.05,
                "threshold": 0.9,
                "merge_window_seconds": 2,
                "aliases": ["小R", "Ruben"],
                "groups": {
                    "123": {"activity_multiplier": 1.5, "base": 0.2, "quiet_hours": ["23:00-07:30"]},
                    "456": {"quiet_hours": "00:00-06:00"},
                },
            }
        }
    )
    assert cfg.decision.base == 0.05
    assert cfg.decision.threshold == 0.9
    assert cfg.decision.merge_window_seconds == 2
    assert cfg.decision.aliases == ["小R", "Ruben"]
    group = cfg.decision.groups["123"]
    assert group.activity_multiplier == 1.5
    assert group.base == 0.2
    assert group.quiet_hours == ["23:00-07:30"]
    # 字符串形式也接受
    assert cfg.decision.groups["456"].quiet_hours == ["00:00-06:00"]


# ----------------------------------------------------------------------
# 人格
# ----------------------------------------------------------------------


def test_load_persona_creates_default(tmp_path):
    path = tmp_path / "data" / "SOUL.md"
    persona = load_persona(path)
    assert path.exists()
    assert persona.text == DEFAULT_PERSONA
    assert "Ruben" in persona.system_prompt()


def test_load_persona_custom_name(tmp_path):
    path = tmp_path / "p.md"
    path.write_text("# 小满\n\n你是一只猫。\n", encoding="utf-8")
    persona = load_persona(path)
    assert persona.name == "小满"
    assert "你是一只猫" in persona.system_prompt()


@pytest.mark.parametrize("position", ["before", "after"])
def test_persona_reply_examples(tmp_path, position):
    soul = tmp_path / "SOUL.md"
    soul.write_text("# 小满", encoding="utf-8")
    examples = tmp_path / "examples.json"
    examples.write_text(json.dumps([None, {"text": 7}, " ", {"text": "  第一条 "}, "第一条", "第二条", "第三条"]), encoding="utf-8")
    cfg = Config.from_dict({"persona": {
        "path": str(soul), "examples_path": str(examples), "examples_position": position, "examples_count": 2,
    }})
    persona = load_persona(cfg.persona_path, **cfg.persona_example_options())
    section = "## 我说话的样子\n- 第一条\n- 第二条"
    parts = (section, "# 小满") if position == "before" else ("# 小满", section)
    assert persona.system_prompt() == "\n\n".join(parts) + "\n"
    assert soul.read_text(encoding="utf-8") == persona.text == "# 小满"
    examples.write_text('["改过的示例"]', encoding="utf-8")
    assert "改过的示例" in persona.system_prompt()


@pytest.mark.parametrize("data", [None, "{", "{}", "[]"])
def test_persona_missing_or_invalid_examples_leave_prompt_unchanged(tmp_path, data):
    examples = tmp_path / "examples.json"
    if data is not None:
        examples.write_text(data, encoding="utf-8")
    persona = load_persona(tmp_path / "SOUL.md", examples_path=examples)
    assert persona.system_prompt() == persona.text.strip() + "\n"


@pytest.mark.parametrize("options", [{"examples_count": 0}, {"examples_enabled": False}])
def test_persona_can_omit_valid_examples(tmp_path, options):
    examples = tmp_path / "examples.json"
    examples.write_text('["不该出现"]', encoding="utf-8")
    cfg = Config.from_dict({"persona": {**options, "examples_path": str(examples)}})
    persona = load_persona(tmp_path / "SOUL.md", **cfg.persona_example_options())
    assert persona.system_prompt() == persona.text.strip() + "\n"


def test_persona_example_defaults_and_fallback():
    legacy = Config.from_dict({"persona": "my-soul.md"})
    assert legacy.persona_path == "my-soul.md"
    assert legacy.persona_example_options() == {
        "examples_enabled": True, "examples_path": "data/reply_examples.json",
        "examples_count": 5, "examples_position": "after",
    }
    invalid = Config.from_dict({"persona": {"examples_count": "many", "examples_position": "middle"}})
    assert invalid.persona_example_options() == legacy.persona_example_options()


def test_persona_name_fallback(tmp_path):
    path = tmp_path / "p.md"
    path.write_text("你是阿七，一个普通人。", encoding="utf-8")
    assert load_persona(path).name == "阿七"


# ----------------------------------------------------------------------
# LLM
# ----------------------------------------------------------------------


def make_client(handler, **kwargs) -> LLMClient:
    transport = httpx.MockTransport(handler)
    kwargs.setdefault("retry_delays", (0.0, 0.0))
    return LLMClient(LLMConfig(api_key="k", chat_model="m", extract_model="m-extract", max_retries=1), transport=transport, **kwargs)


# ----------------------------------------------------------------------
# 角色表与回退链
# ----------------------------------------------------------------------


def test_model_for_prebuilt_roles_fall_back_to_chat():
    cfg = LLMConfig(chat_model="m-chat")
    for role in ("chat", "extract", "consolidate", "fast", "summary", "vision", "自定义角色"):
        assert cfg.model_for(role) == "m-chat"
    assert cfg.model_for() == "m-chat"


def test_model_for_exact_role_then_fallback_chain():
    cfg = LLMConfig(
        chat_model="m-chat",
        models={"fast": "m-fast", "summary": "m-summary", "vision": "m-vision", "custom": "m-custom"},
    )
    assert cfg.model_for("fast") == "m-fast"
    # 没配的预置角色回退 chat；自定义角色也回退 chat
    assert cfg.model_for("extract") == "m-chat"
    assert cfg.model_for("consolidate") == "m-chat"
    assert cfg.model_for("custom") == "m-custom"
    assert cfg.model_for("unknown") == "m-chat"


def test_model_for_legacy_fields_still_compatible():
    cfg = LLMConfig(chat_model="m-chat", extract_model="m-extract", consolidate_model="m-cons")
    assert cfg.model_for("extract") == "m-extract"
    assert cfg.model_for("consolidate") == "m-cons"
    assert cfg.model_for("fast") == "m-chat"
    # 两种写法并存时 models 优先
    cfg.models = {"extract": "m-new"}
    assert cfg.model_for("extract") == "m-new"


def test_model_for_custom_roles_fall_back_to_chat():
    cfg = LLMConfig(chat_model="m-chat", models={"fast": "m-fast", "summary": "m-fast"})
    assert cfg.model_for("summary") == "m-fast"
    # 未知角色不会沿别人的链走，直接回 chat
    assert cfg.model_for("nonexistent") == "m-chat"


def test_llm_config_normalizes_models():
    cfg = LLMConfig(models={"fast": " m-f ", "empty": "", "none": None, " ": "x", "n": 3})
    assert cfg.models == {"fast": "m-f", "n": "3"}
    assert cfg.model_for("n") == "3"


def test_config_from_dict_loads_models_map():
    cfg = Config.from_dict({"llm": {"chat_model": "m-chat", "models": {"fast": "m-fast", "summary": "m-sum"}}})
    assert cfg.llm.model_for("fast") == "m-fast"
    assert cfg.llm.model_for("summary") == "m-sum"
    assert cfg.llm.model_for("vision") == "m-chat"


def test_llm_task_defaults_to_role_and_can_be_customized():
    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    rec = []
    client = LLMClient(
        LLMConfig(api_key="k", chat_model="m", models={"fast": "m-fast"}),
        transport=httpx.MockTransport(handler),
        usage_recorder=rec.append,
        retry_delays=(0.0,),
    )

    async def scenario():
        await client.complete([{"role": "user", "content": "hi"}], role="fast")
        await client.complete([{"role": "user", "content": "hi"}], role="fast", task="title")
        await client.close()

    asyncio.run(scenario())
    assert [u.task for u in rec] == ["fast", "title"]
    assert {u.model for u in rec} == {"m-fast"}


def test_chat_payload_uses_chat_specific_sampling_defaults():
    captured = {}

    def handler(request):
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = LLMClient(
        LLMConfig(api_key="k", chat_model="m", temperature=0.8, chat_temperature=0.8),
        transport=httpx.MockTransport(handler),
    )

    async def scenario():
        await client.complete([{"role": "user", "content": "hi"}], role="chat")
        await client.close()

    asyncio.run(scenario())
    assert captured["temperature"] == 0.8
    assert captured["frequency_penalty"] == 0.3
    assert "min_p" not in captured


def test_llm_complete_payload_and_model():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "你好"}}]})

    client = make_client(handler)

    async def scenario():
        reply = await client.complete([{"role": "user", "content": "hi"}], role="extract", json_mode=True)
        await client.close()
        return reply

    assert asyncio.run(scenario()) == "你好"
    assert captured["url"].endswith("/chat/completions")
    assert captured["auth"] == "Bearer k"
    assert captured["body"]["model"] == "m-extract"
    assert captured["body"]["response_format"] == {"type": "json_object"}


def test_llm_retries_then_succeeds():
    attempts = []

    def handler(request):
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = make_client(handler)

    async def scenario():
        result = await client.complete([{"role": "user", "content": "hi"}])
        await client.close()
        return result

    assert asyncio.run(scenario()) == "ok"
    assert len(attempts) == 2


def test_llm_final_failure_raises():
    def handler(request):
        return httpx.Response(401, text="unauthorized")

    client = make_client(handler)

    async def scenario():
        with pytest.raises(LLMError):
            await client.complete([{"role": "user", "content": "hi"}])
        await client.close()

    asyncio.run(scenario())


def test_llm_missing_choices_raises():
    def handler(request):
        return httpx.Response(200, json={"choices": []})

    client = make_client(handler)

    async def scenario():
        with pytest.raises(LLMError):
            await client.complete([{"role": "user", "content": "hi"}])
        await client.close()

    asyncio.run(scenario())


def test_parse_json_payload():
    assert parse_json_payload('{"a": 1}') == {"a": 1}
    assert parse_json_payload('```json\n[1, 2]\n```') == [1, 2]
    assert parse_json_payload('结果如下：[{"x": 1}] 以上') == [{"x": 1}]
    assert parse_json_payload("") is None
    assert parse_json_payload("不是 JSON") is None


# WebUI 配置

def test_webui_config_defaults_and_from_dict():
    cfg = Config()
    assert cfg.webui.enabled is True
    assert cfg.webui.host == "127.0.0.1"
    assert cfg.webui.port == 8765
    assert cfg.webui.password == ""

    loaded = Config.from_dict(
        {"webui": {"enabled": False, "host": "0.0.0.0", "port": 9000, "password": "pw", "unknown": 1}}
    )
    assert loaded.webui.enabled is False
    assert loaded.webui.host == "0.0.0.0"
    assert loaded.webui.port == 9000
    assert loaded.webui.password == "pw"


def test_is_loopback_host():
    from rubenbot.config import is_loopback_host

    assert is_loopback_host("127.0.0.1")
    assert is_loopback_host("127.9.9.9")
    assert is_loopback_host("localhost")
    assert is_loopback_host("::1")
    assert is_loopback_host("")
    assert not is_loopback_host("0.0.0.0")
    assert not is_loopback_host("192.168.1.2")
    assert not is_loopback_host("example.com")
