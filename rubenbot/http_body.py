"""两个网页抓取入口共用的限额解压，不涉及网络或文本编码。"""

import zlib
from collections.abc import AsyncIterable


class CompressedBodyError(ValueError):
    """压缩正文不受支持、格式错误或解压后超限。"""


async def read_compressed_body(
    chunks: AsyncIterable[bytes], encoding: str, limit: int, *, allow_truncated: bool = False
) -> bytearray:
    """解压正文，最多输出 ``limit`` 字节。

    默认超限抛 :class:`CompressedBodyError`（链接插件、表情下载等需要完整正文）。
    ``allow_truncated=True`` 时按「截断」处理：解到额度就停，返回已解出的部分，
    由调用方补截断标记。抓页只需要前几 KiB 正文，不能因为页面正文比读取
    额度大就整页放弃（实测多数资讯站解压后 35–100 KiB）。
    """
    if encoding not in ('gzip', 'deflate'):
        raise CompressedBodyError('不支持的压缩编码')

    body = bytearray()
    decoder = None
    prefix = b''
    clipped = False
    try:
        async for chunk in chunks:
            while chunk:
                if decoder is None or decoder.eof:
                    if encoding == 'gzip':
                        # 一个 gzip 正文可以含多个成员，共用同一输出额度。
                        wbits = zlib.MAX_WBITS | 16
                    elif decoder is not None:
                        raise CompressedBodyError('压缩正文有多余数据')
                    else:
                        # deflate 也常以 raw 格式发送。凑齐两个字节再校验 zlib 头。
                        if prefix:
                            chunk = prefix + chunk
                            prefix = b''
                        if len(chunk) < 2:
                            prefix = chunk
                            break
                        cmf, flg = chunk[:2]
                        wrapped = cmf & 15 == 8 and cmf >> 4 <= 7 and (cmf * 256 + flg) % 31 == 0
                        wbits = zlib.MAX_WBITS if wrapped else -zlib.MAX_WBITS
                    decoder = zlib.decompressobj(wbits)
                left = limit - len(body)
                # max_length=0 表示无限；多探一个字节，超限就停，不全量展开。
                part = decoder.decompress(chunk, left + 1)
                if len(part) > left:
                    if not allow_truncated:
                        raise CompressedBodyError('正文过大')
                    body.extend(part[:left])
                    clipped = True
                    break
                body.extend(part)
                chunk = decoder.unused_data
            if clipped:
                break
    except zlib.error as exc:
        raise CompressedBodyError('压缩正文格式无效') from exc
    if clipped:
        return body
    if decoder is None or not decoder.eof:
        raise CompressedBodyError('压缩正文不完整')
    return body
