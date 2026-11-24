# 示例扩展：hello

演示协议的全部挂载点（工具、事件、拦截、提示词块、面板、命令），并由
`tests/test_extensions.py` 实际加载，保证示例随接口演进不腐烂。

复制到 `~/.aaa/extensions/hello/` 后重启 aaa 即可使用；放仓库 `.aaa/extensions/hello/`
需要仓库信任。

试用：`/panel demo` 打开面板、`/hello 你好` 发送命令、让模型调用 `ext_hello_greet`。
