# Pi CLI 设计蒸馏

> 来源：pi.dev 官方文档（usage / tui / configuration / sessions / themes / models / custom-provider）+ 本地安装包 `@earendil-works/pi-coding-agent@1.5.2` 源码实证。蒸馏于 2026-11-15。

## 1. 定位与设计理念

- **极简 agent harness**："There are many agent harnesses but this one is yours" —— 核心理念是让用户改 Pi 适配自己的工作流，而非相反。
- **不逐步审批**：工具调用默认直接执行，不每次询问；安全靠"审查产出 + 沙箱跑不可信任务"，进入新项目目录时有 **project trust** 机制守门（项目级配置需信任后才加载）。
- 一切皆可覆盖：系统提示词、主题、快捷键、编辑器、footer 都允许替换，扩展是第一公民。

## 2. 界面三层结构

```
┌──────────────────────────────┐
│ transcript（对话流，滚动区）    │  ← 用户消息 / 回复 / 工具调用 / 思考块
│ …                            │
├──────────────────────────────┤
│ editor（输入编辑器）           │  ← 边框颜色 = 当前思考档位
├──────────────────────────────┤
│ footer（状态栏）              │  ← 目录 / 会话 / 模型 / 上下文占用 / 费用
└──────────────────────────────┘
```

- **fullscreen 模式**（默认）：editor 与状态区固定，transcript 在窗口内滚动，鼠标事件归组件管。
- **regular 模式**：用终端原生 scrollback，鼠标交还终端，所有交互必须保留键盘路径。
- 切换：`/settings` 或 `--tui-mode`。
- 启动头列出本次加载的指令与资源清单（context files / skills / extensions）。

## 3. 交互机制

### 输入
| 操作 | 效果 |
|---|---|
| `Enter` | 发送 |
| `Shift+Enter` | 换行 |
| `Ctrl+G` | 跳外部编辑器写长 prompt |
| `@` | 搜索并引用文件（支持图片粘贴/拖入） |
| `Tab` | 路径补全 |
| `!cmd` | 跑 shell 命令，输出进对话上下文 |
| `!!cmd` | 跑命令但**不**把输出给模型 |
| `/` | 命令菜单（skills/prompts/extensions 可注册新命令） |

### steering（边跑边改向）
| 操作 | 时机 |
|---|---|
| `Enter` 发消息 | 等当前响应+工具跑完，引导**下一轮** |
| `Alt+Enter` | 排队，等整个任务结束再执行 |
| `Alt+Up` | 把排队消息退回编辑器 |
| `Escape` | 中止当前任务（排队消息一并退回） |

### 查看与切换
| 键 | 作用 |
|---|---|
| `Ctrl+O` | 展开/折叠工具输出 |
| `Ctrl+T` | 显示/隐藏思考块 |
| `Ctrl+L` | 打开模型选择器 |
| `Shift+Tab` | 循环思考档位 |
| `Ctrl+P` | 循环可用模型 |
| `Ctrl+X` | 复制最后一条回复 |
| `/hotkeys` | 查看当前会话全部生效快捷键 |

## 4. 样式系统（themes + pi-tui）

### 主题机制
- 内置 `system` / `dark` / `light`。默认 **system**：启动时向终端查询前景/背景 + 16 ANSI 色（最多等 100ms），按色相映射 Pi 语义色，并强制 **WCAG 4.5:1 对比度**；终端切深浅色时实时重建。
- 自动模式写法 `"theme": "light/dark"`；判定顺序：终端上报背景 → appearance 通知 → `COLORFGBG` → dark。
- 自定义主题：JSON 放 `~/.pi/agent/themes/<name>.json`（name 必须与文件名一致，`system` 保留字），**只热重载 agent 目录下的活动主题**，其余用 `/reload`。项目主题放 `.pi/themes/`（需 trust）。
- 颜色六种写法：`#hex` / `oklch()` / `okhsl()`（内置主题用 OKHSL，保证等饱和度观感一致、全在 gamut 内）/ 256 色索引 / `vars` 变量引用（可链式）/ `""` 终端默认色。
- 主题按**接口角色**而非组件命名，分组：通用（`accent` `border*` `text` `muted` `dim` `success` `error` `warning`）、选区（`selectedBg` `searchMatch*` `scrollbar*`）、消息（`userMessage*` `thinkingText`）、工具（`toolPendingBg` `toolSuccessBg` `toolErrorBg` `toolTitle` `toolOutput`）、Markdown（`md*`）、diff（`toolDiff*`）、语法高亮（`syntax*`）、HTML 导出（`export.*`）。5 个可选色有继承回退（如 `scrollbarTrack`→`muted`）。

### 渲染引擎要点（`@earendil-works/pi-tui`）
- 组件模型：`render(width) → string[]`，可选键盘/鼠标处理；**每行必须在宽度内**，用 `visibleWidth() / truncateToWidth() / sliceByColumn() / wrapTextWithAnsi()` 处理 ANSI/宽字符/emoji 列宽。
- **Pi 每行渲染后重置样式与超链接**，组件每行都要重新套样式；状态变化必须 `invalidate()` 缓存并调 `tui.requestRender()`（TUI 合帧批量重绘）。
- 内置组件族：内容（`Text` `Markdown` `Image` `TruncatedText`）、布局（`Container` `VStack` `HStack` `Box` `Spacer`）、输入（`Input` `Editor`）、选择（`SelectList` `SettingsList`）、滚动（`ScrollView`）、加载（`Loader`）、鼠标区域（`MouseRegion`）。
- 光标：`Focusable` + `CURSOR_MARKER` + `renderFakeCursor()` 反色绘制；**包裹 Input/Editor 的容器必须向下传 focused**，否则中日韩 IME 候选窗错位。
- 语义色 API：`theme.fg(token, s)` / `theme.bg()` / `theme.style(s, {fg, bg, bold})`；token 可换具体色，`theme.colors` 取实色，`mixColors()` 做色彩数学；truecolor 优先，退 256 色。
- 自定义交互：`ctx.ui.custom()` 临时接管交互区（overlay 模式可定位/层叠/显隐）；阻塞类交互须传 `blocked: {kind: "question"|"permission"|"auth"}` 供终端程序状态（OSC 7501 类）显示。
- 诊断：`/debug` 写 `pi-debug.log`；`PI_TUI_WRITE_LOG` 抓原始 ANSI 流。

## 5. 会话与上下文机制

- 会话自动保存（`--no-session` 除外），按**工作目录**分组存于 `~/.pi/agent/sessions/`；`pi --continue` 接最近，`--resume` 开选择器。
- **会话是一棵树**，不是线性历史：`/tree` 在会话内换分支（选用户消息→改写→提交即生成新分支，旧分支保留可回）；`/fork` 从某条用户消息分叉成新会话；`/clone` 复制当前分支。弃用分支可自动摘要并挂到新分支。
- 模型请求以活动分支为基础，结合当前系统提示词与工具声明；符合可见条件的技能只注入名称、描述和路径，正文按需读取。上下文接近上限时自动 compact（摘要 + 保留近期消息，原始条目不删），`/compact` 可手动并附摘要指令。
- 会话绑定创建时目录且不变；目录被删/改名时交互式问：Continue（用当前目录）/ Fork（复制成新会话）/ Cancel。
- `/export` 导 HTML/JSONL；`/share` 传 Radius 或私有 GitHub gist 出查看链接；`/session` 查看文件/ID/消息数/token/费用。

## 6. 配置与扩展机制

### 目录布局
| 路径 | 职责 |
|---|---|
| `~/.pi/agent/settings.json` | 用户设置（含资源路径、Pi 包声明） |
| `~/.pi/agent/keybindings.json` | 快捷键定制 |
| `~/.pi/agent/models.json` | 兼容端点/模型/模型覆写 |
| `~/.pi/agent/auth.json` | API key 与 OAuth 凭据 |
| `~/.pi/agent/mcp.json` | 全局 MCP servers |
| `~/.pi/agent/AGENTS.md` / `CLAUDE.md` | 跨目录用户指令（`AGENTS.override.md` 仅同目录覆盖） |
| `~/.pi/agent/SYSTEM.md` / `APPEND_SYSTEM.md` | 分别替换内置基础提示（身份、工具说明、规则、Pi 文档指引）和追加指令；技能清单按自身条件生成 |
| `~/.pi/agent/extensions|skills|prompts|themes/` | 用户扩展/技能/prompt 模板/主题 |
| `<cwd>/.pi/` | 当前项目的资源目录；两个提示词文件分别查找，受信任的项目文件优先于各自的用户文件，同名不合并 |

- `SYSTEM.md` 不替换整条系统消息；`APPEND_SYSTEM.md`、项目上下文、技能清单和工作目录仍按各自条件拼装。系统提示词中的工具说明与实际工具声明是两条路径。完整调用链见 [Pi 提示词设计](pi-prompt-design.md)。
- Context files（AGENTS.md 系）从 agent 目录、工作目录及**父目录链**发现，不需要 trust；`sessionDir` 也是唯一 trust 前读取的设置。
- 手改文件后 `/reload` 生效。
- 扩展层级递增：AGENTS.md（常驻指令）→ prompt template（`/`菜单复用）→ skill（任务级指令+附件）→ extension（可执行工具/命令/事件/自定义 UI）→ provider extension（自定义协议）→ Pi package（打包分发以上全部）。
- 凭据解析优先级：运行时 `--api-key` > `auth.json` > `models.json` 的 `apiKey` > 环境变量/云凭据。`models.json` 的 key 支持 `$VAR`、`${VAR}`、`!command`（请求时执行不缓存）、`$$`/`$!` 转义。

### models.json 接入兼容端点
```json
{
  "$schema": "https://pi.dev/schemas/models.schema.json",
  "providers": {
    "<name>": {
      "baseUrl": "http://…/v1",
      "api": "openai-completions",
      "apiKey": "…",
      "models": [{ "id": "…", "contextWindow": 49152, "maxTokens": 8192 }]
    }
  }
}
```
- 支持覆写内置 provider：只写 `baseUrl` 即代理转发（保留其模型与认证）；`modelOverrides` 改元数据不换列表；`samplingParamsByThinkingLevel` 按 Pi 思考档位映射采样参数（仅 openai 系 API）。
- 模型没出现在 `/model`：先查凭据是否可解析（自定义模型加载了但无凭据不显示）。

## 7. 模型与思考机制

- `/login` 存凭据到 `auth.json`；`/model` 选择（`Ctrl+S` 存默认）；`/thinking` 选档位（off/minimal/low/medium/high/xhigh/max），`Shift+Tab` 循环。
- 本地 GGUF 两条路：llama.cpp router（`/llama` 管理，按需加载，加载中的才出现在列表）或任意 OpenAI/Anthropic/Google 兼容端点走 `models.json`。
- 会话内切换模型/档位被记录，恢复会话时还原，不影响新会话默认值。

## 参考

- 快速上手：https://pi.dev/docs/latest/quickstart
- 交互：https://pi.dev/docs/latest/usage
- TUI：https://pi.dev/docs/latest/tui
- 配置：https://pi.dev/docs/latest/configuration
- 会话：https://pi.dev/docs/latest/sessions
- 主题：https://pi.dev/docs/latest/themes
- 模型：https://pi.dev/docs/latest/models
