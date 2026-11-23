---
name: release
description: 发版流程：人建 tag、手动审批构建、草稿 Release 与安装校验
---
# 发版

1. 先取得 owner 对 `vX.Y.Z` 的明确确认；确认工作树干净、在 `main`、与 `origin/main` 同步。
2. `python scripts/release.py tag vX.Y.Z`
   脚本先 preflight（版本、工作树、分支、同步、tag 未占用、`uv sync --frozen --extra embedding`、`./tests/run.sh`），再创建 annotated tag；有 `origin` 时推送 `main` 与 tag。CI 不创建 tag。
3. 在 GitHub 手动 dispatch `release.yml`，输入 tag 与 `RELEASE`；preflight 校验 annotated tag 与 `main` 祖先链，`environment: release` 审批后构建、双平台冒烟，生成草稿 Release。
4. `python scripts/release.py verify vX.Y.Z`
   校验本地产物与 `SHA256SUMS`；有 `gh` 时校验 Release 资产齐全。`install.sh` 下载后也会校验 `SHA256SUMS`。
5. owner 手动发布草稿。tag 不可变；失败只修 forward，不重跑 release 脚本。

细节与决策见 `docs/release.md`。
