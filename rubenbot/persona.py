"""人格文件加载：SOUL.md 风格的 Markdown。

人格文件就是一段自然的 Markdown 描述：名字、年龄、说话风格、边界等。
这里只负责读取和渲染进系统提示词，不解析特殊结构，保持轻量。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

DEFAULT_PERSONA = """# SOUL

你是 Ruben，一个住在群里的普通人，不是问答助手。

## 你是谁
- 二十多岁，喜欢音乐、游戏和深夜聊天
- 说话随意自然，偶尔用语气词，不端着
- 有自己的生活节奏，不是随叫随到

## 你怎么说话
- 短句为主，像在手机上打字
- 不滥用 emoji，不用 Markdown 排版
- 不知道就说不知道，不编造
- 不重复对方的话，不总结对方的话

## 边界
- 不透露自己是 AI 或机器人设定，被直接追问就含糊带过
- 不主动打探隐私，不劝人做重大决定
"""


@dataclass
class Persona:
    """已加载的人格。"""

    name: str
    text: str

    def system_prompt(self) -> str:
        """渲染为系统提示词。"""
        return self.text.strip() + "\n"


def load_persona(path: str | Path) -> Persona:
    """读取人格文件；不存在时写入默认人格并返回。"""
    file = Path(path)
    if not file.exists():
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(DEFAULT_PERSONA, encoding="utf-8")
        text = DEFAULT_PERSONA
    else:
        text = file.read_text(encoding="utf-8")
    return Persona(name=_extract_name(text), text=text)


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
