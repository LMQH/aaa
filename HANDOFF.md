# HANDOFF

- 更新日期：2026-11-23
- 版本控制：git（`origin` → `https://github.com/LMQH/aaa.git`），按模块提交；依赖由 `pyproject.toml` + `uv.lock` 复现。
- 当前状态：主会话由 Agno 原生 `Agent.run()` 驱动；内置委派工具在线程中运行独立 Agent、Team 或 Workflow。记忆默认关闭；通过 `/memory engine` 选择内置或外置，重启生效。配置、渠道与模型维持现有入口；依赖由 `uv.lock` 冻结。
- 文档分工：`AGENTS.md` 维护项目要求与流程，`docs/architecture.md` 维护系统结构与跨模块约定，模块 `DESIGN.md` 维护本模块机制与不变量，`README.md` 只讲用法。

## 代码结构

- `config/paths` — `~/.aaa/` 下的全部路径常量。
- `config/settings` — `config.toml` 键表（`KEYS`）、读写、校验与序列化。
- `config/loader` — `secret.yaml` 的 v2 schema、v1→v2 归一、`ConfigError`、`probe_models`。
- `model/provider` — `Channel` / `ChannelModel` 值对象、`API_CLASSES`、模型构造。
- `model/catalog` — 绑定 settings 的渠道目录：`require` / `scope` / `resolve` / `window` / `build` / `fallbacks`。
- `runtime/agent` — `build_agent`、`tools_for_mode`、记忆与主 Agent 装配。
- `runtime/prompt` 与 `runtime/prompts` — 内置中文指令、`SYSTEM.md` / `APPEND_SYSTEM.md` 覆盖与技能目录披露。
- `runtime/logging_setup` — 日志落盘、轮转与库大小告警。
- `runtime/delegation*` — [委派](docs/delegation-design.md)的定义注册、发现、原生执行和确认保存；不承担主会话。
- `context/params` — 历史轮数、摘要预算与压缩阈值。
- `insight` — 上下文洞察：只读采集十层构成与 token 估算；`/insight` 渲染，机制见 `src/aaa/insight/DESIGN.md`。
- `boundary/policy` — 权限模式的能力集合、确认名单与隔离范围；不装配工具。
- `tools/assemble` — 按能力名装配；`builtin/{files,processes,powershell,todos}`、`extensions/weather`。
- `memory` — 外置协议、发现与宿主；应用持有一个外置实例，内置不属于插件。
- `../miniviking` — 随 aaa 分发的外置记忆引擎（独立包）：文件树事实源、SQLite 派生索引、LLM 蒸馏与持久任务队列；经 `aaa.memory_engines` 入口点接入，机制见 `src/miniviking/DESIGN.md`。
- `usage` — 读 Agno metrics，写独立 `usage.db` 账本。
- `sessions/query` 与 `sessions/index` — 会话查询、标题与目录→会话索引。
- `todo_contract` — TODO 条目契约，tools 与 CLI 共用。
- `plugins` — 命令模板、技能、MCP 插件的发现与启停。
- `cli/{args,app,state,run,ui,completer,commands,controller,render,diff,git_status,banner,key_input,tool_details}` — 入口、调度、渲染、输入适配、工具详情、事件与斜杠命令。
- `cli/animations` — 启动动画插件：协议、发现与加载；默认 `default`，可选 `sharingan`。

依赖单向：`cli → runtime/model/boundary/config/context/sessions/usage/memory`；`runtime → model/boundary/config/context/tools/memory`；`sessions → model/config`；`usage → config/model`；`config → model/boundary`；`memory → config.paths`；下层不 import CLI。`miniviking` 只 import `aaa.memory` 的公开协议，`aaa` 核心不 import `miniviking`。`boundary` 与 `tools` 互不 import，由 `runtime.tools_for_mode` 合并。

## 关键约定

- **配置两分**：`secret.yaml` 只放渠道、密钥与默认模型；`config.toml` 放其余全部可调项。用户不手改，经 `/config` 修改，程序校验后全量写回。
- **生效方式**：`runtime` 当场重建 Agent；`immediate` 下次使用生效；`restart` 下次启动生效。非法值拒绝并保持原值；文件中的非法值回落默认并告警。
- **记忆**：内置、外置、关闭互斥。外置只接收独立数据目录，自管配置、模型与存储；启动失败以关闭运行。重建主 Agent 不切来源、不重建外置实例。随包引擎 `miniviking` 走同一外置契约：默认继承 aaa 默认模型做后台蒸馏，模型不可用退化为机械摘要；数据在 `~/.aaa/memory/miniviking/`。
- **记忆查看与切换**：`/memory` 空闲时显示当前记忆引擎并弹出切换菜单；`/memory engine [引擎]` 直达切换（未知引擎拒绝）。运行中只显示状态；关闭时不读旧数据。切换只保存，下次手动启动生效。
- **用量账本**：`/usage` 读 `usage.db`；账本从终态 run 增量生成，按 `(渠道, 模型)` 计价，金额入账时冻结。
- **后台任务**：job 状态只在进程内，有界缓冲；进程退出时 `atexit` 杀掉未结束进程组。
- **工具与权限**：`boundary` 只授权能力名，`tools.assemble` 装配，数值上限来自配置；`update_user_memory` 由 Agno 内部注册，不受权限模式约束。
- **模型/执行体**：模型身份是 `(provider, model_id)`；换模型、权限模式或运行相关配置会重建主 Agent，不改变当前会话 id。
- **会话恢复**：`-c` 续接当前目录最近的 Agent 会话——先查库内目录索引，索引命中非 Agent 记录时跳过，按 `metadata.cwd` 回落到该目录最近的 Agent 会话；`-r` 按完整 id 或前缀匹配 Agent 会话；`/session` 列出并按更新时间切换 Agent 会话。旧库中已有的 Team 记录不再列出、不可续接，不迁移也不删除。
- **审批**：`boundary` 声明确认规则与签名，`cli` 负责询问、签名记忆与非交互拒绝；文件改动落盘前显示 diff。
- **取消与续跑**：取消事件不等于完成，不伪造 `cancelled` 终态；非交互 JSON 不新增字段、退出码 130；取消失败要可见且不算取消；`continue_run` 保留 run id、事件 id 与 channel；审批前或审批中被取消都不续跑。
- **历史**：Agno 原生历史是唯一回放来源；不提供清空上下文的命令，不伪造运行成功或自建回放层。
- **非交互**：默认拒绝需确认的工具；`--allow-shell` 只放行 `run_shell_command`。
- **启动动画**：`cli/animations/*.py` 与 `~/.aaa/animations/*.py` 同名覆盖；模块导出 `ANIMATION`（函数即 `frames(ctx)`），宿主 `cli/banner.py` 负责调度与降级；`ui.banner_animation` 切换，下次启动生效。

## 待定 / 阻塞

- **沙箱**：本期不做。接入点是 `boundary.Isolation` 的声明与可替换的 `PolicySource`。
- **隔离强度**：文件工具受工作区根约束、grep 受根目录约束；shell 只有 cwd 与审批，无路径约束。
- **Team 任务依赖（上游）**：Agno 3.1.12 的 `execute_task` 完成后不自动刷新已阻塞的依赖。委派支持 `tasks`；领队在确认前置任务成功后，用 Agno 原生 `update_task_status` 刷新依赖。
- **真实渠道覆盖**：基础闭环、真实工具、压缩、归档检索、内置记忆写入→新会话注入已实测（见验证证据）；外部引擎真实召回与 `cached_tokens` 上报仍未实测。
- **物理终端**：iTerm/Terminal 的物理 Shift+Enter 与修饰键编码未逐一实测；单会话全路径贯通未整体重跑。
- **README**：已按当前形态重写（定位、委派差异点、快速开始、目录导航）；业务范围表述仍待 owner 补充。
- **发布**：`docs/release.md` 设计已定，尚未发版；构建、安装脚本与 CI 机制已具备但未启用。
- **平台**：Windows 的 PowerShell 与后台任务分支未实跑。

## 下一步

1. testPyPI 演练 → PyPI 注册与 Trusted Publishing（owner 有 testPyPI 凭据；本机 `gh` 已登录，SSH 部署密钥走 443）。
2. 外部引擎（miniviking）真实召回实测；`cached_tokens` 上报核实。
3. 物理终端键位矩阵与单会话全路径贯通实测。

## 当前验证证据

按工作线分组，只保留现行线的最新证据；历史批次在版本控制记录中。

### CLI 启动横幅（2026-11-23）

- **实现验证（Fast 自测）**：macOS arm64，项目 `.venv`、隔离 HOME。`PYTHONPATH=src:tests AGNO_TELEMETRY=false .venv/bin/python -B -m unittest test_cli_banner` → **26 项通过**。用 Python PTY 运行 `.venv/bin/python -B -m aaa.cli.banner --duration 0/1`：按设计观测到默认像素 `α`、固定版本和项目名、`AAA` 八档渐显；确认 `--duration 0` 直接显示终态、未知名称回落并告警、显式 `sharingan` 播放原动画、39 列时回落单行标题。证据为本轮 unittest 与 PTY 输出；未在物理终端逐一验证配色。
- **快捷命令验证（Fast 自测）**：macOS arm64，项目 `.venv`，隔离 HOME、离线渠道占位。Python PTY 启动 `.venv/bin/python -B -m aaa` 三次，验证 `/help` 提示、`/config start sharingan` 保存并在下次启动显示写轮眼、`/config start` 查询、原配置键仍可写入、`/config start default` 重启恢复极简横幅。以上通过，证据为本轮 PTY 的三条 `PASS` 输出。

### 委派（2026-11-23）

- **实现验证**（macOS arm64，项目 `.venv` / Agno 3.1.12，隔离 HOME，关闭遥测）：按 [委派设计](docs/delegation-design.md) 定向验证临时注册、三类对象、Team 四模式、嵌套条件/并行、拒绝子任务审批、同步取消及确认保存后跨会话复用。`PYTHONPATH=src:tests AGNO_TELEMETRY=false .venv/bin/python -B -m unittest test_delegation test_cli_run_lifecycle test_agent_sessions test_agent_build` → **40 项通过**。用离线模型驱动 `aaa --mode json` 注册并委派，观测到子结果与 `confirm_required`，未落盘。未运行真实渠道、MCP、全量或物理终端确认。
- **独立 QA：Pass**（项目 `.venv` / Agno 3.1.12，隔离 HOME、离线）：[报告](agent://DelegationFocusedQA) 按实施计划实测 9 项标准，另验证独立 Team 保存、公共依赖缺失、权限收窄、主 Agent 重建及超时实际退出。候选 `delegation.py` / `delegation_registry.py` / `test_delegation.py` SHA-256 前缀 `b985de69` / `93fe97c3` / `e5573cca`，完整受验文件清单见报告。未驱动物理终端审批。
- **独立代码审查：Pass，无阻断**：[报告](agent://DelegationDiffReview) 只读核对实现与 Agno 3.1.12 源码，覆盖保存审批绑定、权限下传、取消、失败隔离与定义校验，未运行测试。三项低风险边界（准备期异常可能中断整批、重复保存确认只消费一次、批内 Ctrl+C 可能等到子任务自然退出）待随下次改动处理。审查范围与快照见报告。

### MiniViking 外置记忆引擎（2026-11-23）

- **BGE 语义检索（llama.cpp GGUF + sqlite-vec，无 torch）**（macOS arm64，Python 3.13.12；独立 venv llama-cpp-python + huggingface_hub 2.4.0 + sqlite-vec 0.1.9）：按 [引擎设计](src/miniviking/DESIGN.md#召回与注入)，关键词×语义 RRF 混合检索、向量入 **vec0 虚表**（分区键 + 元数据过滤 KNN 在 C 层，L2 距离按归一化换算余弦）、启动全量补嵌、换模型重建虚表重嵌、查询向量缓存、扩展/依赖/下载任一失败降级关键词——`test_miniviking` 28 项覆盖。**真实端到端**：独立 venv 安装增强（预编译 wheel 43 秒）后，在 `ALL_PROXY=socks5://…`（缺 socksio，直接用会失败）环境下经 hf-mirror.com 镜像直连下载 `CompendiumLabs/bge-small-zh-v1.5-gguf` 的 f16 GGUF（48MB，就绪 18.4–21.0 秒，代理剥离后恢复），llama.cpp 本地推理 512 维（3 条 0.02s、批量 8 条 0.22s），语义区分度 cos(代理,代理方法)=0.759 / cos(代理,天气)=0.212–0.213，与 HF f32 路线一致；真实模型 + vec0 混合检索全链路（remember→后台嵌入→语义命中→召回注入）一次通过。`provider="api"` 路径由单测覆盖，未连真实 embedding 端点。
- **嵌入增强安装器与 `/memory embedding`**（macOS arm64，项目 `.venv`，含 `uv` 预编译路径）：`embed_install` 策略链（uv→pip→pipx）、默认源失败自动改清华镜像重试（仅命令参数，不改全局配置）、全程失败给出手动命令——单测覆盖；CLI `/memory embedding`（状态）与 `/memory embedding install`（后台线程安装、进度内存缓冲、不污染对话流）在真实 pty（tmux 100×30、隔离 HOME、fake 渠道）实测：已装环境正确显示"已安装"，install 路径正确识别并提示重启，`/exit` 干净退出。选中 miniviking 引擎且增强缺失时会弹确认框（questionary）后开始后台安装。定向回归 99 项 + 全量离线 **257 项 0 失败**（`test_cli_memory` 已随并行编辑收口恢复）。证据为本轮命令输出。
- **引擎行为**（项目 `.venv` / Python 3.13.12，关闭遥测）：按 [引擎设计](src/miniviking/DESIGN.md) 验证身份契约与参数身份拒绝、中文 trigram 检索与短词回退、蒸馏按 `run_id` 幂等与同标题合并、无模型退化、资源导入任务状态、技能覆盖、`/memory` 分页、宿主工具面与确认集合、Agent 召回注入与工具分发、`close` 收口。`PYTHONPATH=src:tests AGNO_TELEMETRY=false .venv/bin/python -B -m unittest test_miniviking test_agent_build test_memory_engine test_cli_memory test_cli_run_lifecycle` → **98 项通过**。未连接真实模型。
- **wheel 构建与安装态**（`uv build` + hatchling 1.33.0，隔离 HOME，GGUF 版本重建）：wheel 顶层实测含 `aaa/` 与 `miniviking/` 双包，entry_points 含 `[aaa.memory_engines] miniviking`。解包为唯一 import 根后实测：入口点发现、工厂构造 7 工具引擎、`viking_remember` 写入并被检索；未装增强时按预期降级关键词并告警。产物在 `dist/`；安装态未连真实渠道。

### 记忆交互收缩（2026-11-23）

- **`/memory` 收缩与术语统一**（macOS arm64，项目 `.venv` / Agno 3.1.12，隔离 HOME，关闭遥测）：交互收缩为「状态 + 引擎切换」，与 `/config` 同构——裸 `/memory` 显示状态并弹引擎菜单（Esc 取消），`/memory engine [引擎]` 直达切换（未知引擎拒绝且不改动），运行中只显示状态；`/memory list` 与忙时分类函数移除；补全、界面与全部文档术语统一「记忆引擎」。`PYTHONPATH=src:tests AGNO_TELEMETRY=false .venv/bin/python -B -m unittest test_cli_memory test_memory_engine test_agent_build test_cli_run_lifecycle` → **68 项全部通过**。证据为本轮命令输出。

### 命令交互形态准则落地（2026-11-23）

- **准则入档 + 三命令菜单化**（macOS arm64，项目 `.venv` / Agno 3.1.12，隔离 HOME，关闭遥测）：[CLI 设计](src/aaa/cli/DESIGN.md#命令交互形态) 新增「命令交互形态」——有界选择集的命令必须同时提供菜单（引导）与直达参数（快捷）两条等价路径；自由文本与单动作命令不做菜单；菜单只在空闲弹、取消不改状态。`/session` 裸命令升级为操作菜单（切换/搜索/重命名/删除，动作复用直达子命令）；`/export` 无参改为确认预填路径（留空/Esc 取消）；`/plugin` 裸命令本已是选择启停菜单，核对入档。
- **命令清单频次排序**（同环境）：补全菜单与 `/help` 按「次数降序 → 字母 A→Z」排序；用户敲下的命令在分派入口记一次（排队重放与菜单内部转派不计），账本为 `~/.aaa/command_usage.json` 原子小 JSON，损坏回退字母序，自定义斜杠命令一并参与。同套件 → **114 项中 113 通过**，唯一失败为 `McpBridgeTest`（真实 MCP 桥接项，按约定需显式授权、常规验收排除，与本改动无关）。证据为本轮命令输出。

- **委派三缺陷修复**（同环境，离线）：`delegate_task` 项准备失败收敛为单项 error（外层 `except Exception` 兜底整批）；保存确认改按批准次数逐次消费（同批重复要求不再互相吞授权）；批内 Ctrl-C/异常穿出时对全部已启动子任务补发 `cancel_run` 并如实标 `interrupted`，不再静默等待子任务自然退出。随套件回归覆盖。
- **记忆引擎配置面 + 嵌入增强整合**（同环境，离线）：宿主协议新增可选 `config_specs()` / `apply_config()`；miniviking 声明 35 项配置（llm/limits/embedding）并写自己的 `<数据目录>/config.toml`，`/memory` 菜单加「引擎配置」、新增 `/memory config`（两级菜单 + `<键> <值>` 直达，类型/范围校验、密钥遮蔽）。与并行线的 `/memory embedding install|status`（后台安装增强包约 50MB，权重下次启动经 hf-mirror 自动下载）整合进同一子命令族。`PYTHONPATH=src:tests AGNO_TELEMETRY=false .venv/bin/python -B -m unittest test_cli_memory test_memory_engine test_miniviking test_agent_build test_cli_run_lifecycle test_units test_delegation` → **174 项中 173 通过**，唯一失败为 `McpBridgeTest`（真实 MCP 桥接项，需显式授权，与本批无关）。证据为本轮命令输出。

### 上下文洞察（2026-11-23）

- **`/insight` 实现与验证**（macOS arm64，项目 `.venv` / Agno 3.1.12，隔离 HOME，关闭遥测）：新增 `insight` 模块——`tokens`（字节/4 预筛、Agno 计数链、内容哈希 LRU）、`collect`（十层只读采集，复用 `runtime.agent.system_instructions` / `skill_context` / `tools_for_mode` 同源函数）；CLI `/insight` 输出全局估算表，运行中只读放行，不弹菜单。`PYTHONPATH=src:tests AGNO_TELEMETRY=false .venv/bin/python -B -m unittest test_insight test_cli_memory test_memory_engine test_agent_build test_cli_run_lifecycle test_units test_delegation` → **151 项中 150 通过**，唯一失败为 `McpBridgeTest`（真实 MCP 桥接项，需显式授权，与本改动无关）。真实对象冒烟：真实 Agent + 真实历史（FakeModel 写库）+ 真实计数链（`tiktoken o200k_base`，走本地缓存）跑通 `/insight`，输出十层表与脚注；窗口未知时占比留空。未跑真实模型渠道。证据为本轮命令输出。

### 真实渠道 E2E（2026-11-23）

- **P0 闭环 11/11**（macOS arm64，真实渠道 `tokenhub/deepseek-flash`，`HOME=/Users/lmqhx` 经脚本复制到临时 HOME，剔除 SOCKS、保留 HTTPS 代理）：`HOME=/Users/lmqhx ./tests/run.sh e2e --suite p0` → 真实工具读取、danger-full-access 写入、非交互拒绝 shell、`-r` 续接、JSON 事件流（真实 tokens=6390）、坏 key 退出码 1、损坏配置不被覆盖退出码 2、`--allow-shell` 放行、grep 检索、大结果卸载回读、按 ID 续聊，全部 PASS；耗时 54 秒。
- **P1 治理 2/2**：同一环境 `--suite p1` → 第 6 轮触发自动压缩、压缩归档检索答对（未走检索工具，可能仍在保留尾内）；耗时 87 秒。
- **内置记忆写入→新会话注入**（隔离 HOME + 真实渠道）：首轮要求记住「项目代号 KAGUYA-42」，模型真实调用记忆工具保存成功；第二轮**全新会话**仅问代号，答出 `KAGUYA-42`，证明记忆注入生效。未测外部引擎真实召回与 `cached_tokens` 上报。
- **离线全量**：`./tests/run.sh` → **273 项全部通过**（含 `McpBridgeTest`，9.2 秒）。证据为本轮命令输出。

### 项目级扩展（2026-11-23）

- **批 2 文本面**：项目根=向上首个 `.git`；`.aaa/commands`、`.aaa/skills` 与仓库根 `AGENTS.md` 自动加载，项目>用户>内置、先到先得；`/skills`、`/plugin`、补全标 `[项目]/[用户]/[内置]`；新增配置键 `project.enabled`、`project.instructions_max_chars`。`tests/test_project_extensions.py` 覆盖根解析、覆盖顺序、来源标注、AGENTS.md 注入与截断。真实冒烟（临时仓库）：启动提示「已加载项目级扩展 2 项」，命令与技能来源为 project。
- **批 3 信任门**：`src/aaa/trust.py`；记录 `~/.aaa/trusted/repos.json`（仓库 realpath + 每类 SHA-256 指纹）；交互逐类询问、非交互一律不加载并输出说明；`/trust` 查看/`revoke`；记忆引擎与 MCP 扩展仅从已信任项目目录发现，动画按信任目录显式传入。`tests/test_project_trust.py` 覆盖指纹变化、approve/revoke、非交互不询问、项目引擎与动画的加载隔离。
- **验证**：`./tests/run.sh` → **291 项全部通过**。真实渠道冒烟：未信任仓库含自动执行动画，`-p` 运行仅输出「非交互模式：项目级代码扩展未加载（启动动画）」且标记文件不存在；交互式确认后记录指纹。证据为本轮命令输出。
- **CI 与仓库治理**：`.github/` 落地——`ci.yml`（push/PR：`uv sync --frozen --extra embedding` + 全量回归，action 固定 commit SHA）通过 push 验证（run `45142099357`，4m45s）；`release.yml` 只响应已推送 tag，构建、版本一致性校验、双平台安装冒烟并生成草稿 Release（不建 tag、不发布，遵循 owner 确认红线），当前未启用。另含 Issue/PR 模板、CODEOWNERS、SECURITY.md、dependabot（`agno` 基线已 ignore）。修复期间发现 `uv.lock` 仍记旧包名，已 `uv lock` 更新并纳入发布预检。

### 运行期修复（2026-11-23）

- 交互启动在 TTY 下清屏并清滚动历史（`ESC[2J ESC[3J ESC[H`），非 TTY 不输出控制序列；默认 `PROMPT_TOOLKIT_NO_CPR=1`，消除不支持 CPR 终端上的警告（用户设 0 可恢复探测）。
- 新增 `socksio` 依赖：`ALL_PROXY=socks5://…` 下真实模型调用通过（tokenhub/deepseek-flash 回答 ok），不再报「Using SOCKS proxy … socksio」。
- PTY 启动验证：清屏序列存在、无 CPR 警告、项目级扩展提示正常；`./tests/run.sh` → **293 项通过**。
- **测试盲区复盘（为什么 E2E 没抓到 socksio）**：`tests/e2e.py` 曾把「验证环境未装 socksio」写成前提，主动剔除 `ALL_PROXY`/`all_proxy`；`run.sh` 清全部代理；离线套件用假模型不触网。因此所有「真实渠道全绿」都跑在无 SOCKS 的环境里，用户真实 shell 一开就暴露。已修正：e2e 默认保留环境代理（`--no-proxy` 为逃生阀）；新增离线回归——httpx 经 `socks5://` 应抛连接错误而不是缺包。

**边界**：验证使用临时仓库与隔离 HOME；非交互按设计严格不加载代码类。


