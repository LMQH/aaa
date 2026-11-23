# MiniViking

随 aaa 一起安装的外置记忆引擎：自动把对话蒸馏成可检索的上下文树，并在后续对话开始前自动召回注入。aaa 提供插件协议与身份，本引擎自管配置、模型、存储与后台任务；aaa 核心不 import 本包，接入只经 `aaa.memory_engines` 入口点。机制与不变量见 [DESIGN.md](DESIGN.md)。

## 启用

在 aaa 里选择并手动重启（默认关闭）：

```text
/memory engine external:miniviking
```

启用后 Agno 内置记忆关闭；启动失败时 aaa 以关闭状态运行，已保存的选择保留。

## 它做什么

- **自动蒸馏**：每轮结束后后台调用 LLM，抽取值得长期保留的信息（偏好、事实、决定、约定），写成记忆节点并滚动更新会话摘要；同标题自动合并，按 `run_id` 幂等。
- **自动召回**：每轮开始前按输入查本地索引（中文子串友好），命中条目带 `viking://` URI 注入 system prompt；下一轮不重复注入。
- **一棵上下文树**：记忆、导入的资源、技能说明都是同一文件树上的节点；文件树是事实源，SQLite 只是可删可重建的派生索引。
- **显式读写**：模型可用下表工具直接读写记忆树；删除、导入、写技能会请求人工确认。

| 工具 | 用途 | 确认 |
|---|---|---|
| `viking_search` | 关键词检索（memory / resource / skill / all） | 否 |
| `viking_read` | 读节点：abstract / overview / full；可查导入任务状态 | 否 |
| `viking_browse` | 浏览树：tree / list / stat | 否 |
| `viking_remember` | 显式写入一条记忆 | 否 |
| `viking_forget` | 按精确 URI 删除 | 是 |
| `viking_add_resource` | 导入本地文件/目录（后台切块、摘要、入索引） | 是 |
| `viking_add_skill` | 写入技能说明（SKILL.md，数据不执行） | 是 |

## 数据在哪

默认 `~/.aaa/memory/miniviking/`（经 aaa 的 `memory.data_root` 传入）：

```text
├── config.toml        # 可选：模型与限额覆盖
├── state.sqlite3      # 派生索引 + 任务队列（可删，启动时自动重建）
└── vault/<用户>/<agent>/
    ├── memories/      # 蒸馏出的记忆节点
    ├── sessions/      # 会话摘要
    ├── resources/     # 导入的资源（分块 + 索引）
    ├── skills/        # 技能数据（SKILL.md，不执行）
    └── journal/       # 原始轮次（蒸馏队列的事实来源）
```

数据按用户隔离；身份由 aaa 注入，模型不能通过工具参数指定身份。

## 语义检索（可选增强）

默认开启 BGE 语义检索，需要安装可选增强（llama.cpp 跑 GGUF + sqlite-vec 向量索引，**不需要 torch**，仅约 50MB 依赖）。**两种安装方式**：

1. **在 aaa 里装（推荐，无需命令行）**：`/memory embedding install`——后台安装（默认源失败自动改用国内镜像重试），`/memory embedding` 查看进度；选中 miniviking 引擎时也会主动询问是否安装。
2. **命令行**：`pip install 'aaa[embedding]'`（或 uv/pipx 等效命令）。

- 内置默认模型 `bge-small-zh-v1.5`（f16，512 维）；支持全部 BGE 系列按模型名配置。
- 启用引擎后首次使用自动从国内镜像 **hf-mirror.com** **直连下载** GGUF 权重（约 48MB，来源 `CompendiumLabs/{model}-gguf`，BGE 全系 base/large/m3 同模式）到 `<data_dir>/models/`，全程不走代理；之后纯本地推理，查询嵌入本地完成，不发起网络请求。
- 也可改用远程 embedding API（OpenAI 兼容端点，如 SiliconFlow 的 bge-m3）：

```toml
[embedding]
provider = "api"
model = "BAAI/bge-m3"
base_url = "https://api.siliconflow.cn/v1"
api_key = "sk-..."

# 本地来源覆盖（可选）：
# model = "bge-m3"
# model_path = "/path/to/bge-m3-f16.gguf"   # 本地文件，跳过下载
# repo = "CompendiumLabs/bge-m3-gguf"       # 自定义 GGUF 仓库
# file = "bge-m3-f16.gguf"                  # 自定义文件名
```

- 未安装增强、权重下载失败或推理异常时，**自动降级为纯关键词检索**并告警，记忆功能不受影响。
- 换嵌入模型后，后台任务自动对全部节点重新生成向量。

## 模型与配置

- 抽取与摘要默认**继承 aaa 的默认渠道与模型**（`~/.aaa/secret.yaml` 的 `default`，OpenAI 兼容端点）。
- 需要独立模型或关闭继承时，写 `<data_dir>/config.toml`：

```toml
[llm]
inherit = false
provider = "my-provider"
model = "my-model"
base_url = "https://..."
api_key = "sk-..."        # 或 api_key_env = "MINIVIKING_API_KEY"

[limits]                   # 均有内置默认值，可只写需要覆盖的键
max_memories_per_turn = 6
chunk_chars = 4000
```

- 模型不可用（未配置、调用失败重试耗尽）时退化为机械摘要：原始轮次仍可检索，记忆功能不断。

## 边界

- 只做本地文件/目录导入，不做 URL；网络请求只有两处——蒸馏所配的模型与嵌入（GGUF 权重下载 / API），且都不阻断使用（失败降级）。
- 中文检索为关键词（FTS5 trigram + 抽取关键词）与 BGE 语义的混合；多进程写同一数据目录不受支持。
- `/memory` 显示的条目数来自本引擎；与 aaa 的 `load_skill` 技能目录是两套体系，互不影响。

## 与 OpenViking 的关系

MiniViking 的定位与一部分机制借鉴自 [OpenViking](https://github.com/volcengine/OpenViking)（火山引擎开源的 Agent 上下文数据库）：上下文树的组织方式（记忆、资源、技能同构存放）、`viking://` 式 URI 寻址、L0/L1 摘要分层，以及"GGUF 权重 + llama.cpp 本地推理"的嵌入方案。

但它不是 OpenViking 的移植或子集——只借鉴设计思想，未复制任何代码：

- **体量**：OpenViking 是服务化的独立产品（自带原生向量引擎、Web 网关与云 API 生态）；MiniViking 是随 aaa 分发的单一插件引擎，事实源就是一个文件树加 SQLite。
- **运行形态**：OpenViking 独立部署，经 SDK/MCP 被多个宿主使用；MiniViking 只服务 aaa 一个宿主，生命周期与 aaa 完全绑定。
- **许可**：OpenViking 为 AGPL-3.0；MiniViking 是 aaa 的原创实现，仅受 aaa 自身许可约束。

## 开发与测试

- 源码态（aaa 仓库内 `PYTHONPATH=src` 运行）入口点不可见，需在隔离 HOME 下放一行 shim：`~/.aaa/memory_engines/miniviking.py` 内容 `from miniviking.aaa_adapter import ENGINE`。
- 测试（不联网、假模型）：

```bash
PYTHONPATH=src:tests AGNO_TELEMETRY=false .venv/bin/python -B -m unittest test_miniviking
```

- 打包：`uv build`；wheel 同时含 `aaa/` 与 `miniviking/`，入口点组 `aaa.memory_engines`。
