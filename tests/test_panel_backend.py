import importlib.util
import tempfile
import time
import unittest
from pathlib import Path

ROOT=Path(__file__).parents[1]
try:
    spec=importlib.util.spec_from_file_location('panel_backend', ROOT/'panel_backend.py')
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
except FileNotFoundError:
    module=None

class EventStoreTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(module,'persistent panel event store is not implemented')
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'events.db'
        self.store=module.EventStore(self.path)

    def test_log_survives_reopen_with_full_message(self):
        text='完整内容\n'+'测试消息'*200
        event_id=self.store.append('info','[私] 测试联系人: 摘要',kind='received',scope='private',uid='wxid_fixture',name='测试联系人',text=text)
        reopened=module.EventStore(self.path)
        result=reopened.query(kind='received',q='测试联系人')
        self.assertEqual(result['total'],1)
        self.assertEqual(result['items'][0]['id'],event_id)
        self.assertEqual(result['items'][0]['text'],text)

    def test_search_scope_and_cursor_do_not_duplicate_rows(self):
        for i in range(5):
            self.store.append('info',f'消息{i}',kind='received',scope='private',name='测试联系人')
        self.store.append('info','群消息',kind='received',scope='group',name='测试群')
        self.store.append('error','模型错误',kind='error',scope='system')
        first=self.store.query(kind='received',scope='private',limit=2)
        second=self.store.query(kind='received',scope='private',limit=2,before=first['next_before'])
        self.assertEqual(first['total'],5)
        self.assertTrue(first['has_more'])
        self.assertTrue(set(x['id'] for x in first['items']).isdisjoint(x['id'] for x in second['items']))
        self.assertEqual(self.store.query(q='模型错误')['total'],1)

    def test_legacy_log_is_imported_once_and_recognizes_received_messages(self):
        path=Path(self.temp.name)/'bridge.log'
        path.write_text('2026-10-14 12:00:00 [info] [私] 测试甲 (wxid_test): 第一条\n2026-10-14 12:00:01 [sent] 已发送给 测试甲（onebot）\n',encoding='utf-8')
        self.assertTrue(hasattr(self.store,'import_legacy'),'legacy log migration is not implemented')
        self.store.import_legacy(path)
        self.store.import_legacy(path)
        self.assertEqual(self.store.query()['total'],2)
        event=self.store.query(kind='received')['items'][0]
        self.assertEqual(event['scope'],'private')
        self.assertEqual(event['name'],'测试甲')
        self.assertEqual(event['uid'],'wxid_test')

    def test_stats_only_count_today_and_actual_sent(self):
        self.store.append('info','昨日收件',kind='received',ts=time.time()-2*86400)
        self.store.append('info','今日收件',kind='received')
        self.store.append('reply','只生成未发送',kind='reply')
        self.store.append('sent','已经发送',kind='sent')
        self.store.append('error','错误',kind='error')
        stats=self.store.stats()
        self.assertEqual(stats['today_received'],1)
        self.assertEqual(stats['today_sent'],1)
        self.assertEqual(stats['today_errors'],1)
        self.assertEqual(stats['total_events'],5)

class SettingsValidationTests(unittest.TestCase):
    def test_rejects_string_boolean_unknown_fields_and_bad_ranges(self):
        self.assertTrue(hasattr(module,'validate_settings'),'settings validation is not implemented')
        for patch in ({'auto_reply':'false'},{'send_mode':'bad'},{'max_consec':-1},{'max_consec':True},{'down_times':10},{'unknown':1},{'allowlist':'所有人'}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                module.validate_settings(patch)

    def test_allowlist_is_trimmed_deduplicated_and_empty_is_preserved(self):
        self.assertTrue(hasattr(module,'validate_settings'),'settings validation is not implemented')
        self.assertEqual(module.validate_settings({'allowlist':['  测试甲  ','测试甲','wxid_test','']}),{'allowlist':['测试甲','wxid_test']})
        self.assertEqual(module.validate_settings({'allowlist':[],'allow_all':False}),{'allowlist':[],'allow_all':False})
        self.assertEqual(module.validate_settings({'max_consec':0}),{'max_consec':0})

if __name__=='__main__':unittest.main()
