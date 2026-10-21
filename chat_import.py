#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""聊天记录导入模块（独立、纯标准库）。

设计目标
--------
把用户导出的聊天记录（JSON / TXT / CSV）解析成 hermes 的 history 行格式
``{"role": "user"|"assistant", "text": str, "t": int}``，并按联系人上下文
安全、稳定地合并进历史（去重 + 限量裁剪），同时给出可预览的报告。

约定（与项目 history.json 一致）
--------------------------------
* ``user``      = 对方（联系人）说的话
* ``assistant`` = 本人（我方 / hermes 发送方）说的话

因此：``is_sender == 1`` 或 TXT 中 ``self_name`` 匹配到的说话人 → ``assistant``；
``is_sender == 0`` 或对方说话人 → ``user``。

硬性约束
--------
* 单次导入内容 ≤ 5 MiB（UTF-8 字节）、≤ 5000 条、单条正文 ≤ 12000 字，超限 ``ValueError``（中文提示）。
* 角色只按明确字段（``role`` / ``is_sender`` / 明确说话人）映射，**绝不猜测**。
* 时间：整数/浮点秒；≥1e11 视为毫秒并换算为秒；ISO 字符串（带 offset 按 offset，
  无 offset 视为本机本地时间）；其他字符串视为无效 → ``t = 0`` 并给出告警。
  统一为整数秒。
* 无网络、无模型、无发送 API；不读取任何真实数据文件；不打印正文（含告警与报告）。

去重与合并
----------
* 每条消息带稳定 ``import_key`` = ``sha256(source_hash + 源位置序号)``（**不含 uid**，
  因为 history 按 uid 独立合并）。同一文件重复导入 → 全部命中 ``import_key`` 去重。
* 第二层去重：以 ``(role, text, t)`` 多重集（``Counter``）比对，避免重复导入与实际
  聊天完全一致的内容；按计数匹配，不会误删同文件中有意连续出现的两条相同无时间消息。
* 排序：``t = 0``（未知时间）的记录保持原顺序放在最前，其余按 ``t`` 稳定升序；
  超出 ``limit`` 时裁剪最旧的记录（含未知时间记录）。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections import Counter
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    'parse_chat',
    'merge_history',
    'preview_report',
    'render_report',
    'parse_time',
    'sniff_format',
    'MAX_BYTES',
    'MAX_MESSAGES',
    'MAX_TEXT_CHARS',
    'DEFAULT_HISTORY_LIMIT',
]

# ---------------------------------------------------------------- 常量

MAX_BYTES = 5 * 1024 * 1024        # 5 MiB（UTF-8 字节）
MAX_MESSAGES = 5000                # 单次导入最大条数
MAX_TEXT_CHARS = 12000             # 单条正文最大字符数
DEFAULT_HISTORY_LIMIT = 5000       # 每个联系人历史存储上限

ROLE_USER = 'user'
ROLE_ASSISTANT = 'assistant'
VALID_ROLES = (ROLE_USER, ROLE_ASSISTANT)

# TXT 中不需要额外配置即可识别的通用说话人（本人 / 对方）
SELF_ALIASES = frozenset(['我', '本人', '自己', 'me', 'myself', 'self'])
PEER_ALIASES = frozenset(['对方', '对面', '你', 'you', 'ta'])

FORMATS = ('auto', 'json', 'txt', 'csv')

_SENDER_TRUE = frozenset([1, '1', 'true', 'yes', 'y', 't', '是', '真', '本人', 'self', 'me'])
_SENDER_FALSE = frozenset([0, '0', 'false', 'no', 'n', 'f', '否', '假', '对方'])

_TIME_KEYS = ('t', 'timestamp', 'create_time')
_TEXT_KEYS = ('text', 'content', 'strContent')

_ISO_FORMATS = (
    '%Y-%m-%d %H:%M:%S',
    '%Y-%m-%dT%H:%M:%S',
    '%Y-%m-%d %H:%M',
    '%Y-%m-%dT%H:%M',
    '%Y/%m/%d %H:%M:%S',
    '%Y/%m/%d %H:%M',
    '%Y-%m-%d',
    '%Y/%m/%d',
    '%Y%m%d%H%M%S',
)

_MS_THRESHOLD = 1e11               # ≥ 1e11 视为毫秒时间戳

# ---------------------------------------------------------------- 异常/工具


def _fail(message: str) -> None:
    raise ValueError(message)


def _clip(value: str, limit: int = 20) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + '…'


# ---------------------------------------------------------------- 时间解析


def _parse_iso(value: str) -> Optional[datetime]:
    text = value.strip()
    if text.endswith('Z') or text.endswith('z'):
        text = text[:-1] + '+00:00'
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    for pattern in _ISO_FORMATS:
        try:
            return datetime.strptime(text, pattern)
        except ValueError:
            continue
    return None


def parse_time(value: Any) -> Tuple[int, Optional[str]]:
    """把时间字段转成整数秒。

    返回 ``(t, warning)``，``t = 0`` 表示无时间/无法识别。
    只接受数字、纯数字字符串与 ISO 时间字符串；其他字符串按无效处理（不猜测时区/相对时间）。
    """
    if value is None:
        return 0, None
    if isinstance(value, bool):
        return 0, '时间字段是布尔值，无法解析，已按无时间（0）处理'
    if isinstance(value, (int, float)):
        number = float(value)
        if number != number or number in (float('inf'), float('-inf')):
            return 0, '时间字段不是有限数字，已按无时间（0）处理'
        if number <= 0:
            return 0, None
        if number >= _MS_THRESHOLD:
            number = number / 1000.0
        return int(round(number)), None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0, None
        if text.isdigit():
            return parse_time(float(text))
        moment = _parse_iso(text)
        if moment is None:
            return 0, '时间字段「%s」无法解析，已按无时间（0）处理' % _clip(text)
        # 带 offset 的时间按 offset 解析；无 offset 视为本机本地时间（timestamp() 即本地时区）。
        return int(round(moment.timestamp())), None
    return 0, '时间字段类型不支持，已按无时间（0）处理'


def _entry_time(entry: Dict[str, Any]) -> Tuple[int, Optional[str]]:
    for key in _TIME_KEYS:
        if key in entry:
            return parse_time(entry.get(key))
    return 0, None


# ---------------------------------------------------------------- 角色映射


def _map_is_sender(value: Any) -> Optional[str]:
    if isinstance(value, (bool, int, float)):
        if value in _SENDER_TRUE:
            return ROLE_ASSISTANT
        if value in _SENDER_FALSE:
            return ROLE_USER
        return None
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _SENDER_TRUE:
            return ROLE_ASSISTANT
        if text in _SENDER_FALSE:
            return ROLE_USER
    return None


def _map_role(entry: Dict[str, Any], position: int) -> Tuple[Optional[str], Optional[str]]:
    """只在明确给出 ``role`` 或 ``is_sender`` 时映射角色，否则跳过并告警（不猜测）。"""
    role = entry.get('role')
    if isinstance(role, str) and role.strip():
        normalized = role.strip().lower()
        if normalized in VALID_ROLES:
            return normalized, None
        return None, '第 %d 条记录 role「%s」不是 user/assistant，已跳过' % (position, _clip(role, 16))
    if 'is_sender' in entry:
        mapped = _map_is_sender(entry.get('is_sender'))
        if mapped is not None:
            return mapped, None
        return None, '第 %d 条记录 is_sender 取值「%s」无法识别，已跳过' % (
            position, _clip(entry.get('is_sender'), 16))
    return None, '第 %d 条记录缺少明确角色（role/is_sender），已跳过（不猜测角色）' % position


def _extract_text(entry: Dict[str, Any]) -> Optional[str]:
    for key in _TEXT_KEYS:
        value = entry.get(key)
        if isinstance(value, str):
            return value
    return None


# ---------------------------------------------------------------- 通用条目归一化


def _normalize_entries(entries: Sequence[Any]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """把 JSON/CSV 条目归一化为内部消息行（含 _index）。"""
    messages: List[Dict[str, Any]] = []
    warnings: List[str] = []
    for offset, entry in enumerate(entries):
        position = offset + 1
        if not isinstance(entry, dict):
            warnings.append('第 %d 条记录不是对象，已跳过' % position)
            continue
        role, warning = _map_role(entry, position)
        if role is None:
            if warning:
                warnings.append(warning)
            continue
        text = _extract_text(entry)
        if text is None:
            warnings.append('第 %d 条记录缺少 text/content 正文字段，已跳过' % position)
            continue
        if not text.strip():
            warnings.append('第 %d 条消息正文为空，已跳过' % position)
            continue
        moment, time_warning = _entry_time(entry)
        if time_warning:
            warnings.append('第 %d 条消息：%s' % (position, time_warning))
        messages.append({'role': role, 'text': text, 't': moment, '_index': position})
    return messages, warnings


# ---------------------------------------------------------------- JSON


def _load_json_entries(text: str) -> List[Any]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        _fail('JSON 解析失败（第 %d 行第 %d 列：%s），请确认导出文件完整' % (exc.lineno, exc.colno, exc.msg))
    if isinstance(data, dict):
        if 'messages' not in data:
            _fail('JSON 对象缺少 messages 数组，仅支持顶层数组或 {"messages": [...]}')
        entries = data['messages']
    elif isinstance(data, list):
        entries = data
    else:
        _fail('JSON 顶层必须是数组或 {"messages": [...]} 对象')
    if not isinstance(entries, list):
        _fail('JSON 的 messages 字段必须是数组')
    return entries


# ---------------------------------------------------------------- CSV


def _csv_header_ok(line: str) -> bool:
    tokens = [token.strip().lower() for token in line.split(',')]
    if len(tokens) < 2:
        return False
    has_role = any(token in ('role', 'is_sender') for token in tokens)
    has_text = any(token in ('text', 'content', 'strcontent') for token in tokens)
    return has_role and has_text


def _parse_csv(text: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    buffer = io.StringIO(text)
    reader = csv.DictReader(buffer)
    fieldnames = [name for name in (reader.fieldnames or []) if name is not None]
    index = {}
    for name in fieldnames:
        index.setdefault(name.strip().lower(), name)
    has_role = 'role' in index or 'is_sender' in index
    has_text = any(key in index for key in ('text', 'content', 'strcontent'))
    if not (has_role and has_text):
        _fail('CSV 表头无法识别：至少需要 role 或 is_sender 之一，以及 text/content/strContent 之一')
    entries: List[Dict[str, Any]] = []
    for row in reader:
        entry: Dict[str, Any] = {}
        for key in ('role', 'is_sender', 'text', 'content', 'strContent') + _TIME_KEYS:
            source = index.get(key.lower())
            if source is not None:
                entry[key] = row.get(source)
        entries.append(entry)
    return _normalize_entries(entries)


# ---------------------------------------------------------------- TXT

_TS_CORE = r'\d{4}[-/]\d{1,2}[-/]\d{1,2}[ T]\d{1,2}:\d{2}(?::\d{2})?'

_RE_BRACKET_NAME = re.compile(r'^\s*\[(?P<ts>' + _TS_CORE + r')\]\s*(?P<name>[^:：]{1,40}?)\s*[:：]\s*(?P<body>.*)$')
_RE_BRACKET_PLAIN = re.compile(r'^\s*\[(?P<ts>' + _TS_CORE + r')\]\s*(?P<name>[^:：]{1,40}?)\s*$')
_RE_TS_FIRST = re.compile(r'^\s*(?P<ts>' + _TS_CORE + r')\s+(?P<name>[^:：]{1,40}?)\s*$')
_RE_NAME_TS_LAST = re.compile(r'^\s*(?P<name>[^:：]{1,40}?)\s+(?P<ts>' + _TS_CORE + r')\s*$')
_RE_NAME_BODY = re.compile(r'^\s*(?P<name>[^:：]{1,40}?)\s*[:：]\s*(?P<body>.*)$')
_RE_TIME_PREFIX = re.compile(r'^\s*\d{1,2}\s*[:：]\s*\d{2}')


def _speaker_name_ok(name: str) -> bool:
    stripped = name.strip()
    if not stripped or len(stripped) > 40:
        return False
    if stripped.isdigit():
        return False
    if '//' in stripped:
        return False
    return True


def _match_txt_header(line: str) -> Optional[Tuple[str, Optional[str], Optional[str]]]:
    """识别一行是否是消息头。

    返回 ``(说话人, 时间字符串或None, 首行正文或None)``；不是消息头返回 ``None``。
    """
    match = _RE_BRACKET_NAME.match(line)
    if match and _speaker_name_ok(match.group('name')):
        return match.group('name').strip(), match.group('ts'), match.group('body')
    match = _RE_BRACKET_PLAIN.match(line)
    if match and _speaker_name_ok(match.group('name')):
        return match.group('name').strip(), match.group('ts'), None
    if not _RE_TIME_PREFIX.match(line):
        match = _RE_TS_FIRST.match(line)
        if match and _speaker_name_ok(match.group('name')):
            return match.group('name').strip(), match.group('ts'), None
    match = _RE_NAME_TS_LAST.match(line)
    if match and _speaker_name_ok(match.group('name')):
        return match.group('name').strip(), match.group('ts'), None
    if _RE_TIME_PREFIX.match(line):
        return None
    match = _RE_NAME_BODY.match(line)
    if match and _speaker_name_ok(match.group('name')):
        return match.group('name').strip(), None, match.group('body')
    return None


def _resolve_speaker(name: str, self_name: str, peer_name: str) -> Tuple[Optional[str], Optional[str]]:
    """返回 ``(role, prefix)``；``role`` 为 ``None`` 表示无法确定（需要 self_name）。"""
    clean = name.strip()
    if self_name and clean == self_name.strip():
        return ROLE_ASSISTANT, None
    if peer_name and clean == peer_name.strip():
        return ROLE_USER, None
    key = clean.lower()
    if clean in SELF_ALIASES or key in SELF_ALIASES:
        return ROLE_ASSISTANT, None
    if clean in PEER_ALIASES or key in PEER_ALIASES:
        return ROLE_USER, None
    if self_name:
        # 群聊/其他成员：按 user 处理，但保留说话人前缀，避免丢失成员信息。
        return ROLE_USER, '[%s] ' % clean
    return None, None


def _parse_txt(text: str, self_name: str, peer_name: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    warnings: List[str] = []
    messages: List[Dict[str, Any]] = []
    preamble_lines: List[int] = []
    current: Optional[Dict[str, Any]] = None

    def flush() -> None:
        if current is None:
            return
        parts = list(current['parts'])
        prefix = current.get('prefix') or ''
        first = next((i for i, part in enumerate(parts) if part.strip()), None)
        if first is not None and prefix:
            parts[first] = prefix + parts[first].lstrip()
        body = '\n'.join(parts).strip('\n')
        if not body.strip():
            warnings.append('第 %d 行的消息正文为空，已跳过' % current['line'])
            return
        messages.append({
            'role': current['role'],
            'text': body,
            't': current['t'],
            '_index': current['line'],
        })

    for lineno, raw in enumerate(text.split('\n'), start=1):
        line = raw.rstrip('\r')
        header = _match_txt_header(line)
        if header is not None:
            flush()
            current = None
            name, ts_text, inline = header
            role, prefix = _resolve_speaker(name, self_name, peer_name)
            if role is None:
                _fail('第 %d 行的说话人「%s」无法确定是本人还是对方，'
                      '请先填写本人昵称（self_name）后再导入' % (lineno, _clip(name, 24)))
            if ts_text:
                moment, time_warning = parse_time(ts_text)
                if time_warning:
                    warnings.append('第 %d 行的消息：%s' % (lineno, time_warning))
            else:
                moment = 0
            parts = [inline] if inline is not None else []
            current = {'role': role, 't': moment, 'parts': parts,
                       'prefix': prefix, 'line': lineno}
            continue
        if current is None:
            if line.strip():
                preamble_lines.append(lineno)
            continue
        current['parts'].append(line)

    flush()
    if preamble_lines:
        warnings.append('文件开头有 %d 行无法识别归属，已忽略（第 %d 行起）'
                        % (len(preamble_lines), preamble_lines[0]))
    return messages, warnings


# ---------------------------------------------------------------- 入口：parse_chat


def sniff_format(text: str) -> str:
    """在 auto 模式下猜测格式：json / csv / txt。"""
    stripped = text.lstrip('\ufeff \t\r\n')
    if stripped.startswith('{') or stripped.startswith('['):
        return 'json'
    for line in stripped.splitlines():
        if line.strip():
            return 'csv' if _csv_header_ok(line) else 'txt'
    return 'txt'


def _read_content(content: Any) -> Tuple[str, bytes]:
    if isinstance(content, bool) or content is None or not isinstance(content, (str, bytes)):
        _fail('聊天记录内容必须是字符串或 UTF-8 字节，收到 %s' % type(content).__name__)
    if isinstance(content, bytes):
        raw = content
        try:
            text = content.decode('utf-8')
        except UnicodeDecodeError:
            _fail('聊天记录不是合法的 UTF-8 编码，请确认文件编码后重新导入')
        return text, raw
    return content, content.encode('utf-8')


def _finalize(messages: List[Dict[str, Any]], warnings: List[str],
              source_hash: str) -> Dict[str, Any]:
    if not messages:
        _fail('未解析到任何可用消息（已跳过 %d 条），请检查文件格式或角色/正文字段' % len(warnings))
    if len(messages) > MAX_MESSAGES:
        _fail('解析到 %d 条消息，超过单次导入上限 %d 条，请拆分文件后重新导入'
              % (len(messages), MAX_MESSAGES))
    for message in messages:
        if len(message['text']) > MAX_TEXT_CHARS:
            _fail('第 %d 条消息正文 %d 字，超过单条上限 %d 字，请拆分后重新导入'
                  % (message['_index'], len(message['text']), MAX_TEXT_CHARS))
    rows = []
    for message in messages:
        key = hashlib.sha256(('%s:%s' % (source_hash, message['_index'])).encode('utf-8')).hexdigest()
        rows.append({
            'role': message['role'],
            'text': message['text'],
            't': int(message['t']),
            'import_key': key,
        })
    return {'messages': rows, 'warnings': warnings}


def parse_chat(content: Any, format: str = 'auto', self_name: str = '',
               peer_name: str = '') -> Dict[str, Any]:
    """解析聊天记录内容。

    参数
    ----
    content : str | bytes
        聊天记录全文（≤ 5 MiB UTF-8）。bytes 必须是合法 UTF-8。
    format : 'auto' | 'json' | 'txt' | 'csv'
        auto 时按首个非空行嗅探（``{``/``[`` → json；可识别的 CSV 表头 → csv；否则 txt）。
    self_name : str
        本人昵称（TXT 精确匹配本人说话人；群聊中其他说话人按 user 处理并保留 ``[昵称]`` 前缀）。
    peer_name : str
        对方昵称（TXT 精确匹配对方说话人）。

    返回
    ----
    dict: ``{messages, warnings, detected_format, source_hash, total}``
        ``messages`` 每项为 ``{role, text, t, import_key}``。

    异常
    ----
    ValueError：内容超限、编码非法、格式不支持、角色不明确、无可用消息等（均为中文具体提示）。
    """
    text, raw = _read_content(content)
    if len(raw) > MAX_BYTES:
        _fail('聊天记录 %d 字节，超过 5MiB（%d 字节）上限，请拆分文件后重新导入' % (len(raw), MAX_BYTES))
    source_hash = hashlib.sha256(raw).hexdigest()
    if text.startswith('\ufeff'):
        text = text[1:]
    if not text.strip():
        _fail('聊天记录内容为空，无法解析')

    picked = (format or 'auto').strip().lower()
    if picked not in FORMATS:
        _fail('不支持的格式「%s」，可选 auto/json/txt/csv' % _clip(str(format), 16))
    detected = sniff_format(text) if picked == 'auto' else picked

    if detected == 'json':
        messages, warnings = _normalize_entries(_load_json_entries(text))
    elif detected == 'csv':
        messages, warnings = _parse_csv(text)
    else:
        messages, warnings = _parse_txt(text, self_name or '', peer_name or '')

    built = _finalize(messages, warnings, source_hash)
    return {
        'messages': built['messages'],
        'warnings': built['warnings'],
        'detected_format': detected,
        'source_hash': source_hash,
        'total': len(built['messages']),
    }


# ---------------------------------------------------------------- 合并


def _coerce_rows(rows: Any, what: str) -> List[Any]:
    if rows is None:
        return []
    if isinstance(rows, dict):
        if isinstance(rows.get('messages'), list):
            return list(rows['messages'])
        keys = list(rows.keys())
        if len(keys) == 1 and isinstance(rows[keys[0]], list):
            return list(rows[keys[0]])
        _fail('merge_history 只能合并单个联系人的消息列表：'
              '请从 history.json 按 uid 取出一个数组后再合并（%s）' % what)
    if isinstance(rows, (list, tuple)):
        return list(rows)
    if isinstance(rows, Iterable) and not isinstance(rows, (str, bytes)):
        return list(rows)
    _fail('%s 必须是消息列表、{"messages": [...]} 或单个 uid 的数组' % what)
    return []


def _normalize_row(row: Any) -> Optional[Dict[str, Any]]:
    """保留旧行全部字段，仅规范化 role/text/t 以保证可比对与排序。"""
    if not isinstance(row, dict):
        return None
    normalized = dict(row)
    role = normalized.get('role')
    if not isinstance(role, str) or not role.strip():
        role = ROLE_USER
    normalized['role'] = role
    text = normalized.get('text')
    if text is None:
        text = ''
    elif not isinstance(text, str):
        text = str(text)
    normalized['text'] = text
    moment, _warning = parse_time(normalized.get('t', 0))
    normalized['t'] = moment
    return normalized


def merge_history(existing: Any, incoming: Any, limit: int = DEFAULT_HISTORY_LIMIT) -> Dict[str, Any]:
    """把 ``incoming`` 合并进单个联系人的 ``existing`` 历史。

    返回 ``{messages, added, duplicates, trimmed, total}``：
    完整合并列表 + 新增/重复/裁剪统计（``total == len(messages)``）。
    旧行保留 role/text/t 及全部其他字段；``t = 0`` 的记录保持原顺序排在最前，
    其余按 ``t`` 稳定升序；超过 ``limit`` 时裁剪最旧记录。
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        _fail('limit 必须是正整数（每个联系人保留的条数），收到 %r' % (limit,))
    limit = int(limit)

    old_rows: List[Dict[str, Any]] = []
    for row in _coerce_rows(existing, 'existing'):
        normalized = _normalize_row(row)
        if normalized is not None:
            old_rows.append(normalized)
    incoming_rows: List[Dict[str, Any]] = []
    for row in _coerce_rows(incoming, 'incoming'):
        normalized = _normalize_row(row)
        if normalized is not None:
            incoming_rows.append(normalized)

    known_keys = set()
    for row in old_rows:
        key = row.get('import_key')
        if isinstance(key, str) and key:
            known_keys.add(key)
    # 多重集计数：既防止重复导入已存在的内容，又不误删有意连续出现的相同无时间消息。
    remaining = Counter((row['role'], row['text'], row['t']) for row in old_rows)

    kept: List[Dict[str, Any]] = []
    duplicates = 0
    for row in incoming_rows:
        key = row.get('import_key')
        if isinstance(key, str) and key and key in known_keys:
            duplicates += 1
            continue
        signature = (row['role'], row['text'], row['t'])
        if remaining.get(signature, 0) > 0:
            remaining[signature] -= 1
            duplicates += 1
            if isinstance(key, str) and key:
                known_keys.add(key)
            continue
        if isinstance(key, str) and key:
            known_keys.add(key)
        kept.append(row)

    rows = old_rows + kept
    unknown = [row for row in rows if row['t'] <= 0]
    timed = sorted([row for row in rows if row['t'] > 0], key=lambda row: row['t'])
    ordered = unknown + timed
    trimmed = max(0, len(ordered) - limit)
    final = ordered[trimmed:]
    return {
        'messages': final,
        'added': len(kept),
        'duplicates': duplicates,
        'trimmed': trimmed,
        'total': len(final),
    }


# ---------------------------------------------------------------- 预览报告


def _summary_text(report: Dict[str, Any]) -> str:
    return ('聊天记录导入预览：本次将写入 %d 条（新增 %d 条，重复跳过 %d 条，'
            '超出上限裁剪 %d 条）；现有 %d 条，待导入 %d 条。'
            % (report['total'], report['added'], report['duplicates'], report['trimmed'],
               report.get('existing_total', 0), report.get('incoming_total', 0)))


def preview_report(existing: Any, incoming: Any, limit: int = DEFAULT_HISTORY_LIMIT,
                   warnings: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """不落盘的导入预览：统计与 ``merge_history`` 完全一致（``total == len(messages)``）。

    报告只含计数与消息行，不含任何格式化后的正文文本；``render_report`` 也不会打印正文。
    """
    old_rows = _coerce_rows(existing, 'existing')
    new_rows = _coerce_rows(incoming, 'incoming')
    merged = merge_history(old_rows, new_rows, limit=limit)
    report = {
        'added': merged['added'],
        'duplicates': merged['duplicates'],
        'trimmed': merged['trimmed'],
        'total': merged['total'],
        'messages': merged['messages'],
        'existing_total': sum(1 for row in old_rows if isinstance(row, dict)),
        'incoming_total': sum(1 for row in new_rows if isinstance(row, dict)),
        'warnings': list(warnings or []),
    }
    report['summary'] = _summary_text(report)
    return report


def render_report(report: Dict[str, Any]) -> str:
    """把预览报告渲染为中文摘要字符串（只有计数，绝不包含聊天正文）。"""
    if not isinstance(report, dict):
        _fail('render_report 需要 preview_report 返回的报告字典')
    return report.get('summary') or _summary_text(report)
