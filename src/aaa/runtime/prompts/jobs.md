长时间运行的命令用 `run_shell_command(args, background=true)` 启动，返回 job id。随后用 `job_output(job_id)` 查看状态和末尾输出，用 `job_kill(job_id)` 终止。

- 没有完成通知，需要时再查。`job_output` 首行是状态：`running`，或 `finished` 加退出码；其后是输出，标准输出与错误输出混在一起。
- 输出很多时只保留最后一段，更早的输出读不到。
- 后台任务只属于当前 aaa 进程。退出 aaa 时未结束的任务会被终止，之后也查不到旧的 job id。
