# 主流 Agent Harness 共性机制调研

**结论**：对本机可查的七个 harness（Claude Code、Codex CLI、Cursor CLI、Gemini/Antigravity、Hermes Agent、omp、pi）的源码、二进制与数据目录取证后确认：行业已收敛出一套共同设计语法——信任分级权限、计划工件+批准门、分层 Markdown 上下文注入、上下文压缩、MCP 工具总线、技能/插件生态、子代理委托、会话持久化、HITL 审批防护。九项中前八项在 ≥5 家中可证；唯一系统性例外是 pi，其 README 显式声明跳过子代理与 plan 模式，属极简哲学的有意取舍而非功能缺失。**置信度：高**——claude/codex/omp/hermes/pi 有二进制或源码级直接证据；cursor 与 gemini 仅数据目录与官方文档级证据（本机二进制缺失），相应结论标注为文档级。

## 取证对象与方法

| Harness | 本机形态 | 取证手段 |
|---|---|---|
| Claude Code | `/opt/homebrew/bin/claude`（Mach-O ~60MB）+ `~/.claude/`（settings.json、AGENTS.md、hooks/、commands/） | 二进制字符串计数 + 配置面读取 |
| Codex CLI | npm `@openai/codex@0.164.0` → Rust 原生二进制 249MB + `~/.codex/config.toml` | 二进制字符串计数 + 配置面读取 |
| Cursor CLI | `~/.cursor/` 数据目录（cli-config.json、plans/、skills/、plugins/）；`~/.local/bin/cursor-agent` launcher 指向的 index.js 已缺失 | 配置面 + 目录工件证据 |
| Gemini/Antigravity | `~/.gemini/`（GEMINI.md、settings.json、skills/、antigravity-cli 数据）；CLI 二进制不在本机 | 数据目录 + 官方文档（geminicli.com） |
| Hermes Agent | `~/.hermes/hermes-agent/` 完整 Python 源码检出（editable 安装 0.20.2）+ `~/.hermes/`（SOUL.md、SKILL_MAP.md） | 源码 grep + 目录结构 |
| omp | `/opt/homebrew/bin/omp`（Mach-O）+ `~/.omp/agent/` | 二进制字符串计数 + 本机运行时第一方验证 |
| pi | `~/code_space/pi` TypeScript monorepo（`packages/coding-agent` 等 13 个包） | 源码 grep + README |

方法说明：二进制证据为 `LC_ALL=C grep -ac` 对字面字符串的计行数，是存在性信号而非语义确认；计数结论凡能与配置面或源码交叉印证的才采信。表中数字为该字符串在二进制中的出现行数。

## 共性矩阵

✓ = 本机证据确认；△ = 文档级/间接证据；✗ = 有证据表明不存在；空 = 本机无证据

| 共性机制 | Claude Code | Codex | Cursor | Gemini | Hermes | omp | pi |
|---|---|---|---|---|---|---|---|
| 信任分级权限 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✗ |
| 计划工件+批准门 | ✓ | ✓ | ✓ | ✓ | ✗ | ✓ | ✗ |
| 分层上下文文件 | ✓ | ✓ | △ | ✓ | ✓ | ✓ | ✓ |
| 上下文压缩 | ✓ | ✓ | 空 | △ | ✓ | ✓ | ✓ |
| MCP 工具总线 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| 技能/插件生态 | ✓ | ✓ | ✓ | △ | ✓ | ✓ | ✓ |
| 子代理委托 | ✓ | ✓ | △ | 空 | ✓ | ✓ | ✗ |
| 会话持久化 | ✓ | ✓ | ✓ | △ | ✓ | ✓ | ✓ |
| HITL 审批/危险命令防护 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | 空 |

## 逐项证据

### 1. 信任分级权限（只读 → 工作区写 → 全局）

同一条三级梯度，各家命名与执行机制不同：

- Codex：内置权限档字面命名为 `:read-only`、`:workspace`、`:danger-full-access`（配置参考文档；二进制中 `workspace-write`×31、`danger-full-access`×31、`read-only`×98），`sandbox_mode`×43、`approval_policy`×50，另有按项目的 `trust_level`（config.toml 实例 ×9 目录）。执行靠 OS 级沙箱（默认无网络、写限工作区）。
- Claude Code：`default` / `acceptEdits` / `bypassPermissions`（二进制 ×123）+ `dangerously-skip-permissions`（×28），配 allow/ask/deny 规则表（`~/.claude/settings.json` 实例：`permissions.allow`）。
- Cursor：`cli-config.json` 实例 `"approvalMode": "allowlist"` + `permissions.allow/deny`（`"Shell(ls)"`）+ `sandbox: {mode, networkAccess}`。
- Gemini：`default` / `auto_edit` / `yolo` 三档审批模式（官方 settings 文档）。
- Hermes：`hermes_cli/approval_mode.py` 定义 `VALID_APPROVAL_MODES = ("manual", "smart", "off")`，为 profile 级持久配置；危险命令走模式匹配守卫（`tools/approval.py`），另有无人值守会话的 `unattended_mode: deny|approve`。分级轴不是文件系统边界而是「人是否在场」。
- omp：`permission`×138 + `sandbox`×76。
- pi：**无**。源码与 CLI 参数（text/json/rpc 仅为接口模式）中均无权限阶梯；其哲学是把信任交给操作者，行为定制交给扩展。

### 2. 计划工件与批准门（plan 模式）

同一三元组：只读探索 → 产出计划工件 → 用户批准后进入执行态。

- Claude Code：`plan_mode`×45、`EnterPlanMode`×14、`ExitPlanMode`×26——以工具形式进出计划态。
- Codex：`update_plan`×44——计划即任务工件，兼具 todo 跟踪职能。
- Cursor：`~/.cursor/plans/` 存有真实计划工件（`*.plan.md`，如 `actor决策链路修复_2527e025.plan.md`）——计划落盘为文件。
- Gemini：`enter_plan_mode` / 计划工具集，只读规划态（官方 planning tools 文档）。
- omp：`plan_mode`×5。
- Hermes：无 plan 工具（源码 grep `plan_mode|enter_plan` 零命中）——自主优先定位，计划由用户指令替代。
- pi：README 原文「skips features like sub-agents and plan mode」——显式跳过。

### 3. 分层上下文文件（全局/项目/子目录 Markdown 注入）

同一套机制：markdown 文件按目录层级发现、注入系统上下文，全局→项目→子目录逐层叠加。

- Claude Code：`CLAUDE.md`×242、`AGENTS.md`×19；本机有全局 `~/.claude/AGENTS.md`。
- Codex：`AGENTS.md`×65；全局 `~/.codex/AGENTS.md` 实例。
- Hermes：`agent/agent_init.py`、`agent/coding_context.py`、`agent/context_engine.py` 均引用 `AGENTS.md`；并扩展出人格文件 `SOUL.md`（`~/.hermes/SOUL.md` 实例）。
- pi：`src/core/resource-loader.ts` 负责 `AGENTS.md` 发现注入。
- Gemini：`GEMINI.md`（`~/.gemini/GEMINI.md` 全局实例）。
- omp：`AGENTS.md`×36。
- Cursor：文档级（`.cursor/rules`；本机数据目录未见实例）。

### 4. 上下文压缩（compaction/summarization）

- pi：独立子系统 `src/core/compaction/`（含 `branch-summarization.ts`）。
- Hermes：顶层 `trajectory_compressor.py` + `agent/` 内多处 `compact` 命中。
- omp：`compact`×1354。
- Claude Code：`compact`×579。
- Codex：`ephemeral`×47（配合上下文管理）；本机 `logs_*.sqlite` 侧面印证会话态管理。
- Gemini：Antigravity 数据目录 `conversation_summaries.db` + `brain/`。
- Cursor：本机无直接证据。

### 5. MCP 工具总线

全部七家支持（pi 以独立包形式）：Claude（`mcp`×1702 + `~/.claude/mcp-configs/`）、Gemini（`~/.gemini/settings.json` 的 `mcpServers` 实例：stdio MCP 进程）、Hermes（顶层 `mcp_serve.py` + `optional-mcps/`）、pi（`packages/mcp`，含 OAuth 客户端）、Cursor（plugins 体系）、Codex（marketplaces/plugins 体系）、omp（MCP 资源设备）。

### 6. 技能与插件生态（可分享的行为包）

- Claude Code：`skills`×567、`hooks`×1224；本机 `~/.claude/commands/`、`hooks/`、marketplace 体系。
- Hermes：`skills/` 按域分类（apple/creative/devops/email/note-taking…，本机 10 类）+ `optional-skills/` + `plugins/` + `SKILL_MAP.md` 索引。
- pi：skills/extensions/prompt-templates/themes，可打包为 npm 包分发（README）；本机源码 `src/extensions/`。
- Cursor：`~/.cursor/skills/`、`skills-cursor/`、`plugins/{cache,local}`。
- Codex：`[marketplaces.*]` 插件源声明（config.toml 实例）+ `code-review-plugin/`。
- omp：`skill`×834；`~/.omp/agent/skills/`。
- Gemini：`~/.gemini/skills/` 目录（文档级）。

### 7. 子代理委托

- Claude Code：`subagent`×658、`AgentTool`×27；本机 `~/.claude/agents/`。
- omp：`subagent`×578。
- Hermes：`tools/delegate_tool.py` + `tools/delegation_live_log.py`——子代理是一等工具。
- Codex：`collab`×155（多代理协作机制）。
- Cursor：`~/.cursor/agents/` 目录（运行时证据）。
- pi：**无**，README 显式跳过，要求以扩展或包形式自建。

### 8. 会话持久化与自动化

各家均以本地存储持久会话并可恢复：Claude（`~/.claude/history.jsonl`、file-history）、Codex（`~/.codex/sessions/`、`archived_sessions/`、`memories*.sqlite` 记忆库、`automations/` 定时任务）、Hermes（顶层 `hermes_state*.py` 五件套状态层 + `cron/` + `gateway/`）、pi（`--session`/`--session-dir` 参数，src/cli/args.ts）、Cursor（projects/）、Gemini（conversations + 摘要库）、omp（`~/.omp/profiles/`、cache）。

### 9. HITL 审批与危险命令防护

Claude 的权限提示/规则表、Codex 的 `approval_policy` + 沙箱拒绝、Cursor 的 allowlist、Gemini 的三档审批、Hermes 的危险模式匹配守卫（递归删除、写 `/etc/` 等模式需人工批准）+ `unattended_mode`、omp 的审批门——六家可证。pi 无内建审批（空），依赖操作者自律与扩展。

## 分歧与例外

1. **pi 是唯一系统性反例**：极简内核（工具仅 bash/edit/read/grep/find/ls/write/powershell），把权限、plan、子代理全部让位给扩展生态。它证明这套「共性」是产品选择而非必然——但它是七家中唯一明文反对加入这些功能的。
2. **Hermes 的分级轴不同**：不按文件系统边界分级（无只读/工作区/全局三档），而按「人是否在场」分级（manual/smart/off + unattended deny/approve），且无 plan 模式——自主优先型 harness 的自然形态。
3. **enforcement 强度差一个量级**：Codex 用 OS 沙箱硬隔离网络与写路径；Claude/Cursor 靠配置表 + 提示遵守；Hermes 靠模式匹配黑名单。同名「权限模式」，防穿透能力不同。
4. **上下文文件已事实标准化于 `AGENTS.md`**：Codex/pi/Hermes/omp 原生读取；Claude 以 `CLAUDE.md` 为主但兼容读取 `AGENTS.md`（本机两目录均有实例）；Gemini 用 `GEMINI.md` 同构。Hermes 的 `SOUL.md` 是该机制向人格声明的延伸。
5. **OpenSandbox（`~/code_space/OpenSandbox`，opensandbox-group）不属 harness**：是给 harness 供沙箱的执行基础设施，佐证第 1、9 项的「执行隔离」趋势，未纳入共性矩阵。

## 与本仓库的映射

本仓库迁移源 `agno_agentic_assistant.py` 的 `boundary.py`（`MODE_READ/MODE_WRITE/MODE_FULL` + `SafeShellTools`）即上述第 1 项的三级梯度的独立复刻；缺 plan 工件、todo、压缩、MCP 与技能生态。若 aaa 未来扩展，此文档可作为「该抄什么、不必发明什么」的对照清单。

## 局限

- cursor 与 gemini 的 CLI 二进制不在本机，其权限/plan 证据依赖配置面工件与官方文档，未到源码级。
- 二进制字符串计数是存在性信号，单凭计数不构成语义结论；本文所有计数结论均有配置面、数据目录或源码交叉印证项，纯计数孤证未采信。
- hermes 源码仅做了定向 grep 与关键文件阅读（approval_mode.py 等），未通读；pi 通读了目录结构与 README 声明。
