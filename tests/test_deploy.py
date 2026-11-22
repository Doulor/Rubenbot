"""部署验证只用临时 main、合成日志和本地替身，绝不连接部署目标。"""

import hashlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tools import _deploy_remote as host
from tools import deploy

ROOT = Path(__file__).resolve().parents[1]


def test_arguments():
    assert not any(vars(deploy.parse([])).values())
    assert all(vars(deploy.parse(['--skip-tests', '--sync-persona', '--dry-run'])).values())
    with pytest.raises(SystemExit):
        deploy.parse(['--sync'])


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for cmd in [['init', '-q', '-b', 'main'], ['config', 'user.name', 'test'],
                ['config', 'user.email', 'test@example.invalid']]:
        deploy.git(*cmd)
    Path('tools').mkdir()
    for name in ('deploy.sh', 'deploy.py', '_deploy_remote.py'):
        shutil.copy2(ROOT / 'tools' / name, Path('tools') / name)
    js = Path(deploy.APP)
    js.parent.mkdir(parents=True)
    js.write_bytes(b'fixture')
    deploy.git('add', '.')
    deploy.git('commit', '-qm', 'initial')
    return tmp_path


def test_dry_run_clean_main_no_writes_or_network(repo, tmp_path_factory, monkeypatch):
    def snapshot():
        return {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in repo.rglob('*') if p.is_file()}

    # 任何尝试联网的命令都使测试失败；也不允许刷新 git 索引或生成字节码。
    bin_dir = tmp_path_factory.mktemp('bin')
    for name in ('ssh', 'scp', 'curl', 'pytest', 'ruff'):
        exe = bin_dir / name
        exe.write_text('#!/bin/sh\nexit 91\n')
        exe.chmod(0o755)
    monkeypatch.setenv('PATH', str(bin_dir) + ':' + os.environ['PATH'])
    commit = deploy.anchor()
    before = snapshot()
    out = subprocess.check_output(['tools/deploy.sh', '--dry-run'], text=True)
    assert snapshot() == before
    for expected in (commit, '--timeout=300', 'scp', '8765', 'setsid', '零 ERROR', '回滚', '不同步人设'):
        assert expected in out
    assert '上次线上提交' in out and 'fallback' in out
    Path('untracked').touch()
    with pytest.raises(deploy.Failure, match='先提交'):
        deploy.anchor()
    deploy.git('switch', '-qc', 'feature')
    with pytest.raises(deploy.Failure, match='main'):
        deploy.anchor()


@pytest.mark.parametrize(('argv', 'matches'), [
    (['/usr/bin/python3.12', '-u', '-m', 'rubenbot', '-c', 'config.yaml'], True),
    (['.venv/bin/python', '.venv/bin/rubenbot', '-c', 'config.yaml'], True),
    (['.venv/bin/rubenbot'], True), (['python', '-m', 'rubenbot_fake'], False),
    (['sh', '-c', 'python -m rubenbot'], False), (['python', 'task.py', '-m', 'rubenbot'], False),
])
def test_exact_command(argv, matches):
    assert host.bot_command(argv) is matches


def test_stop_only_matching_process(tmp_path, monkeypatch):
    site = tmp_path / 'site'
    site.mkdir()
    monkeypatch.setattr(host, 'SITE', site)
    monkeypatch.setattr(host, 'listening', lambda: False)
    children = []
    try:
        for directory in (site, tmp_path):
            (directory / 'rubenbot.py').write_text('import time; time.sleep(20)')
            children.append(subprocess.Popen([sys.executable, '-m', 'rubenbot'], cwd=directory))
        assert host.pid() == children[0].pid
        host.stop(children[0].pid)
        assert children[0].wait(timeout=3) < 0 and children[1].poll() is None
    finally:
        for child in children:
            child.terminate()
            child.wait(timeout=3)


def test_busy_port_times_out(monkeypatch):
    monkeypatch.setattr(host, 'pid', lambda: 0)
    monkeypatch.setattr(host, 'listening', lambda: True)
    times = iter((0, 31))
    monkeypatch.setattr(time, 'monotonic', lambda: next(times))
    with pytest.raises(RuntimeError, match='8765'):
        host.stop(0)


def test_detach_and_log_boundary(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    host.LOG.write_text('ERROR 旧日志\nSnowLuma WS 已连接：旧\n')
    script = Path('start.sh')
    script.write_text('#!/bin/sh\nprintf "新日志\\n"\n')
    script.chmod(0o755)
    real = subprocess.Popen
    children = []

    def observe(argv, **kwargs):
        assert kwargs['start_new_session'] and kwargs['close_fds']
        assert kwargs['stdin'] == subprocess.DEVNULL
        assert kwargs['stdout'] is kwargs['stderr']
        children.append(real(argv, **kwargs))
        return children[-1]

    monkeypatch.setattr(host.subprocess, 'Popen', observe)
    host.start()
    assert children[0].wait(timeout=3) == 0
    assert host.LOG.read_text() == '新日志\n'
    assert next(tmp_path.glob('bot.log.before-*')).read_text().startswith('ERROR')


@pytest.mark.parametrize(('current', 'code', 'log', 'ok'), [
    (12, b'200', 'SnowLuma WS 已连接：x', True),
    (0, b'200', 'SnowLuma WS 已连接：x', False), (11, b'200', 'SnowLuma WS 已连接：x', False),
    (12, b'302', 'SnowLuma WS 已连接：x', False), (12, b'200', '', False),
    (12, b'200', 'SnowLuma WS 已连接：x\n ERROR detail', False),
    (12, b'200', 'SnowLuma WS 已连接：x\nSnowLuma WS 断开：x', False),
])
def test_remote_health(repo, monkeypatch, current, code, log, ok):
    monkeypatch.setattr(host, 'pid', lambda: current)
    monkeypatch.setattr(host.subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess(a, 0, code))
    host.LOG.write_text(log)
    assert host.probe(11).startswith('OK ') is ok


@pytest.mark.parametrize(('home', 'asset', 'body', 'ok'), [
    (200, 200, b'fixture', True), (301, 200, b'fixture', False),
    (200, 404, b'fixture', False), (200, 200, b'stale', False),
])
def test_public_health(tmp_path, monkeypatch, home, asset, body, ok):
    def curl(args):
        if args[-1] == deploy.PUBLIC:
            return str(home).encode()
        assert args[-1] == deploy.PUBLIC + 'static/app.js'
        Path(args[args.index('-o') + 1]).write_bytes(body)
        return str(asset).encode()

    monkeypatch.setattr(deploy, 'run', curl)
    md5 = hashlib.md5(b'fixture').hexdigest()
    if ok:
        deploy.public_checks(md5, tmp_path)
    else:
        with pytest.raises(deploy.Failure):
            deploy.public_checks(md5, tmp_path)


@pytest.mark.parametrize('sync', [False, True])
def test_unpack_preserves_site_files(tmp_path, monkeypatch, sync):
    src, site = (tmp_path / name for name in ('src', 'site'))
    for directory, value in [(src, 'git'), (site, 'live')]:
        (directory / 'data/persona').mkdir(parents=True)
        for path in ['data/private', 'data/persona/identity.md', 'config.yaml', 'bot.log', 'start.sh', 'code']:
            (directory / path).write_text(value)
    archive = tmp_path / 'test.tar.gz'
    subprocess.run(['tar', '-czf', archive, '-C', src, 'data', 'config.yaml', 'bot.log', 'start.sh', 'code'], check=True)
    monkeypatch.chdir(site)
    monkeypatch.setattr(host, 'ARCHIVE', archive)
    host.unpack(sync)
    assert Path('code').read_text() == 'git'
    assert Path('data/persona/identity.md').read_text() == ('git' if sync else 'live')
    assert all(Path(p).read_text() == 'live' for p in ['data/private', 'config.yaml', 'bot.log', 'start.sh'])


@pytest.mark.parametrize('stamp', [None, 'a' * 40, 'b' * 64, 'a' * 41])
def test_status_only_returns_full_hash(tmp_path, monkeypatch, capsys, stamp):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(host, 'SITE', tmp_path)
    monkeypatch.setattr(host, 'pid', lambda: 10)
    if stamp is not None:
        host.STAMP.write_text(stamp + '\n')
    host.main('status')
    expected = stamp if stamp and len(stamp) in (40, 64) else 'unknown'
    assert capsys.readouterr().out.strip() == f'10 {expected}'


@pytest.mark.parametrize(('stamp', 'failures', 'rc'), [
    ('previous', 0, 0), ('previous', 1, 1), ('previous', 2, 2),
    ('unknown', 1, 1), ('same', 1, 1), ('missing-object', 1, 2),
])
def test_release_and_pinned_rollback(repo, monkeypatch, stamp, failures, rc, capsys):
    previous = deploy.anchor()
    Path(deploy.APP).write_bytes(b'release')
    deploy.git('commit', '-qam', 'release')
    commit = deploy.anchor()
    before = {'previous': previous, 'unknown': 'unknown', 'same': commit, 'missing-object': 'a' * 40}[stamp]
    target = commit if before == 'unknown' else before
    expected = target if failures else commit
    calls = []
    archives = []
    real_run = deploy.run
    md5 = hashlib.md5(b'fixture' if expected == previous else b'release').hexdigest()

    def run(args, **kwargs):
        if args[:2] == ['git', 'archive']:
            archives.append(args[-1])
        if args[0] == 'scp':
            assert args[1:3] == ['-F', deploy.SSH_CONFIG] and args[-1] == 'homecloud:rubenbot-main.tar.gz'
            assert Path(args[-2]).stat().st_size > 0
            return b''
        assert args[0] == 'git'
        return real_run(args, **kwargs)

    def remote(action, *args, **kwargs):
        calls.append(action)
        if action == 'status':
            return f'{10 + calls.count("start")} {before}'
        if action == 'stop':
            assert int(args[0]) == 10 + calls.count('start')
        if action == 'probe':
            if calls.count('start') <= failures:
                # 模拟部署中分支向前移动，回滚必须复用进场时的哈希。
                if deploy.git('rev-parse', 'HEAD') == commit:
                    Path(deploy.APP).write_text('later')
                    deploy.git('commit', '-qam', 'advanced')
                return 'WAIT pid=0 http=000 ws=0 ERROR=1'
            return f'OK {10 + calls.count("start")} {md5}'
        if action == 'stamp':
            assert args == (expected,)
        if action == 'diagnose':
            return 'ERROR=1 当前PID=0'
        return ''

    monkeypatch.setattr(deploy, 'run', run)
    monkeypatch.setattr(deploy, 'remote', remote)
    monkeypatch.setattr(deploy, 'public_checks', lambda *a: calls.append('public'))
    assert deploy.deploy(deploy.parse(['--skip-tests'])) == rc
    assert calls[0] == 'lock' and calls[-1] == 'unlock'
    assert archives == ([commit, target] if failures else [commit])
    assert calls.count('start') == len(archives) - (stamp == 'missing-object')
    assert calls.count('public') == calls.count('stamp') == int(rc != 2)
    out = capsys.readouterr().out
    assert ('fallback' in out) == (stamp in ('unknown', 'same'))
    if rc == 2:
        assert 'ERROR=1 当前PID=0' in out
        if stamp == 'missing-object':
            assert f'归档 {target} 失败' in out
    else:
        assert f'commit {before} → {expected}' in out
