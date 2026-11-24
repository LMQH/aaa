---
name: create-workflow
description: 按 Agno 原生编排创建可试跑、可复用的 Workflow 定义，明确步骤、数据流、分支与并行。
---
# 创建可委派的 Workflow

1. 写清流程的输入、最终结果和成功条件。把必须执行的步骤及条件固定在流程定义中；只把需要判断的工作交给执行对象，不让模型临场改变流程结构。
2. 每个执行步骤只选原生 Agno `Agent`、`Team` 或 `Workflow`。引用须能由宿主解析且经授权；为当前任务新设计 Agent 或 Team 时，分别调用 `get_skill_instructions(skill_name="create-agent")` 或 `get_skill_instructions(skill_name="create-team")` 产出依赖定义草案，列明依赖，不能把草案当成已注册引用。复用其他 Workflow 时检查引用链，不能形成循环。
3. 按顺序列出 `steps`，用本次输入和 Agno 的前序输出传递数据。执行步骤形如 `{"kind":"agent","name":"inspect","ref":"reviewer"}`；Team 和 Workflow 对应 `team`、`workflow`。`{"kind":"parallel","name":"checks","steps":[...]}` 只用于彼此独立的步骤；`{"kind":"condition","name":"gate","after":"inspect","equals":"通过","then":[...]}` 比较前序输出，可附 `"else"` 步骤列表。步骤名不得重复；不嵌入可执行代码。
4. 交付同一份数据定义草案：名称、输入输出、步骤及引用、分支和失败行为。临时使用与持久化复用同一份定义；是否保存是独立动作，不把运行进度或当次审批写进定义。

例如：检查 Agent 输出通过与否；通过才调用评审 Team，未通过就返回检查原因；需要共用的检查段可以引用另一条 Workflow。步骤推进归 Agno，不归主会话。

用 `list_delegations` 查已保存引用（写作 `public:名称`）。把现场依赖一并放进 `{entry, definitions}`；入口定义是 `{"kind":"workflow","steps":[...]}`。调用 `register_delegation` 后再用 `delegate_task` 试跑；已保存对象不需重新注册。

如果流程下次还有明确用途，说明复用价值，再用 `save_delegation(definition=原定义)` 提请确认；一次性流程直接保持临时，不询问。用户明确要求持久化时跳过价值判断，但仍需确认具体定义及私有依赖。
