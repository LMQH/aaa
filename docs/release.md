# 构建与发布

状态：**发版门代码侧就绪，尚未解禁**（2026-11-23）。tag 由 `scripts/release.py` 创建，CI 不创建 tag；release workflow 只允许手动 dispatch + `confirm=RELEASE` + `environment: release` 审批；`SHA256SUMS` 已接入安装校验。远程建立后配置 tag protection 与环境 required reviewers，再解禁。

## 一、定位与边界

构建发布是**仓库工具**，不是运行时模块：

- 代码放 `scripts/`，**不进 wheel**，不 import `src/aaa`，不出现在用户机器上。
- 脚本与用户机交互只通过子进程（`uv`、`git`、`gh`）与 HTTP（PyPI API），不加载被测代码。
- 安装脚本不依赖仓库检出：只依赖 GitHub Release 的固定资产名与 PyPI 包。
- CI 的 YAML 只做编排，判断与生成都在脚本里。

版本唯一事实源是 **git tag**（`vX.Y.Z`，从 `v0.1.0` 起）；文件里不手写版本。`pyproject.toml` 用 `dynamic = ["version"]` 由 tag 派生，本地无 tag 时回落 `0.0.0`。

## 二、产物与渠道

| 渠道 | 内容 | 定位 |
|---|---|---|
| GitHub Release | wheel + sdist + `install.sh` + `SHA256SUMS` | **首版主渠道**：不依赖额外账号审批，先形成可安装产物 |
| PyPI | wheel + sdist，包名 `amaterasu-agno-agent`，命令名 `aaa` | 下一步（owner 暂无 PyPI 账号） |
| testPyPI | 同 PyPI 的发布演练 | 下一步，使用 owner 现有账号与凭据 |
| Homebrew tap | 指向 PyPI 的 formula | 后续可选，首版不做 |

明确不做：原生单文件二进制（每平台 ~150MB + 签名公证）、Docker 镜像、zipapp、预览包通道。

理由：依赖里有 9 个原生扩展，单文件跨平台不成立；「用户不用配环境」由安装脚本自举 uv 满足，不需要嵌入解释器。

## 三、目录与职责

```text
scripts/
  release.py         # 唯一入口：preflight / tag / verify
  install.sh         # POSIX 安装：macOS / Ubuntu / Arch / WSL
  install.ps1        # Windows 安装
.aaa/
  skills/release/    # Agent 操作规程（SKILL.md）
.github/workflows/
  ci.yml             # push / PR：离线回归 + 密钥扫描 + lint
  release.yml        # 手动 dispatch：preflight → 审批 → 构建/冒烟 → 草稿 Release
docs/release.md      # 本文
```

技能发现：omp 默认扫描 `.omp/skills/<name>/SKILL.md`，也支持 `skills.customDirectories` 指向自定义目录（非递归，`*/SKILL.md`）。`.aaa/skills/release/SKILL.md` 需要二者之一接线（symlink 或配置），实现时二选一并记录。

## 四、用户侧体验（不变量）

```bash
# macOS / Linux / WSL
curl -fsSL https://github.com/LMQH/aaa/releases/latest/download/install.sh | sh
```

| 不变量 | 拦截的故障 |
|---|---|
| 安装脚本自举 `uv` 与固定 Python 版本，用户不需要预装任何东西 | 「先装 Python/uv」把非技术用户挡在门外 |
| 首版从 Release 附件 wheel 安装（`uv tool install <wheel 地址>`）；PyPI 批次上线后切换为按包名安装 | 无 PyPI 账号也能形成可安装产物 |
| 全程不读 stdin、不要 sudo、不提问；命令装到 `~/.local/bin`，PATH 只提示不擅自改系统配置 | 半交互卡死、权限弹窗、企业机装不上 |
| 指定版本用 `--version vX.Y.Z`，默认最新稳定版 | 复现旧版本、回滚 |
| 数据与代码分离（`~/.aaa/`），安装/升级不动用户数据 | 升级丢配置与记忆 |
| 装完只打印一行 `aaa --version` 验证结果 | 用「打不开」的方式交付 |
| 升级 = 重跑 `install.sh`（`aaa update` 尚未实现；PyPI 批次再补） | 自研下载替换带来的半更新 |
| 回滚 = 重新安装指定版本的 wheel（GitHub Release 附件） | 复杂化回滚机制 |
| `--mirror cn`（阿里主、清华回退）/ `--mirror tuna` / `--index-url URL` 换依赖源；失败时提示换源并可用 `UV_HTTP_TIMEOUT` 延长 | 依赖源访问慢或不可达 |

## 五、发布流程（人建 tag）

1. `python scripts/release.py preflight vX.Y.Z`（可选，只检查）
   校验：工作区干净、在 `main`、与 `origin/main` 同步、tag 未被占用（本地与远端）、版本号合法、`uv sync --frozen --extra embedding` 与 `./tests/run.sh` 通过。
2. `python scripts/release.py tag vX.Y.Z`
   非交互：先跑同一 preflight，再创建 annotated tag；有 `origin` 时推送 `main` 与 tag。**调用前必须取得 owner 对该版本号的明确确认**（见 `AGENTS.md` 授权与决定权）；CI 不创建 tag；推送后不再重跑本步骤，失败只修 forward。
3. 在 GitHub 手动 dispatch `release.yml`，输入 tag 与 `RELEASE`；preflight 校验 annotated tag 与 `main` 祖先链，`environment: release` 审批后构建、双平台冒烟、生成草稿 Release。
4. `python scripts/release.py verify vX.Y.Z`
   校验：本地产物与 `SHA256SUMS`；有 `gh` 时校验 Release 资产齐全。
5. 草稿 Release 由 owner 手动发布；`install.sh` 下载后校验 `SHA256SUMS`，校验失败不安装。

语义化约定（pre-1.0）：`minor` 允许破坏性变更，`patch` 只含修复与新增；`major` 暂不使用。变更描述由 owner 维护的 `CHANGELOG` 承载，不由脚本生成。

## 六、CI 设计（两级）

状态：`ci.yml` / `release.yml` 已落地（2026-11-23）；第三方 action 固定到 commit SHA；发版 tag 由 `scripts/release.py` 创建，CI 不创建 tag；release 只构建并生成草稿，发布由 owner 决定；PyPI / testPyPI job 后续批次启用。

**`ci.yml`（push / PR）**

- `ubuntu-latest`；`uv sync --frozen`；`./tests/run.sh`；构建在 release workflow。
- 不跑真实渠道 e2e：需要密钥、会花钱、依赖外网。
- `concurrency` 按 ref 取消旧跑。

**`release.yml`（仅 `workflow_dispatch(tag, confirm)`）**

| job | 依赖 | 内容 |
|---|---|---|
| `preflight` | — | 校验 annotated tag、`main` 祖先链；`uv sync --frozen --extra embedding` 与 `./tests/run.sh` |
| `build` | preflight | `environment: release` 审批；`uv build`；版本等于 tag；生成 `SHA256SUMS`；ubuntu 安装冒烟 |
| `smoke` | build | matrix：`ubuntu-latest` / `macos-latest`；`install.sh --wheel` 校验并安装，断言 `aaa --version` |
| `draft-release` | smoke | 建草稿 Release 上传资产；不自动发布 |
| `publish-pypi` / `publish-testpypi` | build | 后续批次启用：`uv publish`，OIDC Trusted Publishing（testPyPI 可用 token 凭据） |
| `e2e`（可选） | 手动 | `workflow_dispatch` 且勾选 `run_e2e` 时执行，使用 repository secret |

通用约束：`permissions: {}` 起步按需授予；第三方 action pin 到 commit SHA；并发按 tag 去重且不取消；所有 checkout `persist-credentials: false`。

## 七、凭据

| 用途 | 方案 | 是否入库 |
|---|---|---|
| GitHub Release | 内置 `GITHUB_TOKEN`，仅 `release` job 有 `contents: write` | 否 |
| testPyPI 发布 | owner 现有账号与 token，存 Actions secret（仅发布 job 可读） | 否 |
| PyPI 发布 | Trusted Publishing（OIDC），不存 token；批次启用 | 否 |
| Homebrew tap（后续） | fine-grained PAT，存 Actions secret | 否 |
| 真实渠道 e2e | repository secret，仅手动 job 可读，fork PR 不可见 | 否 |
| 用户 API Key | 用户侧 `~/.aaa/secret.yaml`（0600），安装过程不接触 | 否 |

公开仓库开启 secret scanning 与 push protection；CI 不使用 `pull_request_target` 检出并执行 PR 代码。

## 八、合规

- 许可证：**Apache-2.0**（与 agno 同族，含专利授权）。依赖许可：agno Apache-2.0，tiktoken / questionary / PyYAML MIT，prompt_toolkit BSD-3，兼容。
- 发布包声明 `requires-python = ">=3.11,<3.14"`；安装脚本安装固定 3.13 版本，避免落到未验证解释器。
- 安装脚本审计面：无 `sudo`、不改系统文件、不覆盖已有 `~/.aaa/` 配置、失败不留下半装状态。

## 九、落地步骤

| 步 | 内容 | 验收 |
|---|---|---|
| 1 | `pyproject` 元数据、动态版本（hatch-vcs）、`[project.scripts]`、LICENSE | 干净环境安装后 `aaa --version/--help` 可用 |
| 2 | `uv build` + 隔离 HOME 安装冒烟 | wheel 含 `aaa`/`miniviking` 双包与入口点；`aaa --version` 等于 tag |
| 3 | `scripts/install.sh`（POSIX） | 临时 `HOME` 下从 Release wheel 装出可运行命令；重复执行幂等 |
| 4 | GitHub Release 草稿 + `SHA256SUMS` | 资产齐全且校验一致 |
| 5 | `ci.yml` + `release.yml` | `ci.yml` 已通过 push 验证（全链路 291 项 + 构建）；`release.yml` 待首个 owner 确认的 tag 验证；dependabot 已生效（`agno` 基线已 ignore） |
| 6 | testPyPI / PyPI 批次 | testPyPI 演练发布；PyPI 注册后 `uv tool install` 可用 |

## 十、风险与取舍

- **需要网络自举**：安装依赖 `astral.sh` 与 GitHub/PyPI 可达；离线环境不支持（首版不解决）。
- **Windows 未实跑**：首版只宣称 POSIX（macOS / Linux / WSL）；`install.ps1` 与 Windows 冒烟在后续批次。
- **无版本文件**：非 git 环境（下载 sdist 手工安装）版本由构建时注入，不回落 `0.0.0`。
- **包名注册**：`amaterasu-agno-agent` 在 PyPI 批次开始前注册占用；改名成本高。
- **tag 不可变**：版本一旦发布不复用；错误只能由下一版本修正。

## 十一、发布说明：已知限制（随 Release notes 发布）

- 沙箱不是 OS 级隔离：文件工具受工作区约束，shell 只有工作目录与人工审批。
- Windows 未验证：仅支持 macOS / Linux / WSL。
- 外部记忆引擎（miniviking）的真实召回与 `cached_tokens` 上报未实测。
- 委派为同步执行，不支持后台目标追踪；子任务的确认动作一律拒绝并回传原因。
