# memory — 记忆

## 来源与所有权

- 记忆默认关闭。内置、外置、关闭三态互斥；`/memory` 管理选择，重启才生效。已保存选择与本进程生效来源分开；重建 Agent 不重新选择。
- 内置直接装配 Agno 原生 `MemoryManager`，不进插件注册表或外置协议。保留原生存储、注入和工具语义。
- 外置由应用在启动时构造一次，并在退出时调用可选的 `close()`；执行体和 `/memory` 复用该实例。Agno 内置记忆关闭。外置加载失败时提示并以关闭状态运行，不覆盖保存的选择，不回退到内置。
- 关闭时不构造记忆，不浏览旧数据。切换不搬、不删数据。

## 外置接入

- 本地文件 `~/.aaa/memory_engines/<id>.py` 导出 `ENGINE`，或安装包声明 `aaa.memory_engines` entry point。同名时本地优先，发现阶段不执行插件代码。
- 工厂接收一个 `Path`：该引擎的数据目录（`memory.data_root/<id>`）。引擎自己管理配置、模型、凭据、存储与后台任务；不传 Agent、Agno 数据库或模型。插件为受信任的进程内代码，不是沙箱。
- `recall(RecallQuery)` 返回 `MemoryEntry` 序列。可选 `observe(Observation)` 接收本轮问答，`read(identity, *, limit, page=1)` 提供分页，`close()` 释放资源。调用为同步约定；引擎自己的后台处理及崩溃恢复由引擎负责。

### 身份

- 召回与观察载荷的 `identity`、工具调用与浏览的身份参数均为 `MemoryIdentity`。aaa 提供 `user_id`、稳定的逻辑 `agent_id`（当前为 `aaa-main`）、当前 `session_id` 与 `run_id`。没有当前会话或运行时对应字段为 `None`；`/memory` 不借用历史或正在进行的 run id。引擎消费身份，自定存取维度，不从模型传入的工具参数推断身份。
- CLI 正常运行和续跑使用与 `/memory` 相同的配置用户（空值按 `default`），不让续跑回退到历史用户。`/edit`、`/redo` 走 aaa 的续跑入口，以 Agno 原生事件中的新 `run_id` 为准；直接调用 Agno `continue_run()` 不经过这层身份接线。

## 工具与提示块

- 引擎可实现 `tools() -> Sequence[MemoryTool]` 声明工具（名称、描述、JSON Schema 参数、`read_only`、`requires_confirmation`），并实现 `call_tool(name, arguments, identity) -> str` 处理调用。不声明就不注册；声明了工具但缺少 `call_tool`、工具重名或名称不合法都在启动期报错，进入"关闭降级"。
- 工具在装配期与现有工具合并；重名直接拒绝启动，不覆盖。工具名与描述由 aaa 写进 system prompt（`<memory_tools>` 块）。
- 调用分发回同一引擎实例；宿主从 Agno 当轮上下文提取身份，不向引擎传 Agent、Agno 对象或提示词模板。
- `read_only` 描述工具是否只读，不决定审批。引擎通过 `requires_confirmation` 决定是否请求确认，缺省为 `False`；请求确认时，aaa 复用现有确认流程与已批准签名。
- 每次调用有 `memory.tool_timeout` 上限，异常一律转成 `Error: <类型>` 文本返回，不打断执行循环；超大结果沿用 Agno 现有的工具结果卸载。
- 引擎可实现 `prompt_block() -> str` 提供静态使用说明，宿主追加到 system prompt；缺失或失败都不追加，引擎不能改宿主模板。

## 执行与界面

- aaa 用公开的 Agno hooks 和 instructions 向主 Agent 注入外置召回。正常轮次注入 system prompt；`/edit` 和 `/redo` 沿用历史快照，不重新召回，也不改用户原文。
- 召回与观察显式携带用户和会话；召回按宿主条数和字符预算截断，异常或超时降级；观察以运行标识关联，失败不回滚会话。守护线程超时只停止等待，不能取消插件代码。`close()` 同样有等待上限；插件须自行停止非守护后台线程。
- `/memory` 空闲时显示当前引擎并弹出操作菜单（切换引擎；外置引擎附「引擎配置」），运行中只显示状态；外置没有 `read` 就不显示条目数。引擎选择只保存，不构造候选插件，不重启。
- 引擎可声明 `config_specs()` / `apply_config(values)`（均为可选方法）：前者列出可调项（键 `section.field`、类型、范围/可选值、当前值、是否密钥），宿主据此渲染 `/memory config` 并校验输入；后者把键值写进引擎自己的配置文件。两者缺失即没有配置面，内置记忆无此协议。
