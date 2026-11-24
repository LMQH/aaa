# 扩展与自定义

aaa 的开放面分两类：**加文件的插件**与**改配置的自定义**。本文给出全貌、生效时机与信任等级；每类的格式与示例见对应文档。

## 一览

| 类型 | 位置 | 写法 | 生效 | 信任等级 |
|---|---|---|---|---|
| 启动动画 | `~/.aaa/animations/<name>.py` | 模块导出 `ANIMATION` | 下次启动 | 启动即执行的代码 |
| 斜杠命令 | `~/.aaa/commands/<name>.md` | Markdown 提示词模板 | 下次启动 | 纯文本 |
| MCP 扩展 | `~/.aaa/extensions/<name>/extension.toml` | TOML 的 `[mcp]` 段 | 下次启动 | 外部工具，每次执行都审批 |
| 外置记忆引擎 | `~/.aaa/memory_engines/<id>.py` 或 entry point `aaa.memory_engines` | 工厂接收独立数据目录；可声明工具与提示块 | 通过 `/memory` 菜单选择，重启生效 | 受信任的进程内代码 |
| 技能 | `~/.agents/skills/<name>/SKILL.md` | frontmatter + 正文，可含 `references/` 与 `scripts/` | 下次启动 | 文本 + 脚本执行面（见 `skills.md`） |
| 编程式扩展 | `~/.aaa/extensions/<name>/`：`extension.toml` + `extension.py` | `activate(ctx)` 注册工具/事件/拦截/提示词块/面板/命令 | 下次启动 | 进程内代码，需仓库信任（项目级）；见 `extensions.md` |
| 渠道与模型 | `~/.aaa/secret.yaml` | YAML v2 | 重启 | 含密钥，文件权限 0600 |
| 配置项 | `~/.aaa/config.toml` | 经 `/config` 修改 | 逐键不同 | — |
| 项目级扩展 | 仓库根 `.aaa/`：`commands`、`skills`、`extensions`、`animations`、`memory_engines` | 同用户级格式，另加仓库根 `AGENTS.md` | 下次启动 | 文本自动；代码类需仓库信任（`/trust`） |

技能目录在 `~/.agents/skills/`，其余都在 `~/.aaa/` 下。

## 生效时机

文件类扩展在**启动时发现一次**，运行期不重载；改完文件要重启 aaa。

配置项逐键不同：

- `immediate`：写入后，下一次读取该值的地方生效；
- `runtime`：当场重建 Agent；
- `restart`：下次启动生效。

每条的实际档位在修改后由提示给出：立即应用，或显示「该配置在下次启动时生效」。

## 怎么确认被发现了

| 类型 | 验证 |
|---|---|
| 启动动画 | 用安装 aaa 的 Python 执行 `python -m aaa.cli.banner --animation <名> --duration 1`，详见[调试](animations.md#调试) |
| 斜杠命令 | `/help` 或补全菜单出现该命令 |
| MCP 扩展 | `/plugin` 列出，`●` 为启用、`○` 为停用 |
| 记忆引擎 | `/memory` 显示当前引擎并提供切换菜单 |
| 技能 | `/skills` 列出名字与 description |
| 渠道与模型 | `/model` 能看到该渠道下的模型 |
| 配置项 | `/config <键>` 回显当前值 |

## 不开放的部分

以下不在扩展面内，要改只能改代码并走评审：

- **原生工具**：进程内任意代码，与插件不是同一信任等级，agent 不能创建。
- **执行循环**：Agno 拥有执行路径，不覆写 `Model.response` / `response_stream`。
- **运行期换模型**：模型身份是 `(provider, model_id)`，换模型一律重建执行体（Team 或旧会话 Agent）。
- **OS 级沙箱**：文件工具受工作区根约束，shell 只有工作目录与人工审批。

## 信任等级差别

写文件的插件不等价：

- 命令模板与 AGENTS.md 是**纯文本**，只进入提示词，不执行；技能正文同样是文本，但其 `scripts/` 子目录里的脚本可由模型经 `get_skill_script` 执行（见 `skills.md`）；
- MCP 扩展是**声明**，工具来自外部进程，名字加 `mcp_` 前缀，且每次执行都要求确认；
- 编程式扩展是 **Python 代码**，在进程内执行；项目级需仓库信任，注册的工具名字加 `ext_` 前缀并按外部信任面逐次确认；其工具拦截器可拒绝调用（只能收紧）；
- 启动动画是 **Python 代码**，在启动时于本进程执行，没有审批环节；
- 外置记忆引擎是 **Python 代码**，仅在选中后于进程启动时加载，无沙箱；不接管 Agent 循环。
- 渠道与模型含**密钥**，文件权限固定 0600，解析失败时程序报错退出而不是覆盖。

## 各类型文档

- [启动动画](animations.md)
- [自定义斜杠命令](commands.md)
- [MCP 扩展](mcp-extensions.md)
- [技能](skills.md)
- [记忆引擎](memory-engine.md)
- [渠道与模型](models-and-channels.md)
- [配置项](settings.md)
- [项目级扩展](project-extensions.md)
