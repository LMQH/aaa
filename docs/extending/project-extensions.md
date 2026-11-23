# 项目级扩展

把仓库专属的命令、技能与项目指令随仓库提交，任何人克隆后用 `aaa` 打开该仓库即可使用。项目根 = 从当前目录向上找到的第一个含 `.git` 的目录；没有 git 时取当前目录。

## 目录与信任

| 路径 | 内容 | 生效 | 信任 |
|---|---|---|---|
| `.aaa/commands/<name>.md` | 斜杠命令模板 | 下次启动 | 纯文本，自动加载 |
| `.aaa/skills/<name>/SKILL.md` | 技能 | 下次启动 | 纯文本，自动加载 |
| `AGENTS.md`（仓库根） | 项目指令 | 下次启动 | 纯文本，自动加载 |
| `.aaa/extensions/<name>/extension.toml` | MCP 扩展声明 | 下次启动 | 需仓库信任；每次工具调用仍审批 |
| `.aaa/animations/<name>.py` | 启动动画 | 下次启动 | 需仓库信任（进程内代码） |
| `.aaa/memory_engines/<id>.py` | 记忆引擎 | 下次启动 | 需仓库信任（进程内代码） |

**优先级**：项目级 > 用户级（`~/.aaa`、`~/.agents/skills`）> 内置。同名先到先得，`/skills`、`/plugin` 与补全里显示 `[项目]` / `[用户]` / `[内置]`。

## 信任门

代码类（动画、记忆引擎、MCP 声明）首次在某个仓库出现时，交互启动会逐类询问并列出文件；同意后记录到 `~/.aaa/trusted/repos.json`（仓库路径 + 每类内容指纹），文件一改就重新询问。移动或复制仓库视为新仓库。

- **非交互**（`-p` / `--mode json`）：代码类一律不加载，向 stderr 输出一行说明，不写信任记录。
- `/trust`：查看当前仓库各类状态与已信任仓库；`/trust revoke <序号|仓库路径>` 撤销，重启生效。
- 项目级 `SYSTEM.md` 不支持：基础指令只允许用户级覆盖，避免仓库篡改。

## 配置键

| 键 | 默认 | 说明 |
|---|---|---|
| `project.enabled` | `true` | 关闭后只读用户级与内置 |
| `project.instructions_max_chars` | `8000` | `AGENTS.md` 注入上限（0 不注入） |

## 最小示例

```text
repo/
  AGENTS.md                     # 项目规则（自动注入系统提示）
  .aaa/
    commands/summarize.md       # /summarize 命令模板
    skills/repo-brief/SKILL.md  # 项目技能
```

随仓库提交即可；不需要信任确认（纯文本）。放代码类之前先想清楚：任何克隆该仓库的人都会在首次交互启动时被询问是否信任。

本仓库自身提交了 `.aaa/commands`（`/pr`、`/review`）与 `.aaa/skills/release`，可直接作为范例。
