"""识图前预处理：文件头尺寸、按需下采样、压缩失败与原图回退。"""

import asyncio
import struct
import zlib

from rubenbot.vision_image import image_size, needs_shrink, prepare_image


def png(width: int, height: int) -> bytes:
    """造一张纯色 PNG。"""
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    pixels = (b"\x00" + b"\x00" * width * 3) * height
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))


GIF = b"GIF89a" + struct.pack("<HH", 320, 200) + b"\x00" * 4
JPEG_HEADER = (b"\xff\xd8" + b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9
               + b"\xff\xc0" + struct.pack(">HBHH", 17, 8, 480, 640) + b"\x00" * 11)
# VP8X：24–26 是 canvas 宽 -1，27–29 是高 -1
WEBP = (b"RIFF" + struct.pack("<I", 40) + b"WEBP" + b"VP8X" + struct.pack("<I", 10)
        + b"\x00" * 4 + struct.pack("<I", 32)[:3] + struct.pack("<I", 32)[:3] + b"\x00" * 8)


def test_image_size_reads_common_headers():
    assert image_size(png(800, 600)) == (800, 600)
    assert image_size(GIF) == (320, 200)
    assert image_size(JPEG_HEADER) == (640, 480)
    assert image_size(WEBP) == (33, 33)
    assert image_size(b"not an image") is None
    assert image_size(b"") is None


def test_needs_shrink_by_side_and_bytes():
    assert needs_shrink(png(2000, 1000), ".png", 1024)
    assert not needs_shrink(png(800, 600), ".png", 1024)
    # 长边不超上限但体积很大时也要压
    big = b"\x89PNG\r\n\x1a\n" + b"x" * 2_000_000
    assert needs_shrink(big, ".png", 1024)
    # 关闭压缩
    assert not needs_shrink(png(4000, 4000), ".png", 0)


def run(coro):
    return asyncio.run(coro)


def test_prepare_image_uses_encoder_and_reports_jpeg():
    seen = {}

    async def encoder(data, side, quality):
        seen.update(bytes=len(data), side=side, quality=quality)
        return b"\xff\xd8shrunk"

    result, mime = run(prepare_image(png(2000, 1000), ".png", max_side=1024,
                                     quality=80, encoder=encoder))
    assert result == b"\xff\xd8shrunk" and mime == "image/jpeg"
    assert seen["side"] == 1024 and seen["quality"] == 80


def test_prepare_image_keeps_small_original_untouched():
    async def encoder(data, side, quality):  # pragma: no cover - 不应被调用
        raise AssertionError("小图不该重编码")

    data = png(320, 200)
    result, mime = run(prepare_image(data, ".png", max_side=1024, quality=80, encoder=encoder))
    assert result is data and mime == "image/png"


def test_prepare_image_falls_back_to_original_when_encoder_fails():
    async def encoder(data, side, quality):
        raise RuntimeError("编码器炸了")

    data = png(2000, 1000)
    result, mime = run(prepare_image(data, ".png", max_side=1024, quality=80, encoder=encoder))
    assert result is data and mime == "image/png"


def test_prepare_image_keeps_original_when_shrunk_is_larger():
    async def encoder(data, side, quality):
        return data + b"padding"

    data = png(2000, 1000)
    result, mime = run(prepare_image(data, ".png", max_side=1024, quality=80, encoder=encoder))
    assert result is data and mime == "image/png"
