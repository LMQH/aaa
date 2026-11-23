#!/bin/sh
# 开发入口：直接跑仓库源码，不装 wheel。
# 用法：~/.local/bin/aaa-dev 是调用本脚本的 wrapper；工作区是当前目录。
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PY="${AAA_PYTHON:-$ROOT/.venv/bin/python}"

if [ ! -x "$PY" ]; then
    echo "找不到 $PY；先在 $ROOT 执行 uv sync --frozen --extra embedding" >&2
    exit 2
fi

exec env PYTHONPATH="$ROOT/src" "$PY" -m aaa "$@"
