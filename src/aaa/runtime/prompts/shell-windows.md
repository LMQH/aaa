在 Windows 上用 `run_shell_command` 执行命令时，传 `["cmd", "/c", ...]` 或 `["powershell", "-NoProfile", "-Command", ...]`。参数数组同样直接执行，不经过 shell。

- 命令有超时，超时后被终止并回填 `Error: timeout …`。
- 返回的是输出末尾的若干行，`tail` 可调；成功时只返回标准输出，失败时返回标准错误。
- `background=true` 在 Windows 上不可用。
