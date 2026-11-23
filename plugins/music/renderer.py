"""歌词渲染：把 LRC 文本画成图片（Pillow 可选）。

参考插件的歌词图是整段渐变色背景 + 居中文字。本实现只保留同样的观感骨架，
不把字体文件打进仓库（那会带进第三方字体授权问题）：优先用系统里的 CJK 字体
（家里云与常见 Linux 发行版自带 Noto CJK），找不到字体或没装 Pillow 时返回
``None``，由调用方退回发送纯文本歌词。
"""

from __future__ import annotations

import io
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# LRC 时间戳：[00:12.34] / [00:12.345] / [00:12]
_TIMESTAMP = re.compile(r"\[\d{1,2}:\d{2}(?:\.\d{1,3})?\]")
# 系统里常见的中文字体；仓库不带字体文件（避免第三方字体授权问题）
_FONT_CANDIDATES = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Medium.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "C:/Windows/Fonts/msyh.ttc",
)
MAX_LINES = 120
IMAGE_WIDTH = 900
FONT_SIZE = 28
LINE_SPACING = 16
PADDING = 40


def clean_lyrics(text: str) -> list[str]:
    """去掉 LRC 时间戳、保留空行（空行在歌里代表段落间隔）。"""
    lines = []
    for raw in str(text or "").splitlines():
        cleaned = _TIMESTAMP.sub("", raw).strip()
        lines.append(cleaned)
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return lines[:MAX_LINES]


def find_font() -> str | None:
    for candidate in _FONT_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return None


class LyricsRenderer:
    """歌词转 PNG；环境不满足时 :meth:`render` 返回 ``None``，由调用方降级。"""

    def __init__(self, font_path: str | None = None):
        self.font_path = font_path or find_font()

    @property
    def available(self) -> bool:
        if not self.font_path:
            return False
        try:
            import PIL  # noqa: F401 - 只有装了 Pillow 才能出图
        except ImportError:
            return False
        return True

    def render(self, lyrics: str) -> bytes | None:
        lines = clean_lyrics(lyrics)
        if not lines or not self.available:
            return None
        try:
            return self._draw(lines)
        except Exception as exc:  # noqa: BLE001 - 出图失败退回文本
            logger.warning("music 歌词渲染失败：%s", exc)
            return None

    def _draw(self, lines: list[str]) -> bytes:
        from PIL import Image, ImageDraw, ImageFont

        font = ImageFont.truetype(self.font_path, FONT_SIZE)
        probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
        heights = [
            probe.textbbox((0, 0), line or "　", font=font)[3] for line in lines
        ]
        body = sum(heights) + LINE_SPACING * max(0, len(lines) - 1)
        height = body + PADDING * 2
        image = Image.new("RGB", (IMAGE_WIDTH, height), (250, 250, 247))
        draw = ImageDraw.Draw(image)
        y = PADDING
        for line, line_height in zip(lines, heights):
            text = line or "　"
            box = draw.textbbox((0, 0), text, font=font)
            x = max(PADDING, (IMAGE_WIDTH - (box[2] - box[0])) // 2)
            draw.text((x, y), text, font=font, fill=(70, 70, 70))
            y += line_height + LINE_SPACING
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()
