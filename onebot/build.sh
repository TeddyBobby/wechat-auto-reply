#!/usr/bin/env bash
set -euo pipefail
umask 077
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [ "$(uname -s)" != Darwin ] || [ "$(uname -m)" != arm64 ]; then
  echo "原生微信适配目前只支持 macOS Apple Silicon；面板可独立运行。" >&2
  exit 1
fi
for tool in go clang curl tar shasum; do
  command -v "$tool" >/dev/null 2>&1 || { echo "缺少 $tool：请安装 Go 1.25+ 和 Xcode Command Line Tools。" >&2; exit 1; }
done
VERSION=17.8.0
SHA256=7606cd0eda21829e268ed66e1ac7dcd819570baee7b7049a96e8986969bf32d1
# cgo toolchains do not consistently preserve quoted paths containing spaces.
# A temporary SDK symlink also supports checkouts in Desktop directories with spaces.
BUILD_TMP="$(mktemp -d /tmp/wechat-frida-build.XXXXXX)"
trap 'rm -rf "$BUILD_TMP"' EXIT
DEVKIT="${FRIDA_DEVKIT_DIR:-$ROOT/.deps/frida-$VERSION-macos-arm64}"
if [ ! -f "$DEVKIT/frida-core.h" ] || [ ! -f "$DEVKIT/libfrida-core.a" ]; then
  if [ -n "${FRIDA_DEVKIT_DIR:-}" ]; then
    echo "FRIDA_DEVKIT_DIR 缺少 frida-core.h 或 libfrida-core.a" >&2; exit 1
  fi
  mkdir -p "$DEVKIT"
  ARCHIVE="$BUILD_TMP/devkit.tar.xz"
  echo "下载 Frida $VERSION 开发包并校验 SHA-256…"
  curl --fail --location --retry 3 --connect-timeout 20 \
    "https://github.com/frida/frida/releases/download/$VERSION/frida-core-devkit-$VERSION-macos-arm64.tar.xz" -o "$ARCHIVE"
  ACTUAL="$(shasum -a 256 "$ARCHIVE" | cut -d ' ' -f 1)"
  [ "$ACTUAL" = "$SHA256" ] || { echo "开发包校验失败，未解压。" >&2; exit 1; }
  tar -xJf "$ARCHIVE" -C "$DEVKIT"
fi
DEVKIT="$(cd "$DEVKIT" && pwd)"
ln -s "$DEVKIT" "$BUILD_TMP/sdk"
export CGO_ENABLED=1 GOARCH=arm64
export CGO_CFLAGS="-I$BUILD_TMP/sdk"
export CGO_LDFLAGS="-L$BUILD_TMP/sdk -lfrida-core"
cd "$ROOT/onebot/src"
go build -trimpath -o "$ROOT/onebot/onebot" .
echo "✓ OneBot 编译完成（未启动、未连接微信）。"
