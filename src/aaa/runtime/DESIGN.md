# runtime — 模块设计

## 边界与目标

装配主 Agent、配置进程日志；不介入 Agno 执行路径。启动与重建按当前模型、权限和记忆引擎装配，新建与续聊只选择会话。

## 拥有者与不变量

- `build_agent` 是主 Agent 的唯一装配入口。
- 切换模型或权限模式重建执行体，不原地改模型或 fallback 配置。
- 持久化固定为 `~/.aaa/data/sessions.db`（`SqliteDb`）；`metadata={"cwd": …}` 写进会话记录，是 `-c` 的目录归属依据。
- 压缩参数全部来自 `context`（其值来自 `config.toml`）：阈值 `compact_threshold(window, settings)`、`uncompacted_runs=history_runs(settings)`、预算 `compaction_budget(settings)`，并开启压缩消息存储/搜索与溢出即压缩。
- `tools_for_mode` 将 `boundary` 声明的能力集合、根、启动目录与确认名单交给 `tools.assemble`，并传入配置中的数值上限。
- `runtime/prompts/` 维护内置中文指令，结构参考 Pi：基础指令（身份与工作方式）、当前目录、权限模式说明、按工具裁剪的工具指引（文件与检索、shell、PowerShell、后台任务、任务清单、委派）。构造时，非空 `~/.aaa/SYSTEM.md` 替换基础指令，`~/.aaa/APPEND_SYSTEM.md` 追加用户说明；缺失或空文件不覆盖，不可读时记警告。工具指引只在对应工具实际装配时追加，段内以工具名开头的条目也按工具裁剪。装配顺序固定为：基础指令 → 用户追加 → 项目指令 → 当前目录 → 权限模式说明 → `<permission_mode>` → shell → PowerShell → 文件与检索 → 后台任务 → 任务清单 → 委派。文本传给 Agno `instructions`，不接管系统消息或授权。
- 技能由 Agno 原生 `agno.skills.Skills` 装配（`plugins.build_skills`）：loader 按 builtin → user → project 传入（Agno 后到覆盖，等效先到先得的优先级 project > user > builtin），`validate=False` 保持宽容发现；`<skills_system>` 索引与 `get_skill_instructions`/`get_skill_reference`/`get_skill_script` 三工具由 `Agent(skills=…)` 原生注入，aaa 不自建读取工具。脚本执行属模型可用能力（owner 已接受）。来源标签（project/user/builtin）由宿主按目录先到先得单独记录，供 `/skills` 与启动摘要使用。
- 主 Agent 装配 `list_delegations`、`register_delegation`、`delegate_task`、`save_delegation`。宿主注册表跨同一 CLI 实例的 Agent 重建复用；临时定义按原生 run 的用户与会话隔离，保存只接受本轮 HITL 已确认的原始定义。
- Agent 只使用应用传入的生效来源：内置保留 Agno 原生 `MemoryManager`（同一 model/db）；外置只借用宿主召回与观察回调，不创建原生 manager；关闭两者都不用。装配不发现、不创建外置引擎。
- 不启用 knowledge 与治理策略；`add_history_to_context` 与 `num_history_runs` 负责把同会话历史带进下一轮。
- `fallback_models` 为空时传 `None`，不传空列表。

## 对外接口

- `build_agent(..., skills=None, memory=None, delegation=None, delegation_models=()) -> Agent`：CLI 提供扩展、技能束、记忆引擎、委派注册表与可选模型范围。
- `tools_for_mode(mode, settings, policy=resolve_policy) -> list`：`policy` 是隔离/能力声明的替换点。
- `setup_logging(settings)`：为 `agno` 与 `aaa` 配置同一个文件 handler，移除终端 handler 并关闭传播，保证非交互 JSON 输出为纯 JSONL；重复调用不重复挂载；级别取 `log.level`，`AAA_LOG_LEVEL` 可临时覆盖。
- 日志轮转与库告警阈值来自 `log.max_bytes` / `log.db_warn_bytes`；`sessions.db` 超限只告警，不清理。


