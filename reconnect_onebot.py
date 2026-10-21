#!/usr/bin/env python3
"""Reconnect only OneBot to the native current account; never opens/restarts WeChat."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from account_runtime import NativeAccountProbe

SOURCE_DIR=Path(__file__).resolve().parent
ROOT=Path(os.environ.get('WECHAT_HOME',str(SOURCE_DIR))).expanduser().resolve()

def configured_token():
    """Read explicit credentials or the private token created by start.sh --native."""
    value=os.environ.get('WECHAT_ONEBOT_TOKEN','')
    if value:
        return value
    path=ROOT/'.runtime'/'onebot-token'
    if path.is_symlink() or not path.is_file():
        raise RuntimeError('请先运行 ./start.sh --native 生成本地令牌，或设置 WECHAT_ONEBOT_TOKEN')
    os.chmod(path,0o600)
    value=path.read_text(encoding='utf-8').strip()
    if not value:
        raise RuntimeError('本地 OneBot 令牌为空')
    return value

def owned_onebot(pid):
    """Require this checkout's recorded PID, start time and executable identity."""
    record_path=ROOT/'.runtime'/'onebot.pid.json'
    if record_path.is_symlink() or not record_path.is_file():
        raise RuntimeError('OneBot 没有本副本的进程记录，拒绝停止未知服务')
    record=json.loads(record_path.read_text(encoding='utf-8'))
    target=(ROOT/'onebot'/'onebot').resolve()
    if int(record.get('pid',0))!=int(pid) or int(pid)<=1 or Path(record.get('target','')).resolve()!=target:
        raise RuntimeError('OneBot 进程记录不属于本副本，拒绝停止')
    started=subprocess.check_output(['ps','-p',str(pid),'-o','lstart='],text=True).strip()
    command=subprocess.check_output(['ps','-ww','-p',str(pid),'-o','command='],text=True).strip()
    if not started or started!=record.get('started') or not command.startswith(str(target)+' '):
        raise RuntimeError('OneBot PID、启动时间或命令身份不符，拒绝停止')
    return record_path

def current_context():
    identity=NativeAccountProbe(ROOT)()
    if not identity.get('id'):
        raise RuntimeError('无法唯一确认当前微信账号，不重连、不猜测目录')
    accounts=ROOT.resolve()/'accounts'
    workspace=accounts/hashlib.sha256(identity['id'].encode('utf-8')).hexdigest()
    image=workspace/'onebot'/'image'
    if any(path.is_symlink() for path in (accounts,workspace,image.parent,image)):
        raise RuntimeError('账号图片缓存路径不允许符号链接，不回退到其他账号目录')
    return identity,str(image)+os.sep

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run',action='store_true')
    parser.add_argument('--yes',action='store_true')
    parser.add_argument('--image-path',action='store_true',help='Read-only current account workspace media directory for existing launchers')
    args=parser.parse_args()
    native,image=current_context()
    if args.image_path:
        print(image);return
    token=configured_token()
    config=os.environ.get('WECHAT_CONFIG',str(SOURCE_DIR/'onebot/wechat_version/4_1_12_53_mac.json'))
    if not config or not Path(config).expanduser().is_file():
        raise RuntimeError('请设置 WECHAT_CONFIG 为有效的微信版本配置文件路径')
    config=str(Path(config).expanduser().resolve())
    runtime=ROOT/'onebot'
    if not (runtime/'onebot').is_file() or not os.access(runtime/'onebot',os.X_OK):
        raise RuntimeError('请先安装 OneBot 可执行文件到 WECHAT_HOME/onebot/onebot')
    base='http://127.0.0.1:'+str(int(os.environ.get('WECHAT_PANEL_PORT','8080')))
    state=json.load(urllib.request.urlopen(base+'/api/state',timeout=12))
    account=state.get('account') or {}
    if account.get('id')!=native['id'] or not account.get('verified'):
        raise RuntimeError('后台账号与原生微信账号不一致，停止重连')
    if state.get('stats',{}).get('pending'):
        raise RuntimeError('有正在生成的回复，暂不重连')
    bot=next((s for s in state['services'] if s['id']=='onebot'),{})
    old_pid=bot.get('pid')
    if old_pid:
        owned_onebot(old_pid)
    report={'only_reconnect_onebot':True,'restart_wechat':False,'restart_bridge':False,
            'wechat_pid':native['wechat_pid'],'old_onebot_pid':old_pid,'current_account_directory_only':True,
            'auto_reply_setting_preserved':state.get('auto_reply')}
    print(json.dumps(report,ensure_ascii=False),flush=True)
    if args.dry_run:return
    if not args.yes and input('只重连 OneBot，按原设置恢复收发，继续？[y/N] ').strip().lower()!='y':return
    image_dir=Path(image)
    image_dir.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    os.chmod(image_dir.parent,0o700)
    image_dir.mkdir(mode=0o700,exist_ok=True)
    os.chmod(image_dir,0o700)
    if old_pid:
        record_path=owned_onebot(old_pid)
        os.kill(old_pid,signal.SIGTERM)
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            result=subprocess.run(['lsof','-t','-nP','-iTCP:58080','-sTCP:LISTEN'],capture_output=True,text=True)
            if not result.stdout.strip():break
            time.sleep(.15)
        else:raise RuntimeError('旧OneBot端口未释放，没有重复启动')
        record_path.unlink(missing_ok=True)
    occupied=subprocess.run(['lsof','-t','-nP','-iTCP:58080','-sTCP:LISTEN'],capture_output=True,text=True)
    if occupied.stdout.strip():
        raise RuntimeError('58080 端口被占用，不启动、不终止未知进程')
    check=NativeAccountProbe(ROOT)()
    if (check['id'],check['wechat_pid'])!=(native['id'],native['wechat_pid']):
        raise RuntimeError('确认过程中微信账号或进程变化，已停止')
    log=runtime/'startup.log'
    if log.exists():
        backup=ROOT/'diagnostics'/('onebot-before-reconnect-'+datetime.datetime.now().strftime('%Y%m%d-%H%M%S')+'.log')
        backup.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        shutil.copy2(log,backup);os.chmod(backup,0o600)
    run_dir=ROOT/'.runtime'
    run_dir.mkdir(mode=0o700,parents=True,exist_ok=True)
    os.chmod(run_dir,0o700)
    started=subprocess.check_output(['ps','-p',str(os.getpid()),'-o','lstart='],text=True).strip()
    record_path=run_dir/'onebot.pid.json'
    record_path.write_text(json.dumps({'pid':os.getpid(),'started':started,'target':str((runtime/'onebot').resolve())}),encoding='utf-8')
    os.chmod(record_path,0o600)
    fd=os.open(str(log),os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600);os.chmod(log,0o600)
    os.dup2(fd,1);os.dup2(fd,2);os.close(fd)
    os.chdir(runtime)
    command=[str(runtime/'onebot'),'-type=gadget','-image_path='+image,'-token='+token,
             '-receive_host=127.0.0.1:58080',
             '-send_url=http://127.0.0.1:'+str(int(os.environ.get('WECHAT_HOOK_PORT','36060')))+'/onebot',
             '-wechat_conf='+config]
    os.execv(command[0],command)

if __name__=='__main__':
    try:main()
    except (RuntimeError,OSError,ValueError,subprocess.SubprocessError) as error:
        print('OneBot reconnect stopped: '+str(error),file=sys.stderr);sys.exit(1)
