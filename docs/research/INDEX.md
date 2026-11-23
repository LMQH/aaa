# 调研索引

**性质**：本目录是独立的调研仓库，只提供事实、候选评估与源码核查。

- **仅供参考，不承诺作为实现基线。** 这里的结论不构成对任何实现的约束；只有被转述或复制进设计文档的结论才生效。
- **设计文档不引用本目录。** 架构与模块设计文档里不出现本目录的链接或路径；需要用到结论时，把结论本身写进设计文档。
- **治理独立。** 本目录按主题维护，不代表产品状态；代码或设计变更时，不强制同步本目录。

| 文档 | 内容 | 依据 |
|---|---|---|
| `Agent-Harness与Workflow架构综述.md` | 五家 harness 的架构与 workflow 形态综述 | 源码/官方文档取证（2026-11-12） |
| `harness-common-features.md` | 七个 harness 的共性机制：信任分级、计划门、上下文注入、压缩、MCP、技能、子代理、会话、HITL | 源码/二进制/数据目录取证 |
| `Harness最小实现公式模型与闭环实现准则.md` | 五项公式 `Provider + Tools + AgentLoop + Context + Boundary` 与闭环准则 | 方法论提炼 |
| `模型上下文治理方案.md` | 上下文切片、公理/定理、记忆契约与引擎边界的治理设计 | 五家实现交集提炼 |
| `Agno-SDK原生覆盖能力调研.md` | Agno 3.1.12 在 Provider/Tools/AgentLoop/Context/Boundary/运维上的原生覆盖与缺口 | 本机 agno 3.1.12 源码 + 官方文档 |
| `Agno与Pi-Core对比调研.md` | Agno 与 Pi 的两条起点：配置 vs 接缝，工作量与可介入性对比 | 本机源码 |
| [`harness-subagent-delegation.md`](harness-subagent-delegation.md) | Pi（p2 插件源码：持久后台会话、team 点对点、reporter 链路）、Hermes（线程子代理、免确认写技能）与 aaa（原生结构创建+确认保存）的委派机制对比，含同名 team 语义辨析 | 本机 Pi 源码 b2aabe029、本机 Hermes bab7be3ca、本机 aaa 实现（2026-11-23） |
| `Pi-分层结构详解.md` | Pi 的分层结构：模型层、Agent core、编码应用层 | 本机 Pi 源码 |
| `pi-cli-design.md` | Pi CLI 的命令面、会话、主题、模型与自定义 provider 蒸馏 | 官方文档 + 安装包源码（2026-11-15） |
| `omp-cli-design.md` | omp CLI 的子命令、状态栏、上下文维护栈、扩展生态蒸馏 | 全量 `--help` + 配置树 + 二进制字符串（2026-11-16） |
| `最小 AgentLoop 实现学习笔记.md` | 以 Pi `agent-loop.ts` 为参照的 803 行最小循环拆解 | 本机 Pi 源码（2026-10-28） |
| `python-release-playbook.md` | 纯 Python CLI 的最小发版模式（打包、锁、发布流程） | 定案方案 |
| `从任务到目标的Agent架构演进.md` | Harrison Chase《From tasks to goals》：任务→目标的范式转变与六项工程落地要求 | LangChain 官方博客（2026-11-16） |
| `从任务到目标的Goal设计方法.md` | Harrison Chase《Goal Engineering》：目标设计以便于人工反馈为核心约束，含 Actively AI 案例 | LangChain 官方博客（2026-11-14） |
| `PersonalAgent与BusinessAgent架构差异调研.md` | 两种形态的四条结构性差异与十维对照；Agno（身份/存储/外壳强、执行缝缺）、Pi Core（镜像互补）、Hermes（多实例≠多租户）三条技术线的租户化距离与选型含义 | agno 3.1.12 源码复核 + Pi 源码快照 + omp 19.0.0 发布说明 + Hermes 官方文档（2026-11-19） |
| `佐助写轮眼进化历程.md` | 《火影忍者》佐助三勾玉→万花筒→永恒万花筒的契机、经历与能力差别整理（非技术参考） | 原作主线整理（2026-11-18） |

新增调研：按主题建文件，开头一段写结论与置信度，并把条目补进本表。
