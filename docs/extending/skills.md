# 技能

技能是给模型的领域说明：一个含 `SKILL.md` 的目录，发现、索引披露与正文读取由 Agno 原生 `agno.skills` 承担，aaa 只在装配点提供目录与来源标签。技能还可携带 `references/` 参考文档与 `scripts/` 可执行脚本。

## 位置与命名

- 用户目录：`~/.agents/skills/<技能名>/SKILL.md`
- 随包目录：`aaa/builtin_skills/<技能名>/SKILL.md`
- 项目目录：仓库 `.aaa/skills/<技能名>/SKILL.md`
- 技能名取 frontmatter 的 `name`，缺省用目录名；发现只扫一层 `<目录>/<技能名>/SKILL.md`。同名时后装配的覆盖先装配的：装配按 builtin → user → project 传入 loader，因此优先级为 project > user > builtin（用户目录排在随包目录之前，可以覆盖内置技能）。

启动时扫描一次，改完要重启 aaa。

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

`/skills` 列出发现的技能与各自 description 的第一行。

## 创建方式

- 让 agent 调用 `create_skill(name=..., content=...)`，每次写入需要确认；同名已存在时默认拒绝，要覆盖须显式确认。
- 或直接建目录写文件。

## 生效

重启 aaa 后生效。技能不修改配置，不触发 Agent 重建。

## 与命令模板的区别

- 命令模板：用户显式输入 `/name` 才触发，正文直接作为消息发送。
- 技能：模型自己决定何时读取，正文按需进入上下文。
