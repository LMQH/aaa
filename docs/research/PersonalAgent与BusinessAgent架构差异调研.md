# PersonalAgent 与 BusinessAgent 的架构差异调研

日期：2026-11-19
范围：架构形态对比，不绑定具体产品和实现。证据源三组：Agno 3.1.12 安装包源码（2026-11-19 复核）、Pi Core 源码快照（2026-09-11）及其产品化上游 omp 19.0.0 发布说明（2026-11-19）、Hermes Agent 官方文档（2026-11-19 读取）
用途：回答「PersonalAgent 长成 BusinessAgent，差的是什么；主流 harness 家族与 Agent SDK 各自站在哪一侧、差多少」

## 结论摘要

**两者的最大差别不是「多了登录和计费」，而是四条结构性变化：身份显式化、信任主体换人、状态从私有变共享、执行从进程内变托管。**「多租户适配」是这四条在服务化场景下的合称；其中前两条（身份、信任主体）是个人形态无法局部修补、必须重新设计的部分，后两条（状态、执行）决定迁移是增量还是重构。

| 判定 | 结论 | 置信度 |
|---|---|---|
| PersonalAgent → BusinessAgent 的结构性差异 | 四条：身份模型、信任主体、状态归属、执行托管；其余六维（工具面、上下文治理、成本计量、可观测、入口、交付）是可演进的形态差异 | 高（逐维论证见第二节） |
| Agno 与 BusinessAgent 的距离 | 「身份/存储/运行时外壳」一侧接近配置级增量：`user_id`/`session_id` 一等参数、15 种共享数据库、AgentOS 自带 auth/authz(RBAC)/调度/审批/沙箱归属；「执行治理」一侧撞接缝缺口：无 per-request 预检点、无统一策略源、无总成本上限 | 高（安装源码复核） |
| Pi Core 与 BusinessAgent 的距离 | 「执行治理」一侧接缝齐全（`transformContext`/`beforeToolCall`/`shouldStopAfterTurn`/`prepareNextTurn`）；「身份/存储/外壳」一侧全部缺位，且 coding-agent 层文档自认不管租户、队列、审计、授权中心——这些被划给「产品宿主」 | 高（源码快照 + 官方边界声明） |
| Hermes 是否算 BusinessAgent | **不是。** profiles 是同一台机器上多个独立个人 agent 的状态隔离（独立 home、配置、密钥、记忆、会话、gateway），信任主体仍是单一操作者；官方明文 profiles 不做沙箱，文件系统权限与 OS 用户相同。是「多实例个人部署栈」，不是「一服务多租户」 | 高（官方文档 2026-11-19） |
| 三者的缺口性质 | **互补**：Agno 强（状态与外壳）恰是 Pi/Hermes 无；Pi 强（执行缝）恰是 Agno 缺；Hermes 只补了交付形态（常驻、多渠道、可容器化），没碰信任结构 | 中（结构性判断） |
| 多租户 ≠ 多用户 | `user_id` 原语解决「多用户」（互信个体共用）；租户（互不信任的组织单元）的硬隔离边界与授权面仍需自建——「Agno 原生面向多租户」的准确表述是「原生面向多用户服务化」 | 高（语义区分 + 源码证据） |
| 上游趋势佐证 | omp 19.0.0 继续加深单人形态：每用户单 daemon、会话资产 `local://` 本地化、跨实例协作以同目录为界 | 高（发布说明） |

## 一、工作定义

| 对象 | 定义 |
|---|---|
| **PersonalAgent** | 单人、单机、进程内闭环：一个进程服务一个坐在终端或聊天窗口前的人；凭据是个人密钥；状态在本地库；信任模型是「操作者即 owner」，HITL 审批人就是资源所有者本人 |
| **BusinessAgent** | 多租户服务端系统：一个部署服务 N 个互不信任的组织单元；凭据托管在服务端；状态在共享存储；信任模型是「策略即 owner」，授权由策略引擎与租户约束决定，不在场的人调用不在场的资源 |

「多租户」在本篇取严格义：租户间是**互不信任的硬隔离边界**（数据、记忆、凭据、配额、可见性五者都按租户切分）。多人共用但互信（如团队共用一个记忆池）只是「多用户」，不触发全部结构变化。

## 二、差异的十个维度

按四簇组织。**性质**列区分三类：*增量*（在现有架构上加组件）、*重构*（改现有组件的形状）、*新增*（Personal 形态中不存在、需从零建的面）。

### A. 身份与状态簇

| 维度 | PersonalAgent | BusinessAgent 要求 | 性质 |
|---|---|---|---|
| 身份模型 | 隐式单用户：用户身份是配置或环境常量，一个进程服务一个人；无认证 | 显式三级身份：租户 → 用户 → 会话；请求携带经认证的主体，匿名不存在 | 重构 |
| 状态存储 | 本地单库（SQLite 文件），只增不减 | 共享数据库 + 按租户隔离（库级/schema 级/行级 + 强制过滤）；保留、删除、备份恢复策略 | 重构 |
| 记忆归属 | 按用户池隔离，单人单池 | 跨租户记忆必须硬隔离；租户内共享记忆需要显式共享域设计；记忆维护需要版本语义 | 重构 |

### B. 信任与授权簇

| 维度 | PersonalAgent | BusinessAgent | 性质 |
|---|---|---|---|
| 凭据归属 | 个人密钥本地文件/环境变量，一个渠道一份 | 服务端凭据托管；按租户路由计费凭据；密钥轮换与吊销 | 新增 |
| 授权模型 | HITL：审批人就是 owner；静态确认名单 + 同一次批准只问一次的去重即可 | 策略引擎：RBAC/能力声明，租户 A 不可见租户 B 的工具与数据；模型输出不是业务完成，域服务独立校验 | 重构 |
| 工具面与沙箱 | 静态能力清单 + 工作区根约束 + shell 审批（通常无路径约束） | 按租户/按会话动态工具可见性；OS 级隔离（沙箱 per 会话）；逃逸即跨租户事故 | 新增 |

### C. 运行与治理簇

| 维度 | PersonalAgent | BusinessAgent | 性质 |
|---|---|---|---|
| 并发与生命周期 | 单进程、单会话串行；后台任务进程内不落盘 | 多会话并发、队列、后台调度、断线恢复；run 状态可持久可观测 | 重构 |
| 成本与配额 | 本地账本按供应商/模型计价，花费是本人的钱 | 每租户配额、限流、预算熔断、成本归因到租户/会话/请求三级；计费对接 | 重构 |
| 可观测与审计 | 本地日志轮转，无审计需求 | 中心化 OTel、审计留存（谁在何时让模型动了什么资源）、合规导出 | 新增 |

### D. 交付簇

| 维度 | PersonalAgent | BusinessAgent | 性质 |
|---|---|---|---|
| 入口形态 | 终端 REPL / 单轮打印 | Web/API/SSE 入口；会话可跨设备续接 | 增量 |
| 部署与 SLA | 本机一条命令 | 环境分层、健康门、回滚基线、可用性承诺 | 新增 |

**结构性结论**：B 簇的授权模型与 A 簇的身份模型是「换信任主体」的一体两面——Personal 形态里「问一下本人」在 Business 形态里没有对应物（本人不在场），必须换成机器可执行的策略判定。这一条无法增量演化，是四条结构性变化的根。其余维度可增量或按新增面处理。

## 三、Agno 侧：站在哪一侧，差多少

### 3.1 已有的租户化地基（配置级）

以下均对 `agno 3.1.12` 安装包源码实证（路径相对 `site-packages/agno/`），2026-11-19 复核：

| 能力 | 证据 | 对 BusinessAgent 的含义 |
|---|---|---|
| `user_id`/`session_id` 一等参数 | `session/agent.py:27`；`db/base.py:558-606` 的会话增删查均带 `user_id` 作用域（含 `reassign_session_owner`） | 多用户隔离原语现成；Agent 构造不变，换 `user_id` 即换用户 |
| 共享持久化 | 15 种数据库后端（postgres/mysql/mongo/redis/…） | 从本地 SQLite 换共享库是装配点改动，不碰执行路径 |
| 记忆按用户归属 | `UserMemory` 含 `user_id` 字段；`MemoryManager` 按用户池读写 | 记忆隔离原语现成；共享域与版本语义仍需自建 |
| AgentOS 运行时外壳 | `os/`：REST/SSE/WebSocket 路由、`os/auth.py`、`os/authz/`（role store、admin router = RBAC 实证）、job 队列、审批、组件 Registry | 服务化外壳（认证入口、API 面、后台调度）接近配置级 |
| 沙箱归属 | `sandbox/` 执行契约（create/exec/pause/resume/stop），归属与队列由 AgentOS 负责 | 执行隔离的后端契约现成；「哪租户可开沙箱」的策略仍需自建 |
| 输入/输出校验 | Guardrails（PII/注入/审核）为 pre/post-hook | 租户输入面校验有落点；不检查历史与检索文档 |
| 可观测与评估 | OpenTelemetry 自动埋点；eval 四件套 | 审计与质量面有落点，留存策略自建 |

### 3.2 撞上的缺口（与租户化正交但被租户放大）

Agno 的三处已知架构摩擦，在单人形态下是「以后再补」的债，在多租户形态下多数升级为**硬前置**：

| 缺口 | 单人形态代价 | 多租户形态代价 |
|---|---|---|
| 无 per-request 预检点（钩子只有 run 级与工具级，工具循环在 Model 内部；落在「一批工具结果之后、下次请求之前」的只有内部 `after_tool_results`） | 治理只能塞进 Model 子类或外建循环 | 按租户做预算预检、按租户注入工具面、跨租户上下文审查——全都要求请求前接缝，没有它，租户策略无处安放 |
| 无统一策略源（授权/校验/配额/可见性散在 pre_hooks/post_hooks/tool_hooks/构造参数/HITL 标记五处） | 边界声明分散，可自建替换点 | 租户策略必须单源可审计；五处分散意味着授权判定不可集中复核 |
| 无总成本上限（配额只到调用次数、重试次数与单命令超时）；记忆维护删除留史（`clear_user_memories` 后重写，条目无 kind/revision/status） | 个人自担成本；个人记忆删改可接受 | 预算熔断是租户硬需求；租户数据有合规留存要求，删改不留史不可接受；没有上限面意味着超支只能事后追账 |

**小结**：Agno 与 BusinessAgent 的距离在「身份/存储/外壳」半边是**常规服务端工程**（换库、接 AgentOS、写租户路由）；在「执行治理」半边撞的是它一直缺的接缝——那半边的工作会从「配置」变成「改框架或外建」。

## 四、个人 harness 家族：Pi Core 与 Hermes

### 4.1 Pi Core：执行缝齐全（治理友好的一侧）

`pi-agent-core` 的 `AgentOptions` 把治理需要的位置全部开成公共 API：`transformContext`（每次模型调用前）、`beforeToolCall`/`afterToolCall`（工具策略）、`shouldStopAfterTurn`/`prepareNextTurn`（回合级控制）。租户策略（预算预检、上下文注入、工具改写）在 Pi 里是「写策略」而非「改框架」——执行治理半边近乎就绪。

### 4.2 Pi Core：身份/存储/外壳全部缺位（且是声明过的边界）

| BusinessAgent 需要 | Pi Core 现状 |
|---|---|
| 会话存储 | session 是原语，存储自接（JSONL 本地文件为默认形态） |
| 身份/租户 | 无任何概念；`AgentSession` 无主体字段 |
| 服务化外壳 | coding-agent 层自认不负责「业务平台编排、租户、队列、审计、授权中心」，划给产品宿主 |
| 多实例协作 | 上游 omp 19.0.0 的协作边界仍是单用户形状：共享后台进程收拢为**每用户**单 daemon；跨实例消息以**同目录**为界；会话资产 `local://` 会话本地化 |

最后一条值得展开：omp 是 Pi 同一作者的产品化上游（发布包仍为 `@oh-my-pi/pi-agent-core`、`@oh-my-pi/pi-coding-agent` 这一组 pi 包），它 18.x→19.x 的演进方向（per-user daemon、每机加载语音模型、本地凭据 auth broker、个人订阅用量上报）说明**单人形态在产品线上被持续加深而非服务化**。把 Pi/omp 延伸成多人共用服务，不是沿着它的路线多走几步，而是另起控制平面。

### 4.3 Hermes：多实例，不是多租户

Hermes Agent（NousResearch，定位「The agent that grows with you」）有两个入口：终端 TUI，以及常驻 gateway 连接 Telegram/Discord/Slack/WhatsApp/Signal/Email。它支持 **profiles**——这是最容易被误读为「原生多租户」的特性。

**profiles 是什么。** 同一台机器上多个独立个人 agent：每个 profile 一个 home 目录（`~/.hermes/profiles/<name>/`），内含自己的 `config.yaml`、`.env`、`SOUL.md`、记忆、会话、技能、cron 任务与 `state.db`；创建后自动获得同名命令别名（`coder chat`、`coder gateway start`）。每个 profile 的 gateway 是独立进程、独立 bot token、可安装为独立 systemd/launchd 服务；官方 Docker 镜像内按 profile 由 s6 监管。

**为什么它不改变信任结构。** 官方文档对边界的表述很直接：

- **「Profiles do not sandbox the agent.」** local backend 下 agent 仍拥有与 OS 用户相同的文件系统权限，profile 不阻止它访问 profile 目录之外的文件。
- 隔离粒度是**目录级状态隔离**：N 个 profile = N 个进程 × N 份状态 × N 套凭据；没有请求级主体概念，`user_id` 这一层不存在。
- 认证只有入口级控制：bot token 与消息 allowlist（`GATEWAY_ALLOWED_USERS` 等）决定「谁可以给我的 bot 发消息」，不是租户授权面；没有按租户的配额、计费、审计面。

**对四条结构性变化的对照：**

| 结构性变化 | Hermes profiles |
|---|---|
| 身份显式化 | 无：无请求级主体；认证只到 bot token / 消息 allowlist 的入口级 |
| 信任主体换人 | 无：单一操作者拥有全部 profile，审批仍是本人 |
| 状态共享 | 半条：state 按 profile 目录隔离，但仍是本地单机单库（per-profile `state.db`），无共享存储 |
| 执行托管 | 无：gateway 是常驻进程，不是带租户隔离的托管平台；官方明确不做沙箱 |

**它比普通 coding harness 多了什么。** 常驻服务（systemd/launchd/s6）、消息平台入口、multiplexed gateway（一个 gateway 路由多个 profile）、profile 分发包（profile distributions，按版本分发整个 agent）、桌面 Bot Mode 名册——这些是**交付形态**要素，不是租户架构要素。

**判定**：Hermes 是 PersonalAgent 的产品化极致形态——常驻、多渠道、可容器化、多实例，但多实例的服务对象仍是同一操作者。社区确有「profile + 容器 + 看板」拼多客户部署的玩法，其模式恰是：缺失的控制平面（沙箱、授权、可观测）全部靠外部自建——这佐证而非反驳「profiles ≠ 多租户」。

## 五、缺口全景与选型含义

### 5.1 三方缺口表

| BusinessAgent 需要的面 | Agno 3.1.12 | Pi Core（快照） | Hermes |
|---|---|---|---|
| 身份与多用户原语 | 有（`user_id` 一等参数） | 无 | 无（profile ≠ 主体） |
| 共享持久化 | 15 种后端 | 原语，存储自接 | 无（每 profile 本地库） |
| 服务化外壳（API/RBAC/调度） | AgentOS 接近配置级 | 无，声明为宿主职责 | 部分：常驻 gateway 与消息入口，无 RBAC 面 |
| 执行隔离契约 | 沙箱后端 + AgentOS 归属 | 无（策略钩子可表达，执行体自建） | 无（官方明文不做沙箱） |
| per-request 治理接缝 | **无**（run 级钩子 + 内部 `after_tool_results`） | **有**（`transformContext` 公共 API） | 未考证 |
| 工具级策略钩子 | `tool_hooks` | `beforeToolCall`/`afterToolCall` | 未考证 |
| 成本上限与归因 | usage 字段齐全，无上限面 | 无 | 无 |
| 统一策略源 | 无（五处分散） | 无（但接缝处可单点自建） | 无 |

### 5.2 选型含义

| 目标 Business 形态 | 主导的一半 | 结论 |
|---|---|---|
| 常规 SaaS：多租户 API 服务、身份计费隔离为主、会话短、成本不敏感 | 身份/存储/外壳 | **Agno 起点显著更高**：外壳与隔离原语接近配置级，增量是常规服务端工程；Pi 需先自建半个平台 |
| 治理密集：长会话、成本可控、按租户预检与熔断 | 执行治理缝 | 两者都要补：Agno 的补法是改框架或外建循环（跟随上游成本高），Pi 的补法是写策略 + 自建身份存储外壳（工作量大但都在自己地基上） |
| 两半并重（大多数真实业务） | — | 差距趋同；选择依据变成语言生态与退出成本（Agno 的 Team/Workflow/AgentOS 锁定深，Pi 是纯库退出易） |
| 少量客户、自托管、可接受实例级隔离 | 交付 | Hermes profile-per-client + 容器墙是社区现状：隔离靠容器而非平台，每客户一份运维与凭据，规模不经济 |

**对「哪个更容易长成多人共用服务」的回答**：Agno 明显更容易，且原因可以精确到半边——它赢在迁移中无法自建捷径的那半边（身份/存储/外壳）；它输的那半边（执行缝）在两种形态里都要付代价，不构成差距。Hermes 与 Pi 属于同一家族：交付形态更成熟（Hermes）或执行缝更开放（Pi），但都没跨过信任结构这条线。

## 六、证据与限制

**实证（高置信）**

- Agno：安装包源码 `agno 3.1.12`——`session/agent.py:27`（`user_id` 字段）、`db/base.py:558-606`（user_id 作用域的会话操作）、`os/authz/`（RBAC role store 与 admin router）、`sandbox/`、`os/` 的 auth 与路由；逐项细节以源码为准，2026-11-19 复核。
- Pi：源码快照（2026-09-11）的分层边界与 `AgentOptions` 钩子面（`packages/agent`、`packages/coding-agent`）。
- omp 19.0.0：GitHub 发布说明（v18.22.2...v19.0.0，2026-11-19），引用点为 per-user daemon、`local://`、`task.crossInstance` 同目录边界、包名 `@oh-my-pi/pi-*`。
- Hermes：官方文档 `hermes-agent.nousresearch.com` 的 Profiles、CLI、profile-distributions、multi-profile-gateways 页面与 GitHub README，2026-11-19 读取；引用句「Profiles do not sandbox the agent」为官方原文。

**限制**

- Agno 结论绑定 3.1.12；文档站可能领先安装版本，升级后须复核第三节两表。
- Pi 结论基于快照，「文档未涉及」不等于「实现不存在」；omp 与快照的包结构已随版本演化，跨版本引用须注意。
- Hermes 只做了官方文档级考证，未做源码级核对；其治理钩子面在多租户视角下的能力标「未考证」，不作断言。
- 「常规 SaaS / 治理密集」的两分是结构性判断，非工时估算；实际迁移成本随团队对框架内部的熟悉程度与业务合规要求变化。
- 本篇为调研，不构成实现基线；结论需转述进设计文档才对实现生效。
