"""Loopback-only management API and static panel, separate from message delivery."""
import copy
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
from panel_backend import BOOL_KEYS, validate_settings


def listener_pid(port):
    try:
        result = subprocess.run(['/usr/sbin/lsof', '-t', '-nP', '-iTCP:'+str(port), '-sTCP:LISTEN'], capture_output=True, text=True, timeout=1.5)
        return int(result.stdout.splitlines()[0]) if result.stdout.strip() else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def process_start_time(pid):
    if pid is None:
        return None
    try:
        result = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'lstart='],
                                env={**os.environ, 'LC_ALL': 'C'}, capture_output=True, text=True, timeout=1.5)
        return time.mktime(time.strptime(result.stdout.strip(), '%a %b %d %H:%M:%S %Y'))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def service_snapshot(ns):
    if ns.get('PREVIEW_MODE'):
        return [
            {'id': 'wechat', 'name': '微信 · FridaGadget', 'on': False, 'status': 'offline',
             'label': '未接入', 'detail': '只读预览不会探测或连接本机微信', 'port': None, 'pid': None},
            {'id': 'onebot', 'name': 'OneBot 收发服务', 'on': False, 'status': 'offline',
             'label': '未启动', 'detail': '原生接入步骤见 docs/NATIVE_SETUP.md', 'port': None, 'pid': None},
            {'id': 'bridge', 'name': '本地管理面板', 'on': True, 'status': 'online',
             'label': '只读预览', 'detail': '独立空数据目录；未开启 Webhook 或发送通道',
             'port': ns['WEB_PORT'], 'pid': os.getpid()},
            {'id': 'hermes', 'name': 'Hermes 模型调用', 'on': False, 'status': 'offline',
             'label': '未调用', 'detail': '预览模式不会执行模型或读取其配置', 'port': None, 'pid': None},
        ]
    cache = ns.get('_health_cache', {})
    now = time.monotonic()
    if now-cache.get('time', -99) > 4:
        items = []
        definitions = [('wechat', '微信 · FridaGadget', 27042), ('onebot', 'OneBot 收发服务', 58080)]
        for key, name, port in definitions:
            online = ns['port_open'](port)
            pid = listener_pid(port) if online else None
            status, label = ('online', '运行中') if online else ('offline', '未运行')
            detail = '本地控制通道已连接；微信登录状态未独立探测' if key == 'wechat' else '等待服务启动'
            if key == 'onebot' and online:
                path = Path(ns.get('RUNTIME_DIR',ns['HOME_DIR']))/'onebot/startup.log'
                tail = ''
                created = process_start_time(pid)
                if path.exists() and created is not None and path.stat().st_mtime >= created:
                    with path.open('rb') as stream:
                        stream.seek(max(0, path.stat().st_size-128000))
                        tail = stream.read().decode(errors='replace')
                previous = ns.get('_onebot_ready', {})
                ready = previous.get('pid') == pid and previous.get('ready', False)
                ready = ready or '捕获到 StartTask manager:' in tail
                if 'unable to intercept' in tail or '[JS日志报错]' in tail:
                    status, label, detail = 'waiting', '初始化异常', '端口已打开，但 hook 日志存在错误；不等同发送可用'
                elif ready:
                    detail = '收发通道运行中，直发 manager 已捕获'
                else:
                    status, label, detail = 'waiting', '等待就绪', '端口已打开，等待捕获微信自然网络任务；尚未确认直发就绪'
                ns['_onebot_ready'] = {'pid': pid, 'ready': ready}
            items.append({'id': key, 'name': name, 'on': online, 'status': status, 'label': label,
                          'detail': detail, 'port': port, 'pid': pid})
        items.append({'id': 'bridge', 'name': 'Hermes 消息桥接', 'on': True, 'status': 'online',
                      'label': '运行中', 'detail': 'Webhook 接收、白名单判断与管理面板',
                      'port': ns['ONEBOT_LISTEN'][1], 'pid': os.getpid()})
        cache = {'time': now, 'items': items}
        ns['_health_cache'] = cache
    items = copy.deepcopy(cache['items'])
    with ns['model_lock']:
        runtime = dict(ns['model_runtime'])
    installed = bool(shutil.which('hermes'))
    status = 'waiting' if runtime['pending'] else runtime['status']
    if not installed:
        status = 'offline'
    labels = {'online': '最近调用成功', 'offline': '不可用', 'waiting': '正在调用', 'unknown': '尚未探测'}
    items.append({'id': 'hermes', 'name': 'Hermes 模型调用', 'on': status == 'online', 'status': status,
                  'label': labels[status], 'detail': runtime['detail'] if installed else '未找到 Hermes CLI',
                  'port': None, 'pid': None, 'last_activity': runtime['last_call']})
    return items


def state_snapshot(ns):
    with ns['state_lock']:
        settings = copy.deepcopy(dict(ns['state']))
    with ns['data_lock']:
        contacts = dict(ns['contacts'])
        history = {key: len(value) for key, value in list(ns['history'].items())}
    telemetry = {}
    try:
        store = ns['get_event_store']()
        stats = store.stats()
        recent = [{key: event[key] for key in ('id', 't', 'lvl', 'msg')} for event in reversed(store.query(limit=8)['items'])]
        for event in recent:
            event['msg'] = event['msg'][:240]
    except (sqlite3.Error, OSError) as error:
        stats = dict.fromkeys(('today_received', 'today_sent', 'today_errors', 'last_received', 'last_sent', 'total_events'))
        with ns['logs_lock']:
            recent = list(ns['logs'][-8:])
        telemetry['telemetry_error'] = '日志存储暂不可用：' + str(error)
    with ns['model_lock']:
        stats['pending'] = ns['model_runtime']['pending']
    account = ns['account_manager'].snapshot() if ns.get('account_manager') is not None else None
    services = service_snapshot(ns)
    if account is not None and not account.get('ready'):
        for service in services:
            if service['id'] == 'onebot' and service['on']:
                service.update(status='waiting',label='待重连',detail=account['detail'])
    return {**settings, **telemetry, 'preview_mode': bool(ns.get('PREVIEW_MODE')), **({'account':account} if account is not None else {}), 'services': services, 'logs': recent,
            'contacts': contacts, 'history': history, 'stats': stats,
            'muted_left': ns['muted_left'](), 'muted_desc': ns['muted_desc'](),
            'bridge_uptime': max(0, time.monotonic()-ns['START_TIME']), 'server_time': time.time(), 'pid': os.getpid()}


def apply_settings(ns, payload):
    patch = validate_settings(payload)
    if ns.get('account_manager') is not None:
        if patch.get('auto_reply') or patch.get('send_enabled'):
            ns['account_guard'](require_ready=True)
        if patch.get('send_mode') == 'clipboard':
            raise ValueError('账号隔离模式请使用按 ID 直发')
    with ns['state_lock']:
        result = {**ns['state'], **patch}
        ns['atomic_json'](ns['STATE_FILE'], {key: result.get(key) for key in ns['_PERSIST']})
        ns['state'].update(patch)
        result = copy.deepcopy(dict(ns['state']))
    ns['log']('info', '配置已保存：' + ('、'.join(patch) or '无变更'))
    return result


def create_panel(ns):
    class Panel(BaseHTTPRequestHandler):
        def _headers(self, status, content_type, length):
            manager = ns.get('account_manager')
            scope = getattr(self,'_account_scope',None)
            if manager is not None and scope is not None and not getattr(self,'_account_rejected',False):
                from account_runtime import AccountChanged
                if manager.current(fresh=True).token != scope.token:
                    raise AccountChanged('账号已变化，请刷新页面')
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(length))
            self.send_header('Cache-Control', 'no-store')
            if scope is not None:
                self.send_header('X-Wechat-Account',scope.token)
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()

        def _j(self, data, status=200):
            body = json.dumps(data, ensure_ascii=False).encode()
            self._headers(status, 'application/json; charset=utf-8', len(body))
            self.wfile.write(body)

        def _local(self, mutation=False):
            host = self.headers.get('Host', '')
            try:
                local = urlparse('http://'+host).hostname in ('127.0.0.1', 'localhost', '::1')
            except ValueError:
                local = False
            origin = self.headers.get('Origin')
            bad_origin = origin is not None and (urlparse(origin).netloc != host or urlparse(origin).scheme not in ('http','https'))
            fetch_site = self.headers.get('Sec-Fetch-Site')
            if not local or (mutation and (bad_origin or (fetch_site and fetch_site not in ('same-origin', 'none')))):
                self._j({'ok': False, 'error': '仅允许本机同源请求'}, 403)
                return False
            return True

        def _payload(self):
            try:
                size = int(self.headers.get('Content-Length', '0'))
            except ValueError:
                raise ValueError('无效请求长度')
            route=urlparse(self.path).path
            # A decoded 5MiB text file can expand sixfold inside a JSON string.
            # Keep the decoded-file limit in parse_chat; this is only the wire budget.
            maximum={'/api/history/import_preview':6*5*1024*1024+65536,'/api/media/upload':12*1024*1024}.get(route,262144)
            if size < 0 or size > maximum:
                raise ValueError('请求过大')
            if self.headers.get('Content-Type','').split(';')[0] != 'application/json':
                raise ValueError('请使用 application/json')
            try:
                self.connection.settimeout(15)
                value = json.loads(self.rfile.read(size) or b'{}')
            except (ValueError, UnicodeError):
                raise ValueError('无效 JSON')
            if not isinstance(value, dict):
                raise ValueError('请求必须是 JSON 对象')
            if ns.get('account_manager') is not None:
                ns['account_guard']()
            return value

        def do_GET(self):
            if not self._local():
                return
            parsed = urlparse(self.path)
            args = parse_qs(parsed.query)
            try:
                if parsed.path in ('/', '/panel.html'):
                    path = Path(__file__).with_name('panel.html')
                    body = path.read_bytes()
                    self._headers(200, 'text/html; charset=utf-8', len(body))
                    self.wfile.write(body)
                elif parsed.path == '/favicon.ico':
                    self._headers(204, 'image/x-icon', 0)
                elif parsed.path == '/api/state':
                    self._j(state_snapshot(ns))
                elif parsed.path == '/api/assistant/settings':
                    self._j({'ok':True,'settings':ns['get_assistant_service']().settings.snapshot()})
                elif parsed.path == '/api/assistant/context':
                    self._j(ns['get_assistant_service']().context_view(args.get('uid', [''])[0]))
                elif parsed.path == '/api/history':
                    get = lambda key,default=None: args.get(key,[default])[0]
                    self._j(ns['get_assistant_service']().history_page(get('uid',''),get('limit',50),get('before')))
                elif parsed.path == '/api/media':
                    asset_id=args.get('id',[''])[0]
                    path,mime,name=ns['get_assistant_service']().media.resolve(asset_id)
                    body=path.read_bytes()
                    self._headers(200,mime,len(body))
                    self.wfile.write(body)
                elif parsed.path == '/api/logs':
                    get = lambda key, default: args.get(key, [default])[0]
                    before = get('before', None)
                    self._j(ns['get_event_store']().query(kind=get('kind','all'),scope=get('scope','all'),
                            q=get('q',''),limit=get('limit',50),before=before))
                elif parsed.path == '/api/toggle':
                    self._j({'ok': False, 'error': '请 POST /api/settings 并指定明确值，GET 不允许更改设置'}, 405)
                elif parsed.path == '/api/mute':
                    self._j({'ok': False, 'error': '请 POST /api/mute'}, 405)
                else:
                    self._j({'ok': False, 'error': '接口不存在'}, 404)
            except (ValueError, TypeError) as error:
                self._j({'ok': False, 'error': str(error)}, 400)
            except (OSError, RuntimeError, sqlite3.Error) as error:
                self._j({'ok': False, 'error': str(error)}, 500)

        def _mute(self, payload):
            minutes = payload.get('minutes', 0)
            if type(minutes) is not int or not 0 <= minutes <= 1440:
                raise ValueError('静音分钟数必须是 0 到 1440 的整数')
            ns['mute'](minutes)
            ns['log']('info', f'静音设置：{minutes} 分钟')
            self._j({'ok': True, 'muted': ns['muted_desc']()})

        def do_POST(self):
            if not self._local(mutation=True):
                return
            path = urlparse(self.path).path
            try:
                data = self._payload()
                if path == '/api/history/import_preview':
                    self._j(ns['get_assistant_service']().import_preview(data))
                elif path == '/api/history/import':
                    self._j(ns['get_assistant_service']().import_commit(data))
                elif path == '/api/assistant/preview':
                    self._j(ns['get_assistant_service']().preview(data))
                elif path == '/api/media/upload':
                    asset=ns['get_assistant_service']().media.upload(data.get('filename','image'),data.get('data_base64',''))
                    self._j({'ok':True,'asset':asset})
                elif path == '/api/assistant/settings':
                    settings=ns['get_assistant_service']().settings.update(data)
                    ns['log']('info','已保存 AI 全局提示词与上下文设置')
                    self._j({'ok':True,'settings':settings})
                elif path == '/api/assistant/profile':
                    settings=ns['get_assistant_service']().settings.update_profile(data)
                    uid=data['uid'].strip()
                    ns['log']('info','已保存联系人 AI 专属配置',uid=uid)
                    self._j({'ok':True,'settings':settings,'profile':settings['profiles'][uid]})
                elif path in ('/api/settings', '/api/allowlist'):
                    self._j({'ok': True, 'state': apply_settings(ns, data)})
                elif path == '/api/mute':
                    self._mute(data)
                elif path == '/api/contacts':
                    delta = 'changes' in data
                    contacts = data.get('changes') if delta else data.get('contacts')
                    deleted = data.get('deleted', [])
                    if not isinstance(deleted, list) or any(not isinstance(key, str) or not key.strip() for key in deleted):
                        raise ValueError('删除列表必须是联系人 ID 数组')
                    if not isinstance(contacts,dict) or len(contacts)>2000:
                        raise ValueError('联系人必须是 ID 到昵称的映射，最多 2000 条')
                    if any(not isinstance(k,str) or not isinstance(v,str) or not k.strip() or not v.strip() or max(len(k),len(v))>200 for k,v in contacts.items()):
                        raise ValueError('联系人 ID 和昵称不能为空，且不超过 200 字')
                    contacts = {key.strip(): value.strip() for key,value in contacts.items()}
                    with ns['data_lock']:
                        if delta:
                            merged = {**ns['contacts'], **contacts}
                            for key in deleted:
                                merged.pop(key, None)
                            contacts = merged
                        if len(contacts)>2000:
                            raise ValueError('联系人最多 2000 条')
                        ns['atomic_json'](ns['CONTACTS_FILE'],contacts)
                        ns['contacts'].clear(); ns['contacts'].update(contacts)
                    ns['log']('info',f'联系人表已保存：{len(contacts)} 条')
                    self._j({'ok': True, 'state': {'contacts': contacts}})
                elif path == '/api/history_clear':
                    uid = data.get('uid','')
                    if not isinstance(uid,str):
                        raise ValueError('上下文目标必须是字符串')
                    with ns['data_lock']:
                        result = dict(ns['history']) if uid else {}
                        result.pop(uid,None)
                        ns['atomic_json'](ns['HISTORY_FILE'],result)
                        ns['history'].clear(); ns['history'].update(result)
                    ns['log']('info','已清空上下文：'+(uid or '全部'))
                    self._j({'ok': True})
                elif path == '/api/send':
                    with ns['state_lock']:
                        if not ns['state']['send_enabled']:
                            raise ValueError('发送已禁用，请先检查回复设置中的允许发送开关')
                    text, target, name = data.get('text',''),data.get('target',''),data.get('name','')
                    if not isinstance(text,str) or not text.strip() or len(text)>12000:
                        raise ValueError('消息不能为空，且不超过 12000 字')
                    if not isinstance(target,str) or not isinstance(name,str) or not (target or name):
                        raise ValueError('请指定目标 ID 或唯一联系人')
                    group = data.get('is_group',False)
                    if type(group) is not bool:
                        raise ValueError('is_group 必须是布尔值')
                    mode = data.get('mode')
                    if mode is not None and mode not in ('onebot','auto','clipboard'):
                        raise ValueError('未知发送模式')
                    if not target and (name.startswith('wxid_') or name == 'filehelper' or name.endswith('@chatroom')):
                        target, name = name, ns['contacts'].get(name,'')
                    try:
                        used = ns['dispatch_send'](target,name,text,group,mode)
                        self._j({'ok': True,'used':used})
                    except Exception as error:
                        ns['log']('error','手动发送失败：'+str(error))
                        self._j({'ok':False,'error':str(error)},502)
                else:
                    self._j({'ok':False,'error':'接口不存在'},404)
            except (ValueError, TypeError) as error:
                self._j({'ok':False,'error':str(error)},400)
            except (OSError, RuntimeError, sqlite3.Error) as error:
                self._j({'ok':False,'error':'保存或执行失败：'+str(error)},500)

        def log_message(self, *args):
            pass
    def account_fenced(method):
        def handle(self):
            self._account_scope = None
            self._account_rejected = False
            manager = ns.get('account_manager')
            parsed = urlparse(self.path)
            if manager is None or not parsed.path.startswith('/api/'):
                return method(self)
            if not self._local(mutation=self.command == 'POST'):
                return
            from account_runtime import AccountChanged
            scope = manager.current(fresh=True)
            self._account_scope = scope
            supplied = self.headers.get('X-Wechat-Account','')
            if parsed.path == '/api/media' and not supplied:
                supplied = parse_qs(parsed.query).get('account_token',[''])[0]
            if parsed.path != '/api/state' and supplied != scope.token:
                self._account_rejected = True
                return self._j({'ok':False,'code':'ACCOUNT_CHANGED','error':'账号已变化，请刷新页面'},409)
            if parsed.path != '/api/state' and not scope.workspace.account_id:
                self._account_rejected = True
                return self._j({'ok':False,'code':'ACCOUNT_UNCONFIRMED','error':'未确认微信账号，操作已暂停'},403)
            try:
                with manager.bind(scope):
                    return method(self)
            except AccountChanged:
                self._account_scope = manager.current(fresh=True)
                self._account_rejected = True
                self._j({'ok':False,'code':'ACCOUNT_CHANGED','error':'账号已变化，请刷新页面'},409)
        return handle
    Panel.do_GET = account_fenced(Panel.do_GET)
    Panel.do_POST = account_fenced(Panel.do_POST)
    return Panel
