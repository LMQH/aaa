# omp CLI 设计蒸馏

> 来源：omp v18.20.0 全量 `--help` 输出、运行时完整配置树（744 个配置键，含类型/默认值/来源注释）、本机二进制字符串实证（223MB Mach-O，Bun 编译 TS）。蒸馏于 2026-11-16。

## 1. 定位与形态

- 单二进制终端编码 agent。四种运行模式：交互 TUI（默认）、`-p/--print` 非交互、`--mode json`、`--mode rpc / rpc-ui`（可 `--no-ui` headless 供程序驱动）；`omp acp` = Agent Client Protocol over stdio，可作外部编辑器的 agent 后端。
- 另有 Tern 原生面板形态（compose 条、浏览器画中画）与 TUI 并存。
- 架构原则：主 Agent 是唯一用户面协调者；专业工作派发子代理（task/workpool），内部冲突与交接不外露。
- 配置三层：`cfg://` 键值树（`~/.omp/agent/config.yml` 等）＋ AGENTS.md 行为规则 ＋ skills/extensions/plugins 生态。

## 2. CLI 启动面（flags 分组）

| 组 | 代表 flag |
|---|---|
| 模型 | `--model`（模糊"opus"或精确 provider/model）、`--smol/--slow/--plan` 角色、`--thinking off…max/auto`、`--service-tier`、`--models`（限定 Ctrl+P 循环集合） |
| 预算守护 | `--max-time`、`--max-cost`（达到即停发请求，print 模式 exit 3）、`--warn-cost` |
| 行为模式 | `--goal`（目标模式直入）、`--plan-yolo`（只读计划→自动批准→转实现）、`--prewalk`（首次写文件切快模型）、`--auto-approve`/`--approval-mode` |
| 会话 | `-c` continue、`-r` resume（ID 前缀/路径/选择器）、`--restore`（连被中断轮与在跑子代理一起恢复）、`--no-session`、`--session-dir` |
| 上下文/工具 | `--system-prompt` / `--append-system-prompt` / `--system-prompt-template`（Handlebars）、`--no-tools`/`--tools`、`--no-lsp`、`--no-context-files`、`--no-skills`/`--skills` glob、`--no-rules`、`-e`/`--hook` 加载扩展 |
| 移植/隔离 | `--from-claude` / `--from-codex` 导入会话；`--profile` 隔离 auth/会话/设置/缓存＋`--alias` 生成 shell 快捷方式；`--config` 可叠加 YAML overlay；`--add-dir` 多根工作区 |
| UI 微调 | `--spinner-interval`、`--max-fps`、`--motion full/reduced/none`、`--hide-thinking`、`--print-thoughts`、`--export` |

初始消息支持 `@file`、`@image` 前缀注入文件与图片。

## 3. 子命令面（归类蒸馏）

- **会话/分享**：`share`（加密链接上传）、`anonymize`（脱敏副本）、`session pack/unpack`（跨机移交）、`play`/`clip`（会话录制回放/上传）、`stats`、`usage`（各已认证账号配额）、`gc`（存储回收）。
- **模型/基准**：`models`、`login`、`token`、`bench`（TTFT/prefill/解码吞吐 p50/p95）、`tiny-models`（本地小模型下载）、`toks`（多内置 tokenizer 计数）。
- **诊断/实验室**：`gallery`（工具/composer/状态栏渲染器可视化画廊）、`predict`（各补全引擎幽灵文本实况对比）、`render`（生产管线重绘计时）、`if-bench`（指令遵循与工作记忆基准）、`find`（按行为语义定位代码行段）、`compress`（文件压入 prompt register 并报告丢弃量）。
- **运行时**：`bash`/`sh`/`zsh`（内建 pi-shell 引擎）、`ps`（守护后台进程管理：日志/停/杀/重启）、`browser-relay`、`git`（全屏 git UI：分屏 diff+暂存侧栏）、`cleanse`（加权并行子代理修诊断）、`commit`、`worktree`、`skill`/`plugin`、`completions`、`update`。
- **凭据设施**：`auth`（JSON 导入导出）、`auth-broker`（凭据保险库）、`auth-gateway`（凭据后端的 HTTP 正向代理）。

## 4. TUI 结构与状态栏

- 三段式：transcript（对话流）＋ composer（`shape: band` 布态输入区）＋ status line。
- 状态栏：preset `default|minimal|compact|full|nerd|ascii|custom`；左段 `[vim,model,mode,path,git,pr]`、右段 `[session_name,token_total,cost,context_pct]`；分隔符 `powerline|powerline-thin|slash|pipe|block|none|ascii`；`contextLine: off|percentage|annotated|embedded`（上下文余量呈现）；`sessionAccent`（会话名着色描边）。
- 后台可视化：每个后台 job 画 live widget（图表/进度条/状态网格）；置顶子代理卡（collapsed 带展开器）；子代理当前工具调用实时预览；bash 命令渲染为交互执行可视化。
- 键盘回显保 30fps；repaint 上限 `maxFps`（默认 30）。

## 5. 样式系统

- **主题**：`theme.dark` / `theme.light` 双键独立指定。深色主题为"金属/宝石"命名族（二进制实证：titanium、graphite、silver、gold、amber、chrome、bronze、jade、pearl、obsidian、violet…），浅色内置 `light`。
- **运动**：`motion full|reduced|none` 全局档；`shimmer classic|kitt|disabled`（加载动画风格）；终端标题 spinner `braille|pulse|dots|line` + 运行状态指示。
- **文本渲染**：colorfulMarkdown（标题/粗斜/行内码按 ANSI 层级着色）；mermaid 转 ASCII 图内联；SVG 块内联渲染成图；终端内联图片（上限 8 张）；Kitty OSC 66 双倍字号 H1；数值表格下自动绘制主题色图表（autoGraph）；colorBlindMode（diff 新增行用蓝替代绿）。
- **符号**：`symbolPreset unicode|nerd|ascii` 三档字形体系；emoji 补全；消息可贴 emoji 徽章回应（reactions）。
- **终端协议集成**（字符串实证）：OSC 7501 程序状态（工作/等待/完成/失败）、OSC 9;4 进度、OSC 99 通知、OSC 8 超链接化路径、OSC 66 文本缩放。
- HUD 挤压时把行送 scrollback（completeScrollback）；resize 以 rebuild 保持 scrollback 连续性。

## 6. 输入与编辑器机制

- 排队策略：`steeringMode` / `followUpMode` 均 one-at-a-time（默认）；插话打断时机 `interruptMode immediate|wait`。
- 双击 Esc：`rewind|tree|none`（回退点或会话树）。
- composer 增强：预测下一条消息的幽灵文本（可四候选）、实时 tok/s 读数、拼写补全（ngram/smollm/apple）与错词标记、大粘贴菜单（代码块/XML/落盘）、空会话输 `exit` 直接退出。
- magicKeywords：ultrathink / chillax / orchestrate 等独立关键词触发隐藏行为（最高思考档、降档、多代理编排通知）。
- `ask` 工具：原生 HTML 表单向用户提问（radio/checkbox/select/图片块），等待期系统通知，超时自动提交预选值。

## 7. 会话与上下文机制

- 存储：`~/.omp/agent/sessions/<cwd 编码名>/session.jsonl`，按工作目录分组；`PI_CODING_AGENT_DIR` 可迁移整个 agent 目录。
- 恢复链：`resume`（接续）＜ `restore`（恢复被中断的最后一轮＋在跑子代理，崩溃续命）；`session lease` 可查会话被哪个运行中实例持有；pack/unpack 跨机移交。
- 标题：默认 fork（派生侧请求生成），或 tiny 本地模型；judge 按轮次复核标题仍否贴切；replan 时刷新，用户改名后不再动。
- 上下文维护栈（多级回退，序可配）：`remote → snapcompact → handoff → shake → soft`；阈值支持百分比/固定 token/按模型；mid-turn 在工具循环安全点检查；后台预压缩（临近阈值先异步摘要再拼接）；缓存感知修剪（同一文件重复 read 结果被取代、无用工具结果消费后剪除）；idle 压缩可选。
- goal 模式：会话级目标 + 隐藏 goal 工具 + 状态栏 token 预算显示；interactive 模式下轮间可自续。
- checkpoint/rewind 工具（默认关）提供 git 基座的检查点回退。

## 8. 模型路由机制

- **角色**：`default` / `smol` / `slow` / `judge` ＋ `team_*` 系列；存储范围 global|project。
- **选择器语法**：`provider/model-id[@upstream][:level]`，如 `zhipu-coding-plan/glm-5.3-flash:high`；支持模糊名。
- **容错链**：`retry.fallbackChains` 按角色/选择器/provider 通配映射有序回退链；触发面 `fallbackOn any|usage-limit|except-usage-limit`；usage-aware 回退可用配额报告优先同源账号、保留余量策略、等额重置；退避 500ms→300s，maxRetries 10。
- **思考**：`defaultThinkingLevel auto`（auto 分类器上限可配）；分档 thinkingBudgets（minimal 1k…max 32k）；流式循环检测 + 连续相同工具调用纠偏（loopGuard）。
- **本地小模型**：ONNX 执行后端可选 gpu/cpu/mlx/metal/webgpu/cuda/dml…，量化档 q4…fp32；用于会话标题、记忆、词补全；`spotlight` 用 whiskers 模型做 find 与输出整形。
- **Provider 面**：数十家凭据环境变量矩阵（Anthropic/OpenAI/Gemini/Copilot/Bedrock/Vertex/OpenRouter/网关类…）；运行时调优含 prompt-cache 保留档、空闲缓存预热、流首事件/静默看门狗、每 provider 并发上限、安全分类器 fallback；`models.yml` 自定义兼容端点（baseUrl/apiKey/api/models）。
- 凭据解析：env → auth.json/oauth → auth-broker 保险库；`--api-key` 运行时最高。

## 9. 工具与审批机制

- 内建工具 17 个默认启用（read/edit/write/bash/grep/glob/find/eval/task/wait/todo/ask/web_search/lsp/ast_edit/debug/ida）＋ 12 个可选（checkpoint/rewind/memory 系/github/security_scan…）。
- 审批：`approvalMode always-ask|always-ask-cached|write|yolo`；按工具覆盖；`restrictedFolders` 强审批区。
- bash 防呆：interceptor 把 cat/grep/find/sed -i/echo 重定向/nohup/watch 等改道专用工具；gitGuard 拦截丢弃共享工作的 git 命令；>60s 自动转后台受管 job。
- **xd:// 设备面**：低频重工具（ast_edit/lsp/debug 等）不占系统提示词 schema，按需经 `read/write` 到 `xd://<tool>` 的 JSON 派发挂载（docs catalog/inline 可选）。
- LSP：写后自动诊断、定义/引用/hover、懒启动、按项目 daemon broker 共享。
- 扩展生态：extensions（ctx API：会话模式/工具调用超时/渲染器权限）、plugins（marketplace 自动更新检查）、MCP（项目 `.mcp.json` 自动发现、资源更新注入会话）、skills（多来源发现＋Skillshare registry＋`/skill:name` 命令化）、rules＋TTSR（流式输出命中规则即时中断）。
- secrets 混淆器在出站前抹除凭据形状 token；security 命名空间提供扫描规划。

## 10. 多代理与运行时

- `task` 工具批量 shape：一次调用携带 `tasks[]` 并发派发；speculativeLaunch（流式完一个起一个）；上限并发 32、递归深度 2；软请求预算 200 次/代理，越线注入收尾提示，1.5x 强制；completionProbe 侧问完成度百分比。
- 角色化子代理：architect/backend/qa/code-review/compliance/…，`omp agents unpack` 可导出到用户/项目目录定制；按代理配模型覆盖与账号池；空闲 420s 落盘停靠（可唤醒）。
- 隔离：worktree clone-first（APFS/btrfs/zfs/reflink 自动探测）；task 级隔离按 patch 或 branch 合并回主检出。
- 协作/广播：collab（wss 中继共享会话，view/control 权限）、stream（公网直播屏幕与对话）、browser-relay（驱动用户自己的 Chrome 标签）。
- eval 设备：IPython/Bun 双内核单元格；browser 与 computer-use 助手挂载于 eval 进程。

## 11. 成本与守护

- `--max-cost / --warn-cost / --max-time` 与会话级设置；`omp usage` 看各家配额余量。
- power.sleepPrevention：跑任务时防系统/显示器休眠（idle 起步）。
- advisor：第二模型被动审阅每轮，按 continueSeverity（nit|concern|blocker）决定是否打断；审阅间隔与积压背压可配。
- autoqa：工具输出与所声明行为不符时自动写 grievance 报告。
- 记忆后端族：`off|local|hindsight|mnemopi|sharpshooter`，召回/沉淀/反思独立开关；autolearn 停机后催促沉淀技能。

## 参考

- 全量 CLI 面：`omp --help`
- 配置树：`cfg://`（每键含类型/默认/来源）
- 行为规则：`~/.omp/agent/AGENTS.md`
- 二进制实证：`/opt/homebrew/Cellar/omp/18.20.0/bin/omp`（主题族、shimmer、分隔符、OSC 协议字符串）
