---
name: release
description: 发版流程：tag 驱动构建、GitHub Release 资产与安装验证
---
# 发版

1. 确认工作树干净、`./tests/run.sh` 全绿、真实渠道 e2e 已通过。
2. 版本由 git tag 派生：打 annotated tag `vX.Y.Z` 并推送 `main` 与 tag。
3. `uv build`；校验 wheel 含 `aaa/` 与 `miniviking/`、版本等于 tag；生成 `SHA256SUMS`。
4. `gh release create --draft` 上传 wheel/sdist/install.sh/SHA256SUMS，下载校验哈希后发布。
5. 用公开 URL 跑一次安装冒烟（`aaa --version`）。
细节与决策见 `docs/release.md`。
