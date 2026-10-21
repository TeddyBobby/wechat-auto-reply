#!/usr/bin/env bash
# No private configuration is copied or loaded by this installer.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
PY="${WECHAT_PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "需要 Python 3.9+，请先安装 Python：https://www.python.org/downloads/" >&2
  exit 1
fi
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else "需要 Python 3.9+")'
case "${1:-}" in
  "") echo "✓ 面板依赖检查通过（仅使用 Python 标准库）。运行 ./start.sh 启动。" ;;
  --native)
    bash "$ROOT/onebot/build.sh"
    echo "✓ OneBot 已编译。微信/Gadget 和 Hermes 配置见 docs/NATIVE_SETUP.md。"
    ;;
  *) echo "用法：./setup.sh [--native]" >&2; exit 2 ;;
esac
