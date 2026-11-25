# HANDOFF

- 更新日期：2026-11-25
- 版本控制：git（`origin` → `https://github.com/LMQH/aaa.git`）；`main` 已推送；依赖由 `pyproject.toml` + `uv.lock` 复现。
- 当前状态：主会话由 Agno 原生 `Agent.run()` 驱动；内置委派工具在线程中运行独立 Agent、Team 或 Workflow。记忆默认关闭；通过 `/memory engine` 选择内置或外置，重启生效。开发自测入口为 `scripts/aaa-dev.sh`（源码直跑）；发布门已就绪，尚未发版。
- 文档分工：`AGENTS.md` 维护项目要求与流程，`docs/architecture.md` 维护系统结构与跨模块约定，模块 `DESIGN.md` 维护本模块机制与不变量，`README.md` 只讲用法。

## 代码结构

- `config/paths` — `~/.aaa/` 下的全部路径常量。
- `config/settings` — `config.toml` 键表（`KEYS`）、读写、校验与序列化。
- `config/loader` — `secret.yaml` 的 v2 schema、v1→v2 归一、`ConfigError`、`probe_models`。
- `model/provider` — `Channel` / `ChannelModel` 值对象、`API_CLASSES`、模型构造。
- `model/catalog` — 绑定 settings 的渠道目录：`require` / `scope` / `resolve` / `window` / `build` / `fallbacks`。
- `runtime/agent` — `build_agent`、`tools_for_mode`、记忆与主 Agent 装配。
- `runtime/prompt` 与 `runtime/prompts` — 内置中文指令：基础指令、权限模式说明与按工具裁剪的工具指引，`SYSTEM.md` / `APPEND_SYSTEM.md` 覆盖；技能索引由 Agno skills 原生注入。
- `runtime/logging_setup` — 日志落盘、轮转与库大小告警。
- `runtime/delegation*` — [委派](docs/delegation-design.md)的定义注册、发现、原生执行和确认保存；不承担主会话。
- `context/params` — 历史轮数、摘要预算与压缩阈值。
- `insight` — 上下文洞察：只读采集十层构成与 token 估算；`/insight` 渲染，机制见 `src/aaa/insight/DESIGN.md`。
- `boundary/policy` — 权限模式的能力集合、确认名单与隔离范围；不装配工具。
- `tools/assemble` — 按能力名装配；`builtin/{files,processes,powershell,todos}`、`extensions/weather`。
- `memory` — 外置协议、发现与宿主；应用持有一个外置实例，内置不属于插件。
- `../miniviking` — 随 aaa 分发的外置记忆引擎（独立包）：文件树事实源、SQLite 派生索引、LLM 蒸馏与持久任务队列；经 `aaa.memory_engines` 入口点接入，机制见 `src/miniviking/DESIGN.md`。
- `usage` — 读 Agno metrics，写独立 `usage.db` 账本。
- `sessions/query`、`sessions/index` 与 `sessions/fork` — 会话查询、标题、目录→会话索引与会话分叉。
- `todo_contract` — TODO 条目契约，tools 与 CLI 共用。
- `plugins` — 命令模板、技能、MCP 插件的发现与启停；技能包由技能根的分组目录投射（`PACK.md` 描述、`.disabled` 停用）。
- `cli/{args,app,state,run,ui,completer,commands,controller,render,renderers,images,report,settings_view,usage_stats,diff,git_status,banner,key_input,tool_details}` — 入口、调度、渲染（Markdown、终端图片、外部渲染器）、输入适配、设置视图、工具详情、事件与斜杠命令。
- `cli/picker` — 自建选择器与单行输入：`❯` 强调色选中、无底色、输入筛选、Esc 逐级返回。
- `cli/animations` — 启动动画插件：协议、发现与加载；默认 `default`，可选 `sharingan`。默认动画为 6 行块状 `A` 标识 + 半块 `AAA` 字标，版本贴末行，渐变旋转 + 高光扫过。

依赖单向：`cli → runtime/model/boundary/config/context/sessions/usage/memory`；`runtime → model/boundary/config/context/tools/memory`；`sessions → model/config`；`usage → config/model`；`config → model/boundary`；`memory → config.paths`；下层不 import CLI。`miniviking` 只 import `aaa.memory` 的公开协议，`aaa` 核心不 import `miniviking`。`boundary` 与 `tools` 互不 import，由 `runtime.tools_for_mode` 合并。

## 关键约定

- **配置两分**：`secret.yaml` 只放渠道、密钥与默认模型；`config.toml` 放其余全部可调项。用户不手改，经 `/config` 修改，程序校验后全量写回。
- **生效方式**：`runtime` 当场重建 Agent；`immediate` 下次使用生效；`restart` 下次启动生效。非法值拒绝并保持原值；文件中的非法值回落默认并告警。
- **记忆**：内置、外置、关闭互斥。外置只接收独立数据目录，自管配置、模型与存储；启动失败以关闭运行。重建主 Agent 不切来源、不重建外置实例。随包引擎 `miniviking` 走同一外置契约：默认继承 aaa 默认模型做后台蒸馏，模型不可用退化为机械摘要；数据在 `~/.aaa/memory/miniviking/`。
- **记忆查看与切换**：`/memory` 空闲时显示当前记忆引擎并弹出切换菜单；`/memory engine [引擎]` 直达切换（未知引擎拒绝）。运行中只显示状态；关闭时不读旧数据。切换只保存，下次启动生效。
- **用量账本**：`/usage` 读 `usage.db`；账本从终态 run 增量生成，按 `(渠道, 模型)` 计价，金额入账时冻结。会话分叉复制来的轮次不重复入账。
- **后台任务**：job 状态只在进程内，有界缓冲；进程退出时 `atexit` 杀掉未结束进程组。
- **工具与权限**：`boundary` 只授权能力名，`tools.assemble` 装配，数值上限来自配置；`update_user_memory` 由 Agno 内部注册，不受权限模式约束。
- **模型/执行体**：模型身份是 `(provider, model_id)`；换模型、权限模式或运行相关配置会重建主 Agent，不改变当前会话 id。
- **会话恢复**：`-c` 续接当前目录最近的 Agent 会话——先查库内目录索引，索引命中非 Agent 记录时跳过，按 `metadata.cwd` 回落到该目录最近的 Agent 会话；`-r` 按完整 id 或前缀匹配 Agent 会话；`/session` 列出并按更新时间切换 Agent 会话。旧库中已有的 Team 记录不再列出、不可续接，不迁移也不删除。
- **审批**：`boundary` 声明确认规则与签名，`cli` 负责询问、签名记忆与非交互拒绝；文件改动落盘前显示 diff。
- **取消与续跑**：取消事件不等于完成，不伪造 `cancelled` 终态；非交互 JSON 不新增字段、退出码 130；取消失败要可见且不算取消；`continue_run` 保留 run id、事件 id 与 channel；审批前或审批中被取消都不续跑。
- **历史**：Agno 原生历史是唯一回放来源；不提供清空上下文的命令，不伪造运行成功或自建回放层。
- **会话分叉**：`/fork` 回到某一轮之前——当前会话整份复制成新会话，副本只保留目标轮次之前的轮次，原会话不动；同一会话内不做树状导航。
- **打断并发出**：运行中草稿非空按 Esc，或取消后再提交消息，都登记为「停止后发送」；只在收到原生取消终态后发出，消息带打断前的请求、已发生的工具调用与新指示。
- **非交互**：默认拒绝需确认的工具；`--allow-shell` 只放行 `run_shell_command`。
- **`@文件`**：消息里的 `@路径` 在发送时附加内容——图片走图片通道，文本以 `<file>` 块拼进消息（10 万字符截断）；目录与读不了的路径保留原文并提示，补全逻辑不变。
- **启动动画**：`cli/animations/*.py` 与 `~/.aaa/animations/*.py` 同名覆盖；模块导出 `ANIMATION`（函数即 `frames(ctx)`），宿主 `cli/banner.py` 负责调度与降级；`ui.banner_animation` 切换，下次启动生效。

## 待定 / 阻塞

- **沙箱**：本期不做。接入点是 `boundary.Isolation` 的声明与可替换的 `PolicySource`。
- **隔离强度**：文件工具与 `grep` 受工作区根约束并共用排除路径；相对路径按启动目录解析；shell 的工作目录是启动目录，只有审批，无路径约束。
- **Team 任务依赖（上游）**：Agno 3.1.12 的 `execute_task` 完成后不自动刷新已阻塞的依赖。委派支持 `tasks`；领队在确认前置任务成功后，用 Agno 原生 `update_task_status` 刷新依赖。
- **真实渠道覆盖**：P0 闭环与真实工具已通过 2026-11-24 的真实渠道验收；压缩与归档检索已在 2026-11-25 重跑（P1.7、P1.8 PASS）；内置记忆写入→新会话注入以 2026-11-23 为准，未重跑；miniviking BGE 语义检索真实端到端已通过；外部引擎经真实渠道的召回注入与 `cached_tokens` 上报仍未整体实测。
- **物理终端**：三处均已用 `scripts/terminal-probe.py` 实测。iTerm2：Shift+Enter `ESC[27;2;13~`、Alt+Enter `ESC[27;3;13~`、Ctrl+Enter `ESC[27;5;13~`、Shift+Tab `ESC[Z`，四者互不混淆，owner 另用物理按键确认换行生效。Terminal.app：不支持扩展键——Shift+Enter 与 Enter 同为 CR、Alt+Enter 为 `ESC`+CR、Ctrl+Enter 无任何字节（被终端吞掉），全程无 CSI u / modifyOtherKeys 序列；外部依据为 fsck.com 终端键盘协议综述（2026-02）与仓库两处笔记，反证源 terminfo.dev（2026-10）同页正文与表格自相矛盾，不采信。另在真实 Terminal.app 窗口里读过未改动的环境（2026-11-25）：`TERM_PROGRAM=Apple_Terminal`、`TERM=xterm-256color`、`COLORTERM=truecolor`，`supports_modified_keys()` 判为 False，白名单依据的环境变量名属实。本机 tmux 3.6a（2026-11-25，脚本模拟外层终端向 tmux 客户端写入 `ESC[27;2;13~`）：默认配置下窗格内只收到裸 `CR`；加上 `set -s extended-keys on` 与 `set -as terminal-features "xterm*:extkeys"` 后原样透传。Omarchy 主机（`thinkbook-omarchy`，tmux 3.8，owner 授权的只读检查）：`~/.config/tmux/tmux.conf` 已设 `extended-keys on` 与 `extended-keys-format csi-u`，外层特性只登记 `xterm-kitty:extkeys`；脚本模拟外层 iTerm2 向 tmux 写入 `ESC[27;2;13~` 或 `ESC[13;2u`，窗格内均收到 `ESC[13;2u`，`cli/key_input.py` 认该形式，兼容。外层为 `xterm-kitty` 时 tmux 起不来（远端没有该 terminfo），只影响从 Mac 以 `TERM=xterm-kitty` ssh 过去的场景。代码事实是 `supports_modified_keys` 用白名单（iTerm2/WezTerm/vscode/ghostty/Windows Terminal/tmux），不含 `Apple_Terminal`。aaa 现在在 tmux 内且未开启 `extended-keys` 时启动会提示怎么修（`key_input.tmux_extended_keys_hint`）。已知限制（只说明、不修）：经 ssh 直连远端时终端类型不随 ssh 传递，Shift+Enter 不可用，见 `docs/commands.md`。未覆盖：WezTerm、ghostty、VS Code、Warp、Windows Terminal 本机未安装（或需 Windows）；「iTerm2 + 本机 tmux」未在真机合测。
- **发布**：发版门已就绪（`scripts/release.py` 人建 tag、手动 dispatch、`environment: release` 审批、草稿 Release）；尚未发版，`release.py tag` 的完整成功路径未跑（会创建真实 tag），早期检查与 pre-push 兜底已验证。PyPI/testPyPI 通道未实现；发行名待定——`aaa` 在 PyPI 与 testPyPI 均已被占，候选 `amaterasu-agno-agent` 两边可用，owner 未拍板。
- **平台**：Windows 已修复并在 GitHub 的 Windows 机器上实测通过（2026-11-25，见「Windows 实测」节）：启动、中文输出、文件与命令工具、后台任务、假模型对话、打包均正常；单测 486 项全过（跳过 15 项均为平台性限制：扩展键开关只在 POSIX 终端、`/bin/echo` 参数传递、shell 假渲染器、符号链接需权限）。界面符号与图标全平台统一，不再回退 ASCII。剩余项见「下一步」。

## 下一步

按优先级排列。扩展协议五阶段与终端呈现升级（图片协议、可插拔外部渲染器、主题语义 token）均已交付（见「当前验证证据」），其余为既有欠账。

1. 终端键位与全路径：本机可做的都已完成（见「物理终端」条与各验证节）。剩余——
   a. SSH 直连远端时 Shift+Enter 不可用：终端类型（`TERM_PROGRAM`）不随 ssh 传递，远端的 aaa 认不出终端、不开启扩展键；套 tmux 不受影响。owner 2026-11-25 决定：不属于产品问题，只在 `docs/commands.md` 说明，不修。
   b. 「iTerm2 + 本机 tmux」真机组合的 Shift+Enter：两端分别验证过，未在真机合测；本机 `~/.tmux.conf` 未配置，加 `set -s extended-keys on` 与 `set -as terminal-features "xterm*:extkeys"` 后即可试。
   c. WezTerm、ghostty、VS Code、Warp、Windows Terminal：本机未安装或需 Windows，仅凭白名单放行，未真机验证。
2. 首次正式发版：owner 确认版本与发行名 → `scripts/release.py tag` → 手动 dispatch + environment 审批 → 草稿发布。
3. testPyPI 演练 → PyPI Trusted Publishing。
4. 外部引擎（miniviking）真实召回与 `cached_tokens` 上报核实。
5. Windows 收尾：`windows` 作业已并入 `ci.yml`，每次 push/PR 在 `windows-latest` 上跑启动、工具、后台任务、假模型对话与打包，失败即变红；测试分支与其独立工作流已删除。剩余——
   a. 真实终端按键（Shift+Enter、`Ctrl-J`、补全菜单）未验证：Windows 走 prompt_toolkit 的 Win32 输入，CI 无法模拟按键。
   b. Windows Terminal / conhost 的界面呈现（统一符号在缺字字体下的样子、状态行宽度）未真机走查。
   c. 子进程输出编码按区域编码解码，与前台一致；若某个外部程序输出与系统代码页不同（例如强制 UTF-8 的老工具），仍可能乱码——需要按程序指定编码时再议。

## 待办方向（owner 2026-11-25 列入）

四项大方向，均未拆解；每项动工前先出方案（边界、契约、验收），不直接实施：

1. 补齐算力模型渠道协议：`model/provider.py` 现只映射 `deepseek` 与 `openai-completions` 两种协议；缺哪些协议、渠道字段与模型探测面补到什么程度，待拆。
2. 增强工具能力：在现有能力集合（文件、检索、shell、PowerShell、后台任务、任务清单、委派、MCP、技能）之外按真实需求补工具，清单待定。
3. 完善内置技能：`builtin_skills` 现有 create-agent、create-extension、create-team、create-workflow 四个，按使用场景补齐，范围待拆。
4. 设计对齐产品的语义：产品语义先行，架构与模块 `DESIGN.md` 按产品语义逐条复核对齐，范围待拆。

## 当前验证证据

按工作线分组，只保留最新证据；历史批次在版本控制记录中。

### 技能包（目录即包，2026-11-25）

- 环境：macOS arm64，`.venv` Python 3.13。单测用临时目录；界面走查用 PTY（默认 80 列）、临时 HOME、合成技能（平铺 1 个 + `lark-cli` 包内 2 个，附 `PACK.md`）与指向 `127.0.0.1:9` 的假渠道。
- 变更：撤 TOML 声明式包。发现改 Hermes 语义——任意深度，含 `SKILL.md` 的目录即技能且不再下钻（点目录、符号链接目录、技能支持目录不进）；`<根>/<包>/…` 归包，包名取根下第一段；`PACK.md` 给包描述；`.disabled` 支持技能与包两级停用。索引 names-only（`PackSkills`：包条目 = 名字 + 描述 + 成员名单，包内技能描述省略、名字保留；read-only 变体 `without_scripts()` 保留包视图并收起脚本工具）。信任指纹与发现共用遍历：嵌套技能文件与 `PACK.md` 全部进指纹。`GatedSkills` 由 `PackSkills` 取代，TOML 读写、`SKILL_PACKS_FILE` 与 `packs_file` 装配参数移除。
- 命令与步骤：`uvx ruff==0.17.6 check src tests`；`./tests/run.sh`（484 项 OK，skip 1）；`tests/test_skill_packs.py` 18 项；PTY 里 `/skills`、`/insight`、`/plugin` → 回车停用 → 重启 → `/skills`、`/insight` → `/plugin lark-cli` 启用 → 重启 → `/skills`。
- 预期依据：`docs/extending/skills.md`「技能包」一节，`src/aaa/runtime/DESIGN.md` 装配节的技能条。
- 结果：ruff 全过，484 项 OK。单测覆盖：递归发现（平铺与包内并存、支持目录与点目录不误判、符号链接不下钻）、两级停用、跨根同名优先级不变、names-only 渲染（包条目含描述与成员名单、包内描述省略、成员正文可读、无包技能保持完整条目）、read-only 保留包视图并收脚本工具、信任指纹含嵌套文件与 `PACK.md`、`/plugin` 切包写 `.disabled`、`/skills` 带 `[包名]`。PTY：初始 `/skills` 列出平铺与包内技能（`[lark-cli]` 标签）；`/plugin` 停用 `lark-cli` 提示重启生效；重启后 `/skills` 只剩平铺技能；启用后恢复。
- 独立 QA（`QaDirPacks`，4 分 14 秒，只读；受审修订 HEAD `a86ba1b` + 工作区）：9 条全部 PASS，无阻断缺陷；发现 1 项低危 D1——跨根同名时包归属与装配赢家脱节（平铺赢家被标成包成员），已修复并补回归测试；残余：索引替换正则绑定 Agno 3.1.12 的文本格式（升级改格式会静默退化）、名为支持目录的包不可见、`/insight` 面板数值与 Windows 未覆盖。
- 真机 lark 包：`~/.agents/skills/lark/` 收纳 28 个 `lark-*` 技能，`PACK.md` 写触发场景；进程内装配 92 个技能、索引 12,306 → 5,374 tokens（−56%），成员描述省略、名字保留。注意：`.skill-lock.json`（安装器账本）对这批记录已失真，安装器同步可能重装回平铺；omp 新会话看不到包内技能。

### e2e 渠道与临时目录（2026-11-25）

- 环境：macOS arm64，`.venv` Python 3.13；真实渠道，默认渠道为 `deepseek/deepseek-flash`，每个用例独立临时 HOME。
- 命令与步骤：`uvx ruff==0.17.6 check tests/e2e.py tests/pty_cases.py`；`tests/e2e.py --suite p1`；`tests/e2e.py --suite p0 --only P0.6`；`tests/e2e.py --pty --only P2.12`。
- 预期依据：P1.7、P1.8 的窗口改在本次运行真正使用的渠道上（与 P0.6 同一个取法），自动压缩按 `context_window − 16384` 的阈值触发；P2.12 改用本机第一个 deepseek 协议渠道，没有则 SKIP；临时目录统一建在本次运行的根下，跑完删除，有失败或带 `--keep-temp` 时保留并打印路径；PTY 读取按截止时间返回，不再被静默子进程无限阻塞。
- 结果：ruff 全过。P1.7 第 4 轮触发自动压缩 PASS；P1.8 PASS（答对但未走检索工具，可能仍在保留尾内）；P0.6 换用共用取法后 PASS（exit=1，日志有 ERROR）；P2.12 PASS（11 秒，探到 1 个模型，落盘权限 0600）。P1 合计 52.6 秒。成功跑完临时目录数不变（29→29）；`--keep-temp` 保留并打印路径；失败的运行保留目录。
- 过程：P2.12 先按「把渠道地址打进 Base URL」改过，向导的预填默认值会追加成非法地址、探查失败；改回接受向导默认值后通过。
- 证据位置：本节与 `tests/e2e.py`、`tests/pty_cases.py`。
- 未覆盖：P1.8 未实际走到 `search_compacted_history`；`--no-proxy` 仍不生效（`STRIP_PROXY` 只赋值、无人读取），本轮未修。

### 会话分叉、打断并发出、中文提示词与工具作用范围（2026-11-24）

- 环境：macOS arm64，`.venv` Python 3.13，agno 3.1.12，HEAD `e0d3412` + 本线未提交改动；PTY 110×34，`TERM=xterm-256color`，临时 HOME，渠道指向本地慢速流式假端点；真实渠道验收用默认渠道 `deepseek/deepseek-flash`，临时 HOME，保留 HTTPS 代理。
- 命令与步骤：`./tests/run.sh`；`tests/e2e.py --suite p0`（真实渠道）；伪终端里用 `-r` 续接预置三轮的会话，执行 `/fork` → ↓ → Enter，在预填的草稿上补字后回车，流式输出期间输入一句话并按 Esc；改错验证 5 处（不传启动目录、先列完整棵树、grep 不做排除、去掉相对路径解析、`list_files` 不换算路径）。
- 预期依据：`src/aaa/cli/DESIGN.md`「打断并发出」「会话分叉」两节，`src/aaa/sessions/DESIGN.md`，`src/aaa/runtime/DESIGN.md` 提示词一条，`src/aaa/tools/DESIGN.md` 与 `src/aaa/boundary/DESIGN.md` 里关于作用范围的条目。
- 结果：离线回归 388 项 OK（9.8 秒）。三个权限模式下命令都在启动目录执行，全权限档的相对路径按启动目录解析，`list_files` 给出的路径能原样传回其他工具；`grep` 搜不到 `.env`、`node_modules` 下的内容，豁免项照常可搜。`/fork` 列出三轮，选中第 2 轮后提示「已分叉到会话 …，回到第 2 轮之前」，草稿预填原句；新会话里模型收到的上下文只有第 1 轮与新的提问。运行中按 Esc 后提示「已请求打断，运行确认取消后立即发送」，确认取消后自动发出；后一次请求的最后一条用户消息带打断前的请求与新指示，历史里没有被取消的那一轮。提示词按工具裁剪，用假模型实际装配三种权限模式，系统消息各段齐全、`<cwd>` 正确、无重复；workspace-write 全量装配约 2,500 字符。真实渠道 P0 11/11（26 秒）。改错验证 5 处全部被测试抓到。
- 证据位置：本节与 `tests/run.sh`、`tests/e2e.py --suite p0` 的输出；`tests/test_session_fork.py`、`tests/test_cli_interrupt.py`、`tests/test_prompt_assembly.py`、`tests/test_tools_scope.py`。
- 未覆盖：Windows；压缩记录较多的长会话分叉；审批待决与打断相邻的路径只有单测；P1（压缩、归档检索）未在真实渠道重跑；分叉、打断与提示词三项没有独立 QA 结论。
- 独立 QA：分叉与打断（`QaForkInterrupt` 37 分钟）、提示词（`QaZhPrompts` 29 分钟）两个只读 QA 任务按 owner 要求中止，未出结论，以主 Agent 的自测证据交付；提示词 QA 中止前的有效发现是 grep 不受排除路径约束，已修复。工具作用范围的修复由 `QaScopeFixes` 验收（3 分 36 秒，受审修订 HEAD `e0d3412` + 工作区）：三项 PASS；发现 1 项低级缺陷——全权限档 `list_files` 的路径不能原样回读——已修复并补测试。

### 技能装配移交 Agno（2026-11-24）

- 环境：macOS arm64，`.venv` Python 3.13，agno 3.1.12；本机真实技能数据（project 1 + user 89 + builtin 4）。
- 变更：技能发现/披露/读取整体移交 `agno.skills`——`plugins.build_skills`（loader 按 builtin → user → project，`validate=False`）、`Agent(skills=…)` 原生注入 `<skills_system>` 索引与 `get_skill_instructions`/`get_skill_reference`/`get_skill_script` 三工具；删除自建 `discover_skills`/`skill_body`/`make_skill_tool`/`skill_context` 与 `prompts/skills.md`；`create-extension` 引用改为 `references → docs/extending` 符号链接，打包双路径适配（wheel 直构跟随链接收集、sdist exclude 链接 + force-include 实文件，`uv build --wheel` 与 sdist→wheel 均验证无重复）；`/skills`、`/insight`、内置技能正文与 `docs/extending/skills.md` 同步更新。脚本执行进模型信任面，owner 已明确接受（2026-11-24 本会话）。
- 命令：`./tests/run.sh`；`plugins.build_skills` 真实数据装配；`uv build` 后核对 wheel 内 `create-extension/references`。
- 预期依据：`src/aaa/runtime/DESIGN.md` 装配节、`docs/extending/skills.md`、agno `skills/agent_skills.py`。
- 结果：353 项 OK；真实装配 94 个技能（来源分布 1/89/4）、同名覆盖语义 project > user > builtin 与迁移前一致、snippet 约 3.9 万字符含 `<skills_system>`、三技能工具在列；wheel 内 9 份 `references/*.md` 实文件齐全；符号链接逃逸在发现后仍被路径安全检查拒绝。
- 证据位置：本节与 `tests/run.sh` 输出；`dist/` 构建产物验证后保留未删（gitignore 覆盖，不入库）。
- 未覆盖：`get_skill_script(execute=true)` 对真实技能的执行未实测（QA 订正：本机 89 个用户技能中 7 个带 `scripts/`，共 29 个脚本，如 lark-sheets 9 个；QA 用临时目录合成脚本验证了执行通路）；Windows 布局未跑。
- D1 决策与收紧（owner 2026-11-24 本会话拍板，选项 c）：技能纳入仓库信任类别——`trust.KINDS` 新增 `skills`（指纹覆盖 `.aaa/skills/<名>/` 全部文件，含 `scripts/`），项目技能目录仅在信任后装配（`AppState.start(trusted=…)`，非交互未信任时跳过并提示 `--trust-repo`）；read-only 不开放脚本执行——`runtime.skills_for_mode` 在合并点换用 `plugins.GatedSkills`（移除 `get_skill_script` 工具，索引中脚本清单置 `none` 且不宣传该工具），`/insight` 同步按模式计数。残余：workspace-write 非交互不带 `--allow-shell` 时技能脚本仍可执行（owner 仅限定 read-only）。
- 变更附带影响：分类目录 `<分类>/<技能名>/SKILL.md` 不再被发现（Agno 只扫一层，静默丢弃）；技能索引体积从约 6,946 tokens 增至 12,306 tokens（索引现列出引用与脚本清单），每轮随系统消息发送。
- 独立 QA（`QaSkillMigration`，43m20s，只读；受审修订 HEAD `943e313` + 工作区 diff）：主流程验收 1–4 通过（353 项 OK、真实装配 94 个技能且来源/覆盖方向正确、三工具正文与引用逐份比对一致、越界路径被拒、系统提示含 `<skills_system>` 且无正文泄漏）；发现并推动修复 `uv build --wheel` 直构失败（符号链接 + force-include 双份写入，R2 打包配置复核通过）；缺陷 D3–D8（miniviking 文档残留、来源标签按目录名而非 frontmatter 名、四处「纯文本不执行」过期表述、畸形 frontmatter 使 `/skills` 抛异常、DESIGN/HANDOFF 旧签名）当轮已全部修复并补 D4/D6 回归测试；D1（技能脚本执行绕过权限模式与 `--allow-shell`，含未信任仓库的 `.aaa/skills` 脚本）待 owner 决定。防假测试 11 处改错抓到 6 处；`/insight` 接线与排序等 5 处测试缺口未补。证据目录 `/tmp/aaa_qa_skills.Ba3e2a/`。

### Windows 实测（2026-11-25）

- 环境：GitHub Actions 的 `windows-latest`（Windows 10.0.26100，x64），uv 0.13.10，Python 3.13。摸底阶段跑在测试分支 `test/windows-ci` 的独立工作流上；修复后该作业已并入 `ci.yml`，测试分支已删除。仓库公开，运行免费；日志公开，全程只用假渠道与本地假模型。
- 变更：四处修复——`cli/key_input.py` 的 VT100 输入类只在 POSIX 分支定义（Windows 上该模块导入即断言）；Windows 启动时把 stdout/stderr 重设为 UTF-8；`danger-full-access` 的锚点根下绝对路径先折算成根相对路径再交给 Agno（Windows 上带盘符的路径与其他盘符不再被判越界）；Windows 上按 Windows 规则拆 MCP 启动命令并以结构化参数交给 Agno（Agno 的 `command=` 走 POSIX 拆词）。信任指纹与描述统一用正斜杠；依赖平台符号、`pwd`、`/bin/echo` 与类 Unix 绝对路径的测试改用产品符号表、平台命令与自建目录，后台任务与扩展键开关测试在 Windows 按设计跳过。工作流新增重定向中文帮助探针与汇总步骤：任一探针或测试失败时任务变红。修复提交为 `cb7c02c`（main），Windows 作业并入 `ci.yml` 的提交为 `9ccd6b7`。
- 命令与步骤：首版与修正版（`45571926465`、`45575349762`）用于摸底；修复后两次（`45579784195` 红、`45581571970` 绿）。每轮依次安装依赖、默认编码探针、单元测试（`python -m unittest discover`）、启动与工具直调（含 `--version`、重定向中文 `--help`、导入 `key_input`）、假模型下跑一轮对话、构建 wheel、汇总；日志作为产物 `windows-logs` 保留 14 天。
- 预期依据：aaa 在 Windows 上应能启动、跑完一轮对话并使用工具；单测全过或按设计跳过。
- 结果（最终 `45581571970`）：全部步骤成功。单测 `Ran 447 tests … OK (skipped=9)`；`--version`、重定向中文 `--help`、导入 `key_input` 均退出 0；全权限模式下相对路径与 `C:/Windows/win.ini` 都能读写，`list_files` 正常；后台任务返回「不支持 Windows」的明确提示；PowerShell 工具（pwsh 7.6.10）控制台编码 utf-8、中文输出正常；`trust.describe` 输出 `.aaa/skills/toy/SKILL.md`；假模型对话输出中文无编码异常；wheel 构建成功。摸底轮曾出现 25 项问题，逐条对应上面的修复或测试调整。
- 未覆盖：真实终端里的按键（CI 无法模拟）；Windows Terminal / conhost 的界面呈现；npm/npx 一类 `.cmd` 启动的 MCP 服务（拆词与 `.cmd` 处理有代码路径，未实跑）。
- 证据位置：GitHub 运行 `45581571970` 的日志与 `windows-logs` 产物；本节。

### Windows 能力对齐：后台任务、整树终止与符号统一（2026-11-25）

- 环境：macOS arm64（本地 POSIX 回归）与 GitHub Actions `windows-latest`；uv 0.13.10，Python 3.13；分支 `feat/windows-parity`，合并后删除。
- 变更：Windows 上启用后台任务——每个 job 一个 Job Object（`KILL_ON_JOB_CLOSE`），`job_kill`、前台超时与退出回收都作用于整棵进程树，Job Object 建不起来时退回 `taskkill /T /F`；子进程输出前后台统一按区域编码解码；提示词不再在 Windows 隐藏后台任务段，工具注册、提示词与错误信息口径一致；界面符号与命令图标全平台统一，去掉 ASCII 回退。
- 命令与步骤：本地 `./tests/run.sh` 全量；CI 跑 `ci.yml` 的 `windows` 作业（启动、工具直调含后台任务探针、假模型对话、打包、汇总）。新增用例「杀 job 带走子进程」两端都跑：POSIX 用 `kill(pid,0)` 复核，Windows 用 `tasklist` 复核。
- 预期依据：`src/aaa/tools/DESIGN.md`（整树回收与解码一致）、`docs/commands.md`「平台」。
- 结果：分支运行 `45591569357` 四个作业全绿；Windows 单测 `Ran 486 tests … OK (skipped=15)`，工具直调里后台任务返回 `job … started` 而非拒绝；POSIX 本地 486 项通过。
- 未覆盖：Windows 真实终端按键与界面观感；外部程序输出编码与系统代码页不一致时的乱码。
- 证据位置：GitHub 运行 `45591569357` 与 `windows-logs` 产物；本节。

### 公式链真实 TeX 验收、SSH 与 Omarchy tmux（2026-11-25）

- 环境：macOS arm64，`.venv` Python 3.13；owner 授权本机安装组件做验证、连接 Omarchy 主机检查 tmux。TeX Live 2026（scheme-minimal + collection-latex + dvipng + standalone + preview）装在 `/tmp/aaa-texlive`（196MB，不进 PATH、不写 `$HOME`，从清华 CTAN 镜像安装共 86 秒）；`latex` 为 pdfTeX 1.40.29，`dvipng` 1.18。
- 变更：修正公式包装——`standalone` 文档类把正文装进盒子，行间公式标记 `\[…\]` 在其中非法，原包装在真实 TeX 下报 `Missing $ inserted`、整条公式链失效；改为 `$\displaystyle …$`，并剥掉公式自带的外层行间标记。新增可选测试 `RealTexTest`（装有 `latex` 与 `dvipng` 才跑，否则跳过），以及包装形态与剥标记的单测。
- 命令与步骤：`./tests/run.sh`；`PATH` 含 TeX 时单独跑 `tests.test_cli_renderers.RealTexTest`；端到端用本地假模型服务驱动真实 `python -m aaa`——先直接调渲染器，再在 REPL 里流式输出两个 `$$` 块，随后输出一个非法公式；打开产物 PNG 目视核对。
- 预期依据：`src/aaa/cli/DESIGN.md`「可插拔外部渲染器」。
- 结果：477 项 OK；ruff 全过。真实 TeX 下：直接渲染 `\frac{a}{b}+\sqrt{x^2+1}=\sum_{i=1}^{n}x_i` 得 260×76 PNG；REPL 内两个合法公式块出 2 张图（1KB、2KB），公式源码未回显，前后文字完好；非法公式回落为源码块且不出图。产物目视正确：分式、根号、带上下限的求和与 `E = mc²` 均无误。反向验证：把包装临时换回 `\[…\]`，真实 TeX 下渲染失败，说明 `RealTexTest` 能抓住这类退化。常规环境下 17 项渲染器测试 OK（`RealTexTest` 跳过）。
- SSH 与 tmux：查到一个限制——ssh 默认不转发 `TERM_PROGRAM`（本机 sshd 的 `AcceptEnv` 只有 `LANG LC_*`），直连远端的 aaa 认不出终端、不会开启扩展键（套 tmux 不受影响）。owner 2026-11-25 决定：不属于产品问题，只在 `docs/commands.md` 说明，不修。本机 SSH 自连测试不做（owner 指示本机连本机不合适）；此前的一次尝试在认证处被拒，其临时 known_hosts 文件已删除。Omarchy 主机只读检查结果见「物理终端」条；测试用唯一临时会话名，未向远端写文件，结束后确认无 tmux 进程残留。
- 未覆盖：「iTerm2 + tmux」真机组合；装在 `/tmp` 的 TeX 重启后消失，未验证重启后的行为（按设计未装即回落源码）。
- 证据位置：本节与 `tests/run.sh` 输出；代码未提交，提交需 owner 指令。

### 公式渲染链与单会话全路径走查（2026-11-25）

- 环境：macOS arm64，`.venv` Python 3.13；owner 指示「剩下的也要做，继续推进」。公式链探查：本机无 latex/dvipng/tex2svg/mathjax/katex/ImageMagick/rsvg/matplotlib，仅有 node 与 mermaid-cli。
- 变更：(1) 渲染器扩成多步流水线——`cli/renderers.py` 新增 `RenderStep`，`RendererSpec.steps` 为命令序列，`available()` 要求整条流水线命令齐全才注册；`render()` 逐步执行，任一步非零退出即停；缓存键改为「流水线 + 源码」。内置登记公式链 `math → latex → dvipng`（公式包成 `standalone` 最小文档）。(2) `split_block` 同时认带语言的代码围栏与 `$$…$$` 公式块，`MarkdownStream.on_code_block` 改用它；块内再含 `$$` 的散文不认。
- 命令与步骤：`./tests/run.sh`；`tests/test_cli_renderers.py` 15 项（在原有 8 项上新增：`$$` 块进钩子并取到内层源码、公式链需两步命令齐全才注册、两步依次执行且源码被包成 TeX 文档、首步失败即停且末步不执行、`$$` 块单行与多行识别、含多个 `$$` 的散文与普通段落不误判）。单会话全路径走查：用本地假模型服务（按最后一条消息回文字、发 `run_shell_command` 工具调用、慢速流）驱动真实 `python -m aaa`（临时 HOME、伪终端 140 列、`TERM_PROGRAM=iTerm.app`），同一会话内依次执行 9 步，连跑两遍。
- 预期依据：`src/aaa/cli/DESIGN.md`「可插拔外部渲染器」、`docs/commands.md` 键位表。
- 结果：475 项 OK；ruff 全过。走查两遍均 9/9——启动进入 REPL；Shift+Enter 插入换行且模型请求数不变；多行括号粘贴不提交；普通一轮对话完成；`/mode workspace-write` 后工具调用先出审批面板、回车后执行出 `approval-ok` 并续跑完成；慢速流中按 Esc 取消本轮；`/help` 面板打开后 Esc 关闭、输入区回到面板下方；`/todos` 面板同上；窗口内双击 Ctrl-C 退出码 0 且写出扩展键还原序列。
- 未覆盖：走查中的按键是向伪终端写入 iTerm2 会发的字节序列，不是 iTerm2 窗口里的物理按键；走查未覆盖 `/fork`、`/redo`、`/edit`、会话恢复与压缩触发。公式链当时只用假命令验证，真实 TeX 验收见上一节。
- 证据位置：本节与 `tests/run.sh` 输出；走查脚本为一次性脚本，未入库；代码未提交，提交需 owner 指令。

### 主题语义 token、加载失败可见、tmux 透传与提示（2026-11-25）

- 环境：macOS arm64，`.venv` Python 3.13，tmux 3.6a；owner 指示「先改能够改的，推进剩下的」。
- 变更：(1) 主题改为语义 token——`cli/ui.py` 用 `TOKENS`/`THEME_TOKENS`（19 个 token）+ `CLASS_TEMPLATES`（类名到样式模板）+ `THEME_CLASS_OVERRIDES`（contrast 8 项、neon 1 项显式覆盖）生成样式表，改配色只动 token，新增界面元素只加一行模板。(2) 扩展加载失败在终端可见——启动摘要点名加载失败的编程式扩展与连接失败的 MCP 插件。(3) tmux 键位提示——`key_input.tmux_extended_keys_hint` 在 tmux 内且 `extended-keys` 未开启时启动即提示怎么修；`docs/commands.md` 的 `Shift+Enter` 说明补上确切的两行配置。
- 命令与步骤：`./tests/run.sh`；`tests/test_cli_theme_tokens.py` 3 项（三主题最终样式串与重构前字面量逐项相等、每主题定义全部 token、模板占位符均为已定义 token 且覆盖不指向不存在的类）；`tests/test_extensions.py` 的 `LoadFailureVisibilityTest`；`tests/test_cli_key_input.py` 的 `TmuxExtendedKeysHintTest` 4 项。tmux 两段端到端——(a) 脚本扮演外层终端，向 tmux 客户端的终端写入 `ESC[27;2;13~`，在默认配置与开启 `extended-keys` 两种配置下各跑一次探针，看窗格内收到的字节；(b) 在真实 tmux 会话里启动 aaa（临时 HOME + 本机假渠道），两种配置下各看一次启动输出是否出现修复提示。
- 预期依据：`src/aaa/cli/DESIGN.md`、`docs/commands.md` 键位表、HANDOFF「物理终端」条。
- 结果：468 项 OK；ruff 全过。(a) 默认配置窗格内收到裸 `CR`，开启后原样收到 `ESC[27;2;13~`；(b) 默认配置启动出现「tmux 未开启 extended-keys」提示，开启后不出现。主题重构由金标准测试确认界面零变化（测试先抓到两处偏差——contrast 表头 token 选错、neon 指针词序——修正后逐项相等）。
- 未覆盖：tmux 透传用脚本模拟外层终端，证明的是 tmux 的转发配置成立，不是「iTerm2 + tmux」的真机组合；WezTerm、ghostty、VS Code、Windows Terminal、SSH 均未真机验证。
- 证据位置：本节与 `tests/run.sh` 输出；代码未提交，提交需 owner 指令。

### 终端内图片与外部渲染器（2026-11-25）

- 环境：macOS arm64，`.venv` Python 3.13；iTerm2（`TERM_PROGRAM=iTerm.app`）；本机装有 mermaid-cli 11.16.0（`mmdc`）；端到端用临时 HOME + 指向本机假渠道/假模型服务的真实 `python -m aaa` 进程（不触真实模型）。
- 变更：`src/aaa/cli/images.py`（环境变量探测、OSC 1337 与 kitty graphics 编码含分块、PNG/GIF/JPEG 尺寸解析、`[图片] 名称 WxH NKB` 文本降级、「整行即存在的图片路径」识别）；`ui.images`、`ui.image_max_kb`、`ui.external_renderers`、`ui.render_timeout` 进键表；`app.py` 三个入口——`/image` 本地预览、工具结果整行图片路径自动渲染、`MarkdownStream.on_code_block` 接管 Mermaid 围栏；`src/aaa/cli/renderers.py`（围栏语言 → 外部命令 → PNG 的规格表、`mmdc` 探测、按「命令 + 源码」哈希缓存、超时与失败返回空）；渲染在工作线程执行，成功出图，失败回落源码块。带转义序列的终端写入改走 `app.output.write_raw`。`docs/commands.md` 与 `cli/DESIGN.md` 同步。
- 命令与步骤：`./tests/run.sh`；`tests/test_cli_images.py` 13 项与 `tests/test_cli_renderers.py` 8 项；PTY 端到端三段——(1) `/image` 一张 380KB 的 PNG，抓原始字节核对 OSC 1337 载荷与原文件逐字节一致、序列后紧跟换行与界面重绘；(2) 假模型流式输出合法 Mermaid 围栏，核对图片上屏且源码不再作为代码块回显、前后文字完好；(3) 假模型输出非法 Mermaid，核对回落为源码块且不出图。
- 预期依据：`src/aaa/cli/DESIGN.md`「终端内图片」「可插拔外部渲染器」。
- 结果：459 项 OK；ruff 全过；端到端 (1)(2)(3) 全部符合预期（合法图 10.9KB PNG，真 `mmdc` 单次渲染约 0.4 秒）。端到端过程中抓到并修复一个真缺陷：`output.write` 会把 `ESC` 换成 `?`，图片序列此前发到终端的是乱码。
- 未覆盖：屏上像素观感未由人眼确认（字节流已核对）；kitty 协议未在 kitty/Ghostty 真机验证；tmux 内按设计直接降级未测透传；公式渲染无内置规格（接口已就绪，缺「一步产出 PNG」的常见命令）。
- 证据位置：本节与 `tests/run.sh` 输出；代码未提交，提交需 owner 指令。

### 扩展生态收口：接口欠账、面板命名空间、作者文档（2026-11-25）

- 环境：macOS arm64，`.venv` Python 3.13，agno 3.1.12。
- 变更：补齐审定接口面的三处欠账——`ctx.settings`（只读视图，未知键返回默认值，无写入口）、`ctx.log`（`aaa.ext.<名>` logger，写入 aaa 日志）、`ctx.register_command`（与 `commands/*.md` 同名字空间、同 `$ARGUMENTS`，文件模板优先，名字经 `[a-z0-9][a-z0-9_-]*` 校验）；`ctx.home` 暴露扩展目录。面板名自动加 `<扩展名>/` 前缀（不同扩展同名可共存，同一扩展重名仍拒绝加载），`/panel` 支持唯一短名。新增作者文档 `docs/extending/extensions.md` 与可运行示例 `docs/extending/examples/hello/`（在测试中实际加载，防止示例腐烂），并在 `docs/extending/README.md` 一览与 `create-extension` 技能表登记。
- 命令：`./tests/run.sh`；`tests/test_extensions.py` 30 项（新增：settings 只读/未知键默认/无 set、日志前缀、命令注册校验与同名拒绝、扩展命令与 md 命令合并且 md 优先、面板短名解析与跨扩展同名、示例扩展全挂载点加载）。
- 预期依据：`src/aaa/extensions/DESIGN.md`、`docs/extending/extensions.md`。
- 结果：420 项 OK；ruff 全过。
- 未覆盖与边界：开发循环仍为「重启生效、无 /reload」；示例扩展未在真实模型会话里跑（只验证装载与注册）。加载失败的终端提示在本节当时未做，已于 2026-11-25 补上（见「主题语义 token、加载失败可见、tmux 透传与提示」节）。前四阶段的遗留未覆盖并入本条：`ext_` 工具走真实 HITL 审批流未端到端测过；`/plugin` 不显示编程扩展的启停；面板逻辑有单测但未起 PTY 走查；提示词块在真实模型下的行为未测；扩展间通信未设计（留待真实需求）。
- 证据位置：本节与 `tests/run.sh` 输出；代码未提交，提交需 owner 指令。

### 工具调用前钩子与审批第三档（2026-11-25）

- 环境：macOS arm64，`.venv` Python 3.13，agno 3.1.12；owner 决定开前置钩子、按建议修确认语义并加会话级放行。
- 变更：`ExtensionContext.intercept_tool(handler)`——注册工具调用前拦截器，`ToolCall`/`ToolDecision` 契约，拒绝以说明作为工具结果；`extensions.loader.tool_guard` 挂 Agno 原生 `Agent(tool_hooks=…)`（装配点，`delegation._rejection` 同款机制），只在有拦截器时挂。契约：只能收紧（不放行需确认的调用、不改参——Agno 的确认暂停在钩子之前），异常或超时（新键 `extensions.hook_timeout`，默认 5 秒）视为拒绝并写日志，同一扩展 3 次失败会话内挂起。修正 `confirm="mode"` 语义为跟随模式确认名单（原实现在 read-only 也询问，偏离审定接口面）。审批加第三档「本会话总是允许」：`AppState.tool_allows`（会话级、不落盘）按工具名放行，`approve_requirements` 先查会话放行再询问；文件改动类工具不提供该档。
- 命令：`./tests/run.sh`；`tests/test_extensions.py` 24 项（新增：mode 确认语义两档、会话放行与文件审阅不记忆、拦截器拒绝/放行/异常三次挂起/超时拒绝、Agent 运行内工具被拦截）。
- 预期依据：`src/aaa/extensions/DESIGN.md` 关键约定「工具调用前拦截」「审批第三档」「确认规则」。
- 结果：413 项 OK；ruff 全过。
- 未覆盖与边界：交互审批三档 UI 未起 PTY 走查（现有审批相关测试全绿，行内三选项不改变高度）；钩子只覆盖主 Agent，委派子 Agent 的工具调用不经拦截（子 Agent 本就不执行需确认的动作）；拦截线程超时后无法终止，只在后台跑完；宪法「成本治理不进运行路径」红线文字未动，owner 表示「或许可以调整」，钩子契约对用途中立，是否改写红线待 owner 明确。
- 证据位置：本节与 `tests/run.sh` 输出；代码未提交，提交需 owner 指令。

### 仓库、CI 与发布门（2026-11-24）

- `main` 已推送；PR #1（Dependabot `setup-uv`）已 squash 合并，合并后 CI run `45210930919` 的 `test`、`secret-scan`、`lint` 全部 success。
- GitHub：`main-protection` / `release-tags` ruleset active，bypass 仅 `LMQH`；`release` environment required reviewer `LMQH`、`can_admins_bypass=false`、部署分支 `main`/`v*`；Actions 默认只读；secret scanning、push protection、Dependabot alerts/security updates 已开启。
- 开发入口：`scripts/aaa-dev.sh` 从 `/tmp` 与 `pi` 目录执行 `--version` 均为 `aaa 0.0.0+dev`；本机 wrapper 为 `~/.local/bin/aaa-dev`。


