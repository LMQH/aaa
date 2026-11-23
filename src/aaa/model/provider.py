"""Provider 层：渠道数据与 Agno 模型构造。

一个渠道（Channel）= 一个端点：协议、地址、密钥与模型清单。模型类由协议名查表
得到，不在渠道上硬编码——DeepSeek 会给除 deepseek-chat 外的模型注入
extra_body.thinking，自定义 OpenAI 兼容端点必须走 OpenAILike 才不发私有参数。

超时与重试来自配置（`model.timeout` / `retries` / `retry_delay`），由调用方
传入 `settings`；本模块不 import `aaa.config`，保持 config → model 的单向依赖。
"""

from dataclasses import dataclass
from typing import Any, Dict, Optional, Protocol, Tuple, Type

from agno.models.base import Model
from agno.models.deepseek import DeepSeek
from agno.models.openai.like import OpenAILike

DEFAULT_API = "deepseek"

# 协议名 -> Agno 模型类；新增协议只改这张表。
API_CLASSES: Dict[str, Type[Model]] = {
    "deepseek": DeepSeek,
    "openai-completions": OpenAILike,
}


class SettingsLike(Protocol):
    """只读配置的最小接口；具体实现是 `config.settings.Settings`。"""

    def get(self, path: str) -> Any: ...


@dataclass(frozen=True)
class OffPeak:
    """低谷价窗口：`windows` 内的时间按 `multiplier` 打折，窗口用本地时间的分钟数。

    `(start, end)` 允许 start > end，表示跨午夜（如 22:00 → 02:00）。
    """

    multiplier: float
    windows: Tuple[Tuple[int, int], ...] = ()


@dataclass(frozen=True)
class Cost:
    """一个 (渠道, 模型) 的单价，单位为「美元 / 百万 token」。"""

    input: float
    output: float
    cache_read: float = 0.0
    cache_write: float = 0.0
    off_peak: Optional[OffPeak] = None


def parse_cost(raw: Any) -> Optional[Cost]:
    """把 secret.yaml 的 `cost` 映射转成 `Cost`；形状不合法时抛 ValueError。

    调用方（`config.loader`）负责把异常降级为「丢弃该价目并告警」——价格是
    可选元数据，一个笔误不该让程序起不来。
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("cost 必须是映射")
    values = {}
    for key in ("input", "output", "cache_read", "cache_write"):
        default = 0.0 if key in ("cache_read", "cache_write") else None
        value = raw.get(key, default)
        if value is None:
            raise ValueError(f"cost.{key} 缺失")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"cost.{key} 必须是数字")
        if value < 0:
            raise ValueError(f"cost.{key} 不能为负")
        values[key] = float(value)

    off_peak = None
    raw_off = raw.get("off_peak")
    if raw_off is not None:
        if not isinstance(raw_off, dict):
            raise ValueError("cost.off_peak 必须是映射")
        multiplier = raw_off.get("multiplier")
        if isinstance(multiplier, bool) or not isinstance(multiplier, (int, float)):
            raise ValueError("cost.off_peak.multiplier 必须是数字")
        if not 0 <= multiplier <= 1:
            raise ValueError("cost.off_peak.multiplier 必须在 0 与 1 之间")
        windows = []
        for item in raw_off.get("windows") or []:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                raise ValueError("cost.off_peak.windows 每项必须是 [开始, 结束]")
            windows.append((_minute_of_day(item[0]), _minute_of_day(item[1])))
        if windows and multiplier == 1:
            raise ValueError("cost.off_peak.multiplier 为 1 时窗口无意义")
        off_peak = OffPeak(multiplier=float(multiplier), windows=tuple(windows))
    return Cost(off_peak=off_peak, **values)


def _minute_of_day(text: Any) -> int:
    """`"HH:MM"` → 当天第几分钟。"""
    if not isinstance(text, str) or ":" not in text:
        raise ValueError("时间窗口必须写成 HH:MM")
    hour, _, minute = text.partition(":")
    try:
        h, m = int(hour), int(minute)
    except ValueError:
        raise ValueError("时间窗口必须写成 HH:MM")
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError("时间窗口超出 00:00–23:59")
    return h * 60 + m


@dataclass(frozen=True)
class ChannelModel:
    """渠道下的一个模型：id 与它自己的元数据。"""

    id: str
    context_window: int
    api: Optional[str] = None
    max_tokens: Optional[int] = None
    cost: Optional[Cost] = None


@dataclass(frozen=True)
class Channel:
    """一个渠道端点：协议 + 地址 + 密钥 + 模型清单。"""

    id: str
    api: str
    base_url: str
    api_key: str
    models: Tuple[ChannelModel, ...]
    name: Optional[str] = None

    def api_for(self, model: ChannelModel) -> str:
        return model.api or self.api

    def find(self, model_id: str) -> Optional[ChannelModel]:
        for model in self.models:
            if model.id == model_id:
                return model
        return None


def build_model(channel: Channel, model: ChannelModel, settings: SettingsLike) -> Model:
    """渠道 + 模型的唯一构造点：协议查表，超时与重试来自配置。"""
    kwargs = dict(
        id=model.id,
        base_url=channel.base_url,
        api_key=channel.api_key,
        timeout=settings.get("model.timeout"),
        retries=settings.get("model.retries"),
        delay_between_retries=settings.get("model.retry_delay"),
        exponential_backoff=True,
    )
    if model.max_tokens:
        kwargs["max_tokens"] = model.max_tokens
    return API_CLASSES[channel.api_for(model)](**kwargs)


def build_title_model(channel: Channel, model: ChannelModel) -> Model:
    """生成会话标题用的模型：不参与会话，不套重试策略。"""
    return API_CLASSES[channel.api_for(model)](
        id=model.id, base_url=channel.base_url, api_key=channel.api_key
    )
