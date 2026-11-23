# sessions — 模块设计

## 边界与目标

会话查询、命名与标题生成，是 Agno `SqliteDb` 的薄封装。不拥有会话内容（Agno 写库），只回答两件事：哪个会话是本目录最近的、「-c/-r」该续接哪个；会话标题怎么来。

## 拥有者与不变量

- 会话 id 由 `new_session_id(settings)` 生成：uuid hex 前 `sessions.id_length` 位，短且可读。
- 「本目录最近会话」由库内索引表 `aaa_cwd_session(cwd PK, session_id, seq)` 维护，与 Agno 的会话表同库。`seq` 单调递增，用来在 `updated_at` 只有秒级精度时仍能定序；主键查询走 B-tree，等价于按键取值的索引。
- 会话名称格式 `"{title_prefix} {TAG: summary}"`：`TAG` 为 3–5 位大写 ASCII，摘要不超过 12 词；标题截断长度取 `sessions.title_length`；生成失败返回空串，不报错、不改名。
- 已命名的会话不重复生成（进程内 `named` 集合 + `get_session_name` 双重判断）。
- 会话内容与排序完全复用 Agno 的数据库接口；本模块只额外拥有索引表 `aaa_cwd_session` 这一小块数据，不缓存会话列表。
- 会话列表、新建、续聊与导出都以 Agent 会话为范围。

## 对外接口

- `new_session_id(settings)`、`remember_session(cwd, session_id)`、`list_sessions(runnable, limit)`。
- `latest_session_for_cwd(runnable, cwd, settings)`：仅续接 Agent。索引命中 Agent 则使用它；否则回落到 `metadata.cwd` 匹配的 Agent 记录。索引实现见 `sessions/index.py`。
- `find_session(runnable, prefix, settings)`：完整 id 优先，其次前缀；多个前缀命中取最近的一个。
- `session_label(session)`：标题 · id · 更新时间。
- `generate_title(channel, model, text, max_length)`：经 `model.build_title_model` 构造不带重试的模型，一次非流式 `response`；输出清洗为 ASCII 起始、按配置截断。
- `ensure_session_name(..., settings)`：首次发言后命名，所有失败静默返回。


