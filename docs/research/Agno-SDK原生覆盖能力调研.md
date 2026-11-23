# Agno SDK 原生覆盖能力调研

日期：2026-11-16
范围：本机安装 `agno 3.1.12`（`/tmp/aaa-verify/lib/python3.13/site-packages/agno`）
对照基线：`Harness最小实现公式模型与闭环实现准则.md`（五项公式）、`模型上下文治理方案.md`（Context 治理）

## 结论摘要

Agno 原生覆盖“能跑的 Harness”，并已内建 Context 治理的两条主机制：**会话压缩（compaction）**与**工具结果卸载（offload）**。缺的是成本治理的另一半——驻留预算、阶梯编排与成本归因——以及条目流数据模型。

| 组件 | 原生覆盖 | 结论 |
|---|---|---|
| Provider | 54 个供应商目录，统一 API、重试、回退、token 计量含缓存字段 | 可直接沿用，仅上下文窗口与单价需自备 |
| Tools | 152 个工具模块，函数/Toolkit/MCP/Context Provider/内置文件系统五种来源；超大结果卸载为可回读指针 | 可直接沿用，暴露面受静态列表约束 |
| AgentLoop | Agent 循环、流式事件、checkpoint、后台、HITL 暂停续跑、Team/Workflow | 主体可用，缺“每次模型请求前”接缝与单轮上限 |
| Context | 系统消息拼装、历史选择、**压缩与归档、工具结果卸载**、摘要、多源注入、Skills | 装配可用；定理 2/3/8 有原生落点，成本治理无原生实现 |
| Boundary | Guardrails、HITL 审批、tool_call_limit、重试、沙箱契约 | 有机制无策略源；沙箱是后端契约，不是隔离策略 |

## 调研方法

1. **源码为准**：结论以 `agno 3.1.12` 安装包为准，路径相对 `site-packages/agno/`。
2. **文档定名**：能力名称、参数语义、用法边界以 `docs.agno.com` 对应页面复核。
3. **缺口以“没有找到”为证据**：凡写“无原生实现”，均说明在对应模块检索过、未发现该能力。
4. **版本绑定**：源码结论只对 3.1.12 成立；升级後须复核第四、七、八节。

## 一、Provider

**原生能力**

- 54 个供应商目录覆盖 OpenAI、Anthropic、Google、DeepSeek、Groq、Mistral、Azure、AWS、litellm、llama.cpp、LM Studio 等；`model="provider:model_id"` 字符串或类实例两种配置方式。
- 请求级重试（`retries`、`delay_between_retries`、`exponential_backoff`），非可重试错误（鉴权、无效请求、上下文窗口错误）跳过；Agent/Team 级另可整轮重试。
- 回退模型（`fallback_models`）在限流、故障、上下文窗口错误时切换；窗口溢出时另有压缩模块的 `on_context_overflow` 重折路径（见 4.1）。
- `count_tokens` 统一计量；`metrics.py` 的 `ModelMetrics` 含 `input_tokens` / `output_tokens` / `cache_read_tokens` / `cache_write_tokens` / `reasoning_tokens` / `cost`。
- `ModelRequestStartedEvent` / `ModelRequestCompletedEvent` 逐请求上报，后者带 `input_tokens`、`cache_read_tokens`、`cache_write_tokens`、`time_to_first_token`。

**出处**

- 源码：`models/`（54 个供应商目录）、`models/base.py`（统一调用、tool 循环、token 计数）、`metrics.py`、`run/agent.py:471-490`

**覆盖判断**

Provider 职责（适配请求、响应、工具调用协议）**完整覆盖**；公理 5“供应商中立”天然成立；公理 7 的单价与缓存折扣有数据源，无成本模型。

**缺口**

- 全 `models/` 无 `context_window` 元数据：窗口大小不随模型对象暴露，需外部配置。
- 单价只在 usage 的 `cost` 字段（供应商上报则有），无定价表抽象。
- 缓存字段依赖供应商上报，跨供应商不齐。

## 二、Tools

**原生能力**

- 五种来源：Python 函数（按函数名、docstring、类型注解生成 schema）、Toolkit、MCP（`MCPTools`）、Context Provider、`fs.FileSystem`（数据库后端的持久文件系统，工具集含 read/list/search/write/append/replace_lines/move/delete/check_lines）。
- 执行流程：发送上下文与工具定义 → 模型请求调用 → 校验参数并执行 → 结果回填 → 继续直到最终回答；异步运行支持并行工具调用。工具循环在 `models/base.py` 的 `Model.response`（:651）/`response_stream`（:1367）内部，不在 Agent 层。
- 控制面：`tool_call_limit`（单次运行调用上限）、`include_tools`/`exclude_tools`、`tool_hooks`、`tool_choice`、工具异常配置。
- HITL 标记：`requires_confirmation`、`requires_user_input`、`external_execution`。
- 结果形态：字符串/数字/字典/列表，或 `ToolResult`（含图片、视频、音频、文件、元数据）。
- **超大结果卸载**：`offload_tool_results` 把超阈值结果写入 AgentFS，消息里留 `<result id=… lines=… size=…>` 信封加首尾预览，并自动追加 `read_result` / `search_result` 两个回读工具；两者不受 `NEVER_OFFLOADED_TOOLS` 与调用上限影响。

**出处**

- 源码：`tools/`（152 个模块）、`tools/function.py`、`tools/toolkit.py`、`offload/`、`fs/toolkit.py`、`agent/_tools.py:238-281`

**覆盖判断**

工具声明、执行、结果回传**完整覆盖**；结果卸载满足“替换须可解析”。

**缺口**

- 工具可见性由构造期静态列表决定（`tools=[…]` 或可调用工厂），与权限判定没有结构关联。
- 无“schema 目录化 + 元工具取回”。Context Provider 解决的是来源级紧凑接口，不是 schema 爆炸。
- 沙箱是独立后端契约（`sandbox/`），不在工具层；工具不因沙箱存在而自动隔离。

## 三、AgentLoop

**原生能力**

- `run`/`arun`/`print_response`，流式与事件（`stream_events`），`RunOutput`；`followups` 生成后续建议。
- `checkpoint`：`"runs"` / `"tool-batch"` / `"tools"` 三种存档粒度；工具批存档通过 `after_tool_results` 回调落到会话库。
- 后台执行、断线后 SSE 重连（AgentOS）。
- HITL：运行在需要确认/输入/外部执行时暂停，`continue_run` 按同一状态续跑。
- 重试与退避（整轮）、`tool_call_limit`、推理子循环（`reasoning_model`/`reasoning_agent`）。
- 上层循环变体：Team（`coordinate`/`route`/`broadcast`/`tasks`，`max_iterations=10`）、Workflow（`Steps`/`Parallel`/`Loop`/`Condition`/`Router`）。
- Learning：`agent.learning` 在 run 后抽取学习产物（见 4.1 记忆）。

**出处**

- 源码：`agent/_run.py`（主循环、预/后钩子、事件）、`models/base.py`（工具循环）、`team/_run.py`、`workflow/`、`run/agent.py`

**覆盖判断**

循环推进、输出处理、暂停续跑、流式与事件**覆盖**。

**缺口**

- 单 Agent 没有最大工具往返轮数：只有 `tool_call_limit`（调用次数）与 Team 的 `max_iterations`。
- **没有“每次模型请求前”的一等钩子**：`pre_hooks`/`post_hooks` 是 run 级（`agent/_run.py:469`、`:627`），工具循环在 `models/base.py` 内部；唯一落在“一批工具结果之后、下次请求之前”的 `after_tool_results` 是内部关键字参数（`models/base.py:845`），只被 `checkpoint="tool-batch"` 使用。
- 上下文窗口溢出只有压缩模块的重折路径，没有裁剪与降级。
- 停止原因以 `RunStatus` 表达，粒度粗于“明确结束原因”。

## 四、Context

### 4.1 原生装配与治理能力

| 能力 | 机制 | 出处 |
|---|---|---|
| 系统消息分层 | `instructions`、`description`、`system_message`、`expected_output`、`additional_context`，加名称/时间/地点/状态/依赖等动态字段 | `/agents/building-agents`、`agent/_messages.py:917` |
| 历史选择 | `add_history_to_context`、`num_history_runs`、`num_history_messages`、`max_tool_calls_from_history` | `/sessions/overview`、`agent/_messages.py:245` |
| **会话压缩** | `Compaction`：`compact_at_tokens` 阈值、`uncompacted_runs` 或 `uncompacted_tokens` 保留尾、尾部体积上限、`compacted_token_budget` 摘要预算、归档原文、检索归档、`on_context_overflow` 溢出重折、`enforce_min_fold_ratio` 最小折叠比 | `/compression/overview`、`compaction/compaction.py` |
| 压缩归档 | `CompactionRecord`（`first_kept_message_id` 锚点、tokens_before/after）存 `agno_compactions` 表；原文经 `render_messages` 存归档；`search_compacted_history` 工具按会话检索 | `compaction/archive.py`、`compaction/types.py` |
| **工具结果卸载** | `offload_tool_results`（True 或 `ResultStore(threshold_chars, preview_lines, preview_chars)`）；默认阈值 16,000 字符、预览 20 行 / 1,200 字符；单结果 8MB、单会话 200MB 配额 | `offload/store.py:30-46`、`offload/setup.py` |
| 工具结果压缩 | `compress_tool_results` / `CompressionManager`（改写 tool 消息的 `compressed_content`，原文留在 `content`），与卸载互斥 | `compression/manager.py`、`agent/_init.py:275` |
| 会话摘要 | `enable_session_summaries`、`add_session_summary_to_context`、`SessionSummaryManager(last_n_runs, conversation_limit)`；与压缩同开时会带两份历史摘要（有告警） | `/sessions/session-summaries`、`agent/_init.py:252` |
| 记忆（旧面） | `enable_agentic_memory` 经 `update_user_memory` 工具；`update_memory_on_run` 自动抽取；条目字段 `memory`/`memory_id`/`topics`/`user_id`/`input`/`updated_at`/`feedback` | `/memory/overview`、`memory/manager.py` |
| 记忆（新面） | `LearningMachine`：`user_profile`、`memories`、`session_context`、`entity_memory`、`learned_knowledge`、`decision_log` 六类存储 + `Curator`（prune / deduplicate） | `learn/` |
| 知识 | `Knowledge` + 向量库（21 种）+ 分块/嵌入/重排/读取器；Agentic RAG 与传统 RAG | `/knowledge/overview` |
| 状态与依赖 | `session_state`、`enable_agentic_state`、`dependencies`、`RunContext` | `/state/overview` |
| 跨会话检索 | `search_past_sessions`、`read_chat_history`、`read_tool_call_history` | `/history/overview` |
| 活跃上下文注入 | Context Provider 11 个内置来源（calendar、database、fs、gdrive、gmail、google、mcp、slack、web、wiki、workspace） | `/context-providers/overview` |
| 技能 | SKILL.md；索引进系统提示（`agent/_messages.py:1089`），正文/引用/脚本经 `get_skill_instructions` 等按需取 | `/skills/overview`、`skills/` |
| 计量 | `count_tokens`；压缩可用 `token_counter` 或 `use_model_token_count` 指定口径 | `/compression/token-counting` |
| 观测事件 | `ModelRequestStarted/Completed`、`CompressionStarted/Completed`、`CompactionStarted/Completed`（带 messages_compacted、tokens_before/after、archived） | `run/agent.py:471-530` |

### 4.2 与治理方案逐条对齐

**十类切片**

| 方案要素 | Agno 落点 | 状态 |
|---|---|---|
| 第 1 类 Base Prompt（六模块封闭枚举） | 系统消息按 `system_message` 或 `instructions`/`description`/`expected_output`/`additional_context`/动态字段拼接 | 可表达，非封闭枚举 |
| 第 2 类内置工具 schema | `tools` 列表序列化进请求 | 有 |
| 第 3 类自定义工具（直传/目录化） | 函数、Toolkit、FileSystem 直传；Context Provider 为来源级紧凑接口 | 部分（无 schema 目录 + 元工具） |
| 第 4 类技能索引 | `Skills.get_system_prompt_snippet()` 进系统提示 | 有，且为渐进披露 |
| 第 5 类注入内容 | 记忆、摘要、知识、依赖、状态、Context Provider；**均落在 system 侧** | 有通道；落位与方案不同（方案要求消息侧），无快照与时效声明 |
| 第 6–10 类消息 | 按 run 存于 `RunOutput.messages`；推理块在 `Message.reasoning_content` / `redacted_reasoning_content` | 有形态；无类型化条目 |

**公理**

| 公理 | 现状 | 判定 |
|---|---|---|
| 1 存储只追加 | `run` 按 `run_id` upsert；压缩是新增 `agno_compactions` 记录，原文归档；卸载载荷存 AgentFS 并有 `agno_tool_results` 索引 | 部分：被移出视窗的内容可解析，但无封闭条目类型，run 级 upsert 不是条目流 |
| 2 改动只有四类 | 追加=新 run；剔除=`num_history_messages`/`max_tool_calls_from_history`/`store_history_messages`；替换=卸载信封、`compressed_content`；压缩=`CompactionRecord` + 锚点 | 部分：四类各有实现，无统一声明与记录 |
| 3 循环不可分 | `compaction/_cut.choose_boundary` 按用户轮边界回退，不落在 tool-call 与 tool-result 之间 | 符合 |
| 4 有界性 | 溢出重折一次、摘要失败返回 `SUMMARY_FAILED`、`min_fold_ratio` 拒绝亏本折叠；无互斥锁、无冷却、无崩溃孤儿识别 | 部分 |
| 5 供应商中立 | 供应商差异收敛于 `models/` | 符合 |
| 6 用户消息参与 | `num_history_messages` 可裁历史消息；最后用户轮自然保留 | 部分 |
| 7 成本三因子 | 有 usage 与 cost、有缓存字段；无“体积 × 驻留轮数”积分与归因 | 不符 |

**定理**

| 定理 | 现状 | 判定 |
|---|---|---|
| 1 双旋钮预检 | `compact_at_tokens`（溢出阈值）+ `on_context_overflow`（兜底）；无驻留预算 | 部分：单旋钮 |
| 2 尾部保留 | `uncompacted_runs` 与 `uncompacted_tokens` 互斥；尾部体积上限 `min(阈值/(1+ratio), (阈值−overhead)/2)`；切点按用户轮回退 | 部分：体积上限从溢出阈值导出，非独立驻留预算；回退后不另生成前缀摘要 |
| 3 摘要规范 | 独立摘要模型、`compacted_token_budget` 默认截尾、投影时 tool 结果按 `MAX_ARCHIVED_TOOL_RESULT_CHARS` 截断、摘要吸收前序摘要（锚点链） | 部分：超预算截尾而非失败；净收益用静态比率 |
| 4 成本阶梯淘汰 | 能力分散：卸载（替换）、压缩、`max_tool_calls_from_history`（剪裁）、`send_media_to_model`/`store_media`（丢图片） | 不符：无阶梯与顺序 |
| 5 剔除优先级 | 无 | 不符 |
| 6 混合计量 | `count_tokens` 以投影为对象、usage 为锚；`token_counter`/`use_model_token_count` 可选口径 | 部分：无内容类型分档与来源标注 |
| 7 索引常驻正文按需 | Skills 即“摘要常驻 + 正文按需” | 符合 |
| 8 入口硬上限 | 卸载阈值 + 首尾预览 + `read_result`/`search_result` 回读 + 配额 | 符合 |
| 9 驻留降级 | 无；卸载只作用于产出端 | 不符 |
| 10 缓存敏感提交 | `enforce_min_fold_ratio`/`min_fold_ratio` 是静态净收益替身；无剩余轮数与缓存折扣；同一轮内的多个动作无合并提交概念 | 部分 |

## 五、Boundary

**原生能力**

- 输入校验：Guardrails（PII、提示注入、OpenAI 审核，另有 `BaseGuardrail` 扩展点），实现为 pre-hook，在会话加载与依赖解析之后、上下文构建之前执行；不检查历史、依赖、检索文档与附件。
- 输出校验：post-hook（`OutputCheckError`），流式场景无法撤回已发出内容。
- 人工放行：`requires_confirmation`/`requires_user_input`/`external_execution`，运行暂停，`active_requirements` 逐条确认后 `continue_run`。
- 配额：`tool_call_limit`、模型与整轮重试、`reasoning_max_steps`、Team `max_iterations`、沙箱 `execution_timeout`。
- 沙箱：`sandbox/` 定义按会话拥有的计算契约（create / exec / pause / resume / stop），后端含 docker 与 agentcore，checkpoint 可存 postgres 或 s3；归属与队列由 AgentOS 负责。
- 路径安全：`safe_join_relative_path`、`PathSecurityError`（文件类工具内部）。

**出处**

- 源码：`hooks/`、`guardrails/`、`approval/`、`sandbox/`、`utils/path_safety.py`

**覆盖判断**

授权（人工放行）、输入校验、部分配额、计算隔离**有机制**；统一策略源**没有**。

**缺口**

- **无统一策略源**：授权、校验、配额、可见性分散在 `pre_hooks`/`post_hooks`/`tool_hooks`/构造参数/HITL 标记五处。
- **沙箱不是策略**：`sandbox/` 提供执行后端，不表达“某能力对某主体不可见”或“某路径可写”。
- **无权限驱动的工具可见性**：`tools` 是构造期静态列表或按用户/会话的工厂。
- **无总成本上限**：配额只到调用次数、重试次数与单命令超时。

## 六、运维与扩展

| 扩展能力 | 原生 | 出处 |
|---|---|---|
| 持久化 | 15 种数据库后端（sqlite/postgres/mysql/mongo/redis/dynamo/firestore/clickhouse/singlestore/surrealdb/valkey/gcs_json/json/in_memory/async_postgres） | `/database/overview` |
| 文件系统 | `fs.FileSystem`（数据库后端的 AgentFS，带命名空间与配额） | `fs/` |
| 多 Agent | Team（四种协作模式、嵌套、可调用工厂） | `/teams/overview` |
| 控制流 | Workflow（顺序、并行、循环、条件、路由、嵌套） | `/workflows/overview` |
| 运行时 | AgentOS：REST/SSE/WebSocket、MCP/A2A/AG-UI、RBAC、后台任务、调度、审批、组件版本、沙箱归属 | `os/` |
| 可观测 | OpenTelemetry 自动埋点 | `/observability/overview` |
| 评估 | Accuracy、Agent-as-Judge、Reliability、Performance、Eval Suite | `eval/` |
| 技能 | Skills 包（SKILL.md） | `skills/` |
| 学习 | LearningMachine + Curator | `learn/` |
| 组件注册 | Registry（按版本发布与解析 Agent/Team） | `registry/` |

## 七、三处架构摩擦

**1. 没有“每次模型请求前”的接缝。**
钩子只有 run 前后与工具前后；上下文在 `get_run_messages`（每次 run 一次）拼装后交给 `Model.response`，工具循环与逐请求调用都在 Model 内部。降落在“一批工具结果之后、下次请求之前”的只有内部 `after_tool_results` 关键字参数，且被 `checkpoint="tool-batch"` 占用。双旋钮预检与提交判定只能通过三种方式落地：自定义 `Model` 子类（治理渗进 Provider 层）、修改 Agent 内部实现（跟随上游成本高）、在 Agent 之外自建循环。

**2. 状态模型方向相反。**
Agno 的会话是 `runs` 列表（每 run 带完整消息）+ 单个 `summary`，按 run 粒度 upsert。压缩以独立记录表 + 归档补上了“原文可解析”，卸载又补上了“大载荷可回读”，但两者都是机制级补齐：没有封闭条目类型，没有剔除/替换的声明记录，视窗也不是从条目流逐轮重建，而是从 runs 选取后套用压缩记录。

**3. 记忆与治理方案的语义冲突。**
`MemoryManager.optimize_memories(strategy=SUMMARIZE, apply=True)` 先 `clear_user_memories` 再重写，`Curator.prune`/`deduplicate` 同样删除条目；`UserMemory` 与 `learn` 各类条目均无 `kind`/`revision`/`status`/`source`，也没有期望版本冲突检测。这与“覆盖以新条目声明表达、旧条目留史不删”直接相反。

## 八、采纳建议

- **直接沿用**：Provider（含重试、回退、计量与缓存字段）、Tools（含 MCP、Context Provider、FileSystem、结果卸载）、AgentLoop 主体、持久化、Team/Workflow、AgentOS、可观测、评估、Skills、沙箱契约。
- **配置面即可承接**：定理 2（尾部与体积上限）、定理 3（摘要预算与归档）、定理 8（入口硬上限）、定理 10 的静态近似（`enforce_min_fold_ratio`）、公理 1 的“原文可解析”。
- **需要外层承接**：驻留预算与成本积分归因、阶梯淘汰编排、剔除优先级、驻留降级、净收益公式与合并提交、per-request 预检点、条目流数据模型、Boundary 统一策略源。
- **记忆不沿用**：`MemoryManager` 与 `LearningMachine` 都不能直接承载治理方案 2.3——缺条目语义字段与版本冲突语义，且维护操作破坏留史。
- **待验证**：① 各供应商是否上报缓存字段与 cost；② 上下文窗口与单价的元数据来源；③ 自定义 `Model` 子类在 3.1.12 的稳定性承诺；④ 卸载信封在流式渲染下的可读性。

## 九、证据索引

**官方文档**

- `https://docs.agno.com/sdk/introduction`
- `https://docs.agno.com/models/overview`、`/models/fallback-models`
- `https://docs.agno.com/tools/overview`、`/tools/mcp/overview`
- `https://docs.agno.com/agents/overview`、`/teams/overview`、`/workflows/overview`
- `https://docs.agno.com/sessions/overview`、`/sessions/session-summaries`、`/history/overview`
- `https://docs.agno.com/compression/overview`、`/compression/token-counting`
- `https://docs.agno.com/memory/overview`、`/knowledge/overview`、`/context-providers/overview`
- `https://docs.agno.com/skills/overview`、`/learning/overview`
- `https://docs.agno.com/hooks/overview`、`/guardrails/overview`、`/hitl/overview`
- `https://docs.agno.com/state/overview`、`/dependencies/overview`
- `https://docs.agno.com/agent-os/introduction`、`/observability/overview`、`/evals/overview`
- `https://docs.agno.com/database/overview`

**本机源码（相对 `site-packages/agno/`）**

- `agent/agent.py`：Agent 构造参数（上下文、记忆、压缩、卸载、状态、钩子、checkpoint、工具上限）
- `agent/_run.py`：主循环、预/后钩子位置、`after_tool_results` 构造、事件
- `agent/_messages.py`：系统消息拼装、历史选择、压缩应用与溢出重折
- `agent/_tools.py`：压缩归档工具、卸载回读工具的挂载
- `compaction/compaction.py`：阈值、尾部上限、最小折叠比、摘要预算
- `compaction/archive.py`、`compaction/types.py`：归档与 `CompactionRecord`
- `offload/store.py`、`offload/setup.py`、`offload/tools.py`：卸载阈值、信封、回读、配额
- `models/base.py`：工具循环与 `after_tool_results` 调用点
- `memory/manager.py`、`learn/`、`learn/curate.py`：记忆与学习、维护
- `session/agent.py`：`AgentSession`（`runs` + `summary`）
- `hooks/`、`guardrails/`、`approval/`、`sandbox/`：钩子、校验、HITL、沙箱
- `fs/`：AgentFS
- `run/agent.py`、`metrics.py`：事件与计量字段
