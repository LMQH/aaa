# Agent Harness 与 Workflow 架构综述

日期：2026-11-12
证据基础：本机五家 harness 源码/官方文档取证（Claude Code 2.1.311、OpenAI Codex 0.164.0、DeepSeek Harness、Pi、NousResearch Hermes-agent），取证明细见附录。标 `[INFERENCE]` 处为推断，其余均为源码/文档直接证据。

---

## 一、分类框架

### 1.1 术语

**harness（脚手架/scaffold）= 模型之外的一切**：工具调用循环、工具集、system prompt、上下文管理、权限与沙箱。通行表述：**agent = model × harness**。同一模型换 harness 分差显著，harness 本身是工程研究对象（"harness engineering"）。

### 1.2 四个分类轴

| 轴 | 取值 | 代表 |
|---|---|---|
| 任务域（垂直 vs 通用） | coding / deep-research / computer-use·browser / 个人助理（horizontal） | Claude Code、Codex CLI、DSH、Pi / Deep Research / Operator、browser-use / Hermes、OpenClaw、Coze |
| 运行面 | CLI·TUI / IDE 内嵌 / 云端 VM / 嵌入式 SDK | Claude Code、Gemini CLI / Cursor / Devin、Codex cloud / Agent SDK 系 |
| 控制流 | 固定 workflow / 单循环反射式 / orchestrator-workers / plan-then-execute | 节点图平台 / Claude Code 主循环 / Task 与 subagent / todo 计划机制 |
| 单体 vs 多智能体 | 单 agent / 多 agent 编排 | 五家主流默认单体+可选子代理 / AutoGen、LangGraph、agent teams |

正交边界：**框架 ≠ 成品 harness**。LangChain/LangGraph/AutoGen 是构建库，不直接交付终端用户；谈 harness 分类默认排除框架层。

"垂直/通用"二分是产品层说法，不是架构分类主轴：五家源码骨架同构（tool-use loop + 上下文管理 + 权限层），差异在工具面、权限模型与上下文策略。

### 1.3 控制流轴精讲

理解控制流只需三个问题：

1. **下一步谁选**——代码还是模型？
2. **什么时候停**——终止条件写在哪？
3. **一步做完结果去哪**——怎么进上下文、何时压缩？

两个端点（Anthropic《Building Effective Agents》即取此二极）：

```python
# workflow：路径住在代码里，LLM 是被排好序的函数
r1 = llm(EXTRACT, doc)
r2 = llm(TRANSLATE, r1)
return review(r2)                 # 下一步永远是写死的

# agent：路径住在模型的决策里，harness 只当记账员
messages = [system, user]
while not done(messages):         # 终止是唯一的"代码路径"
    act = model(messages)         # 模型决定下一步
    messages.append(tools.run(act))
```

workflow 可预测、可单测，但每条分支要人事先想到；agent 能走没预料到的路，但错误决策会复利。

**中间谱系**（按模型决策比重排）：

| 模式 | 模型决定 | 代码决定 | 例子 |
|---|---|---|---|
| 链式 workflow | 每步的内容 | 每步的顺序 | 抽取→翻译→审校 |
| 路由 | 走哪条分支 | 分支内的一切 | 客服分流 |
| fan-out / fan-in | 每份子任务内容 | 分几份、何时合 | 批量摘要 |
| evaluator-optimizer | 怎么改 | 循环上限 | 审查-修复环 |
| orchestrator-workers | 怎么拆、怎么合 | worker 能力边界 | Claude Code 的 Agent/Task、Hermes delegate |
| 单循环反射式 | 每一步 | 工具集/权限/终止 | Claude Code 主循环、Pi runLoop |
| plan-then-execute | 先出计划再逐步执行 | 计划作上下文锚点 | todo list 机制 |

成品 harness 是**叠加态**：单循环做骨架 + todo（计划降级为上下文锚点）+ 子代理（workers，各自又是完整循环）。判别一个 harness 别看宣传，看 transcript：下一步在代码里可预测 → workflow；下一步要从"模型读完上一步结果"才涌现 → agent。

---

## 二、五家 harness 源码级对比

### 2.1 总表

| 维度 | Claude Code 2.1.311 | Codex 0.164.0 | DeepSeek Harness (DSH) | Pi | Hermes |
|---|---|---|---|---|---|
| 本体 | Bun 单文件二进制 263MB + `sdk-tools.d.ts` | Rust 单体二进制（npm 包装） | TS monorepo，Cordis 插件架构 | TS monorepo，极简循环 | Python 单进程 + 多平台网关 |
| 主循环 | 单循环，模型逐 turn 决策 | thread→turn→item，单循环 | turn/step 循环（`core/agent-loop/src/agent.ts:254`），**loop 本身是可替换插件** | `agent-loop.ts` runLoop，极简 | `conversation_loop.py:1908` while，单循环 |
| 终止决定者 | 模型不再调工具 + 权限/预算 | 模型 + goal token 预算 | inbox 空 + `agent/turn-stopping` 闸（可 re-steer） | stopReason 硬退 + `finishTurn` 钩子 | 模型文本终答 + max_iterations(500) + **护栏硬停** |
| 子代理 | `Agent` 工具：`subagent_type/model/effort/isolation(worktree\|remote)`，可后台、可命名寻址（SendMessage） | `collaboration` 命名空间：`spawn_agent/wait_agent/interrupt_agent`，树形 task path，并发 4 槽 | **可替换 provider seam**（spawn/fork/acp/codex/claude-code/dsh-sdk），one-shot + continuable 双模式 | **CLI 无**（README:"skips sub-agents and plan mode"）；实验包 `durable` 有 subagent conversation | `delegate_task` 递归树（leaf 剥权限工具），线程池并发，异步完成走"伪 user turn"保 prompt cache |
| 确定性拦截层 | Hooks 33 个生命周期点（PreToolUse 可 deny）；本机 settings.json 实用中 | Hooks 14 事件（`codex-rs/hooks/src/lib.rs`），command/mcp_tool 处理器 | 事件瀑布 `tools/pre-execute→approval→execute→post-execute`，无 approval 服务时降级 deny | `beforeToolCall/afterToolCall/turn_end` 扩展边界 | `tool_guardrails.py`：sha256(工具+参数) 签名护栏 warn/halt + 每 turn 硬上限（web_search≤50） |
| Skill 形态 | SKILL.md 懒加载："long reference material costs almost nothing until you need it" | SKILL.md 渐进披露（catalog ≤2% 上下文或 8000 字符），`.system/` 内置 | SKILL.md → durable system-reminder 目录注入，`skill` 工具按需加载 | SKILL.md XML 目录注入（仅 name/description），正文按需 read | SKILL.md 索引注入 + 多源 skills_hub 市场客户端（搜索→隔离→安全扫描→安装） |
| 计划持久化 | TodoWrite + Task 依赖边（`addBlocks/addBlockedBy/owner`） | `turn/plan/updated`（无执行语义）+ `goal`（objective+tokenBudget） | `todo_write` 整表替换 + turn 投影（turn/start 清空 standing plan） | 无 CLI 级 plan mode | todo + goal（judge 判定续跑） |
| 定时调度 | `CronCreate`（5 字段 cron，`durable`→scheduled_tasks.json，7 天过期）+ `ScheduleWakeup`（60–3600s）+ `RemoteTrigger`（云端） | CLI 无调度器 `[INFERENCE]`；ChatGPT 桌面产品面才有 automations | `schedule` 包：会话内提醒（≥5min），无跨会话 cron | 无 | `cron/`：jobs.json + 60s tick，at-most-once，每 job 开新会话，`no_agent` 纯脚本模式，blueprint 目录 |
| **Workflow 编排层** | **有**：`Workflow` 工具执行 JS 脚本（`agent()/parallel()/pipeline()/phase()`），持久化 `.claude/workflows/`，`/name` 复用，`resumeFromRunId` 续跑，可后台/上云 | **无**（app-server 无 workflow 端点；Agent Builder 是独立 API 产品） | **有执行核**：同原语集 JS 脚本 + worker 线程；**注册表/持久化/resume/嵌套全部标注 deferred** | 无（实验包 `durable` 有 task graph，不进 CLI） | 无（kanban = SQLite 队列 + 进程 worker + judge 续跑，非 DAG） |
| 外部编排 seam | `-p`/stream-json/`--json-schema`/`--bare`；TS/Python Agent SDK；CI；Channels/webhook | app-server JSON-RPC 2.0（审批回调/dynamicTools 注入）、`exec --json --output-schema`、SDK、GitHub Action | headless bundle、cordis patch 全量可替换 | SDK/扩展（进程内 TS） | 平台网关（10+ IM）、ACP 适配器 |

### 2.2 各家要点与设计理念

**Claude Code**（`@anthropic-ai/claude-code`，SDK 面共 44 个工具输入类型）
- 编排全家桶已内置：Workflow / Cron / ScheduleWakeup / RemoteTrigger / Monitor / Task 依赖边 / agent teams（实验，lead+teammates+共享任务列表+mailbox）。
- 官方理念：*"Claude Code is the layer around the model that provides the tools and manages the context the model sees. This surrounding layer is what the term agentic harness refers to."*；*"When this guide says 'Claude chooses'… it's the model doing the reasoning."*；workflows.md：*"With subagents, skills, and agent teams, Claude is the orchestrator: it decides turn by turn what to spawn or assign next … **A workflow moves the plan into code.**"*；博客：*"Claude can now write its own harness on the fly."*
- 本机使用面：90+ subagent 定义、70+ skills、hooks 实用中；`~/.claude/workflows/` 未启用。

**Codex**（`@openai/codex`）
- 工具面由二进制在会话初始化注入（rollout jsonl 证据）：`apply_patch/exec_command(write_stdin)/goal 系列/request_user_input/web.run/clock.sleep` 等。
- 多代理默认禁止主动 spawn（`<multi_agent_mode>`），按模型与配置放开。
- hooks 与 skills 的分工被官方写成设计原则：*"Unlike instructions in AGENTS.md or skills, which the model may or may not follow, **hooks run the same way every time**."*
- 无原生 workflow；"Workflows/Automations" 属 ChatGPT/Agent Builder 产品面，与 CLI 分离。

**DeepSeek Harness**（Cordis 架构，论文《A Programming Paradigm for Spatiotemporal Composability》）
- *"产品的每一部分都是插件，包括模型适配器、工具注册表、会话日志，以及 agent loop 本身，因此每一部分都可以从配置替换。"*；*"不存在需要打补丁的特权内核。"*
- 上下文不变量："模型可见即已记录"——一切进模型的内容必须可从 append-only 会话日志重建。
- workflow 包：脚本 = 模型每次调用现写的自包含 JS（只有 `agent()/parallel()/pipeline()/phase()/log()` 五个全局原语），worker 线程 `node:vm` 执行（**明确不是安全边界**），`pipeline` 无 barrier、`parallel` 有 barrier、不支持嵌套；run 事实另写 4 个 log-only 事件。`tool-ralph` 是固定脚本的 Ralph loop（模型只给 objective/maxRounds）。
- 设计取舍："A future process or sandbox engine can replace the implementation without changing the tool."

**Pi**（badlogic/pi-mono）
- *"Pi is a minimal, extensible agent harness that you can make your own."*；*"Adapt Pi to your workflows, not the other way around."*；*"Pi ships with powerful defaults but skips features like sub-agents and plan mode. Ask Pi to build what you want."*；无内置权限系统。
- 三层扩展形态泾渭分明：skills=上下文注入（自然语言）、prompt templates=文本替换、**extensions=进程内可执行 TS（jiti），可拦截/改写工具调用、可在 turn_end 决定续停，但不能替换 runLoop** `[INFERENCE]`。
- `codemode`：QuickJS 沙箱让模型写 JS 调工具，嵌套工具调用不进 LLM 上下文——上下文经济学的小型示范。
- `durable`（实验）：checkpoint 级会话恢复 + task 状态机 + subagent conversation，刻意不进 CLI 运行时。

**Hermes**（NousResearch/hermes-agent）
- 定位自述：*"The self-improving AI agent… the only agent with a built-in learning loop — it creates skills from experience, improves them during use…"*（skill_manage + /learn + 回合末后台评审 fork + curator 空闲整理）。
- 两条铁律：*"Per-conversation prompt caching is sacred… Anything that mutates past context… invalidates that cache."*；*"The core is a narrow waist; capability lives at the edges… Most new capability should arrive as a CLI command + skill, a service-gated tool, or a plugin — not as core surface."*
- 护栏与执行分离：`tool_guardrails.py` 自述 "intentionally side-effect free… Runtime code owns whether those decisions become warning guidance, synthetic tool results, or controlled turn halts."
- 编排能力 = 单循环 + 递归子代理 + cron 定时 + 看板队列，无 DAG 引擎。

### 2.3 关键发现

1. **Claude 与 DSH 的 workflow 引擎几乎同构**：同为"模型写自包含 JS 编排脚本 + `agent()/parallel()/pipeline()/phase()` 四原语 + LLM 节点全包成 agent 调用"。两个独立团队收敛到同一形态，且**都不是 Coze 式节点图**。
2. 两家差距即成熟度：Claude 多做了持久化注册表（`.claude/workflows/` + `/name` + `resumeFromRunId` + cron `durable`），这四项恰是 DSH `dynamic-workflows.md:59-63` 明确 deferred 的清单。
3. 五家共同收敛点：单主循环、SKILL.md+frontmatter 渐进披露、子代理=上下文隔离+工具收窄+结果回传、hooks=确定性生命周期拦截、会话日志=唯一事实源。差异只在编排层的有无与形态。

---

## 三、节点图时代（Dify/Coze/AutoGen 式）为何落幕

**核心判定**：死掉的不是"确定性编排"这个目标（hooks、护栏、workflow 脚本都是它的存活形态），死掉的是"**用可视化节点图充当通用 agent 的控制层**"这个实现。错在抽象位置放反：把最容易的部分（流程结构）写死在图上，把最困难的部分（每步可靠性）留给人肉填。

### 3.1 五条死因

1. **不确定性长在节点内部，图管不住。** LLM 调用的方差发生在节点内（输出不合 schema、提取缺失），图只能在每个节点塞重试/校验/修复，错误处理组合爆炸；而"下一步做什么"最需要模型判断（取决于上一步拿回什么），图却把这里写死成边。
2. **上下文经济学：每条边都是一次有损压缩。** 节点间靠序列化变量传状态，模型看不到全程历史；单循环 + 只追加 transcript 赢在质量，也赢在成本——prompt cache 按前缀命中，追加式每轮命中，节点图每节点前缀不同 = 每步缓存全灭。Hermes 铁律：*"Per-conversation prompt caching is sacred."* DSH 不变量："模型可见即已记录"。
3. **模型增速让每条硬编码的边变成天花板。** 2023 模型弱，节点图是拐杖；模型学会规划/可靠调工具/自我纠错后，画死的分支全部变成上限。不对称性：skill 是提示词，模型变强自动受益零改造成本；图要人重新接线。押注"模型变强率"的厂商全部选软先验。LangChain 自身放弃 chain 抽象转向状态机运行时、市场进一步走向 Agent SDK 与自由循环 `[INFERENCE：公开演进轨迹]`。
4. **任务分布错配：为不存在的复用付维护税。** 节点图收益公式 = 高频 × 流程稳定 × 作者不会写代码。coding/研究任务是长尾，图画一次永不复用；真正高频稳定的流程早已固化在脚本/Makefile/CI。节点图平台退守到前提成立的领地：非开发者业务自动化（Coze/Dify/n8n 市场）。
5. **调试归因碎片化。** 图的调试 = 逐节点查 I/O 契约，trace 被边切碎；agent 的调试 = 读一份 transcript。五家持久化单位全部收敛为会话日志（rollout jsonl / SessionEvent 流 / JSONL 树 / sqlite），无一家以节点 I/O 为观测中心。

### 3.2 遗产存活表（代码形态重生）

| 节点图时代的目标 | 2026 存活形态 | 证据 |
|---|---|---|
| 流程可审计、可重放 | append-only 会话日志 | DSH"模型可见即已记录"；Claude/Codex rollout |
| 关键点确定性拦截 | hooks / 护栏 / 审批闸 | Codex："hooks run the same way every time" |
| 复杂流程可编排 | **workflow 脚本**（四原语包 LLM 节点，节点间纯代码） | Claude `Workflow` 工具、DSH `packages/workflow` |
| 非工程师造自动化 | 独立产品面（Coze/Dify；OpenAI 拆为 ChatGPT Plugin Creator/Agent Builder，与 CLI 分离） | plugins.txt、Agent Builder 文档 |

判决书浓缩（Claude 官方 workflows.md）：*"Claude is the orchestrator: it decides turn by turn what to spawn or assign next … **A workflow moves the plan into code.**"* —— 进代码，不进图。

---

## 四、Workflow 提案评估

提案流程：用户自然语言 → Agent 澄清需求 → 组装 Workflow → 执行（节点含 LLM 子代理/固定工具序/非 LLM 动作）→ 结果回传总结 → Workflow 持久化复用。

### 4.1 判定

**方向已被业界验证并部分实现。** Claude Code 的 Workflow 工具链与提案逐字对应（脚本 + meta + phases、存 `.claude/workflows/`、`/name` 复用、`resumeFromRunId` 续跑、异步运行+回传）；DSH 已建执行核，持久化四件套（注册表/斜杠命令/脚本持久化/journaling+resume）deferred——这份 deferred 清单即现成的贡献清单。形态收敛结论：**"workflow=代码脚本"，不是 YAML 节点图**。

### 4.2 三个增量空间（均有源码证据）

1. **非 LLM 节点（价值最高）**：两家的脚本原语只有 `agent()`——每个节点都是一次 LLM 调用。发 Lark 消息这类确定性步骤只能让 agent 跑 lark-cli（多一次模型往返）或跳出 workflow 用 hooks/cron。直接暴露 `tool()/exec()` 零 token 节点给脚本，是两家都没做的空白。
2. **声明式元数据层**：Claude 的 meta 埋在脚本里；面向人审查/参数 schema/触发器声明的独立清单层仍空缺——应做成"脚本+元数据"，而非节点图。
3. **蒸馏**：`resumeFromRunId` 只解决续跑；"从成功轨迹自动生成 workflow 脚本"（Agent Workflow Memory 方向）五家均未实现。

### 4.3 风险

- workflow 运行器明确不是安全边界（DSH worker-thread README "Trust and isolation boundary"；Claude Workflow 调用走权限批准）。自建此层时节点权限模型必须从第一天设计。
- 复用与漂移的平衡：固化收益 vs 环境漂移维护成本，模型临场判断优于预先画图——蒸馏时机应由运行证据驱动。

### 4.4 形态建议（代码即图）

```python
# 节点图形态（提案字面版）：一个运行时 + 三类节点定义
wf = Workflow("lark-report")
wf.node("collect",  AgentNode(role="a"))
wf.node("pipeline", ToolSeqNode(["a", "b", "c"]))
wf.node("notify",   ShellNode("lark-cli send ..."))
wf.edge("collect", "pipeline", "notify")

# 脚本形态（Claude/DSH 已验证）：没有运行时，代码即图
async def run(args):
    facts = await agent("收集数据", role="a")     # 节点A：LLM
    out   = await pipeline(facts)                  # 节点B：工具序（纯代码）
    sh("lark-cli send", out)                       # 节点C：零 token（增量点）
```

后者短、可版本化（git）、失败读 transcript 定位，天然容纳三个增量。最小验证路径：在 DSH 上给 workflow runtime 加 `exec` 原语（deferred 清单没有它，改动面小），跑通"skill → 生成脚本 → cron 触发 → Lark 日报"闭环，对比 token 成本与漂移率。

---

## 附录：证据索引

| Harness | 本机位置 | 关键证据文件 |
|---|---|---|
| Claude Code 2.1.311 | `/opt/homebrew/lib/node_modules/@anthropic-ai/claude-code/` | `sdk-tools.d.ts`（44 工具 schema，Workflow :2871-2902、Agent :774-815）；官方 docs：code.claude.com/docs/en/{workflows,hooks,sub-agents,skills,headless,how-claude-code-works,permission-modes}.md |
| Codex 0.164.0 | `~/.local/share/fnm/.../node_modules/@openai/codex/`；运行时 `~/.codex/` | rollout jsonl（工具 schema `:3`、多代理 `:6`）；`/tmp/app-server.txt`（JSON-RPC 全量方法表）；官方 developers.openai.com/codex/{hooks,build-skills,codex-sdk} |
| DeepSeek Harness | `/Users/lmqhx/deepseek-harness/` | `docs/architecture.zh.md`；`packages/workflow/{workflow,workflow-worker-thread,tool-workflow,tool-ralph}`；`docs/subsystems/subagent.md`；`.agents/notes/implemented/feature/2026-07-05-dynamic-workflows.md` |
| Pi | `/Users/lmqhx/code_space/pi/` | `packages/agent/src/agent-loop.ts`；`packages/coding-agent/src/core/agent-session.ts`；`packages/{codemode,durable}/README.md`；`README.md` |
| Hermes | `/Users/lmqhx/.hermes/hermes-agent/` | `agent/{conversation_loop,tool_guardrails,tool_executor,context_compressor}.py`；`tools/{delegate_tool,approval,skills_hub}.py`；`cron/scheduler.py`；`AGENTS.md`；`~/.hermes/config.yaml` |

说明：GitHub 直连当日不可用，Codex 源码以本机二进制 + 会话 rollout + 官方文档快照取证替代；Hermes/Codex/Pi/DSH 均为本机源码直接读取。
