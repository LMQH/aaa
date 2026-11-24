---
name: create-team
description: 创建 Agno Team 定义：设计成员职责、原生协作模式和团队交付物，支持按任务现场定义成员。
---
# 创建可委派的 Team

1. 写清团队目标，以及为什么需要成员之间分工、路由或独立对照。若只需主 Agent 派发互不相关的任务，建议批量委派，不借 Team 重做一次分工。
2. 四种原生模式：`coordinate` 由 leader 分工并汇总；`route` 选择一位成员并返回其结果；`broadcast` 给所有成员同一任务（同步成员不并行）；`tasks` 由 Team 维护任务清单、推进依赖并汇总。
3. 按任务选用已有成员，或现场定义新成员。现场定义前调用 `get_skill_instructions(skill_name="create-agent")`，复用其职责、指令、输入与交付格式；新成员可直接写入 Team 草案，不必先注册成独立资产。团队 leader 与成员的模型、工具与权限都只能选宿主允许的项，不能从成员拼出额外权限。
4. 交付定义草案：名称与团队目标、协作模式与选择理由、leader 的职责、成员引用或内联定义、团队输入、各成员交付与团队最终结果。说明最终结果如何反映成员意见冲突或交付缺失，不编造 Team 原生模式没有的执行保证。

例如：两位审阅者分别查兼容性和回归风险，由 `coordinate` 的 leader 合并证据并标明未覆盖项。若要两人独立审阅同一问题，改选宿主开放的 `broadcast`。

把 Team 写成 `definitions` 中的 `{"kind":"team","mode":"coordinate","instructions":"领队指令","members":["reviewer","public:tester"]}`；`reviewer` 是同包里的 Agent 定义，`public:tester` 是已保存的 Agent。领队可选 `description`、`model`、`tools`，约束与 Agent 相同。根入口例如 `{"entry":"review-team","definitions":{...}}`；注册、试跑及确认保存沿用 `create-agent` 的流程。只保存 Team 时，包内成员不会单独公开。
