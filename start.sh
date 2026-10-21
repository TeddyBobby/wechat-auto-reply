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
# 脱离启动终端的输入与挂断信号；日志始终留在此副本的运行目录。
nohup "$PY" "$BRIDGE" </dev/null > "$RUN_DIR/bridge-run.log" 2>&1 &
BRIDGE_PID=$!
if ! "$PY" - "$RUN_DIR/bridge.pid.json" "$BRIDGE_PID" "$BRIDGE" "$PANEL_PORT" "$MODE" <<'PY'
import json, os, pathlib, signal, subprocess, sys, time, urllib.error, urllib.request

record_file, raw_pid, script, port, mode = sys.argv[1:]
pid = int(raw_pid)
target = str(pathlib.Path(script).resolve())
path = pathlib.Path(record_file)
record = None
started = ''

def process_identity():
    result = subprocess.run(['ps', '-ww', '-p', str(pid), '-o', 'lstart=', '-o', 'command='],
                            capture_output=True, text=True, timeout=2)
    value = result.stdout.strip()
    if result.returncode or not value:
        return None
    # lstart 有固定五段，后面保留命令中的空格（兼容 checkout 路径含空格）。
    pieces = value.split(None, 5)
    if len(pieces) != 6:
        return None
    return (' '.join(pieces[:5]), pieces[5])

def matches_owned(identity):
    return identity is not None and identity[0] == started and identity[1].endswith(' ' + target)

def remove_own_record():
    try:
        if record is not None and json.loads(path.read_text(encoding='utf-8')) == record:
            path.unlink()
    except (OSError, ValueError):
        pass

def cleanup_new_child():
    """只处理本次启动且启动时间/命令身份仍吻合的子进程。"""
    try:
        identity = process_identity()
        if identity is None:
            remove_own_record()
            return
        if not matches_owned(identity):
            print('进程身份已变化，未终止未知进程。', file=sys.stderr)
            return
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            identity = process_identity()
            if identity is None:
                remove_own_record()
                return
            if not matches_owned(identity):
                print('进程身份已变化，未继续清理。', file=sys.stderr)
                return
            time.sleep(.1)
        # 只强制回收仍属于本次失败启动的同一进程，不按端口或进程名称清理。
        if matches_owned(process_identity()):
            os.kill(pid, signal.SIGKILL)
            for _ in range(20):
                if process_identity() is None:
                    remove_own_record()
                    return
                time.sleep(.1)
    except (OSError, subprocess.SubprocessError) as error:
        print('本次子进程清理未完成: ' + type(error).__name__, file=sys.stderr)

try:
    identity = process_identity()
    if identity is None or not identity[1].endswith(' ' + target):
        raise RuntimeError('本次面板子进程已退出或命令身份不符')
    started = identity[0]
    # stop.sh 用独立 lstart 查询；保存原始空白格式以保持兼容。
    original_start = subprocess.check_output(['ps', '-p', str(pid), '-o', 'lstart='], text=True, timeout=2).strip()
    record = {'pid': pid, 'started': original_start, 'target': target}
    path.write_text(json.dumps(record), encoding='utf-8')
    os.chmod(path, 0o600)
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + 15
    last_problem = '服务尚未响应'
    while time.monotonic() < deadline:
        if not matches_owned(process_identity()):
            raise RuntimeError('本次面板子进程已退出或身份发生变化')
        try:
            remaining = deadline - time.monotonic()
            with client.open(f'http://127.0.0.1:{int(port)}/api/state', timeout=min(1, max(.05, remaining))) as response:
                if response.status != 200:
                    raise ValueError('健康响应状态异常')
                state = json.loads(response.read(2 * 1024 * 1024))
            if not isinstance(state, dict):
                raise ValueError('健康响应格式异常')
            if state.get('pid') != pid:
                raise ValueError('端口响应不属于本次面板进程')
            if state.get('preview_mode') is not (mode == 'preview'):
                raise ValueError('面板模式与请求不一致')
            if not matches_owned(process_identity()):
                raise RuntimeError('响应后进程身份已变化')
            break
        except (OSError, ValueError, urllib.error.URLError) as error:
            last_problem = type(error).__name__
            time.sleep(.15)
    else:
        raise RuntimeError('15 秒内面板未能提供健康响应（' + last_problem + '）')
except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, KeyboardInterrupt) as error:
    print('面板启动失败: ' + str(error), file=sys.stderr)
    cleanup_new_child()
    sys.exit(1)
PY
then
  echo "面板未就绪，已尝试回收本次启动的子进程；请查看 $RUN_DIR/bridge-run.log。" >&2
  exit 1
fi
echo "✓ 面板 http://127.0.0.1:$PANEL_PORT"
if [ "$MODE" = preview ]; then
  echo "只读预览：空白数据，不连接微信、不调用 Hermes、不接收或发送消息。"
  echo "安装本机依赖后，可显式运行 ./start.sh --native。"
else
  echo "首次使用默认关闭自动回复和发送，白名单为空；请检查配置后按需开启。"
fi
