# usage — 用量与花费

## 边界与目标

把 Agno 已写好的 run metrics 换算成「渠道 × 模型」的 token 与美元，并用独立账本
冻结历史花费。只读 `sessions.db`，只写自己的 `usage.db`；不进入运行路径
（`docs/architecture.md` 铁律 2）：不做预算拦截、不做在线估算。

## 拥有者与不变量

- **tokens 不重记**。来源只有 `agno_runs.run_data['metrics']`：顶层是 run 汇总，
  `details` 按 Agno 的 `ModelType`（`model` / `compaction_model` /
  `session_summary_model` / `memory_model` …）分组。没有 per-request 数据，也不去造。
- **单价归价目表，金额归账本**。价目表在 `secret.yaml` 的
  `providers.<id>.models[].cost`（美元 / 百万 token），是
  `(渠道, 模型)` 的属性；`usage` 通过注入的取价函数读取，不认识 `Catalog`。
- **成本在入账时算好并冻结**。改价不影响历史，也没有重算命令；只有未定价的行会
  在后续同步时被补算一次（只补空，从不改写已有金额）。
- **账本单独一个文件**（`~/.aaa/data/usage.db`）：它是派生数据，删掉能重建；
  与 Agno 的表不共享事务，也不在运行期争 `sessions.db` 的写锁。
- **只有终态 run 入账**：`COMPLETED` / `CANCELLED` / `ERROR` / `REGENERATED`。
  `PAUSED` / `RUNNING` 的 metrics 还在长，入账会得到半份数字。
- **幂等**：主键 `(run_id, model_type)`；同步时对同一 run 先删后插，并以
  `run_updated_at` 判断是否需要重放（`continue_run` 会更新同一 run 的 metrics）。
- **未知不猜**：渠道未知、模型没配价、缺少 metrics，一律记为未定价/未入账并计数，
  不用默认价兜底、不补零。

## 渠道归因

Agno 的 run 只记 provider 类名（`"OpenAI"`），区分不了同一模型在两个渠道上的价。
因此运行路径在 `agent.run(..., metadata={"channel": <渠道 id>})` 里记下渠道；
账本按 `(channel, model_id)` 计价。没有该字段的历史 run 渠道为 `NULL`，永久未定价。

## 数据

`usage_events`（主键 `(run_id, model_type)`）：渠道、模型、会话、cwd 快照、
`created_at`（UTC 秒）、`run_updated_at`、四类 token + reasoning、`cost_total`。

报表只从账本聚合，不回查 `sessions.db`；`cwd` 与 `created_at` 都是入账时的快照，
目录改名或会话删除不改变历史归属。

## 对外接口

- `cost.cost_of(cost, input, output, cache_read, cache_write, at) -> float | None`：
  纯函数；`at` 为本地时间，用于峰谷乘数。
- `cost.multiplier(cost, at) -> float`：低谷窗口内返回 `off_peak.multiplier`。
- `ledger.sync(price, sessions_db, usage_db) -> SyncResult`：增量入账 + 补算。
- `report.report(period, now, usage_db) -> Report`：按天/周/月聚合，行按
  `输入 + 输出` 降序；`Report` 自带合计与未定价计数。
- 呈现归 `cli`：`/usage`（今天）、`/usage week`、`/usage month`。

## 已定口径

- **金额单位**：美元。网关按人民币报价时由用户换算后填入。
- **合计 token**：`输入 + 输出`（与 Agno `total_tokens` 同义）；缓存读单列，
  不并入合计，避免与「prompt_tokens 已含缓存」的厂商口径重复计数。
- **周期**：自然日 / 自然周（周一起）/ 自然月，本地时区。
- **峰谷价**：`cost.off_peak.{multiplier, windows}`，窗口写本地 `HH:MM`，允许跨午夜；
  窗口由用户按渠道声明，不内置任何厂商规则。
- **角色**：不单独成行，合并进 `(渠道, 模型)`；`model_type` 留在账本里，将来要分
  角色时不用重建表。

## 已知限制

- `cache_write_tokens` 只有部分厂商上报（OpenAI 兼容端点通常没有），该列可能恒为 0。
- 渠道字段上线前的历史 run 永久未定价，这是有意的——猜渠道会算错价。
- 金额精度按 `REAL` 存，单条小到 1e-6 美元级；展示时按量级自适应小数位。
