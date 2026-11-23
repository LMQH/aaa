# aaa

[![ci](https://github.com/LMQH/AAA/actions/workflows/ci.yml/badge.svg)](https://github.com/LMQH/AAA/actions/workflows/ci.yml)

aaa 是一个基于 [Agno](https://github.com/agno-agi/agno) 的中文优先终端 Agent。它的重点是：**把一次对话里跑通的做法，保存成下一次还能执行的流程。**

## 为什么用 aaa

- **把做法留下来，不只留下聊天记录。** 在对话里让主 Agent 创建并同步执行 Agent、Team 或 Workflow。定义默认只在当前会话有效；经你确认保存后，新会话可以直接复用。对于有固定顺序或分支的任务，可以用 Workflow 把它们写进定义，模型只负责其中交给它的步骤。
- **常用操作不用背命令。** 输入 `/` 查看补全，越常用的命令越靠前。输入不带参数的 `/model` 或 `/config` 可以从菜单选，熟悉之后也可以直接带参数。状态栏把目录、Git、模型、用量和上下文放在输入框上方，不接管整个终端。
- **让记忆跨会话，但不必绑定一种实现。** 记忆默认关闭，可选择 Agno 内置记忆或外置引擎。随项目分发的 [MiniViking](src/miniviking/README.md) 把记忆内容存成文件，默认支持关键词检索，可选装本地语义检索增强。

## 从源码开始

需要 Python 3.11–3.13 和 [uv](https://docs.astral.sh/uv/)。在仓库根目录执行：

```bash
uv sync --frozen
PYTHONPATH=src .venv/bin/python -m aaa
```

首次启动按向导填写模型渠道的端点、密钥和模型。之后直接在对话里给任务；用 `/provide` 管理渠道，或看[渠道配置说明](docs/extending/models-and-channels.md)。

### 试一次流程复用

1. 输入：「创建一个名为 `repo-brief` 的只读 Workflow：先读 README 和目录，再总结项目用途、运行入口和待确认的问题。现在运行一次，不改文件。」
2. 结果符合预期后，请主 Agent 保存这个 Workflow，并在终端确认。
3. 用 `/new` 开新会话，再输入：「用已保存的 `repo-brief` 检查当前仓库。」

保存的是**可再次执行的定义**，不是上一次的输出。委派在当前任务里同步执行，不是后台调度。详见[委派机制](docs/delegation-design.md)。

### 常用入口

| 命令 | 用途 |
|---|---|
| `/model` | 在当前会话切换模型 |
| `/session` | 查找和续聊已有会话 |
| `/memory` | 查看或切换记忆引擎，切换后重启生效 |
| `/insight` | 查看上下文各段的 token 估算（只读） |
| `/trust` | 查看或撤销项目代码扩展的仓库信任 |
| `/usage` | 查看按渠道与模型归类的用量和花费 |
| `/help` | 查看命令清单 |

初始权限为只读；需要写入时用 `/mode` 选择权限。权限策略与人工审批不是操作系统沙箱。

脚本可用 `PYTHONPATH=src .venv/bin/python -m aaa -p "任务"` 执行单轮，或用 `--mode json` 消费逐行事件。参数与退出码见[命令手册](docs/commands.md)。

## 当前可用范围

核心 CLI 工作流已可从发布版安装使用。委派执行、确认保存与跨会话复用，内置记忆写入→新会话注入，自动压缩与归档检索，均已通过真实模型渠道验证；离线全量回归 291 项通过。尚未验证：Windows、外部记忆引擎的真实召回、物理终端键位；沙箱不是 OS 级隔离。当前不提供可在后台持续追踪目标的自主运行。

## 文档

- [扩展与自定义](docs/extending/README.md)：技能、自定义命令、MCP、外置记忆引擎、启动动画、项目级扩展及配置方式。
- [项目级扩展](docs/extending/project-extensions.md)：仓库自带命令/技能与 `AGENTS.md` 的规则；本仓库的 `.aaa/` 即示例。
- [架构](docs/architecture.md)：如何复用 Agno 原生执行路径，以及 aaa 各层做什么。
- [开发验证](tests/README.md)：离线回归与真实渠道验收的运行方式。
