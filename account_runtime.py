#!/usr/bin/env python3
"""账号隔离底层：原生账号探测、作用域绑定、按账号存储与旧数据迁移。

只做底层，不含任何 HTTP/OneBot/面板集成代码。集成方（wechat_hermes_bridge）在
``initialize_accounts`` 里替换全局变量为 proxy，并在每个请求/入向线程先 ``bind``。

对外接口（精确约定，勿改签名）::

    AccountManager(root, probe, defaults, migration_owner=None)
        .current(fresh=False) -> Scope
        .scope() -> Scope
        .bind(scope)  # contextmanager
        .assert_current(scope=None, fresh=True, require_ready=False)
        .snapshot(scope=None) -> dict
        .bootstrap() -> dict

    Scope: workspace, token, identity, ready, reason
    Workspace: account_id, home, state, contacts, history, logs, seen,
               consec_short, model_runtime, event_store, assistant_service

    NativeAccountProbe(root)                 # 生产用探测（lsof 只读进程路径）
    MappingProxy(manager, attr) / ListProxy(manager, attr) / SetProxy(manager, attr)
    PathProxy(manager, filename=None)

设计要点：
* 身份 = (账号ID, wechat_pid)；任何一个变化都算身份变化，包含 unknown 状态。
* token 带单调 epoch，A→B→A 之后旧 token 必然失效。
* 身份变化时，退出和进入的 workspace 都把 auto_reply/send_enabled 关掉并落盘；
  首次 bootstrap 迁移的那个账号保留原开关（首次绑定不关）。
* unknown 返回内存空 workspace，绝不露出上一个账号的资料。
* 所有落盘都是 atomic_json（fsync + 0600），目录 0700。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from collections.abc import MutableMapping, MutableSequence, MutableSet
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from threading import RLock

__all__ = [
    "AccountChanged", "BootstrapError", "Scope", "Workspace", "AccountManager",
    "NativeAccountProbe", "MappingProxy", "ListProxy", "SetProxy", "PathProxy",
    "DEFAULT_STATE", "DEFAULT_MODEL_RUNTIME", "PROBE_CACHE_TTL",
]

PROBE_CACHE_TTL = 1.0          # probe 结果最多缓存 1 秒；fresh=True 必须重新观察
SCHEMA = 0o600                 # 文件权限
DIR_MODE = 0o700               # 目录权限

DEFAULT_STATE = {
    "auto_reply": False,
    "reply_groups": False,
    "send_enabled": False,
    "allowlist": [],           # 配置缺失时谁都不回复
    "reply_prefix": "",
    "reply_suffix": "",
    "send_mode": "onebot",
    "auto_skip": True,
    "max_consec": 3,
    "allow_all": False,
    "muted_until": 0,
}
DEFAULT_MODEL_RUNTIME = {
    "status": "unknown", "detail": "尚未调用模型，不代表连接失败",
    "pending": 0, "last_call": None,
}
# 旧数据里属于某个账号的文件；uploads/ 是目录，events.db 走 sqlite backup。
LEGACY_FILES = ("config.json", "contacts.json", "history.json",
                "assistant_settings.json", "bridge.log")
LEGACY_DIRS = ("uploads",)
LEGACY_DB = "events.db"

_ENABLED_KEYS = ("auto_reply", "send_enabled")
_FOLDER_RE = re.compile(r"/xwechat_files/([^/]+)/db_storage/(?:message|contact|session)(?:/|$)")
_HEX_SUFFIX_RE = re.compile(r"[0-9a-fA-F]{4,16}")


class AccountChanged(ValueError):
    """当前原生账号与作用域不匹配（账号变了 / 账号为空）。"""


class BootstrapError(AccountChanged):
    """初始化迁移被拒绝（当前账号不是迁移归属账号等）。"""


# --------------------------------------------------------------------------- #
# 原子写盘 / 小工具
# --------------------------------------------------------------------------- #
def _chmod(path, mode):
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _mkdir(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    _chmod(path, DIR_MODE)
    return path


def atomic_json(path, value):
    """先写临时文件 + fsync，再 os.replace，最后 fsync 目录。文件 0600。"""
    path = Path(path)
    _mkdir(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=".acc-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        _chmod(temporary, SCHEMA)
        os.replace(temporary, path)
        try:
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, ValueError):
        return default


def account_dir_name(account_id):
    """账号目录名 = sha256(accountID)，不直接把 ID 写进路径。"""
    return hashlib.sha256(str(account_id).encode("utf-8")).hexdigest()


def _copy_sqlite(source, destination):
    """热库一致性复制：用 sqlite3 backup，不裸 copy。"""
    source_uri = f"file:{source}?mode=ro"
    src = sqlite3.connect(source_uri, uri=True, timeout=5)
    try:
        dst = sqlite3.connect(str(destination), timeout=5)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    _chmod(destination, SCHEMA)


# --------------------------------------------------------------------------- #
# Workspace：每个账号一套文件
# --------------------------------------------------------------------------- #
class Workspace:
    """单个账号的数据集合，全部从自己的文件加载。"""

    FILES = {
        "state": "config.json",
        "contacts": "contacts.json",
        "history": "history.json",
        "assistant_settings": "assistant_settings.json",
    }

    def __init__(self, home, account_id, defaults=None, ephemeral=False):
        self.account_id = str(account_id or "")
        self.home = Path(home)
        self.ephemeral = bool(ephemeral)
        self.lock = RLock()
        merged = dict(DEFAULT_STATE)
        if defaults:
            merged.update(defaults)
        # defaults 只是新账号的起点，绝不代理任何 Root 文件
        merged["auto_reply"] = False
        merged["send_enabled"] = False
        merged["allowlist"] = []
        self.defaults = merged

        self.state = {**merged, **self._load("state")}
        self.contacts = self._load("contacts")          # {wxid: 搜索名}
        self.history = self._load("history")            # {uid: [{role,text,t}]}
        self.assistant_settings = self._load("assistant_settings") or {}
        self.logs = []                                  # 内存滚动日志
        self.seen = set()                               # 已处理消息去重
        self.consec_short = {}                          # uid -> 连续短消息轮数
        self.model_runtime = dict(DEFAULT_MODEL_RUNTIME)
        self.event_store = None
        self.assistant_service = None
        if not self.ephemeral:
            _mkdir(self.home)

    # ---- 路径 ----
    @property
    def state_path(self):
        return self.home / self.FILES["state"]

    @property
    def contacts_path(self):
        return self.home / self.FILES["contacts"]

    @property
    def history_path(self):
        return self.home / self.FILES["history"]

    @property
    def settings_path(self):
        return self.home / self.FILES["assistant_settings"]

    @property
    def log_path(self):
        return self.home / "bridge.log"

    @property
    def db_path(self):
        return self.home / "events.db"

    @property
    def uploads_dir(self):
        return self.home / "uploads"

    def path(self, filename=None):
        return self.home if filename is None else self.home / filename

    # ---- 加载 / 落盘 ----
    def _load(self, key):
        if self.ephemeral:
            return {}
        value = _read_json(self.home / self.FILES[key], {})
        return value if isinstance(value, dict) else {}

    def save_state(self):
        if self.ephemeral:
            return
        with self.lock:
            atomic_json(self.state_path, self.state)

    def save_contacts(self):
        if self.ephemeral:
            return
        with self.lock:
            atomic_json(self.contacts_path, self.contacts)

    def save_history(self):
        if self.ephemeral:
            return
        with self.lock:
            atomic_json(self.history_path, self.history)

    def save_assistant_settings(self):
        if self.ephemeral:
            return
        with self.lock:
            atomic_json(self.settings_path, self.assistant_settings)

    def save(self, attr):
        {"state": self.save_state, "contacts": self.save_contacts,
         "history": self.save_history,
         "assistant_settings": self.save_assistant_settings}[attr]()

    # ---- 开关 ----
    def set_switches(self, auto_reply=False, send_enabled=False):
        """关闭/设置发送开关；变更才落盘。unknown 内存 workspace 不落盘。"""
        with self.lock:
            changed = False
            for key, value in zip(_ENABLED_KEYS, (auto_reply, send_enabled)):
                if bool(self.state.get(key)) != bool(value):
                    self.state[key] = bool(value)
                    changed = True
            if changed:
                self.save_state()
            return changed

    def disable_sending(self):
        return self.set_switches(False, False)

    @property
    def sending_enabled(self):
        return bool(self.state.get("auto_reply")) or bool(self.state.get("send_enabled"))

    # ---- 关联服务（惰性，由集成方或此处构造）----
    def open_event_store(self):
        """按账号建 events.db（可注入 factory 以复用 panel_backend.EventStore）。"""
        if self.event_store is not None or self.ephemeral:
            return self.event_store
        from panel_backend import EventStore  # 延迟导入，避免循环依赖
        _mkdir(self.home)
        self.event_store = EventStore(self.db_path)
        return self.event_store

    def attach_assistant_service(self, service):
        self.assistant_service = service
        return service


# --------------------------------------------------------------------------- #
# Scope
# --------------------------------------------------------------------------- #
class Scope:
    """一次"原生身份快照"对应的工作作用域。"""

    __slots__ = ("workspace", "token", "identity", "ready", "reason")

    def __init__(self, workspace, token, identity=None, ready=False, reason=""):
        self.workspace = workspace
        self.token = token
        self.identity = dict(identity or {})
        self.ready = bool(ready)
        self.reason = reason

    def __repr__(self):
        return (f"Scope(account_id={self.workspace.account_id!r}, ready={self.ready}, "
                f"reason={self.reason!r}, token={self.token!r})")

    @property
    def account_id(self):
        return self.workspace.account_id

    @property
    def verified(self):
        return bool(self.workspace.account_id)


# --------------------------------------------------------------------------- #
# 生产探测：lsof 只读进程路径
# --------------------------------------------------------------------------- #
class NativeAccountProbe:
    """用 lsof 读进程打开的文件路径识别当前登录微信；不读消息、不读密钥。

    * 微信 PID：``lsof -t -nP -iTCP:27042 -sTCP:LISTEN``
    * 账号 folder：``lsof -nP -p PID -Fn`` 里筛
      ``/xwechat_files/<folder>/db_storage/(message|contact|session)/``；
      必须唯一 folder 才识别。
    * 账号 ID：按上游 main.go 规则 ``folder.rsplit('_', 1)[0]``，
      且尾后缀必须是 4-16 位十六进制；不合规不猜测，返回 unknown（id 空）。
    * OneBot generation：58080 监听 PID + lstart，PID→lstart 缓存避免反复 ps。
    """

    WECHAT_PORT = 27042
    ONEBOT_PORT = 58080

    def __init__(self, root=None, runner=None, cache_ttl=PROBE_CACHE_TTL):
        self.root = root
        self._runner = runner or self._run
        self.cache_ttl = cache_ttl
        self._lstart_cache = {}
        self._cache = None
        self._lock = RLock()
        self._lsof = "/usr/sbin/lsof" if os.path.exists("/usr/sbin/lsof") else "lsof"

    # ---- 外部可用 ----
    def probe(self):
        """返回 {id, wechat_pid, onebot_generation, folder}；失败时 id 为空串。"""
        wechat_pid = self.listener_pid(self.WECHAT_PORT) or 0
        folder, account_id = self._open_account(wechat_pid)
        return {
            "id": account_id,
            "wechat_pid": int(wechat_pid or 0),
            "onebot_generation": self.onebot_generation(),
            "folder": folder,
        }

    __call__ = probe

    # ---- 内部 ----
    @staticmethod
    def _run(argv):
        try:
            env = {**os.environ, "LC_ALL": "C"}
            done = subprocess.run(argv, capture_output=True, text=True, timeout=2.0, env=env)
            return done.returncode, done.stdout or ""
        except (OSError, subprocess.SubprocessError):
            return 1, ""

    def listener_pid(self, port):
        code, out = self._runner([self._lsof, "-t", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"])
        for line in out.splitlines():
            line = line.strip()
            if line.isdigit():
                return int(line)
        return None

    def _open_account(self, pid):
        if not pid:
            return "", ""
        code, out = self._runner([self._lsof, "-nP", "-p", str(pid), "-Fn"])
        folders = set()
        for line in out.splitlines():
            if not line.startswith("n"):
                continue
            match = _FOLDER_RE.search(line[1:])
            if match:
                folders.add(match.group(1))
        if len(folders) != 1:
            return "", ""            # 0 个或多个 folder 都不认，返回 unknown
        folder = folders.pop()
        return folder, self.account_id_from_folder(folder)

    @staticmethod
    def account_id_from_folder(folder):
        """上游 main.go 规则：去掉最后一段下划线后缀；后缀须为 4-16 位十六进制。"""
        folder = str(folder or "").strip()
        if not folder or "_" not in folder:
            return ""
        head, suffix = folder.rsplit("_", 1)
        if not head or not _HEX_SUFFIX_RE.fullmatch(suffix):
            return ""                # 其他格式不盲修
        return head

    def onebot_generation(self):
        pid = self.listener_pid(self.ONEBOT_PORT)
        if not pid:
            return ""
        code, connections = self._runner([self._lsof,'-a','-p',str(pid),'-iTCP:27042','-sTCP:ESTABLISHED','-Fp'])
        if code != 0 or not connections.strip():
            return ''  # An old HTTP listener is not a live connection to this WeChat.
        return f"{pid}:{self._lstart(pid)}"

    def _lstart(self, pid):
        with self._lock:
            cached = self._lstart_cache.get(pid)
        if cached is not None:
            return cached
        code, out = self._runner(["/bin/ps", "-p", str(pid), "-o", "lstart="])
        value = (out or "").strip()
        # 只缓存非空时间，避免把"还没起来"的瞬间永久缓存
        if value:
            with self._lock:
                self._lstart_cache = {pid: value}   # 只保留当前 PID，防止缓存无限增长
        return value or "0"


# --------------------------------------------------------------------------- #
# AccountManager
# --------------------------------------------------------------------------- #
class AccountManager:
    def __init__(self, root, probe, defaults=None, migration_owner=None, cache_ttl=PROBE_CACHE_TTL):
        self.root = Path(root).expanduser()
        self.probe = probe
        self.migration_owner = str(migration_owner) if migration_owner else None
        self.cache_ttl = cache_ttl
        self.accounts_dir = self.root / "accounts"
        self.registry_path = self.accounts_dir / "registry.json"
        self.runtime_path = self.accounts_dir / "runtime.json"
        self.defaults = dict(DEFAULT_STATE)
        if defaults:
            self.defaults.update(defaults)
        # 新账号必须默认关闭、白名单为空，不受调用方 defaults 影响
        self.defaults["auto_reply"] = False
        self.defaults["send_enabled"] = False
        self.defaults["allowlist"] = []
        self._lock = RLock()                       # 一个 RLock 串起"探测+落地"，防旧结果覆盖新结果
        self._context = ContextVar("wechat_account_scope", default=None)
        self._cache = None                         # (monotonic, probe dict)
        self._identity = None                      # (account_id, wechat_pid)
        self._epoch = 0
        self._scopes = {}                          # (account_id, pid) -> Scope
        self._workspaces = {}                      # account_id -> Workspace
        self._unknown_ws = None
        self._runtime = None
        self._nonce = os.urandom(24).hex()
        _mkdir(self.accounts_dir)

    # ---------------- 探测 ---------------- #
    def _observe(self, fresh=False):
        now = time.monotonic()
        cached = self._cache
        if not fresh and cached is not None and now - cached[0] < self.cache_ttl:
            return cached[1]
        raw = self.probe() or {}
        data = {
            "id": str(raw.get("id") or ""),
            "wechat_pid": int(raw.get("wechat_pid") or 0),
            "onebot_generation": str(raw.get("onebot_generation") or ""),
            "folder": str(raw.get("folder") or ""),
        }
        self._cache = (now, data)
        return data

    # ---------------- 账号目录 / workspace ---------------- #
    def account_home(self, account_id):
        return self.accounts_dir / account_dir_name(account_id)

    def _workspace(self, account_id, reset=False):
        if not account_id:
            # unknown：内存空 workspace，绝不落盘、绝不露出上一个账号的资料
            if reset or self._unknown_ws is None:
                self._unknown_ws = Workspace(self.accounts_dir / "_unknown", "",
                                             self.defaults, ephemeral=True)
            return self._unknown_ws
        ws = self._workspaces.get(account_id)
        if ws is None:
            ws = Workspace(self.account_home(account_id), account_id, self.defaults)
            self._workspaces[account_id] = ws
        return ws

    def defaults_for_new(self):
        return dict(self.defaults)

    # ---------------- 解析当前账号 ---------------- #
    def _resolve(self, fresh=False):
        with self._lock:
            data = self._observe(fresh)
            return self._apply(data)

    def _apply(self, data):
        """调用方必须持有 self._lock。"""
        account_id = data["id"]
        pid = data["wechat_pid"]
        generation = data["onebot_generation"]
        key = (account_id, pid)
        first = self._identity is None
        changed = key != self._identity
        previous = self._load_runtime().get('last_identity')
        restart_changed = first and isinstance(previous,list) and tuple(previous) != key

        if changed:
            if not first:
                self._disable(self._workspaces.get(self._identity[0]))
            self._identity = key
            self._epoch += 1

        workspace = self._workspace(account_id, reset=changed)
        if changed and (not first or restart_changed):
            self._disable(workspace)       # 新进入的账号也不自动恢复发送
        if changed:
            self._load_runtime()['last_identity'] = list(key)
            self._save_runtime()

        ready, reason = self._readiness(account_id, generation)
        token = hashlib.sha256(
            f"{self._nonce}\x00{account_id}\x00{pid}\x00{self._epoch}".encode("utf-8")).hexdigest()[:32]
        scope = Scope(workspace=workspace, token=token,
                      identity={"id": account_id, "wechat_pid": pid,
                                "onebot_generation": generation,
                                "folder": data.get("folder", "")},
                      ready=ready, reason=reason)
        self._scopes[key] = scope
        return scope

    def _disable(self, workspace):
        if workspace is None:
            return
        workspace.disable_sending()

    # ---------------- OneBot generation ↔ 账号 ---------------- #
    def _load_runtime(self):
        if self._runtime is None:
            raw = _read_json(self.runtime_path, {})
            self._runtime = raw if isinstance(raw, dict) else {}
            if not isinstance(self._runtime.get("generation"), dict):
                self._runtime["generation"] = {}
        return self._runtime

    def _save_runtime(self):
        atomic_json(self.runtime_path, self._load_runtime())

    def _bind_generation(self, generation, account_id, force=False):
        runtime = self._load_runtime()
        table = runtime["generation"]
        if not generation:
            return None
        if table.get(generation) == account_id:
            return account_id
        if generation not in table or force:
            table[generation] = account_id
            self._save_runtime()
        return table.get(generation)

    def _readiness(self, account_id, generation):
        if not account_id:
            return False, "未识别到已登录微信账号"
        if not generation:
            return False, "OneBot 会话未运行"
        owner = self._bind_generation(generation, account_id)
        if owner == account_id:
            return True, "OneBot 会话已绑定当前账号"
        return False, "OneBot 会话仍绑定其他账号，请重新连接 OneBot"

    # ---------------- 对外 API ---------------- #
    def current(self, fresh=False):
        return self._resolve(fresh=fresh)

    def scope(self):
        bound = self._context.get()
        if bound is not None:
            return bound
        return self.current()

    @contextmanager
    def bind(self, scope):
        """绑定作用域（允许绑定旧 scope，便于错误日志写回原账号；不在这里拒绝）。"""
        token = self._context.set(scope)
        try:
            yield scope
        finally:
            self._context.reset(token)

    def assert_current(self, scope=None, fresh=True, require_ready=False):
        target = scope if scope is not None else self.scope()
        if not target.workspace.account_id:
            raise AccountChanged("当前微信账号未识别，拒绝继续")
        now = self.current(fresh=fresh)
        if now.token != target.token:
            raise AccountChanged(
                f"原生账号已变化（{target.workspace.account_id or 'unknown'} → "
                f"{now.workspace.account_id or 'unknown'}），旧作用域令牌失效")
        if require_ready:
            if not now.ready:
                raise AccountChanged(f"收发服务不可用：{now.reason}")
            ok, why = self._service_bound(now)
            if not ok:
                raise AccountChanged(why)
        return now

    @staticmethod
    def _service_bound(scope):
        service = scope.workspace.assistant_service
        if service is None:
            return True, ""  # AI 配置尚未初始化，不代表 OneBot 未连接。
        checker = getattr(service, "is_bound", None)
        if callable(checker):
            try:
                bound = checker(scope.workspace.account_id)
            except TypeError:
                bound = checker()
            return (bool(bound), "" if bound else "收发服务未绑定当前账号")
        declared = getattr(service, "account_id", None) or getattr(service, "bound_account", None)
        if declared is None:
            return True, ""            # 服务已附着但未声明账号，交由集成层判定
        ok = str(declared) == scope.workspace.account_id
        return ok, ("" if ok else "收发服务未绑定当前账号")

    def snapshot(self, scope=None):
        bound = scope if scope is not None else self.scope()
        workspace = bound.workspace
        account_id = workspace.account_id
        name = ""
        try:
            name = workspace.contacts.get(account_id) or ""
        except Exception:
            name = ""
        if bound.ready:
            status = "ready"
        elif account_id:
            status = "detected"
        else:
            status = "unknown"
        return {
            "id": account_id,
            "name": name or account_id,
            "token": bound.token,
            "ready": bool(bound.ready),
            "verified": bool(account_id),
            "status": status,
            "detail": bound.reason,
        }

    # ---------------- 初始化 / 迁移 ---------------- #
    def bootstrap(self):
        """显式做一次初始化/迁移。只有 current.id == migration_owner 且
        registry.json 还没声明 legacy_owner 时才迁移；账号不符直接拒绝启动。"""
        with self._lock:
            data = self._observe(fresh=True)
            account_id = data["id"]
            generation = data["onebot_generation"]
            result = {"migrated": False, "migration_owner": self.migration_owner,
                      "account_id": account_id, "legacy_owner": None,
                      "copied": [], "skipped": [], "reason": ""}

            registry = self._load_registry()
            if registry.get('legacy_owner'):
                result['legacy_owner'] = registry['legacy_owner']
                result['reason'] = '旧数据已归属，按当前账号正常启动，不再迁移'
            elif self.migration_owner is None:
                result["reason"] = "未指定迁移归属账号，跳过迁移"
            elif not account_id or account_id != self.migration_owner:
                raise BootstrapError(
                    f"当前登录账号不是迁移归属账号（期望 {self.migration_owner}），拒绝启动")
            else:
                registry = self._load_registry()
                if registry.get("legacy_owner"):
                    result["legacy_owner"] = registry["legacy_owner"]
                    result["reason"] = "registry.json 已声明 legacy_owner，重启不再迁移"
                else:
                    result.update(self._migrate(account_id, data))
                    # 初次迁移：把当前 generation 绑给迁移账号
                    self._bind_generation(generation, account_id, force=True)

            self._apply(data)              # 首次绑定不关开关，迁移账号保留原开关
            result["scope"] = self._scope_summary()
            return result

    def _scope_summary(self):
        scope = self._scopes.get(self._identity)
        if scope is None:
            return {}
        return {"id": scope.workspace.account_id, "token": scope.token,
                "ready": scope.ready, "reason": scope.reason}

    def _load_registry(self):
        raw = _read_json(self.registry_path, {})
        return raw if isinstance(raw, dict) else {}

    def _migrate(self, account_id, data):
        home = self.account_home(account_id)
        _mkdir(self.accounts_dir)
        _mkdir(home)
        copied, skipped = [], []
        before = (data["id"], data["wechat_pid"])

        for name in LEGACY_FILES:
            source = self.root / name
            if not source.exists():
                continue
            target = home / name
            if target.exists():
                skipped.append(name)       # 禁止覆盖已有账号数据
                continue
            shutil.copyfile(source, target)
            _chmod(target, SCHEMA)
            copied.append(name)

        for name in LEGACY_DIRS:
            source = self.root / name
            if not source.is_dir():
                continue
            target = home / name
            if target.exists():
                skipped.append(name + "/")
                continue
            shutil.copytree(source, target)
            _chmod(target, DIR_MODE)
            for base, dirs, files in os.walk(target):
                for directory in dirs:
                    _chmod(os.path.join(base, directory), DIR_MODE)
                for filename in files:
                    _chmod(os.path.join(base, filename), SCHEMA)
            copied.append(name + "/")

        source_db = self.root / LEGACY_DB
        if source_db.exists():
            target_db = home / LEGACY_DB
            if target_db.exists():
                skipped.append(LEGACY_DB)
            else:
                _copy_sqlite(source_db, target_db)
                copied.append(LEGACY_DB)

        # 复制前后确认原生账号没变，避免迁移到一半换号串数据
        after_data = self._observe(fresh=True)
        if (after_data["id"], after_data["wechat_pid"]) != before:
            raise BootstrapError("迁移过程中微信账号发生变化，已中止（registry 未写入，可重试）")

        registry = {"legacy_owner": account_id,
                    "migrated_at": int(time.time()),
                    "files": copied, "skipped": skipped}
        atomic_json(self.registry_path, registry)
        return {"migrated": True, "legacy_owner": account_id,
                "copied": copied, "skipped": skipped,
                "reason": "旧数据已绑定到当前登录账号，根目录原文件保留"}


# --------------------------------------------------------------------------- #
# 全局变量代理：一律解析 manager.scope().workspace.<attr>
# --------------------------------------------------------------------------- #
class _ProxyBase:
    def __init__(self, manager, attr):
        self.manager = manager
        self.attr = attr

    def _target(self):
        return getattr(self.manager.scope().workspace, self.attr)

    def __repr__(self):
        return f"{type(self).__name__}({self.attr!r})->{dict(self) if isinstance(self, MutableMapping) else self._target()!r}"


class MappingProxy(_ProxyBase, MutableMapping):
    def __getitem__(self, key):
        return self._target()[key]

    def __setitem__(self, key, value):
        self._target()[key] = value

    def __delitem__(self, key):
        del self._target()[key]

    def __iter__(self):
        return iter(self._target())

    def __len__(self):
        return len(self._target())

    def __contains__(self, key):
        return key in self._target()

    def copy(self):
        return dict(self._target())

    @classmethod
    def _from_iterable(cls, iterable):
        return dict(iterable)


class ListProxy(_ProxyBase, MutableSequence):
    def __getitem__(self, index):
        return self._target()[index]

    def __setitem__(self, index, value):
        self._target()[index] = value

    def __delitem__(self, index):
        del self._target()[index]

    def __len__(self):
        return len(self._target())

    def insert(self, index, value):
        self._target().insert(index, value)

    @classmethod
    def _from_iterable(cls, iterable):
        return list(iterable)


class SetProxy(_ProxyBase, MutableSet):
    def __contains__(self, value):
        return value in self._target()

    def __iter__(self):
        return iter(self._target())

    def __len__(self):
        return len(self._target())

    def add(self, value):
        self._target().add(value)

    def discard(self, value):
        self._target().discard(value)

    @classmethod
    def _from_iterable(cls, iterable):
        return set(iterable)


class PathProxy:
    """动态绑定 workspace.home/filename 的路径；支持 os.fspath/str/+。"""

    def __init__(self, manager, filename=None):
        self.manager = manager
        self.filename = filename

    @property
    def base(self):
        return self.manager.scope().workspace.home

    def _path(self):
        return self.base if self.filename is None else Path(self.base) / self.filename

    def __fspath__(self):
        return str(self._path())

    def __str__(self):
        return str(self._path())

    def __repr__(self):
        return f"PathProxy({str(self)!r})"

    def __add__(self, other):
        return str(self) + (other if isinstance(other, str) else os.fspath(other))

    def __radd__(self, other):
        return (other if isinstance(other, str) else os.fspath(other)) + str(self)

    def __truediv__(self, other):
        return Path(self._path()) / other

    @property
    def parent(self):
        return self._path().parent

    @property
    def name(self):
        return self._path().name

    def exists(self):
        return self._path().exists()

    def is_dir(self):
        return self._path().is_dir()

    def stat(self):
        return self._path().stat()

    def open(self, *args, **kwargs):
        return self._path().open(*args, **kwargs)

    def __eq__(self, other):
        try:
            return os.fspath(self) == os.fspath(other)
        except TypeError:
            return NotImplemented

    def __hash__(self):
        return hash(str(self))
