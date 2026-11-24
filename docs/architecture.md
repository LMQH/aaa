# 总架构 — aaa Harness

定位：运行在终端里的 agent harness，底层是 Agno 3.1.12。本文维护系统结构、模块关系与运行期契约；项目要求、核心红线与固定流程见 `AGENTS.md`，用法见 `README.md` 与 `docs/commands.md`，单个模块的机制见各模块 `DESIGN.md`。

**主 Agent 是 aaa 的执行基座。** CLI 启动时装配主 Agent；新建或续聊只选择会话，不切换执行体类型。交互和非交互请求都由 Agno 原生 `Agent.run()` 执行，审批暂停后用原生 `Agent.continue_run()` 续跑。主模型在自己的循环里决定何时调用工具、何时答复用户，CLI 不接管模型决策。

委派是主 Agent 的内置工具调用，不切换主会话执行体：[委派机制](delegation-design.md)在线程中调用 Agno 原生 Agent、Team 或 Workflow。

## 一、五层职责与 Agno 的边界

| 层 | 由谁实现 | 本项目的落点 |
|---|---|---|
| Provider | Agno `models/` | 只提供渠道与模型元数据（`context_window`、`api` 协议名）；`model` 模块构造实例 |
| Tools | Agno + 本项目工具 | `tools` 模块实现工具本体；`boundary` 决定哪些工具可见 |
| AgentLoop | Agno `Agent` | 主 Agent 负责对话与按需调用工具；aaa 不另建循环 |
| Context | Agno compaction / offload | `context` 持有主会话的历史轮数与压缩参数；`memory` 提供记忆引擎 |
| Boundary | Agno HITL / hooks | `boundary` 声明确认规则（哪些工具需确认、审批策略、危险命令规则、什么算同一次批准）；`cli` 执行询问、签名记忆与非交互拒绝 |
| Usage | 本项目账本 | `usage` 只读 Agno 的 metrics，把 `(渠道, 模型)` 的 token 与金额写进独立 `usage.db`；不介入任何执行点 |

Agno 拥有全部**执行点**：消息拼装、工具循环、压缩触发、会话写入。本项目只在**装配点**（构造参数、工具、run 级钩子、事件）与**数据**（数据库、文件系统）上扩展。具体禁止：不覆写 `Model.response` / `response_stream`，不修改 `Agent` 内部实现，不自建循环；装配点表达不了的能力宁可不做，不侵入执行点。

**扩展协议（计划，边界已定）**：扩展只挂两类位置——Agno 的公开装配点（工具、运行前后钩子、工具中间件、事件流、提示词与会话数据接口）和 aaa 自有层（CLI、配置、`boundary.PolicySource`、`model.API_CLASSES`、插件发现）。不开放改写 Agno 执行路径的扩展：两次模型请求之间没有注入点，运行中改上下文、换压缩算法、同一会话内切换路径都不做。协议归 aaa：扩展只看到 aaa 的接口，由 aaa 翻译到装配点，Agno 升级只改这一层。扩展注册的工具必须声明能力名与确认规则，才进入 `boundary` 的权限模型。最小稳定面与实施顺序见 `HANDOFF.md`。

**成本治理只在运行之外**：`usage` 只做 run 结束后的用量归因；金额入账时冻结，改价不改历史。不做在线预算、阶梯降级与条目流。

**运行底座与 CLI 分别设计**：底座深度使用 Agno 原生能力，内核极简、职责解耦、扩展在装配边界完成；CLI 面向用户体验，交互逻辑简单明确、视觉呈现丰富精美。CLI 的主屏内联呈现不改变 Agno 执行所有权，也不因内核极简而退化为纯文本。owner 决议原则，实现者在原则内选机制并反馈；新原则与跨模块取舍先获批准。CLI 当前机制与不变量见 [`src/aaa/cli/DESIGN.md`](../src/aaa/cli/DESIGN.md)，验证证据与未覆盖范围见 `HANDOFF.md`。

## 二、模块划分

```text
src/aaa/
├── config/      # secret.yaml（渠道/密钥）+ config.toml（可调设置）：IO、归一、探查
├── model/       # 模型身份与构造：provider 值对象 + catalog 解析
├── runtime/     # Agent 装配与进程日志
├── context/     # 上下文参数（压缩阈值、保留尾轮数、摘要预算）
├── boundary/    # 权限策略：模式→能力清单、确认规则、文件根（不装配工具）
├── tools/       # 工具域与装配：builtin/（文件、检索、进程、待办）、extensions/（天气）
├── memory/      # 外置记忆协议、插件发现、召回/观察/工具；内置记忆只读投影
├── usage/       # 用量与花费：读 Agno metrics，写独立账本 usage.db
├── sessions/    # 会话查询、命名、标题、分叉、目录→会话索引
└── cli/         # 入口、参数、命令、状态、渲染、事件消费

src/miniviking/  # 随 aaa 分发的外置记忆引擎：文件树事实源 + SQLite 派生索引 + LLM 蒸馏
```

依赖方向单向，下层不 import 上层：

```text
cli     → runtime, model, boundary, config, context, sessions, usage, memory
runtime → model, boundary, config, context, tools, memory
sessions→ model, config
usage   → config, model
config  → model, boundary
memory  → config.paths
model / context / boundary / tools → 本包内无依赖
todo_contract → 本包内无依赖（TODO 条目契约；tools 与 cli 共用）
```

`boundary` 与 `tools` 互不 import：前者声明能力和隔离，后者按能力名装配；两者由 `runtime.tools_for_mode` 合并。`memory` 定义外置记忆协议、发现与召回/观察宿主。应用启动时选定记忆引擎，外置实例由应用持有，执行体装配和 `/memory` 复用，不向插件传入 Agno 对象。`miniviking` 是与 `aaa` 并列、同一 wheel 分发的外置引擎：只 import `aaa.memory` 的公开协议，`aaa` 核心不 import 它；经 `aaa.memory_engines` 入口点接入。`usage` 只读 Agno db 与 metrics、只写独立 `usage.db`，不参与执行路径。

模型构造入口是 `model.catalog.build`；主 Agent 构造入口是 `runtime.agent.build_agent`。主 Agent 接入当前记忆引擎：内置使用 Agno 原生 `MemoryManager`，外置不启用内置记忆。默认关闭；外置加载失败也以关闭启动。

**单包、包内分层是既定形态。** `aaa` 是一个包（记忆引擎 `miniviking` 因插件契约独立成包），上文模块分层与依赖单向就是本项目的解耦方式，不拆分多包。Pi 的 `pi-ai` / `pi-agent-core` 等包边界是为独立发布与外部 SDK 消费者服务的，aaa 暂无此类需求。仅当出现对外 SDK、独立发布节奏或多团队协作时，才沿已验证的接缝（记忆引擎先例）考虑拆包。

## 三、运行期契约

**配置分层**

| 文件 | 内容 | 写入方 | 权限 |
|---|---|---|---|
| `~/.aaa/secret.yaml` | 渠道、密钥、默认模型、可切换范围、模型价目（`cost`，可选） | 程序（`/provide` 向导），原子写入 | 0600 |
| `~/.aaa/config.toml` | 渠道凭据之外的可调项 | `/config`；记忆引擎仅经 `/memory` 菜单保存，重启生效 | 用户不手改 |

越界、类型错误与未知键：回落默认值并在 `Settings.warnings` 里记录，由 `cli` 提示，不静默采用。

**错误与退出码**

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 运行失败（模型/工具/未捕获异常） |
| 2 | 用法或配置问题（含损坏的 `secret.yaml`：报错退出且不覆盖原文件） |
| 130 | 被 Ctrl-C 取消 |

工具失败一律变成 `Error: …` 文本回填给模型，不抛穿进程；非交互模式下需确认的工具默认拒绝并写日志，`--allow-shell` 显式放行 `run_shell_command`。`boundary.command_approval`（`always` / `dangerous`）控制命令审批询问，不改变工具可见性。外置记忆引擎自行声明哪些工具需要确认，不由命令审批策略推断。

**隔离能力**

文件工具与 `grep` 受工作区根约束，并共用同一套排除路径（环境与密钥文件、依赖与构建目录、`.git` 等）；相对路径按启动目录解析。`shell` 的工作目录是启动目录，只有人工审批，没有路径约束。

**数据位置与生命周期**

| 路径 | 内容 | 生命周期 |
|---|---|---|
| `~/.aaa/data/sessions.db` | 会话、消息、`session_state`、内置记忆、目录索引 | 超过 `log.db_warn_bytes` 只告警，不自动清理 |
| `~/.aaa/memory/<id>/` | 外置引擎的数据目录（根目录可配置） | 引擎自管；切换不迁移 |
| `~/.aaa/data/usage.db` | 用量账本：终态 run × 模型角色的 token 与冻结金额 | 派生数据，可删除重建 |
| `~/.aaa/logs/aaa.log` | 运行日志 | 超过 `log.max_bytes` 轮转，保留 `.1`、`.2` |
| `~/.aaa/history` | 输入历史 | 由 prompt-toolkit 维护 |

**验证入口**

```bash
./tests/run.sh                       # 模块回归，零网络
./tests/run.sh e2e [--pty]           # 真实渠道闭环（P0 + P1，`--pty` 追加交互面）
```

`tests/README.md` 维护用例清单与“已知测不了”；验证记录方式见 `AGENTS.md`。

## 四、依赖与可复现性

- `pyproject.toml` 声明直接依赖与 Python 版本范围，`uv.lock` 记录完整解析；`[tool.uv] constraint-dependencies` 约束传递依赖基线。安装与升级流程见 `AGENTS.md`。
- Python 下限由 `import tomllib` 决定；上限表示未验证版本不被自动选中，实测基线为 3.13.12。
- 锁保证解析结果与已验证环境逐包一致，不保证跨平台行为已验证；Windows 的 PowerShell 与后台任务分支仍未实跑，见 `tests/README.md`。

## 五、开发准则

- **文档只写当前结论，定期清理“已否决、决策记录、修改历史”残留。** 总架构、模块 `DESIGN.md` 与使用说明在每次收口和发布前检查；历史由版本控制保留，不进入正文。

## 六、待定

- 记忆引擎生态：巩固、导出导入、异步队列、参考引擎与多租户尚未实现。
- 沙箱（OS 级隔离）：本期不做；接入点是 `boundary.Isolation` 的声明与可替换的 `PolicySource`。
- **数据保留策略**：会话库只增不减；清理方式（TTL、按大小、手动命令）未定。后台任务状态在进程内，不涉及磁盘保留。
- **错误模型统一**：工具用 `Error:` 文本、配置用 `ConfigError` 异常、其余走退出码；是否需要一份错误分类约定未定。
- **配置键增改流程**：新增可调项是否必须先在 `config/DESIGN.md` 记录并评审，未定。
