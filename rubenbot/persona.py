"""人格文件加载：单文件 SOUL.md 与五文件人设目录。

人格文件就是一段自然的 Markdown 描述：名字、年龄、说话风格、边界等。
这里只负责读取和渲染进系统提示词，不解析特殊结构，保持轻量。

两种形态：

- **五文件目录**（新）：``persona.md``（常驻人格）、``behavior.md``（策划步
  行为风格）、``reply_style.md``（生成步回复风格）、``identity.md``（话题
  触发时注入）、``owner.md``（只在私聊主人时注入）。按步组装，见
  :meth:`Persona.system_prompt`。
- **单文件 SOUL.md**（旧）：整个文件当作人格正文，其余部分为空。目录不存在
  时自动回退到这种形态，保证旧部署照常运行。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

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

# 五文件人设的固定文件名（不含扩展名，顺序即展示顺序）
PERSONA_FILES: tuple[str, ...] = ("persona", "behavior", "reply_style", "identity", "owner")

# 身份话题的默认触发词：宁可多注入一次，也别在身份/生活事实问题上露馅。
DEFAULT_IDENTITY_TRIGGERS: tuple[str, ...] = (
    "机器人", "AI", "人机", "真人", "人工智能", "你到底是什么", "吃什么",
    "住哪", "多大", "睡觉", "上班族",
)

_HASH_HEADING_RE = re.compile(r"^#+\s*")


def persona_dir_for(path: str | Path, configured: str | Path | None = None) -> Path:
    """五文件人设目录：显式配置优先，否则取人格文件同级的 ``persona/``。"""
    if configured is not None and str(configured).strip():
        return Path(configured)
    return Path(path).parent / "persona"


def personafile_path(directory: str | Path, name: str) -> Path:
    """某个五文件人设文件的路径。"""
    if name not in PERSONA_FILES:
        raise ValueError(f"未知的人设文件：{name}")
    return Path(directory) / f"{name}.md"


def load_persona_parts(directory: str | Path) -> dict[str, str]:
    """读取目录里的五个人设文件；目录里没有 ``persona.md`` 视为不存在。"""
    base = Path(directory)
    if not (base / "persona.md").is_file():
        return {}
    parts: dict[str, str] = {}
    for name in PERSONA_FILES:
        path = personafile_path(base, name)
        try:
            parts[name] = path.read_text(encoding="utf-8") if path.is_file() else ""
        except OSError:
            parts[name] = ""
    return parts


@dataclass
class Persona:
    """已加载的人格。

    ``parts`` 非空表示五文件形态；为空表示旧的单文件形态，``text`` 就是
    整个 SOUL 文件的内容。``text`` 在五文件形态下始终等于 ``persona.md``，
    便于插件与事件判官继续按「人格正文」使用。
    """

    name: str
    text: str
    examples_path: str | Path | None = DEFAULT_EXAMPLES_PATH
    examples_enabled: bool = True
    examples_count: int = 5
    examples_position: str = "after"
    parts: dict[str, str] = field(default_factory=dict)
    directory: str = ""

    @property
    def multi_file(self) -> bool:
        """是否按五文件形态加载。"""
        return bool(self.parts)

    def file_text(self, name: str) -> str:
        """某个文件的正文；单文件形态下只有 ``persona`` 有内容。"""
        if name == "persona" and not self.parts:
            return self.text
        return self.parts.get(name, "")

    def identity_anchor(self) -> str:
        """只摘机主写下的常驻事实；旧人设没有此节时不替它编出身。"""
        lines: list[str] = []
        inside = False
        for line in self.file_text("identity").splitlines():
            if line.strip() == "## 常驻身份":
                inside = True
            elif inside and line.lstrip().startswith("#"):
                break
            elif inside and line.strip():
                lines.append(line.strip())
        return " ".join(lines)

    def _stage_body(self, stage: str) -> str:
        """按阶段拼出人格正文。

        - ``planner``：策划步 = persona + behavior
        - ``generator``：生成步 = persona + reply_style
        - ``single``：单段生成 = persona + behavior + reply_style
        - ``legacy``：旧路径，只用 SOUL 正文（五文件形态也退回 persona.md）
        """
        if not self.parts:
            return self.text.strip()
        if stage == "planner":
            names = ("persona", "behavior")
        elif stage == "generator":
            names = ("persona", "reply_style")
        elif stage == "single":
            names = ("persona", "behavior", "reply_style")
        else:  # legacy
            names = ("persona",)
        return "\n\n".join(
            self.parts[name].strip() for name in names if self.parts.get(name, "").strip()
        )

    def system_prompt(
        self,
        *,
        stage: str = "single",
        identity: bool = False,
        owner: bool = False,
    ) -> str:
        """渲染某一阶段的系统提示词，并按配置注入可编辑的说话示例。

        ``identity`` / ``owner`` 是条件注入：由调用方判断话题触发与私聊主人，
        这里只负责把对应文件拼进去（两步规则相同）。
        """
        prompt = self._stage_body(stage)
        extras: list[str] = []
        if identity and self.parts.get("identity", "").strip():
            extras.append(self.parts["identity"].strip())
        if owner and self.parts.get("owner", "").strip():
            extras.append(self.parts["owner"].strip())
        if extras:
            prompt = "\n\n".join([prompt, *extras])
        # 回复样例是旧的可编辑特性：只有五文件形态的策划步不带（它只负责
        # 裁定开不开口）；旧路径、单段生成、生成步都照旧注入。
        if self.examples_enabled and not (self.multi_file and stage == "planner"):
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
    persona_dir: str | Path | None = None,
) -> Persona:
    """读取人格：五文件目录存在就用新形态，否则回退单文件（旧部署兼容）。

    单文件不存在时写入默认人格并返回（保持旧行为）。
    """
    directory = persona_dir_for(path, persona_dir)
    parts = load_persona_parts(directory)
    if parts:
        text = parts.get("persona", "") or DEFAULT_PERSONA
        return Persona(
            name=_extract_name(text),
            text=text,
            examples_path=examples_path,
            examples_enabled=examples_enabled is not False,
            examples_count=examples_count,
            examples_position=examples_position,
            parts=parts,
            directory=str(directory),
        )
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


def _trigger_pattern(trigger: str) -> re.Pattern[str]:
    """英文/数字触发词按词边界匹配，避免 ``AI`` 命中 ``said`` 这类子串。"""
    cleaned = trigger.strip()
    pattern = re.escape(cleaned)
    if cleaned.isascii() and re.fullmatch(r"[\w.-]+", cleaned):
        pattern = rf"(?<![A-Za-z0-9_]){pattern}(?![A-Za-z0-9_])"
    return re.compile(pattern, re.IGNORECASE)


def matches_identity_trigger(texts: Iterable[str], triggers: Iterable[str]) -> bool:
    """最近若干条消息里是否出现身份/生活事实类触发词。"""
    patterns = [_trigger_pattern(t) for t in triggers if isinstance(t, str) and t.strip()]
    if not patterns:
        return False
    for text in texts:
        if not isinstance(text, str) or not text:
            continue
        if any(pattern.search(text) for pattern in patterns):
            return True
    return False


def _extract_name(text: str) -> str:
    """从第一行非空标题里尽量猜名字，猜不到就用 Ruben。"""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            candidate = _HASH_HEADING_RE.sub("", line).strip()
            # 五文件的标题常写成「persona.md — 常驻人格」，这不是名字
            if candidate.lower().startswith("persona.md"):
                break
            if candidate and candidate.upper() not in {"SOUL", "人格", "PERSONA"}:
                return candidate
        break
    for token in ("你是", "你叫", "我叫", "名字是"):
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
