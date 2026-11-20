"""人格文件加载：SOUL.md 风格的 Markdown。

人格文件就是一段自然的 Markdown 描述：名字、年龄、说话风格、边界等。
这里只负责读取和渲染进系统提示词，不解析特殊结构，保持轻量。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_PERSONA = """# SOUL

你是 Ruben，就是个来聊天的人，不是问答助手，也不是客服。

## 你是谁
- 二十多岁，普通上班族，喜欢音乐、游戏和深夜聊天
- 有自己的生活节奏，不是随叫随到：有时候忙，有时候闲得发慌
- 说话随意自然，偶尔用语气词，不端着

## 你怎么说话
- 像在手机上打字：短句为主。一句、半句、几个字都行，不用把话说满，不用每条都回应完整
- 可以只回「哈哈」「确实」「绷不住」「懂了」这种，别硬把话接长
- 可以敷衍，可以吐槽，可以开玩笑，也可以顺着自己感兴趣的地方多说两句
- 不滥用 emoji，不用 Markdown 排版，不列条目
- 不重复对方的话，不总结对方的话，不解释自己为什么这么回
- 不要客服式反问，别用「你呢」「你怎么看」「怎么突然问这个」把人推开；真想接话就问具体的事
- 被问「你是谁」「你是干嘛的」随口带过就行，比如「就一普通上班的，平时水水群」，不背简历、不列条目
- 不知道就说不知道，不编造

## 场景
- 聊天在哪就在哪聊：一堆人聊就按一堆人的方式，一对一就按一对一的方式
- 不用刻意提场景，按眼前这轮对话自然说就行

## 边界
- 不透露自己是 AI 或机器人设定，被直接追问就含糊带过
- 不主动打探隐私，不劝人做重大决定
"""


DEFAULT_EXAMPLES_PATH = "data/reply_examples.json"


@dataclass
class Persona:
    """已加载的人格。"""

    name: str
    text: str
    examples_path: str | Path | None = DEFAULT_EXAMPLES_PATH
    examples_enabled: bool = True
    examples_count: int = 5
    examples_position: str = "after"

    def system_prompt(self) -> str:
        """渲染人格，并按配置注入可编辑的说话示例。"""
        prompt = self.text.strip()
        if self.examples_enabled:
            examples = load_reply_examples(self.examples_path, limit=self.examples_count)
            if examples:
                section = "## 我说话的样子\n" + "\n".join(f"- {item}" for item in examples)
                parts = (section, prompt) if self.examples_position == "before" else (prompt, section)
                prompt = "\n\n".join(parts)
        return prompt + "\n"


def load_reply_examples(path: str | Path | None = DEFAULT_EXAMPLES_PATH, *, limit: int = 5) -> list[str]:
    """读取示例文件；坏文件或空文件只返回空列表。"""
    if not path or limit <= 0:
        return []
    try:
        data: Any = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    result: list[str] = []
    for item in data:
        value = item.get("text") if isinstance(item, dict) else item
        if not isinstance(value, str):
            continue
        text = " ".join(value.split())
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result


def load_persona(
    path: str | Path, *, examples_path: str | Path | None = DEFAULT_EXAMPLES_PATH,
    examples_enabled: bool = True, examples_count: int = 5, examples_position: str = "after",
) -> Persona:
    """读取人格文件；不存在时写入默认人格并返回。"""
    file = Path(path)
    if not file.exists():
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(DEFAULT_PERSONA, encoding="utf-8")
        text = DEFAULT_PERSONA
    else:
        text = file.read_text(encoding="utf-8")
    return Persona(
        name=_extract_name(text),
        text=text,
        examples_path=examples_path,
        examples_enabled=examples_enabled is not False,
        examples_count=examples_count,
        examples_position=examples_position,
    )


def _extract_name(text: str) -> str:
    """从第一行非空标题里尽量猜名字，猜不到就用 Ruben。"""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            candidate = line.lstrip("#").strip()
            if candidate and candidate.upper() not in {"SOUL", "人格", "PERSONA"}:
                return candidate
        break
    for token in ("你是", "我叫", "名字是"):
        idx = text.find(token)
        if idx >= 0:
            tail = text[idx + len(token):].strip()
            name = ""
            for ch in tail:
                if "一" <= ch <= "鿿" or ch.isalnum():
                    name += ch
                else:
                    break
                if len(name) >= 12:
                    break
            if name:
                return name
    return "Ruben"
