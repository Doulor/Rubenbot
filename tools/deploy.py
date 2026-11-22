"""从干净 main 发布到固定站点；不导入或运行 bot 代码。"""

import argparse
import hashlib
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SSH_CONFIG = '/home/hatch/.ssh/config'
HOST = 'homecloud'
OPTIONS = ['-F', SSH_CONFIG, '-oBatchMode=yes', '-oConnectTimeout=8']
PUBLIC = 'https://ruben.doulor.cn/'
APP = 'rubenbot/web/static/app.js'
HELPER = Path(__file__).with_name('_deploy_remote.py')


class Failure(Exception):
    pass


def report(status, message):
    print(f'{status} {message}', flush=True)


def run(args, *, timeout=30, input=None):
    try:
        return subprocess.run(args, input=input, capture_output=True, timeout=timeout,
                              check=True).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        # stderr 可能来自远端配置或代理，不把其原文写入部署日志。
        raise Failure(f'{args[0]} 失败（{type(exc).__name__}，状态 {getattr(exc, "returncode", "未知")}）') from None


def git(*args):
    return run(['git', '--no-optional-locks', *args]).decode().strip()


def anchor():
    if git('branch', '--show-current') != 'main':
        raise Failure('仅允许从 main 部署')
    if git('status', '--porcelain', '--untracked-files=all'):
        raise Failure('工作树不干净，请先提交')
    return git('rev-parse', '--verify', 'refs/heads/main^{commit}')


def parse(argv):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--dry-run', action='store_true', help='只读取本地 Git 并打印计划')
    parser.add_argument('--skip-tests', action='store_true', help='跳过 pytest 和 ruff')
    parser.add_argument('--sync-persona', action='store_true', help='同时覆盖归档中的 data/persona 文件')
    return parser.parse_args(argv)


def dry_plan(commit, args):
    steps = [
        '跳过 pytest 与 ruff' if args.skip_tests else
        'python -m pytest --timeout=300 --timeout-method=thread -x；python -m ruff check .',
        f'ssh -F {SSH_CONFIG} {HOST}：检查 ~/rubenbot-test/start.sh，加部署锁，ps 核对旧 PID、读取上次提交标记',
        f'git archive --format=tar.gz {commit}；scp -F {SSH_CONFIG} 归档 {HOST}:rubenbot-main.tar.gz',
        '在 ~/rubenbot-test/ 执行 tar -xzf，排除 data/、config.yaml、bot.log、start.sh' +
        ('；另解包 data/persona' if args.sync_persona else '；不同步人设'),
        'ps 按账号、cwd 和 argv 查 bot，向精确 PID 发 TERM；轮询 ss 确认 8765 释放（30 秒）',
        'setsid（start_new_session）运行 ./start.sh：stdin=/dev/null，stdout/stderr=新 bot.log，关闭其他 fd',
        '核对新 PID、curl http://127.0.0.1:8765/ 为 200、新日志 WS 已连接且零 ERROR、磁盘 app.js md5',
        f'curl {PUBLIC} 须为 200，curl {PUBLIC}static/app.js 须为 200 且 md5 与 git show 一致',
        f'失败：重新 git archive {commit}，重复上传、解包、停启及全部验活（最多回滚一次）',
        '记录健康提交；输出部署前后 PID/commit、失败时输出日志计数；释放部署锁',
    ]
    for step in steps:
        report('PLAN', step)


def remote(action, *args, timeout=30):
    # 只传固定动作、内部提交号与 PID，不传用户输入，不读取远端 start.sh 内容。
    try:
        return run(['ssh', *OPTIONS, HOST, 'python3', '-', action, *map(str, args)],
                   input=HELPER.read_bytes(), timeout=timeout).decode().strip()
    except Failure as exc:
        raise Failure(f'远端步骤 {action}：{exc}') from None


def public_checks(md5, temp):
    for url, output in [(PUBLIC, os.devnull), (PUBLIC + 'static/app.js', str(temp / 'app.js'))]:
        code = run(['curl', '-sS', '--max-time', '10', '-o', output, '-w', '%{http_code}', url])
        if code != b'200':
            raise Failure(f'公网 {url} 未返回 200')
    if hashlib.md5((temp / 'app.js').read_bytes()).hexdigest() != md5:
        raise Failure('公网 app.js md5 不符')
    report('OK', '公网首页 200，公网 app.js 200 且 md5 与 main 一致')


def health(old, md5, temp):
    deadline = time.monotonic() + 60
    while True:
        state = remote('probe', old)
        if state.startswith('OK '):
            _, new, actual = state.split()
            if actual != md5:
                raise Failure('远端磁盘 app.js md5 不符')
            break
        if not state.endswith(' ERROR=0') or time.monotonic() >= deadline:
            raise Failure(f'本机验活未通过：{state}')
        time.sleep(1)
    public_checks(md5, temp)
    if remote('probe', old) != state:
        raise Failure('公网检查期间 PID、日志或本机服务状态改变')
    report('OK', f'新 PID={new}，WebUI 200，SnowLuma 已连接，新日志零 ERROR，磁盘 md5={md5}')
    return new


def install(commit, old, persona, temp):
    archive = temp / 'release.tar.gz'
    run(['git', 'archive', '--format=tar.gz', '-o', archive, commit], timeout=120)
    md5 = hashlib.md5(run(['git', 'show', f'{commit}:{APP}'])).hexdigest()
    report('OK', f'已归档 {commit}')
    run(['scp', *OPTIONS, archive, HOST + ':rubenbot-main.tar.gz'], timeout=120)
    report('OK', '已上传 ~/rubenbot-main.tar.gz')
    remote('unpack', *(['persona'] if persona else []), timeout=120)
    report('OK', '归档已解包；' + ('已同步人设' if persona else '未同步人设'))
    remote('stop', old, timeout=45)
    report('OK', f'旧 PID={old} 已停止，8765 已释放')
    remote('start')
    report('OK', 'start.sh 已脱离 SSH，开始独立连接验活')
    new = health(old, md5, temp)
    remote('stamp', commit)
    return new


def deploy(args):
    commit = anchor()
    report('OK', f'main 干净，部署前 main / 回滚锚点={commit}')
    if args.dry_run:
        dry_plan(commit, args)
        return 0
    if args.skip_tests:
        report('SKIP', '本地测试与 lint')
    else:
        for cmd in [['pytest', '--timeout=300', '--timeout-method=thread', '-x'], ['ruff', 'check', '.']]:
            if subprocess.run([sys.executable, '-m', *cmd], stderr=subprocess.STDOUT).returncode:
                raise Failure(f'{cmd[0]} 未通过，不连接远端')
            report('OK', f'{cmd[0]} 通过')
    if anchor() != commit:
        raise Failure('预检期间 main 已变化，拒绝部署')
    remote('lock')
    try:
        old, before = remote('status').split()
        report('OK', f'部署前 PID={old} commit={before}')
        with tempfile.TemporaryDirectory(prefix='rubenbot-release-') as directory:
            for rollback in (False, True):
                try:
                    previous = remote('status').split()[0] if rollback else old
                    new = install(commit, previous, args.sync_persona, Path(directory))
                except (Failure, OSError) as exc:
                    report('FAIL', f'{"回滚" if rollback else "部署"}：{exc}')
                    if rollback:
                        try:
                            state = remote('diagnose')
                        except Failure as diag:
                            state = f'当前 PID、日志状态不可读：{diag}'
                        report('FAIL', f'部署前 PID={old} commit={before}；部署后 commit=未验证；{state}')
                        return 2
                    report('RETRY', f'仅一次，按固定锚点重新打包 {commit}')
                else:
                    report('OK', f'{"回滚" if rollback else "部署"}验活通过：PID {old} → {new}；commit {before} → {commit}')
                    return 1 if rollback else 0
    finally:
        remote('unlock')
        report('OK', '部署锁已释放')


if __name__ == '__main__':
    try:
        sys.exit(deploy(parse(sys.argv[1:])))
    except (Failure, OSError, ValueError) as exc:
        report('FAIL', str(exc))
        sys.exit(1)
