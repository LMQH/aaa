"""secret.yaml 的读写、schema 归一与渠道探查。

secret.yaml 只存渠道（含 api_key）与默认模型；非密钥的可调设置在同目录的
config.toml（见 `settings.py`）。v1 的 `providers` 列表写法在内存里转成 v2
映射，启动时不改用户文件，下次保存才落新格式。
"""

import json
import logging
import os
import re
import urllib.parse
import urllib.request
from typing import Dict, List, Optional

import yaml

from .paths import HOME_DIR, SECRET_FILE
from .settings import Settings
from ..model.provider import DEFAULT_API, parse_cost

CONFIG_VERSION = 2

logger = logging.getLogger(__name__)


class ConfigError(Exception):
    """secret.yaml 存在但读不出有效配置；此时绝不进入向导，避免覆盖用户文件。"""


def load_config(settings: Settings) -> Optional[Dict]:
    """读取并归一 secret.yaml。

    文件不存在返回 None（首次启动）；存在但不可解析或没有有效渠道时抛
    `ConfigError`——调用方据此报错退出，不覆盖原文件。
    """
    if not SECRET_FILE.exists():
        return None
    try:
        data = yaml.safe_load(SECRET_FILE.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise ConfigError(f"无法解析 {SECRET_FILE}：{exc}")
    if not isinstance(data, dict):
        raise ConfigError(f"{SECRET_FILE} 顶层必须是映射")
    config = normalize_config(data, settings)
    if config is None:
        raise ConfigError(f"{SECRET_FILE} 里没有可用的渠道（需要 base_url、api_key 与 models）")
    return config


def save_config(config: Dict) -> None:
    """原子写入 secret.yaml，权限固定 0600。"""
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    temp = SECRET_FILE.with_suffix(".tmp")
    temp.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    os.chmod(temp, 0o600)
    temp.replace(SECRET_FILE)


def normalize_config(data: Dict, settings: Settings) -> Optional[Dict]:
    """把任意版本的配置归一成 v2：providers 映射 + default 两键 + enabled_models。"""
    providers = data.get("providers")
    if isinstance(providers, list):
        channels: Dict[str, Dict] = {}
        for index, item in enumerate(providers):
            if not isinstance(item, dict):
                continue
            provider_id = _legacy_id(item, index, channels)
            channel = _channel(provider_id, item, settings)
            if channel:
                channels[provider_id] = channel
        default = None
    elif isinstance(providers, dict):
        channels = {}
        for provider_id, item in providers.items():
            if not isinstance(item, dict):
                continue
            channel = _channel(provider_id, item, settings)
            if channel:
                channels[provider_id] = channel
        default = data.get("default")
    else:
        return None
    if not channels:
        return None

    provider, model = _default_pair(channels, default, data.get("default_model"))
    return {
        "version": CONFIG_VERSION,
        "default": {"provider": provider, "model": model},
        "enabled_models": [item for item in (data.get("enabled_models") or []) if isinstance(item, str)],
        "providers": channels,
    }


def _channel(provider_id: str, raw: Dict, settings: Settings) -> Optional[Dict]:
    """单个渠道：地址、密钥、模型三者缺一不可。"""
    base_url = (raw.get("base_url") or "").rstrip("/")
    api_key = raw.get("api_key") or ""
    models = _models(provider_id, raw, settings)
    if not base_url or not api_key or not models:
        return None
    channel = {
        "name": raw.get("name") or provider_id,
        "api": raw.get("api") or DEFAULT_API,
        "base_url": base_url,
        "api_key": api_key,
        "models": models,
    }
    return channel


def _models(provider_id: str, raw: Dict, settings: Settings) -> List[Dict]:
    """模型条目：v1 是 id 列表 + context_windows 旁表，v2 是对象列表。

    `cost` 是可选的价目；形状不合法时丢弃该价目并告警，不让启动失败。
    """
    fallback_window = settings.get("model.default_context_window")
    windows = raw.get("context_windows") or {}
    models: List[Dict] = []
    for item in raw.get("models") or []:
        if isinstance(item, str):
            entry = {
                "id": item,
                "context_window": int(windows.get(item) or 0) or fallback_window,
            }
        elif isinstance(item, dict) and item.get("id"):
            entry = dict(item)
            entry["context_window"] = int(entry.get("context_window") or 0) or fallback_window
        else:
            continue
        if "cost" in entry:
            try:
                parse_cost(entry["cost"])
            except ValueError as exc:
                logger.warning("忽略 %s/%s 的价目：%s", provider_id, entry.get("id"), exc)
                entry.pop("cost", None)
        models.append({key: value for key, value in entry.items() if value is not None})
    return models


def _legacy_id(item: Dict, index: int, taken: Dict[str, Dict]) -> str:
    """v1 渠道没有 id：取主机名首段（api.deepseek.com -> deepseek），冲突时加序号。"""
    host = urllib.parse.urlparse(item.get("base_url") or "").hostname or item.get("name") or ""
    labels = [label for label in host.split(".") if label]
    if labels and labels[0] == "api":
        labels = labels[1:] or labels
    base = re.sub(r"[^a-z0-9-]", "-", (labels[0] if labels else "").lower()).strip("-")
    base = base or f"channel{index + 1}"
    provider_id, suffix = base, 2
    while provider_id in taken:
        provider_id = f"{base}-{suffix}"
        suffix += 1
    return provider_id


def _default_pair(channels: Dict[str, Dict], default, legacy_model) -> tuple:
    """默认模型：结构化配置优先，失效时回落到第一个渠道的第一个模型。"""
    if isinstance(default, dict):
        provider, model = default.get("provider"), default.get("model")
    else:
        provider, model = next(iter(channels)), legacy_model
    if provider not in channels:
        provider = next(iter(channels))
    ids = [item["id"] for item in channels[provider]["models"]]
    return provider, (model if model in ids else ids[0])


def probe_models(base_url: str, api_key: str):
    """调用 OpenAI 兼容的 GET /models，返回 (模型 id 列表, 上下文窗口)。

    不同服务商字段名不一；能读到就用，读不到返回 0，由用户手动填写。
    """
    url = base_url.rstrip("/") + "/models"
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))

    models: List[str] = []
    windows: Dict[str, int] = {}
    for item in payload.get("data", []):
        if not item.get("id"):
            continue
        models.append(item["id"])
        for key in ("context_length", "context_window", "max_context_length", "max_input_tokens"):
            value = item.get(key)
            if isinstance(value, int) and value > 0:
                windows[item["id"]] = value
                break
    return models, windows
