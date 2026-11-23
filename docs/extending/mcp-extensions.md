# MCP 扩展

一个目录接一个外部 MCP server。扩展只做声明，不写代码。

## 位置与结构

```
~/.aaa/extensions/<插件名>/
    extension.toml      必需
    .disabled           可选；存在即停用
```

插件名取目录名，仅用于 `/plugin` 展示与启停。启动时扫描一次，改完要重启。

## 本地 stdio server

```toml
[mcp]
command = "npx -y some-mcp-server --port 8080"
env = { API_KEY = "..." }
```

- `command`：完整命令行，按 shell 语义拆分执行。
- `env`：可选，只作用于该 server 进程。
- 凭据建议由用户自己填，不写进对话记录。

## 远程 server

```toml
[mcp]
url = "https://example.com/mcp"
transport = "streamable-http"
```

`transport` 可选值为 `streamable-http` 或 `sse`；本地 stdio 不写该键。

## 启停

- `/plugin` 打开列表，选中即切换；`/plugin <名>` 直接切换。
- 启停只增删 `.disabled` 标记文件，**重启后生效**。
- 列表里 `●` 为启用，`○` 为停用。

## 运行期行为

- 工具名统一加 `mcp_` 前缀（超过 64 字符会截断）；来源可据此识别。
- MCP 工具属于外部信任面：**每次执行都要求确认**，审批策略不会自动放行。
- 连接失败的插件只记日志，不阻断启动；失败名单会在启动日志中出现。
- 连接与单次调用的超时共用 `extensions.mcp_timeout`（默认 120 秒），可用 `/config extensions.mcp_timeout <秒>` 调整。该键为即时档：值立即保存；已建立的 MCP 工具在下次重建 Agent（如切换模型）或重启后采用新值。

## 校验

- TOML 必须能被解析，且 `[mcp]` 段里至少有 `command` 或 `url`；否则启动时被忽略。
- 让 agent 创建时会先解析 TOML 再写盘，缺 `[mcp]` 或两者都没有会被拒绝。

## 创建方式

- 让 agent 调用 `create_plugin(kind="extension", name=..., content=...)`，每次写入需要确认；
- 或直接建目录写文件。

## 排查

1. `/plugin` 里看不到：检查目录名与 `extension.toml` 文件名，以及 TOML 是否有 `[mcp]` 且含 `command`/`url`。
2. 看到但工具不可用：看启动日志里的连接失败名单；常见原因是命令不存在、凭据缺失、网络不可达。
3. 工具存在但每次都被打断：这是预期行为，MCP 工具必须逐次确认。
