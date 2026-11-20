"""真机 shell、隔离 shell 与无子进程的访客回退。"""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from functools import lru_cache
from pathlib import Path

from .computer_files import ComputerFiles


# 不绑定宿主根、/etc、/proc、/usr/local 或套接字目录。除工作区外根挂载只读。
_RUNTIME = ('--unshare-all', '--cap-drop', 'ALL', '--new-session', '--die-with-parent',
            '--clearenv', '--setenv', 'HOME', '/sandbox', '--setenv', 'PATH', '/usr/bin:/bin',
            '--ro-bind', '/usr/bin', '/usr/bin', '--symlink', 'usr/bin', '/bin',
            '--ro-bind', '/usr/lib', '/usr/lib', '--ro-bind', '/lib', '/lib',
            '--ro-bind-try', '/lib64', '/lib64', '--ro-bind', '/dev/null', '/dev/null')


@lru_cache(maxsize=4)
def _usable(binary: str) -> bool:
    """文件存在不代表命名空间可用。只探测固定程序，不试跑用户命令。"""
    try:
        return subprocess.run([binary, *_RUNTIME, '--remount-ro', '/', '--', '/usr/bin/prlimit',
                               '--', '/bin/true'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, env={}, timeout=2).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


class ComputerContext(ComputerFiles):
    @staticmethod
    def bwrap_path() -> str | None:
        path = shutil.which('bwrap')
        return os.path.abspath(path) if path and _usable(os.path.abspath(path)) else None

    def _fallback(self, command: str) -> str:
        """没有挂载隔离时，不执行二进制、脚本或 shell，只解释这四种命令。"""
        argv = shlex.split(command)
        if not argv or argv[0] not in {'cat', 'head', 'ls', 'wc'}:
            raise ValueError('bubblewrap 不可用；访客仅支持 cat、head、ls、wc')
        name, *args = argv
        if name == 'ls' and len(args) <= 1:
            return self._list(args[0] if args else '.')
        count = 10
        if name == 'head' and args[:1] == ['-n'] and len(args) == 3:
            count = int(args[1])
            args = args[2:]
        if len(args) != 1 or name == 'ls' or count < 0:
            raise ValueError('访客回退只接受单文件参数，不支持 shell 管道或重定向')
        with self._file(args[0]) as (stream, size):
            if size > self.quota_bytes:
                raise ValueError('单文件超过访客配额')
            if name == 'wc':
                # 分块计数，无需把整个文件或超长单词放入内存。
                lines = words = 0
                whitespace = True
                deadline = time.monotonic() + self.timeout
                while chunk := stream.read(65536):
                    if time.monotonic() >= deadline:
                        raise ValueError('命令超时')
                    lines += chunk.count(b'\n')
                    words += len(chunk.split()) - int(not whitespace and not chunk[:1].isspace())
                    whitespace = chunk[-1:].isspace()
                return self._output(f'{lines} {words} {size} {args[0]}\n'.encode())
            data = stream.read(self.output_limit + 1)
            if name == 'head':
                data = b''.join(data.splitlines(keepends=True)[:count])
            return self._output(data)

    def _validate_tree(self, tree: Path) -> None:
        """限额按逻辑字节计（含稀疏文件），只接受能安全持久化的普通目录和文件。"""
        def fail(exc):
            raise exc

        used = entries = 0
        for parent, dirs, files, fd in os.fwalk(tree, onerror=fail, follow_symlinks=False):
            entries += len(dirs) + len(files)
            if entries > 10000 or len(Path(parent).relative_to(tree).parts) > 64:
                raise ValueError('访客目录条目或深度超过限额')
            for name in dirs + files:
                try:
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue  # 运行时采样允许临时文件消失；退出后另有静态校验。
                if info.st_mode & (stat.S_ISUID | stat.S_ISGID) and not stat.S_ISDIR(info.st_mode):
                    raise ValueError('访客工作区不保留特权文件')
                if stat.S_ISDIR(info.st_mode):
                    continue
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError('访客命令不可保留链接或特殊文件')
                used += info.st_size
                if used > self.quota_bytes:
                    raise ValueError('访客目录超过配额')

    async def _process(self, argv, *, work: Path | None = None) -> str:
        # 明确持有管道读端：真机命令可能另建会话并持有写端，超时仍要能关闭和返回。
        loop = asyncio.get_running_loop()
        rfd, wfd = os.pipe()
        with os.fdopen(rfd, 'rb', buffering=0) as pipe, os.fdopen(wfd, 'wb', buffering=0) as writer:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=work, env={} if work else None, stdin=subprocess.DEVNULL,
                stdout=writer, stderr=subprocess.STDOUT, start_new_session=True,
            )
            stream = asyncio.StreamReader()
            try:
                transport, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(stream), pipe)
                writer.close()
                try:
                    return await self._collect(proc, stream, work)
                finally:
                    transport.close()
            finally:
                if proc.returncode is None:
                    self._kill_group(proc.pid)
                    await proc.wait()

    @staticmethod
    def _kill_group(pid):
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    async def _collect(self, proc, stream, work):
        output = bytearray()

        async def collect():
            while chunk := await stream.read(8192):
                output.extend(chunk[:max(0, self.output_limit + 1 - len(output))])
            await proc.wait()

        reader = asyncio.create_task(collect())
        try:
            async with asyncio.timeout(self.timeout):
                if work:
                    while not reader.done():
                        done, _ = await asyncio.wait({reader}, timeout=.05)
                        if not done:
                            await self._io(self._validate_tree, work)
                await asyncio.shield(reader)
        except TimeoutError:
            raise RuntimeError('命令超时') from None
        finally:
            # 父进程可能已退出，而孙进程还占着输出管道；不能只检查 returncode。
            if not reader.done():
                self._kill_group(proc.pid)
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
        result = self._output(output)
        if proc.returncode:
            raise ValueError(f'命令执行失败（退出码 {proc.returncode}）：{result}')
        return result

    async def _isolated(self, binary, command):
        # 使用同文件系统的私有副本，命令失败、超限或被取消时不污染原工作区。
        with tempfile.TemporaryDirectory(dir=self.root.parent, prefix='.command-') as tmp:
            work = Path(tmp) / 'work'
            await self._io(self._validate_tree, self.root)
            await self._io(shutil.copytree, self.root, work)
            env_args = [arg for key, value in self.env.items() if key not in {'PATH', 'HOME'}
                        for arg in ('--setenv', key, value)]
            argv = [binary, *_RUNTIME, *env_args, '--bind', str(work), '/sandbox', '--chdir', '/sandbox',
                    '--remount-ro', '/', '--', '/usr/bin/prlimit', f'--fsize={self.quota_bytes}',
                    '--core=0', '--', '/bin/sh', '-c', command]
            result = await self._process(argv, work=work)
            await self._io(self._validate_tree, work)
            backup = Path(tmp) / 'previous'
            self.root.rename(backup)
            try:
                work.rename(self.root)
            except BaseException:
                backup.rename(self.root)
                raise
            return result

    async def run_command(self, command: str) -> str:
        if not command.strip():
            raise ValueError('命令不能为空')
        if not self.is_guest:
            return await self._process(['/bin/sh', '-c', command])
        async with self.lock:
            binary = await self._io(self.bwrap_path)
            if binary:
                return await self._isolated(binary, command)
            return await self._io(self._fallback, command)
