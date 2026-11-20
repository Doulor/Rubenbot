"""真机与访客文件操作。访客沿已打开的目录描述符逐级定位，永不跟随链接。"""

from __future__ import annotations

import asyncio
import os
import stat
import weakref
from contextlib import contextmanager
from pathlib import Path
from typing import Mapping


_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_MARKER = "\n[输出已截断]".encode()
# 上下文每次调用重建；同一访客的命令和文件操作共用锁，闲置锁不积累。
_LOCKS: weakref.WeakValueDictionary[Path, asyncio.Lock] = weakref.WeakValueDictionary()


class ComputerFiles:
    def __init__(self, *, root: Path | str | None, quota_bytes: int = 100 * 1024**2,
                 timeout: float = 60, output_limit: int = 32768, env: Mapping[str, str] | None = None):
        if quota_bytes <= 0 or timeout <= 0 or output_limit < 1:
            raise ValueError("执行限额必须为正数")
        self.root = None if root is None else Path(root).absolute()
        self.quota_bytes = int(quota_bytes)
        self.timeout = float(timeout)
        self.output_limit = int(output_limit)
        self.env = dict(env or {})
        self.lock = asyncio.Lock()
        if self.root is not None:
            # 工作区的父目录由部署者持有；用户绝不能写自己或他人的父目录。
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.lock = _LOCKS.setdefault(self.root, self.lock)

    @property
    def is_guest(self) -> bool:
        return self.root is not None

    @staticmethod
    def _parts(path: str) -> tuple[str, ...]:
        if not path or '\x00' in path:
            raise ValueError("路径无效")
        p = Path(path)
        if p.is_absolute() or '..' in p.parts:
            raise ValueError("访客只允许工作区内的相对路径")
        return p.parts

    @contextmanager
    def _directory(self, parts, *, create=False):
        fd = os.open(self.root, _DIRECTORY)
        try:
            for part in parts:
                if create:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                child = os.open(part, _DIRECTORY, dir_fd=fd)
                os.close(fd)
                fd = child
            yield fd
        finally:
            os.close(fd)

    @contextmanager
    def _file(self, path: str, *, write=False):
        flags = os.O_NONBLOCK | (os.O_WRONLY | os.O_CREAT if write else os.O_RDONLY)
        if self.root is None:
            if write:
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, flags, 0o600)
        else:
            parts = self._parts(path)
            if not parts:
                raise ValueError("请指定普通文件")
            with self._directory(parts[:-1], create=write) as parent:
                fd = os.open(parts[-1], flags | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or (self.is_guest and info.st_nlink != 1):
                raise ValueError("只允许普通文件，访客文件不得有硬链接")
            with os.fdopen(fd, 'wb' if write else 'rb', closefd=False) as stream:
                yield stream, info.st_size
        finally:
            os.close(fd)

    def _walk_size(self) -> int:
        """统计普通文件的逻辑字节数，不跟随树内链接；失败时不低估用量。"""
        def fail(exc):
            raise exc

        total = 0
        if self.root is not None:
            with self._directory(()) as root_fd:
                for _, _, files, fd in os.fwalk('.', dir_fd=root_fd, onerror=fail):
                    for name in files:
                        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                        if stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                            total += info.st_size
        return total

    def _output(self, data: bytes) -> str:
        # 解码替换字符也占字节；截断边界不拆分 UTF-8，提示计入总上限。
        raw = data[:self.output_limit + 1].decode('utf-8', 'replace').encode()
        if len(raw) <= self.output_limit and len(data) <= self.output_limit:
            return raw.decode()
        mark = _MARKER[:self.output_limit].decode(errors='ignore')
        return raw[:self.output_limit - len(mark.encode())].decode(errors='ignore') + mark

    def _read(self, path, attachment):
        with self._file(path) as (stream, size):
            if self.is_guest and size > self.quota_bytes:
                raise ValueError("文件超过访客配额")
            if not attachment:
                return self._output(stream.read(self.output_limit + 1))
            if not self.is_guest:
                return stream.read()
            # 检查已打开快照的大小；读取时也设限，而非仅依赖先前的 stat。
            data = stream.read(size + 1)
            if len(data) > size:
                raise ValueError("文件读取期间发生变化")
            return data

    async def read_file(self, path: str) -> str:
        async with self.lock:
            return await self._io(self._read, path, False)

    async def file_bytes(self, path: str) -> bytes:
        async with self.lock:
            return await self._io(self._read, path, True)

    @staticmethod
    async def _io(func, *args):
        # 调用取消不等于后台文件操作结束；完成后才能放锁给下一次命令。
        task = asyncio.create_task(asyncio.to_thread(func, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    def _write(self, path, data):
        if self.is_guest and len(data) > self.quota_bytes:
            raise ValueError("单文件超过访客配额")
        total = self._walk_size()
        if self.is_guest:
            # 新文件超过剩余额度时，在创建文件或目录之前拒绝。
            try:
                with self._file(path) as (_, previous):
                    pass
            except FileNotFoundError:
                previous = 0
            if total - previous + len(data) > self.quota_bytes:
                raise ValueError("访客目录超过配额")
        with self._file(path, write=True) as (stream, _):
            stream.truncate(0)
            stream.write(data)
        return f"已写入 {len(data)} 字节。"

    async def write_file(self, path: str, content: str) -> str:
        async with self.lock:
            return await self._io(self._write, path, content.encode())

    def _list_at(self, location):
        entries = []
        amount = 0
        with os.scandir(location) as it:
            for entry in it:
                line = entry.name + ('/' if entry.is_dir(follow_symlinks=False) else '') + '\n'
                entries.append(line)
                amount += len(os.fsencode(line))
                if amount > self.output_limit:
                    break
        return self._output(os.fsencode(''.join(sorted(entries))))

    def _list(self, path):
        if self.root is None:
            return self._list_at(path)
        with self._directory(self._parts(path)) as fd:
            return self._list_at(fd)

    async def list_dir(self, path: str = '.') -> str:
        async with self.lock:
            return await self._io(self._list, path)
