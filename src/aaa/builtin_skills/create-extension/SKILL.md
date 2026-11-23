---
name: create-extension
description: 为 aaa 创建扩展或插件：启动动画、自定义斜杠命令、MCP 扩展、记忆引擎、技能；也可指导用户配置渠道、模型和运行设置。
---
# 创建 aaa 扩展

选择用户当前要处理的类型，先调用 `load_skill(name="create-extension", reference="对应文件名")` 读取该指南，再按它生成内容或指导操作。一次只读需要的指南。

| 用户要做的事 | 引用文件名 | 写入方式 |
|---|---|---|
| 自定义开场动画 | `animations.md` | 给用户完整文件内容，由用户自己保存到动画目录 |
| 增加斜杠命令 | `commands.md` | `create_plugin(kind="command", ...)` |
| 接入 MCP 服务 | `mcp-extensions.md` | `create_plugin(kind="extension", ...)` |
| 接入记忆引擎 | `memory-engine.md` | 给用户完整文件内容，由用户保存到 `~/.aaa/memory_engines/`；或提供 entry_points 声明 |
| 创建技能 | `skills.md` | `create_skill(...)` |
| 配置渠道或模型 | `models-and-channels.md` | 指导用户使用 `/provide`，凭据由用户自己填写 |
| 调整运行设置或自有提示词 | `settings.md` | 按指南指向 `/config` 或由用户维护提示词文件 |

创建工具写入前需用户确认。动画是启动时执行的 Python，交付文件内容即可，不代用户安装。按指南说明生效时机，并给出对应的验证步骤。
