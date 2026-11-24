# extensions — 扩展协议

扩展协议把「插件」收敛到一个 aaa 自有的接口后面：扩展只见 `ExtensionContext`，
aaa 负责翻译到装配点（Agno 公开接口 + aaa 自有层）。Agno 升级只改翻译层。
边界与红线见 [`docs/architecture.md`](../../docs/architecture.md)「扩展协议」节：
只挂装配点，不触执行路径；注册工具必须声明能力名与确认规则，经 `boundary` 进入权限模型。

## 状态

五个阶段全部实现：
- 阶段 1（协议内核）：编程式扩展（工具 + 事件）、发现/加载/失败隔离、按模式装配。
- 阶段 2（存量归一）：`plugins.discover_extensions` 统一发现入口，命令/技能/MCP/编程扩展/动画目录一次扫描并做信任门控；`AppState.start` 只消费发现结果。
- 阶段 3（提示词块）：`register_prompt_block`（`system_tail`）→ `registry.render_blocks(mode, cap)` 渲染 `<extension_block>` 包裹块，单块字符上限来自 `project.instructions_max_chars`（0 关闭），计入 `/insight` 基础提示词段。
- 阶段 4（面板）：`register_panel`（面板名全局唯一，重复即加载失败）+ `/panel` 命令（列出/打开/off）+ 状态栏插槽（活动面板渲染至多 8 行，渲染异常降级为提示行）。
- 阶段 5（工具调用前钩子）：`intercept_tool` 守门拦截器，挂 Agno 原生 `tool_hooks`（装配点）；扩展从「看客」变为「管家」。

## 组成

- `protocol.py` — 扩展可见的全部接口：`ExtensionContext`（工具/事件/提示词块/面板/工具拦截挂载）、`Event`、冻结的事件目录（9 个）、`PanelSpec`/`PromptBlockSpec`/`RenderInfo`/`ToolSpec`/`ToolCall`/`ToolDecision`、`CURRENT_PROTOCOL_VERSION`。
- `bus.py` — 事件总线：同步派发、异常隔离；同一扩展单会话累计 3 次 handler 异常 → 会话内挂起。发射永不向主循环抛异常。
- `loader.py` — 发现（`extension.toml [extension] kind="python"`，同名先到先得）、导入（`extension.py` 的 `activate(ctx)`，`PROTOCOL_VERSION` 不得高于 aaa）、注册表（`tools_for(capabilities, mode)` 按模式装配并落确认规则；`render_blocks(mode, cap)`；`activate_panel`；`interceptors`）、`tool_guard`（守门钩子）、失败隔离（加载失败只禁用该扩展）。
- `plugins.discover_extensions` — 五类扩展的统一发现入口（aaa 翻译层）：一次扫描 + 信任门控，宿主只消费结果。

## 关键约定

- **工具命名与信任面**：注册工具名固定 `ext_<扩展名>_<工具名>`；`ext_` 前缀与 `mcp_` 同待遇（`boundary.is_external_tool`）——默认每次询问、审批策略不自动放行；用户可在审批时选「本会话总是允许」按工具名放行（会话级、不落盘）。
- **能力映射**：`capability` 填 boundary 既有能力名时跟随该能力的模式存在性；自定义能力默认 `workspace-write` 以上可得，`read_safe=True` 才全模式可得。
- **确认规则**：`always`/`dangerous` → `requires_confirmation=True`；`mode` → 跟随当前模式的确认名单（read-only 与 danger-full-access 名单为空则不问，workspace-write 询问）。`dangerous` v1 等同 `always`（通用 dangerous 策略后续接入时再区分）。
- **生效方式**：启动时发现、重启生效；`.disabled` 停用；`/plugin` 启停不变。
- **信任**：项目级编程扩展属于既有 `extensions` 信任类别（指纹含 `extension.py`），未信任不加载；用户级 `~/.aaa/extensions` 不问信任。
- **失败语义**：加载失败 → 该扩展禁用 + 启动告警；事件 handler 异常 → 跳过并计数，3 次 → 会话内挂起；均不影响主 Agent 与其他扩展。
- **事件语义（v1）**：`run.started`/`message.user` 在轮前派发（run 前钩子）；`tool.*`/`message.assistant`/`run.completed` 在轮末统一回放（run 后钩子扫描本轮产物）；`run.failed` 由 CLI 运行路径派发。自定义事件走 `ext/<扩展名>/<事件>`。
- **工具调用前拦截（阶段 5）**：`intercept_tool(handler)` 在每次工具执行前收到 `ToolCall`，返回 `None`/`True`/`ToolDecision.allow()` 放行，`False`/`ToolDecision.deny(理由)` 拒绝——拒绝以说明作为工具结果返回给模型。**只能收紧**：不能放行需要确认的调用，不支持改参（Agno 的确认暂停在钩子之前，改参会绕过用户已确认的内容）。异常或超时（`extensions.hook_timeout`，默认 5 秒）视为拒绝并写日志；同一扩展累计 3 次失败后会话内挂起，其拦截器随之跳过。钩子只覆盖主 Agent，委派子 Agent 的工具调用不经拦截（子 Agent 本就不执行需确认的动作）。
- **审批第三档**：交互审批提供「批准 / 本会话总是允许 / 拒绝」；「总是允许」按工具名在当前会话放行，文件改动类工具不提供（每次改动都要过目）。
- **不开放**：扩展不接触 Agent 实例、不注入两次模型请求之间、不改运行中上下文与压缩；`ExtensionContext` 是能力收窄的唯一句柄。

## 对外接口

- `extensions.load_extensions(discover_python_extensions(dirs), registry)` — 宿主装配用。
- `registry.tools_for(capabilities, mode)` / `runtime.skills_for_mode` 同层消费；`runtime.agent.build_agent(extensions=registry)` 接线。
- 记忆引擎不进本协议（宿主级引擎，宪法记忆来源互斥）；MCP 插件维持 `extension.toml [mcp]` 通道，归一在阶段 2。
