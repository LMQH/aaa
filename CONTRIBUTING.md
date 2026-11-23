# 贡献指南

aaa 是 Apache-2.0 开源项目。提交 Issue 或 PR 前，先读本页。

## 开发环境

```bash
uv sync --frozen --extra embedding
./tests/run.sh
```

`./tests/run.sh` 是离线回归，不需要真实 API Key。真实渠道 e2e 需要自己的 `~/.aaa/secret.yaml`，不要提交任何密钥、令牌或个人路径。

## Issue

- 使用 bug / feature 表单；写清实际行为、预期行为、复现步骤、`aaa --version` 与系统。
- 安全漏洞不要开公开 Issue，走[私密安全公告](https://github.com/LMQH/aaa/security/advisories/new)。
- 维护者按 `bug`、`enhancement`、`question`、`security`、`good first issue`、`release-blocker` 人工 triage。

## PR

- 先开 Issue 讨论大改动；小修可以直接 PR。
- PR 标题用 `type(scope): 摘要`，type 取 `feat`/`fix`/`docs`/`test`/`chore`；合并时 squash。
- 必须通过 CI：`test`、`secret-scan`、`lint`。
- 外部贡献者需要维护者 1 次 approval；维护者自己的 PR 在 CI 通过后合并。
- 改动要能解释清楚。AI 可以辅助，但提交者必须理解并能为改动负责；未理解的生成代码可能被关闭。
- 行为变化补测试；使用方式变化更新文档。

## 维护者：仓库设置

- `main` 保护：禁 force push、禁删除、linear history；必需检查 `test` / `secret-scan` / `lint`。
- 外部 PR 需 1 次 approval；维护者自己的 PR 在 CI 通过后合并。
- `v*` tag 保护；tag 只由 `scripts/release.py tag` 创建，CI 不创建 tag；发版走 environment 审批（当前发版门未解禁）。
- 开启 GitHub 私密安全公告，作为漏洞唯一入口。
- 贡献按 Apache-2.0 授权，不要求 CLA。

## 安全

见 [SECURITY.md](.github/SECURITY.md)。
