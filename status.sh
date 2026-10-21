#!/bin/bash
# 只读检查；仅显示监听状态及本副本日志位置，不输出联系人或白名单内容。
set -u
SOURCE_DIR="$(cd "$(dirname "$0")" && pwd)"
WECHAT_HOME="${WECHAT_HOME:-$SOURCE_DIR}"
HOOK_PORT="${WECHAT_HOOK_PORT:-36060}"
PANEL_PORT="${WECHAT_PANEL_PORT:-8080}"
echo "=== 端口状态（不代表服务归属已验证） ==="
for p in 27042:"微信 Gadget" 58080:"OneBot" "$HOOK_PORT:桥接" "$PANEL_PORT:面板"; do
  port=${p%%:*}; name=${p#*:}
  if lsof -t -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "  ✓ $name :$port 已监听"
  else
    echo "  ○ $name :$port 未监听"
  fi
done
echo
echo "本副本运行记录: $WECHAT_HOME/.runtime"
echo "OneBot 日志: $WECHAT_HOME/onebot/startup.log"
echo "桥接日志: $WECHAT_HOME/.runtime/bridge-run.log"
echo "日志可能包含私人消息，请勿随源码上传。"
echo "面板: http://127.0.0.1:$PANEL_PORT"
