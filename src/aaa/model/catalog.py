"""Catalog：渠道目录、可切换范围与模型引用解析。

引用形式照 Pi：`provider/model_id`；解析顺序是整串精确、已知渠道 id 最长前缀、
裸 id 唯一匹配。多渠道出现同名模型时必须返回候选而不是取第一个。

配置在构造时绑定（窗口默认值、fallback 上限、模型超时），因此换配置要重建
Catalog——与「换模型即重建 Agent」同一条纪律。
"""

from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

from agno.models.base import Model

from .provider import (
    DEFAULT_API,
    Channel,
    ChannelModel,
    SettingsLike,
    build_model,
    build_title_model,
    parse_cost,
)


@dataclass(frozen=True)
class ModelRef:
    """模型身份：渠道 + 模型 id。切换、显示、落盘都用这一对。"""

    provider: str
    model_id: str

    @property
    def display(self) -> str:
        return f"{self.provider}/{self.model_id}"


@dataclass(frozen=True)
class Resolved:
    ref: ModelRef


@dataclass(frozen=True)
class Ambiguous:
    text: str
    candidates: Tuple[ModelRef, ...]


@dataclass(frozen=True)
class NotFound:
    text: str


Resolution = Union[Resolved, Ambiguous, NotFound]


def from_config(config: Dict, settings: SettingsLike) -> "Catalog":
    """把归一后的配置转成渠道对象；字段缺失由 config 层负责拦截。"""
    fallback_window = settings.get("model.default_context_window")
    channels: Dict[str, Channel] = {}
    for provider_id, raw in (config.get("providers") or {}).items():
        channels[provider_id] = Channel(
            id=provider_id,
            name=raw.get("name"),
            api=raw.get("api") or DEFAULT_API,
            base_url=raw["base_url"],
            api_key=raw["api_key"],
            models=tuple(
                ChannelModel(
                    id=item["id"],
                    api=item.get("api"),
                    context_window=int(item.get("context_window") or fallback_window),
                    max_tokens=item.get("max_tokens"),
                    cost=parse_cost(item.get("cost")),
                )
                for item in raw.get("models") or []
            ),
        )
    return Catalog(channels, settings)


class Catalog:
    """渠道目录：查询、构造模型、解析引用。渠道只在这里被枚举。"""

    def __init__(self, channels: Dict[str, Channel], settings: SettingsLike) -> None:
        self._channels = dict(channels)
        self._settings = settings

    def channels(self) -> Tuple[Channel, ...]:
        return tuple(self._channels.values())

    def channel(self, provider: str) -> Optional[Channel]:
        return self._channels.get(provider)

    def cost(self, provider: Optional[str], model_id: str):
        """按 (渠道, 模型) 取价目；渠道未知或没配价时返回 None。"""
        if not provider:
            return None
        channel = self._channels.get(provider)
        if channel is None:
            return None
        model = channel.find(model_id)
        return model.cost if model else None

    def require(self, provider: str, model_id: str) -> Tuple[Channel, ChannelModel]:
        """取渠道与模型；不存在时抛 ValueError，由调用方转成提示。"""
        channel = self._channels.get(provider)
        if channel is None:
            raise ValueError(f"未知渠道：{provider}")
        model = channel.find(model_id)
        if model is None:
            raise ValueError(f"渠道 {provider} 没有模型：{model_id}")
        return channel, model

    def refs(self) -> Tuple[ModelRef, ...]:
        return tuple(
            ModelRef(channel.id, model.id)
            for channel in self._channels.values()
            for model in channel.models
        )

    def scope(self, patterns: Iterable[str]) -> Tuple[ModelRef, ...]:
        """可切换范围：`enabled_models` 模式匹配；为空表示全部。

        模式同时匹配 `provider/model_id` 与裸 `model_id`，与 Pi 的 enabledModels 一致。
        """
        refs = self.refs()
        patterns = [pattern for pattern in (patterns or []) if pattern]
        if not patterns:
            return refs
        selected: List[ModelRef] = []
        for ref in refs:
            if ref in selected:
                continue
            for pattern in patterns:
                lowered = pattern.lower()
                if fnmatchcase(ref.display.lower(), lowered) or fnmatchcase(ref.model_id.lower(), lowered):
                    selected.append(ref)
                    break
        return tuple(selected)

    def resolve(self, text: str, pool: Optional[Iterable[ModelRef]] = None) -> Resolution:
        """解析用户输入的引用；池子默认是全量渠道。"""
        text = (text or "").strip()
        if not text:
            return NotFound(text)
        refs = tuple(pool) if pool is not None else self.refs()
        lowered = text.lower()

        for ref in refs:
            if ref.display.lower() == lowered:
                return Resolved(ref)

        # 渠道 id 可能含 "/"，模型 id 也可能含 "/"，所以按已知渠道名做最长前缀匹配，
        # 而不是按第一个 "/" 切分。
        for provider in sorted({ref.provider for ref in refs}, key=len, reverse=True):
            prefix = provider.lower() + "/"
            if not lowered.startswith(prefix):
                continue
            model_id = text[len(prefix):]
            for ref in refs:
                if ref.provider == provider and ref.model_id == model_id:
                    return Resolved(ref)
            return NotFound(text)

        candidates = tuple(ref for ref in refs if ref.model_id == text)
        if len(candidates) == 1:
            return Resolved(candidates[0])
        if candidates:
            return Ambiguous(text, candidates)
        return NotFound(text)

    def window(self, provider: str, model_id: str) -> int:
        return self.require(provider, model_id)[1].context_window

    def build(self, provider: str, model_id: str) -> Model:
        channel, model = self.require(provider, model_id)
        return build_model(channel, model, self._settings)

    def title_model(self, provider: str, model_id: str) -> Model:
        channel, model = self.require(provider, model_id)
        return build_title_model(channel, model)

    def fallbacks(self, provider: str, model_id: str) -> List[Model]:
        """同渠道其余模型，数量上限来自配置；跨渠道降级不隐式发生。"""
        channel, _ = self.require(provider, model_id)
        limit = int(self._settings.get("model.fallbacks"))
        if limit <= 0:
            return []
        return [
            build_model(channel, model, self._settings)
            for model in channel.models
            if model.id != model_id
        ][:limit]
