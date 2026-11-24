"""hello 示例扩展：演示协议的全部挂载点。

复制到 ~/.aaa/extensions/hello/ 后重启 aaa 生效。
"""

import json

from aaa.extensions.protocol import ToolDecision

PROTOCOL_VERSION = 1


def activate(ctx):
    ctx.log.info("hello 扩展激活，目录 %s", ctx.home)
    ctx.register_tool(ctx_greet, name="greet", capability="ext/hello/greet", read_safe=True)
    ctx.subscribe("run.completed", _on_run_completed)
    ctx.intercept_tool(_guard)
    ctx.register_prompt_block(name="mode", render=_prompt_block)
    ctx.register_panel(name="demo", title="hello", render=_panel_lines)
    ctx.register_command("hello", "向用户打招呼，语气友好。附加要求：$ARGUMENTS")


def ctx_greet(name: str) -> str:
    """向指定名字打招呼；演示一个只读的安全工具。"""
    return json.dumps({"ok": True, "greeting": f"你好，{name or '世界'}！"}, ensure_ascii=False)


def _on_run_completed(event):
    # 事件是旁路观察：这里只演示用法，不参与主流程。
    pass


def _guard(call):
    """拦下显然危险的递归删除；其余一律放行。"""
    if call.name == "run_shell_command" and "rm -rf" in str(call.args.get("cmd", "")):
        return ToolDecision.deny("示例拦截：禁止递归删除")
    return None


def _prompt_block(info):
    return f"hello 示例扩展已启用（当前模式 {info.mode}）。"


def _panel_lines(view):
    return [f"mode: {view.get('mode', '?')}", f"model: {view.get('model', '?')}"]
