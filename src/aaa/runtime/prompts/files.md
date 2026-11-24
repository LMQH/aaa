文件工具会拒绝越界的路径和被排除的路径（环境与密钥文件、依赖与构建目录、`.git` 等），返回 `Error: …`。

- `read_file`：读文件，输出带行号。文件过大时用 `start_line` / `end_line` 分段读，或先用 `grep` 定位再读对应的行段。
- `list_files`：找文件；`recursive=true` 按目录树列出，`pattern` 按文件名过滤。
- `grep`：找内容，按正则检索，默认从启动 aaa 的目录搜起；用 `path` 和 `include` 收窄范围。
- `write_file`：新建文件或整体重写，父目录自动创建。
- `edit_file`：修改已有文件。先读原文；`old_str` 取原文的精确片段，不带行号，也不要凭记忆写。它必须只命中一处，命中多处时补上下文，确实要全部替换才用 `replace_all`。
- `move_file`：移动或重命名单个文件，目标已存在时默认拒绝。
- `delete_file`：只删文件，不删目录。
