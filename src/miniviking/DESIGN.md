# MiniViking：随 aaa 分发的外置记忆引擎

MiniViking 是与 `src/aaa` 并列、同一 wheel 分发的独立 Python 包：aaa 提供插件协议、发现、身份与注入，MiniViking 自己管理配置、模型、存储与后台任务。`aaa` 核心不 import 本包；接入只经 `aaa.memory_engines` 入口点（`miniviking = miniviking.aaa_adapter:ENGINE`），与本地文件插件同一契约。开发态（`[tool.uv] package = false`）入口点不可发现，需在 `~/.aaa/memory_engines/miniviking.py` 放一行 shim 导入 `ENGINE`。

## 机制

### 数据形态

- 文件树是唯一事实源，位于 `<data_dir>/vault/<user-slug>/<agent-slug>/`，分 `memories/`、`sessions/`、`resources/`、`skills/` 四类目录；`journal/` 存原始轮次。SQLite（`state.sqlite3`，WAL）只是派生索引（FTS5 trigram + 节点表）与任务表，可删可重建；启动时索引为空或 schema 版本不符即全量重建。
- 节点是带 YAML frontmatter 的 Markdown：`title/abstract/overview/tags/keywords/created/updated/source/importance`。摘要由 LLM 生成（见蒸馏），模型不可用时机械生成（首行/首段截断）。
- URI 形如 `viking://<user-slug>/<agent-slug>/<kind>/<rel>`；任务 URI 用 `tasks/<id>`。slug 保留 Unicode 字母数字与 `._-`，改写时追加 sha1 前缀防碰撞；所有落盘路径校验在命名空间根之下，`..` 直接拒绝。

### 身份与隔离

只消费宿主传入的 `MemoryIdentity`，不从工具参数推断身份；工具参数出现 `user_id` 等键直接报错。存取维度是 `user_id + agent_id`；`session_id`/`run_id` 只用于来源、幂等（同一 `run_id` 不重复蒸馏）与会话摘要归属，不做隔离。

### 召回与注入

`recall` 只查本地索引，不调 LLM；范围是 `memories + sessions`。检索是双路混合（RRF 倒数排名融合）：

- **关键词路**：FTS5 trigram，拉丁词按短语、中文按 3-gram OR 再按 bm25 排序；单词不足 3 字符时逐词 LIKE 回退。
- **语义路**（可选增强）：BGE 系列词嵌入，与 OpenViking 同款方案——`llama-cpp-python` 加载 GGUF 权重本地推理，不引入 torch。默认 `bge-small-zh-v1.5`（f16），权重经 hf-mirror.com 镜像直连从 CompendiumLabs/{model}-gguf 自动下载到 `<data_dir>/models/`（镜像模式剥离代理环境，下载结束恢复；`model_path/repo/file` 可覆盖来源）；也可 `provider = "api"` 走 OpenAI 兼容 embeddings 端点。向量存 SQLite 的 **vec0 虚表**（sqlite-vec 扩展，KNN 与元数据过滤在 C 层完成；距离为 L2，归一化向量下与余弦同序），随节点写入由后台任务补齐，换模型自动重建虚表并重嵌；查询向量带 LRU 缓存，推理全程加锁（llama.cpp 上下文不并发）。未安装 `aaa[embedding]`、扩展加载失败、下载或推理失败时自动降级为纯关键词检索并告警。

召回条目文本自带 URI（`[viking://…] 标题 — 摘要`），模型可 `viking_read` 回读；截断与预算由宿主控制。`[embedding]` 的 `query_instruction` 只加在查询前。

### 观察（自动蒸馏）

每轮完成后 `observe` 把 `query/answer/identity` 原子写入 `journal/<session>/<run>.json`（已存在即跳过）并入队 `distill` 任务，5 秒超时内返回。后台 worker（单线程，`tasks.py`）调用 LLM：按系统提示抽取最多 `max_memories_per_turn` 条持久信息（标题/摘要/概述/正文/标签/关键词/重要度），并滚动更新会话摘要节点 `sessions/<session>.md`。同标题（slug 相等）的记忆执行合并：标签与关键词取并集、正文与摘要取新值、created 保留。模型不可用（未配置、初始化失败、调用失败重试耗尽）时，worker 退化为机械摘要，保证原始轮次仍可被检索。

### 模型

默认继承 aaa 的默认渠道与模型（读 `secret.yaml` 的 `default`，OpenAI 兼容端点，`openai` 客户端）；`<data_dir>/config.toml` 的 `[llm]` 可显式覆盖（`provider/model/base_url/api_key/api_key_env`），或 `inherit = false` 关闭继承。环境只有 SOCKS 代理且缺 `socksio` 时，客户端改用 HTTP(S) 代理或直连并告警。抽取与摘要只在后台发生，不进入召回路径。

### 资源导入与技能

- `viking_add_resource` 只做本地文件/目录（不做 URL）：入队 `resource` 任务并立即返回任务 URI；worker 校验类型/大小/数量/深度上限（超限记 `skipped`），按空行切块（默认 4k、最多 20 块），逐块 LLM 摘要后写 `resources/<root>/chunk-NNN.md` 与 `_index.md`。任务状态、失败原因与计数持久化在任务表；`viking_read` 任务 URI 可查状态；重启把 `running` 重置为 `pending` 续跑。
- `viking_add_skill` 写 `skills/<name>/SKILL.md`（数据，不执行）；默认拒绝覆盖同名，需 `overwrite=true`。不接 aaa 的 `load_skill`，两套技能目录并存。

### 工具面

七个工具，全部 `viking_` 前缀（避开宿主保留名）：`viking_search`、`viking_read`、`viking_browse`（只读，不确认）、`viking_remember`（写，不确认——增量、可合并）、`viking_forget`、`viking_add_resource`、`viking_add_skill`（写/删，需确认）。参数 schema 由本包声明，宿主只做装配、超时（`memory.tool_timeout`）与确认；用户级错误返回 `Error: <原因>` 文本，意外异常交给宿主转 `Error: <类型>`。浏览输出全部使用完整 URI，模型可直接回读。

### 提示块与配置

`prompt_block` 只含稳定规则（URI 形式、各工具用途、读取层级、身份不可指定），不含动态目录快照。`config.toml` 的 `[limits]` 管切块、条数、深度、`close_timeout` 等运行参数；语义性常量（工具名、kind、URI 前缀）是代码内常量。

### 生命周期

引擎由 aaa 的 `AppState` 持有并构造（只收一个数据目录 `Path`）；`close()` 先停任务 worker（等待 `limits.close_timeout`，未完成任务退回 `pending`），再关索引连接；重复关闭幂等。启动期失败（目录不可建、索引损坏等）由宿主按外置契约降级为关闭。

### 嵌入增强的安装器（embed_install）

`aaa[embedding]` 的三个依赖由 `embed_install.install` 装进 aaa 当前解释器环境：策略按环境探测（uv → pip → pipx），默认源失败自动改用国内镜像（清华源，仅本次命令参数，不改用户全局配置），全程失败给出可复制的手动命令。CLI 侧 `/memory embedding install` 在后台线程调用它（进度存内存缓冲，`/memory embedding` 查看；不打印进对话流，避免污染内联 UI），`/memory embedding` 显示状态；选中 miniviking 引擎且增强缺失时主动询问。安装写入 site-packages，重启 aaa 后生效——与引擎配置的生效约定一致。本模块不 import llama_cpp 之外的任何增强包，供 aaa CLI 懒加载调用。

## 不变量与边界

- 文件树是事实源；索引损坏或滞后不影响事实，可通过重建恢复。
- 身份四字段全部来自宿主；`/memory` 的 `run_id` 为 `None`，`session_id` 为当前会话或 `None`。
- 蒸馏、导入全部异步且持久化队列；工具调用永不阻塞在 LLM 上。
- 不做多进程并发写同一数据目录；语义检索是可选增强（BGE 本地推理或 API），缺依赖/失败自动降级为关键词。
- 导入只处理文本类文件，大小/数量/深度上限在 `limits`；网络请求只有两处：所配 LLM 的蒸馏调用与 BGE 权重/API 的嵌入调用，均不在召回与工具的关键路径上强制依赖（失败降级）。

## 验收对应

- 自动召回、观察、工具、`/memory` 同一 `MemoryIdentity`；身份不可经参数指定 → `tests/test_miniviking.py::ToolTest`、`HostContractTest`、`AgentIntegrationTest`。
- 同 `run_id` 幂等、同标题合并、无模型退化 → `DistillTest`。
- 资源导入任务状态与续跑 → `ResourceTest`、`tasks.py` 重置逻辑。
- `close` 无残留 worker → `CloseTest`。
- 工具面与确认声明 → `HostContractTest`（confirm 集合 `forget/add_resource/add_skill`）。
