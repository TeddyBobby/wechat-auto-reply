# -*- coding: utf-8 -*-
"""assistant_media 的契约测试（stdlib only，全程不碰真实网络/配置/微信）。

覆盖：本地图片上传与缓存路径安全、OneBot 消息归一化、公共网页正文读取（SSRF 防护）。
运行： /usr/bin/python3 tests/test_assistant_media.py
"""
import base64
import importlib.util
import json
import re
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parents[1]
MODULE_PATH = ROOT / 'assistant_media.py'

# 只做 magic 校验，字节内容可以精简（真实校验只看头部魔数）
PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 40
JPEG = b'\xff\xd8\xff\xe0' + b'\x00' * 40
WEBP = b'RIFF' + (40).to_bytes(4, 'little') + b'WEBP' + b'VP8 ' + b'\x00' * 32
GIF = b'GIF89a' + b'\x00' * 40
SVG = b'<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg"></svg>'

# 合成 OneBot 图片消息 XML（不包含真实媒体标识）：<img> 是不闭合的，安全字段 img_file_name 才可信
IMG_XML = ('<?xml version="1.0"?>\n<msg>\n\t<img aeskey="abc" encryver="1" length="21400" '
           'md5="0123456789abcdef0123456789abcdef">\n'
           '\t<img_file_name>photo.png</img_file_name>\n</msg>')


def load_module():
    if not MODULE_PATH.exists():
        return None
    spec = importlib.util.spec_from_file_location('assistant_media', MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def b64(data):
    return base64.b64encode(data).decode('ascii')


class CDataShareTests(unittest.TestCase):
    def test_wechat_cdata_title_and_url_are_xml_values_not_literal_markup(self):
        module=load_module()
        with tempfile.TemporaryDirectory() as directory:
            store=module.MediaStore(directory)
            normalized=module.normalize_message({'raw_message':'<msg><appmsg><type>5</type><title><![CDATA[测试 & 链接]]></title><url><![CDATA[https://example.com/a?x=1&y=2]]></url></appmsg></msg>','message':[{'type':'share','data':{}}]},store)
            self.assertEqual(normalized['links'],['https://example.com/a?x=1&y=2'])
            self.assertIn('测试 & 链接',normalized['text'])
            self.assertNotIn('CDATA',normalized['text'])

class SpecialIPv6Tests(unittest.TestCase):
    def test_transition_and_deprecated_local_ranges_cannot_bypass_ssrf_filter(self):
        import ipaddress
        module=load_module()
        for address in ('::127.0.0.1','fec0::1','2002:7f00:1::','2002:c0a8:101::','64:ff9b::a00:1','64:ff9b:1::a00:1'):
            with self.subTest(address=address):
                self.assertFalse(module._is_public_ip(ipaddress.ip_address(address)))
        self.assertTrue(module._is_public_ip(ipaddress.ip_address('2606:4700:4700::1111')))
        self.assertTrue(module._is_public_ip(ipaddress.ip_address('8.8.8.8')))

class MediaTestBase(unittest.TestCase):
    """临时目录当作 home：home/onebot/image 是默认允许的入向目录。"""

    def setUp(self):
        self.module = load_module()
        self.assertIsNotNone(self.module, 'assistant_media.py 尚未实现')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'home'
        self.inbox = self.home / 'onebot' / 'image'
        self.inbox.mkdir(parents=True)
        self.store = self.module.MediaStore(self.home)

    def fixture(self, name, data=PNG, directory=None):
        path = (directory or self.inbox) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def stored_files(self):
        return sorted(p.name for p in (self.home / 'uploads').iterdir()
                      if p.name != '.index.json')


# --------------------------------------------------------------------------------------
# 上传：严格 base64 + magic 校验 + 私有权限 + 不信 filename
# --------------------------------------------------------------------------------------
class UploadTests(MediaTestBase):
    def test_upload_png_returns_contract_asset(self):
        asset = self.store.upload('photo.png', b64(PNG))
        self.assertEqual(set(asset.keys()), {'id', 'name', 'mime', 'kind', 'url'})
        self.assertRegex(asset['id'], r'^[0-9a-f]{32}$')
        self.assertEqual(asset['name'], 'photo.png')
        self.assertEqual(asset['mime'], 'image/png')
        self.assertEqual(asset['kind'], 'image')
        self.assertEqual(asset['url'], '/api/media?id=' + asset['id'])

    def test_upload_accepts_jpeg_and_webp(self):
        self.assertEqual(self.store.upload('a.jpg', b64(JPEG))['mime'], 'image/jpeg')
        self.assertEqual(self.store.upload('b.webp', b64(WEBP))['mime'], 'image/webp')

    def test_upload_writes_private_file_and_directory(self):
        asset = self.store.upload('photo.png', b64(PNG))
        path = self.home / 'uploads' / asset['id']
        self.assertTrue(path.is_file())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.home / 'uploads').stat().st_mode & 0o777, 0o700)
        self.assertEqual(path.read_bytes(), PNG)

    def test_upload_uses_random_id_path_and_ignores_extension(self):
        asset = self.store.upload('weird.exe', b64(PNG))
        self.assertEqual(self.stored_files(), [asset['id']])
        self.assertFalse((self.home / 'uploads' / 'weird.exe').exists())

    def test_upload_strips_path_and_limits_name_to_120(self):
        asset = self.store.upload('../../etc/passwd.png', b64(PNG))
        self.assertEqual(asset['name'], 'passwd.png')
        long_name = 'x' * 300 + '.png'
        self.assertEqual(len(self.store.upload(long_name, b64(PNG))['name']), 120)

    def test_upload_mime_comes_from_magic_not_extension(self):
        asset = self.store.upload('liar.png', b64(JPEG))
        self.assertEqual(asset['mime'], 'image/jpeg')

    def test_upload_rejects_strict_base64_violations(self):
        for bad in ('', 'IQ!!', 'IQ=', 'IQ==\n', 'not base64 at all'):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    self.store.upload('x.png', bad)

    def test_upload_rejects_unsupported_formats(self):
        for data in (GIF, SVG):
            with self.subTest(head=data[:8]):
                with self.assertRaises(ValueError):
                    self.store.upload('x.gif', b64(data))

    def test_upload_rejects_oversize(self):
        self.assertEqual(self.module.MAX_UPLOAD_BYTES, 8 * 1024 * 1024)
        with mock.patch.object(self.module, 'MAX_UPLOAD_BYTES', 64):
            with self.assertRaises(ValueError):
                self.store.upload('big.png', b64(PNG + b'\x00' * 80))

    def test_resolve_returns_path_mime_and_filename(self):
        asset = self.store.upload('photo.png', b64(PNG))
        path, mime, name = self.store.resolve(asset['id'])
        self.assertIsInstance(path, Path)
        self.assertEqual(path.read_bytes(), PNG)
        self.assertEqual(mime, 'image/png')
        self.assertEqual(name, 'photo.png')

    def test_resolve_rejects_traversal_and_unknown_ids(self):
        self.store.upload('photo.png', b64(PNG))
        for bad in ('../../etc/passwd', '..', 'a' * 31, 'A' * 32, 'ZZZZ', 'x' * 32):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    self.store.resolve(bad)
        with self.assertRaises(FileNotFoundError):
            self.store.resolve('0' * 32)

    def test_resolve_survives_restart(self):
        asset = self.store.upload('photo.png', b64(PNG))
        reopened = self.module.MediaStore(self.home)
        path, mime, name = reopened.resolve(asset['id'])
        self.assertEqual(name, 'photo.png')
        self.assertEqual(mime, 'image/png')
        self.assertEqual(path.read_bytes(), PNG)

    def test_resolve_rejects_tampered_file(self):
        asset = self.store.upload('photo.png', b64(PNG))
        (self.home / 'uploads' / asset['id']).write_bytes(GIF)
        with self.assertRaises(ValueError):
            self.store.resolve(asset['id'])


# --------------------------------------------------------------------------------------
# 本地导入：只允许 home/onebot/image + 显式 allowed_roots，防符号链接逃逸
# --------------------------------------------------------------------------------------
class ImportLocalTests(MediaTestBase):
    def test_import_local_file_uri_and_plain_path(self):
        path = self.fixture('a.jpg', JPEG)
        asset = self.store.import_local(path.as_uri())
        self.assertEqual(asset['mime'], 'image/jpeg')
        self.assertEqual(asset['name'], 'a.jpg')
        self.assertEqual(asset['url'], '/api/media?id=' + asset['id'])
        # 原件不动，副本进受控 uploads
        self.assertTrue(path.exists())
        self.assertEqual((self.home / 'uploads' / asset['id']).read_bytes(), JPEG)

        plain = self.store.import_local(str(self.fixture('b.png', PNG)))
        self.assertEqual(plain['mime'], 'image/png')

    def test_import_local_outside_allowed_roots_rejected(self):
        outside = self.fixture('x.png', PNG, directory=self.root / 'elsewhere')
        with self.assertRaises(ValueError):
            self.store.import_local(outside.as_uri())
        with self.assertRaises(ValueError):
            self.store.import_local(str(self.home / 'config.json'))

    def test_import_local_symlink_escape_rejected(self):
        secret = self.fixture('secret.png', PNG, directory=self.root / 'secret')
        link = self.inbox / 'link.png'
        link.symlink_to(secret)
        with self.assertRaises(ValueError):
            self.store.import_local(str(link))

    def test_import_local_missing_and_directory_rejected(self):
        with self.assertRaises(FileNotFoundError):
            self.store.import_local(str(self.inbox / 'nope.png'))
        with self.assertRaises(ValueError):
            self.store.import_local(str(self.inbox))

    def test_import_local_oversize_rejected(self):
        with mock.patch.object(self.module, 'MAX_UPLOAD_BYTES', 32):
            with self.assertRaises(ValueError):
                self.store.import_local(str(self.fixture('big.png', PNG)))

    def test_import_local_rejects_non_image_bytes(self):
        with self.assertRaises(ValueError):
            self.store.import_local(str(self.fixture('anim.gif', GIF)))

    def test_import_local_rejects_remote_file_uri_and_other_schemes(self):
        for bad in ('file://evil.example.com/etc/passwd',
                    'file://192.168.1.9/etc/passwd',
                    'http://example.com/a.png',
                    'ftp://example.com/a.png'):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    self.store.import_local(bad)

    def test_import_local_dedupes_by_sha256(self):
        path = self.fixture('dup.png', PNG)
        first = self.store.import_local(str(path))
        second = self.store.import_local(str(path))
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(self.stored_files(), [first['id']])

    def test_default_root_is_inbox_only_and_explicit_roots_extend(self):
        # 默认不扫描微信整个容器：home 下别的目录要显式给出才允许
        other = self.fixture('o.png', PNG, directory=self.home / 'other')
        with self.assertRaises(ValueError):
            self.store.import_local(str(other))
        store = self.module.MediaStore(self.home, allowed_roots=[self.home / 'other'])
        self.assertEqual(store.import_local(str(other))['mime'], 'image/png')
        # 显式根之外仍然拒绝
        with self.assertRaises(ValueError):
            store.import_local(str(self.root / 'secret' / 'secret.png'))


# --------------------------------------------------------------------------------------
# 消息归一化：文本 / 图片 / 链接 / 文件 / 表情 / 系统
# --------------------------------------------------------------------------------------
class NormalizeTests(MediaTestBase):
    def normalize(self, data):
        return self.module.normalize_message(data, self.store)

    def assert_contract(self, result):
        self.assertEqual(set(result.keys()),
                         {'text', 'kind', 'images', 'links', 'notices', 'skip'})
        self.assertIsInstance(result['text'], str)
        self.assertIsInstance(result['images'], list)
        self.assertIsInstance(result['links'], list)
        self.assertIsInstance(result['notices'], list)
        self.assertIsInstance(result['skip'], bool)
        for notice in result['notices']:
            self.assertIn(notice['status'], ('ready', 'failed', 'unsupported'))
            for key in ('kind', 'title', 'summary', 'status'):
                self.assertIn(key, notice)
        return result

    def test_plain_text_preserved_with_newlines(self):
        result = self.assert_contract(self.normalize({
            'message_type': 'private', 'user_id': 'u1', 'sender': {'nickname': '甲'},
            'raw_message': '第一行\n\n第二行  保留空格',
            'message': [{'type': 'text', 'data': {'text': '第一行\n\n第二行  保留空格'}}]}))
        self.assertEqual(result['text'], '第一行\n\n第二行  保留空格')
        self.assertEqual(result['kind'], 'text')
        self.assertFalse(result['skip'])
        self.assertEqual(result['images'], [])
        self.assertEqual(result['links'], [])

    def test_empty_message_is_skipped(self):
        result = self.assert_contract(self.normalize({
            'message_type': 'private', 'user_id': 'u1', 'raw_message': '', 'message': []}))
        self.assertTrue(result['skip'])
        self.assertEqual(result['kind'], 'unsupported')
        self.assertEqual(result['text'], '')

    def test_group_member_prefix_preserved_but_xml_never_becomes_text(self):
        path = self.fixture('g.png', PNG)
        raw = 'wxid_group_member_fixture:\n' + IMG_XML
        result = self.assert_contract(self.normalize({
            'message_type': 'group', 'group_id': 'g1', 'user_id': 'u2',
            'sender': {'nickname': '群名'},
            'raw_message': raw,
            'message': [{'type': 'image', 'data': {'text': IMG_XML, 'url': path.as_uri()}}]}))
        self.assertTrue(result['text'].startswith('wxid_group_member_fixture:'))
        self.assertNotIn('<msg', result['text'])
        self.assertNotIn('aeskey', result['text'])
        self.assertLessEqual(len(result['text'].splitlines()[0]), 64)
        self.assertEqual(result['kind'], 'image')
        self.assertEqual(len(result['images']), 1)

    def test_image_segment_imports_local_file(self):
        path = self.fixture('pic.jpg', JPEG)
        result = self.assert_contract(self.normalize({
            'message_type': 'private', 'user_id': 'u1',
            'raw_message': IMG_XML,
            'message': [{'type': 'image', 'data': {'text': IMG_XML, 'url': path.as_uri()}}]}))
        self.assertEqual(result['kind'], 'image')
        self.assertEqual(len(result['images']), 1)
        asset = result['images'][0]
        self.assertEqual(asset['mime'], 'image/jpeg')
        self.assertEqual(asset['kind'], 'image')
        self.assertEqual(asset['name'], 'photo.png')          # 来自安全字段
        self.assertEqual(self.store.resolve(asset['id'])[1], 'image/jpeg')
        self.assertFalse(result['skip'])

    def test_image_name_from_safe_field_is_sanitised(self):
        xml = ('<msg><img aeskey="k"><img_file_name>../../evil.png</img_file_name></img></msg>')
        path = self.fixture('real.jpg', JPEG)
        result = self.assert_contract(self.normalize({
            'message_type': 'private', 'user_id': 'u1', 'raw_message': xml,
            'message': [{'type': 'image', 'data': {'text': xml, 'url': path.as_uri()}}]}))
        self.assertEqual(result['images'][0]['name'], 'evil.png')

    def test_image_name_falls_back_to_msgsource_safe_field(self):
        # 真实适配器把图片名放在 msgsource 的 <img_file_name>（安全字段）里
        xml = '<msg><img aeskey="k"></img></msg>'
        path = self.fixture('real_name.jpg', JPEG)
        result = self.assert_contract(self.normalize({
            'message_type': 'private', 'user_id': 'u1', 'raw_message': xml,
            'msgsource': '<msgsource><img_file_name>56ced651-cc8c.png</img_file_name></msgsource>',
            'message': [{'type': 'image', 'data': {'text': xml, 'url': path.as_uri()}}]}))
        self.assertEqual(result['images'][0]['name'], '56ced651-cc8c.png')

    def test_image_name_ignores_unsafe_msgsource(self):
        xml = '<msg><img aeskey="k"></img></msg>'
        path = self.fixture('real_name.jpg', JPEG)
        result = self.assert_contract(self.normalize({
            'message_type': 'private', 'user_id': 'u1', 'raw_message': xml,
            'msgsource': '<!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]>'
                         '<msgsource><img_file_name>evil.png</img_file_name></msgsource>',
            'message': [{'type': 'image', 'data': {'text': xml, 'url': path.as_uri()}}]}))
        self.assertEqual(result['images'][0]['name'], 'real_name.jpg')   # 退回真实文件名

    def test_image_outside_allowed_root_is_failed_not_faked(self):
        outside = self.fixture('o.png', PNG, directory=self.root / 'elsewhere')
        result = self.assert_contract(self.normalize({
            'message_type': 'private', 'user_id': 'u1', 'raw_message': IMG_XML,
            'message': [{'type': 'image', 'data': {'text': IMG_XML, 'url': outside.as_uri()}}]}))
        self.assertEqual(result['kind'], 'image')
        self.assertEqual(result['images'], [])
        self.assertEqual(result['notices'][0]['status'], 'failed')
        self.assertIn('error', result['notices'][0])
        self.assertIn('读取失败', result['text'])       # 诚实提示，不假装已识别
        self.assertNotIn('已识别', result['text'])

    def test_image_xml_without_url_is_failed_not_faked(self):
        result = self.assert_contract(self.normalize({
            'message_type': 'private', 'user_id': 'u1', 'raw_message': IMG_XML, 'message': []}))
        self.assertEqual(result['kind'], 'image')
        self.assertEqual(result['images'], [])
        self.assertEqual(result['notices'][0]['status'], 'failed')
        self.assertNotIn('<msg', result['text'])
        self.assertNotIn('aeskey', result['text'])

    def test_doctype_and_entity_rejected(self):
        xml = ('<?xml version="1.0"?><!DOCTYPE msg [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
               '<msg><appmsg><title>&xxe;</title><des>d</des><type>5</type>'
               '<url>https://example.com/a</url></appmsg></msg>')
        result = self.assert_contract(self.normalize({
            'message_type': 'private', 'user_id': 'u1', 'raw_message': xml,
            'message': [{'type': 'share', 'data': {'text': xml}}]}))
        self.assertEqual(result['kind'], 'link')
        self.assertEqual(result['links'], [])
        self.assertEqual(result['notices'][0]['status'], 'failed')
        self.assertNotIn('root:', result['text'])
        self.assertNotIn('file:///', result['text'])

    def test_share_appmsg_type5_becomes_readable_link(self):
        xml = ('<?xml version="1.0"?><msg><appmsg appid="" sdkver="0">'
               '<title>一个标题</title><des>一段描述</des><type>5</type>'
               '<url>https://example.com/article</url></appmsg></msg>')
        result = self.assert_contract(self.normalize({
            'message_type': 'private', 'user_id': 'u1', 'raw_message': xml,
            'message': [{'type': 'share', 'data': {'text': xml}}]}))
        self.assertEqual(result['kind'], 'link')
        self.assertEqual(result['links'], ['https://example.com/article'])
        self.assertIn('一个标题', result['text'])
        self.assertIn('一段描述', result['text'])
        self.assertIn('https://example.com/article', result['text'])
        self.assertNotIn('<appmsg', result['text'])
        self.assertNotIn('<title', result['text'])
        self.assertFalse(result['skip'])

    def test_quote_appmsg_type57_keeps_title(self):
        xml = ('<msg><appmsg><title>被引用的原话</title><des>原话描述</des>'
               '<type>57</type><refermsg><title>更早的引用</title></refermsg></appmsg></msg>')
        result = self.assert_contract(self.normalize({
            'message_type': 'private', 'user_id': 'u1', 'raw_message': xml,
            'message': [{'type': 'share', 'data': {'text': xml}}]}))
        self.assertEqual(result['kind'], 'text')
        self.assertIn('被引用的原话', result['text'])
        self.assertNotIn('<msg', result['text'])

    def test_text_links_extracted_and_chinese_punctuation_stripped(self):
        text = '看这个 https://example.com/a?x=1，还有（https://example.com/b）。'
        result = self.assert_contract(self.normalize({
            'message_type': 'private', 'user_id': 'u1', 'raw_message': text,
            'message': [{'type': 'text', 'data': {'text': text}}]}))
        self.assertEqual(result['kind'], 'text')
        self.assertEqual(result['links'],
                         ['https://example.com/a?x=1', 'https://example.com/b'])
        self.assertEqual(result['text'], text)      # 文本原样保留

    def test_local_file_uri_never_becomes_a_link(self):
        text = '本地 file:///etc/passwd 和 ftp://example.com/x 都别抓'
        result = self.assert_contract(self.normalize({
            'message_type': 'private', 'user_id': 'u1', 'raw_message': text,
            'message': [{'type': 'text', 'data': {'text': text}}]}))
        self.assertEqual(result['links'], [])

    def test_duplicate_links_deduped(self):
        text = 'https://example.com/a https://example.com/a'
        result = self.assert_contract(self.normalize({
            'message_type': 'private', 'user_id': 'u1', 'raw_message': text,
            'message': [{'type': 'text', 'data': {'text': text}}]}))
        self.assertEqual(result['links'], ['https://example.com/a'])

    def test_file_message_reads_safe_name_but_not_content(self):
        xml = ('<msg><appmsg><title>报告.pdf</title><des>请查收</des><type>6</type>'
               '<appattach><totallen>2048</totallen><filename>report.pdf</filename>'
               '</appattach></appmsg></msg>')
        result = self.assert_contract(self.normalize({
            'message_type': 'private', 'user_id': 'u1', 'raw_message': xml,
            'message': [{'type': 'file', 'data': {'text': xml, 'file': 'report.pdf'}}]}))
        self.assertEqual(result['kind'], 'file')
        notice = result['notices'][0]
        self.assertEqual(notice['kind'], 'file')
        self.assertEqual(notice['status'], 'unsupported')
        self.assertEqual(notice['title'], 'report.pdf')
        self.assertIn('内容未读取', notice['summary'])
        self.assertIn('请查收', notice['summary'])
        self.assertFalse(result['skip'])

    def test_face_is_unsupported_and_not_an_image(self):
        xml = '<msg><emoji fromusername="wxid_a" type="2" md5="b9050d3c"/></msg>'
        result = self.assert_contract(self.normalize({
            'message_type': 'private', 'user_id': 'u1', 'raw_message': xml,
            'message': [{'type': 'face', 'data': {'text': xml}}]}))
        self.assertEqual(result['kind'], 'unsupported')
        self.assertEqual(result['images'], [])
        self.assertEqual(result['notices'][0]['status'], 'unsupported')
        self.assertFalse(result['skip'])

    def test_system_message_is_skipped(self):
        for data in ({'message_type': 'system', 'user_id': 'u1', 'raw_message': 'x',
                      'message': [{'type': 'sys', 'data': {}}]},
                     {'message_type': 'private', 'user_id': 'u1', 'raw_message': '<sysmsg>x</sysmsg>',
                      'message': [{'type': 'sys', 'data': {}}]}):
            result = self.assert_contract(self.normalize(data))
            self.assertTrue(result['skip'])
            self.assertEqual(result['kind'], 'system')

    def test_unknown_shapes_raise_value_error_instead_of_crashing(self):
        for data in ('not a dict', 42, None,
                     {'message': 'not a list'},
                     {'message': [{'type': 'text'}]},
                     {'message': ['nope']},
                     {'message': [{'type': 'text', 'data': 'nope'}]}):
            with self.subTest(value=data):
                with self.assertRaises(ValueError):
                    self.normalize(data)

    def test_unknown_segment_type_is_reported_not_crashed(self):
        result = self.assert_contract(self.normalize({
            'message_type': 'private', 'user_id': 'u1', 'raw_message': 'hi',
            'message': [{'type': 'text', 'data': {'text': 'hi'}},
                        {'type': 'mystery', 'data': {'a': 1}}]}))
        self.assertEqual(result['kind'], 'text')
        self.assertEqual(result['text'], 'hi')

    def test_multiple_images_all_imported(self):
        one = self.fixture('one.png', PNG)
        two = self.fixture('two.jpg', JPEG)
        result = self.assert_contract(self.normalize({
            'message_type': 'private', 'user_id': 'u1', 'raw_message': IMG_XML,
            'message': [{'type': 'image', 'data': {'text': IMG_XML, 'url': one.as_uri()}},
                        {'type': 'image', 'data': {'text': IMG_XML, 'url': two.as_uri()}}]}))
        self.assertEqual(len(result['images']), 2)
        self.assertEqual([i['mime'] for i in result['images']], ['image/png', 'image/jpeg'])
        self.assertEqual(result['kind'], 'image')


# --------------------------------------------------------------------------------------
# 网页正文读取：SSRF / 重定向 / 体积 / 内容类型 / 诚实失败
# --------------------------------------------------------------------------------------
class FakeResponse(object):
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self._body = body
        self._read = 0

    def getheader(self, name, default=None):
        return self.headers.get(name.lower(), default)

    def read(self, amount=None):
        if amount is None:
            amount = len(self._body) - self._read
        chunk = self._body[self._read:self._read + amount]
        self._read += len(chunk)
        return chunk

    def close(self):
        pass


class FakeConnection(object):
    """替代 http.client 的连接：不做任何真实网络 IO。"""

    def __init__(self, scheme, host, port, pinned_ip, timeout, routes, record):
        self.scheme = scheme
        self.host = host
        self.port = port
        self.pinned_ip = pinned_ip
        self.timeout = timeout
        self._routes = routes
        self._record = record
        self._response = None
        self.closed = False

    def request(self, method, target, body=None, headers=None):
        headers = {k.lower(): v for k, v in (headers or {}).items()}
        self._record.append({'scheme': self.scheme, 'host': self.host, 'port': self.port,
                             'pinned_ip': self.pinned_ip, 'timeout': self.timeout,
                             'method': method, 'target': target, 'headers': headers})
        entry = self._routes.get(target)
        if entry is None:
            raise AssertionError('unexpected request target: %s' % target)
        if isinstance(entry, Exception):
            raise entry
        self._response = FakeResponse(*entry)

    def getresponse(self):
        return self._response

    def close(self):
        self.closed = True


class FetchBase(MediaTestBase):
    PUBLIC = '93.184.216.34'

    def setUp(self):
        super().setUp()
        self.record = []
        self.routes = {}

    def fake_getaddrinfo(self, ips=None):
        ips = ips or [self.PUBLIC]

        def resolve(host, port, *args, **kwargs):
            out = []
            for ip in ips:
                family = socket.AF_INET6 if ':' in ip else socket.AF_INET
                out.append((family, socket.SOCK_STREAM, 6, '', (ip, port)))
            return out
        return resolve

    def fetch(self, url, routes=None, ips=None, timeout=6, max_bytes=None):
        self.routes.update(routes or {})

        def connect(scheme, host, port, pinned_ip, timeout):
            return FakeConnection(scheme, host, port, pinned_ip, timeout,
                                  self.routes, self.record)

        patchers = [mock.patch.object(self.module, '_connect', connect),
                    mock.patch.object(self.module.socket, 'getaddrinfo',
                                      self.fake_getaddrinfo(ips))]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)
        if max_bytes is None:
            return self.module.fetch_public_page(url, timeout=timeout)
        return self.module.fetch_public_page(url, timeout=timeout, max_bytes=max_bytes)

    @staticmethod
    def html(body, title='页面标题'):
        return ('<html><head><title>%s</title></head><body>%s</body></html>'
                % (title, body)).encode('utf-8')

    BODY = ('本文是第一段正文，讲了一件事。\n' * 8)

    def ok_routes(self, body=None, title='页面标题', headers=None):
        head = {'Content-Type': 'text/html; charset=utf-8'}
        head.update(headers or {})
        return {'/': (200, head, self.html(body or self.BODY, title))}


class FetchSuccessTests(FetchBase):
    def test_ready_result_extracts_title_and_body(self):
        result = self.fetch('https://example.com/', self.ok_routes())
        self.assertEqual(set(result.keys()),
                         {'kind', 'title', 'summary', 'content', 'status', 'url'})
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['kind'], 'link')
        self.assertEqual(result['title'], '页面标题')
        self.assertIn('第一段正文', result['content'])
        self.assertEqual(result['url'], 'https://example.com/')
        self.assertLessEqual(len(result['summary']), 600)
        self.assertLessEqual(len(result['content']), 20000)
        self.assertNotIn('<', result['content'])        # 返回纯文本，不是 HTML

    def test_only_one_connection_per_fetch(self):
        self.fetch('https://example.com/', self.ok_routes())
        self.assertEqual(len(self.record), 1)

    def test_query_string_and_path_are_requested(self):
        routes = {'/a/b?x=1': (200, {'Content-Type': 'text/html; charset=utf-8'},
                               self.html(self.BODY))}
        self.fetch('https://example.com/a/b?x=1', routes)
        self.assertEqual(self.record[0]['target'], '/a/b?x=1')

    def test_connection_pins_verified_ip_and_keeps_hostname(self):
        self.fetch('https://example.com/', self.ok_routes())
        call = self.record[0]
        self.assertEqual(call['pinned_ip'], self.PUBLIC)      # 连接钉住已校验的公网 IP
        self.assertEqual(call['host'], 'example.com')         # TLS SNI / Host 仍用原域名
        self.assertEqual(call['headers']['host'], 'example.com')
        self.assertNotIn('user-agent', call['headers'])       # 不发 UA / cookie
        self.assertNotIn('cookie', call['headers'])

    def test_pinned_connection_dials_only_the_verified_ip(self):
        with mock.patch.object(self.module.socket, 'create_connection') as dial:
            dial.return_value = mock.Mock()
            conn = self.module._PinnedHTTPConnection('example.com', 80, self.PUBLIC, 5)
            conn._create_connection(('example.com', 80), 5, None)
        # 实际拨号用的是已校验 IP，域名只留给 Host/SNI
        self.assertEqual(dial.call_args[0][0], (self.PUBLIC, 80))
        self.assertEqual(conn.host, 'example.com')
        self.assertIsInstance(self.module._connect('http', 'example.com', 80, self.PUBLIC, 5),
                              self.module._PinnedHTTPConnection)
        self.assertIsInstance(self.module._connect('https', 'example.com', 443, self.PUBLIC, 5),
                              self.module._PinnedHTTPSConnection)

    def test_timeout_is_forwarded(self):
        self.fetch('https://example.com/', self.ok_routes(), timeout=3)
        self.assertEqual(self.record[0]['timeout'], 3)

    def test_strips_script_style_nav_footer_and_ads(self):
        body = ('<html><head><title>T</title><style>body{color:red}</style>'
                '<script>alert("x")</script></head><body>'
                '<nav>导航项</nav><div class="ad-slot">广告位</div>'
                '<footer>版权页脚</footer><article><p>真正的正文内容在这里，够长了。</p>'
                '<p>第二段也在这里。</p></article></body></html>').encode('utf-8')
        routes = {'/': (200, {'Content-Type': 'text/html; charset=utf-8'}, body)}
        result = self.fetch('https://example.com/', routes)
        self.assertEqual(result['status'], 'ready')
        self.assertIn('真正的正文内容', result['content'])
        for noise in ('alert', '导航项', '广告位', '版权页脚', 'color:red'):
            self.assertNotIn(noise, result['content'])

    def test_decodes_html_entities(self):
        routes = self.ok_routes(body='A &amp; B 的正文内容，&lt;标签&gt; 要还原成字符。')
        result = self.fetch('https://example.com/', routes)
        self.assertIn('A & B', result['content'])
        self.assertIn('<标签>', result['content'])

    def test_truncates_long_content_and_summary(self):
        routes = self.ok_routes(body='正' * 30000)
        result = self.fetch('https://example.com/', routes)
        self.assertEqual(result['status'], 'ready')
        self.assertLessEqual(len(result['content']), 20000)
        self.assertLessEqual(len(result['summary']), 600)

    def test_plain_text_content_type_allowed(self):
        routes = {'/': (200, {'Content-Type': 'text/plain; charset=utf-8'},
                        ('纯文本正文，没有 HTML 标签。' * 5).encode('utf-8'))}
        result = self.fetch('https://example.com/', routes)
        self.assertEqual(result['status'], 'ready')
        self.assertIn('纯文本正文', result['content'])


class FetchSsrfTests(FetchBase):
    def assert_failed(self, result):
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['kind'], 'link')
        self.assertIn('error', result)
        for key in ('title', 'summary', 'content', 'url'):
            self.assertIn(key, result)

    def test_rejects_private_loopback_linklocal_multicast_reserved(self):
        blocked = ['http://localhost/', 'http://127.0.0.1/', 'http://127.1.2.3/',
                   'http://10.0.0.1/', 'http://172.16.9.9/', 'http://192.168.1.1/',
                   'http://169.254.169.254/latest/meta-data/', 'http://0.0.0.0/',
                   'http://100.64.0.1/', 'http://198.18.0.1/', 'http://224.0.0.1/',
                   'http://240.0.0.1/', 'http://[::1]/', 'http://[fe80::1]/',
                   'http://[fc00::1]/', 'http://[ff02::1]/', 'http://foo.local/']
        for url in blocked:
            with self.subTest(url=url):
                result = self.fetch(url, self.ok_routes())
                self.assert_failed(result)
                self.assertEqual(self.record, [])          # 根本没发起连接

    def test_rejects_hostname_that_resolves_to_private(self):
        for ips in (['127.0.0.1'], ['10.1.2.3'], ['192.168.0.5'], ['169.254.169.254'],
                    ['93.184.216.34', '127.0.0.1']):     # 混合应答也拒绝
            with self.subTest(ips=ips):
                result = self.fetch('https://evil.example.com/', self.ok_routes(), ips=ips)
                self.assert_failed(result)
                self.assertEqual(self.record, [])

    def test_rejects_bad_scheme_credentials_and_ports(self):
        for url in ('ftp://example.com/a', 'file:///etc/passwd',
                    'http://user:pass@example.com/', 'http://example.com:8080/',
                    'https://example.com:22/', 'javascript:alert(1)', ''):
            with self.subTest(url=url):
                result = self.fetch(url, self.ok_routes())
                self.assert_failed(result)
                self.assertEqual(self.record, [])

    def test_redirect_to_private_target_is_blocked(self):
        routes = {'/': (302, {'Location': 'http://127.0.0.1/internal'}, b'')}
        result = self.fetch('https://example.com/', routes)
        self.assert_failed(result)
        self.assertEqual(len(self.record), 1)              # 私网那一跳没发出去

    def test_follows_at_most_three_redirects(self):
        routes = {
            '/': (302, {'Location': 'https://example.com/1'}, b''),
            '/1': (302, {'Location': 'https://example.com/2'}, b''),
            '/2': (302, {'Location': 'https://example.com/3'}, b''),
            '/3': (302, {'Location': 'https://example.com/4'}, b''),
            '/4': (200, {'Content-Type': 'text/html; charset=utf-8'}, self.html(self.BODY)),
        }
        result = self.fetch('https://example.com/', routes)
        self.assert_failed(result)
        self.assertEqual(len(self.record), 4)

    def test_two_redirects_still_succeed(self):
        routes = {
            '/': (302, {'Location': 'https://example.com/1'}, b''),
            '/1': (302, {'Location': 'https://example.com/2'}, b''),
            '/2': (200, {'Content-Type': 'text/html; charset=utf-8'}, self.html(self.BODY)),
        }
        result = self.fetch('https://example.com/', routes)
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(len(self.record), 3)
        self.assertEqual(result['url'], 'https://example.com/')


class FetchFailureTests(FetchBase):
    def assert_failed(self, result):
        self.assertEqual(result['status'], 'failed')
        self.assertIn('error', result)

    def test_rejects_binary_and_unsupported_content_types(self):
        for ctype in ('image/png', 'application/pdf', 'application/octet-stream',
                      'text/event-stream', 'application/json'):
            with self.subTest(ctype=ctype):
                routes = {'/': (200, {'Content-Type': ctype}, b'\x89PNG\r\n\x1a\n' + b'\x00' * 100)}
                self.assert_failed(self.fetch('https://example.com/', routes))

    def test_enforces_declared_and_actual_size_limits(self):
        routes = {'/': (200, {'Content-Type': 'text/html',
                              'Content-Length': str(5_000_000)}, b'x' * 100)}
        self.assert_failed(self.fetch('https://example.com/', routes))

        big = {'/': (200, {'Content-Type': 'text/html'}, self.html('正' * 5000))}
        self.assert_failed(self.fetch('https://example.com/', big, max_bytes=1000))

    def test_login_and_paywall_pages_fail_honestly(self):
        pages = ['<p>请登录后继续阅读本文章</p>',
                 '<p>Subscribe to read the full article</p>',
                 '<p>付费阅读，开通会员后查看</p>']
        for body in pages:
            with self.subTest(body=body):
                routes = {'/': (200, {'Content-Type': 'text/html; charset=utf-8'},
                                self.html(body, title='文章标题'))}
                result = self.fetch('https://example.com/', routes)
                self.assert_failed(result)
                self.assertNotEqual(result['content'], '')

    def test_page_without_body_fails_even_with_title(self):
        routes = {'/': (200, {'Content-Type': 'text/html; charset=utf-8'},
                        self.html('', title='只有标题'))}
        result = self.fetch('https://example.com/', routes)
        self.assert_failed(result)
        self.assertEqual(result['title'], '只有标题')

    def test_http_error_statuses_fail(self):
        for status in (301, 400, 403, 404, 429, 500, 503):
            with self.subTest(status=status):
                routes = {'/': (status, {'Content-Type': 'text/html'}, b'')}
                self.assert_failed(self.fetch('https://example.com/', routes))

    def test_errors_never_leak_local_paths_or_env(self):
        secret = '/Users/example-test-user/fixture/secret'
        for routes in ({'/': RuntimeError('boom %s' % secret)},
                       {'/': socket.timeout('timed out %s' % secret)},
                       {'/': ValueError('bad %s' % secret)}):
            with self.subTest(routes=routes):
                result = self.fetch('https://example.com/', routes)
                self.assert_failed(result)
                blob = json.dumps(result, ensure_ascii=False)
                self.assertNotIn('/Users/', blob)
                self.assertNotIn(secret, blob)
                self.assertNotIn('Traceback', blob)
                self.assertNotIn('example-test-user', blob)


if __name__ == '__main__':
    unittest.main(verbosity=2)
