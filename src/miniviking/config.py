"""引擎自管配置：`<data_dir>/config.toml` 可选，缺省用内置默认值。

模型默认继承 aaa 的默认渠道与模型；需要独立模型时在 `[llm]` 里显式给出
`provider`/`model`/`base_url`/`api_key`（或 `api_key_env`）。
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger("aaa.memory.miniviking")


@dataclass(frozen=True)
class LLMSettings:
    inherit: bool = True
    provider: str = ""
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    api_key_env: str = "MINIVIKING_API_KEY"
    temperature: float = 0.2
    max_tokens: int = 2000
    timeout: float = 90.0


@dataclass(frozen=True)
class Limits:
    max_memories_per_turn: int = 6
    max_turn_chars: int = 12000
    max_file_bytes: int = 2_097_152
    max_files_per_import: int = 200
    max_import_depth: int = 3
    chunk_chars: int = 4000
    max_chunks_per_resource: int = 20
    search_limit: int = 50
    read_full_chars: int = 8000
    browse_depth: int = 3
    list_limit: int = 200
    close_timeout: float = 3.0


@dataclass(frozen=True)
class EmbeddingSettings:
    enable: bool = True
    provider: str = "local"                  # local（GGUF 下载 + llama.cpp 本地推理）| api（OpenAI 兼容 embeddings）
    model: str = "bge-small-zh-v1.5"         # BGE 系列：bge-small-zh-v1.5、bge-m3、bge-large-zh-v1.5……
    endpoint: str = "https://hf-mirror.com"  # HuggingFace 镜像；置空则直连 huggingface.co
    model_path: str = ""                     # 本地 GGUF 路径；设置后跳过下载
    repo: str = ""                           # GGUF 所在 HF 仓库；默认 gpustack/{model}-GGUF
    file: str = ""                           # GGUF 文件名；默认 {model}-f16.gguf
    base_url: str = ""                       # provider=api 时的 embeddings 端点
    api_key: str = ""
    api_key_env: str = "MINIVIKING_EMBEDDING_API_KEY"
    dimensions: int = 0                      # 0 = 从模型自动探测
    query_instruction: str = ""              # 只加在查询前，不加在节点文本前
    max_chars: int = 800                     # 单条节点参与嵌入的最大字符数
    batch: int = 16


@dataclass(frozen=True)
class Config:
    llm: LLMSettings = LLMSettings()
    limits: Limits = Limits()
    embedding: EmbeddingSettings = EmbeddingSettings()


def load(data_dir: Path) -> Config:
    path = Path(data_dir) / "config.toml"
    if not path.is_file():
        return Config()
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("miniviking 配置无法解析，使用默认值：%s", type(exc).__name__)
        return Config()
    if not isinstance(raw, dict):
        return Config()
    return Config(
        llm=_llm(raw.get("llm")),
        limits=_limits(raw.get("limits")),
        embedding=_embedding(raw.get("embedding")),
    )


def _llm(raw: Any) -> LLMSettings:
    if not isinstance(raw, Mapping):
        return LLMSettings()
    base = LLMSettings()
    return LLMSettings(
        inherit=_bool(raw, "inherit", base.inherit),
        provider=_text(raw, "provider", base.provider),
        model=_text(raw, "model", base.model),
        base_url=_text(raw, "base_url", base.base_url),
        api_key=_text(raw, "api_key", base.api_key),
        api_key_env=_text(raw, "api_key_env", base.api_key_env),
        temperature=_number(raw, "temperature", base.temperature, 0.0, 2.0),
        max_tokens=_integer(raw, "max_tokens", base.max_tokens, 64, 32_000),
        timeout=_number(raw, "timeout", base.timeout, 5.0, 600.0),
    )


def _limits(raw: Any) -> Limits:
    if not isinstance(raw, Mapping):
        return Limits()
    base = Limits()
    return Limits(
        max_memories_per_turn=_integer(raw, "max_memories_per_turn", base.max_memories_per_turn, 1, 20),
        max_turn_chars=_integer(raw, "max_turn_chars", base.max_turn_chars, 500, 60_000),
        max_file_bytes=_integer(raw, "max_file_bytes", base.max_file_bytes, 1_024, 64 * 1024 * 1024),
        max_files_per_import=_integer(raw, "max_files_per_import", base.max_files_per_import, 1, 5_000),
        max_import_depth=_integer(raw, "max_import_depth", base.max_import_depth, 1, 10),
        chunk_chars=_integer(raw, "chunk_chars", base.chunk_chars, 500, 20_000),
        max_chunks_per_resource=_integer(raw, "max_chunks_per_resource", base.max_chunks_per_resource, 1, 200),
        search_limit=_integer(raw, "search_limit", base.search_limit, 1, 200),
        read_full_chars=_integer(raw, "read_full_chars", base.read_full_chars, 500, 40_000),
        browse_depth=_integer(raw, "browse_depth", base.browse_depth, 1, 8),
        list_limit=_integer(raw, "list_limit", base.list_limit, 1, 1_000),
        close_timeout=_number(raw, "close_timeout", base.close_timeout, 0.5, 4.0),
    )


def _embedding(raw: Any) -> EmbeddingSettings:
    if not isinstance(raw, Mapping):
        return EmbeddingSettings()
    base = EmbeddingSettings()
    provider = _text(raw, "provider", base.provider)
    if provider not in ("local", "api"):
        logger.warning("miniviking 配置 embedding.provider 只能是 local 或 api，使用默认值 local")
        provider = "local"
    return EmbeddingSettings(
        enable=_bool(raw, "enable", base.enable),
        provider=provider,
        model=_text(raw, "model", base.model),
        endpoint=_text(raw, "endpoint", base.endpoint),
        model_path=_text(raw, "model_path", base.model_path),
        repo=_text(raw, "repo", base.repo),
        file=_text(raw, "file", base.file),
        base_url=_text(raw, "base_url", base.base_url),
        api_key=_text(raw, "api_key", base.api_key),
        api_key_env=_text(raw, "api_key_env", base.api_key_env),
        dimensions=_integer(raw, "dimensions", base.dimensions, 0, 4096),
        query_instruction=_text(raw, "query_instruction", base.query_instruction),
        max_chars=_integer(raw, "max_chars", base.max_chars, 100, 8000),
        batch=_integer(raw, "batch", base.batch, 1, 128),
    )


def _text(raw: Mapping, key: str, default: str) -> str:
    value = raw.get(key, default)
    return value.strip() if isinstance(value, str) else default


def _bool(raw: Mapping, key: str, default: bool) -> bool:
    value = raw.get(key, default)
    return value if isinstance(value, bool) else default


def _integer(raw: Mapping, key: str, default: int, minimum: int, maximum: int) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        logger.warning("miniviking 配置 %s 需要整数，使用默认值 %s", key, default)
        return default
    return min(max(value, minimum), maximum)


def _number(raw: Mapping, key: str, default: float, minimum: float, maximum: float) -> float:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        logger.warning("miniviking 配置 %s 需要数字，使用默认值 %s", key, default)
        return default
    return min(max(float(value), minimum), maximum)
