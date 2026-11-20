"""实测访客和机主的电脑权限；证据只取自本轮真实分派、审计及临时产物。"""

from __future__ import annotations

import re
from pathlib import Path
from secrets import token_hex
from unittest.mock import patch

from rubenbot.core import _parse_tool_arguments


_GUEST = 'guest'
_OWNER = 'console:me'
# 不列设备、挂载路径、进程或环境。即使工具异常，也不把任意输出交给中转。
_METRICS = ("/usr/bin/stat -f -c 'disk_available_blocks=%a' / && "
            "/usr/bin/awk '/^MemAvailable:/ {print \"memory_available_KiB=\" $2}' /proc/meminfo")
_NUMBERS = re.compile(r'disk_available_blocks=([0-9]+)\nmemory_available_KiB=([0-9]+)\n?')
_DENIED = '操作失败：访客只允许工作区内的相对路径'


class _Failure(AssertionError):
    """只携带本模块固定的安全失败原因。"""


async def _turn(ctx, *, sender, chat, prompt, tool, accepts, verifies, success=True):
    """只观察这一次真实调用。分派前筛输入；结果和审计不过关时绝不回传结果。"""
    dispatch = ctx.bot._tool_result_message
    scope = 'host' if sender == _OWNER else 'sandbox'
    who = f'console:{sender}'
    seen = False
    failure = None

    async def guarded(call, event):
        nonlocal seen, failure
        try:
            if failure or seen:
                raise _Failure('本轮出现额外工具调用，已阻止执行')
            if (event.platform, event.sender_id, event.chat_id, event.chat_type) != (
                'console', sender, chat, 'private'
            ):
                raise _Failure('分派事件不是指定的 Console 私聊身份')
            cfg = ctx.bot.config.computer
            if not cfg.enabled or (who in cfg.whitelist) != (sender == _OWNER):
                raise _Failure('电脑开关或身份白名单与本轮权限不符')
            fn = call['function']
            args = _parse_tool_arguments(fn.get('arguments'))
            if fn.get('name') != tool or args is None or not accepts(args):
                raise _Failure('模型请求了本轮白名单外的工具或参数，未执行')
            previous = ctx.store.recent_computer_actions(limit=1)
            last_id = previous[0]['id'] if previous else 0
            result = await dispatch(call, event)
            rows = ctx.store.recent_computer_actions(limit=2)
            new = [r for r in rows if r['id'] > last_id]
            if len(new) != 1 or tuple(new[0][k] for k in ('who', 'context', 'tool', 'success')) != (
                who, scope, tool, success
            ):
                raise _Failure('本次真实操作的新增审计缺失，或账号、上下文、工具、成败不符')
            if not verifies(result['content']):
                raise _Failure('真实工具输出不符合预期安全格式，未回传内容')
            seen = True
            return result
        except _Failure as exc:
            failure = failure or exc
        except Exception:
            failure = _Failure('工具参数、分派或审计读取异常，未回传内容')
        # 产品会捕获分派异常并降级生成，所以还要在 send 返回后检查失败标记。
        raise failure from None

    with patch.object(ctx.bot, '_tool_result_message', guarded):
        try:
            reply = await ctx.send(prompt, chat=chat, sender=sender)
        except Exception:
            raise failure or _Failure('测试会话未能完成本轮消息收发') from None
    if failure:
        raise failure
    if not seen:
        raise _Failure(f'本轮未观察到真实的 {tool} 调用；口头答复不算证据')
    if not reply and tool != 'send_file':
        raise _Failure('工具调用已结束，但本轮没有出站文本')


async def sandbox(ctx) -> str:
    """同一访客完成列求和，再分别验证两个越界路径确实被产品拒绝。"""
    try:
        root = Path(ctx.root).resolve(strict=True)
        config = root / 'config.yaml'
        if not config.is_file() or config.is_symlink():
            raise _Failure('临时数据目录下缺少独立的测试配置文件')
        if Path(ctx.bot.config.database_path).resolve().parent != root:
            raise _Failure('测试库不在指定临时目录，不能确定访客产物和配置的位置')
        workspace = root / 'sandbox' / 'console' / _GUEST
        if workspace.resolve() != workspace:
            raise _Failure('临时访客工作区路径中有链接，已停止')
        name = f'sum-{token_hex(4)}.csv'
        output = workspace / name
    except _Failure:
        raise
    except Exception:
        raise _Failure('无法确认临时工作区或配置的安全边界') from None

    chat = 'e2e-computer-guest'
    progress = []
    failed = False
    snapshot = None
    try:
        if output.exists() or output.is_symlink():
            raise _Failure('求和产物在本轮开始前已存在')
        await _turn(
            ctx, sender=_GUEST, chat=chat, tool='write_file',
            prompt=(f'请对下面 CSV 按列求和。只调用一次 write_file，path 用 {name}，content 用两行 CSV：'
                    '首行保留原表头，次行是对应的两个整数和。不要用其他工具。\n'
                    'red,blue\n12,4\n5,18\n7,11'),
            accepts=lambda a: set(a) == {'path', 'content'} and a['path'] == name
            and isinstance(a['content'], str) and re.fullmatch(r'red,blue\r?\n[0-9]{1,8},[0-9]{1,8}\r?\n?', a['content']),
            verifies=lambda s: re.fullmatch(r'已写入 [0-9]+ 字节。', s),
        )
        if not output.is_file() or output.resolve() != output or output.stat().st_size > 64:
            raise _Failure('访客目录没有本轮新生成的普通小文件')
        data = output.read_bytes()
        if data.splitlines() != [b'red,blue', b'24,33']:
            raise _Failure('生成的 CSV 列和不符，预期 24、33')
        snapshot = data
        progress.append('CSV 已落盘，列和 24、33，写入审计成功')
    except Exception as exc:
        failed = True
        reason = str(exc) if isinstance(exc, _Failure) else '无法安全验证新产物'
        progress.append(f'CSV 失败：{reason}')

    if snapshot is None:
        progress.append('附件跳过：没有已验证的 CSV')
    else:
        try:
            start = len(ctx.console.sent)
            await _turn(
                ctx, sender=_GUEST, chat=chat, tool='send_file',
                prompt=(f'请只调用一次 send_file，把刚才的 {name} 作为附件发回当前会话。'
                        'path 使用该文件名，不要指定收件人，不要另行读取或生成。'),
                accepts=lambda a: a == {'path': name},
                verifies=lambda s: s == f'文件已发回当前会话（{len(snapshot)} 字节）。',
            )
            attachments = [(target, kind, seg.data.get('name'), seg.data.get('content'))
                           for target, kind, segments in ctx.console.sent[start:]
                           for seg in segments if seg.type == 'file']
            if attachments != [(chat, 'private', name, snapshot)]:
                raise _Failure('本轮新增文件段的数量、会话、文件名或字节与产物不一致')
            progress.append('原私聊收到同名、逐字节一致的附件，独立 send_file 审计成功')
        except Exception as exc:
            failed = True
            reason = str(exc) if isinstance(exc, _Failure) else '无法核对本轮文件段'
            progress.append(f'附件失败：{reason}')

    # 两次拒读互不依赖，也不依赖 CSV；一项失败不能掩盖另一项的结果。
    for path, label in [('/etc/passwd', '系统账号文件'), ('../../../config.yaml', '临时项目配置')]:
        try:
            await _turn(
                ctx, sender=_GUEST, chat=chat, tool='read_file', success=False,
                prompt=(f'请实际用 read_file 测试此路径的访问权限，path 原样填 {path}。'
                        '只调用一次，只报告是否被拒绝，不展示正文，不要改用其他工具。'),
                accepts=lambda a: a == {'path': path}, verifies=lambda s: s == _DENIED,
            )
            progress.append(f'{label}已被路径边界拒绝，sandbox 审计一致')
        except Exception as exc:
            failed = True
            reason = str(exc) if isinstance(exc, _Failure) else '无法核实分派结果'
            progress.append(f'{label}拒读验证失败：{reason}')
    evidence = '；'.join(progress)
    if failed:
        raise _Failure(evidence)
    return evidence


async def host(ctx) -> str:
    """只放行一个固定的只读指标查询，并验证两项数值和本次机主审计。"""
    await _turn(
        ctx, sender=_OWNER, chat='e2e-computer-owner', tool='run_command',
        prompt=('请查根文件系统可用块数和可用内存 KiB。只调用一次 run_command，command 原样使用下面整行，'
                '不要添加、改写或拆分命令；成功后简短确认。\n' + _METRICS),
        accepts=lambda a: a == {'command': _METRICS}, verifies=_NUMBERS.fullmatch,
    )
    return '机主实际执行固定只读查询，获得磁盘可用块数和内存可用 KiB 两个数值；本次 run_command 成功审计标为 host。'
