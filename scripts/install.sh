#!/bin/sh
# aaa 安装脚本：macOS / Linux / WSL。从 GitHub Release 安装 wheel，自举 uv。
# 用法：sh install.sh [--version vX.Y.Z] [--wheel 路径] [--mirror cn|tuna|aliyun] [--index-url URL]
set -eu

REPO="LMQH/aaa"
PACKAGE="amaterasu-agno-agent"
VERSION=""
WHEEL=""
MIRROR=""
INDEX_URL=""

usage() {
    cat <<'EOF'
用法: install.sh [选项]

  --version vX.Y.Z   安装指定版本（默认最新稳定版）
  --wheel 路径       直接安装本地 wheel（离线/冒烟用）
  --mirror cn        使用国内镜像下载依赖（阿里，失败回退清华）
  --mirror tuna      只用清华镜像
  --mirror aliyun    只用阿里镜像
  --index-url URL    指定 PyPI 索引地址
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --version) VERSION="${2:-}"; shift 2 ;;
        --wheel) WHEEL="${2:-}"; shift 2 ;;
        --mirror) MIRROR="${2:-}"; shift 2 ;;
        --index-url) INDEX_URL="${2:-}"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "未知参数: $1" >&2; usage >&2; exit 2 ;;
    esac
done

# 依赖源列表：显式 --index-url 优先；--mirror 预设主/备；默认官方源。
INDEXES=""
case "$MIRROR" in
    "") ;;
    cn|aliyun)
        INDEXES="https://mirrors.aliyun.com/pypi/simple https://pypi.tuna.tsinghua.edu.cn/simple" ;;
    tuna)
        INDEXES="https://pypi.tuna.tsinghua.edu.cn/simple https://mirrors.aliyun.com/pypi/simple" ;;
    *) echo "未知镜像: ${MIRROR}（可用 cn / tuna / aliyun）" >&2; exit 2 ;;
esac
if [ -n "$INDEX_URL" ]; then
    INDEXES="$INDEX_URL"
fi

PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
export PATH

if ! command -v uv >/dev/null 2>&1; then
    echo "未找到 uv，开始安装 ..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    export PATH
    if ! command -v uv >/dev/null 2>&1; then
        echo "uv 安装失败；请手动安装 https://docs.astral.sh/uv/" >&2
        exit 1
    fi
fi

TMP=$(mktemp -d "${TMPDIR:-/tmp}/aaa-install.XXXXXX")
trap 'rm -rf "$TMP"' EXIT INT TERM
SUMS=""

verify_checksum() {
    sums="$1"
    wheel="$2"
    name=$(basename "$wheel")
    if [ ! -s "$sums" ]; then
        echo "SHA256SUMS 缺失或为空：$sums" >&2
        exit 1
    fi
    line=$(grep -E "  ${name}$" "$sums" || true)
    if [ -z "$line" ]; then
        echo "SHA256SUMS 不包含 $name" >&2
        exit 1
    fi
    dir=$(dirname "$wheel")
    if command -v sha256sum >/dev/null 2>&1; then
        ( cd "$dir" && printf '%s\n' "$line" | sha256sum -c - ) || {
            echo "校验失败：$name" >&2
            exit 1
        }
    elif command -v shasum >/dev/null 2>&1; then
        ( cd "$dir" && printf '%s\n' "$line" | shasum -a 256 -c - ) || {
            echo "校验失败：$name" >&2
            exit 1
        }
    else
        echo "缺少 sha256sum 或 shasum，无法校验" >&2
        exit 1
    fi
}

if [ -z "$WHEEL" ]; then
    TAG="$VERSION"
    if [ -z "$TAG" ]; then
        # 不用未认证 API（易触发 60 次/时限流）；从 latest 重定向解析 tag。
        TAG=$(curl -fsSLI -o /dev/null -w '%{url_effective}' \
            "https://github.com/$REPO/releases/latest" 2>/dev/null \
            | sed -n 's#.*/releases/tag/\([^/]*\).*#\1#p')
    fi
    if [ -z "$TAG" ]; then
        echo "无法确定最新版本（网络或代理异常）；请用 --version vX.Y.Z" >&2
        exit 1
    fi
    case "$TAG" in v*) ;; *) TAG="v$TAG" ;; esac
    VER=${TAG#v}
    WHEEL="$TMP/amaterasu_agno_agent-$VER-py3-none-any.whl"
    URL="https://github.com/$REPO/releases/download/$TAG/amaterasu_agno_agent-$VER-py3-none-any.whl"
    echo "下载 $URL"
    curl -fsSL --retry 3 -A "aaa-install/0.1" -o "$WHEEL" "$URL"
    SUMS="$TMP/SHA256SUMS"
    SUMS_URL="https://github.com/$REPO/releases/download/$TAG/SHA256SUMS"
    echo "下载 $SUMS_URL"
    curl -fsSL --retry 3 -A "aaa-install/0.1" -o "$SUMS" "$SUMS_URL"
else
    if [ -f "$(dirname "$WHEEL")/SHA256SUMS" ]; then
        SUMS="$(dirname "$WHEEL")/SHA256SUMS"
    fi
fi

if [ ! -f "$WHEEL" ]; then
    echo "wheel 不存在: $WHEEL" >&2
    exit 1
fi

if [ -n "$SUMS" ]; then
    verify_checksum "$SUMS" "$WHEEL"
else
    echo "警告：未找到 SHA256SUMS，跳过 wheel 校验" >&2
fi

UV_HTTP_TIMEOUT="${UV_HTTP_TIMEOUT:-60}"
export UV_HTTP_TIMEOUT

try_install() {
    index="$1"
    if [ -n "$index" ]; then
        echo "安装 ${PACKAGE}（依赖源：${index}）..."
        UV_DEFAULT_INDEX="$index" UV_INDEX_URL="$index" uv tool install --force "$WHEEL" >/dev/null
    else
        echo "安装 ${PACKAGE}（PyPI 官方源）..."
        echo "正在下载依赖；国内网络可用 --mirror cn 换源。"
        uv tool install --force "$WHEEL" >/dev/null
    fi
}

ok=0
if [ -n "$INDEXES" ]; then
    for index in $INDEXES; do
        if try_install "$index"; then
            ok=1
            break
        fi
        echo "该源安装失败：${index}，尝试下一个 ..." >&2
    done
else
    if try_install ""; then
        ok=1
    fi
fi
if [ "$ok" -ne 1 ]; then
    echo "安装失败。可尝试：" >&2
    echo "  sh install.sh --mirror cn          # 国内镜像" >&2
    echo "  UV_HTTP_TIMEOUT=180 sh install.sh   # 代理较慢时延长超时" >&2
    exit 1
fi

BIN=$(uv tool dir --bin 2>/dev/null || echo "$HOME/.local/bin")
PATH="$BIN:$PATH"
export PATH

if command -v aaa >/dev/null 2>&1; then
    echo "完成：$(aaa --version)"
    case ":$PATH:" in
        *":$BIN:"*) ;;
        *) echo "提示：把 $BIN 加入 PATH 后即可直接运行 aaa" ;;
    esac
else
    echo "安装完成，但 aaa 不在 PATH；可直接执行 $BIN/aaa" >&2
    exit 1
fi
