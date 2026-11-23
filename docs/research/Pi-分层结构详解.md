# Pi 分层结构详解

日期：2026-09-11  
范围：本机 Pi 源码 `/Users/lmqhx/code_space/pi`。

## 结论摘要

Pi 不是一个单体产品，而是从「模型访问」到「通用 Agent 运行时」再到「编码 Agent 应用」的分层组合。最实用的理解是：

```text
应用/产品层：你自己的 Agent、CLI 或 Web 服务
        │
Pi coding-agent：会话、资源发现、内建编码工具、CLI/TUI/RPC、扩展
        │
Pi agent-core：Agent 循环、工具执行、状态、消息、上下文、事件
        │
Pi ai：多 Provider 模型目录、鉴权、请求/流式响应适配
        │
模型 Provider：OpenAI / Anthropic / Google / 自定义兼容端点等
```

因此，创建一个「Pi Agent」有两种不同层次：

1. 直接使用 `pi-agent-core` 的 `new Agent(...)`，自行提供模型流函数、状态、工具与上下文策略；
2. 使用 `pi-coding-agent` 的 `createAgentSession(...)`，获得会话持久化、资源加载、技能、扩展、默认工具和模型运行时。

你提到的 `AgentCfg` 不是本版 Pi 公共 API 的类型名。在 Pi SDK 中，最接近的入口是 `CreateAgentSessionOptions`、`DefaultResourceLoader` 和 `AgentOptions`。

## 1. 仓库与包边界

| 层 | 包 | 解决的问题 | 不负责的事情 |
|---|---|---|---|
| 模型适配 | `@earendil-works/pi-ai` | 统一多模型 Provider、模型描述和流式调用 | Agent 循环、会话、CLI |
| 通用 Agent 运行时 | `@earendil-works/pi-agent-core` | LLM 回合循环、工具调用、上下文转换、状态、事件、通用 session/harness 原语 | 编码工具实现、终端 UI、Pi CLI 协议 |
| 编码 Agent 应用 | `@earendil-works/pi-coding-agent` | 编码工具、会话管理、上下文压缩、资源发现、扩展、技能、CLI/TUI/RPC | 业务平台编排、租户、队列、审计、授权中心 |
| 产品宿主 | 自定义应用、CLI 或 Web 服务 | Agent 身份、业务任务、授权、调度、可观测性、多 Agent 编排 | 不应复制 Pi 的底层事件循环 |

Pi 根 README 对前三层的定义分别是「Unified multi-provider LLM API」「Agent runtime with tool calling and state management」「Interactive coding agent CLI」。

## 2. pi-ai：模型和 Provider 适配层

### 职责

`pi-ai` 把不同 Provider 的请求、认证方式、流式输出及模型元数据统一为 TypeScript API。`pi-agent-core` 不绑定特定模型供应商；它接收一个 `StreamFn`，而 `Models.streamSimple` 是一种可满足该契约的实现。

### 关键边界

- 模型对象包含 Provider、模型 ID、上下文窗口、费用、能力等元数据；
- `StreamFn(model, context, options)` 返回流式 Assistant 事件；
- 供应商/API 故障应编码在流事件和最终消息中，而不是让底层 Agent 循环直接崩溃；
- Pi 的 Provider 统一性不意味着所有 Provider 的工具、思考等级或鉴权能力完全相同，coding-agent 会按模型能力做约束。

### 对二开的意义

如果只是把 RAG 的检索结果注入 Prompt，无需改动 `pi-ai`。只有需要新模型供应商、特殊鉴权、私有网关协议或新的流式传输时，才应在此层扩展。

## 3. pi-agent-core：通用 Agent 运行时层

### 3.1 核心对象

`Agent` 是此层的中心对象。构造参数 `AgentOptions` 允许注入：

- `initialState`：系统提示词、模型、消息、工具、思考等级；
- `streamFn`：模型调用实现；
- `convertToLlm`：将应用消息转成 Provider 可理解的 LLM 消息；
- `transformContext`：在每次模型调用前裁剪、汇总或注入上下文；
- `beforeToolCall` / `afterToolCall`：工具执行前后的策略钩子；
- `shouldStopAfterTurn`、`prepareNextTurn`：回合级停止或状态替换策略；
- 队列模式、工具并行/串行执行、transport 与思考预算。

### 3.2 Agent 循环

一次 `agent.prompt()` 的典型路径为：

```text
用户消息
  → agent_start / turn_start 事件
  → transformContext（可选）
  → convertToLlm（必经）
  → streamFn 调用模型
  → assistant 流式事件
  → 若模型请求工具：校验、beforeToolCall、执行、afterToolCall
  → toolResult 消息
  → 下一回合或 agent_end
```

工具可以串行执行，也可以在完成预检后并行执行。`agent-core` 只定义工具和调度语义，不提供「读文件、执行 shell、编辑代码」这些编码领域工具本身。

### 3.3 三类消息与应用扩展消息

模型只直接理解 `user`、`assistant`、`toolResult`。Pi 允许应用借由 TypeScript declaration merging 添加自定义消息类型，例如 UI 状态、业务事件或审计记录；之后由 `convertToLlm` 决定该消息应：

- 被转换为模型可读上下文；
- 被过滤，仅供应用 UI 或存储使用；
- 被替换为摘要。

这使 RAG、外部记忆、业务状态可在不污染 Provider 层的情况下进入 Agent 循环。

### 3.4 Context 是 RAG 的自然插入点

`transformContext(messages, signal)` 在每次模型调用前运行，适合：

1. 从当前用户问题提取检索 query；
2. 请求向量库、全文索引、知识图谱或业务 API；
3. 按 token 预算筛选、压缩和引用检索结果；
4. 以专用上下文消息或用户消息插入结果；
5. 再调用 `convertToLlm`。

该方案适合「自动背景知识注入」。若检索应由模型显式决定，则将检索封装为 `AgentTool` 更合适。两者可并存：前者提供低成本的常驻上下文，后者提供按需深查。

### 3.5 core 中的 harness/session 范围

当前 core 还导出 session、compaction、skills、system prompt、harness runtime、telemetry 等通用原语。它不是只包含一个 while-loop；但它仍不是 Pi CLI，也没有规定一个面向外部进程的统一「Pi 协议」。

## 4. pi-coding-agent：执行应用、CLI 与 SDK 层

### 4.1 AgentSession

`AgentSession` 把低层 `Agent` 包装为编码 Agent 的会话运行时，补齐：

- JSONL 会话持久化与恢复；
- 设置、模型选择与模型切换；
- 上下文压缩；
- 内建工具和自定义工具注册；
- Prompt 队列、steer/follow-up 行为；
- 扩展事件与命令；
- CLI 的 TUI、print、RPC 三种运行模式共用的业务会话层。

所以「执行层」不能只等同于 CLI：`pi-coding-agent` 也是可嵌入的 Node/TypeScript SDK。Node 应用应优先直接使用 `AgentSession`；非 Node 宿主可启动 CLI 的 RPC mode。

### 4.2 createAgentSession：产品侧最常用的创建入口

`createAgentSession(options)` 的主要配置为：

| 需求 | 对应配置/组件 |
|---|---|
| 工作目录和全局 Pi 目录 | `cwd`、`agentDir` |
| 模型、思考等级、模型切换范围 | `model`、`thinkingLevel`、`scopedModels` |
| 默认工具控制 | `tools`、`excludeTools`、`noTools` |
| 加入业务工具 | `customTools` 或 extension 的 `registerTool()` |
| 身份/系统提示词 | `DefaultResourceLoader.systemPromptOverride` 或附加提示词 override |
| 技能集 | `DefaultResourceLoader.skillsOverride`，或约定目录自动发现 |
| 扩展 | `additionalExtensionPaths`、`extensionFactories` 或约定目录自动发现 |
| 会话与设置存储 | `sessionManager`、`settingsManager` |
| 模型认证与目录 | `modelRuntime` |

`AgentSessionConfig` 是 Session 已被创建后内部依赖的完整配置，不是建议由业务代码直接手工组装的稳定高层 API。

### 4.3 资源加载层

`DefaultResourceLoader` 负责发现和加载：

- extensions；
- skills；
- prompt templates；
- themes；
- context files；
- system prompt 及其附加内容。

它支持 `systemPromptOverride`、`appendSystemPromptOverride`、`skillsOverride`、`extensionFactories` 等显式注入。因此「Agent 身份提示词、工具配置、技能配置」这个理解是对的，但在 Pi 中它们并非集中在一个名为 `AgentCfg` 的单一结构体里。

### 4.4 内建工具

coding-agent 实现了编码场景所需的工具，如 read、bash、edit、write、grep、find、ls、PowerShell。默认活跃的内建工具为 read、bash、edit、write；调用方可收紧 allow-list 或 deny-list。

这些工具是 coding-agent 的应用层功能。若你把 Pi 用作非编码业务 Agent，通常应关闭默认编码工具，再注册业务工具或仅保留只读工具。

### 4.5 三种进程模式

| 模式 | 用途 | 主要接口 |
|---|---|---|
| TUI/interactive | 人直接在终端工作 | `pi` |
| print | 一次性非交互任务 | `pi -p` |
| RPC | 外部非 Node 宿主控制 Pi 进程 | `pi --mode rpc`，严格 LF 分隔 JSONL |

RPC 是 coding-agent 定义的进程边界协议。它不属于 `pi-agent-core`。另一方面，`--mode json` 是 print 模式输出 Agent 事件流的 CLI 约定，也同样在 coding-agent 层。

## 5. 扩展、Skills 与 MCP 的边界

### 5.1 官方的取舍

Pi coding-agent 的官方理念是核心保持小，功能通过 extension、skill 和 package 组合。默认不内置 MCP；官方明确允许「写一个 extension 来添加 MCP 支持」。这不是「Pi 不支持 MCP」，而是「MCP 不在基础发行物的固定核心中」。

### 5.2 Extension 能做什么

Extension 可订阅 Agent/会话事件、注册自定义工具、注册命令、改写工具行为、增加 UI 交互，并读取宿主提供的配置。它是把外部能力接入 Pi 的主要运行时接缝。

MCP bridge 的常见流程：

```text
Pi extension 启动
  → 创建 MCP client transport
  → tools/list
  → schema 转换为 Pi 工具参数定义
  → pi.registerTool(...)
  → LLM 像调用原生 Pi 工具一样调用 MCP 工具
```

### 5.3 Skills 的职责

Skill 是面向模型的说明/工作流资源，而不是 transport 或授权机制。它适合表达「如何使用某个领域能力」，不应承担保存密钥、发放 capability、编排多 Agent 或实施业务权限判断。

## 6. 推荐的二开落点

### A. 纯 Pi SDK 的领域 Agent

适用于 Node/TypeScript 服务直接托管 Agent。

```text
createAgentSession
  + DefaultResourceLoader（身份、skills、extensions）
  + customTools（业务能力）
  + transformContext（受 token 预算约束的 RAG）
  + SessionManager（会话）
```

### B. RAG 的分层建议

| 需求 | 建议落点 |
|---|---|
| 每轮自动补充少量可信上下文 | `agent-core.transformContext` 或 coding-agent session 的等价上下文层 |
| 模型按需深度检索 | Pi 自定义工具或 MCP bridge |
| 多租户文档权限、capability、审计 | 外部控制平面，不放进 Prompt |
| 向量化、索引构建、重排序和缓存 | 独立 RAG 服务；Pi 只消费结果 |
| 检索答案质量评估 | 平台 eval/observability 层，记录 query、来源、采纳和反馈 |

## 7. 证据索引

Pi 源码：

- `/Users/lmqhx/code_space/pi/README.md`：包边界与项目定位；
- `/Users/lmqhx/code_space/pi/packages/agent/README.md`：Agent、消息流和事件流；
- `/Users/lmqhx/code_space/pi/packages/agent/src/agent.ts`：`AgentOptions` 与 `Agent`；
- `/Users/lmqhx/code_space/pi/packages/agent/src/types.ts`：`AgentLoopConfig`、工具与上下文钩子；
- `/Users/lmqhx/code_space/pi/packages/coding-agent/src/core/sdk.ts`：`CreateAgentSessionOptions` 与创建流程；
- `/Users/lmqhx/code_space/pi/packages/coding-agent/src/core/resource-loader.ts`：系统提示词、技能和 extension 注入；
- `/Users/lmqhx/code_space/pi/packages/coding-agent/README.md`：扩展哲学与「No MCP」的原意；
- `/Users/lmqhx/code_space/pi/packages/coding-agent/docs/rpc.md`：RPC JSONL 协议。

## 限制说明

本文基于当前工作区版本 `@earendil-works` 命名空间的 Pi 源码；API 和 CLI 事件字段会随上游版本变化。
