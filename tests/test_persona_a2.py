"""随仓人设的装配回归；具体语气留给主聊审稿，不把范例台词锁死。"""

from pathlib import Path

from rubenbot.persona import load_persona


DATA = Path(__file__).resolve().parents[1] / "data"


def test_shipped_draft_keeps_help_and_evidence_in_both_stages():
    persona = load_persona(DATA / "SOUL.md", examples_enabled=False)
    assert persona.multi_file and persona.name == "Ruben"
    for stage in ("planner", "generator"):
        prompt = persona.system_prompt(stage=stage)
        assert "慢热" in prompt and "群友" in prompt
        assert "拿不准的公开事实去找原文" in prompt and "还没查到" in prompt
        assert "多萝本人在私聊" not in prompt
    assert "群友亲口讲" in persona.system_prompt(stage="planner")
    assert "查到的能说出根据" in persona.system_prompt(stage="generator")
    assert "观点不同" in persona.system_prompt(stage="generator")
