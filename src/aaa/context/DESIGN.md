# context — 模块设计

## 边界与目标

上下文压缩的参数与派生规则。只提供构造 Agno `Compaction` 所需的取值与函数，不持有会话、不执行压缩——压缩触发与折叠由 Agno 完成。

## 拥有者与不变量

- 所有取值来自 `config.toml`：`context.history_runs`、`context.compaction_budget`、`context.threshold_ratio`、`context.threshold_reserve`。
- 尾轮数的含义：设了压缩阈值后，压缩接管回放窗口，该值不再限制回放轮数，只决定折叠时保留多少尾轮原文。
- 阈值规则 `compact_threshold(window)`：`window - max((1-ratio)×window, reserve)`，下限 1。Agno 要求阈值为正整数，所以预留量吃掉整个窗口时退到 1 而不是 0。
- 阈值只有一个来源：`runtime.build_agent` 用它装配 `Compaction`，`cli/app` 的状态条用同一个函数画预留标记。

## 对外接口

- `history_runs(settings)`、`compaction_budget(settings)`、`compact_threshold(window, settings)`。
- `settings` 是只读接口（`get(path)`），本模块不 import `aaa.config` 的具体实现。


