# Python 发布模式调研：已否决

结论：通用「zipapp 单文件 + `git push --tags` 自动发版 + Windows 安装脚本 + 自更新」模式不适用于 aaa。

- aaa 依赖原生扩展，zipapp 单文件不成立；现行是 wheel + `uv tool install`。
- tag 由人经 `scripts/release.py` 创建，CI 不创建 tag；发布走手动 dispatch + `environment: release` 审批，不在 tag push 时自动发布。
- 首版只支持 POSIX；Windows 安装脚本与冒烟尚未实现。
- `aaa update` 尚未实现；升级 = 重跑 `install.sh`。
- 现行设计见 `docs/release.md`。
