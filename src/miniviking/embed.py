"""BGE 系列词嵌入：本地 GGUF 推理（llama.cpp，无需 torch）与 OpenAI 兼容 API。

本地路径与 OpenViking 同款：`llama-cpp-python` 加载 BGE 的 GGUF 权重，经
hf-mirror.com 镜像直连自动下载；依赖 `aaa[embedding]` 可选增强
（llama-cpp-python + huggingface_hub）。未安装或下载失败时返回 None，
引擎自动降级为纯关键词检索并告警，不阻断。
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Optional, Protocol, Sequence

from .config import EmbeddingSettings

logger = logging.getLogger("aaa.memory.miniviking")

GGUF_REPO_PREFIX = "CompendiumLabs"  # BGE 系列 GGUF 转换的常用发布方（small/base/large/m3 全系）


class Embedder(Protocol):
    def name(self) -> str: ...
    def dimension(self) -> int: ...
    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class LocalBGE:
    """BGE 系列 GGUF 本地推理（llama.cpp）；权重缺失时经镜像直连自动下载。"""

    def __init__(
        self, model: str, cache_folder: str, *, endpoint: str = "", dimensions: int = 0,
        model_path: str = "", repo: str = "", file: str = "",
    ) -> None:
        if endpoint:
            os.environ["HF_ENDPOINT"] = endpoint
        try:
            from llama_cpp import Llama
        except ImportError as exc:
            raise RuntimeError(
                "未安装嵌入增强依赖；请安装可选增强（如 pip install 'aaa[embedding]'）"
            ) from exc
        logical = _logical_name(model)
        weights = self._resolve_weights(
            logical, cache_folder,
            model_path=model_path, repo=repo, file=file, endpoint=endpoint,
        )
        # 权重解析阶段已按镜像模式剥离代理；推理本身不联网。
        self._llama = Llama(model_path=str(weights), embedding=True, verbose=False)
        self._name = f"local-gguf:{logical}"
        n_embd = self._llama.n_embd
        self._dim = dimensions or int(n_embd() if callable(n_embd) else n_embd)
        self._lock = threading.Lock()

    @staticmethod
    def _resolve_weights(
        logical: str, cache_folder: str, *, model_path: str, repo: str, file: str,
        endpoint: str,
    ):
        local = str(model_path or "").strip()
        if local:
            path = Path(local).expanduser()
            if not path.is_file():
                raise ValueError(f"model_path 不存在：{path}")
            return path
        from huggingface_hub import hf_hub_download

        repo_id = repo.strip() or f"{GGUF_REPO_PREFIX}/{logical}-gguf"
        candidates = [file.strip()] if file.strip() else [
            f"{logical}-f16.gguf", f"{logical}-f32.gguf", f"{logical}-q8_0.gguf",
        ]
        stripped = {}
        if endpoint:
            # 镜像模式即直连下载：剥离代理环境，避免 SOCKS 代理缺 socksio
            # 导致 huggingface_hub 构建客户端失败；结束后恢复。
            for key in ("ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy",
                        "HTTP_PROXY", "http_proxy"):
                value = os.environ.pop(key, None)
                if value is not None:
                    stripped[key] = value
        try:
            last_error: Optional[Exception] = None
            for filename in candidates:
                try:
                    return hf_hub_download(
                        repo_id=repo_id, filename=filename, cache_dir=cache_folder,
                    )
                except Exception as exc:  # 文件名候选逐个回退
                    last_error = exc
            mirror = os.environ.get("HF_ENDPOINT", "")
            raise RuntimeError(f"镜像 {mirror} 上找不到 {repo_id} 的权重") from last_error
        finally:
            os.environ.update(stripped)

    def name(self) -> str:
        return self._name

    def dimension(self) -> int:
        return self._dim

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        with self._lock:  # llama.cpp 的上下文不允许并发推理
            response = self._llama.create_embedding(input=list(texts))
        return [_normalize(item["embedding"]) for item in response["data"]]


class ApiBGE:
    """OpenAI 兼容 embeddings 端点上的 BGE 系列（如 SiliconFlow 的 bge-m3）。"""

    def __init__(
        self, model: str, base_url: str, api_key: str, *,
        dimensions: int = 0, timeout: float = 30.0,
    ) -> None:
        from .llm import ChatConfig, openai_client

        self._model = model
        self._name = f"api:{base_url}#{model}"
        self._client = openai_client(ChatConfig(
            base_url=base_url, api_key=api_key, model=model, timeout=timeout,
        ))
        self._dim = dimensions

    def name(self) -> str:
        return self._name

    def dimension(self) -> int:
        if not self._dim:
            self._dim = len(self.embed(["维数探测"]))
        return self._dim

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        response = self._client.embeddings.create(model=self._model, input=list(texts))
        vectors = [[float(value) for value in item.embedding] for item in response.data]
        return [_normalize(vector) for vector in vectors]


def build_embedder(settings: EmbeddingSettings, data_dir) -> Optional[Embedder]:
    """按配置构造嵌入器；任何失败都降级为 None（纯关键词检索），只告警不抛出。"""
    if not settings.enable:
        return None
    try:
        if settings.provider == "api":
            key = settings.api_key or os.environ.get(settings.api_key_env, "")
            if not (settings.base_url and settings.model and key):
                logger.warning("miniviking 嵌入 provider=api 需要 base_url、model 与 api_key，已降级为关键词检索")
                return None
            return ApiBGE(
                settings.model, settings.base_url, key,
                dimensions=settings.dimensions,
            )
        return LocalBGE(
            settings.model, cache_folder=str(data_dir / "models"),
            endpoint=settings.endpoint, dimensions=settings.dimensions,
            model_path=settings.model_path, repo=settings.repo, file=settings.file,
        )
    except Exception as exc:
        logger.warning(
            "miniviking 词嵌入不可用（%s），语义检索已降级为关键词检索：%s",
            type(exc).__name__, exc,
        )
        return None


def _logical_name(model: str) -> str:
    """BGE 逻辑名：兼容 org/name 写法取末段；短名原样（如 bge-small-zh-v1.5）。"""
    name = str(model or "").strip().split("/")[-1]
    if not name:
        raise ValueError("嵌入模型名不能为空")
    return name


def _normalize(vector: list[float]) -> list[float]:
    norm = sum(value * value for value in vector) ** 0.5
    if norm <= 0:
        return vector
    return [value / norm for value in vector]
