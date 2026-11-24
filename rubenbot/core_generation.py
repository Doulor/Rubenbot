"""生成与分段、工具清单及本轮工具调用。"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

from . import computer
from .adapters.base import MessageEvent
from .core_shared import (
    _log_identity,
    _parse_tool_arguments,
    chat_tools,
    clean_reply_patterns,
    logger,
    strip_transcript_prefix,
)
from .llm import ToolUnsupported, normalize_dsml_message
from .memory.extract import audience_for_chat
from .memory.store import make_person_id
from .reminder import describe_fire_at, resolve_fire_at
from .reply_segments import split_segments
from .verification import NO_EVIDENCE, WebReply, verify

# 查询类电脑工具：只读结果本身就是答复；其余是产生副作用或交付物的动作。
_QUERY_TOOLS = frozenset({"read_file", "list_dir", "fetch_url", "run_command"})
# 联网搜索工具的定义。只在 search 回退链上至少一个渠道配了 key 时随 recall
# 一起开放；一个都没有时模型完全看不见它（不会调用后才报错）。
WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "遇到拿不准的公共事实，搜索并只读打开结果页查证；知道的直接答。"
            "只摘当前提问里适合公开检索的关键词，以空格隔开，不带群名、账号或私密信息。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索词：几个具体的关键词",
                },
                "count": {
                    "type": "integer",
                    "description": "要几条结果，默认 5，最多 8",
                },
            },
            "required": ["query"],
        },
    },
}


# 对外开放的工具名（工具循环里按名字分派）
_TOOL_NAMES = frozenset({"recall", "set_reminder", "adapter_actions", "adapter_call"})


class GenerationMixin:
    """生成与分段、工具清单及本轮工具调用。"""

    async def recall(
        self,
        person_query: str = "",
        topic: str = "",
        time_range: str = "",
        *,
        event: MessageEvent | None = None,
        now: float | None = None,
    ) -> str:
        """T2：按人、话题或时段查全域时间线。隐私开启时才校验当前受众。"""
        now = self._now() if now is None else now
        if event is not None:
            audience_now = audience_for_chat(event.chat_type, event.chat_id)
            speaker = make_person_id(event.platform, event.sender_id)
        else:
            audience_now, speaker = "unknown", ""
        try:
            result = await self.memory_reader.recall(
                person_query,
                topic,
                time_range,
                audience_now=audience_now,
                owner_person_ids={speaker} if event and computer.is_host(self.config.computer, event) else (),
                target_platform=event.platform if event else "",
                target_chat_id=event.chat_id if event else "",
                speaker=speaker,
                now=now,
            )
            # 空结果或分寸审查扣下也是一次完成的本地查询，不代表记得某件事。
            self._recall_checked = True
            return result
        except Exception as exc:  # noqa: BLE001 - 回忆失败只当没查到
            self._recall_checked = False
            logger.warning("recall 失败：%s", exc)
            return "回忆失败，暂时想不起来。"

    async def _verify_call(self, function: dict[str, Any], event: MessageEvent) -> WebReply:
        """查证调用直接结束工具循环；包括配置变更、参数错误、无证据三种失败。"""
        try:
            args = _parse_tool_arguments(function.get("arguments")) or {}
            query = args.get("query")
            if self.searcher is not None and isinstance(query, str) and not args.keys() - {"query", "count"}:
                return await verify(self.searcher, self.llm, question=event.text, query=query,
                                    count=args.get("count"), chat_key=f"{event.platform}:{event.chat_id}")
        except Exception as exc:
            # 已选查证便不再走 generate 的自由生成兜底；日志不带查询、正文或凭据。
            logger.warning("本轮查证未完成：%s", type(exc).__name__)
        return WebReply(NO_EVIDENCE)

    def tool_definitions(self, event: MessageEvent | None = None) -> list[dict[str, Any]]:
        """当前开放的 LLM 工具：recall / set_reminder 看配置（见 chat_tools），
        搜索配了 key 才给；电脑工具按发送者划分真机或访客沙盒，
        访客不展示可绕过沙盒的外部工具。无入站事件时不授予电脑能力。
        """
        tools = chat_tools(self.config)
        if self.searcher is not None:
            tools.append(WEB_SEARCH_TOOL)
        tools.extend(computer.definitions(self.config.computer, event))
        if computer.guest_session(self.config.computer, event):
            return tools
        tools.extend(self.mcp.tools())
        if event is not None:
            tools.extend(
                self.gateway.tools(
                    self.adapters.get(event.platform),
                    chat_id=event.chat_id,
                    chat_type=event.chat_type,
                    sender_id=event.sender_id,
                )
            )
        return tools

    async def generate(
        self,
        messages: list[dict[str, str]],
        *,
        event: MessageEvent | None = None,
        task: str = "chat",
        computer_task: bool = False,
        deliver_file: bool = False,
    ) -> str:
        """生成回复；模型支持工具调用时先走有限轮次的工具循环。

        工具不可用、模型不用工具、或任何一步出问题，都退化为普通单次生成。
        ``task`` 只用于用量打标：单段直出是 ``chat``，两段式的第二段是
        ``gen_stage2``。
        """
        if event is not None and self.tool_definitions(event):
            try:
                reply = await self._generate_with_tools(messages, event, task=task, operation=computer_task,
                                                        deliver_file=deliver_file)
            except ToolUnsupported as exc:
                logger.debug("当前模型不支持工具调用，退化为单次生成：%s", exc)
                reply = None
            except Exception as exc:  # noqa: BLE001 - 工具循环失败不能拖死回复
                logger.warning("工具循环失败，退化为单次生成：%s", exc)
                if computer_task and event is not None:
                    # 异常逃出循环：无法确认做到哪步，只说明这次没办成。
                    reply = await self._task_outcome(
                        event, task=task, done=(), failed_tool="", error="执行中途出了岔子",
                        file_sent=False, partial=False,
                    )
                else:
                    reply = None
            if reply is not None:
                return reply if isinstance(reply, (WebReply, computer.TaskOutcome)) else reply.strip()
        if computer_task:
            # 工具不可用或没有真实调用，不能换一次无工具生成来冒充操作回执；
            # 但仍欠对方一句说明，不能静默当办成了。
            if event is not None:
                return await self._task_outcome(
                    event, task=task, done=(), failed_tool="", error="这边工具用不了",
                    file_sent=False, partial=False,
                )
            return computer.TaskOutcome(
                computer.one_sentence(computer.fallback_sentence(
                    failed_tool="", error="这边工具用不了",
                )),
                reason="computer_task_failed",
            )
        try:
            reply = await self.llm.complete(messages, role="chat", task=task)
        except Exception as exc:  # noqa: BLE001 - LLM 失败不能带死循环
            logger.warning("生成回复失败：%s", exc)
            return ""
        return (reply or "").strip()

    def _generation_tools(self, event: MessageEvent, operation: bool) -> list[dict[str, Any]]:
        cfg = self.config.computer
        if operation:
            return computer.definitions(cfg, event)
        tools = self.tool_definitions(event)
        if event.is_group and computer.is_host(cfg, event):
            # 普通群聊上下文含其他人的原话，只有隔离后的执行轮可以使用真机。
            return [t for t in tools if t['function']['name'] not in computer.COMPUTER_TOOLS]
        return tools

    async def _generate_with_tools(
        self,
        messages: list[dict[str, str]],
        event: MessageEvent,
        *,
        task: str = "chat",
        operation: bool = False,
        deliver_file: bool = False,
    ) -> str | None:
        """有界工具循环；操作委托只接受真实执行结果，不走自由生成兜底。

        操作委托的结束形态只有两种：交付物送达或没有动作时静默返回空串，
        失败 / 部分完成时返回一句说明（``computer.TaskOutcome``）。模型在
        动作之后自行写下的总结、确认、邀约一律丢弃。
        """
        if not getattr(self.llm, "supports_tools", True):
            raise ToolUnsupported("当前模型已确认不支持工具调用")
        chat_with_tools = getattr(self.llm, "chat_with_tools", None)
        if not callable(chat_with_tools):
            raise ToolUnsupported("当前 LLM 客户端不支持工具调用")
        max_rounds = max(0, int(self.config.memory.recall_tool_max_rounds))
        if max_rounds == 0:
            raise ToolUnsupported("工具循环已关闭")
        file_sent = False
        done: list[str] = []
        host = computer.is_host(self.config.computer, event)
        if operation and host:
            max_rounds = self.config.computer.max_task_rounds
        working: list[dict[str, Any]] = list(messages)

        def silent(reason: str = "computer_task", *, completed: bool = True) -> computer.TaskOutcome:
            # 交付完成即静默：不补总结、不确认、不邀约。
            return computer.TaskOutcome("", reason=reason, delivered=file_sent, completed=completed)

        def missing_deliverable() -> computer.TaskOutcome:
            # 说明生成失败时给兜底句，绝不把未完成说成已交付；调用点负责 await。
            return self._task_outcome(
                event, task=task, done=tuple(done), failed_tool="send_file",
                error="", file_sent=file_sent,
                partial=bool(done or file_sent), state="文件还没发出去",
            )

        for index in range(max_rounds):
            # 上一轮等待模型或执行工具期间 MCP 可能断线或重连，不能沿用旧清单。
            tools = self._generation_tools(event, operation)
            if not tools:
                if operation:
                    return silent("computer_task_failed", completed=False)
                return await self.llm.complete(working, role="chat", task=task)
            required = operation and (index == 0 or (deliver_file and not file_sent))
            message = await self._call_with_tools(working, tools, role="chat", task=task,
                                                  tool_choice='required' if required else 'auto')
            tool_calls = message.get("tool_calls") or []
            if operation:
                flight = self._flights.get((event.platform, event.chat_type, event.chat_id))
                if flight and flight.updates:
                    return silent("computer_task_failed", completed=False)  # 等待模型时机主补充了要求；执行前重裁，执行后不重放。
                if not self.config.computer.enabled or computer.is_host(self.config.computer, event) != host:
                    return silent("computer_task_failed", completed=False)
                for call in tool_calls:
                    fn = call['function']
                    if fn['name'] not in computer.COMPUTER_TOOLS:
                        computer.audit_denial(self.config.computer, self.store, event, fn['name'],
                                              _parse_tool_arguments(fn.get('arguments')))
                        return silent("computer_task_failed", completed=False)
                if required and not tool_calls:
                    # 有前置成果却始终没交出附件：算部分完成；一步都没做就保持沉默。
                    return (await missing_deliverable()) if deliver_file and done else silent("computer_task_failed")
                if tool_calls and flight:
                    flight.collecting = False  # 从实际分派起封口，新消息另起一轮。
            if not tool_calls:
                content = (message.get("content") or "").strip()
                if operation:
                    if file_sent:
                        return silent()  # 文件已送达：模型再写什么总结都不发。
                    if deliver_file:
                        return await missing_deliverable()
                    if content and done and all(name in _QUERY_TOOLS for name in done):
                        # 纯查询类委托：结果本身由模型作答，没有额外汇报。
                        # 夹带过写文件 / 发文件等副作用的不算，成功即静默。
                        return content
                    # 只做了写文件之类的动作：成功即静默，不补确认。
                    return silent()
                if content:
                    # 模型没要工具、直接给了回复：这就是一次正常生成，不必再花一次请求
                    return content
                return None
            if not operation and event.is_group and computer.is_host(self.config.computer, event):
                for call in tool_calls:
                    fn = call.get('function', {})
                    if fn.get('name') in computer.COMPUTER_TOOLS:
                        args = _parse_tool_arguments(fn.get('arguments'))
                        computer.audit_denial(self.config.computer, self.store, event, fn['name'], args)
                        if flight := self._flights.get((event.platform, event.chat_type, event.chat_id)):
                            flight.computer_failed = True
                        return ''  # 模型自造的真机调用也不能在普通群聊轮执行。
            # 一旦模型选择查证，就只执行该次只读调用；同批即使夹带别的工具也不分派。
            for call in tool_calls:
                function = call.get("function") if isinstance(call, dict) else None
                if isinstance(function, dict) and function.get("name") == "web_search":
                    return await self._verify_call(function, event)
            working.append(
                {
                    "role": "assistant",
                    "content": message.get("content") or "",
                    "tool_calls": tool_calls,
                }
            )
            for call in tool_calls:
                if not host and computer.is_host(self.config.computer, event):
                    # 等待同批上个动作时权限也可能热更新，绝不能把访客任务提升到真机。
                    fn = call['function']
                    computer.audit_denial(self.config.computer, self.store, event, fn['name'],
                                          _parse_tool_arguments(fn.get('arguments')))
                    return silent("computer_task_failed", completed=False)
                result = await self._tool_result_message(call, event)
                success = result.pop('computer_success', None)
                if success is False:
                    # 失败已审计；不让自由生成掩盖拒绝，也不执行同批余下动作。
                    if not operation:
                        return ''
                    # 操作委托失败只发一句人话说明哪步没成、现在是什么状态。
                    return await self._task_outcome(
                        event, task=task, done=tuple(done), failed_tool=call['function']['name'],
                        error=str(result.get('content') or ''), file_sent=file_sent,
                        partial=bool(done or file_sent),
                    )
                if success:
                    done.append(call['function']['name'])
                if success and call['function']['name'] == 'send_file':
                    file_sent = True
                    # 交付物也是 bot 自己的动作：静默发送没有正文，不记一条的话
                    # 事后被问「你刚刚给我发过什么」就只剩别人的近况可复述。
                    sent_path = (_parse_tool_arguments(call['function'].get('arguments')) or {}).get('path') or ''
                    self._record_self_action(event, f"发了文件 {Path(sent_path).name or '（未命名）'}")
                working.append(result)
        # 已耗尽预算，不再分派；成功即静默，不追加完成总结。
        if operation:
            if deliver_file and not file_sent:
                return await missing_deliverable()
            return silent()
        tools = self._generation_tools(event, operation)
        if not tools:
            return await self.llm.complete(working, role="chat", task=task)
        message = await self._call_with_tools(
            working, tools, role="chat", task=task, tool_choice="none"
        )
        return message.get("content") or ""

    async def _task_outcome(
        self,
        event: MessageEvent,
        *,
        task: str,
        done: tuple[str, ...],
        failed_tool: str,
        error: str,
        file_sent: bool,
        partial: bool,
        state: str = "",
    ) -> computer.TaskOutcome:
        """失败 / 部分完成时让模型用自己的口吻说一句；模型不可用就给兜底句。

        只给执行记录和本轮原话，不把群聊历史、记忆或工具输出正文交给这次生成。
        """
        record = computer.outcome_record(done=done, failed_tool=failed_tool, error=error,
                                         file_sent=file_sent, state=state)
        messages = [
            {"role": "system", "content": self.persona_system_prompt(event, stage="generator")
             + "\n\n" + computer.OUTCOME_GUIDANCE},
            {"role": "user", "content": f"执行记录：{record}\n本轮原话：{event.text}"},
        ]
        text = ""
        try:
            kwargs: dict[str, Any] = {"role": "chat", "task": "gen_outcome"}
            if self._accepts_kwarg(self.llm.complete, "max_tokens"):
                kwargs["max_tokens"] = 120
            text = await self.llm.complete(messages, **kwargs)
        except Exception as exc:  # noqa: BLE001 - 说明拿不到时用兜底句
            logger.warning("电脑任务说明生成失败：%s", exc)
        sentence = computer.one_sentence(text)
        if not sentence:
            sentence = computer.fallback_sentence(
                failed_tool=failed_tool, error=error, done=done, file_sent=file_sent,
                state=state or None,
            )
        reason = "computer_task_partial" if partial else "computer_task_failed"
        return computer.TaskOutcome(sentence, reason=reason)

    @staticmethod
    def _accepts_kwarg(callable_: Any, name: str) -> bool:
        """可调用对象是否接受 ``name`` 这个关键字参数（有 ``**kwargs`` 也算）。"""
        try:
            parameters = inspect.signature(callable_).parameters
        except (TypeError, ValueError):  # 内建/无法取签名：按支持处理，交给调用方兜底
            return True
        return name in parameters or any(
            item.kind is inspect.Parameter.VAR_KEYWORD for item in parameters.values()
        )

    async def _call_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        role: str,
        task: str,
        tool_choice: str = "auto",
    ) -> dict[str, Any]:
        """调用客户端的工具接口；只给不认 ``task`` 参数的实现传它认识的参数。"""
        kwargs: dict[str, Any] = {"role": role, "tool_choice": tool_choice}
        if self._accepts_kwarg(self.llm.chat_with_tools, "task"):
            kwargs["task"] = task
        message = await self.llm.chat_with_tools(messages, tools, **kwargs)
        # LLMClient 已在客户端层转好；这里再兜一次，保证测试替身/其它实现
        # 吐出的 DSML 文本调用同样走结构化执行通道。
        return normalize_dsml_message(message)

    async def _tool_result_message(self, call: dict[str, Any], event: MessageEvent) -> dict[str, Any]:
        """分派内置和 MCP 工具，把结果或错误按调用 ID 回给模型。"""
        call_id = str(call.get("id") or "")
        function = call.get("function") or {}
        name = str(function.get("name") or "")
        flight = self._flights.get((event.platform, event.chat_type, event.chat_id))
        if flight and name != "recall":
            # 提醒、外部工具或平台动作可能已有副作用，重裁不能再执行一次。
            flight.used_action = True
        cfg = self.config.computer
        if name in computer.COMPUTER_TOOLS:
            success, content = await computer.call(
                cfg, self.store, Path(self.config.database_path).parent / "sandbox", event,
                self.adapters.get(event.platform), name, _parse_tool_arguments(function.get("arguments")),
            )
            if flight and not success:
                flight.computer_failed = True
            return {"role": "tool", "tool_call_id": call_id, "content": content, "computer_success": success}
        if (name.startswith("mcp__") or name in ("adapter_call", "adapter_actions")) and computer.guest_session(cfg, event):
            computer.audit_denial(cfg, self.store, event, name, _parse_tool_arguments(function.get('arguments')))
            if flight:
                flight.computer_failed = True
            return {"role": "tool", "tool_call_id": call_id, "content": "访客沙盒不提供该接口。", "computer_success": False}
        if name.startswith("mcp__"):
            # manager 只按当前注册表查找，校验参数、执行、审计，断线也只回工具错误。
            content = await self.mcp.call(name, _parse_tool_arguments(function.get("arguments")))
            return {"role": "tool", "tool_call_id": call_id, "content": content}
        if name not in _TOOL_NAMES:
            return {
                "role": "tool",
                "tool_call_id": call_id,
                "content": f"没有名为 {name or '（空）'} 的工具。",
            }
        args = _parse_tool_arguments(function.get("arguments")) or {}
        if name in ("adapter_actions", "adapter_call"):
            content = await self._gateway_tool(name, args, event)
            return {"role": "tool", "tool_call_id": call_id, "content": content}
        if name == "set_reminder":
            try:
                content = await self.set_reminder(args, event=event)
            except Exception as exc:  # noqa: BLE001 - 工具失败只把原因告诉模型
                logger.warning("set_reminder 执行失败：%s", exc)
                content = f"设置提醒失败：{exc}"
            return {"role": "tool", "tool_call_id": call_id, "content": content}
        if not self.config.memory.recall_tool_enabled:
            return {"role": "tool", "tool_call_id": call_id, "content": "回忆工具未开启。"}
        try:
            content = await self.recall(
                str(args.get("person_query") or ""),
                str(args.get("topic") or ""),
                str(args.get("time_range") or ""),
                event=event,
            )
        except Exception as exc:  # noqa: BLE001 - 工具失败只把原因告诉模型
            logger.warning("recall 工具执行失败：%s", exc)
            content = f"回忆失败：{exc}"
        return {"role": "tool", "tool_call_id": call_id, "content": content}

    async def _gateway_tool(self, name: str, args: dict[str, Any], event: MessageEvent) -> str:
        """网关工具分派：目录查询（渐进披露）与执行；失败只回文本。"""
        adapter = self.adapters.get(event.platform)
        if name == "adapter_actions":
            if adapter is None:
                return "当前没有可用的适配器。"
            if not self.gateway.available(adapter, chat_id=event.chat_id, chat_type=event.chat_type, sender_id=event.sender_id):
                return "当前会话不可查询网关目录。"
            action = str(args.get("action") or "").strip()
            if action:
                return self.gateway.describe_action(adapter, action)
            keyword = str(args.get("keyword") or "").strip()
            return self.gateway.list_actions(adapter, keyword=keyword)
        action = str(args.get("action") or "").strip()
        params = args.get("params")
        if not isinstance(params, dict):
            params = {}
        return await self.gateway.call(
            adapter,
            action,
            params,
            chat_id=event.chat_id,
            chat_type=event.chat_type,
            source="model",
            requester=event.sender_id,
        )

    async def set_reminder(
        self,
        args: dict[str, Any],
        *,
        event: MessageEvent | None = None,
        now: float | None = None,
    ) -> str:
        """set_reminder 工具实现：校验参数、检查每会话上限、落库。

        返回给模型看的说明文字（成功或拒绝原因）；不抛异常给工具循环。
        """
        now = self._now() if now is None else now
        cfg = self.config.reminder
        if not cfg.enabled:
            return "提醒功能当前未开启。"
        if event is None:
            return "当前没有可用的会话，无法设置提醒。"
        text = str(args.get("text") or "").strip()
        if not text:
            return "提醒内容不能为空，请用 text 参数写清楚要提醒什么。"
        text = text[: max(1, int(cfg.max_text_chars))]
        fire_at, error = resolve_fire_at(
            args, now, max_delay_minutes=max(1, int(cfg.max_delay_minutes))
        )
        if error is not None:
            return error
        pending = self.store.count_pending_jobs(
            kind="reminder", platform=event.platform, chat_id=event.chat_id
        )
        if pending >= cfg.max_pending_per_chat:
            return (
                f"这个会话已经有 {pending} 条提醒在等，达到上限 {cfg.max_pending_per_chat} 条，"
                "这次先记不了了；可以等前面的提醒到点后再设。"
            )
        job = self.store.add_scheduled_job(
            kind="reminder",
            fire_at=float(fire_at),
            platform=event.platform,
            chat_id=event.chat_id,
            chat_type=event.chat_type,
            payload={
                "text": text,
                # 台账：谁提的、提醒谁。到点渲染只认下面两个结构化 id，
                # 不拿姓名推断「谁让提醒谁」（见 reminder.parties_from_payload）。
                "requester_name": event.sender_name,
                "requester_id": make_person_id(event.platform, event.sender_id),
                # 一期工具只支持「提醒我」：接收者就是发话的这个人。到点是一条
                # 发回本会话的消息，群聊里旁人也能看见，但话是说给这个人听的。
                "recipient_id": make_person_id(event.platform, event.sender_id),
            },
            created_by=make_person_id(event.platform, event.sender_id),
            now=now,
        )
        logger.info(
            "设置提醒 #%s：%s/%s %s（%s）", job.id, event.platform, event.chat_id, text, describe_fire_at(fire_at, now),
            extra=_log_identity(event),
        )
        return f"已设好提醒：{describe_fire_at(fire_at, now)}提醒「{text}」。"

    def split_reply(self, reply: str) -> list[str]:
        """把一条回复拆成要发送的若干段。

        完整换行段优先，连接标点收尾的行接上下文；具体句界与保护规则见分段器。
        """
        reply = clean_reply_patterns(strip_transcript_prefix(reply.strip()))
        if not reply:
            return []
        return split_segments(reply, max_segments=self.config.reply.max_segments)
