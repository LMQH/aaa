"""应用家目录与文件布局。路径与文件位置只在这里定义。"""

from pathlib import Path

HOME_DIR = Path.home() / ".aaa"
DATA_DIR = HOME_DIR / "data"
MEMORY_DATA_ROOT = HOME_DIR / "memory"
LOG_DIR = HOME_DIR / "logs"
DB_FILE = DATA_DIR / "sessions.db"
USAGE_FILE = DATA_DIR / "usage.db"
DELEGATIONS_DIR = DATA_DIR / "delegations"
COMMAND_USAGE_FILE = DATA_DIR / "command_usage.json"
TRUST_FILE = DATA_DIR / "trusted" / "repos.json"
LOG_FILE = LOG_DIR / "aaa.log"
SECRET_FILE = HOME_DIR / "secret.yaml"
CONFIG_FILE = HOME_DIR / "config.toml"
SYSTEM_PROMPT_FILE = HOME_DIR / "SYSTEM.md"
APPEND_SYSTEM_PROMPT_FILE = HOME_DIR / "APPEND_SYSTEM.md"
HISTORY_FILE = HOME_DIR / "history"
# 扩展点：自定义斜杠命令、插件（MCP）、技能、记忆引擎、启动动画的发现目录。
COMMANDS_DIR = HOME_DIR / "commands"
EXTENSIONS_DIR = HOME_DIR / "extensions"
ANIMATIONS_DIR = HOME_DIR / "animations"
SKILLS_DIRS = (Path.home() / ".agents" / "skills",)
MEMORY_ENGINES_DIR = HOME_DIR / "memory_engines"
