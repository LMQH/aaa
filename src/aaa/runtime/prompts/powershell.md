执行 PowerShell 时用 `run_powershell` 传单条命令字符串，不通过 `run_shell_command` 转发。命令以 `-NoProfile -NonInteractive` 执行：不加载用户配置，也不能交互。

- 超时与输出规则同 `run_shell_command`；不支持后台运行。
