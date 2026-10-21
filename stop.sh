#!/bin/bash
# 只停止本副本启动并记录的服务，微信保持运行。
set -euo pipefail
SOURCE_DIR="$(cd "$(dirname "$0")" && pwd)"
export WECHAT_HOME="${WECHAT_HOME:-$SOURCE_DIR}"
PY="${WECHAT_PYTHON:-python3}"

"$PY" - "$SOURCE_DIR" <<'PY'
import json, os, pathlib, signal, subprocess, sys, time

root = pathlib.Path(os.environ['WECHAT_HOME']).expanduser().resolve()
source = pathlib.Path(sys.argv[1]).resolve()
for name, target in [('bridge', source/'wechat_hermes_bridge.py'), ('onebot', root/'onebot/onebot')]:
    record_path = root/'.runtime'/f'{name}.pid.json'
    if not record_path.is_file() or record_path.is_symlink():
        print(f'{name}: 无本副本的进程记录，跳过。')
        continue
    try:
        record = json.loads(record_path.read_text(encoding='utf-8'))
        pid = int(record['pid'])
        if pid <= 1 or pathlib.Path(record['target']).resolve() != target.resolve():
            raise ValueError('进程记录与本副本不符')
        started = subprocess.run(['ps', '-p', str(pid), '-o', 'lstart='], capture_output=True, text=True)
        if started.returncode or not started.stdout.strip():
            record_path.unlink()
            print(f'{name}: 已停止。')
            continue
        command = subprocess.check_output(['ps', '-ww', '-p', str(pid), '-o', 'command='], text=True).strip()
        # ps 不保留含空格路径的引号；同时校验绝对目标路径和启动时间。
        expected = str(target.resolve())
        matches = (command.startswith(expected + ' ') if name == 'onebot'
                   else command.endswith(' ' + expected))
        if not matches or started.stdout.strip() != record.get('started'):
            raise ValueError('PID、启动时间或命令身份不符，拒绝停止未知进程')
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = subprocess.run(['ps', '-p', str(pid), '-o', 'lstart='], capture_output=True, text=True)
            if status.returncode or status.stdout.strip() != record['started']:
                record_path.unlink(missing_ok=True)
                print(f'{name}: 已停止。')
                break
            time.sleep(.1)
        else:
            print(f'{name}: 已发送正常退出请求；进程仍在结束中，未强制终止。')
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(f'{name}: {error}', file=sys.stderr)
print('完成。微信保持运行。')
PY
