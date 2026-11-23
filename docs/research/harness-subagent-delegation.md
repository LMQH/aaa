# Harness 委派机制对比：Pi、Hermes 与 aaa

**结论（置信度：Pi/Hermes 高——本机源码逐行核实；aaa 高——本机实现加本会话 QA；DeepSeekHarness 低——沿用旧调研，未复核）**：三家的主循环都不被委派接管，但委派的对象不同——aaa 委派的是**执行结构**（Agno 原生 Agent/Team/Workflow，模型可现场创建并经用户确认保存），Pi 委派的是**会话**（同一存储里的持久后台对话，天然留档），Hermes 委派的是**进程内子 Agent 实例**（线程池运行，技能可自动写盘）。"Team" 一词在 Pi 与 aaa 中是两种不同机制。

## Pi（本机源码 `code_space/pi`，chord b2aabe029，2026-11-10）

实现在 `apps/p2/plugins/subagents/`；核心循环 `packages/agent` 无任何委派概念（全库检索零命中），委派纯属插件/应用层机制。

- **子代理 = 独立持久会话**（pi-durable conversation），不是线程也不是每任务子进程（`tools.ts:1-2`，改自 pi-durable 示例 23）。子代理有自己的转写、模型、thinking 档位、工具选择甚至独立环境与 cwd；工具动作 `spawn/send/stop/configure`（`tools.ts:410-414`）。
- **`team` = 子代理间点对点通信**，不是 leader 编排：仅子代理可用（`scope: "subagent"`，"Only subagents can use the team tool"，`tools.ts:706`），动作 `send/answer/list`；`send` 发给 `parent` 或具名兄弟，忙碌时 `steer`（插话）或 `followUp`（排队）（`tools.ts:693-762`）。
- **后台运行 + 持久报告链路**：`p2.subagent-reporter` 持久任务分 deliver→report 两阶段、检查点化、崩溃可恢复；报告投递到"发起消息的那个会话"——用户 `/tree` 切走分支仍能收到；按 answer 去重（`tools.ts:145-223`）。子代理 anchor 任务立即终态，主代理的 Stop/idle 不等它；`abort({background})` 仍可达（`tools.ts:45-57`）；`waitForIdle` 与完成竞速做有界等待（`:73-74`）。
- **上下文继承**：p2 继承发起回合的 origin（`SUBAGENT_TASK_ENTRY`，`tools.ts:157,179`）。p3 计划 `context: "inherit"`——在派生点 fork 父会话，子代理知道父知道的一切，同模型时共享前缀即提示缓存命中（`chord/docs/p3-features/book.md`「Subagents with the parent's context」，2026-11-02 owner 决策）。**文档级计划，未见实现。**
- **p3 计划**：`subagent/team/agent` 升为基础工具；子代理进程归入 detached 进程组、整树可杀（`chord/src/node/workers/process.ts:1-4` 的 worker 基建已实现：fork + IPC link + heartbeat）。
- 一次性 `agent` 工具（脚本生成）**只读**（`worker.ts:44`）。

## Hermes（本机源码 `~/.hermes/hermes-agent`，bab7be3ca）

- 子代理 = **进程内线程**中独立 `AIAgent`（`tools/delegate_tool.py:64,1812`）；批量用 `DaemonThreadPoolExecutor`（`:3743`）；并发上限与子超时默认关闭（`:734,819`）；leaf 角色禁递归委派（`:52`）。
- 子线程拿不到交互审批回调，危险命令**默认自动拒绝**、可配置全放行（`:64-98`）。
- 后台委派完成事件回投为新的空闲回合，不插入进行中的对话（`tools/async_delegation.py` 头注释）。
- 技能自我进化**默认直接写盘**：`skills.write_approval` 缺省 off（`tools/write_approval.py:74-90,265-293`），前台 `skill_manage` 与后台回合后审阅都可创建/修补技能；开启配置才改为暂存审批。
- **无 Workflow/Team 概念**：本机源码复核，`workflow` 一词仅见于注释与无关工具，没有步骤引擎或成员编排。多步任务靠 `delegate_task` 的 orchestrator 角色（`:718-728`）递归委派——拆几步、先做哪个由子代理每次临场决定，流程不存在为可复跑的结构。

## aaa（本机实现，`docs/delegation-design.md`）

- 委派对象 = **Agno 原生 Agent/Team/Workflow**，由数据定义现场组装；定义默认临时，模型按复用价值提议、用户确认后原定义连同私有依赖落盘（快照逐字节绑定，不自动覆盖）。
- 首版同步：批量在并发上限内的线程池执行，逐项返回终态/结果/拒绝原因；取消只请求不强杀，未确认停止不报"已取消"；子任务遇需确认动作不执行、原因回传主 Agent。
- Team 是 Agno 的 leader 编排模式（coordinate/route/broadcast/tasks）；Workflow 是固定步骤 + 并行 + 字面条件分支。

## 逐维对照

| 维度 | Pi p2 | Hermes | aaa |
|---|---|---|---|
| 委派对象 | 持久会话（对话即子代理） | 进程内子 Agent 实例 | 原生 Agent/Team/Workflow 结构 |
| 运行形态 | 后台为主，可 stop/插话 | 同步批量 + 可选后台 | 同步批量（首版） |
| team 含义 | 兄弟子代理互发消息 | 无此概念 | leader 编排成员（Agno 语义） |
| 上下文继承 | p2 继承回合 origin；p3 计划 fork 父会话（缓存命中） | 全新对话 | 不继承主会话历史 |
| 持久化观 | 会话即持久：运行记录天然跨会话可见、可切进去看 | 技能持久（默认免确认写盘） | 定义持久（须确认）、运行进度不存 |
| 结构创建权 | spawn 时选配置，不创建新结构类型 | 无 | 模型现场创建三类结构并资产化 |
| 审批 | 子代理独立工具面；一次性 agent 只读 | 自动拒绝（可配全放行） | 拒绝并回传原因，不转发 |
| 结果回传 | reporter 任务：父分支跟踪、去重、崩溃恢复 | 聚合 JSON，事件回投 | 逐项终态 + refusals，批次保序 |
| 停止语义 | abort 信号/stop 动作；进程组整树可杀（p3 worker 层） | 请求取消，daemon 线程不硬杀 | 请求取消；未确认停止不谎报 |

**三家共同的不变量**：主循环不被接管；子执行体不共用父的交互审批；停止都是"请求 ≠ 已停"；结果回传带关联与去重。

## 对 aaa 的定位与启示

- **aaa 独有**：把"怎么做事"做成可创建、可确认保存的**结构资产**（Team 编排、Workflow 步骤）。Pi 的持久物是会话本身（运行留档天然存在）；aaa 是定义持久、运行不存——两者回答不同问题。
- **同名陷阱**：Pi 的 `team` 是兄弟通信，aaa 的 `Team` 是 leader 编排；对照外部方案时先对齐语义再比较。
- **若 aaa 未来做后台委派**，Pi 的 reporter（父分支跟踪、检查点恢复、完成事件回投新回合）是最完整的参考形态；Hermes 的完成队列是同一思想的简化版。
- **上下文继承是相反取舍**：Pi 的 inherit-fork 优化"延续性任务"的成本（缓存命中），aaa 的不继承优化隔离与结果独立性；两者都成立，取决于任务形态，不必互相说服。

## 证据边界

Pi 机制事实全部来自本机 checkout 源码行号（上列）；p3 相关条目（inherit-fork、基础工具化、进程组）是该仓文档中的 owner 决策与计划，非已实现行为。Hermes 条目来自本机 `bab7be3ca` 源码。aaa 条目反映本会话实现与 QA 快照（`HANDOFF.md` 2026-11-23）。DeepSeekHarness 行来自旧调研（官方公开分支的 `subagent`/`workflow` 工具：前者每子任务独立子代理、后者模型写 JS 编排脚本交工作流引擎），本轮未复核。
