#!/usr/bin/env python3
"""
微信 ⇄ Hermes 桥接（4.1.12 稳定版）
收消息: OneBot hook -> http://127.0.0.1:36060/onebot
发消息: OneBot 按目标 ID 直发；剪贴板仅在显式选择时使用
管理面板: http://127.0.0.1:8080
"""
import hashlib, hmac, json, os, re, sqlite3, subprocess, sys, tempfile, time, socket, socketserver, urllib.request, signal
from contextvars import ContextVar
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from threading import Thread, Lock, RLock
from urllib.parse import urlparse, parse_qs
from panel_backend import EventStore

ONEBOT_LISTEN = ("127.0.0.1", int(os.environ.get("WECHAT_HOOK_PORT", "36060")))
ONEBOT_API = "http://127.0.0.1:58080"
TOKEN = os.environ.get("WECHAT_ONEBOT_TOKEN", "")
WEB_PORT = int(os.environ.get("WECHAT_PANEL_PORT", "8080"))
HERMES_TIMEOUT = 150
PREVIEW_MODE = False  # Set only by the explicit runtime bootstrap, never during tests/import.

state = {"auto_reply": False, "reply_groups": False, "send_enabled": False,
         "allowlist": [],           # 配置缺失时谁都不回复，实际白名单从 config.json 读取
         "reply_prefix": "", "reply_suffix": "",  # 仅自动回复使用，默认不加标记
         "send_mode": "onebot",     # auto 是直发兼容别名，不做不确定的 UI 回落
         "auto_skip": True,         # 不需要回复时自动闭嘴
         "max_consec": 3,           # 连续短消息最多回几轮，超了就停
         "allow_all": False,        # 白名单为空时是否放行所有人（默认否，防误伤）
         "muted_until": 0}          # 静音到某个时间戳（0=不静音）
state_lock = RLock()
STATE_DEFAULTS = json.loads(json.dumps(state))
data_lock = RLock()
logs = []
logs_lock = Lock()

# ---------------- 持久化：state 落盘 + 日志追加写文件 ----------------
SOURCE_DIR = Path(__file__).resolve().parent
HOME_DIR = str(Path(os.environ.get("WECHAT_HOME", str(SOURCE_DIR))).expanduser().resolve())
RUNTIME_DIR = HOME_DIR  # Native runtime stays here; account data lives under accounts/.
account_manager = None
STATE_FILE = os.path.join(HOME_DIR, "config.json")
LOG_FILE = os.path.join(HOME_DIR, "bridge.log")
EVENT_DB = os.path.join(HOME_DIR, "events.db")
START_TIME = time.monotonic()
_event_store = None
_store_lock = Lock()
_event_context = ContextVar("wechat_event_context", default={})
model_lock = Lock()
direct_send_lock = RLock()
model_runtime = {"status": "unknown", "detail": "尚未调用模型，不代表连接失败", "pending": 0, "last_call": None}

# 需要落盘的字段（muted_until 也存，重启后静音仍然有效）
_PERSIST = ("auto_reply", "reply_groups", "send_enabled", "allowlist",
            "reply_prefix", "reply_suffix", "send_mode", "auto_skip", "max_consec",
            "allow_all", "muted_until")


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            d = json.load(f)
        for k in _PERSIST:
            if k in d:
                state[k] = d[k]
        return True
    except Exception:
        return False


def atomic_json(path, value):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".wechat-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_state():
    with state_lock:
        atomic_json(STATE_FILE, {k: state.get(k) for k in _PERSIST})


def get_event_store():
    global _event_store
    with _store_lock:
        if account_manager is not None:
            workspace = account_manager.scope().workspace
            if workspace.event_store is None:
                workspace.event_store = EventStore(workspace.home/'events.db')
                workspace.event_store.import_legacy(workspace.home/'bridge.log')
            return workspace.event_store
        if _event_store is None:
            store = EventStore(EVENT_DB)
            store.import_legacy(LOG_FILE)
            _event_store = store
        return _event_store


def mute(minutes: int):
    """静音 N 分钟（0 = 取消静音）。"""
    with state_lock:
        state["muted_until"] = 0 if minutes <= 0 else int(time.time() + minutes * 60)
    save_state()


def muted_left():
    """还剩多少秒静音；0 表示没静音。"""
    left = int(state.get("muted_until", 0) or 0) - int(time.time())
    return max(0, left)


def muted_desc():
    s = muted_left()
    if not s:
        return "未静音"
    m, sec = divmod(s, 60)
    return f"静音中，还剩 {m} 分 {sec} 秒" if m else f"静音中，还剩 {sec} 秒"
SEEN = set()
SEND_LOCK = Lock()   # 剪贴板操作串行化

# wxid → 用于搜索的名字（昵称/备注名）。昵称常常等于 wxid，靠这张表兜底。
CONTACTS_FILE = os.path.join(HOME_DIR, "contacts.json")


def load_contacts():
    try:
        with open(CONTACTS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_contacts():
    try:
        with data_lock:
            atomic_json(CONTACTS_FILE, dict(contacts))
    except Exception as e:
        log("error", f"保存联系人表失败: {e}")


# Imports must never read a user's legacy data. Explicit native bootstrap loads
# only the verified account workspace; preview bootstrap keeps an empty account.
contacts = {}   # {wxid: 搜索名}

# ---------------- 每个联系人的对话历史（作为上下文传给 LLM）----------------
HISTORY_FILE = os.path.join(HOME_DIR, "history.json")
HISTORY_LIMIT = 5000        # 保存历史；模型携带条数在 AI 会话设置中单独控制
CONTEXT_TURNS = 10          # 传给 LLM 的历史条数上限


def load_history():
    try:
        with open(HISTORY_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_history():
    try:
        with data_lock:
            atomic_json(HISTORY_FILE, dict(history))
    except Exception as e:
        log("error", f"保存对话历史失败: {e}")


history = {}   # Loaded only by explicit account bootstrap, never on import.


def record_turn(uid, role, text, model_text=None, source=None, message_id=None, timestamp=None, images=None):
    if not uid or not text:
        return
    with data_lock:
        attachments = []
        for image in (images or [])[:1]:
            if isinstance(image, dict) and isinstance(image.get('id'), str) and re.fullmatch(r'[0-9a-f]{32}', image['id']):
                attachments.append({'id': image['id'], **({'quality': 'thumbnail'} if image.get('quality') == 'thumbnail' else {})})
        h = history.setdefault(uid, [])
        mid = message_id if isinstance(message_id,str) and message_id else None
        if mid:
            for existing in h:
                if existing.get('message_id')==mid and existing.get('role')==role:
                    if attachments and not existing.get('images'):
                        existing['images'] = attachments
                        save_history()
                    if source in ('manual','ai') and existing.get('source')=='self_sync':
                        existing.update(source=source,text=text)
                        if model_text is not None and model_text!=text:existing['model_text']=model_text
                        save_history()
                    return False
        row = {"role": role, "text": text, "t": int(timestamp if timestamp is not None else time.time())}
        if source:row['source']=source
        if mid:row['message_id']=mid
        if attachments:row['images'] = attachments
        if role == 'assistant' and model_text is not None and model_text != text:
            row['model_text'] = model_text  # 上下文不重复学习发送层添加的标记
        h.append(row)
        if len(h) > HISTORY_LIMIT:
            del h[:-HISTORY_LIMIT]
        save_history()
        return True


def build_context(uid, who):
    """把该联系人之前的对话拼成上下文。没有历史则返回空串。"""
    h = history.get(uid) or []
    if not h:
        return ""
    lines = []
    for m in h[-CONTEXT_TURNS:]:
        t = time.strftime("%m-%d %H:%M", time.localtime(m.get("t", 0)))
        speaker = who if m["role"] == "user" else "我"
        lines.append(f"[{t}] {speaker}: {m['text']}")
    return "\n".join(lines)


def group_name(data):
    """Only explicit conversation metadata is a group title, never sender.nickname."""
    value=data.get('group_name')
    return value if isinstance(value,str) and value.strip() else ''


def conversation_name(data,uid):
    sender=data.get('sender') if isinstance(data.get('sender'),dict) else {}
    cached=contacts.get(uid)
    if data.get('message_type')=='group':
        return group_name(data) or (cached if cached and cached!=uid else '未获取群名')
    return cached or sender.get('nickname') or uid


def learn_contact(uid, nickname):
    """从消息里学习 wxid → 昵称 映射（只在昵称有信息量时记录）。"""
    if not uid or not nickname or nickname == uid:
        return
    with data_lock:
        if contacts.get(uid) == nickname:
            return
        contacts[uid] = nickname
        save_contacts()
    log("info", f"记录联系人: {uid} → {nickname}")


def log(level, msg, **fields):
    details = {**_event_context.get(), **fields}
    try:
        event_id = get_event_store().append(level, msg, **details)
    except (sqlite3.Error, OSError):
        event_id = None  # Logging must not abort message handling or a successful settings write.
    entry = {"id": event_id, "t": time.strftime("%H:%M:%S"), "lvl": level, "msg": msg[:200]}
    with logs_lock:
        logs.append(entry)
        del logs[:-100]
    # 追加写盘（重启也不丢；按天轮转，保留 7 天）
    try:
        line = f"{time.strftime('%Y-%m-%d')} {entry['t']} [{level}] {msg}\n"
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 5_000_000:
            os.rename(LOG_FILE, LOG_FILE + ".old")
    except Exception:
        pass


def one_line(s, n=120):
    """把多行内容压成单行，避免一条消息在日志里拆成好几行。"""
    s = " ".join((s or "").split())
    return s[:n] + ("…" if len(s) > n else "")


# ---------- 「不需要回复」的本地快速过滤（不调 LLM，省时省钱）----------
# 收尾语/客套话/纯表情：这类消息回了就是没完没了地接话
NO_REPLY_PATTERNS = [
    r"^[嗯哦噢啊哈呵额呃诶欸嗨~～。.！!？?\s]+$",
    r"^(好|好的|好嘞|好滴|好哒|行|行吧|可以|收到|了解|明白|知道了|懂了|OK|Ok|ok|"
    r"妥|中|没问题|嗯嗯|对对|是的|没错)[。.!！~～\s]*$",
    r"^(谢谢|谢了|多谢|感谢|辛苦了|麻烦你了|辛苦|3Q|thx|thanks)[。.!！~～\s]*$",
    r"^(拜拜|再见|回见|晚安|早安|早|睡了|先这样|就这样|先不聊了|不聊了|"
    r"没事|不用了|算了|好的拜拜|晚点聊|改天聊)[。.!！~～\s]*$",
    r"^[\[【(（][^\]】)）]{0,8}(表情|图片|动图|照片|视频|链接|小程序|语音|"
    r"位置|名片|文件|转账|红包|sticker|emoji)[^\]】)）]{0,8}[\]】)）]$",
    r"^[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F\U0001F1E6-\U0001F1FF\s]+$",  # 纯 emoji
]
NO_REPLY_RE = [re.compile(p, re.I) for p in NO_REPLY_PATTERNS]

SHORT_MSG_LEN = 6      # 短于这个长度算"短消息"（用于连续接话计数）
consec_short = {}      # uid -> 连续短消息回了几轮


def need_reply(text):
    """本地判断这条消息是否值得回复。返回 (bool, 原因)"""
    t = (text or "").strip()
    if not t:
        return False, "空消息"
    for r in NO_REPLY_RE:
        if r.match(t):
            return False, f"收尾/客套/表情（{t[:12]}）"
    return True, ""


def verify(body: bytes, sig: str) -> bool:
    if not TOKEN or not sig.startswith("sha1="):
        return False
    return hmac.compare_digest(sig[5:], hmac.new(TOKEN.encode(), body, hashlib.sha1).hexdigest())


def send_via_onebot(target_id: str, text: str, is_group: bool = False):
    """按 wxid/群号精确发送。只有服务器确认后 OneBot 才返回 ok；超时不重发。"""
    if not TOKEN:
        raise RuntimeError("请显式设置 WECHAT_ONEBOT_TOKEN，未配置时禁止发送")
    url = f"{ONEBOT_API}/send_{'group' if is_group else 'private'}_msg"
    payload = {"group_id" if is_group else "user_id": target_id,
               "message": [{"type": "text", "data": {"text": text}}]}
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {TOKEN}"}, method="POST")
    native_log=Path(RUNTIME_DIR)/'onebot/startup.log'
    try:log_offset=native_log.stat().st_size
    except OSError:log_offset=None
    with urllib.request.urlopen(req, timeout=25) as resp:
        r = json.loads(resp.read() or b"{}")
    if r.get("status") != "ok":
        raise RuntimeError(f"onebot 返回: {r}")
    # Receipt metadata, not a substitute for the successful authenticated response.
    # dispatch_send serializes bridge sends. Never guess if several acks interleave.
    if log_offset is not None:
        try:
            with native_log.open('rb') as stream:
                stream.seek(log_offset);tail=stream.read().decode('utf-8',errors='replace')
            ids=re.findall(r'\[direct-send\] server_ack taskId=\d+ ret=0 server_message_id=(\d+)',tail)
            if len(ids)==1:return ids[0]
        except OSError:pass
    return None


def dispatch_send(target_id: str, nickname: str, text: str,
                  is_group: bool = False, mode: str = None, model_text=None, source='manual') -> str:
    """按配置选择发送方式，返回实际使用的方式。"""
    account_guard(require_ready=True)
    if mode is None:
        with state_lock:
            mode = state.get("send_mode", "onebot")

    if mode in ("onebot", "auto"):
        if not target_id:
            if nickname == "文件传输助手":
                target_id = "filehelper"
            else:
                with data_lock:
                    matches = [uid for uid, name in contacts.items() if name == nickname]
                if len(matches) == 1:
                    target_id = matches[0]
        if not target_id:
            raise RuntimeError("直发需要唯一目标 wxid/群 ID，请在联系人表补全；不会回落到昵称搜索")
        account_guard(require_ready=True)
        with direct_send_lock:
            mid=send_via_onebot(target_id, text, is_group)
            record_turn(target_id,'assistant',text,model_text=model_text,source=source,message_id=mid)
            log('sent','已发送给 '+(contacts.get(target_id) or target_id)+'（onebot）',uid=target_id,
                name=contacts.get(target_id) or target_id,scope='group' if is_group else 'private',text=text,member='我')
        return "onebot"

    if account_manager is not None:
        raise ValueError('账号隔离模式仅支持按 ID 直发，不使用剪贴板搜索')
    name = nickname or contacts.get(target_id, "")
    if not name or name == target_id:
        raise RuntimeError(
            f"剪贴板模式需要联系人名字，但 {target_id} 没有可用昵称。"
            f"请在面板的「联系人表」里补上 {target_id} 的微信昵称/备注名")
    send_via_clipboard(name, text)
    return "clipboard"


def send_via_clipboard(nickname: str, text: str) -> bool:
    """用 剪贴板 + Cmd+F 搜索 + Cmd+V 发送，避免 hook 发消息导致微信崩溃。"""
    with SEND_LOCK:
        def sa(script, timeout=5):
            return subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)

        def paste(s):
            subprocess.run(["pbcopy"], input=s.encode(), timeout=3)

        sa('tell application id "com.tencent.xinWeChat" to activate')
        time.sleep(0.25)

        # 打开搜索
        sa('tell application "System Events" to keystroke "f" using command down')
        time.sleep(0.25)

        # 粘贴联系人名
        paste(nickname)
        time.sleep(0.1)
        sa('tell application "System Events" to keystroke "v" using command down')
        time.sleep(1.0)

        sa('tell application "System Events" to keystroke return')
        time.sleep(0.4)

        # 粘贴正文
        paste(text)
        time.sleep(0.12)
        sa('tell application "System Events" to keystroke "v" using command down')
        time.sleep(0.12)
        sa('tell application "System Events" to keystroke return')
        time.sleep(0.15)
        return True


def format_auto_reply(text):
    """Decorate only AI replies, never manual sends, empty output, or SKIP."""
    if not text or text.strip().upper() == 'SKIP':
        return text
    with state_lock:
        prefix, suffix = state.get('reply_prefix', ''), state.get('reply_suffix', '')
    return (prefix if prefix and not text.startswith(prefix) else '') + text + (suffix if suffix and not text.endswith(suffix) else '')


def ask_hermes(text, who, is_group, ctx="", image_path=None, prepared=False):
    with model_lock:
        model_runtime.update(status="waiting", detail="正在生成回复", last_call=time.time())
        model_runtime["pending"] += 1
    try:
        result = _ask_hermes(text, who, is_group, ctx, image_path, prepared)
        with model_lock:
            model_runtime.update(status="online" if result else "offline",
                                 detail="最近一次模型调用成功" if result else "最近一次模型调用未返回有效内容")
        return result
    except Exception as error:
        with model_lock:
            model_runtime.update(status="offline", detail="最近一次模型调用失败：" + str(error)[:180])
        raise
    finally:
        with model_lock:
            model_runtime["pending"] -= 1


def _ask_hermes(text, who, is_group, ctx="", image_path=None, prepared=False):
    """One read-only model turn; prompts/images stay in controlled local files."""
    from assistant_core import compose_prompt
    prompt=text if prepared else compose_prompt(text,who,is_group,ctx,get_assistant_service().settings.effective(who))
    directory=Path(HOME_DIR)/'.assistant_runs'
    directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    fd,path=tempfile.mkstemp(prefix='prompt-',suffix='.txt',dir=str(directory))
    image_copy=None
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as stream: stream.write(prompt)
        command=['hermes','chat','-Q','--ignore-rules','--source','tool','-t','none',
                 '--max-turns','1','--run-budget','120','--query-file',path]
        if image_path:
            from assistant_media import detect_image_mime, MAX_UPLOAD_BYTES
            with open(image_path,'rb') as stream: image_bytes=stream.read(MAX_UPLOAD_BYTES+1)
            mime=detect_image_mime(image_bytes)
            if not mime or len(image_bytes)>MAX_UPLOAD_BYTES: raise ValueError('模型图片输入无效或超过 8MB')
            suffix={'image/png':'.png','image/jpeg':'.jpg','image/webp':'.webp'}[mime]
            image_fd,image_copy=tempfile.mkstemp(prefix='image-',suffix=suffix,dir=str(directory))
            with os.fdopen(image_fd,'wb') as stream: stream.write(image_bytes)
            command.extend(['--image',image_copy])
        result=run_model_process(command,directory)
        if result.returncode!=0:
            error_text=((result.stderr or '')+'\n'+(result.stdout or '')).lower()
            if any(marker in error_text for marker in ('context_length_exceeded','model_context_window_exceeded','maximum context length','context window exceeded','prompt is too long','input is too long','too many input tokens')):
                raise RuntimeError('历史内容超出当前模型的上下文窗口，请改为最近 N 条或使用更大窗口的模型；未自动截断或重试')
            raise RuntimeError('Hermes 调用失败（退出码 '+str(result.returncode)+'），请检查模型连接或额度')
        reply=clean_hermes_output(result.stdout)
        if not reply: raise RuntimeError('模型未返回可用正文')
        return reply
    finally:
        if os.path.exists(path): os.unlink(path)
        if image_copy and os.path.exists(image_copy): os.unlink(image_copy)


def run_model_process(command, directory):
    kwargs = {'text':True,'cwd':str(directory),'env':{**os.environ,'NO_COLOR':'1'}}
    if account_manager is None:
        return subprocess.run(command,capture_output=True,timeout=HERMES_TIMEOUT,**kwargs)
    account_guard()
    process = subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True,**kwargs)
    deadline = time.monotonic()+HERMES_TIMEOUT
    try:
        while True:
            account_guard()
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(command,HERMES_TIMEOUT)
            try:
                out,err = process.communicate(timeout=.5)
                account_guard()
                return subprocess.CompletedProcess(command,process.returncode,out,err)
            except subprocess.TimeoutExpired:
                continue
    except BaseException:
        try:
            os.killpg(process.pid,signal.SIGTERM)
            process.communicate(timeout=2)
        except (ProcessLookupError,subprocess.TimeoutExpired):
            try: os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError: pass
            process.communicate()
        raise


def clean_hermes_output(out: str) -> str:
    """从 hermes CLI 输出里抽出真正的回复。

    CLI 输出结构（实测）：
        Query: <提示词>                 <- 回显，要去掉
        Initializing agent...
        ────────────────
        <状态/报错行>
         ─  ⚕ Hermes  ──────────     <- 正文框开始
           <真正的回复>
         ─────────────────────
        Resume this session with:      <- 页脚，之后全部丢弃
          hermes --resume ...
        Session: ...  Title: ...  Duration: ...

    加 `-Q` 后通常只剩正文，这里做兼容。
    检测到 API 失败（配额/限流）时返回空串，绝不把报错发给别人。
    """
    if not out:
        return ""
    lines = out.splitlines()

    # 1) 砍掉页脚
    for i, l in enumerate(lines):
        if l.strip().startswith(("Resume this session", "Session:", "Title:")):
            lines = lines[:i]
            break

    # 2) 只在「⚕ Hermes」框之后才是正文；没有框则退回到分隔线逻辑
    start = 0
    for i, l in enumerate(lines):
        if "Hermes" in l and "─" in l:
            start = i + 1
    if start == 0:
        for i, l in enumerate(lines):
            s = l.strip()
            if len(s) >= 5 and not set(s) - set("─═-=—_ "):
                start = i + 1

    def is_border(s):
        s = s.strip()
        return (not s) or (len(s) >= 3 and not set(s) - set("─═-=—_ ⚕"))

    body = lines[start:]
    while body and is_border(body[0]):
        body.pop(0)
    while body and is_border(body[-1]):
        body.pop()

    text = "\n".join(x.strip() for x in body).strip()

    # 3) 失败检测
    if any(m in text for m in ("API call failed", "Rate limited after",
                               "insufficient_quota", "Final error")):
        log("error", f"Hermes 调用失败（模型/配额）: {one_line(text, 80)}")
        return ""

    # 4) 兜底：还残留 CLI 回显行就逐行剔除
    bad = ("Query:", "Initializing", "Thinking", "⚠", "🔄", "⏱️", "🔌", "🌐", "📝", "📋", "session_id:", "Warning:")
    kept = [x for x in text.splitlines() if not x.lstrip().startswith(bad)]
    return "\n".join(kept).strip()


_conversation_locks = {}

def message_route(data, own):
    """Authenticate direction from participants; self_id alone is not the login ID."""
    sender=data.get('sender') if isinstance(data.get('sender'),dict) else {}
    group=str(data.get('group_id') or '')
    top=str(data.get('user_id') or '')
    nested=str(sender.get('user_id') or '')
    if top and nested and top!=nested and top!=group:
        return None
    from_id=nested or top
    to_id=str(data.get('to_user_id') or data.get('receiver_id') or '')
    self_id=str(data.get('self_id') or '')
    if from_id==own:
        peer=group or to_id or (self_id if self_id!=own else '')
        direction='self_sync'
    elif self_id==own or to_id==own:
        peer=group or from_id
        direction='incoming'
    else:return None
    if not peer or peer==own or len(peer)>200 or any(ord(c)<32 for c in peer):return None
    return direction,peer,peer.endswith('@chatroom')


def handle_incoming(data, account_scope=None):
    if account_manager is not None:
        if not isinstance(data, dict):
            return
        scope = account_scope or account_manager.current(fresh=True)
        try:
            account_manager.assert_current(scope, fresh=True)
        except ValueError:
            return
        route=message_route(data,scope.workspace.account_id)
        if route is None:return
        direction,peer,group=route
        data={**data,'_direction':direction,'_conversation_uid':peer,
              'message_type':'group' if group else 'private','group_id':peer if group else ''}
        with account_manager.bind(scope):
            return _handle_scoped_incoming(data)
    return _handle_scoped_incoming(data)


def _handle_scoped_incoming(data):
    if not isinstance(data,dict):
        log('error','收到非对象格式的消息，已忽略')
        return
    with data_lock:
        mid = data.get('message_id', '')
        key=(data.get('_direction','incoming'),data.get('_conversation_uid') or data.get('group_id') or data.get('user_id'),str(mid))
        if mid and key in SEEN:
            return
        if mid:
            SEEN.add(key)
        if len(SEEN) > 2000:
            SEEN.clear()
    group = data.get('message_type') == 'group'
    uid = str(data.get('_conversation_uid') or (data.get('group_id') if group else data.get('user_id')) or '')
    sender_data = data.get('sender') if isinstance(data.get('sender'),dict) else {}
    sender = sender_data.get('nickname') or uid
    own_sync=data.get('_direction')=='self_sync'
    display=(contacts.get(uid) or ('未获取群名' if group else uid)) if own_sync else conversation_name(data,uid)
    context={'scope':'group' if group else 'private','uid':uid,'name':display,'text':''}
    token=_event_context.set(context)
    try:
        if own_sync:
            return _process_own_sync(data,uid)
        with data_lock:
            lock_key = (account_manager.scope().workspace.account_id,uid) if account_manager is not None else uid
            lock=_conversation_locks.setdefault(lock_key,RLock())
        with lock:
            return _process_incoming(data)
    except Exception as error:
        log('error','接收消息处理失败：'+str(error))
    finally:
        _event_context.reset(token)


def _process_own_sync(data,uid):
    from assistant_media import normalize_message
    service=get_assistant_service()
    message=normalize_message(data,service.media)
    if message['skip']:return
    text=message['text'] or ('[图片]' if message['images'] else '')
    if not text:return
    sender=data.get('sender') if isinstance(data.get('sender'),dict) else {}
    prefix=re.match(r'^([^:\n]{1,200})[:：]\s*(.+)$',text,re.S)
    if data['message_type']=='group' and prefix and prefix[1] in (str(data.get('user_id') or ''),sender.get('nickname')):
        text=prefix[2]
    with data_lock:
        if uid not in contacts:
            contacts[uid]=uid;save_contacts()
        added=record_turn(uid,'assistant',text,source='self_sync',message_id=str(data.get('message_id') or ''),images=message['images'])
    if added:
        log('sent','本人发送 → '+(contacts.get(uid) or uid)+': '+one_line(text),text=text,member='我')
    # Never call the model or dispatch a message in response to our own sync.


def _process_incoming(data):
    from assistant_media import normalize_message
    is_group=data.get('message_type')=='group'
    gid=str(data.get('group_id') or '')
    uid=str(data.get('_conversation_uid') or (gid if is_group else data.get('user_id')) or '')
    if not uid: return
    sender_data=data.get('sender') if isinstance(data.get('sender'),dict) else {}
    nick=sender_data.get('nickname') or ''
    sender=nick or uid
    if sender=='weixin': return
    service=get_assistant_service()
    message=normalize_message(data,service.media)
    text=message['text']
    context=dict(_event_context.get() or {})
    context.update(text=text,uid=uid,name=conversation_name(data,uid))
    _event_context.set(context)
    if message['skip']:
        log('info','收到系统或暂不支持的消息，未触发回复',kind='received',text=text or '[暂不支持的消息类型]')
        return
    if not text and not message['images']: return
    if is_group:
        title=group_name(data)
        if title and title!=uid:
            learn_contact(uid,title)
        else:
            with data_lock:
                if uid not in contacts:
                    contacts[uid]=uid  # Keep the row selectable without inventing a title.
                    save_contacts()
    else:
        learn_contact(uid,nick)
    display=conversation_name(data,uid)
    prompt_text,prompt_who=text,display
    if is_group:
        member=nick
        match=re.match(r'^([^:\n]{1,24})[:：]\s*(.+)$',text,re.S)
        if match:
            member=member or match.group(1).strip()
            prompt_text=match.group(2)
        prompt_who=display+' 群里的 '+(member or '成员')
        log('info','[群] 群「'+display+'」· '+one_line(text),name=display,member=member)
    else:
        log('info','[私] '+display+' ('+uid+'): '+one_line(text))
    with data_lock:
        previous=[dict(row) for row in history.get(uid,[])]
        human_before=next((row for row in reversed(history.get(uid,[])) if row.get('source') in ('self_sync','manual')),None)
    # Capturing context is independent of sending. The model is still gated below.
    added=record_turn(uid,'user',text or '[图片]',source='incoming',message_id=str(data.get('message_id') or ''),images=message['images'])
    if added is False:return
    with state_lock:
        if not state['auto_reply']:
            log('muted','自动回复已关闭；已保存本会话上下文，未调用模型')
            return
        if not state['send_enabled'] or muted_left():
            log('muted','发送已关闭或处于静音，未调用模型')
            return
        allow=[item for item in state.get('allowlist',[]) if item]
        if not allow and not state.get('allow_all',False):
            log('skip','白名单为空，未调用模型')
            return
        if allow and not any(item in (sender,uid) for item in allow):
            log('skip','不在白名单，未调用模型')
            return
        if is_group and not state['reply_groups']:
            log('skip','群消息未启用自动回复')
            return
    if state.get('auto_skip',True) and message['kind']=='text' and not message['images'] and not message['links']:
        ok,why=need_reply(prompt_text)
        if not ok:
            log('skip',display+' 的消息无需回复（'+why+'）')
            return
        consec_short[uid]=consec_short.get(uid,0)+1 if len(prompt_text)<SHORT_MSG_LEN else 0
        maximum=int(state.get('max_consec',3))
        if maximum>0 and consec_short[uid]>maximum:
            log('skip',display+' 已到连续短消息回复上限')
            return
    try:
        account_guard(require_ready=True)
        generated=service.generate(uid,prompt_text,images=message['images'],links=message['links'],
                                   notices=message['notices'],is_group=is_group,who=prompt_who,history_rows=previous)
        account_guard(require_ready=True)
        reply=generated['reply']
    except Exception as error:
        log('error','Hermes 调用失败：'+str(error))
        return
    if not reply or reply.strip().upper()=='SKIP':
        log('skip','Hermes 判断无需回复')
        return
    with data_lock:
        human_now=next((row for row in reversed(history.get(uid,[])) if row.get('source') in ('self_sync','manual')),None)
        if human_now is not human_before:
            log('skip','生成期间本人已发言，取消过期 AI 回复')
            return
    with state_lock:
        if not state['send_enabled'] or not state['auto_reply'] or muted_left():
            log('muted','生成期间回复或发送已关闭／静音，取消发送')
            return
        model_text = reply
        reply = format_auto_reply(reply)
    log('reply','→ '+display+': '+one_line(reply),text=reply)
    try:
        used=dispatch_send(uid,contacts.get(uid) or '',reply,is_group,model_text=model_text,source='ai')
    except Exception as error:
        log('error','发送失败：'+str(error))


# ---------- OneBot webhook ----------
class Hook(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(n)
        if not verify(body, self.headers.get("X-Signature", "")):
            self.send_response(403); self.end_headers(); return
        try:
            data = json.loads(body)
        except Exception:
            self.send_response(400); self.end_headers(); return
        scope = account_manager.current(fresh=True) if account_manager is not None else None
        Thread(target=handle_incoming, args=(data,scope), daemon=True).start()
        self.send_response(200); self.end_headers()

    def log_message(self, *a): pass


# ---------- AI 会话配置与上下文服务（不改变自动回复/发送开关） ----------
_assistant_service = None
_assistant_lock = RLock()

def initialize_accounts(root=None, probe=None, migration_owner=None, allow_legacy_migration=True):
    """Explicit production bootstrap; imports/tests alone never migrate real data."""
    global account_manager
    from account_runtime import AccountManager, NativeAccountProbe, MappingProxy, ListProxy, SetProxy, PathProxy
    directory = Path(root or RUNTIME_DIR)
    approval = directory/'account-migration-approval.json'
    if allow_legacy_migration and migration_owner is None and approval.exists():
        record = json.loads(approval.read_text(encoding='utf-8'))
        if record.get('user_approved_legacy_binding'):
            migration_owner = record.get('owner_id')
    manager = AccountManager(directory, probe or NativeAccountProbe(directory), STATE_DEFAULTS, migration_owner=migration_owner)
    manager.bootstrap()
    for key in ('state','contacts','history','consec_short','model_runtime'):
        globals()[key] = MappingProxy(manager,key)
    globals()['logs'] = ListProxy(manager,'logs')
    globals()['SEEN'] = SetProxy(manager,'seen')
    globals()['HOME_DIR'] = PathProxy(manager)
    for key, filename in (('STATE_FILE','config.json'),('CONTACTS_FILE','contacts.json'),
                          ('HISTORY_FILE','history.json'),('LOG_FILE','bridge.log'),('EVENT_DB','events.db')):
        globals()[key] = PathProxy(manager,filename)
    account_manager = manager
    return manager


def account_guard(require_ready=False):
    if account_manager is not None:
        account_manager.assert_current(account_manager.scope(),fresh=True,require_ready=require_ready)


def _bound_account_call(function, scope, guard=True):
    manager = account_manager
    def call(*args,**kwargs):
        with manager.bind(scope):
            if guard:
                manager.assert_current(scope,fresh=True)
            result = function(*args,**kwargs)
            if guard:
                manager.assert_current(scope,fresh=True)
            return result
    return call


def get_assistant_service():
    global _assistant_service
    with _assistant_lock:
        if account_manager is not None:
            scope = account_manager.scope()
            workspace = scope.workspace
            cached = workspace.assistant_service
            if cached is None or getattr(cached,'account_token',None) != scope.token:
                from assistant_core import AssistantService
                namespace = dict(globals())
                for key in ('state','contacts','history','model_runtime'):
                    namespace[key] = getattr(workspace,key)
                namespace['HOME_DIR'] = workspace.home
                namespace['HISTORY_FILE'] = str(workspace.home/'history.json')
                for key in ('ask_hermes','format_auto_reply','record_turn','atomic_json'):
                    namespace[key] = _bound_account_call(globals()[key],scope)
                namespace['log'] = _bound_account_call(log,scope,guard=False)
                cached = AssistantService(workspace.home,namespace)
                cached.account_token = scope.token
                cached.account_id = workspace.account_id
                workspace.assistant_service = cached
            return cached
        if _assistant_service is None or str(_assistant_service.home) != str(Path(HOME_DIR)):
            from assistant_core import AssistantService
            _assistant_service = AssistantService(HOME_DIR, globals())
        return _assistant_service


# ---------- 管理面板（静态页面和 API 独立维护） ----------
from panel_server import create_panel
Panel = create_panel(globals())


def port_open(p):
    if PREVIEW_MODE:
        return False
    s = socket.socket(); s.settimeout(0.4)
    try:
        s.connect(("127.0.0.1", p)); return True
    except Exception:
        return False
    finally:
        s.close()


class LocalHTTPServer(ThreadingHTTPServer):
    """Loopback services must not depend on reverse DNS during startup."""
    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name = 'localhost'
        self.server_port = self.server_address[1]


def main(native=None):
    global PREVIEW_MODE, account_manager
    native = os.environ.get('WECHAT_MODE') == 'native' if native is None else native
    PREVIEW_MODE = not native
    if PREVIEW_MODE:
        # Empty account probe never inspects a running personal WeChat account.
        preview_home = Path(RUNTIME_DIR)/'.runtime'/'preview'
        initialize_accounts(root=preview_home, probe=lambda: {}, allow_legacy_migration=False)
        class PreviewPanel(Panel):
            def do_POST(self):
                self.close_connection = True
                self._j({'ok': False, 'error': '当前为只读预览，不连接微信或模型。安装依赖后使用 ./start.sh --native。'}, 403)
        with LocalHTTPServer(('127.0.0.1', WEB_PORT), PreviewPanel) as server:
            print(f'只读预览面板: http://127.0.0.1:{WEB_PORT}', flush=True)
            print('使用空白数据，不连接微信、不调用 Hermes、不启动消息 Webhook。', flush=True)
            server.serve_forever()
        return
    if not TOKEN:
        raise SystemExit("请显式设置 WECHAT_ONEBOT_TOKEN，未配置时不启动桥接")
    initialize_accounts()
    print(f"✅ 已启用按微信登录账号隔离的数据目录 {HOME_DIR}")
    allow_desc = state['allowlist'] or ('放行所有人' if state.get('allow_all') else '空，谁都不回')
    print(f"   白名单={allow_desc} 自动回复={state['auto_reply']} "
          f"静音={muted_desc()}")
    log("info", f"桥接启动（白名单={allow_desc}，{muted_desc()}）")
    Thread(target=lambda: LocalHTTPServer(("127.0.0.1", WEB_PORT), Panel).serve_forever(), daemon=True).start()
    print(f"📊 面板: http://127.0.0.1:{WEB_PORT}")
    print(f"🔗 接收: http://{ONEBOT_LISTEN[0]}:{ONEBOT_LISTEN[1]}/onebot")
    print(f"📝 日志: {LOG_FILE}")
    LocalHTTPServer(ONEBOT_LISTEN, Hook).serve_forever()


if __name__ == "__main__":
    main()
