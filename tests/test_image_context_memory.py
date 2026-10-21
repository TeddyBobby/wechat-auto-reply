"""Receive image -> persisted history -> text follow-up. No real model/send.
All imported sources come from this checkout.
"""
import base64
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SOURCE = ROOT
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aXioAAAAASUVORK5CYII=')


def load(name):
    spec = importlib.util.spec_from_file_location('image_memory_test_'+name, SOURCE/(name+'.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ImageContextMemoryChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        with patch.dict(os.environ, {'WECHAT_HOME': self.temp.name}):
            self.bridge = load('wechat_hermes_bridge')
        self.core = load('assistant_core')
        self.service = self.core.AssistantService(self.temp.name, vars(self.bridge))
        self.bridge.get_assistant_service = lambda: self.service
        self.service.settings.update({'image_enabled': True, 'context_enabled': True,
                                      'context_messages': 20, 'context_char_limit': 1000})
        self.calls = []
        def model(prompt, who, group, context, image_path, safe):
            self.calls.append({'prompt': prompt, 'image': image_path})
            return 'OFFLINE TEST ONLY'
        self.bridge.ask_hermes = model
        def forbidden(*a, **k):
            raise AssertionError('Never send in this test')
        self.bridge.dispatch_send = forbidden
        self.bridge.state.update(auto_reply=False, send_enabled=False)

    def receive(self, uid='friend_a', name='first.png', quality=None, own=False):
        path = self.service.media.inbox/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(PNG)
        data = {'user_id': uid, 'message_type': 'private', 'message_id': name,
                'message': [{'type': 'image', 'data': {'url': str(path),
                             **({'image_quality': quality} if quality else {})}}]}
        if own:
            self.bridge._process_own_sync(data, uid)
        else:
            self.bridge._process_incoming(data)
        return self.bridge.history[uid][-1]

    def test_newest_picture_replaces_old_picture_and_never_crosses_uid(self):
        first = self.receive()
        second = self.receive(name='second.png')
        # Import dedupes equal bytes; use a separate validated upload for a
        # genuinely distinct id in the same conversation.
        other = self.service.media.upload('other.png', base64.b64encode(PNG).decode())
        second['images'] = [{'id': other['id']}]
        self.service.generate('friend_a', '这张图呢？')
        self.assertEqual(self.calls[-1]['image'], str(self.service.media.resolve(other['id'])[0]))
        self.assertNotEqual(other['id'], first['images'][0]['id'])
        self.service.generate('friend_b', '图片是什么意思？')
        self.assertIsNone(self.calls[-1]['image'])
        # A new current attachment wins over historical ones, even with history disabled.
        self.service.settings.update({'context_enabled': False})
        self.service.generate('friend_a', '新图', images=first['images'])
        self.assertEqual(self.calls[-1]['image'], str(self.service.media.resolve(first['images'][0]['id'])[0]))

    def test_context_and_image_settings_are_respected(self):
        self.receive()
        self.bridge.record_turn('friend_a', 'user', '最近一条文字' + '字' * 1100)
        for settings in ({'context_enabled': False}, {'context_messages': 0},
                         {'context_messages': 1}, {'context_char_limit': 1000},
                         {'image_enabled': False}):
            with self.subTest(settings=settings):
                self.service.settings.update({'context_enabled': True, 'context_messages': 20,
                                              'context_char_limit': 2000, 'image_enabled': True, **settings})
                self.service.generate('friend_a', '什么意思？')
                self.assertIsNone(self.calls[-1]['image'])

    def test_missing_or_legacy_latest_picture_never_reuses_older_picture(self):
        self.receive()
        newer = self.receive(name='second.png')
        newer['images'] = [{'id': 'f'*32}]
        self.service.generate('friend_a', '图片是什么意思？')
        self.assertIsNone(self.calls[-1]['image'])
        self.assertIn('缺失', self.calls[-1]['prompt'])
        del newer['images']
        self.service.generate('friend_a', '图片是什么意思？')
        self.assertIsNone(self.calls[-1]['image'])
        self.assertIn('未保存可用附件', self.calls[-1]['prompt'])
        self.service.generate('friend_a', '新图读取失败', notices=[{'kind': 'image', 'status': 'failed'}])
        self.assertIsNone(self.calls[-1]['image'])

    def test_own_picture_keeps_attachment_and_thumbnail_warning(self):
        row = self.receive(quality='thumbnail', own=True)
        self.assertEqual(row['images'][0]['quality'], 'thumbnail')
        self.assertEqual(row['role'], 'assistant')
        self.assertEqual(self.calls, [])
        self.service.generate('friend_a', '上一张图是什么？')
        self.assertIsNotNone(self.calls[-1]['image'])
        self.assertIn('缩略图', self.calls[-1]['prompt'])

    def test_received_image_persists_and_text_followup_reattaches_it(self):
        row = self.receive()
        self.assertTrue(row.get('images'), 'image attachment lost at record_turn')
        self.assertEqual(self.calls, [])  # receiving with auto off never calls model
        self.assertNotIn('file:', json.dumps(row))
        rows = json.loads(Path(self.bridge.HISTORY_FILE).read_text())
        self.assertEqual(rows['friend_a'][0]['images'], row['images'])
        # Simulate a bridge/service restart: no in-memory last-image shortcut.
        service = self.core.AssistantService(self.temp.name, {**vars(self.bridge), 'history': rows})
        result = service.generate('friend_a', '刚才的图片是什么意思？')
        expected = str(service.media.resolve(row['images'][0]['id'])[0])
        self.assertEqual(self.calls[-1]['image'], expected)
        self.assertIn('历史图片', self.calls[-1]['prompt'])
        self.assertTrue(any(i.get('source') == 'history' for i in result['insights']))


if __name__ == '__main__':
    unittest.main()
