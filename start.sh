#!/bin/bash
# 默认仅启动安全只读面板；--native 才启动本机微信连接。
set -euo pipefail
umask 077
SOURCE_DIR="$(cd "$(dirname "$0")" && pwd)"
PY="${WECHAT_PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || { echo "缺少 Python 3，请先运行 ./setup.sh。" >&2; exit 1; }
MODE=preview
case "${1:-}" in
  ''|--preview) ;;
  --native) MODE=native ;;
  *) echo "用法: ./start.sh [--preview|--native]" >&2; exit 1 ;;
esac
export WECHAT_MODE="$MODE"
WECHAT_HOME=$("$PY" -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).expanduser().resolve())' "${WECHAT_HOME:-$SOURCE_DIR}")
export WECHAT_HOME
RUN_DIR="$WECHAT_HOME/.runtime"
BRIDGE="$SOURCE_DIR/wechat_hermes_bridge.py"
HOOK_PORT="${WECHAT_HOOK_PORT:-36060}"
PANEL_PORT="${WECHAT_PANEL_PORT:-8080}"
[ -f "$BRIDGE" ] || { echo "桥接源码不存在。" >&2; exit 1; }

# 使用 Python 标准库检查端口；预览模式不依赖 macOS 或 lsof。
"$PY" - "$MODE" "$PANEL_PORT" "$HOOK_PORT" <<'PY'
import socket, sys
mode, panel, hook = sys.argv[1:]
ports = [int(panel)] + ([int(hook), 58080] if mode == 'native' else [])
if len(set(ports)) != len(ports) or any(not 1024 <= port <= 65535 for port in ports):
    raise SystemExit('服务端口必须互不重复，且处于 1024 到 65535。')
for port in ports:
    with socket.socket() as candidate:
        try:
            candidate.bind(('127.0.0.1', port))
        except OSError:
            raise SystemExit(f'端口 {port} 被占用或不可用；不会终止任何现有进程。可用 WECHAT_PANEL_PORT 指定其他面板端口。')
PY
mkdir -p "$RUN_DIR"
chmod 700 "$RUN_DIR"

if [ "$MODE" = native ]; then
  [ "$(uname -s)" = Darwin ] || { echo "--native 目前仅支持兼容版本的 macOS 微信；其他系统可使用默认只读面板。" >&2; exit 1; }
  for dependency in lsof open hermes; do
    command -v "$dependency" >/dev/null 2>&1 || { echo "本机模式缺少依赖: $dependency；请参考 README 安装，或不带 --native 启动只读面板。" >&2; exit 1; }
  done
  APP="${WECHAT_APP:-/Applications/WeChat.app}"
  ONEBOT_DIR="$WECHAT_HOME/onebot"
  export WECHAT_CONFIG="${WECHAT_CONFIG:-$SOURCE_DIR/onebot/wechat_version/4_1_12_53_mac.json}"
  [ -f "$WECHAT_CONFIG" ] || { echo "WECHAT_CONFIG 指定的微信版本配置不存在。" >&2; exit 1; }
  [ -d "$APP" ] || { echo "找不到微信应用: $APP" >&2; exit 1; }
  [ -x "$ONEBOT_DIR/onebot" ] || { echo "请先按 README 构建 OneBot: $ONEBOT_DIR/onebot" >&2; exit 1; }
  # 首次本机启动生成独立强随机令牌；不使用源码内置凭据。
  WECHAT_ONEBOT_TOKEN=$("$PY" - "$RUN_DIR/onebot-token" <<'PY'
import os, pathlib, secrets, sys
value = os.environ.get('WECHAT_ONEBOT_TOKEN', '')
if not value:
    path = pathlib.Path(sys.argv[1])
    if path.is_symlink():
        raise SystemExit('令牌文件不允许符号链接。')
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        os.chmod(path, 0o600)
        value = path.read_text(encoding='utf-8').strip()
    else:
        value = secrets.token_urlsafe(48)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(value+'\n')
    if not value:
        raise SystemExit('本地令牌文件为空，请检查 .runtime/onebot-token。')
print(value)
PY
)
  export WECHAT_ONEBOT_TOKEN
  listening() { lsof -t -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }
  if ! listening 27042; then
    echo "▶ 启动微信..."
    open "$APP"
    echo -n "  等待 Gadget 端口"
    for ((i=0; i<60; i++)); do
      sleep 1
      echo -n "."
      listening 27042 && break
    done
    echo
  fi
  listening 27042 || { echo "Gadget 未就绪，请检查微信版本及 FridaGadget 安装。" >&2; exit 1; }
  echo "▶ 启动 OneBot..."
  bash "$SOURCE_DIR/onebot-retry.sh" 12 || {
    echo "OneBot 初始化失败，请查看 $ONEBOT_DIR/startup.log；不会自动重启微信。" >&2
    exit 1
  }
fi

echo "▶ 启动面板（$MODE）..."
"$PY" "$BRIDGE" > "$RUN_DIR/bridge-run.log" 2>&1 &
BRIDGE_PID=$!
"$PY" - "$RUN_DIR/bridge.pid.json" "$BRIDGE_PID" "$BRIDGE" <<'PY'
import json, os, pathlib, subprocess, sys
record, pid, script = sys.argv[1:]
started = subprocess.check_output(['ps', '-p', pid, '-o', 'lstart='], text=True).strip()
path = pathlib.Path(record)
path.write_text(json.dumps({'pid': int(pid), 'started': started, 'target': script}), encoding='utf-8')
os.chmod(path, 0o600)
PY
sleep 2
if ! kill -0 "$BRIDGE_PID" 2>/dev/null; then
  echo "面板未启动，请查看 $RUN_DIR/bridge-run.log。" >&2
  exit 1
fi
echo "✓ 面板 http://127.0.0.1:$PANEL_PORT"
if [ "$MODE" = preview ]; then
  echo "只读预览：空白数据，不连接微信、不调用 Hermes、不接收或发送消息。"
  echo "安装本机依赖后，可显式运行 ./start.sh --native。"
else
  echo "首次使用默认关闭自动回复和发送，白名单为空；请检查配置后按需开启。"
fi
