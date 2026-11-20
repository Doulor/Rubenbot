"""文件层直接针对临时工作区，验证不同调用实例之间也共享约束。"""

import asyncio
import os

import pytest

from rubenbot.computer_files import ComputerFiles


@pytest.fixture
def files(tmp_path):
    return ComputerFiles(root=tmp_path / 'sandbox', quota_bytes=128, output_limit=32)


@pytest.mark.parametrize('path', ['../neighbor', 'dir/../data', '/absolute', 'sym', 'symdir/private'])
@pytest.mark.parametrize('operation', ['read_file', 'file_bytes', 'write_file', 'list_dir'])
def test_file_path_boundary(files, tmp_path, operation, path):
    outside = tmp_path / 'private'
    outside.write_bytes(b'out')
    (files.root / 'sym').symlink_to(outside)
    (files.root / 'symdir').symlink_to(tmp_path, target_is_directory=True)
    args = (path, 'changed') if operation == 'write_file' else (path,)
    with pytest.raises((ValueError, OSError)):
        asyncio.run(getattr(files, operation)(*args))
    assert outside.read_bytes() == b'out'


def test_quota_rejection_has_no_file_side_effects(files):
    async def run():
        await files.write_file('dir/a', 'a' * 64)
        await files.write_file('b', 'b' * 64)
        for path, size in [('x/y/z', 1), ('dir/a', 65), ('too-big', 129)]:
            with pytest.raises(ValueError, match='配额'):
                await files.write_file(path, 'x' * size)
        assert not (files.root / 'x').exists() and not (files.root / 'too-big').exists()
        assert await files.file_bytes('dir/a') == b'a' * 64
        await files.write_file('dir/a', '')
        await files.write_file('x/y/z', 'works')
        assert await files.list_dir('x/y') == 'z\n'
    asyncio.run(run())


def test_parallel_writers_share_workspace_budget(files):
    other = ComputerFiles(root=files.root, quota_bytes=128, output_limit=32)

    async def run():
        return await asyncio.gather(files.write_file('one', 'a' * 100), other.write_file('two', 'b' * 100),
                                    return_exceptions=True)

    assert sum(isinstance(r, ValueError) for r in asyncio.run(run())) == 1
    assert sum(f.stat().st_size for f in files.root.iterdir()) == 100


def test_reject_hardlinks_and_special_files_without_blocking(files, tmp_path):
    source = tmp_path / 'host-file'
    source.write_text('original')
    os.link(source, files.root / 'hardlink')
    os.mkfifo(files.root / 'pipe')
    for name in ['hardlink', 'pipe']:
        for operation, args in [(files.file_bytes, (name,)), (files.write_file, (name, 'new'))]:
            with pytest.raises((ValueError, OSError)):
                asyncio.run(operation(*args))
    assert source.read_text() == 'original'


def test_host_resolves_normal_links_and_handles_large_output(tmp_path):
    host = ComputerFiles(root=None, quota_bytes=1, output_limit=32)
    target = tmp_path / 'target'
    target.write_bytes(b'file')
    link = tmp_path / 'link'
    link.symlink_to(target)
    asyncio.run(host.write_file(str(link), '任意' * 200))
    assert link.is_symlink() and asyncio.run(host.file_bytes(str(link))) == target.read_bytes()
    text = asyncio.run(host.read_file(str(link)))
    assert text.endswith('[输出已截断]') and len(text.encode()) <= 32
    target.write_bytes(bytes([255]) * 20)
    assert len(asyncio.run(host.read_file(str(target))).encode()) <= 32
