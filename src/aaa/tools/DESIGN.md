# tools — 模块设计

## 边界与目标

模型可调用的工具本体，按两个域组织：`builtin/`（文件、检索、进程、任务清单）与 `extensions/`（天气）。`assemble.build_tools` 只接受「能力名清单 + 文件根 + 需确认工具名 + 数值上限」，**不 import `boundary`**——权限模式与能力清单的合并由 `runtime.tools_for_mode` 完成。工具失败必须变成可读结果回填给模型，不能把异常抛穿进程。

能力名是本模块与调用方唯一的共享词汇：`read` / `list` / `write` / `edit` / `move` / `delete` / `grep` / `shell` / `powershell`。新增能力就在这里加一个键，不在 `boundary` 里写分支。

## 目录

```text
tools/
├── assemble.py          # 装配：能力名 → 工具列表；tool_names 与平台提示
├── builtin/             # 通用 agent 能力
│   ├── files.py         # 文件与检索（在 Agno Workspace 上加 aaa 的路径与检索语义）
│   ├── processes.py     # 受管进程：前台执行 + 后台 job
│   ├── powershell.py    # PowerShell，条件装配
│   └── todos.py         # 任务清单（session_state）
└── extensions/          # 可替换或示例性工具
    └── weather.py       # 天气
```

## 工具清单（模型可见 15 个）

### 文件与检索（`builtin/files.py`）

| 工具 | 作用 | 约束 |
|---|---|---|
| `read_file` | 读文件，带行号 | 单文件上限 `tools.file_max_lines` 行 / `tools.file_max_length` 字节（默认 2000 / 200KB） |
| `list_files` | 列目录，可按 `pattern` 递归 | 同时承担 `find` 职责；剪枝 `.git`、`node_modules` 等 |
| `write_file` | 新建或整体覆盖 | 父目录自动创建 |
| `edit_file` | 精确文本替换 | 只允许唯一匹配，多处匹配报错 |
| `move_file` | 移动或重命名 | 限定工作区根 |
| `delete_file` | 删除 | 仅 `danger-full-access` 可见 |
| `grep` | 正则检索，带行号 | 与文件工具是同一个 `WorkspaceTools`：共用排除规则，默认从启动目录搜起；Python 正则，不起子进程 |

### 受管进程（`builtin/processes.py`）

| 工具 | 作用 | 约束 |
|---|---|---|
| `run_shell_command(args, tail, background)` | 以 argv 直接执行命令 | 前台 `tools.shell_timeout` 秒超时（默认 120）后杀整个进程组；`background=true` 返回 job id |
| `job_output(job_id, tail)` | 读后台任务状态与末尾输出 | 状态 `running` / `finished` + exit code |
| `job_kill(job_id)` | 终止后台任务 | 向进程组发 `SIGKILL` |

后台状态只在当前进程内（`_jobs` 字典 + 读线程缓冲），轮询制、无守护进程：`job_output` 读有界内存缓冲（默认 1MB，超出丢最旧字节），已结束 job 保留 20 条，进程退出时 `atexit` 杀掉未结束的进程组。**不落盘**——跨 `-p` 调用查不到上一次的 job，要跨进程持久必须改 `docs/architecture.md` 的运行期契约。

### PowerShell（`builtin/powershell.py`）

| 工具 | 作用 | 约束 |
|---|---|---|
| `run_powershell(command, tail)` | 执行单条 PowerShell 命令 | 仅当系统存在 `pwsh`/`powershell` 时装配；`-NoProfile -NonInteractive -Command`；超时与输出上限同 shell |

### 任务清单（`builtin/todos.py`）

| 工具 | 作用 |
|---|---|
| `todo_add(items, phase)` | 追加任务，换行或分号分隔多条；`phase` 指定所属阶段 |
| `todo_list()` | 列出任务与完成状态 |
| `todo_start(index)` | 按 1 基编号标记为进行中；原来的进行中项退回待办 |
| `todo_done(index)` | 按 1 基编号标记完成；越界只提示，不改状态 |

### 天气（`extensions/weather.py`）

| 工具 | 作用 |
|---|---|
| `get_weather(city)` | 查城市当前天气；wttr.in 纯文本接口，`tools.weather_timeout` 秒超时，无 Key |

## 可见性（由 boundary 声明，runtime 合并）

| 模式 | 可见工具 |
|---|---|
| `read-only` | 天气、任务清单 4 个、`read_file`、`list_files`、`grep`（8 个） |
| `workspace-write` | 上一档 + `write_file`、`edit_file`、`move_file`、`run_shell_command`、`job_output`、`job_kill`（14 个） |
| `danger-full-access` | 上一档 + `delete_file`（15 个） |

`job_output` / `job_kill` 与 `run_shell_command` 同属 `shell` 能力，开关联动；天气与任务清单不随模式变化。

## 拥有者与不变量

- `weather`：wttr.in 纯文本接口，无 API Key、无额外依赖；任何异常都返回 `Error: …` 文本。
- `todos`：状态存在 `run_context.session_state["todos"]`，随会话写库；条目形如 `{"text", "done"}`。
- 子进程与信号只出现在 `builtin/processes.py` 与 `builtin/powershell.py` 两个文件；其他工具不允许直接 `subprocess`。`ProcessTools` 继承 Agno `ShellTools`，保持工具名 `run_shell_command` 以使 `requires_confirmation_tools` 生效。
- 前台命令超时、输出上限、默认尾行由 `config.toml` 的 `tools.shell_*` 传入。整树回收两端一致：POSIX 以新 session 建进程组、`killpg(SIGKILL)`；Windows 建独立进程组，超时与停止都用 Job Object（`KILL_ON_JOB_CLOSE`）终止整棵进程树，Job Object 建不起来时退回 `taskkill /T /F`。
- 后台任务两端可用：每个 job 一个独立 Job Object（Windows）或独立进程组（POSIX），`job_kill`、超时与退出时回收都作用于整棵树，不留下孤儿进程。job 状态只活在当前进程，不落盘。
- 子进程输出解码前后台一致：都按区域编码（`locale.getencoding()`）解码，后台按最后字节有界保留。
- 输出超出上限时截断并加 `[output truncated]`；返回前按 `tail` 取末尾行。默认上限高于 Agno 卸载阈值，让超限结果以「信封 + 可回读全文」的形态进上下文。
- 退出码非零时优先返回 stderr，其次 stdout，两者皆空时返回 `Error: exit code N`。
- 后台 job id 必须是 8 位十六进制，直接用于路径拼接前先校验，防穿越。
- 相对路径按启动目录解析，命令也在启动目录执行；根只决定文件工具的边界，三档里只有 `danger-full-access` 的根是文件系统锚点（POSIX 的 `/`、Windows 的盘符根），Windows 上该档接受带盘符的绝对路径与其他盘符。`build_tools(cwd=…)` 把根与启动目录分开传。
- `grep` 与文件工具共用 Workspace 的排除规则与豁免项，跳过符号链接、二进制与超过 1 MB 的文件，单次检索 30 秒封顶。`WorkspaceTools` 依赖 Agno `Workspace._resolve` 与 `_is_excluded`，Agno 升级时由 `tests/test_tools_scope.py` 拦截变化。

## 对外接口

- `weather_tool(timeout_seconds)` 按配置超时返回工具函数（`get_weather` 是它的默认实例）；`todo_add / todo_start / todo_list / todo_done` 依赖 `run_context` 注入。
- `ProcessTools(base_dir, requires_confirmation_tools=[...], …上限)`；`PowerShellTools(requires_confirmation_tools=[...])` 仅在存在可执行文件时由装配点创建。
- `build_tools(capabilities, *, root, cwd=None, confirm_tools=(), file_caps=None, weather_timeout=None, shell_timeout=None, shell_output_bytes=None, shell_tail=None)`：本模块唯一对外装配入口；`cwd` 省略时同 `root`。
- `workspace_tool(root, capabilities, file_caps=None, confirm=(), base=None)`：文件类与检索类工具，`grep` 随 `grep` 能力装配在同一个工具对象上。Workspace 在装配 `edit` 时强制附带一句英文 edit 说明，构造参数覆盖不了，与中文指引并存。
- `tool_names(tools)`：用于判断哪些命令提示适用；提示词资源由 `runtime` 维护。
- 失败语义：所有工具都以文本返回错误，不抛异常——模型据此自行继续或说明。


