"""斜杠命令、模型名与配置键的补全。"""

from typing import Any, List, Optional, Protocol

from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import FormattedText

from ..memory import engine_ids
from .. import trust as trust_mod
from . import usage_stats
from .ui import COMMANDS, ICONS

# /model 之后的固定参数。
MODEL_ARGUMENTS = [("next", "切换到下一个"), ("prev", "切换到上一个")]

# /memory 之后的子命令与引擎 id 补全。
MEMORY_ARGUMENTS = [("engine", "切换记忆引擎"), ("config", "引擎配置"), ("embedding", "嵌入增强（安装/状态）")]


def memory_choices(extra_dir=None) -> dict[str, str]:
    return {"off": "关闭", "builtin": "内置"} | {
        f"external:{engine_id}": f"外置：{engine_id}" for engine_id in engine_ids(extra_dir)
    }


class SettingsLike(Protocol):
    def groups(self) -> List[str]: ...

    def keys_of(self, group: str) -> List[Any]: ...


class SlashCompleter(Completer):
    """补全：/ 开头的命令、/model 后的模型引用，以及 /config 的键。"""

    def __init__(self, models: List[str], settings: SettingsLike, extra_commands: Optional[list] = None) -> None:
        self.models = models
        self.settings = settings
        self.extra_commands = extra_commands or []

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor

        if text.startswith("/config "):
            prefix = text[len("/config "):]
            for group in self.settings.groups():
                if f"{group}." in prefix and not prefix.endswith("."):
                    continue
                if group.startswith(prefix) and not prefix.endswith("."):
                    yield Completion(f"{group}.", start_position=-len(prefix), display_meta="配置分组")
                for key in self.settings.keys_of(group):
                    path = key.path
                    if path.startswith(prefix):
                        yield Completion(path, start_position=-len(prefix), display_meta=key.label)
            return

        if text.startswith("/model "):
            prefix = text[len("/model "):]
            for value, description in MODEL_ARGUMENTS:
                if value.startswith(prefix):
                    yield Completion(value, start_position=-len(prefix), display_meta=description)
            for model in self.models:
                if model.startswith(prefix):
                    yield Completion(model, start_position=-len(prefix), display_meta="model")
            return

        if text.startswith("/memory "):
            parts = text[len("/memory "):].split(" ", 1)
            sub = parts[0]
            if len(parts) == 1:
                for value, description in MEMORY_ARGUMENTS:
                    if value.startswith(sub):
                        yield Completion(value, start_position=-len(sub), display_meta=description)
                return
            if sub == "engine":
                prefix = parts[1]
                extra = trust_mod.trusted_project_dir(self.settings, "memory_engines")
                for value, label in memory_choices(extra).items():
                    if value.startswith(prefix):
                        yield Completion(value, start_position=-len(prefix), display_meta=label)
            return

        # @引用：补全当前目录（递归一层）下的文件。
        if "@" in text:
            at_index = text.rfind("@")
            prefix = text[at_index + 1:]
            if " " not in prefix:
                import os
                base = prefix.rsplit("/", 1)
                directory, name = (base[0] or ".", base[1]) if len(base) == 2 else (".", prefix)
                try:
                    entries = sorted(os.scandir(directory), key=lambda e: e.name)
                except OSError:
                    return
                for entry in entries:
                    if entry.name.startswith(name):
                        suffix = "/" if entry.is_dir() else ""
                        yield Completion(
                            entry.name + suffix, start_position=-len(name),
                            display_meta="目录" if entry.is_dir() else "文件",
                        )
                return

        if text.startswith("/"):
            entries = [*COMMANDS, *self.extra_commands]
            rank = {name: i for i, name in enumerate(usage_stats.ordered([c for c, _ in entries]))}
            for command, description in sorted(entries, key=lambda e: rank.get(e[0], len(rank))):
                if command.startswith(text):
                    yield Completion(
                        command,
                        start_position=-len(text),
                        display=FormattedText([
                            *([("class:completion-icon", ICONS[command] + " ")] if command in ICONS else []),
                            ("class:completion-command", command),
                        ]),
                        display_meta=description,
                    )
