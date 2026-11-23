# config — 配置

## 边界与目标

两个文件、两类事实：

- `~/.aaa/secret.yaml`：渠道（协议、地址、api_key、模型清单）、默认模型、可切换范围。密钥只在这里。
- `~/.aaa/config.toml`：其余所有可调设置（记忆、上下文、模型参数、工具上限、默认权限模式、日志、界面、会话语义）。

`secret.yaml` 只做 IO、归一与 v1→v2 迁移；`config.toml` 由程序写入。一般设置经 `/config`，记忆引擎仅经 `/memory` 菜单选择。

## 拥有者与不变量

- 路径只在这里定义（`paths.py`）：家目录、数据、日志、`sessions.db`、`secret.yaml`、`config.toml`、输入历史。
- 落盘 schema 恒为 v2：`version`、`default: {provider, model}` 两键、`enabled_models`、`providers` 映射。
- `secret.yaml` 读时不改用户文件：v1 只内存归一，下次保存才落 v2；保存原子且 `0600`。
- `secret.yaml` 存在但读不出有效配置时抛 `ConfigError`，绝不进向导（向导会覆盖原文件）；只有文件不存在才视为未配置。
- `config.toml` 的键表是唯一来源（`settings.py` 的 `KEYS`）：路径、默认值、类型、范围、界面说明、生效方式都在一处。新增可调项只改这张表。
- `config.toml` 全量写回（原子替换）；未知键忽略，缺失或非法值使用默认值，非法值记入 `Settings.warnings`。
- 生效方式三分：`runtime`（立即重建执行体）、`immediate`（下次使用生效）、`restart`（下次启动生效）。
- `memory.source` 默认 `off`，另外可选 `builtin` 或 `external:<id>`，重启生效。外置引擎不可用时保留已保存选择，本进程以关闭状态运行。`memory.data_root` 指定外置数据根目录，重启生效。
- 旧键迁移：`memory.enabled` / `memory.engine` 读取时静默换算为 `memory.source`（`enabled=false` → `off`；`agno-native`/`builtin` → `builtin`；其余 → `external:<值>`），`memory.source` 存在时以它为准；迁移不告警，文件在下一次保存时自然归一。

## 对外接口

- `load_config(settings) -> Optional[Dict]`：`None` 表示文件不存在；不可解析或无可用渠道抛 `ConfigError`。
- `save_config(config)`：写 `secret.yaml`（0600 原子）。
- `normalize_config(data, settings)`：v1 渠道 id 取主机名首段（`api.deepseek.com` → `deepseek`），冲突加序号；窗口缺省取 `model.default_context_window`；默认模型失效时回落到第一个渠道的第一个模型。
- `probe_models(base_url, api_key)`：`GET /models`，15 秒超时；读不到窗口记 0 交由用户手填。
- `Settings`：`get(path)`、`set(path, raw)`（校验并抛 `ValueError`）、`groups()`、`keys_of(group)`、`describe(path)`、`to_toml()`。
- `load_settings()` / `save(settings)`。


