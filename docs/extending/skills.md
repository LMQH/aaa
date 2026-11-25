# 技能

技能是给模型的领域说明：一个含 `SKILL.md` 的目录，发现、索引披露与正文读取由 Agno 原生 `agno.skills` 承担，aaa 只在装配点提供目录、来源标签与技能包过滤。技能还可携带 `references/` 参考文档与 `scripts/` 可执行脚本。

## 位置与命名

- 用户目录：`~/.agents/skills/<技能名>/SKILL.md`
- 随包目录：`aaa/builtin_skills/<技能名>/SKILL.md`
- 项目目录：仓库 `.aaa/skills/<技能名>/SKILL.md`
- 技能名取 frontmatter 的 `name`，缺省用目录名；发现任意深度，含 `SKILL.md` 的目录即技能且不再下钻（点目录与 `references/` 等支持目录不进）。同名时后装配的覆盖先装配的：装配时每个技能目录一个 loader，按 builtin → user → project 传入，因此优先级为 project > user > builtin（用户目录排在随包目录之前，可以覆盖内置技能）。

启动时扫描一次，改完要重启 aaa。

## 技能包

技能根下的分组目录就是技能包：`<根>/<包>/<技能名>/SKILL.md` 归该包，平铺的 `<根>/<技能名>/SKILL.md` 无包。包只是目录组织投射出的视图，不改技能名，也不参与仓库信任门控（项目技能仍要先过 `/trust`）。`~/.agents/skills` 被其他工具共用且只认平铺，往里建分类目录会让别的工具看不见这些技能。

- 包描述写在包目录的 `PACK.md` frontmatter `description` 里，说明什么场景该用这个包——它进系统提示词，是模型决定是否深入的唯一依据，写法标准与技能的 `description` 相同。
- 索引是 names-only：每个包渲染成「名字 + 描述 + 成员名单」，包内技能的描述不进索引，名字全部保留，成员照常 `get_skill_instructions(成员名)` 加载。技能多的大包靠这个省索引，小包不值得成包。
- 停用：包目录放 `.disabled` 标记整包停用（`/plugin` 回车或 `/plugin <包名>` 切换），技能目录放 `.disabled` 只停单个技能。停用的技能不装配：不进索引，`/skills` 看不到，模型也读不到。
- 包内可以有更深的嵌套（`<包>/<子目录>/<技能名>/`），子目录不改变包归属。
- 名为 `references`、`scripts`、`templates`、`assets` 的目录不会成为包或技能根；跨根同名时技能归优先级最高的那一份，包标签跟着装配赢家走。
- 治理参考：成包前先写好 `PACK.md` 的触发场景；包内技能少于 5 个时直接平铺更划算。

## 格式

```markdown
---
name: release-check
description: 发布前检查版本、锁文件与变更记录。用户说“发版”“打 tag”时使用。
---

# 发布前检查

正文写方法、约定与示例。模型看到的就是这一段，frontmatter 不会给它。

```

规则：

- frontmatter 由 Agno 按 `yaml.safe_load` 解析，标准 YAML 语法可用；解析失败回落为逐行 `key: value`。
- `description` 必填：它是 `/skills` 展示与模型判断“要不要读”的唯一依据，写清触发场景。
- `name` 与目录名保持一致的目的是避免误解，索引实际用的是发现时的技能名。

## 模型怎么用它

- 系统提示词由 Agno 注入 `<skills_system>` 索引：技能名、描述与 `references/`、`scripts/` 清单；正文不预载。
- 模型按需调用 `get_skill_instructions(skill_name)` 读取正文；调用 `get_skill_reference(skill_name, reference_path)` 读取 `references/` 下的文档；调用 `get_skill_script(skill_name, script_path, execute=...)` 读取或执行 `scripts/` 下的脚本。
- 脚本执行是模型可用的能力（owner 已接受该信任面）：`execute=true` 由 Agno 直接以技能目录为工作目录运行脚本。不要在技能里放不希望被独立执行的代码。
- 引用与脚本各自限定在技能目录的 `references/`、`scripts/` 子目录内；发现期列入的文件才可读取，引用/脚本路径解析到子目录外（含符号链接逃逸）会被 Agno 的路径安全检查拒绝。`SKILL.md` 本身若是指向目录外的符号链接，Agno 会照常读取链接目标内容，不做拦截。
- 三个工具所有权限模式可用。包内 `create-extension` 的引用随 wheel 提供（源码布局是指向 `docs/extending/` 的 `references` 符号链接），使用时不需要检出 aaa 源码。

## 查看

`/skills` 列出发现的技能与各自 description 的第一行，带 `[来源]`，归了包的再带 `[包名]`。

`/plugin` 列出技能包（含已停用的，附 `PACK.md` 描述）与 MCP 插件，回车切换启停；`/plugin <包名>` 直达。

## 创建方式

- 让 agent 调用 `create_skill(name=..., content=...)`，每次写入需要确认；同名已存在时默认拒绝，要覆盖须显式确认。
- 或直接建目录写文件。

## 生效

重启 aaa 后生效。技能不修改配置，不触发 Agent 重建；`/plugin` 切换技能包只增删包目录的 `.disabled` 标记，同样重启生效。

## 与命令模板的区别

- 命令模板：用户显式输入 `/name` 才触发，正文直接作为消息发送。
- 技能：模型自己决定何时读取，正文按需进入上下文。
