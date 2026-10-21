"""Local control-panel storage and validation. No WeChat sending side effects."""
import os
from pathlib import Path
import re
import sqlite3
import time
from threading import RLock

KINDS = {'received', 'reply', 'sent', 'skip', 'muted', 'error', 'info'}
BOOL_KEYS = {'auto_reply', 'reply_groups', 'send_enabled', 'auto_skip', 'allow_all'}


def validate_settings(patch):
    if not isinstance(patch, dict):
        raise ValueError('配置必须是 JSON 对象')
    unknown = set(patch) - BOOL_KEYS - {'allowlist', 'send_mode', 'max_consec', 'reply_prefix', 'reply_suffix'}
    if unknown:
        raise ValueError('未知配置项：' + ', '.join(sorted(unknown)))
    result = {}
    for key, value in patch.items():
        if key in BOOL_KEYS:
            if type(value) is not bool:
                raise ValueError(key + ' 必须是布尔值')
        elif key == 'max_consec':
            if type(value) is not int or not 0 <= value <= 20:
                raise ValueError(key + ' 必须是 0 到 20 的整数')
        elif key in ('reply_prefix', 'reply_suffix'):
            if not isinstance(value, str) or len(value) > 500:
                raise ValueError('回复前缀和后缀必须是文本，每项最多 500 字')
        elif key == 'send_mode':
            if value not in ('onebot', 'clipboard', 'auto'):
                raise ValueError('未知发送模式')
        elif key == 'allowlist':
            if not isinstance(value, list) or len(value) > 500 or any(not isinstance(item, str) or len(item) > 200 for item in value):
                raise ValueError('白名单必须是字符串列表，最多 500 项，每项不超过 200 字')
            value = list(dict.fromkeys(item.strip() for item in value if item.strip()))
        result[key] = value
    return result



class EventStore:
    """Bounded, persistent activity log; connections are never shared across threads."""
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = RLock()
        with self._connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
                    lvl TEXT NOT NULL, kind TEXT NOT NULL, msg TEXT NOT NULL,
                    scope TEXT NOT NULL DEFAULT 'system', uid TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL DEFAULT '', text TEXT NOT NULL DEFAULT '',
                    member TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS events_kind_ts ON events(kind, ts);
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            ''')
        os.chmod(self.path, 0o600)

    def _connect(self):
        db = sqlite3.connect(str(self.path), timeout=5)
        db.row_factory = sqlite3.Row
        return db

    def append(self, level, msg, **fields):
        kind = fields.get('kind') or ('received' if str(msg).startswith(('[私]', '[群]')) else level)
        if kind not in KINDS:
            kind = 'info'
        ts = float(fields.get('ts') or time.time())
        scope = fields.get('scope', 'system')
        if scope not in ('private', 'group', 'system'):
            scope = 'system'
        values = (ts, str(level), kind, str(msg)[:12000], scope,
                  str(fields.get('uid', ''))[:200], str(fields.get('name', ''))[:200],
                  str(fields.get('text', ''))[:12000], str(fields.get('member', ''))[:200])
        with self.lock, self._connect() as db:
            result = db.execute('INSERT INTO events(ts,lvl,kind,msg,scope,uid,name,text,member) VALUES(?,?,?,?,?,?,?,?,?)', values)
            event_id = result.lastrowid
            if event_id % 100 == 0:
                db.execute('DELETE FROM events WHERE id <= ?', (event_id-20000,))
        return event_id

    @staticmethod
    def _event(row):
        event = dict(row)
        event['t'] = time.strftime('%H:%M:%S', time.localtime(event['ts']))
        event['date'] = time.strftime('%Y-%m-%d', time.localtime(event['ts']))
        return event

    def query(self, kind='all', scope='all', q='', limit=50, before=None):
        if kind != 'all' and kind not in KINDS:
            raise ValueError('未知日志类型')
        if scope not in ('all', 'private', 'group', 'system'):
            raise ValueError('未知会话类型')
        limit = max(1, min(200, int(limit)))
        clauses, params = [], []
        if kind != 'all':
            clauses.append('kind=?'); params.append(kind)
        if scope != 'all':
            clauses.append('scope=?'); params.append(scope)
        if q:
            pattern = '%' + str(q)[:200].replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
            clauses.append("(msg LIKE ? ESCAPE '\\' OR text LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\' OR uid LIKE ? ESCAPE '\\')")
            params.extend([pattern]*4)
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        with self._connect() as db:
            total = db.execute('SELECT count(*) FROM events' + where, params).fetchone()[0]
            if before is not None:
                cursor_clause = ' AND id<?' if where else ' WHERE id<?'
                where += cursor_clause
                params.append(int(before))
            rows = db.execute('SELECT * FROM events' + where + ' ORDER BY id DESC LIMIT ?', params + [limit+1]).fetchall()
        has_more = len(rows) > limit
        items = [self._event(row) for row in rows[:limit]]
        return {'items': items, 'total': total, 'has_more': has_more,
                'next_before': items[-1]['id'] if has_more and items else None}

    def import_legacy(self, path):
        """One-time bounded migration; old receive summaries remain explicitly summaries."""
        with self.lock:
            with self._connect() as db:
                if db.execute("SELECT value FROM meta WHERE key='legacy_imported'").fetchone():
                    return
            records = []
            path = Path(path)
            if path.exists():
                with path.open('rb') as stream:
                    stream.seek(max(0, path.stat().st_size - 1000000))
                    lines = stream.read().decode('utf-8', errors='replace').splitlines()
                pattern = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[([a-z]+)\] (.*)$')
                for line in lines:
                    match = pattern.match(line)
                    if match:
                        stamp, level, message = match.groups()
                        try:
                            ts = time.mktime(time.strptime(stamp, '%Y-%m-%d %H:%M:%S'))
                        except ValueError:
                            continue
                        records.append([level, message, ts])
                    elif records and len(records[-1][1]) < 12000:
                        records[-1][1] += '\n' + line
                for level, message, ts in records[-5000:]:
                    fields = {'ts': ts}
                    private = re.match(r'^\[私\] (.*?) \(([^()]+)\): (.*)', message, re.S)
                    group = re.match(r'^\[群\] 群「(.*?)」· (.*)', message, re.S)
                    if private:
                        fields.update(scope='private', name=private[1], uid=private[2], text=private[3])
                    elif group:
                        fields.update(scope='group', name=group[1], text=group[2])
                    self.append(level, message, **fields)
            with self._connect() as db:
                db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('legacy_imported','1')")

    def stats(self):
        now = time.localtime()
        midnight = time.mktime((now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, 0, 0, -1))
        with self._connect() as db:
            counts = dict(db.execute('SELECT kind,count(*) FROM events WHERE ts>=? GROUP BY kind', (midnight,)).fetchall())
            total = db.execute('SELECT count(*) FROM events').fetchone()[0]
            latest = dict(db.execute("SELECT kind,max(ts) FROM events WHERE kind IN ('received','sent') GROUP BY kind").fetchall())
        return {'today_received': counts.get('received', 0), 'today_sent': counts.get('sent', 0),
                'today_errors': counts.get('error', 0), 'total_events': total,
                'last_received': latest.get('received'), 'last_sent': latest.get('sent')}
