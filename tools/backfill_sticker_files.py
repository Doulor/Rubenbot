"""存量表情回填：给索引里 ``file`` 为空的表情补上图片本体。

病灶（2026-11-21 线上）：115 条表情收集时只存了原生引用（QQ 文件名 +
会过期的下载链接），图片本体从未下载，导致 WebUI 无图、发表情无文件。

回填顺序（每条）：

1. 先试索引里存的 URL（``native_refs`` 里的 ``url`` / ``file`` 若是 http 地址）；
2. 再试 OneBot ``get_image``：用 ``native_refs`` 里的文件名，经 SnowLuma
   HTTP 接口取回（与 identity 刷新同一路子）；
3. 成功就用 :meth:`StickerStore.attach_file` 落盘并更新索引，失败不动原记录。

用法::

    python -m tools.backfill_sticker_files --config config.yaml
    python -m tools.backfill_sticker_files --dir data/stickers --limit 20 --dry-run

本工具只读配置、写表情库；不会主动连除配置里 SnowLuma 以外的服务。
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Mapping
from urllib.parse import urlparse

import httpx

from rubenbot.config import Config
from rubenbot.stickers import Sticker, StickerStore

logger = logging.getLogger("backfill_sticker_files")

DOWNLOAD_TIMEOUT = 15.0
_ONEBOT_TIMEOUT = 20.0
# 这些状态码基本等价于「链接已过期」（QQ 多媒体地址过期后返回 4xx）；
# 5xx 算普通失败，下次重跑还有机会。
_EXPIRED_STATUSES = {400, 401, 403, 404, 410}


@dataclass
class Counts:
    """回填结果计数；``expired`` 表示链接或平台侧文件已失效。"""

    success: int = 0
    failed: int = 0
    expired: int = 0
    skipped: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "success": self.success,
            "failed": self.failed,
            "expired": self.expired,
            "skipped": self.skipped,
        }


@dataclass
class Attempt:
    """一次取字节的结果：``data`` 非空即成功，``expired`` 标记失效。"""

    data: bytes | None = None
    expired: bool = False
    reason: str = ""


Fetch = Callable[[str], Awaitable["Attempt"]]
FetchImage = Callable[[str], Awaitable["Attempt"]]


def url_candidates(sticker: Sticker) -> list[str]:
    """从索引记录里挑出可下载的 http(s) 地址（去重保序）。"""
    urls: list[str] = []
    refs = sticker.native_refs if isinstance(sticker.native_refs, Mapping) else {}
    for ref in refs.values():
        if not isinstance(ref, Mapping):
            continue
        data = ref.get("data") if isinstance(ref.get("data"), Mapping) else ref
        for key in ("url", "file"):
            value = str(data.get(key) or "").strip()
            if value.startswith(("http://", "https://")) and value not in urls:
                urls.append(value)
    return urls


def native_file_names(sticker: Sticker) -> list[str]:
    """从索引记录里挑出 OneBot ``get_image`` 可用的文件名（去重保序）。"""
    names: list[str] = []
    refs = sticker.native_refs if isinstance(sticker.native_refs, Mapping) else {}
    for ref in refs.values():
        if not isinstance(ref, Mapping):
            continue
        data = ref.get("data") if isinstance(ref.get("data"), Mapping) else ref
        for key in ("file", "filename", "name"):
            value = str(data.get(key) or "").strip()
            if not value or value.startswith(("http://", "https://")):
                continue
            if value not in names:
                names.append(value)
    return names


class OneBotImageFetcher:
    """经 OneBot v11 HTTP ``get_image`` 取图（SnowLuma 同款接口）。

    ``get_image`` 返回的 ``data.file`` 可能是协议端本地路径、也可能是 http
    地址；前者在本机可读时直接读盘，后者下载。过期/找不到都归为 expired。
    """

    def __init__(self, http_url: str, *, access_token: str = "", timeout: float = _ONEBOT_TIMEOUT):
        self.http_url = http_url.rstrip("/")
        self.access_token = access_token
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            headers = {"Authorization": f"Bearer {self.access_token}"} if self.access_token else {}
            self._client = httpx.AsyncClient(base_url=self.http_url, timeout=self.timeout, headers=headers)
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    async def get_image(self, file: str) -> Attempt:
        try:
            client = await self._get_client()
            response = await client.post("/get_image", json={"file": file})
        except httpx.HTTPError as exc:
            return Attempt(reason=f"get_image 请求失败：{exc}")
        if response.status_code >= 400:
            expired = response.status_code in _EXPIRED_STATUSES
            return Attempt(expired=expired, reason=f"get_image 返回 HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError:
            return Attempt(reason="get_image 响应不是 JSON")
        if not isinstance(payload, Mapping):
            return Attempt(reason="get_image 响应格式异常")
        if payload.get("status") == "failed":
            message = str(payload.get("message") or payload.get("wording") or "get_image 失败")
            return Attempt(expired=_looks_expired(message), reason=message)
        data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
        target = str(data.get("url") or data.get("file") or "").strip()
        if not target:
            return Attempt(expired=True, reason="get_image 没有返回文件")
        return await self._read_target(target)

    async def _read_target(self, target: str) -> Attempt:
        """``get_image`` 给出的目标：http 地址下载，本地路径直接读。"""
        if target.startswith(("http://", "https://")):
            return await download_url(target)
        path = Path(target)
        try:
            if path.is_file():
                return Attempt(data=path.read_bytes())
        except OSError as exc:
            return Attempt(reason=f"读取协议端文件失败：{exc}")
        return Attempt(expired=True, reason=f"协议端文件不存在：{target}")


def _looks_expired(message: str) -> bool:
    text = message.lower()
    return any(word in text for word in ("过期", "expired", "not found", "不存在", "失效"))


def _public_host(url: str) -> bool:
    """只下载公网 http(s) 地址，避免配置文件里的链接变成内网跳板。"""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return False
        host = parsed.hostname
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return True  # 域名交给连接层解析；这里不做 DNS 预检
        return address.is_global and not address.is_multicast and not address.is_reserved
    except ValueError:
        return False


async def download_url(url: str) -> Attempt:
    """下载 http(s) 图片；4xx 视为过期，其余网络错误视为失败。"""
    if not _public_host(url):
        return Attempt(reason="不是可下载的公网地址")
    try:
        async with httpx.AsyncClient(
            timeout=DOWNLOAD_TIMEOUT, follow_redirects=True, trust_env=False,
        ) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        return Attempt(reason=f"下载失败：{exc}")
    if response.status_code >= 400:
        return Attempt(
            expired=response.status_code in _EXPIRED_STATUSES,
            reason=f"下载返回 HTTP {response.status_code}",
        )
    return Attempt(data=response.content or None)


async def backfill(
    store: StickerStore,
    *,
    fetch: Fetch = download_url,
    fetch_image: FetchImage | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    max_file_bytes: int | None = None,
) -> Counts:
    """遍历 file 为空的条目依次尝试 URL / get_image；返回计数。"""
    counts = Counts()
    targets = [item for item in store.find_by_tags(None) if not item.file]
    if limit is not None:
        targets = targets[:limit]
    for sticker in targets:
        attempts: list[Attempt] = []
        data: bytes | None = None
        for url in url_candidates(sticker):
            attempt = await fetch(url)
            attempts.append(attempt)
            if attempt.data:
                data = attempt.data
                break
            logger.info("表情 %s 的索引 URL 取图失败：%s", sticker.id, attempt.reason)
        if data is None and fetch_image is not None:
            for name in native_file_names(sticker):
                attempt = await fetch_image(name)
                attempts.append(attempt)
                if attempt.data:
                    data = attempt.data
                    break
                logger.info("表情 %s 的 get_image 取图失败：%s", sticker.id, attempt.reason)
        if data is None:
            if any(attempt.expired for attempt in attempts):
                counts.expired += 1
            elif attempts:
                counts.failed += 1
            else:
                counts.skipped += 1
            continue
        if dry_run:
            counts.success += 1
            continue
        if store.attach_file(sticker.id, data, max_file_bytes=max_file_bytes):
            counts.success += 1
        else:
            counts.failed += 1
            logger.warning("表情 %s 取到字节但格式不受支持或超过上限，未落盘", sticker.id)
    return counts


async def run(config_path: str | Path, *, limit: int | None = None, dry_run: bool = False,
              http_url: str = "") -> Counts:
    """按配置文件加载表情库并执行回填。"""
    config = Config.load(config_path)
    store = StickerStore(config.stickers.dir)
    snow = config.adapter_options("snowluma")
    base_url = str(http_url or snow.get("http_url") or "").strip()
    fetcher: OneBotImageFetcher | None = None
    if base_url:
        fetcher = OneBotImageFetcher(base_url, access_token=str(snow.get("access_token") or ""))
    try:
        counts = await backfill(
            store,
            fetch_image=fetcher.get_image if fetcher is not None else None,
            limit=limit,
            dry_run=dry_run,
            max_file_bytes=config.stickers.max_file_bytes,
        )
    finally:
        if fetcher is not None:
            await fetcher.close()
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="回填表情库缺失的图片本体")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径（读 stickers.dir 与 SnowLuma http_url）")
    parser.add_argument("--dir", default="", help="直接指定表情库目录，覆盖配置里的 stickers.dir")
    parser.add_argument("--http-url", default="", help="覆盖 SnowLuma HTTP 地址")
    parser.add_argument("--limit", type=int, default=0, help="最多处理多少条（0 表示全部）")
    parser.add_argument("--dry-run", action="store_true", help="只统计能取到图的条目，不落盘")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        if args.dir:
            counts = asyncio.run(_run_dir(args.dir, args.http_url, args.limit or None, args.dry_run))
        else:
            counts = asyncio.run(run(
                args.config, limit=args.limit or None, dry_run=args.dry_run, http_url=args.http_url,
            ))
    except FileNotFoundError as exc:
        print(f"配置或表情库不存在：{exc}", file=sys.stderr)
        return 1
    print(json.dumps(counts.as_dict(), ensure_ascii=False))
    return 0 if not counts.failed else 1


async def _run_dir(dir_path: str, http_url: str, limit: int | None, dry_run: bool) -> Counts:
    store = StickerStore(dir_path)
    fetcher = OneBotImageFetcher(http_url) if http_url else None
    try:
        return await backfill(
            store, fetch_image=fetcher.get_image if fetcher else None,
            limit=limit, dry_run=dry_run,
        )
    finally:
        if fetcher is not None:
            await fetcher.close()


if __name__ == "__main__":  # pragma: no cover - 命令行入口
    raise SystemExit(main())
