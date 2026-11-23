#!/bin/sh
# 高置信密钥扫描。只报文件名/位置，不打印命中内容。
# 用法：
#   check_secrets.sh                 扫描当前工作树
#   check_secrets.sh --staged        扫描暂存区（pre-commit）
#   check_secrets.sh --all-history   扫描全部提交（CI）
#
# .secretsignore 每行一个精确字符串；命中行含该字符串即忽略。
# 以 # 开头的行与空行不参与匹配。

set -eu

cd "$(git rev-parse --show-toplevel)"

PATTERN='(-----BEGIN ([A-Z]+ )?PRIVATE KEY-----|sk-(proj-)?[A-Za-z0-9_-]{20,}|sk_live_[A-Za-z0-9]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{35})'

ignore_tmp=""
cleanup() {
    if [ -n "$ignore_tmp" ] && [ -f "$ignore_tmp" ]; then
        rm -f "$ignore_tmp"
    fi
}
trap cleanup EXIT INT TERM

if [ -f .secretsignore ]; then
    ignore_tmp=$(mktemp)
    grep -v '^[[:space:]]*#' .secretsignore | grep -v '^[[:space:]]*$' > "$ignore_tmp" || true
fi

filter_hits() {
    if [ -s "$ignore_tmp" ]; then
        grep -vF -f "$ignore_tmp"
    else
        cat
    fi
}

mode="${1:-tree}"
hits=""

case "$mode" in
    --staged)
        hits=$(git grep --cached -n -I -E "$PATTERN" | filter_hits | cut -d: -f1 | sort -u || true)
        ;;
    --all-history)
        if git log -p --all --no-color | grep -E "$PATTERN" | filter_hits | grep -q .; then
            hits="<git history>"
        fi
        ;;
    *)
        hits=$(git grep -n -I -E "$PATTERN" | filter_hits | cut -d: -f1 | sort -u || true)
        ;;
esac

if [ -n "$hits" ]; then
    echo "密钥扫描命中高置信模式（${mode}）：" >&2
    printf '%s\n' "$hits" >&2
    echo "不要提交真实密钥；文档示例改用明显占位符。" >&2
    exit 1
fi

exit 0
