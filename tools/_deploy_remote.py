"""由 deploy.py 经 SSH 标准输入执行，只在家里云使用标准库。"""

import hashlib
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

SITE = Path.home() / 'rubenbot-test'
ARCHIVE = Path.home() / 'rubenbot-main.tar.gz'
APP = Path('rubenbot/web/static/app.js')
LOG = Path('bot.log')
STAMP = Path('.deploy-commit')
LOCK = Path.home() / '.rubenbot-deploy.lock'


def bot_command(argv):
    if not argv:
        return False
    command = Path(argv[0]).name
    if command == 'rubenbot':
        return True
    if not re.fullmatch(r'python(?:3(?:\.\d+)?)?', command):
        return False
    args = argv[1:]
    while args and args[0] in ('-u', '-B', '-E', '-s'):
        args = args[1:]
    return args[:2] == ['-m', 'rubenbot'] or bool(args and Path(args[0]).name == 'rubenbot')


def pid():
    rows = subprocess.check_output(['ps', '-ww', '-u', str(os.getuid()), '-o', 'pid=,args='])
    matches = []
    for line in rows.splitlines():
        if b'rubenbot' not in line:
            continue
        n = int(line.split(None, 1)[0])
        proc = Path(f'/proc/{n}')
        try:
            if (proc / 'cwd').resolve(strict=True) != SITE.resolve():
                continue
            argv = (proc / 'cmdline').read_bytes().rstrip(b'\0').split(b'\0')
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if bot_command([os.fsdecode(a) for a in argv]):
            matches.append(n)
    if len(matches) > 1:
        raise RuntimeError('多个匹配的 bot PID，拒绝选择')
    return matches[0] if matches else 0


def listening():
    return bool(subprocess.check_output(['ss', '-Hltn', 'sport = :8765']).strip())


def unpack(persona):
    args = ['tar', '-xzf', str(ARCHIVE)]
    subprocess.run(args + ['--exclude=data', '--exclude=config.yaml', '--exclude=bot.log',
                           '--exclude=start.sh'], check=True)
    if persona:
        subprocess.run(args + ['data/persona'], check=True)


def stop(old):
    if old:
        # 持有 pidfd 并再次核实身份，避免 PID 重用时向其他进程发信号。
        fd = os.pidfd_open(old)
        try:
            if pid() != old:
                raise RuntimeError('旧 PID 已变化，拒绝发信号')
            signal.pidfd_send_signal(fd, signal.SIGTERM)
        finally:
            os.close(fd)
    deadline = time.monotonic() + 30
    while pid() or listening():
        if time.monotonic() >= deadline:
            raise RuntimeError('旧进程或 8765 端口未释放')
        time.sleep(0.5)


def start():
    # 以新日志作为边界，兼容 start.sh 自己用 > 或 >> 打开 bot.log。
    if LOG.exists():
        LOG.rename(f'bot.log.before-{time.time_ns()}')
    with LOG.open('ab') as log:
        subprocess.Popen(['./start.sh'], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         start_new_session=True, close_fds=True)


def log_state(path):
    ws = False
    errors = 0
    if path.exists():
        with path.open('rb') as lines:
            for line in lines:
                if 'SnowLuma WS 已连接：'.encode() in line:
                    ws = True
                elif 'SnowLuma WS 断开：'.encode() in line:
                    ws = False
                errors += bool(re.search(rb'\bERROR\b', line))
    return ws, errors


def probe(old):
    current = pid()
    ws, errors = log_state(LOG)
    result = subprocess.run(['curl', '-s', '--max-time', '3', '-o', '/dev/null', '-w',
                             '%{http_code}', 'http://127.0.0.1:8765/'], capture_output=True)
    code = result.stdout.decode() if result.returncode == 0 else '000'
    if errors or not (current and current != old and ws and code == '200'):
        return f'WAIT pid={current} http={code} ws={int(ws)} ERROR={errors}'
    return f'OK {current} {hashlib.md5(APP.read_bytes()).hexdigest()}'


def main(action, *args):
    os.chdir(SITE)
    if action == 'lock':
        if not os.access('start.sh', os.X_OK):
            raise RuntimeError('start.sh 不可执行')
        LOCK.mkdir(mode=0o700)
    elif action == 'unlock':
        LOCK.rmdir()
    elif action == 'status':
        previous = STAMP.read_text().strip() if STAMP.is_file() else ''
        print(pid(), previous if re.fullmatch(r'[0-9a-f]{40}(?:[0-9a-f]{24})?', previous) else 'unknown')
    elif action == 'unpack':
        unpack(args == ('persona',))
    elif action == 'stop':
        stop(int(args[0]))
    elif action == 'start':
        start()
    elif action == 'probe':
        print(probe(int(args[0])))
    elif action == 'stamp':
        STAMP.write_text(args[0] + '\n')
    elif action == 'diagnose':
        ws, errors = log_state(LOG)
        print(f'日志存在={LOG.exists()} ws={int(ws)} ERROR={errors} 当前PID={pid()}')
    else:
        raise ValueError('未知远端步骤')


if __name__ == '__main__':
    main(*sys.argv[1:])
