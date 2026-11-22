"""用户文档与当前配置契约的一致性检查。"""

from pathlib import Path

import pytest
import yaml

from rubenbot.config import Config


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CONFIG = ROOT / "config.example.yaml"
USER_DOCS = (ROOT / "README.md", ROOT / "SPEC.md", ROOT / "CHANGELOG.md")

RETIRED_TOKENS = (
    "decision.base",
    "decision.threshold",
    "generation.two_stage",
    "persona.planner.planner_gate",
    "decision.exclude_bot_momentum",
    "emotion.threshold_adjust",
    "_decide_legacy",
    "owner_recent",
    "plugins/privacy",
)


def _key_paths(value, prefix=""):
    """递归枚举 YAML mapping 的点分路径。"""
    if not isinstance(value, dict):
        return
    for key, child in value.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        yield path
        yield from _key_paths(child, path)


def test_shipped_config_example_loads_with_current_defaults():
    """示例配置应可加载，且关键新配置不能悄悄漂移。"""
    config = Config.load(EXAMPLE_CONFIG)

    assert config.privacy.enabled is False
    assert config.memory.timeline_recent_hours == pytest.approx(24)
    assert config.memory.timeline_max_chars == 1800
    assert config.memory.timeline_hit_limit == 6
    assert config.attention.talk_value == pytest.approx(0.75)
    assert config.attention.engaged_window_seconds == pytest.approx(120)
    assert config.reply.max_segments == 3
    assert config.reply.segment_delay_per_char_ms == pytest.approx(130)
    assert config.reply.segment_delay_min_ms == pytest.approx(1500)
    assert config.reply.segment_delay_max_ms == pytest.approx(4000)
    assert config.llm.model_strategy == "failover"
    assert config.enabled_adapters == ["console", "snowluma"]


def test_shipped_config_example_contains_no_retired_keys():
    """配置示例不能把已退役字段带回用户可复制的模板。"""
    raw = yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8")) or {}
    paths = set(_key_paths(raw))
    assert not ({token for token in RETIRED_TOKENS} & paths)
    assert "owner_recent" not in {path.rsplit(".", 1)[-1] for path in paths}


def test_user_docs_do_not_mention_retired_runtime_names():
    """用户文档不能继续宣传已删除的配置、开关或插件。"""
    hits = []
    for path in USER_DOCS:
        text = path.read_text(encoding="utf-8")
        hits.extend(f"{path.name}: {token}" for token in RETIRED_TOKENS if token in text)
    assert not hits, "\n".join(hits)


@pytest.mark.parametrize("path", (ROOT / "README.md", ROOT / "SPEC.md"))
def test_docs_record_current_privacy_and_timeline_contract(path):
    """README 与规格都应记录隐私 core 和跨会话时间线的硬限制。"""
    text = path.read_text(encoding="utf-8")
    for fact in (
        "privacy.enabled",
        "默认",
        "24 小时",
        "1800 字",
        "最多 3 条",
        "160 字",
        "basis",
        "ledger",
        "settled",
        "common",
        "guess",
        "/清空我的数据",
    ):
        assert fact in text, f"{path.name} 缺少文档事实：{fact}"

    if path.name == "README.md":
        assert "rubenbot/privacy/" in text
    else:
        assert "核心隐私模块" in text


@pytest.mark.parametrize("path", (ROOT / "README.md", ROOT / "SPEC.md"))
def test_docs_record_current_reply_and_webui_contract(path):
    """文档应反映当前 1～3 段回复和已落地 WebUI。"""
    text = path.read_text(encoding="utf-8")
    assert "1～3" in text or "1~3" in text
    assert "model_strategy" in text
    assert "failover" in text
    assert "round_robin" in text
    assert "random" in text
    assert "模型" in text and "WebUI" in text
