#!/bin/bash
# 只重试本脚本创建的 OneBot 子进程；不会根据端口停止未知进程。
set -euo pipefail
umask 077
SOURCE_DIR="$(cd "$(dirname "$0")" && pwd)"
export WECHAT_HOME="${WECHAT_HOME:-$SOURCE_DIR}"
WECHAT_HOME="$(cd "$WECHAT_HOME" && pwd)"
export WECHAT_HOME
ONEBOT_DIR="$WECHAT_HOME/onebot"
RUN_DIR="$WECHAT_HOME/.runtime"
PY="${WECHAT_PYTHON:-python3}"
HOOK_PORT="${WECHAT_HOOK_PORT:-36060}"
MAX_TRIES=${1:-15}
case "$MAX_TRIES" in ''|*[!0-9]*) echo "重试次数必须为正整数" >&2; exit 1 ;; esac
[ "$MAX_TRIES" -ge 1 ] && [ "$MAX_TRIES" -le 100 ] || { echo "重试次数范围为 1 到 100" >&2; exit 1; }
if [ -z "${WECHAT_ONEBOT_TOKEN:-}" ]; then
  [ -f "$RUN_DIR/onebot-token" ] && [ ! -L "$RUN_DIR/onebot-token" ] || { echo "请先通过 ./start.sh --native 生成本地令牌，或设置 WECHAT_ONEBOT_TOKEN。" >&2; exit 1; }
  chmod 600 "$RUN_DIR/onebot-token"
  WECHAT_ONEBOT_TOKEN=$(cat "$RUN_DIR/onebot-token")
fi
: "${WECHAT_ONEBOT_TOKEN:?本地令牌文件为空}"
WECHAT_CONFIG="${WECHAT_CONFIG:-$SOURCE_DIR/onebot/wechat_version/4_1_12_53_mac.json}"
[ -f "$WECHAT_CONFIG" ] || { echo "WECHAT_CONFIG 文件不存在" >&2; exit 1; }
[ -x "$ONEBOT_DIR/onebot" ] || { echo "OneBot 尚未安装" >&2; exit 1; }
WECHAT_CONFIG=$("$PY" -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).expanduser().resolve())' "$WECHAT_CONFIG")
if lsof -t -nP -iTCP:58080 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "端口 58080 已被占用，拒绝停止未知进程或重复启动。" >&2
  exit 1
fi
IMGPATH=$("$PY" "$SOURCE_DIR/reconnect_onebot.py" --image-path) || exit 1
mkdir -p "$IMGPATH"
chmod 700 "${IMGPATH%/}" "$(dirname "${IMGPATH%/}")"
mkdir -p "$RUN_DIR"
chmod 700 "$RUN_DIR"
cd "$ONEBOT_DIR"

CHILD_PID=""
CHILD_STARTED=""
cleanup_child() {
  if [ -n "$CHILD_PID" ]; then
    # 除直接子进程 PID 外，还验证启动时间，避免误伤复用的 PID。
    local current
    current=$(ps -p "$CHILD_PID" -o lstart= 2>/dev/null | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' || true)
    if [ -n "$current" ]; then
      if [ "$current" != "$CHILD_STARTED" ]; then
        echo "子进程身份已变化，不终止、不继续重试。" >&2
        CHILD_PID=""
        return 1
      fi
      kill -TERM "$CHILD_PID" 2>/dev/null || true
      for ((stopping=0; stopping<50; stopping++)); do
        current=$(ps -p "$CHILD_PID" -o lstart= 2>/dev/null | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' || true)
        [ "$current" != "$CHILD_STARTED" ] && break
        sleep .1
      done
      if [ "$current" = "$CHILD_STARTED" ]; then
        echo "OneBot 尚未退出，保留进程记录并停止重试。" >&2
        CHILD_PID=""
        return 1
      fi
    fi
    wait "$CHILD_PID" 2>/dev/null || true
    CHILD_PID=""
    rm -f "$RUN_DIR/onebot.pid.json"
  fi
}
trap cleanup_child EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

for ((i=1; i<=MAX_TRIES; i++)); do
  if lsof -t -nP -iTCP:58080 -sTCP:LISTEN >/dev/null 2>&1; then
    echo "端口 58080 已被其他进程占用，停止重试。" >&2
    exit 1
  fi
  LOG="$ONEBOT_DIR/startup.log"
  "$ONEBOT_DIR/onebot" -type=gadget -image_path="$IMGPATH" -token="$WECHAT_ONEBOT_TOKEN" \
    -receive_host="127.0.0.1:58080" -send_url="http://127.0.0.1:$HOOK_PORT/onebot" \
    -wechat_conf="$WECHAT_CONFIG" > "$LOG" 2>&1 &
  CHILD_PID=$!
  CHILD_STARTED=$(ps -p "$CHILD_PID" -o lstart= 2>/dev/null | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' || true)
  "$PY" - "$RUN_DIR/onebot.pid.json" "$CHILD_PID" "$ONEBOT_DIR/onebot" <<'PY'
import json, os, pathlib, subprocess, sys
record, pid, executable = sys.argv[1:]
started = subprocess.check_output(['ps', '-p', pid, '-o', 'lstart='], text=True).strip()
path = pathlib.Path(record)
path.write_text(json.dumps({'pid': int(pid), 'started': started, 'target': executable}), encoding='utf-8')
os.chmod(path, 0o600)
PY
  sleep 14
  if kill -0 "$CHILD_PID" 2>/dev/null &&
     ! grep -q "Cannot find 'req2buf'\|unable to intercept" "$LOG" &&
     grep -q "基址解析(模块表)" "$LOG" &&
     grep -q "Dynamic Text Message Setup Complete" "$LOG" &&
     grep -q "Reply message setup complete" "$LOG"; then
    echo "✓ 第 $i 次 hook 安装成功（$LOG）"
    echo "直发仍需等待 StartTask manager 捕获；无需手工发送消息预热。"
    CHILD_PID=""
    exit 0
  fi
  echo "第 $i 次尚未就绪；只回收本次创建的 OneBot。"
  cleanup_child
  sleep 2
done
echo "初始化未成功。请检查微信版本、登录状态及 FridaGadget 安装。" >&2
exit 1
