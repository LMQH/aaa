# 外置记忆引擎

外置引擎与内置记忆互斥。aaa 提供独立数据目录、生命周期和注入；引擎自己管理配置、模型、凭据与存储。

## 接入方式

两种入口，标识即引擎名；同名时本地文件优先。

### 本地文件

放在 `~/.aaa/memory_engines/<id>.py`，模块导出工厂 `ENGINE`，工厂接收一个 `Path`：外置引擎的数据目录，即 `memory.data_root/<id>`。默认根目录 `~/.aaa/memory`。不传 Agno 模型、Agent 或会话库。

### entry_points

包安装到 aaa 所在环境，并在 `pyproject.toml` 声明：

```toml
[project.entry-points."aaa.memory_engines"]
tiny = "tiny_memory:Engine"
```

入口名 `tiny` 对应 `/memory engine external:tiny`。

### 示例

aaa 随包自带一个完整引擎 `miniviking`（同一 wheel、入口点已注册），`/memory engine external:miniviking` 即可启用；源码见 `src/miniviking/`，可作为接入参考。

```python
from pathlib import Path
from aaa.memory import MemoryEntry, MemoryIdentity, MemoryTool, RecallQuery


class Engine:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir  # 配置、模型、凭据、存储都由引擎自己管理

    def recall(self, query: RecallQuery):
        return [MemoryEntry(memory="用户偏好深色主题", memory_id="pref-1")]

    def tools(self):  # 可选：不实现就不注册工具
        return [MemoryTool(
            name="search_tree",
            description="在记忆树里搜索",
            parameters={"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
            read_only=True,
        )]

    def call_tool(self, name, arguments, identity: MemoryIdentity):
        # identity 由 aaa 提供，不从 arguments 读取身份。
        return f"hit:{arguments.get('q', '')}"

    def prompt_block(self):  # 可选：静态使用说明
        return "需要记忆时先用 search_tree 检索。"


ENGINE = Engine
```

## 协议

- `recall(query)`：必须实现。`query.identity` 是当轮身份；返回 `MemoryEntry` 序列。
- `observe(event)`：可选。每轮完成提交一次，包含 `query`、`answer` 和 `identity`。
- `read(identity, *, limit, page=1)`：可选。返回 `(条目列表, 总数)`，条目形状同上；不实现时 `/memory` 显示不支持。
- `close()`：可选。正常退出时释放连接、停止后台任务；宿主只限制等待时间，不能强制停止线程。
- `tools()`：可选。返回 `MemoryTool` 序列（名称、描述、JSON Schema `parameters`、`read_only`、`requires_confirmation`）。不实现就不注册工具。
- `call_tool(name, arguments, identity)`：声明了 `tools()` 就必须实现。返回值转成字符串回给模型。aaa 会限时并把异常降级成 `Error: <类型>`。
- `prompt_block()`：可选。返回一段静态说明文本，宿主追加到 system prompt；引擎不能改宿主模板，缺失就不追加。
- `config_specs()`：可选。返回可调配置项列表（每项 `key`（`section.field`）、`type`（str/int/float/bool）、`current`、`minimum`/`maximum`/`choices`、`secret`、`description`）；宿主据此渲染 `/memory config` 并做类型与范围校验。不实现就没有配置面。
- `apply_config(values)`：可选。接收 `{key: value}`，由引擎写入自己的配置文件并负责语义校验；返回空表示成功，非空文本按错误提示。约定重启 aaa 后生效；密钥类项在配置面里遮蔽显示。

四个入口都使用 `MemoryIdentity`。`user_id` 是当前用户；`agent_id` 是稳定的 `aaa-main`；`session_id` 和 `run_id` 取实际会话和运行。`/memory` 不属于任何运行，`run_id` 为 `None`；没有当前会话时 `session_id` 也为 `None`。引擎自己决定用哪些维度存取，不自行补造身份。

接入已有引擎时，召回和观察从 `query.identity`、`event.identity` 读取身份；`read()` 的首个参数不再是用户 ID 字符串，`call_tool()` 的第三个参数也使用同一个 `MemoryIdentity`。

方法同步调用；后台工作由引擎管理。引擎不得持有 Agent 引用或直接改宿主模板。

## 注入与失败

- 正常轮次开始前召回，结果进入 system prompt，不改用户原文。召回异常、超时：本轮无记忆，回答继续。
- 观察异常、超时：记录日志，不重试、不回滚。`/edit` 和 `/redo` 沿用原轮快照，不重新召回；完成后仍以新轮次的 `run_id` 提交观察，重做不会撤回引擎已经收到的原轮记录。

- `read_only` 只描述工具是否只读。引擎自行设置 `requires_confirmation`（默认 `False`）：需要 aaa 询问用户时显式设为 `True`，无论工具是否只读。aaa 复用已批准签名；非交互模式默认拒绝需要确认的调用。
- 如果接入时只声明了 `read_only`，请为仍需人工确认的工具显式设置 `requires_confirmation=True`。
- 每次工具调用限时 `memory.tool_timeout`（默认 15 秒）；超时或异常以 `Error: <类型>` 文本回给模型，不中断会话。
- 超大结果沿用 Agno 现有的工具结果卸载（超过 16,000 字符落盘，上下文只留摘要）。
- 引擎自己的模型、配置、存储完全自管；aaa 不代管凭据，也不把工具写入全局权限清单。

## 选择与验证

1. 放置文件或将包安装到 aaa 所在的环境。
2. 在 `/memory` 的菜单里选择外置引擎，保存后手动重启。
3. 用 `/memory` 确认当前引擎（若提供 `read`，会显示条目数）。
4. 外置引擎加载失败时，aaa 提示并以关闭状态运行；已保存选择不变。

引擎代码在 aaa 进程内执行；只应加载可信来源。aaa 不提供沙箱、巩固、导出导入或数据迁移；工具面由引擎声明、aaa 装配与确认。确认提示会展示工具参数（沿用现有审批界面），引擎不应把敏感信息放进参数。
