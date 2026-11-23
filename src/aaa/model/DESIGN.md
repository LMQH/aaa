# model — 模块设计

## 边界与目标

模型身份与 Agno 模型实例的唯一来源。`provider` 定义渠道值对象与构造点，`catalog` 提供渠道目录、可切换范围与引用解析。只管「有哪些模型、怎么造」，不管「什么时候换成谁」（`cli/state`）与「agent 怎么装配」（`runtime`）。

## 拥有者与不变量

- 模型身份是 `ModelRef(provider, model_id)` 一对，不拼字符串；显示与输入都是 `provider/model_id`。
- `Channel` / `ChannelModel` 是冻结值对象；`api` 是协议名（`deepseek`、`openai-completions`），不是 Agno 类名。
- `API_CLASSES` 是协议名到 Agno 模型类的唯一映射，新增协议只改这张表。
- `build_model` / `build_title_model` 是唯一构造点。主模型的 `timeout` / `retries` / `retry_delay` 来自配置（`model.timeout`、`model.retries`、`model.retry_delay`），不写在渠道字段里；标题模型不带重试。
- `Catalog` 复制入参渠道表，渠道只从它枚举。
- fallback 只取同渠道其余模型，数量上限来自 `model.fallbacks`；跨渠道降级不隐式发生。
- `ChannelModel.context_window` 是必填整数；渠道未声明时由 `model.default_context_window` 补齐（归一与 `from_config` 都读同一键）。

## 对外接口

- `from_config(config, settings) -> Catalog`：配置在构造时绑定（窗口默认、fallback 上限、模型超时），换配置要重建 Catalog。
- `Catalog.require / channels / channel / refs / scope / resolve / window / build / title_model / fallbacks`（`build` / `fallbacks` 用构造时绑定的 settings）。
- 解析顺序：整串精确 → 已知渠道 id 最长前缀 → 裸 id 唯一匹配。前缀命中但模型不存在时直接 `NotFound`，不再回落到裸 id 匹配。
- 失败语义：`require` 抛 `ValueError`，消息面向用户；`resolve` 不抛，返回 `Resolved / Ambiguous / NotFound`，多候选一并返回。
- `scope(patterns)`：模式用 `fnmatch` 同时匹配 `provider/model_id` 与裸 `model_id`，结果去重；模式为空表示全部。
- `resolve(text, pool)` 的池子默认全量，`/model` 传入可切换范围。


