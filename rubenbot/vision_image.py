"""送视觉模型前的图片预处理：按需下采样、压缩，压短识图耗时。

线上实测视觉描述要 6–13 秒，其中像素量与上传体积是主要成本。这里只做
一件事：图片长边超过上限（或字节数偏大）时，缩到 ``max_side`` 以内并重新
编码成 JPEG，再交给视觉模型；能看清字、图和常见截图即可，不追求原图质量。

压缩是**可选能力**，不引入核心依赖：

1. 装了 Pillow 就用它（按需导入，没装不影响启动）；
2. 否则用系统里的 ``ffmpeg``（家里云自带）；
3. 都没有、或压缩失败时，原样返回原图——宁可不压，也不能因为压缩把识图
   搞坏。

参数来自 ``stickers.vision_image_max_side`` / ``vision_image_quality``；
``max_side <= 0`` 表示不压缩。图片格式识别不清、尺寸未知时按字节数判断，
只对小字节原图保持原样。
"""

from __future__ import annotations

import asyncio
import io
import logging
import shutil
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

# 原图在这个字节数以内且长边不超上限时不再重编码，避免无谓的画质损失。
KEEP_ORIGINAL_BYTES = 1_500_000
# 外部编码进程的超时（秒）：超时就放弃压缩，按原图送。
ENCODE_TIMEOUT = 10.0

_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

# 注入的编码器签名：``(原图字节, 长边上限或 None, 质量) -> JPEG 字节或 None``
Encoder = Callable[[bytes, int | None, int], Awaitable[bytes | None]]

_JPEG_SOF = frozenset(range(0xC0, 0xC4)) | frozenset(range(0xC5, 0xC8)) | frozenset(range(0xC9, 0xCC)) \
    | frozenset(range(0xCD, 0xD0))


def image_size(data: bytes | bytearray | None) -> tuple[int, int] | None:
    """按文件头读图片宽高；认不出时返回 None（不猜）。"""
    if not data:
        return None
    raw = bytes(data[:64])
    try:
        if raw.startswith(b"\x89PNG\r\n\x1a\n") and raw[12:16] == b"IHDR":
            return int.from_bytes(raw[16:20], "big"), int.from_bytes(raw[20:24], "big")
        if raw.startswith((b"GIF87a", b"GIF89a")):
            return int.from_bytes(raw[6:8], "little"), int.from_bytes(raw[8:10], "little")
        if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
            return _webp_size(bytes(data))
        if raw.startswith(b"\xff\xd8"):
            return _jpeg_size(bytes(data))
    except (IndexError, ValueError):
        return None
    return None


def _webp_size(data: bytes) -> tuple[int, int] | None:
    chunk = data[12:16]
    if chunk == b"VP8X" and len(data) >= 30:
        return (int.from_bytes(data[24:27], "little") + 1,
                int.from_bytes(data[27:30], "little") + 1)
    if chunk == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
        return (int.from_bytes(data[26:28], "little") & 0x3FFF,
                int.from_bytes(data[28:30], "little") & 0x3FFF)
    return None


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    index = 2
    while index + 4 <= len(data):
        if data[index] != 0xFF:
            return None
        marker = data[index + 1]
        if marker == 0xFF:  # 填充字节
            index += 1
            continue
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        length = int.from_bytes(data[index + 2:index + 4], "big")
        if marker in _JPEG_SOF:
            if index + 9 > len(data):
                return None
            return (int.from_bytes(data[index + 7:index + 9], "big"),
                    int.from_bytes(data[index + 5:index + 7], "big"))
        index += 2 + length
    return None


def needs_shrink(data: bytes, suffix: str | None, max_side: int) -> bool:
    """要不要重编码：长边超上限，或原图体积偏大。"""
    if max_side <= 0 or not data:
        return False
    size = image_size(data)
    if size is not None and max(size) > max_side:
        return True
    return len(data) > KEEP_ORIGINAL_BYTES


async def prepare_image(
    data: bytes,
    suffix: str | None,
    *,
    max_side: int,
    quality: int,
    encoder: Encoder | None = None,
) -> tuple[bytes, str]:
    """按需下采样后返回 ``(图片字节, MIME)``；不需要或压不了时原样返回。"""
    mime = _MIME_BY_SUFFIX.get(suffix or "", "image/png")
    if not needs_shrink(data, suffix, max_side):
        return data, mime
    try:
        shrunk = await _encode(data, max_side or None, quality, encoder)
    except Exception as exc:  # noqa: BLE001 - 压缩失败退回原图，不影响识图
        logger.warning("图片压缩失败，按原图送视觉模型：%s", type(exc).__name__)
        shrunk = None
    if not shrunk:
        return data, mime
    if len(shrunk) >= len(data):
        # 内容简单、原图本来就小：重编码反而更大时保留原图（不失真也不加负）。
        return data, mime
    logger.debug("视觉图片已压缩 %d → %d 字节", len(data), len(shrunk))
    return shrunk, "image/jpeg"


async def _encode(data: bytes, side: int | None, quality: int, encoder: Encoder | None) -> bytes | None:
    if encoder is not None:
        return await encoder(data, side, quality)
    shrunk = await _encode_with_pillow(data, side, quality)
    if shrunk:
        return shrunk
    path = shutil.which("ffmpeg")
    if path is None:
        return None
    return await _encode_with_ffmpeg(path, data, side, quality)


async def _encode_with_pillow(data: bytes, side: int | None, quality: int) -> bytes | None:
    try:
        import PIL  # noqa: F401 - 没装 Pillow 时直接走 ffmpeg
    except ImportError:
        return None
    try:
        return await asyncio.to_thread(_pillow_shrink, data, side, quality)
    except Exception as exc:  # noqa: BLE001 - 交给下一条通道
        logger.debug("Pillow 压缩失败：%s", type(exc).__name__)
        return None


def _pillow_shrink(data: bytes, side: int | None, quality: int) -> bytes:
    from PIL import Image

    resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS", None)
    with Image.open(io.BytesIO(data)) as image:
        image.load()
        if side and max(image.size) > side:
            if resampling is not None:
                image.thumbnail((side, side), resampling)
            else:  # Pillow < 9 没有 Resampling 命名空间，先用默认重采样
                image.thumbnail((side, side))
        if image.mode in ("RGBA", "LA", "P"):
            # 透明区域铺白底，避免转 JPEG 后字和线条糊成黑块。
            converted = image.convert("RGBA")
            background = Image.new("RGB", converted.size, (255, 255, 255))
            background.paste(converted, mask=converted.split()[-1])
            image = background
        else:
            image = image.convert("RGB")
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=quality)
        return buffer.getvalue()


def _ffmpeg_quality(quality: int) -> int:
    # ffmpeg 的 -q:v 是 2（最好）–31（最差），与 0–100 的质量大致反向。
    return max(2, min(31, int(round((100 - quality) / 4)) + 2))


async def _encode_with_ffmpeg(path: str, data: bytes, side: int | None, quality: int) -> bytes | None:
    args = [path, "-hide_banner", "-loglevel", "error", "-nostdin", "-i", "pipe:0",
            "-frames:v", "1"]
    if side:
        args += ["-vf", f"scale=w='min(iw,{side})':h='min(ih,{side})':force_original_aspect_ratio=decrease"]
    args += ["-q:v", str(_ffmpeg_quality(quality)), "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"]
    process = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(data), timeout=ENCODE_TIMEOUT)
    except TimeoutError:
        process.kill()
        await process.wait()
        logger.debug("ffmpeg 压缩超时，按原图送视觉模型")
        return None
    if process.returncode != 0 or not stdout:
        return None
    return stdout
