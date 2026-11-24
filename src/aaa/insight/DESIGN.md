# insight — 上下文洞察（只读）

## 边界与目标

只读采集当前模型窗口与上下文构成，输出全局分段 token 估算表；不装配执行体、不实例化记忆引擎、不写库、不改 settings。CLI 的 `/insight` 与将来的状态条诊断共用本模块；渲染留在 `cli`。

状态：计划（尚未实现）。代码落地后本文件转为现状说明，实现批次与验证证据记在 HANDOFF。

## 拥有者与不变量

- **只读复用**：各段内容必须经 `runtime.prompt`、`tools.assemble` 等现有装配函数的同一批入口生成，禁止复制拼装逻辑；只读取已加载对象与库内数据。
- **口径一致**：计数复用 Agno 的 `agno.utils.tokens`（压缩阈值同源），不另造分词器；本地计数一律标注 ≈，权威值只取 Agno run 的 usage。
- **以视窗投影为对象**：按压缩、截断后的实际送模型形态计数，不按条目流或产出端原始尺寸。
- **不重复计数**：技能索引、记忆注入、压缩摘要各自成行；基础提示词行只标记它们的位置与指针。
- **成本边界**：单次命令调用内完成；大段文本按内容哈希缓存，超长文本不进缓存。

## 分段口径

按上下文治理方案的十层编号（1–10）展示；实现未拆分的层按整段计，缺席的层显示「无」。

| # | 类目 | 当前实现来源 | 状态 |
|---|---|---|---|
| 1 | 基础提示词 | `runtime.prompt`：`SYSTEM.md` 内置/覆盖渲染体 + `APPEND_SYSTEM.md` | 现行（实现未拆六模块，整段计） |
| 2 | 内置工具 schema | `tools.assemble` 内置面 | 现行 |
| 3 | 自定义工具 schema | 插件 / MCP 工具（含 `mcp_` 前缀） | 现行 |
| 4 | 技能索引 | Agno `Skills.get_system_prompt_snippet()`（`<skills_system>`），随系统消息发送 | 现行 |
| 5a | 记忆注入 | 记忆工具清单与 `prompt_block` 已并入第 1 类（指针，不重复计数）；召回块运行期注入、不落史 | 现行 |
| 5b | 压缩摘要 | 历史中 `from_history` 的摘要消息（Agno 压缩投影） | 视实际存在 |
| 6 | 用户消息 | Agno 消息 `role=user` | 现行 |
| 7 | 工具调用 | assistant 消息内 `tool_calls` | 存在时 |
| 8 | 工具结果 | `role=tool` 消息 | 存在时 |
| 9 | 推理 | `Message.reasoning_content` / `redacted_reasoning_content` | 条件：供应商与配置决定是否入史 |
| 10 | 助手回复 | assistant 文本（扣除工具调用与推理） | 现行 |

注：记忆块物理上可能并入 system 提示词发送，仍按来源归入第 5 类；第 1 类行只留指针，不重复计数。TODO 清单不注入上下文（只经工具与 `/todos` 查看），不单列。

## 对外接口（计划）

- `collect(state, ...) -> InsightData`：模型窗口、最近一次 run 的权威 usage、十层各行（名称、估算 token、来源摘要）。签名与数据结构待代码落地时确定。
- 计数：UTF-8 字节数预筛（明显低于窗口时不精确计数）→ `agno.utils.tokens` 计数 → 模块内 LRU（按内容哈希）。
- 展示：`/insight` 无参数，输出全局表；不支持按段过滤。运行中只读放行（与 `/memory` 状态一致），不弹菜单、不改状态。

## 依赖

`insight → model, config, tools, sessions, memory, plugins, runtime.prompt`（只读）；`cli → insight`。禁止反向依赖；不 import `miniviking`。

## 展示口径（已定）

- 表脚注显示实际使用的分词器名，来源链：HF tokenizer → tiktoken → 字符启发式。
- 模型窗口缺失或为 0 时，占比列显示「未知」并留空，不伪造。
- 第 5 类分两行：记忆注入、压缩摘要，各自标注来源。
- 估算值一律标 ≈；权威 usage 单独列出，不与估算混算。
