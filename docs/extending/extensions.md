# 编程式扩展

一个目录就是一个扩展：`extension.toml` 声明类型，`extension.py` 导出 `activate(ctx)`。
扩展只看到 aaa 的协议接口，不接触 Agno 执行路径；aaa 负责把它翻译到装配点。

可运行的完整示例见 [`examples/hello/`](examples/hello/)（也在测试里跑，不会腐烂）。

## 目录与清单

```text
~/.aaa/extensions/<name>/          用户级（不问仓库信任）
<仓库>/.aaa/extensions/<name>/      项目级（需仓库信任）
    extension.toml
    extension.py
```

```toml
# extension.toml
[extension]
kind = "python"
```

`extension.py`：

```python
PROTOCOL_VERSION = 1          # 可选；aaa 版本更低时拒绝加载

def activate(ctx):
    ...
```

- 启动时发现、**重启生效**；`/plugin` 可启停（`.disabled` 标记）。
- 项目级扩展属于 `extensions` 信任类别：首次交互启动会合并询问一次，非交互未信任时不加载（提示用 `--trust-repo`）。
- 加载失败只禁用该扩展并在日志留痕（`~/.aaa/logs/aaa.log`），不影响其他扩展与主 Agent。

## 上下文（`ctx`）

| 成员 | 说明 |
|---|---|
| `ctx.name` / `ctx.protocol_version` | 扩展名与协议版本 |
| `ctx.home` | 扩展目录路径（随包资源的起点） |
| `ctx.settings.get(key, default)` | 只读设置视图，只能读键表内的配置项 |
| `ctx.log` | 前缀为 `aaa.ext.<name>` 的 logger，写入 aaa 日志文件 |
| `ctx.register_tool(...)` | 注册工具（见下） |
| `ctx.subscribe(event, handler)` | 订阅事件（见下） |
| `ctx.intercept_tool(handler)` | 工具调用前拦截（见下） |
| `ctx.register_prompt_block(...)` | 向系统消息尾部加入提示词块 |
| `ctx.register_panel(...)` | 状态栏面板（`/panel` 切换） |
| `ctx.register_command(name, template)` | 斜杠命令，与 `commands/*.md` 同名字空间 |

## 工具

```python
def activate(ctx):
    ctx.register_tool(
        my_tool, name="lookup", capability="read", confirm="always",
    )

def my_tool(query: str) -> str:
    """一句话说明这个工具做什么（模型据此判断何时调用）。"""
    return "..."
```

- 工具名固定为 `ext_<扩展名>_<name>`；`ext_` 前缀属外部信任面——默认每次询问、审批策略不自动放行。用户可在审批时选「本会话总是允许」按工具名放行。
- `capability`：填 boundary 能力名（`read`/`write`/`shell`…）则跟随该能力在各权限模式的存在性；自定义名（建议 `ext/<扩展名>/<工具>`）默认 `workspace-write` 以上可得，`read_safe=True` 才全模式可得。
- `confirm`：`always` 每次询问；`mode` 跟随当前模式的确认名单（read-only 与 danger-full-access 不问，workspace-write 询问）。

## 事件

目录固定 9 个：`run.started`、`run.completed`、`run.failed`、`tool.called`、`tool.returned`、`tool.confirmed`、`message.user`、`message.assistant`、`compaction.done`；自定义事件可用 `ext/<扩展名>/<事件>`。

```python
def activate(ctx):
    ctx.subscribe("run.completed", on_done)

def on_done(event):
    print(event.name, event.payload)
```

- 同步调用、有界；handler 抛异常记日志并跳过，同一扩展单会话累计 3 次异常 → 挂起（不再派发）。
- v1 时序：`run.started`/`message.user` 轮前派发；`tool.*`/`message.assistant`/`run.completed` 在轮末统一回放；`run.failed` 由 CLI 失败路径派发。

## 工具调用前拦截

```python
from aaa.extensions.protocol import ToolDecision

def activate(ctx):
    ctx.intercept_tool(guard)

def guard(call):
    if call.name == "run_shell_command" and "rm -rf" in str(call.args.get("cmd", "")):
        return ToolDecision.deny("禁止递归删除")
    return None      # None/True/allow() 放行；False/deny(理由) 拒绝
```

- 拒绝理由作为工具结果返回给模型，用户可见。
- **只能收紧**：不能放行需要确认的调用，也不能改参（Agno 的确认暂停在钩子之前，改参会绕过用户已确认的内容）。
- 异常或超时（`extensions.hook_timeout`，默认 5 秒）视为拒绝；同一扩展 3 次失败 → 会话内挂起，拦截器随之跳过。
- 只覆盖主 Agent；委派子 Agent 的工具调用不经过拦截。

## 提示词块

```python
def activate(ctx):
    ctx.register_prompt_block(name="env", render=lambda info: f"当前模式：{info.mode}")
```

每块以 `<extension_block source="<扩展>/<名>">` 包裹追加到系统消息尾部；单块上限取 `project.instructions_max_chars`（0 关闭，超限截断并标注）；`/insight` 计入基础提示词段。

## 面板

```python
def activate(ctx):
    ctx.register_panel(name="status", title="我的状态", render=lambda view: ["行一", "行二"])
```

- 完整名为 `<扩展名>/<名>`：不同扩展可以同名，互不冲突。
- `/panel` 列出，`/panel <名>` 打开（短名唯一时可用），`/panel off` 关闭。
- `render(view)` 收到 `{"mode", "model", "session_id", "cwd"}`，返回行文本；最多显示 8 行，渲染异常降级为一行提示。

## 命令

```python
def activate(ctx):
    ctx.register_command("hello", "向用户打招呼，注意 $ARGUMENTS")
```

与 `commands/*.md` 同名字空间、同 `$ARGUMENTS` 语义；与文件模板同名时文件模板优先。

## 失败与隔离

- 加载失败 → 该扩展禁用 + 日志告警；不影响其他扩展与主 Agent。
- 事件 / 拦截器异常 → 跳过并计数，3 次后会话内挂起。
- 面板渲染异常 → 面板显示一行降级提示。
- 提示词块渲染异常 → 跳过该块。

## 不开放

扩展不接触 Agent 实例，不注入两次模型请求之间，不改运行中上下文与压缩；`ExtensionContext` 是能力收窄的唯一句柄。协议版本用 `PROTOCOL_VERSION` 声明，高于 aaa 支持的版本会被拒绝加载。
