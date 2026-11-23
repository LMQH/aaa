#!/usr/bin/env bash
# 统一测试入口：离线单测默认执行；带 e2e 参数时追加真实渠道闭环。
#
# 照 Pi 的做法：固定环境（时区/语言/代理），让结果只受代码影响。
set -euo pipefail

cd "$(dirname "$0")/.."

PY="${PYTHON:-./.venv/bin/python}"
if [[ ! -x "$PY" ]]; then
    echo "找不到解释器：$PY（先 uv sync，或用 PYTHON=... 指定）" >&2
    exit 2
fi

export PYTHONPATH="src:tests"
export TZ=UTC LC_ALL=C LANG=C
unset ALL_PROXY all_proxy HTTP_PROXY http_proxy HTTPS_PROXY https_proxy 2>/dev/null || true

"$PY" -W error::ResourceWarning -m unittest discover -s tests -p 'test_*.py'

if [[ "${1:-}" == "e2e" ]]; then
    shift
    "$PY" tests/e2e.py "$@"
fi
