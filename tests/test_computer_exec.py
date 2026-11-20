"""电脑执行层的隔离、路径和限额测试。"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from rubenbot.computer_exec import ComputerContext


def run(awaitable):
    return asyncio.run(awaitable)


@pytest.fixture
def sandbox(tmp_path: Path) -> ComputerContext:
    return ComputerContext(root=tmp_path / "guest", quota_bytes=128, timeout=1, output_limit=32)


def test_host_has_full_shell_access_and_limits(tmp_path: Path) -> None:
    ctx = ComputerContext(root=None, quota_bytes=128, timeout=1, output_limit=32)
    result = run(ctx.run_command("printf host-output"))
    assert result == "host-output"
    text = run(ctx.run_command("printf '%0100d' 1"))
    assert text.endswith('[输出已截断]') and len(text.encode()) <= 32
    with pytest.raises(ValueError, match="退出码"):
        run(ctx.run_command("printf failure >&2; exit 7"))


def test_guest_path_safety_and_symlinks(sandbox: ComputerContext) -> None:
    run(sandbox.write_file("inside.txt", "inside"))
    with pytest.raises(ValueError):
        run(sandbox.read_file("../inside.txt"))
    with pytest.raises(ValueError):
        run(sandbox.read_file("/etc/passwd"))
    outside = sandbox.root.parent / "outside.txt"
    outside.write_text("secret")
    link = sandbox.root / "link.txt"
    link.symlink_to(outside)
    with pytest.raises((ValueError, OSError)):
        run(sandbox.read_file("link.txt"))
    with pytest.raises((ValueError, OSError)):
        run(sandbox.write_file("link.txt", "overwrite"))


def test_guest_fallback_is_python_data_processing_only(
    sandbox: ComputerContext, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ComputerContext, "bwrap_path", staticmethod(lambda: None))
    run(sandbox.write_file("data.txt", "b\na\na\n"))
    assert run(sandbox.run_command("head data.txt")) == "b\na\na\n"
    with pytest.raises(ValueError, match="bubblewrap|回退"):
        run(sandbox.run_command("sh -c 'cat data.txt'"))
    with pytest.raises(ValueError, match="shell"):
        run(sandbox.run_command("cat data.txt | sort"))
    with pytest.raises(ValueError):
        run(sandbox.run_command("cat /etc/passwd"))


def test_bwrap_is_used_when_available(sandbox: ComputerContext, monkeypatch: pytest.MonkeyPatch) -> None:
    if not ComputerContext.bwrap_path():
        pytest.skip("测试环境没有可用 bubblewrap")
    monkeypatch.setenv('HOST_SECRET_FOR_TEST', 'must-not-leak')
    run(sandbox.write_file('report', 'original'))
    assert run(sandbox.run_command('test -z "$HOST_SECRET_FOR_TEST" && test "$HOME" = "$PWD" '
                                   '&& test ! -e /etc && test ! -e /proc && test ! -e /usr/local '
                                   '&& printf done > report && cat report')) == 'done'
    assert run(sandbox.read_file('report')) == 'done'
    assert run(sandbox.run_command("python3 -I -c 'import socket; print([n for _, n in socket.if_nameindex()])'")) == "['lo']\n"
    # 单文件未达限额、两者相加超额：即使 shell 不等轮询就退出也不提交。
    with pytest.raises(ValueError, match='配额'):
        run(sandbox.run_command('truncate -s 90 a; truncate -s 90 b'))
    assert not (sandbox.root / 'a').exists()
    with pytest.raises(ValueError):
        run(sandbox.run_command('truncate -s 256 a'))
    assert not (sandbox.root / 'a').exists()
    with pytest.raises(ValueError, match='配额'):
        run(sandbox.run_command('truncate -s 90 a; truncate -s 90 b; sleep 3'))
    with pytest.raises(ValueError, match='链接'):
        run(sandbox.run_command('ln -s /etc link'))
    assert not (sandbox.root / 'link').is_symlink()
    with pytest.raises(RuntimeError, match='超时'):
        run(sandbox.run_command('printf staged > report; sleep 3'))
    assert run(sandbox.file_bytes('report')) == b'done'


def test_guest_env_is_clean_and_quota_checked(sandbox: ComputerContext, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ComputerContext, "bwrap_path", staticmethod(lambda: None))
    monkeypatch.setenv("SECRET_HOST_VALUE", "must-not-leak")
    run(sandbox.write_file("data.txt", "ok"))
    assert run(sandbox.read_file("data.txt")) == "ok"
    assert "SECRET_HOST_VALUE" not in run(sandbox.run_command("cat data.txt"))
    with pytest.raises((ValueError, RuntimeError), match="配额"):
        run(sandbox.write_file("too-large.txt", "x" * 129))
    run(sandbox.write_file("one.txt", "x" * 100))
    with pytest.raises((ValueError, RuntimeError), match="配额"):
        run(sandbox.write_file("two.txt", "y" * 29))


def test_detects_present_but_unusable_bwrap(sandbox, tmp_path, monkeypatch):
    # 代表安装了 bwrap、但系统策略不准创建命名空间的机器。
    binary = tmp_path / 'bwrap'
    binary.write_text('#!/bin/sh\nexit 1\n')
    binary.chmod(0o700)
    monkeypatch.setenv('PATH', str(tmp_path))
    assert sandbox.bwrap_path() is None
    run(sandbox.write_file('file', 'ok'))
    assert run(sandbox.run_command('cat file')) == 'ok'
    with pytest.raises(ValueError, match='仅支持'):
        run(sandbox.run_command('sh -c true'))


def test_timeout_kills_host_process_group() -> None:
    ctx = ComputerContext(root=None, quota_bytes=128, timeout=0.05, output_limit=32)
    with pytest.raises(RuntimeError, match="超时"):
        run(ctx.run_command("sleep 10"))


def test_special_files_rejected(sandbox: ComputerContext) -> None:
    fifo = sandbox.root / "pipe"
    os.mkfifo(fifo)
    with pytest.raises((ValueError, OSError)):
        run(sandbox.file_bytes("pipe"))
