# CLI 交互基线核实

结论（高置信）：AAA 要的 omp 式内联体验可用 Python prompt_toolkit 主屏 Application 实现，不能只靠 PromptSession + 裸 print。omp 的主界面保留终端 scrollback；当前 Pi 默认 alternate screen，但有 regular 主屏模式；Hermes 经典 CLI 使用同版本 prompt_toolkit 的非全屏 Application。三者的取消、忙时输入和退出并非同一标准，必须区分参考事实与 AAA 适配选择。本文是源码/文档核实，不是运行实测；实施选择唯一维护在 [CLI 设计](../../src/aaa/cli/DESIGN.md)。

核实日期：2026-11-18。未运行参考 CLI、未安装依赖、未访问用户凭据、未测 MCP。

## 来源与可复现边界

| 对象 | 核实基线 | 限制 |
|---|---|---|
| omp | 当前安装提供的 `omp://` 一手技术文档，见下列链接 | 文档没有版本/commit 元数据；文档声明源码路径不等于已读对应源码 |
| Pi | `badlogic/pi-mono` 已重定向至 `earendil-works/pi`；`main` 快照 `ff8721f178fbb5c7ed560ed5c6ba254c5ed425e4` | 不能把旧版 Pi 的屏幕默认值套到本快照 |
| Hermes | `NousResearch/hermes-agent` 的 main 源码与官方文档；取样时 HEAD `05348a3094a2356b9a271a2f22cdd6711c9952b8` | 调研按 main 取样；该 SHA 是同时段 HEAD，不声称每个取样请求均原子绑定此 SHA |
| Python 依赖 | Agno 3.1.12、prompt_toolkit 3.0.53 | API 事实以本机已安装源码为准 |

Pi [commit](https://github.com/earendil-works/pi/commit/ff8721f178fbb5c7ed560ed5c6ba254c5ed425e4)；Hermes [HEAD 记录](https://github.com/NousResearch/hermes-agent/commit/05348a3094a2356b9a271a2f22cdd6711c9952b8)。研究切片：`agent://OmpPiMechanisms`、`agent://HermesMechanisms`；Hermes 切片尾部输出不完整，本文不引用缺失部分。

## 1. 屏幕模型：名称不是机制

- **omp**：帧分为已完成的 `history` 和可替换 `viewport`；普通帧不重写已提交历史。resize 可暂借 alternate buffer，全屏 overlay 可用 alternate screen，不能据此说默认对话就是全屏应用。没有独立 `--tui` 启动参数；交互界面本身称 TUI。[渲染契约](omp://tui-core-renderer.md)、[运行细节](omp://tui-runtime-internals.md)、[CLI 参数](omp://cli-reference.md)。
- **Pi**：`tuiMode` 默认 `fullscreen`，使用 alternate screen；`regular` 使用主屏滚动历史。是同一个前端里的 renderer 选择，参数是 `--tui-mode`，不是独立 `--tui` 产品。[用法](https://github.com/earendil-works/pi/blob/ff8721f178fbb5c7ed560ed5c6ba254c5ed425e4/packages/coding-agent/docs/usage.md)、[renderer 选择](https://github.com/earendil-works/pi/blob/ff8721f178fbb5c7ed560ed5c6ba254c5ed425e4/packages/coding-agent/src/modes/interactive/tui-renderer.ts)。
- **Hermes**：经典 CLI 为 Python `Application(full_screen=False)` + Rich；另有 Node/React Ink 前端，并有 inline/alternate 两种布局。采用经典 CLI 不等于自动获得全部 omp 渲染能力。[经典运行布局](https://github.com/NousResearch/hermes-agent/blob/main/hermes_cli/cli_tui_runtime_mixin.py#L430-L466)、[Ink 布局](https://github.com/NousResearch/hermes-agent/blob/main/ui-tui/src/components/appLayout.tsx#L654-L661)、[官方 CLI 说明](https://hermes-agent.nousresearch.com/docs/user-guide/cli)。

prompt_toolkit 官方明确 `full_screen=True` 才使用 alternate screen；普通 `Application` 可只占主屏需要的高度：[布局与 Application](https://python-prompt-toolkit.readthedocs.io/en/stable/pages/full_screen_apps.html)。

## 2. 键位与队列：共同模式和真实差异

| 行为 | omp | Pi | Hermes 经典 CLI |
|---|---|---|---|
| 换行 | 主 composer 无完整默认表；hook 的 promptStyle 说明 Enter 提交、Shift+Enter 换行 | Shift+Enter / Ctrl-J 换行 | Shift+Enter 通过扩展 ANSI 映射，另有兼容键 |
| Ctrl-C | 清草稿可召回，二次退出；精确时间窗口未由本次 omp 文档证实 | 首次清草稿，500ms 内二次 shutdown，不直接 abort | 优先关覆盖层/清草稿，再中断 agent；有二次强退分支 |
| Esc | 当前交互优先，然后 abort | 恢复排队内容并 abort | 当前模态取消；官方说明称双 Esc 可 backtrack，但取样源码处理器展示的是清草稿/附件，未完整证实回溯链路 |
| Ctrl-D | 空编辑器退出 | 空编辑器 shutdown | 空输入退出；活动 turn 可 detach 留在 gateway |
| 外部编辑器 | Ctrl-G | Ctrl-G | Ctrl-G / Alt-G，保存后可进入提交路径 |
| 队列取回 | Alt-Up / Shift-Up 逐条；abort 可整体恢复 | Alt-Up 整体恢复 | 有 busy mode 与队列管理，不能假设相同键位 |
| 工具详情 | Ctrl-O | Ctrl-O | 本次没有充分核实同一键位 |
| 忙时普通输入 | 支持 steer / follow-up 等交付策略；主 Enter 精确默认未证实 | Enter steer，follow-up 另有键 | interrupt / queue / steer 可配，默认 interrupt |
| 忙时斜杠命令 | 内置注册表先分派；扩展命令可立即执行，文本模板另行展开 | 先分派本地命令，部分状态修改命令自己拒绝 | `/`、`!` 始终本地分派，不作为 steering 文本 |

依据：omp [键位](omp://keybindings.md)、[命令分派](omp://slash-command-internals.md)、[扩展](omp://extensions.md)；Pi [交互源码](https://github.com/earendil-works/pi/blob/ff8721f178fbb5c7ed560ed5c6ba254c5ed425e4/packages/coding-agent/src/modes/interactive/interactive-mode.ts#L4459-L4477)、[编辑器](https://github.com/earendil-works/pi/blob/ff8721f178fbb5c7ed560ed5c6ba254c5ed425e4/packages/tui/src/components/editor.ts)、[键位](https://github.com/earendil-works/pi/blob/ff8721f178fbb5c7ed560ed5c6ba254c5ed425e4/packages/coding-agent/docs/keybindings.md)；Hermes [键位与忙时分派](https://github.com/NousResearch/hermes-agent/blob/main/hermes_cli/cli_tui_mixin.py#L918-L957)、[输入分派区](https://github.com/NousResearch/hermes-agent/blob/main/hermes_cli/cli_tui_mixin.py#L1383-L1517)、[外部编辑器](https://github.com/NousResearch/hermes-agent/blob/main/hermes_cli/cli_modal_mixin.py#L90-L160)。

AAA 的 FIFO 后续消息、不引入 steering、忙时拒绝会话/Agent 变更、Ctrl-D 忙时不退出、编辑器保存只回草稿，是适应进程内 Agno 的选择，不是把 Hermes gateway/detach 或 Pi steer 机制照搬过来。500ms 是 Pi 源码参考值，omp 文档仅证实双击退出。

Hermes Esc 的文档/源码差异保留为未证实项：[官方 CLI 文档](https://hermes-agent.nousresearch.com/docs/user-guide/cli)、[取样 HEAD 的处理器](https://github.com/NousResearch/hermes-agent/blob/05348a3094a2356b9a271a2f22cdd6711c9952b8/hermes_cli/cli_tui_mixin.py#L1037-L1052)。AAA 不据此引入双 Esc 回溯。

## 3. Shift+Enter：同语言栈的实际限制

本机 prompt_toolkit 3.0.53：

- `input/ansi_escape_sequences.py:125-131` 把 `ESC[27;2;13~`（Shift+Enter）降为 `Keys.ControlM`，与 Enter 相同。
- `keys.py` 的 Enter 是 ControlM，没有独立 `s-enter` 枚举。
- `input/vt100_parser.py:173-191` 创建 `KeyPress(key, insert_text)`，保留已识别序列原文，可供局部适配区分；未识别的 CSI-u 不能假定仍是单个 KeyPress。
- [Hermes `pt_input_extras.py`](https://github.com/NousResearch/hermes-agent/blob/main/hermes_cli/pt_input_extras.py#L88-L116) 明确补充 Kitty CSI-u / xterm modifyOtherKeys 序列，并对默认序列覆盖映射；还有修饰锁定变体处理。它使用全局补丁，AAA 不必照搬其作用域。
- 若终端对 Enter 和 Shift+Enter 都发送普通 CR，程序无从分辨；Hermes 源注释也明确指出 macOS Terminal/部分 Windows Terminal 的此限制。终端侧映射是物理限制的兼容方案，不是更换用户主键位。

官方[键位文档](https://python-prompt-toolkit.readthedocs.io/en/stable/pages/advanced_topics/key_bindings.html)说明 Alt/Esc 前缀的歧义、异步 handler 与终端暂停上下文。适配必须验证转义序列分包与 bracketed paste，不能设置 eager Esc 后破坏组合键。

## 4. 渲染、审批与富文本

- omp 的正文不是「只有纯文本」：有 Markdown 组件、`md*` token 与默认开启的 colorful Markdown。工具卡按 viewport 空间降为完整/语义摘要/单行/隐藏，但执行状态不变。[主题](omp://theme.md)、[运行布局](omp://tui-runtime-internals.md)。
- Pi 的 [Markdown 组件](https://github.com/earendil-works/pi/blob/ff8721f178fbb5c7ed560ed5c6ba254c5ed425e4/packages/tui/src/components/markdown.ts) 按块复用、只重排流式尾部；[diff 组件](https://github.com/earendil-works/pi/blob/ff8721f178fbb5c7ed560ed5c6ba254c5ed425e4/packages/coding-agent/src/modes/interactive/components/diff.ts) 有行级颜色，单删单增配对行使用词级 inverse。多行块没有同等词级高亮。omp 有 native word diff 原语，但本次**没有证实默认工具卡已用词级高亮**；用户给出的效果仍是 AAA 必须满足的目标。
- Hermes 的经典 CLI 使用 `_cprint` 把后台线程输出调度到 UI loop，协调 erase/print/reset/redraw，并有 resize 历史重放；不是裸 `patch_stdout()` 就解决全部问题。审批为同一布局中的条件容器，工作线程等待 UI 回覆。[输出实现](https://github.com/NousResearch/hermes-agent/blob/main/hermes_cli/cli_render.py#L680-L840)、[条件布局](https://github.com/NousResearch/hermes-agent/blob/main/hermes_cli/cli_tui_mixin.py#L348-L395)、[扩展接缝说明](https://hermes-agent.nousresearch.com/docs/developer-guide/extending-the-cli)。
- Hermes 使用部分 prompt_toolkit 私有 renderer 方法与全局输入补丁，是参考机制而非 AAA 的复制模板；AAA 优先公共 `Application`、`run_in_terminal` 和单一输入拥有者。本机 `application/run_in_terminal.py` 的公开 API 会暂停输入、清出 UI、输出并恢复重绘。
- Hermes 流式路径与终局 Rich 路径不同，不能推断「用了 Rich 就天然流式渲染完整 Markdown」。AAA 的流式富文本要单独验收。

## 5. 状态条、Git 与启动生命周期

- omp 状态条有 model/path/git/context/spend 等 segment、staged/dirty/untracked 语义颜色；文档**没有证实其 Git 刷新频率与 watcher 实现**。[设置](omp://settings.md)、[主题](omp://theme.md)。
- Pi [footer data provider](https://github.com/earendil-works/pi/blob/ff8721f178fbb5c7ed560ed5c6ba254c5ed425e4/packages/coding-agent/src/core/footer-data-provider.ts) 主要提供 branch，监听 Git 目录并处理 worktree/reftable，500ms debounce；它不能作为「三者都有脏文件计数」的证据。[footer](https://github.com/earendil-works/pi/blob/ff8721f178fbb5c7ed560ed5c6ba254c5ed425e4/packages/coding-agent/src/modes/interactive/components/footer.ts) 按显示宽度对齐。
- omp spinner 动画可 reduced/none，默认间隔80ms，maxFps 默认30；不是每一帧重读 Git/数据库。[设置](omp://settings.md)。
- omp 启动 header 在 live viewport，首次转录溢出前退休一次，不反复提交；这一生命周期可借鉴，AAA 三阶段眼图与品牌是 owner 自有设计，不来自参考项目。[启动生命周期](omp://tui-runtime-internals.md)。

## 6. 本项目采用边界

本文保留外部项目的取样依据，不维护 AAA 的缺陷清单或实施计划。CLI 当前机制以模块设计为准，实际验证与覆盖边界记录在 `HANDOFF.md`；静态参考不能代替交互验收。
