# Pi 提示词设计：从资源到模型请求

**结论（高置信度，限定版本）**：Pi 的默认系统提示词是代码按请求条件拼装的，不是从一份内置 `SYSTEM.md` 读出来的。用户的 `SYSTEM.md` 替换的是**内置基础提示**：采用文件内容作为开头，同时不生成内置的工具说明、行为规则和 Pi 文档指引。追加指令、项目上下文、技能目录和工作目录在该分支之后独立组装。只有扩展显式强制替换才会绕过整份分段提示词；这也不是关闭技能扫描或移除工具声明。

**基线**：本机 npm 包 `@earendil-works/pi-coding-agent@1.5.2`。以安装包 `dist/` 的可执行 JavaScript 为准，随包 `.js.map` 可定位原 TypeScript（`system-prompt.js.map` 内嵌原文）。下文 `[S1]` 等指向末尾的版本内源码位置。Pi 官网 [配置](https://pi.dev/docs/latest/configuration)、[扩展](https://pi.dev/docs/latest/extensions)可作使用入口，不拿 `latest` 的措辞替代版本内代码判断。

## 1. 两个 Markdown 文件如何选中

`DefaultResourceLoader` 在资源加载时分别解析基础提示与追加提示，两份文件的选址互不影响：[S1]

| 输入 | 优先级 | 解释 |
|---|---|---|
| 基础提示 | `--system-prompt <text\|path>` → 受信任的 `<cwd>/.pi/SYSTEM.md` → `<agent-dir>/SYSTEM.md` → 内置基础提示 | 单一来源，不合并。默认 agent 目录为 `~/.pi/agent`。 |
| 追加提示 | 一个或多个 `--append-system-prompt <text\|path>` → 受信任的 `<cwd>/.pi/APPEND_SYSTEM.md` → `<agent-dir>/APPEND_SYSTEM.md` → 无 | 提供 CLI 参数时取 CLI 列表，不再补自动发现文件；重复参数按传入次序以空行连接。项目和用户同名文件不叠加。 |

项目查找基于当前 `cwd`，不是沿父目录搜索。基础提示可以来自项目，追加提示同时来自用户目录。路径存在则按 UTF-8 读取并去除 BOM；`resolvePromptInput` 对不存在的显式参数按字面文本处理，对已存在但读取失败的路径告警并返回输入字符串。SDK 还可通过 `systemPromptOverride`、`appendSystemPromptOverride` 在解析后变换结果。提示词文件不是模板，不替换变量。[S1]

`AGENTS.md` / `CLAUDE.md` 走另一条路径：每目录选一个文件，优先 `AGENTS.override.md`；汇集用户目录、祖先目录到当前目录的正文，独立生成 `project_context`。这是上下文注入，不是两个提示词文件的覆盖机制；该加载调用由 `noContextFiles` 控制，不走上述项目提示词的 trust 判定。`/reload` 会重读资源并重建会话的运行时装配。[S1][S3]

## 2. 默认系统提示词由什么组成

`AgentSession._rebuildSystemPrompt()` 把资源加载器的基础提示作为 `customPrompt`，追加内容作为 `appendSystemPrompt`；已发现的技能、项目上下文、当前工作目录、选中的工具与工具提示分别传入。`buildSystemPromptSections()` 按下表的顺序生成默认段；除开头 `preamble` 外，段落用对应的 `<段名>…</段名>` 封装。[S2][S3]

| 段 | 默认内容 | 选中非空 `SYSTEM.md` 时 |
|---|---|---|
| `preamble` | 一句话定义身份和职责：在 Pi 中帮助用户检查文件、运行命令、修改代码 | 用用户文件的文本替换 |
| `tools` | 已选、未隐藏、带 `promptSnippet` 的工具简述；不等同于完整工具声明 | 不生成内置段 |
| `rules` | 按工具清单加入适用的 `promptGuidelines`，去空、去重；最后加「回答简洁、清楚展示文件路径」 | 不生成内置段 |
| `docs` | 安装包的 README、`docs/`、`examples/` 的绝对路径与阅读时机；只在询问 Pi 本身时引导阅读 | 不生成内置段 |
| `addendum` | `APPEND_SYSTEM.md` 或显式追加内容，非空才生成 | 条件不变 |
| `project_context` | 已选上下文文件的路径及正文；至少选中一个文件才生成 | 条件不变 |
| `skills` | 符合条件的技能名称、描述、路径，不带正文 | 条件不变 |
| `cwd` | 当前目录；反斜杠统一为 `/` | 条件不变 |

`SYSTEM.md` 有两层效果，不宜说「只换身份」：`if (customPrompt)` 设置 `preamble`，而 `tools`、`rules`、`docs` 仅在 `else` 分支生成。**零字节文件**读成空字符串，该分支不成立，仍生成内置基础提示；不会转去查用户目录同名文件。扩展可通过 `systemPromptOptions.sections` 写命名段，非空内容可以覆盖同名段，不能借此写 `preamble`。[S1][S2][S4]

**工具提示和工具能力分开**。例如 `read` 工具定义同时含 `description`、参数 schema、`promptSnippet`、`promptGuidelines`。前两者属于模型请求的结构化工具声明；后两者供系统提示词生成器用。用 `SYSTEM.md` 后不显示 `tools` 段，不代表工具从请求或执行层消失。模型是否获得声明，由会话应用的工具集和隐藏声明规则决定，不由 Markdown 文本决定。内置 `docs` 也只是路径与阅读指引，不把文档正文灌进提示词。[S2][S3][S5]

## 3. 技能是怎样被发现并按需阅读的

1. **运行时发现**：资源加载器先解析启用的技能路径，再调用 `loadSkills()`。技能加载器读取文件并解析 frontmatter，构造包含名称、描述、文件路径和基目录的记录。这一步不依赖系统提示词文件。[S1][S6]
2. **让模型知道有哪些技能**：提示词生成器有独立的 `skills` 分支。所选工具中有可见的 `read` 时指示用 `read`，否则尝试可见的 `bash`；若没有可见的读取工具，但其中任一个已选且隐藏，则生成不点名工具的阅读提示；两者都未选则不生成技能段。`disable-model-invocation: true` 的条目不进入 `<available_skills>`；其余只列 `name`、`description`、`location`，并转义 XML 特殊字符。没有可列条目时省略整段。[S2][S6]
3. **读取正文与参考资料**：模型判断任务匹配后，按 `location` 读取技能文件。只有技能正文需要某份参考资料时才再读；相对引用以 `SKILL.md` 所在目录为基准，调用工具时使用绝对路径。没有自动递归展开，也不是专用 `load_skill` 工具负责跳转。用户显式输入 `/skill:<name>` 是另一条路径：会话读取该技能正文并展开到本轮输入，不依赖模型先看见目录。[S5][S6][S7]

因此，**技能扫描、模型看到技能目录、实际读取技能文件**是三个不同的行为。`SYSTEM.md` 不关闭其中任何一步；强制替换可移除本次请求中的技能目录，但这不是删除技能注册记录。文档是否随包只是交付问题；绝对路径还需要有能读取该位置的工具，才构成渐进披露。[S2][S3][S4][S6]

## 4. 从分段提示到模型请求

- **常规路径**：一次 Agent run 开始时，扩展可通过 `before_agent_start` 修改结构化选项；同一次 run 内的多次模型请求不会逐次重触发此钩子。`_preparePromptAndToolLoadout()` 先应用实际工具集，再按段名比较当前会话与目标状态；文本变化写新值，删除段写 `null`，无变化不发系统段补丁。`getCurrentSystemMessage()` 按会话消息重放文本、分段和工具声明增量。`pi-ai` 的 `resolveTranscript()` 提供兼容路径：不支持会话中途系统消息的适配器可折叠成头部系统消息。[S2][S3][S4][S8]
- **强制路径**：`before_agent_start` 返回 `systemPrompt` 时写入 `forceSystemPrompt`，包括显式空字符串。`buildSystemPromptState()` 的强制分支只返回文本，没有 `sections`。该 run 的主模型请求在发送前把系统消息折叠为一条头部消息，使用指定文本并保留当前工具声明；run 结束时清除强制选项。**记录层仍保存结构化段落差量**，不会用这段强制文本覆盖会话历史。它与 `SYSTEM.md → customPrompt` 不是同一个入口。[S2][S3][S4]

## 5. 其他内置提示不归主系统提示词管理

| 类别 | 在哪定义 | 怎么进入模型 |
|---|---|---|
| 主对话提示和工具使用指导 | `system-prompt.js` 的静态文案及工具模块的 `*ToolSystemPromptContribution` | 按当前能力生成系统消息；工具描述和参数另进 schema。[S2][S5] |
| `/` 提示词模板 | `prompt-templates.js` 发现资源并展开匹配模板 | 替换位置参数后作为用户输入，不追加到 system role。[S7] |
| 上下文压缩、分支摘要 | `compaction/utils.js` 定义摘要助手的系统提示，任务文案分别在 `compaction.js`、`branch-summarization.js` | 单独请求；把待摘要对话和指令构造成用户消息。已有摘要时走更新提示；也有压缩单轮前缀的专用提示。[S9] |
| Bug 报告摘要 | `bug-report.js` 的 `BUG_SUMMARY_SYSTEM_PROMPT` 与任务指令 | 单独摘要请求，不继承主会话的 `SYSTEM.md`。[S9] |

核心设计是**按用途分开，由宿主装配**，不是把所有内置提示统一存成 Markdown。若借鉴到 aaa（建议，非现行设计）：可以自行选择将静态文案外置为包资源；应独立定义用户覆盖范围、动态工具和技能目录的保留条件，以及技能引用文档的可读通道。不要把 Pi 自己的代码内置方式误当成 Markdown 外置的证据。

## 6. 源码与核验

以下路径相对于上述 npm 包根目录（`$(npm root -g)/@earendil-works/pi-coding-agent`）；`N–M` 为安装包 JS 行号。

- **[S1] 资源与优先级**：`dist/cli/args.js:72–78`、`dist/main.js:696–697`；`dist/config.js:492–516`；`dist/core/resource-loader.js:114–147,179–209,282–290,439–443,480–509,643–663,959–979`。
- **[S2] 组装与变更**：`dist/core/system-prompt.js:25–65,68–149`，`dist/config.js:375–390`（文档路径）。
- **[S3] 会话装配和请求投影**：`dist/core/agent-session.js:1351–1392,1409–1445,1492–1535,1544–1559,1589–1593,2995–3007,3108–3123`。
- **[S4] 扩展钩子**：`dist/core/extensions/runner.js:1129–1171`（结构化选项、返回值、生效点）。
- **[S5] 工具元数据和文件读取**：`dist/core/tools/read.js:16–24,173–213`；`node_modules/@earendil-works/pi-ai/dist/utils/transcript.js:105–112`。
- **[S6] 技能元数据和提示格式**：`dist/core/skills.js:208–265,278–311`。
- **[S7] 显式调用与模板**：`dist/core/agent-session.js:1570–1573,1732–1747,1811–1834`；`dist/core/prompt-templates.js:77–110,193–263,268–281`。
- **[S8] 消息重放和文本渲染**：`node_modules/@earendil-works/pi-ai/dist/utils/transcript.js:40–104`；`node_modules/@earendil-works/pi-ai/dist/utils/text.js:10–34`。
- **[S9] 辅助调用**：`dist/core/compaction/utils.js:146`；`dist/core/compaction/compaction.js:379–469,499–541,666–679,729–747`；`dist/core/compaction/branch-summarization.js:162,207–237`；`dist/core/bug-report.js:217–234,252–278`。

**离线核验**：用 Node 直接导入安装包的系统提示词生成器、消息重放函数和资源加载器，在合成输入与临时目录下验证默认、`customPrompt`、空文件对应输入、追加与项目上下文、强制覆盖、技能可见分支、段删除重放，以及两份文件的项目可信优先级。没有启动用户的 Pi 会话、读取用户提示词文件或调用模型；不把组装结果当作模型实际会选对技能的证据。
