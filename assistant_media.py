# -*- coding: utf-8 -*-
"""微信/Hermes 助手媒体与链接处理（独立模块，只用标准库）。

职责：
1. ``MediaStore``：受控的本地图片入库（上传 / OneBot 入向图片导入）与解析。
   - 所有图片一律复制进 ``home/uploads``（随机 uuid 命名，不信文件名/后缀），
     模型只看受控副本；
   - 入向本地图片只允许来自 ``home/onebot/image`` 以及显式传入的 ``allowed_roots``
     （默认不扫描整个微信容器），并拒绝目录外路径、符号链接逃逸、远程 file URI。
2. ``normalize_message``：把 OneBot 消息归一化成
   ``{text, kind, images, links, notices, skip}``。XML（raw_message 或消息段里的
   XML）绝不直接当正文；分享链接解析出人能读的标题/描述/URL；图片从受控本地路径
   导入；表情/文件诚实标 unsupported；系统消息跳过。
3. ``fetch_public_page``：读取公共网页正文（SSRF 防护）。连接钉住已校验的公网
   IP、每次重定向重新校验、只收 text/* 且限 1MB、抽正文并给摘要；登录页/付费墙/
   无正文一律诚实返回 failed。

安全说明（给以后接 UI 的人）：
- ``fetch_public_page`` 返回的 ``content`` 是**纯文本**（已去 script/style/nav/footer
  与广告块），不是 HTML；前端展示请一律用 ``textContent``，绝不要把返回值直接
  塞进 ``innerHTML``。
- 本模块不会自动跟踪正文里的第三方链接；重复 URL 去重在这里做，最多几条由主逻辑
  决定（建议 ≤3）。
- 本轮不支持原生 SVG/GIF 表情，也不提供发图接口。
"""
import base64
import binascii
import hashlib
import html
import http.client
import ipaddress
import json
import os
import re
import socket
import tempfile
import threading
import uuid
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

# --------------------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------------------
MAX_UPLOAD_BYTES = 8 * 1024 * 1024        # 单个图片上限 8MiB
MAX_NAME_CHARS = 120                      # 展示名上限
MAX_PREFIX_CHARS = 64                     # 群消息「成员名:」前缀保留上限
INDEX_FILE = '.index.json'                # uploads 下的元数据（支持重启后 resolve）
_ID_RE = re.compile(r'^[0-9a-f]{32}$')
_UNSAFE_XML_RE = re.compile(r'<!\s*(?:DOCTYPE|ENTITY)', re.I)
_URL_RE = re.compile(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+")
_URL_TRAILING = '，。！？；：、）】」》…,.!?;:'

# 网页读取
ALLOWED_CONTENT_TYPES = ('text/html', 'text/plain', 'application/xhtml+xml')
ALLOWED_PORTS = (80, 443)
MAX_REDIRECTS = 3
MAX_CONTENT_CHARS = 20000
MAX_SUMMARY_CHARS = 600
MIN_CONTENT_CHARS = 20
REDIRECT_STATUSES = (301, 302, 303, 307, 308)
BLOCKED_HOST_SUFFIXES = ('.localhost', '.local', '.internal', '.home.arpa', '.lan')
LOGIN_MARKERS = (
    '请登录', '请先登录', '登录后查看', '登录后继续', '登录后阅读', '立即登录',
    '扫码登录', '扫描二维码登录', 'sign in to continue', 'sign in to read',
    'please log in', 'log in to continue', 'login required',
    'subscribe to read', 'subscribers only', 'subscription required',
    '付费阅读', '付费后查看', '订阅后查看', '开通会员', '会员专享', '购买后查看',
)

# 消息段类型
_SEG_SYSTEM = ('sys', 'system')
_SEG_IMAGE = ('image',)
_SEG_SHARE = ('share', 'appmsg', 'link')
_SEG_FILE = ('file',)
_SEG_FACE = ('face', 'emoji')


# --------------------------------------------------------------------------------------
# 通用工具
# --------------------------------------------------------------------------------------
def _safe_name(name, fallback='image'):
    """只保留 basename，去掉控制字符并限长——绝不信任调用方给的名字/路径。"""
    if not isinstance(name, str):
        return fallback
    cleaned = name.replace('\\', '/').replace('\x00', '')
    base = os.path.basename(cleaned).strip()
    base = re.sub(r'[\x00-\x1f\x7f]', '', base).strip()
    if base in ('', '.', '..'):
        return fallback
    return base[:MAX_NAME_CHARS]


def detect_image_mime(data):
    """按真实字节魔数判断图片类型；不支持的类型返回 None（不看文件名后缀）。"""
    if not isinstance(data, (bytes, bytearray)) or len(data) < 12:
        return None
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return 'image/png'
    if data[:3] == b'\xff\xd8\xff':
        return 'image/jpeg'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    return None


def _write_private(path, data):
    """原子写文件并置 600 权限。"""
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.tmp-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, str(path))
    finally:
        if os.path.exists(temporary):
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _atomic_json(path, value):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.tmp-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=1)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, str(path))
    finally:
        if os.path.exists(temporary):
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _looks_like_xml(value):
    if not isinstance(value, str):
        return False
    head = value.lstrip()
    if head.startswith('<'):
        return True
    return '<?xml' in head[:200].lower()


def _xml_safe(text):
    """拒绝 DOCTYPE/ENTITY：不做任何实体解析，直接不信任这份 XML。"""
    return not _UNSAFE_XML_RE.search(text or '')


def _xml_field(xml, tag, attr=None):
    """从（可能不闭合的）微信 XML 里取一个字段值；取不到返回空串。"""
    if not xml:
        return ''
    if attr:
        pattern = r'<%s\b[^>]*\b%s\s*=\s*["\']([^"\']*)["\']'
        match = re.search(pattern % (re.escape(tag), re.escape(attr)), xml, re.I | re.S)
        value = match.group(1) if match else ''
    else:
        pattern = r'<%s\b[^>]*>(.*?)</%s>' % (re.escape(tag), re.escape(tag))
        match = re.search(pattern, xml, re.I | re.S)
        value = match.group(1) if match else ''
    value=value.strip()
    if value.startswith('<![CDATA[') and value.endswith(']]>'):
        return value[9:-3].strip()
    return html.unescape(value).strip()


def _split_message_prefix(raw):
    """群消息 raw 常形如 ``成员名:\\n<?xml...`` 或带 openim 前缀。

    从第一个 ``<msg``/``<?xml`` 切开：前缀单独保留（限量），正文只取 XML 之后的部分，
    避免把整段 XML 当正文喂给模型。
    """
    if not isinstance(raw, str) or not raw:
        return '', ''
    lowered = raw.lower()
    index = -1
    for marker in ('<?xml', '<msg'):
        position = lowered.find(marker)
        if position != -1 and (index == -1 or position < index):
            index = position
    if index <= 0:
        return '', raw
    prefix = ' '.join(raw[:index].split())[:MAX_PREFIX_CHARS]
    return prefix, raw[index:]


def _parse_appmsg(xml):
    """解析微信 appmsg（分享/引用/文件）。返回 safe=False 表示 XML 不安全。"""
    if not _xml_safe(xml):
        return {'safe': False}
    return {
        'safe': True,
        'type': _xml_field(xml, 'type'),
        'title': _xml_field(xml, 'title'),
        'des': _xml_field(xml, 'des'),
        'url': _xml_field(xml, 'url'),
        'filename': _xml_field(xml, 'filename'),
    }


def extract_links(text):
    """从纯文本里抽 http/https 链接，去掉尾部中文标点并去重。"""
    links = []
    for raw in _URL_RE.findall(text or ''):
        link = raw.rstrip(_URL_TRAILING)
        if link and link not in links:
            links.append(link)
    return links


# --------------------------------------------------------------------------------------
# 受控图片仓库
# --------------------------------------------------------------------------------------
class MediaStore(object):
    """本地图片的受控仓库：只有 store 写入的副本才是可信的。"""

    def __init__(self, home, allowed_roots=None):
        self.home = Path(home).expanduser().resolve()
        self.uploads = self.home / 'uploads'
        self.inbox = self.home / 'onebot' / 'image'
        self.index_path = self.uploads / INDEX_FILE
        roots = [self.inbox]
        for root in (allowed_roots or []):
            roots.append(Path(root).expanduser())
        self.allowed_roots = []
        for root in roots:
            real = os.path.realpath(str(root))
            if real not in self.allowed_roots:
                self.allowed_roots.append(real)
        self._lock = threading.RLock()
        self.uploads.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(str(self.uploads), 0o700)
        except OSError:
            pass
        self._index = self._load_index()

    # ---------------- 元数据 ----------------
    def _load_index(self):
        try:
            with open(str(self.index_path), encoding='utf-8') as stream:
                raw = json.load(stream)
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {key: value for key, value in raw.items()
                if _ID_RE.match(key) and isinstance(value, dict)}

    def _save_index(self):
        _atomic_json(self.index_path, self._index)

    @staticmethod
    def _asset(entry):
        return {'id': entry['id'], 'name': entry['name'], 'mime': entry['mime'],
                'kind': 'image', 'url': '/api/media?id=' + entry['id']}

    # ---------------- 写入 ----------------
    @staticmethod
    def _decode_base64(value):
        if isinstance(value, (bytes, bytearray)):
            try:
                value = bytes(value).decode('ascii')
            except UnicodeDecodeError:
                raise ValueError('base64 数据不是 ASCII')
        if not isinstance(value, str) or not value:
            raise ValueError('base64 数据为空')
        try:
            data = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError('base64 数据非法')
        if not data:
            raise ValueError('base64 数据为空')
        return data

    def _store_bytes(self, data, mime, name, dedupe=False):
        digest = hashlib.sha256(data).hexdigest()
        with self._lock:
            if dedupe:
                for entry in self._index.values():
                    if entry.get('sha256') == digest:
                        return self._asset(entry)
            media_id = uuid.uuid4().hex          # 随机固定路径，不带原后缀
            _write_private(self.uploads / media_id, data)
            entry = {'id': media_id, 'name': name, 'mime': mime, 'kind': 'image',
                     'size': len(data), 'sha256': digest}
            self._index[media_id] = entry
            self._save_index()
        return self._asset(entry)

    def upload(self, filename, data_base64):
        """严格 base64 + 魔数校验后入库，返回 asset。"""
        data = self._decode_base64(data_base64)
        if len(data) > MAX_UPLOAD_BYTES:
            raise ValueError('图片超过大小限制')
        mime = detect_image_mime(data)
        if mime is None:
            raise ValueError('不支持的图片格式（仅支持 PNG/JPEG/WEBP）')
        return self._store_bytes(data, mime, _safe_name(filename))

    # ---------------- 本地导入 ----------------
    @staticmethod
    def _local_path(value):
        if isinstance(value, Path):
            value = str(value)
        if not isinstance(value, str) or not value.strip():
            raise ValueError('本地路径为空')
        raw = value.strip()
        if raw.lower().startswith('file:'):
            parts = urlsplit(raw)
            if (parts.scheme or '').lower() != 'file':
                raise ValueError('不是 file URI')
            netloc = (parts.netloc or '').lower()
            if netloc not in ('', 'localhost'):
                raise ValueError('拒绝远程 file URI（只允许本机路径）')
            path = unquote(parts.path or '')
            if not path:
                raise ValueError('file URI 缺少路径')
            return path
        if '://' in raw:
            raise ValueError('只接受本地路径或本机 file URI')
        return raw

    def _within_allowed_roots(self, real_path):
        for root in self.allowed_roots:
            root_real = os.path.realpath(root)
            if real_path == root_real:
                return True
            if real_path.startswith(root_real.rstrip(os.sep) + os.sep):
                return True
        return False

    def import_local(self, path_or_file_uri):
        """把受控目录里的本地图片复制进 uploads；可按 SHA256 去重。"""
        candidate = Path(self._local_path(path_or_file_uri))
        real = Path(os.path.realpath(str(candidate)))      # 解析符号链接，防逃逸
        if not self._within_allowed_roots(str(real)):
            raise ValueError('路径不在允许的本地图片目录内')
        if not real.exists():
            raise FileNotFoundError('本地图片不存在')
        if not real.is_file():
            raise ValueError('不是普通文件')
        if real.stat().st_size > MAX_UPLOAD_BYTES:
            raise ValueError('图片超过大小限制')
        data = real.read_bytes()
        if len(data) > MAX_UPLOAD_BYTES:
            raise ValueError('图片超过大小限制')
        mime = detect_image_mime(data)                     # 用真实字节校验
        if mime is None:
            raise ValueError('不支持的图片格式（仅支持 PNG/JPEG/WEBP）')
        return self._store_bytes(data, mime, _safe_name(real.name), dedupe=True)

    # ---------------- 读取 ----------------
    def resolve(self, media_id):
        """按 id 取回 (Path, mime, filename)；只认已写入且仍有效的本地 id。"""
        if not isinstance(media_id, str) or not _ID_RE.match(media_id):
            raise ValueError('无效的媒体 id')
        with self._lock:
            entry = self._index.get(media_id)
        if not entry:
            raise FileNotFoundError('未知的媒体 id')
        path = self.uploads / media_id
        if not path.is_file():
            raise FileNotFoundError('媒体文件已丢失')
        mime = detect_image_mime(path.read_bytes())
        if mime is None or mime != entry.get('mime'):
            raise ValueError('媒体文件已损坏或被替换')
        return path, mime, (entry.get('name') or 'image')


# --------------------------------------------------------------------------------------
# OneBot 消息归一化
# --------------------------------------------------------------------------------------
def _error_category(error):
    """把异常归类成不含本机路径的安全文案。"""
    if isinstance(error, FileNotFoundError):
        return '本地文件不存在'
    if isinstance(error, PermissionError):
        return '本地文件不可读'
    if isinstance(error, ValueError):
        return '本地文件未通过校验'
    return '本地文件处理失败'


def _image_notice(title, summary, error=None, status='failed'):
    notice = {'kind': 'image', 'title': title or '图片', 'summary': summary,
              'status': status}
    if error:
        notice['error'] = error
    return notice


def _handle_image(seg_data, media_store, images, notices, text_parts, source_name=''):
    """图片段：从受控本地路径导入；失败就诚实报 failed，绝不假装已识别。

    ``source_name`` 是事件级 ``msgsource`` 里的安全字段（真实适配器会把
    ``<img_file_name>`` 放这里），图片 XML 里没有名字时才用它。
    """
    xml = seg_data.get('text') if isinstance(seg_data.get('text'), str) else ''
    url = seg_data.get('url') or seg_data.get('file') or ''
    name = ''
    if xml and _looks_like_xml(xml):
        if not _xml_safe(xml):
            notices.append(_image_notice('图片', '检测到不安全的 XML 声明，已拒绝处理',
                                         'unsafe xml'))
            text_parts.append('[图片：读取失败]')
            return
        name = _xml_field(xml, 'img_file_name')       # 只信安全字段里的名字
    if not name:
        name = source_name or ''
    if not isinstance(url, str) or not url.strip():
        notices.append(_image_notice(name or '图片', '图片缺少本地文件，未能读取',
                                     'no local file'))
        text_parts.append('[图片：读取失败]')
        return
    try:
        asset = media_store.import_local(url.strip())
    except (ValueError, FileNotFoundError, OSError) as error:
        notices.append(_image_notice(name or '图片', '图片读取失败', _error_category(error)))
        text_parts.append('[图片：读取失败]')
        return
    if name:
        asset = dict(asset)
        asset['name'] = _safe_name(name, fallback=asset['name'])
    if seg_data.get('image_quality') == 'thumbnail':
        asset = dict(asset)
        asset['quality'] = 'thumbnail'
        text_parts.append('[图片为微信缩略图，细节可能不足]')
        notices.append(_image_notice(name or '图片', '已读取微信缩略图，细节可能不足', status='ready'))
    images.append(asset)


def _file_notice(name, description):
    summary = '文件内容未读取，暂不支持解析'
    description = ' '.join((description or '').split())
    if description:
        summary += '。说明：' + description
    return {'kind': 'file', 'title': _safe_name(name, 'file'),
            'summary': summary[:300], 'status': 'unsupported'}


def _handle_appmsg(xml, text_parts, links, notices, flags):
    """分享(appmsg5) / 引用(appmsg57) / 文件(appmsg6)。"""
    info = _parse_appmsg(xml)
    if not info.get('safe'):
        flags['link'] = True
        notices.append({'kind': 'link', 'title': '链接',
                        'summary': '检测到不安全的 XML 声明，已拒绝解析',
                        'status': 'failed', 'error': 'unsafe xml'})
        return
    msg_type = (info.get('type') or '').strip()
    title = info.get('title') or ''
    des = info.get('des') or ''
    url = info.get('url') or ''

    if msg_type == '6' or (info.get('filename') and not url):
        flags['file'] = True
        notices.append(_file_notice(info.get('filename') or title or '文件', des))
        return

    if msg_type == '57':
        # 引用消息：title/des 是人能读的正文，可以保留
        for part in (title, des):
            if part:
                text_parts.append(part)
        return

    # 外链分享：给人能读的标题 + 描述 + URL
    flags['link'] = True
    block = '\n'.join(part for part in (title, des) if part)
    if url:
        block = (block + '\n' + url) if block else url
        if url.startswith('http://') or url.startswith('https://'):
            if url not in links:
                links.append(url)
    if block:
        text_parts.append(block)


def _handle_raw_xml(xml, media_store, text_parts, images, links, notices, flags,
                    source_name=''):
    """消息段为空、只有 raw_message 是 XML 时的兜底分类。"""
    if not _xml_safe(xml):
        notices.append({'kind': 'unsupported', 'title': 'XML 消息',
                        'summary': '检测到不安全的 XML 声明，已拒绝处理',
                        'status': 'failed', 'error': 'unsafe xml'})
        return
    lowered = xml.lower()
    if '<img' in lowered or '<image' in lowered:
        flags['image'] = True
        _handle_image({'text': xml}, media_store, images, notices, text_parts,
                      source_name)
        return
    if '<emoji' in lowered and '<appmsg' not in lowered:
        flags['face'] = True
        notices.append({'kind': 'face', 'title': '表情', 'summary': '原生表情暂不支持',
                        'status': 'unsupported'})
        return
    if '<appmsg' in lowered:
        _handle_appmsg(xml, text_parts, links, notices, flags)
        return
    notices.append({'kind': 'unsupported', 'title': 'XML 消息',
                    'summary': '暂不支持解析该 XML 消息', 'status': 'unsupported'})


def normalize_message(data, media_store):
    """OneBot 消息 → {text, kind, images, links, notices, skip}。

    结构不合法（不是 dict / message 不是数组 / 消息段不是对象）抛 ValueError，
    让调用方按单条消息处理，不要崩掉收消息线程。
    """
    if not isinstance(data, dict):
        raise ValueError('OneBot 消息必须是 JSON 对象')
    segments = data.get('message')
    if segments is None:
        segments = []
    if not isinstance(segments, list):
        raise ValueError('message 字段必须是数组')
    for segment in segments:
        if not isinstance(segment, dict):
            raise ValueError('消息段必须是对象')
        if not isinstance(segment.get('data'), dict):
            raise ValueError('消息段 data 必须是对象')
    raw_message = data.get('raw_message')
    if raw_message is None:
        raw_message = ''
    if not isinstance(raw_message, str):
        raise ValueError('raw_message 必须是字符串')

    text_parts = []
    images = []
    links = []
    notices = []
    flags = {'image': False, 'link': False, 'file': False, 'face': False}
    is_system = data.get('message_type') == 'system'
    # 事件级 msgsource 里的安全字段（真实适配器的 <img_file_name>）；不安全就忽略
    source_name = ''
    msgsource = data.get('msgsource')
    if isinstance(msgsource, str) and msgsource and _xml_safe(msgsource):
        source_name = _xml_field(msgsource, 'img_file_name')

    for segment in segments:
        seg_type = str(segment.get('type') or '').strip().lower()
        seg_data = segment['data']
        if seg_type in _SEG_SYSTEM:
            is_system = True
        elif seg_type == 'text':
            value = seg_data.get('text')
            if not isinstance(value, str) or not value:
                continue
            if _looks_like_xml(value):                       # 文本段里的 XML 也不当正文
                _handle_raw_xml(value, media_store, text_parts, images, links,
                                notices, flags, source_name)
            else:
                text_parts.append(value)
        elif seg_type in _SEG_IMAGE:
            flags['image'] = True
            _handle_image(seg_data, media_store, images, notices, text_parts,
                          source_name)
        elif seg_type in _SEG_FILE:
            flags['file'] = True
            xml = seg_data.get('text') if isinstance(seg_data.get('text'), str) else ''
            if xml and _looks_like_xml(xml):
                _handle_appmsg(xml, text_parts, links, notices, flags)
            else:
                notices.append(_file_notice(seg_data.get('file') or seg_data.get('name')
                                            or '文件', ''))
        elif seg_type in _SEG_SHARE:
            xml = seg_data.get('text') if isinstance(seg_data.get('text'), str) else ''
            if xml and _looks_like_xml(xml):
                _handle_appmsg(xml, text_parts, links, notices, flags)
            else:
                flags['link'] = True
                title = seg_data.get('title')
                url = seg_data.get('url')
                block = '\n'.join(part for part in (title, seg_data.get('content'))
                                  if isinstance(part, str) and part)
                if isinstance(url, str) and url:
                    if url.startswith(('http://', 'https://')):
                        block = (block + '\n' + url) if block else url
                        if url not in links:
                            links.append(url)
                if block:
                    text_parts.append(block)
        elif seg_type in _SEG_FACE:
            flags['face'] = True
            notices.append({'kind': 'face', 'title': '表情',
                            'summary': '原生表情暂不支持', 'status': 'unsupported'})
        elif seg_type:
            notices.append({'kind': 'unsupported', 'title': seg_type,
                            'summary': '暂不支持的消息类型', 'status': 'unsupported'})

    prefix, body = _split_message_prefix(raw_message)
    if not text_parts and not images and not notices and body:
        if _looks_like_xml(body):
            _handle_raw_xml(body, media_store, text_parts, images, links, notices,
                            flags, source_name)
        else:
            text_parts.append(body)

    parts = list(text_parts)
    if prefix and (parts or images or links or notices):
        parts = [prefix] + parts
    text = '\n'.join(part for part in parts if part)

    for link in extract_links(text):
        if link not in links:
            links.append(link)

    if is_system:
        kind = 'system'
    elif flags['image']:
        kind = 'image'
    elif flags['link']:
        kind = 'link'
    elif flags['file']:
        kind = 'file'
    elif text.strip():
        kind = 'text'
    else:
        kind = 'unsupported'

    if is_system:
        skip = True
    else:
        skip = not (text.strip() or images or links or notices)

    return {'text': text, 'kind': kind, 'images': images, 'links': links,
            'notices': notices, 'skip': skip}


# --------------------------------------------------------------------------------------
# 公共网页正文读取（SSRF 防护）
# --------------------------------------------------------------------------------------
class _FetchError(Exception):
    """带安全文案的失败；payload 里可以带已经抽到的标题/正文。"""

    def __init__(self, message, payload=None):
        super(_FetchError, self).__init__(message)
        self.payload = payload or {}


class _PinnedConnectionMixin(object):
    """把 TCP 连接钉在已经校验过的公网 IP 上（防 DNS 重绑定）。

    Host 头与 TLS SNI 仍然使用原域名——只是不再二次解析域名。
    """

    def __init__(self, host, port, pinned_ip, timeout):
        super(_PinnedConnectionMixin, self).__init__(host, port, timeout=timeout)
        self._pinned_ip = pinned_ip
        self._create_connection = self._dial_pinned

    def _dial_pinned(self, address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
                     source_address=None):
        return socket.create_connection((self._pinned_ip, address[1]),
                                        timeout, source_address)


class _PinnedHTTPConnection(_PinnedConnectionMixin, http.client.HTTPConnection):
    pass


class _PinnedHTTPSConnection(_PinnedConnectionMixin, http.client.HTTPSConnection):
    pass


def _connect(scheme, host, port, pinned_ip, timeout):
    if scheme == 'https':
        return _PinnedHTTPSConnection(host, port, pinned_ip, timeout)
    return _PinnedHTTPConnection(host, port, pinned_ip, timeout)


def _parse_ip(value):
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def _is_public_ip(address):
    """只放行真正可全球路由的地址；私网/环回/链路本地/组播/保留一律拒绝。"""
    mapped = getattr(address, 'ipv4_mapped', None)
    if mapped is not None:
        return _is_public_ip(mapped)
    if isinstance(address, ipaddress.IPv6Address):
        # Python 3.9 labels several deprecated/transition ranges global. They
        # can route embedded private IPv4 addresses, so don't rely on that flag.
        if address not in ipaddress.IPv6Network('2000::/3'):
            return False
        if address.sixtofour is not None or address.teredo is not None or address.is_site_local:
            return False
    return bool(address.is_global) and not address.is_multicast


def _is_blocked_hostname(host):
    if host in ('localhost',):
        return True
    for suffix in BLOCKED_HOST_SUFFIXES:
        if host.endswith(suffix):
            return True
    return False


def _validate_url_shape(url):
    if not isinstance(url, str) or not url.strip():
        raise _FetchError('URL 为空')
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        raise _FetchError('URL 解析失败')
    scheme = (parts.scheme or '').lower()
    if scheme not in ('http', 'https'):
        raise _FetchError('仅支持 http/https')
    if parts.username or parts.password:
        raise _FetchError('拒绝带账号密码的 URL')
    try:
        host = parts.hostname
        port = parts.port
    except ValueError:
        raise _FetchError('URL 主机名或端口非法')
    if not host:
        raise _FetchError('URL 缺少主机名')
    host = host.strip().lower().rstrip('.')
    if _is_blocked_hostname(host):
        raise _FetchError('拒绝访问本机/内网主机')
    default_port = 443 if scheme == 'https' else 80
    if port is None:
        port = default_port
    if port not in ALLOWED_PORTS:
        raise _FetchError('仅允许 80/443 端口')
    target = parts.path or '/'
    if parts.query:
        target += '?' + parts.query
    host_header = host if port == default_port else '%s:%d' % (host, port)
    return {'scheme': scheme, 'host': host, 'port': port, 'target': target,
            'host_header': host_header}


def _resolve_public_ip(host, port):
    literal = _parse_ip(host)
    if literal is not None:
        if not _is_public_ip(literal):
            raise _FetchError('拒绝访问非公网地址')
        return str(literal)
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except Exception:
        raise _FetchError('域名解析失败')
    addresses = []
    for info in infos:
        address = info[4][0]
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise _FetchError('域名解析失败')
    for address in addresses:
        parsed = _parse_ip(address)
        if parsed is None or not _is_public_ip(parsed):
            raise _FetchError('目标地址不在公网范围')      # 混合应答也拒绝
    return addresses[0]


def _parse_content_type(value):
    raw = (value or '').split(';')[0].strip().lower()
    charset = 'utf-8'
    if value:
        match = re.search(r'charset\s*=\s*["\']?([\w\-]+)', value, re.I)
        if match:
            charset = match.group(1).lower()
    return raw, charset


def _parse_length(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _read_limited(response, max_bytes):
    chunks = []
    total = 0
    while True:
        chunk = response.read(65536)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise _FetchError('响应体超过大小限制')
        chunks.append(chunk)
    return b''.join(chunks)


def _close(connection):
    try:
        connection.close()
    except Exception:
        pass


def _decode_body(body, charset):
    try:
        return body.decode(charset, errors='replace')
    except (LookupError, UnicodeDecodeError):
        return body.decode('utf-8', errors='replace')


# 抽正文时直接丢弃的标签/广告块
_SKIP_TAGS = {'script', 'style', 'noscript', 'template', 'svg', 'iframe', 'nav',
              'footer', 'header', 'aside', 'form', 'button', 'select', 'option',
              'canvas', 'video', 'audio', 'object', 'embed'}
_VOID_TAGS = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link',
              'meta', 'param', 'source', 'track', 'wbr'}
_BLOCK_TAGS = {'p', 'div', 'br', 'li', 'tr', 'section', 'article', 'h1', 'h2', 'h3',
               'h4', 'h5', 'h6', 'blockquote', 'pre', 'table', 'ul', 'ol', 'dd', 'dt',
               'figure', 'figcaption', 'main'}
_NOISE_TOKENS = {'ad', 'ads', 'advert', 'advertisement', 'advertising', 'sponsor',
                 'sponsored', 'promo', 'promotion', 'banner', 'popup', 'popover',
                 'cookie', 'cookies', 'newsletter', 'subscribe', 'sidebar', 'menu',
                 'navbar', 'breadcrumb', 'share', 'social', 'related', 'recommend',
                 'comment', 'comments'}


class _TextExtractor(HTMLParser):
    """只保留人能读的正文，丢掉脚本/样式/导航/页脚/广告块。"""

    def __init__(self):
        HTMLParser.__init__(self, convert_charrefs=True)
        self.title = ''
        self.heading = ''
        self._chunks = []
        self._skip_depth = 0
        self._in_title = False
        self._in_heading = False

    @staticmethod
    def _is_noise(attrs):
        for key, value in attrs or ():
            if key in ('class', 'id') and value:
                tokens = [t for t in re.split(r'[^a-z0-9]+', str(value).lower()) if t]
                for token in tokens:
                    if token in _NOISE_TOKENS or token.startswith('advert'):
                        return True
        return False

    def handle_starttag(self, tag, attrs):
        if self._skip_depth:
            if tag not in _VOID_TAGS:
                self._skip_depth += 1
            return
        if tag in _VOID_TAGS:
            if tag in _BLOCK_TAGS:
                self._chunks.append('\n')
            return
        if tag in _SKIP_TAGS or self._is_noise(attrs):
            self._skip_depth = 1
            return
        if tag == 'title':
            self._in_title = True
        elif tag == 'h1':
            self._in_heading = True
        if tag in _BLOCK_TAGS:
            self._chunks.append('\n')

    def handle_startendtag(self, tag, attrs):
        if not self._skip_depth and tag in _BLOCK_TAGS:
            self._chunks.append('\n')

    def handle_endtag(self, tag):
        if self._skip_depth:
            if tag not in _VOID_TAGS:
                self._skip_depth -= 1
            return
        if tag == 'title':
            self._in_title = False
        elif tag == 'h1':
            self._in_heading = False
        if tag in _BLOCK_TAGS:
            self._chunks.append('\n')

    def handle_data(self, data):
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data
            return
        if self._in_heading:
            self.heading += data
        self._chunks.append(data)

    def body_text(self):
        lines = []
        for chunk in ''.join(self._chunks).split('\n'):
            line = ' '.join(chunk.split())
            if line:
                lines.append(line)
        return '\n'.join(lines)


def _extract_readable(body_text, content_type):
    if content_type == 'text/plain':
        lines = [' '.join(line.split()) for line in body_text.split('\n')]
        return '', '\n'.join(line for line in lines if line)
    parser = _TextExtractor()
    try:
        parser.feed(body_text)
        parser.close()
    except Exception:
        pass
    title = ' '.join(parser.title.split()) or ' '.join(parser.heading.split())
    return title[:300], parser.body_text()


def _looks_like_login(content):
    lowered = (content or '').lower()
    for marker in LOGIN_MARKERS:
        if marker in lowered or marker in content:
            return True
    return False


def _fetch_public(url, timeout, max_bytes):
    """真正发起请求；任何失败都抛 _FetchError（文案安全，不带本机信息）。"""
    current = url
    redirects = 0
    while True:
        shape = _validate_url_shape(current)
        pinned_ip = _resolve_public_ip(shape['host'], shape['port'])
        connection = _connect(shape['scheme'], shape['host'], shape['port'],
                              pinned_ip, timeout)
        try:
            connection.request('GET', shape['target'], headers={
                'Host': shape['host_header'],
                'Accept': 'text/html,application/xhtml+xml,text/plain;q=0.9',
                'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
                'Connection': 'close',
            })
            response = connection.getresponse()
            status = int(getattr(response, 'status', 0))
            if status in REDIRECT_STATUSES:
                location = response.getheader('Location')
                if redirects >= MAX_REDIRECTS:
                    raise _FetchError('重定向次数过多')
                if not location:
                    raise _FetchError('重定向缺少 Location')
                redirects += 1
                current = urljoin(current, location.strip())
                continue
            if status != 200:
                raise _FetchError('目标返回 HTTP %d' % status)
            content_type, charset = _parse_content_type(response.getheader('Content-Type'))
            if content_type not in ALLOWED_CONTENT_TYPES:
                raise _FetchError('不支持的内容类型：%s' % (content_type or '未知'))
            declared = _parse_length(response.getheader('Content-Length'))
            if declared is not None and declared > max_bytes:
                raise _FetchError('响应体超过大小限制')
            body = _read_limited(response, max_bytes)
        finally:
            _close(connection)
        break

    text = _decode_body(body, charset)
    title, content = _extract_readable(text, content_type)
    content = content[:MAX_CONTENT_CHARS]
    if _looks_like_login(content):
        raise _FetchError('该页面需要登录或为付费内容',
                          {'title': title, 'content': content})
    if len(content.strip()) < MIN_CONTENT_CHARS:
        raise _FetchError('无法提取正文内容', {'title': title, 'content': content})
    summary = ' '.join(content.split())[:MAX_SUMMARY_CHARS]
    return {'title': title, 'summary': summary, 'content': content}


def fetch_public_page(url, timeout=6, max_bytes=1000000):
    """读取公共网页正文，返回 {kind,title,summary,content,status,url[,error]}。

    失败一律返回 status='failed' 的可读对象，不抛异常、不泄露本机路径与环境。
    content 是纯文本（前端请用 textContent 渲染）。
    """
    result = {'kind': 'link', 'title': '', 'summary': '', 'content': '',
              'status': 'failed', 'url': url if isinstance(url, str) else ''}
    try:
        data = _fetch_public(url, timeout, max_bytes)
    except _FetchError as error:
        result['error'] = str(error)
        for key, value in (error.payload or {}).items():
            result[key] = value
    except Exception:
        result['error'] = '请求失败（网络或解析错误）'
    else:
        result.update(data)
        result['status'] = 'ready'
    return result
