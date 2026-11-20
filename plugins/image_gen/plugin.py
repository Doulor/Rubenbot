"""画图插件：群聊/私聊里「画一张 <描述>」调用生图接口，把图发回去。

一期只接硅基流动（siliconflow，``POST /v1/images/generations``），需要自备
key；没配 key 时插件照常加载但**不注册触发**，只打一行日志（与联网搜索批次
同一惯例）。

流程：识别触发词 → 本地黑名单 → 单人冷却 → 每日全群上限 → 调接口
→ 下载图片字节落到插件 data 目录（接口返回的临时 URL 不长期引用）
→ 以图片段发送。描述直接用用户原话，不让模型改写提示词。

内容边界：依赖服务商自身审核；provider 返回审核失败时如实回「这个画不了」。
所有失败都降级成一句话，不吞请求、不抛异常打断对话。
"""

from __future__ import annotations

import base64
import logging
import math
import os
import re
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable, Mapping

import httpx
import yaml

from rubenbot.adapters.base import Segment

logger = logging.getLogger(__name__)

# 硅基流动生图接口
SILICONFLOW_URL = "https://api.siliconflow.cn/v1/images/generations"
DEFAULT_PROVIDER = "siliconflow"
DEFAULT_MODEL = "Kwai-Kolors/Kolors"
SUPPORTED_PROVIDERS = ("siliconflow",)

API_TIMEOUT = 60.0
# 图片下载超时：临时 URL 一般很快就给
DOWNLOAD_TIMEOUT = 30.0
# 单张图大小上限：协议端一般不收过大图片（QQ 官方上限 5M）
MAX_IMAGE_BYTES = 5 * 1024 * 1024

DEFAULT_TRIGGERS = ("画一张", "画个", "画一幅", "帮我画", "生成图片")
DEFAULT_COOLDOWN_SECONDS = 120.0
DEFAULT_DAILY_LIMIT = 20

# 配置里的 ${ENV_VAR}：展开时未定义的变量替换成空串
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# 触发词后面常见的量词/虚词，剥掉之后再判断「有没有描述」
_TRIM_CHARS = " \t\r\n：:，,、。.!！?？~～…—-"
_FILLER_PREFIXES = ("一张", "一幅", "一个", "一张图", "一幅画")

# 本地黑名单：只收最明显越界的一小撮，主要口径仍交给 provider 审核；
# 部署方按群规在 config.yaml 里增删。空列表 = 只靠 provider 审核。
DEFAULT_BLACKLIST: tuple[str, ...] = (
    "儿童色情",
    "幼女",
    "恋童",
    "血腥",
    "分尸",
    "虐杀",
)


@dataclass(frozen=True)
class ImageGenConfig:
    """归一化后的插件配置；类型不对的项退回默认值，不让配置错误炸掉插件。"""

    provider: str = DEFAULT_PROVIDER
    api_key: str = ""
    model: str = DEFAULT_MODEL
    triggers: tuple[str, ...] = DEFAULT_TRIGGERS
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS
    daily_limit: int = DEFAULT_DAILY_LIMIT
    blacklist: tuple[str, ...] = DEFAULT_BLACKLIST

    @property
    def configured(self) -> bool:
        """配了 key 才注册触发；空 key / 没展开的 ${ENV} 都算没配。"""
        return bool(self.api_key.strip())

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "ImageGenConfig":
        raw = data if isinstance(data, Mapping) else {}
        provider = str(raw.get("provider") or DEFAULT_PROVIDER).strip().lower()
        if provider not in SUPPORTED_PROVIDERS:
            logger.warning(
                "image_gen 的生图服务商 %s 暂不支持，改用 %s", provider, DEFAULT_PROVIDER
            )
            provider = DEFAULT_PROVIDER
        triggers = _as_list(raw.get("triggers"))
        blacklist = _as_list(raw.get("blacklist"))
        return cls(
            provider=provider,
            api_key=_expand_env(str(raw.get("api_key") or "")).strip(),
            model=str(raw.get("model") or DEFAULT_MODEL).strip() or DEFAULT_MODEL,
            triggers=triggers if triggers is not None else DEFAULT_TRIGGERS,
            cooldown_seconds=_as_non_negative(
                raw.get("cooldown_seconds"), DEFAULT_COOLDOWN_SECONDS
            ),
            daily_limit=_as_non_negative_int(raw.get("daily_limit"), DEFAULT_DAILY_LIMIT),
            blacklist=blacklist if blacklist is not None else DEFAULT_BLACKLIST,
        )


def read_config(directory: str | Path | None = None) -> ImageGenConfig:
    """读取插件目录下的 config.yaml（其次 config.example.yaml）；没有或损坏时用默认值。"""
    if directory is None:
        return ImageGenConfig()
    base = Path(directory)
    file = next(
        (item for item in (base / "config.yaml", base / "config.example.yaml") if item.exists()),
        None,
    )
    if file is None:
        return ImageGenConfig()
    try:
        data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 - 配置坏了也要能启动
        logger.warning("image_gen 读取配置 %s 失败，使用默认配置：%s", file, exc)
        return ImageGenConfig()
    return ImageGenConfig.from_mapping(data)


def load_config(ctx: Any = None) -> ImageGenConfig:
    """优先用 ``ctx.extras["image_gen_config"]`` 覆盖，其次读插件目录 config.yaml。"""
    extras = getattr(ctx, "extras", None) if ctx is not None else None
    if isinstance(extras, Mapping):
        override = extras.get("image_gen_config")
        if isinstance(override, Mapping):
            return ImageGenConfig.from_mapping(override)
    directory = getattr(getattr(ctx, "plugin", None), "path", None)
    return read_config(directory)


def data_dir_for(ctx: Any = None) -> Path:
    """图片落盘目录：插件目录下的 ``data/``（卸载时随目录一起删掉）。"""
    directory = getattr(getattr(ctx, "plugin", None), "path", None)
    return Path(directory) / "data" if directory is not None else _default_data_dir()


def _default_data_dir() -> Path:
    """没有宿主上下文时（单测/嵌入式）退回到代码文件旁边的 data/。"""
    return Path(__file__).resolve().parent / "data"


@dataclass(frozen=True)
class GenResult:
    """一次生图的结果：成功时带图片字节，失败时带给人看的原因。"""

    ok: bool
    image: bytes = b""
    suffix: str = ".png"
    error: str = ""


def parse_trigger(text: str, triggers: tuple[str, ...] | list[str]) -> str | None:
    """识别画图触发词，返回描述；未命中返回 None。

    命中但没带描述时返回空串（由调用方回一句提示）。触发词按长度从长到短
    匹配，避免短词抢先吃掉长词。
    """
    value = (text or "").strip()
    if not value:
        return None
    for trigger in sorted((item for item in triggers if item), key=len, reverse=True):
        if not value.startswith(trigger):
            continue
        prompt = value[len(trigger) :].strip().strip(_TRIM_CHARS).strip()
        # 「画个」+「一只猫」→「一只猫」；「画一张」+「一只猫」同理
        for filler in _FILLER_PREFIXES:
            if prompt.startswith(filler) and len(prompt) > len(filler):
                prompt = prompt[len(filler) :].strip()
                break
        return prompt
    return None


def parse_response(data: Any) -> tuple[str, str] | None:
    """从生图响应里取 ``(图片, 扩展名)``。

    图片是 URL 时原样返回（由调用方下载）；也兼容 ``b64_json`` 实现。
    没有可用图片返回 None。
    """
    if not isinstance(data, Mapping):
        return None
    images = data.get("images") or data.get("data")
    if not isinstance(images, list) or not images:
        return None
    first = images[0]
    if not isinstance(first, Mapping):
        return None
    url = str(first.get("url") or "").strip()
    if url:
        return url, ""
    encoded = str(first.get("b64_json") or "").strip()
    if encoded:
        return encoded, ".png"
    return None


def detect_suffix(content: bytes) -> str:
    """按文件头猜图片扩展名，认不出按 png 处理。"""
    if content.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if content.startswith(b"\x89PNG"):
        return ".png"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return ".webp"
    if content.startswith(b"GIF8"):
        return ".gif"
    return ".png"


def is_review_failure(status: int, body: Any) -> bool:
    """判断是不是服务商的内容审核拒绝（4xx 且错误码/文案像审核）。"""
    if not (400 <= status < 500):
        return False
    text = str(body or "").lower()
    return any(
        hint in text
        for hint in ("content", "sensitiv", "policy", "forbidden", "moderation", "risk", "审核", "违规")
    )


def _expand_env(value: str) -> str:
    """展开 ``${ENV_VAR}``；未定义时留空串（与原配置加载语义一致）。"""
    return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), value)


class ImageGenerator:
    """生图的识别、节流与执行；时钟与 HTTP 客户端可注入，便于测试。"""

    def __init__(
        self,
        config: ImageGenConfig | Mapping[str, Any] | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] | None = None,
        today: Callable[[], date] | None = None,
        data_dir: str | Path | None = None,
    ):
        if isinstance(config, ImageGenConfig):
            self.config = config
        else:
            self.config = ImageGenConfig.from_mapping(config)
        self._client = client
        self._owns_client = client is None
        self.clock = clock or time.monotonic
        self.today = today or date.today
        self.data_dir = Path(data_dir or _default_data_dir())
        # (平台, 生成者) -> 上次生成时间
        self._last_at: dict[tuple[str, str], float] = {}
        # (平台, 会话) -> (日期, 当天已生成次数)；每日上限是全群口径
        self._daily: dict[tuple[str, str], tuple[date, int]] = {}
        # 已落盘图片的进程内计数，避免同一秒内并发生成时文件名撞车
        self._saved_count = 0

    # ------------------------------------------------------------------
    # 钩子
    # ------------------------------------------------------------------

    async def on_message(self, runtime, event):
        """命中「画一张/画个」就处理并拦截消息，不让它再走模型回复。"""
        text = getattr(event, "text", "") or ""
        matched = parse_trigger(text, self.config.triggers) is not None
        try:
            return await self._handle(runtime, event)
        except Exception as exc:  # noqa: BLE001 - 插件异常不能拖垮主流程
            logger.exception("image_gen 处理画图异常：%s", exc)
            # 只有本插件认领的消息才回一句；无关消息的异常直接放行
            if matched:
                await self._reply(runtime, event, "画图出了点问题，稍后再试试")
                return False
            return None

    async def _handle(self, runtime, event):
        prompt = parse_trigger(getattr(event, "text", "") or "", self.config.triggers)
        if prompt is None:
            return None
        if not prompt:
            await self._reply(runtime, event, "想画什么？说「画一张 描述」就好")
            return False

        hit = self._blacklist_hit(prompt)
        if hit is not None:
            logger.info("image_gen 命中本地黑名单「%s」，拒绝请求", hit)
            await self._reply(runtime, event, "这个画不了，换个描述试试")
            return False

        key = (str(getattr(event, "platform", "") or ""), str(getattr(event, "sender_id", "")))
        now = self.clock()
        last = self._last_at.get(key)
        if last is not None:
            remaining = self.config.cooldown_seconds - (now - last)
            if remaining > 0:
                await self._reply(
                    runtime, event, f"刚画过啦，{math.ceil(remaining)} 秒后再画吧"
                )
                return False
        chat_key = (
            str(getattr(event, "platform", "") or ""),
            str(getattr(event, "chat_id", "") or ""),
        )
        used = self._daily_count(chat_key)
        if self.config.daily_limit and used >= self.config.daily_limit:
            await self._reply(runtime, event, "今天的画用完了，明天再来吧")
            return False
        # 先记冷却再请求：接口挂了也不让人在冷却期内反复重试打接口
        self._last_at[key] = now

        result = await self.generate(prompt)
        if not result.ok:
            # 失败不扣每日额度：服务商故障不该把当天的画额度耗光
            await self._reply(runtime, event, result.error)
            return False
        self._bump_daily(chat_key)
        await self._send_image(runtime, event, result)
        return False

    async def aclose(self) -> None:
        """插件停用时释放自己创建的 HTTP 连接池（注入的客户端留给调用方）。"""
        client = self._client
        if client is None or not self._owns_client:
            return
        self._client = None
        try:
            await client.aclose()
        except Exception as exc:  # noqa: BLE001 - 关闭失败不致命
            logger.debug("image_gen 关闭 HTTP 客户端失败：%s", exc)

    # ------------------------------------------------------------------
    # 节流
    # ------------------------------------------------------------------

    def _daily_count(self, chat_key: tuple[str, str]) -> int:
        today = self.today()
        stored = self._daily.get(chat_key)
        if stored is None or stored[0] != today:
            self._daily[chat_key] = (today, 0)
            return 0
        return stored[1]

    def _bump_daily(self, chat_key: tuple[str, str]) -> None:
        today = self.today()
        _, count = self._daily.get(chat_key, (today, 0))
        self._daily[chat_key] = (today, count + 1)

    def _blacklist_hit(self, prompt: str) -> str | None:
        lowered = prompt.lower()
        for word in self.config.blacklist:
            if word and word.lower() in lowered:
                return word
        return None

    # ------------------------------------------------------------------
    # 生成
    # ------------------------------------------------------------------

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=API_TIMEOUT)
            self._owns_client = True
        return self._client

    async def generate(self, prompt: str) -> GenResult:
        """调接口并下载图片；所有失败都收敛成 ``GenResult.error``。"""
        client = await self._get_client()
        payload = {
            "model": self.config.model,
            "prompt": prompt,
            "image_size": "1024x1024",
            "batch_size": 1,
            "num_inference_steps": 20,
            "guidance_scale": 7.5,
        }
        try:
            response = await client.post(
                SILICONFLOW_URL,
                headers={"Authorization": f"Bearer {self.config.api_key}"},
                json=payload,
                timeout=API_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            logger.warning("image_gen 请求 %s 失败：%s", self.config.provider, exc)
            return GenResult(False, error="生图服务连不上，稍后再试试")

        if response.status_code >= 400:
            body = _safe_body(response)
            logger.warning(
                "image_gen 接口返回 %s：%s", response.status_code, str(body)[:200]
            )
            if is_review_failure(response.status_code, body):
                return GenResult(False, error="这个画不了，换个描述试试")
            return GenResult(False, error="生图服务出错了，稍后再试试")

        parsed = parse_response(_safe_json(response))
        if parsed is None:
            logger.warning("image_gen 响应里没有图片：%s", _safe_text(response)[:200])
            return GenResult(False, error="这次没画出图，换个描述试试")

        source, suffix = parsed
        if source.startswith(("http://", "https://")):
            image = await self._download(source)
            if image is None:
                return GenResult(False, error="图画好了但取不回来，稍后再试试")
        else:
            try:
                image = base64.b64decode(source, validate=False)
                suffix = suffix or detect_suffix(image)
            except Exception as exc:  # noqa: BLE001 - 坏数据只降级
                logger.warning("image_gen 图片 base64 无效：%s", exc)
                return GenResult(False, error="图画好了但取不回来，稍后再试试")

        if not image:
            return GenResult(False, error="图画好了但取不回来，稍后再试试")
        if len(image) > MAX_IMAGE_BYTES:
            logger.warning("image_gen 图片过大（%s 字节），不发送", len(image))
            return GenResult(False, error="这张图太大发不出去，换个描述试试")
        # 接口没声明格式时按文件头猜；猜不出按 png
        return GenResult(True, image=image, suffix=detect_suffix(image))

    async def _download(self, url: str) -> bytes | None:
        client = await self._get_client()
        try:
            # 图床通常会重定向到 CDN，跟随跳转再取字节
            response = await client.get(url, timeout=DOWNLOAD_TIMEOUT, follow_redirects=True)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("image_gen 下载图片失败（%s）：%s", url, exc)
            return None
        return response.content

    def save_image(self, image: bytes, suffix: str = ".png") -> Path | None:
        """把图片留一份在插件 data 目录；失败不致命（照常发送）。"""
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
            # 同一秒内并发生成时也不能互相覆盖：补一段进程内计数
            self._saved_count += 1
            path = self.data_dir / f"{stamp}-{os.getpid()}-{self._saved_count}{suffix}"
            path.write_bytes(image)
            return path
        except OSError as exc:
            logger.warning("image_gen 保存图片失败：%s", exc)
            return None

    # ------------------------------------------------------------------
    # 发送
    # ------------------------------------------------------------------

    async def _send_image(self, runtime, event, result: GenResult) -> None:
        path = self.save_image(result.image, result.suffix)
        adapter = _adapter_for(runtime, event)
        if adapter is None:
            logger.debug(
                "image_gen：没有平台 %s 的适配器，发不出去", getattr(event, "platform", "")
            )
            return
        # 图片段用 base64：OneBot 的 image 段支持 base64://，Telegram 会直接上传；
        # 不把接口的临时 URL 当图床长期引用（本地 data 目录已经留了一份）
        segment = Segment(
            "image",
            {
                "base64": base64.b64encode(result.image).decode("ascii"),
                "mime_type": _mime_for(result.suffix),
                "filename": path.name if path is not None else f"image{result.suffix}",
            },
        )
        try:
            await adapter.send(event.chat_id, [segment], chat_type=event.chat_type)
        except Exception as exc:  # noqa: BLE001 - 图片发不出去就降级发文字
            logger.warning("image_gen 图片段发送失败（%s），降级发文字说明", exc)
            location = f"（图已存在 {path}）" if path is not None else ""
            await self._reply(runtime, event, f"图画好了但发不出来{location}")

    async def _reply(self, runtime, event, text: str) -> None:
        adapter = _adapter_for(runtime, event)
        if adapter is None:
            return
        try:
            await adapter.send(event.chat_id, [Segment.text(text)], chat_type=event.chat_type)
        except Exception as exc:  # noqa: BLE001 - 发送失败不能拖垮主流程
            logger.warning("image_gen 回复发送失败：%s", exc)


def _adapter_for(runtime, event):
    adapters = getattr(runtime, "adapters", None)
    if not isinstance(adapters, Mapping):
        return None
    return adapters.get(getattr(event, "platform", ""))


def _mime_for(suffix: str) -> str:
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(suffix.lower(), "image/png")


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except Exception:  # noqa: BLE001 - 非 JSON 响应按空处理
        return None


def _safe_text(response: httpx.Response) -> str:
    try:
        return response.text
    except Exception:  # noqa: BLE001
        return ""


def _safe_body(response: httpx.Response) -> str:
    data = _safe_json(response)
    if data is not None:
        return str(data)
    return _safe_text(response)


def _as_list(value: Any) -> tuple[str, ...] | None:
    """显式写成列表（哪怕空）则尊重配置；字段缺失返回 None 表示用默认。"""
    if value is None:
        return None
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return None
    return tuple(
        text for text in (str(item).strip() for item in value if item is not None) if text
    )


def _as_non_negative(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return max(0.0, float(value))


def _as_non_negative_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def register(ctx) -> None:
    """插件入口：配了 key 才注册消息钩子；没配只留一行日志。"""
    config = load_config(ctx)
    if not config.configured:
        logger.info("image_gen 未配置 api_key，画图触发未注册")
        return
    generator = ImageGenerator(config, data_dir=data_dir_for(ctx))
    ctx.extras["image_gen"] = generator
    ctx.on_message(generator.on_message)
    ctx.on_stop(generator.aclose)
    logger.debug(
        "image_gen 已注册（模型 %s，冷却 %.0fs，每日上限 %s）",
        config.model,
        config.cooldown_seconds,
        config.daily_limit or "不限",
    )
