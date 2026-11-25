# 测试体系

分三层，各管一段，不互相替代：

| 层 | 文件 | 跑什么 | 成本 |
|---|---|---|---|
| 单元 | `tests/test_*.py`（含 `fixtures.py`） | 模块契约 + **假模型驱动的 Agent 循环**（工具往返、HITL 暂停/确认/拒绝、压缩参数派生、配置校验、会话索引、审批签名、用量账本：计价与峰谷窗口、同步幂等、未定价补算、报表排序） | 毫秒级，零网络 |
| 端到端 | `tests/e2e.py` | 真实 CLI 进程 + 真实渠道 + 真实工具，断言 stdout/产物/日志/库 | 分钟级，几十 token |
| 交互 | `tests/pty_cases.py` | PTY 驱动 REPL：HITL 审批、`/provide` 向导 | 秒级，一次真实探查 |

## 开发自测入口

日常人工自测用 `scripts/aaa-dev.sh`（源码直跑，不装 wheel）；自动化回归仍用下面的 `./tests/run.sh`。人测负责真实终端、视觉与探索性使用。

统一入口：

```bash
./tests/run.sh              # 离线单测（固定 TZ/LC、清代理、-W error）
./tests/run.sh e2e           # 追加 e2e 全量
./tests/run.sh e2e --suite p0 --pty
```

`fixtures.FakeModel` 按脚本回放 `ModelResponse`（`text()` / `tool_call()`），只实现 `invoke` 及流式/异步变体；消息拼装、工具执行、会话写入仍由 Agno 完成，因此测的是真实循环而非另一套实现。

Agent 主会话回归（启动、新建、目录续聊和按 ID 续聊）：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:tests .venv/bin/python -B -m unittest test_agent_sessions
```

记忆专项（零网络、不运行 MCP）：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:tests .venv/bin/python -m unittest test_cli_memory test_memory_engine test_agent_build test_units.SettingsTest test_cli_keys test_cli_render
```

覆盖当前用户池隔离、状态显示、关闭时读取、引擎菜单的保存/取消/失败，以及原生记忆装配；不以离线结果代替真实渠道写入验收。


# 端到端测试计划

目的：把「只打过桩」的链路用真实渠道跑一遍，闭合 Coding Agent 的最小可交付闭环，并记录仍然测不了的部分。

脚本：`tests/e2e.py`（stdlib + PyYAML，无 pytest 依赖）。计划是脚本的唯一说明，脚本是计划的唯一执行入口。

## 前置

| 项 | 要求 |
|---|---|
| 解释器 | 仓库根执行 `uv sync --frozen` 后的 `.venv/bin/python`，或 `AAA_PYTHON=<python>` 指定 |
| 配置 | `~/.aaa/secret.yaml` 存在，至少一个可用渠道；脚本复制到临时 `HOME` 后按需改动，不碰真实配置 |
| 网络 | 需要外网或代理；默认保留环境代理（含 `ALL_PROXY`）；代理不可用时加 `--no-proxy` |
| tokenizer | 需要 `tiktoken`；缺失时 token 计数退回 `字符数/4`，真实历史实测低估 3.17×，压缩触发会晚约 3 倍。脚本在跑 P1 前检查，缺失直接报错 |
| 成本 | P0 十条约 70–100 秒；P1.7/P1.8 各最多 10–12 轮长输出，输入每轮约 +540 token |

## 渠道基线（本次配置）

| 渠道 | api | 模型 | 窗口 |
|---|---|---|---|
| `tokenhub` | `openai-completions` | `deepseek-flash`、`gpt-6-luna` | 1048576 / 131072 |
| `deepseek` | `deepseek` | `deepseek-flash`、`deepseek-v4-pro` | 1048576（官方 `/models` 上报） |

`deepseek-flash` 同名跨渠道，因此裸 id 解析应返回 `Ambiguous`——这是 P0 之外的一条已手工验证事实，脚本不重复。

## 用例

### P0 闭环（默认执行）

| # | 目标 | 步骤 | 断言 |
|---|---|---|---|
| P0.1 | 真实工具调用 | read-only，临时工作目录放 `data.txt`，要求模型读取 | 输出包含文件标记 |
| P0.2 | 真实写入 | `danger-full-access`，要求写 `out.txt` | 文件存在且含标记 |
| P0.3 | 非交互拒绝 shell | `workspace-write`，要求执行 `echo` | 日志含「非交互模式无法确认工具」与 `run_shell_command`；进程正常结束 |
| P0.4 | 多轮续接 | json 模式第一轮埋标记并取 `session_id`，`-r` 追问 | 续接后答出标记 |
| P0.5 | JSON 事件流 | 要求列目录 | 出现 `tool_start`；`result` 带 `session_id` 与非零 `tokens` |
| P0.6 | 错误路径 | 临时配置换成坏 key | 退出码非 0；日志含 `ERROR` |
| P0.7 | 损坏配置不被覆盖 | 把 `secret.yaml` 写成非法 YAML 后启动 | 退出码 2；提示解析错误；文件字节不变 |
| P0.8 | `--allow-shell` 放行 | `workspace-write` + `--allow-shell`，要求执行 `echo` | 命令执行、结果回填；日志无拒绝记录 |
| P0.9 | grep 工具 | read-only，要求用 grep 找 `build_agent` 定义位置（Agno `CodingTools`） | 答出 `src/app.py`（正则检索闭环） |
| P0.10 | 大结果卸载可回读 | read-only，1500 行文件，要求读最后一行 | 会话库出现 `<result id=` 与 `read_result`；答出末行标记 |

### P1 治理（默认执行）

| # | 目标 | 步骤 | 断言 |
|---|---|---|---|
| P1.7 | 压缩触发 | 临时配置把窗口压到 20000（阈值 3616），连跑最多 10 轮长输出（每轮 60 行；输入每轮约 +540 token） | 日志出现 `Auto-compacting` |
| P1.8 | 归档检索 | 埋标记 → 与 P1.7 同款长输出最多 12 轮直到折叠 → 追问标记 | 能答出；日志出现 `search_compacted_history` 则记为强证据，否则记「仍在保留尾内」 |

### P2 交互面（`--pty`，默认不跑）

| # | 目标 | 步骤 | 断言 |
|---|---|---|---|
| P2.11 | HITL 真实确认 | PTY 起 REPL → `/mode workspace-write` → 要求执行 `echo hitl-ok` → 应答 `y` | 出现「允许执行」，审批后输出 `hitl-ok` |
| P2.12 | `/provide` 真实向导 | 空 `HOME` 起 REPL，按提示输入渠道 id、协议、URL、Key，等真实探查后勾选模型 | `secret.yaml` 落盘 0600、渠道与模型正确、默认模型可用 |

P2 实现在 `tests/pty_cases.py`，由 `e2e.py --pty` 导入。

## 单元回归

`tests/test_*.py`，不联网、不建真实 `HOME`（需要路径的用例用临时目录），运行 `./tests/run.sh`。

覆盖：

- `Catalog`：resolve 四态、scope、fallbacks。
- `boundary`：三档能力/确认/隔离声明、`signature` 键序无关、`PolicySource` 替换后工具面变化。
- `tools`：grep 注册与 `search_content` 退场、后台 job 生命周期（启动/轮询/退出码/终止/非法 id/状态不落盘）、PowerShell 条件装配与参数拼装。
- `sessions`：目录→会话索引（未知目录、覆盖推进、`seq` 单调）。
- `config.settings`：默认值、类型/越界回落并告警、未知键忽略、不可解析文件、落盘回读。
- `context.params`：阈值按 `max(W×(1-ratio), reserve)` 派生、最小为 1。
- **Agent 循环（假模型）**：工具往返返回最终答复、HITL 暂停 → 确认续跑、拒绝理由回填给模型。
- **CLI 生命周期**：原生取消/取消意图、终态缺失报错、审批无需聚焦键（↑↓ 选择、Enter 确认、Esc 拒绝、500ms 防误触）、队列暂停与跨会话取回、`/redo` 只选已完成 run、channel 元数据透传、初始调用异常可见。
- **渲染**：Markdown 分块、代码围栏、diff 行级与配对行词级高亮、颜色关闭时纯文本。
- **输入键位**：Shift+Enter 只换行、普通 Enter 只提交一次。
- **TODO**：旧条目归一、阶段、显式进行中、幂等与越界拒绝、全局编号、树形展示与窄宽截断。
- **Git 状态**：分支/改动/未跟踪计数、detached、非仓库。
- **启动装配**：`build_agent` 接受插件工具并把工具名写进提示词。
- **启动横幅**：三幅素材完整性、品牌三行、静态/动画输出。
- **工具详情**：Ctrl-O 展开/收起最近一次调用；完整参数与结果按终端列宽分页，制表、组合字符与陈旧调用隔离。
- **输入适配**：xterm/Kitty 修改键回车经真实 VT100 解析后只换行，松键不重复，粘贴内容不被改写。
- **空闲菜单**：`/model`、`/mode`、`/config`、`/memory` 在运行中的事件循环内可选可取消。

## 已知测不了（脚本不包含，别当已覆盖）

| 项 | 原因 |
|---|---|
| fallback 真实切换 | 同渠道共享 `base_url`，无法让主模型单独失败；需要注入 429/5xx 或按模型给独立端点 |
| 上下文窗口溢出恢复（`on_context_overflow`） | 需要真实超长请求；1M 窗口下成本不可接受 |
| PowerShell 工具 | 已由 GitHub Windows 机器实测（`windows-latest`，pwsh 7.6.10：编码、中文输出、目录）；本机仍没有 `pwsh`/`powershell`，日常回归只覆盖“不存在时不装配”与参数拼装 |
| `-c`（按目录续接） | 端到端无用例；`-c` 依赖的目录→会话索引仅有单测覆盖（P0.4 走 `-r`，不经过索引） |
| 真实渠道记忆写入与注入 | 本轮只验证离线原生存取、CLI 与装配；真实模型主动记录及下一轮注入未实测 |
| 多渠道路径歧义 | 已手工验证（`deepseek-flash` → `Ambiguous`），未入脚本以免重复成本 |
| 长命令后台化 | 未实现，>120s 即杀进程组 |
| 真实 TTY 键位矩阵 | Shift+Enter 的终端编码、Ctrl-C 双击、resize 目前只有单元/PTY 启动级证据；需在 iTerm/Terminal 实测 |
| MCP 实测 | 按 owner 要求暂不实测；本地 toy 桥接用例保留但排除在常规验收命令之外 |
| 横幅跨终端表现 | 动效已验收并默认启用；其他终端、字体和配色下的表现未逐一实测 |
| 物理键盘矩阵 | 交互由 VT100 解析与 PTY 协议模拟覆盖；iTerm/Terminal 的物理 Shift+Enter 与修饰键编码未逐一实测 |
| 单会话全路径贯通 | 各路径有专项证据；启动→审批→TODO→排队/取消→切会话→退出的一次性长流程未整体重跑 |

## 清理

每次运行在系统临时目录下建一个根（`aaa-e2e-run-*`），用例的 `HOME` 与工作目录都建在其下；跑完即删。有失败或带 `--keep-temp` 时保留并打印路径，便于排查。`~/.aaa/data/sessions.db` 不被脚本写入。

## 审批后执行

```bash
.venv/bin/python tests/e2e.py --list      # 只列用例，零请求
.venv/bin/python tests/e2e.py             # P0 + P1
.venv/bin/python tests/e2e.py --suite p0  # 只跑闭环
.venv/bin/python tests/e2e.py --pty       # 追加交互面
.venv/bin/python tests/e2e.py --keep-temp # 保留临时目录（默认跑完删除）
```
