# boundary — 模块设计

## 边界与目标

权限与边界声明。把「模型能做什么」表达成能力名集合，把「隔离到哪」表达成 `Isolation(file_root, command_cwd)`，再交给 `runtime.tools_for_mode` 合并、`tools.assemble` 装配。审批的**规则**在本模块（哪些工具需确认、什么算同一次批准），审批的**交互**在 `cli`。本模块不装配工具、不读配置、不认识具体实现。

## 拥有者与不变量

- 模式是封闭三档：`read-only`（默认）、`workspace-write`、`danger-full-access`；`MODES` 是唯一来源，`cli/args` 的 `--permission` 与 `/mode` 都从它取值。
- 能力名（`read` / `list` / `grep` / `write` / `edit` / `move` / `delete` / `shell` / `powershell`）的词汇与实现归 `tools`；本模块只做「模式 → 能力」授权。
- `read-only`：`read`/`list`/`grep`，没有写与命令。
- `workspace-write`：再加 `write`/`edit`/`move`/`shell`；shell 每次执行需确认。
- `danger-full-access`：再加 `delete`/`powershell`；shell 不需确认，文件根为文件系统锚点（POSIX 的 `/`、Windows 的盘符根）。
- 文件根：前两档是 cwd，`danger-full-access` 是文件系统锚点。
- 命令工作目录与相对路径的解析起点三档都是启动目录（`Isolation.command_cwd`，由 `runtime.tools_for_mode` 交给工具装配）；根只决定文件工具能触达的范围。
- 默认模式来自配置 `boundary.default_mode`（`restart` 生效）；模块内不保留默认模式常量。
- 提示词描述的能力由 `tool_names()` 从实际装配结果展开，不手写工具清单。

## 对外接口

- `resolve_policy(mode, base=None) -> ModePolicy`：完整声明（能力、确认名单、隔离范围），是默认实现。
- `PolicySource = Callable[[str, Path], ModePolicy]`：替换点；沙箱或插件给出自己的实现，工具与运行时都不用改。
- `Isolation(file_root, command_cwd)`：只描述隔离范围，不描述执行方式。
- `capabilities(mode)`、`confirm_tools(mode)`、`root(mode)`、`isolation(mode)`：薄访问器。
- `signature(tool_name, args) -> str`：审批签名键（工具名 + 键序无关的参数），决定「什么算同一次批准」。
- `MODES`：模式名单的唯一来源，`config.settings` 的 `boundary.default_mode` 可选项也取自它。
- 调用方是 `runtime.tools_for_mode`：它把能力集合、根、确认工具名与数值上限交给 `tools.assemble.build_tools`。

## 已知例外

使用内置记忆时，Agno 注册 `update_user_memory`，不经过 aaa 的工具能力清单；使用外置或关闭时不注册该工具。外置记忆插件为受信任的进程内代码；其声明的工具按下一段走 aaa 的确认路径，不注册 Agno 原生记忆工具。

外置引擎声明哪些工具需要确认，不由全局权限清单或命令审批策略推断。显式请求确认的工具经 Agno HITL 进入现有确认流程，可复用已批准签名；非交互模式默认拒绝。不承诺沙箱。


