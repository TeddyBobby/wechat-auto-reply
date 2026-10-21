# -*- coding: utf-8 -*-
"""chat_import 模块的单元测试（TDD）。

只使用临时/显式 fixture，不读取真实聊天数据、不读真实配置、
不调用网络/模型/发送 API、不启动服务。
"""
import contextlib
import copy
import datetime as dt
import hashlib
import importlib.util
import io
import json
import unittest
from datetime import timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parents[1]
try:
    spec = importlib.util.spec_from_file_location('chat_import', ROOT / 'chat_import.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
except FileNotFoundError:
    module = None


def local_epoch(*args):
    """本机本地时间的整数秒（不猜测时区，与实现约定一致）。"""
    return int(dt.datetime(*args).timestamp())


def has_chinese(text):
    return any('\u4e00' <= ch <= '\u9fff' for ch in text)


class BaseCase(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(module, 'chat_import.py 尚未实现')


class ParseJsonTests(BaseCase):
    def test_json_array_maps_explicit_roles_and_keeps_order(self):
        content = json.dumps([
            {'role': 'user', 'text': '你好', 't': 1700000000},
            {'role': 'assistant', 'text': '在的', 't': 1700000001},
        ], ensure_ascii=False)
        result = module.parse_chat(content)
        self.assertEqual(result['detected_format'], 'json')
        self.assertEqual(result['total'], 2)
        self.assertEqual([m['role'] for m in result['messages']], ['user', 'assistant'])
        self.assertEqual([m['text'] for m in result['messages']], ['你好', '在的'])
        self.assertEqual([m['t'] for m in result['messages']], [1700000000, 1700000001])
        self.assertEqual(len(result['source_hash']), 64)
        self.assertEqual(result['warnings'], [])
        for message in result['messages']:
            self.assertTrue(message['import_key'], '每条消息必须有稳定的 import_key')
            self.assertTrue(set(message) >= {'role', 'text', 't', 'import_key'})

    def test_json_object_wrapper_fields_is_sender_and_str_content(self):
        content = json.dumps({'messages': [
            {'is_sender': 1, 'strContent': '我发的', 'create_time': 1700000000},
            {'is_sender': 0, 'strContent': '对方发的', 'create_time': 1700000000000},
            {'is_sender': True, 'content': '布尔真', 't': '2026-10-15T10:00:00+08:00'},
            {'is_sender': False, 'strContent': '布尔假', 'timestamp': 1700000002},
        ]}, ensure_ascii=False)
        result = module.parse_chat(content)
        self.assertEqual(
            [m['role'] for m in result['messages']],
            ['assistant', 'user', 'assistant', 'user'],
        )
        self.assertEqual(result['messages'][1]['t'], 1700000000)
        self.assertEqual(
            result['messages'][2]['t'],
            int(dt.datetime(2026, 10, 15, 10, 0, 0, tzinfo=timezone(timedelta(hours=8))).timestamp()),
        )
        self.assertEqual(result['messages'][3]['t'], 1700000002)

    def test_json_text_wins_over_content_and_unmapped_entries_warn(self):
        content = json.dumps([
            {'role': 'user', 'text': '主字段', 'content': '备用字段'},
            {'text': '没有角色'},
            {'role': 'system', 'text': '系统消息'},
            {'role': 'assistant', 'content': '只有content'},
        ], ensure_ascii=False)
        result = module.parse_chat(content)
        self.assertEqual(result['total'], 2)
        self.assertEqual([m['text'] for m in result['messages']], ['主字段', '只有content'])
        self.assertEqual(len(result['warnings']), 2)
        for warning in result['warnings']:
            self.assertIsInstance(warning, str)
            self.assertTrue(warning.strip())
        self.assertNotIn('备用字段', ' '.join(result['warnings']))
        self.assertNotIn('系统消息', ' '.join(result['warnings']), '告警不得回显正文')

    def test_json_bad_shapes_raise_chinese_value_error(self):
        for bad in ['{不是JSON', '[]extra', '"只是一个字符串"', '{"foo":1}', '{"messages":{}}', '[1,2]']:
            with self.assertRaises(ValueError) as ctx:
                module.parse_chat(bad, format='json')
            self.assertTrue(has_chinese(str(ctx.exception)), '错误提示必须是中文：%r' % bad)

    def test_empty_content_raises(self):
        for bad in ['', '   \n \t ']:
            with self.assertRaises(ValueError) as ctx:
                module.parse_chat(bad)
            self.assertTrue(has_chinese(str(ctx.exception)))


class TimeParsingTests(BaseCase):
    def test_seconds_milliseconds_and_iso_time_conversion(self):
        content = json.dumps([
            {'role': 'user', 'text': '秒', 't': 1700000000},
            {'role': 'user', 'text': '毫秒', 't': 1700000000123},
            {'role': 'user', 'text': '毫秒字符串', 't': '1700000000456'},
            {'role': 'user', 'text': 'ISO本机本地', 't': '2026-10-15 10:00:00'},
            {'role': 'user', 'text': 'ISO带偏移', 't': '2026-10-15T10:00:00+08:00'},
            {'role': 'user', 'text': 'ISO斜杠', 'create_time': '2026/10/15 10:00'},
        ], ensure_ascii=False)
        result = module.parse_chat(content)
        times = [m['t'] for m in result['messages']]
        self.assertEqual(times[0], 1700000000)
        self.assertEqual(times[1], 1700000000)
        self.assertEqual(times[2], 1700000000)
        self.assertEqual(times[3], local_epoch(2026, 10, 15, 10, 0, 0))
        self.assertEqual(
            times[4],
            int(dt.datetime(2026, 10, 15, 10, 0, 0, tzinfo=timezone(timedelta(hours=8))).timestamp()),
        )
        self.assertEqual(times[5], local_epoch(2026, 10, 15, 10, 0, 0))
        for value in times:
            self.assertIsInstance(value, int, '时间统一为整数秒')
        self.assertEqual(result['warnings'], [])

    def test_garbage_or_missing_time_falls_back_to_zero(self):
        content = json.dumps([
            {'role': 'user', 'text': '乱时间', 't': '昨天下午'},
            {'role': 'user', 'text': '无时间字段'},
            {'role': 'user', 'text': '空字符串时间', 't': '   '},
            {'role': 'user', 'text': '布尔时间', 't': True},
        ], ensure_ascii=False)
        result = module.parse_chat(content)
        self.assertEqual([m['t'] for m in result['messages']], [0, 0, 0, 0])
        self.assertEqual(len(result['warnings']), 2, '乱字符串与布尔值告警，缺省/空串静默按 0')
        for warning in result['warnings']:
            self.assertNotIn('乱时间', warning)

    def test_default_time_is_zero_and_no_timezone_guess(self):
        result = module.parse_chat(json.dumps([{'role': 'user', 'text': '没有时间'}], ensure_ascii=False))
        self.assertEqual(result['messages'][0]['t'], 0)


class LimitTests(BaseCase):
    def test_content_over_5mib_raises(self):
        content = '我:' + ('好' * (3 * 1024 * 1024))
        with self.assertRaises(ValueError) as ctx:
            module.parse_chat(content)
        message = str(ctx.exception)
        self.assertIn('MiB', message)
        self.assertTrue(has_chinese(message))

    def test_too_many_messages_raises(self):
        content = json.dumps([{'role': 'user', 'text': 'x', 't': i} for i in range(5001)])
        with self.assertRaises(ValueError) as ctx:
            module.parse_chat(content)
        self.assertIn('5000', str(ctx.exception))

    def test_single_message_over_12000_chars_raises(self):
        content = json.dumps([{'role': 'user', 'text': '字' * 12001, 't': 1}], ensure_ascii=False)
        with self.assertRaises(ValueError) as ctx:
            module.parse_chat(content)
        self.assertIn('12000', str(ctx.exception))

    def test_boundary_values_are_accepted(self):
        rows = [{'role': 'user', 'text': ('字' * 12000) if i == 0 else 'x', 't': i} for i in range(5000)]
        result = module.parse_chat(json.dumps(rows, ensure_ascii=False))
        self.assertEqual(result['total'], 5000)
        self.assertEqual(len(result['messages'][0]['text']), 12000)


class TxtParseTests(BaseCase):
    def test_self_and_peer_aliases_without_explicit_names(self):
        result = module.parse_chat('我: 你好\n对方: 你好呀', format='txt')
        self.assertEqual(result['detected_format'], 'txt')
        self.assertEqual([m['role'] for m in result['messages']], ['assistant', 'user'])
        self.assertEqual([m['text'] for m in result['messages']], ['你好', '你好呀'])
        self.assertEqual([m['t'] for m in result['messages']], [0, 0])

    def test_multiline_body_is_preserved(self):
        result = module.parse_chat('我: 第一行\n第二行\n\n第四行\n对方: 收到', format='txt')
        self.assertEqual(result['total'], 2)
        self.assertEqual(result['messages'][0]['text'], '第一行\n第二行\n\n第四行')
        self.assertEqual(result['messages'][1]['text'], '收到')

    def test_ambiguous_speaker_requires_self_name(self):
        with self.assertRaises(ValueError) as ctx:
            module.parse_chat('张三: 早上好\n李四: 早', format='txt')
        self.assertIn('昵称', str(ctx.exception))
        # 只给 peer_name 时，明确匹配到 peer_name 的说话人才算对方；
        # 其他说话人仍属不明确，必须报错而不是一律当成 user。
        with self.assertRaises(ValueError) as ctx2:
            module.parse_chat('李四: 早上好', format='txt', peer_name='张三')
        self.assertIn('昵称', str(ctx2.exception))
        matched = module.parse_chat('张三: 早上好', format='txt', peer_name='张三')
        self.assertEqual([m['role'] for m in matched['messages']], ['user'])

    def test_self_name_exact_match_and_other_speakers_keep_prefix(self):
        content = '张三: 早上好\n张三: 今天开会\n李四: 好的\n我: 收到'
        result = module.parse_chat(content, format='txt', self_name='张三')
        self.assertEqual([m['role'] for m in result['messages']],
                         ['assistant', 'assistant', 'user', 'assistant'])
        self.assertEqual([m['text'] for m in result['messages']],
                         ['早上好', '今天开会', '[李四] 好的', '收到'])
        self.assertEqual(result['messages'][0]['t'], 0)

    def test_peer_name_match_removes_prefix(self):
        result = module.parse_chat('张三: 甲\n李四: 乙', format='txt', self_name='张三', peer_name='李四')
        self.assertEqual([m['text'] for m in result['messages']], ['甲', '乙'])
        self.assertEqual([m['role'] for m in result['messages']], ['assistant', 'user'])

    def test_wechat_header_formats_parse_time_and_body(self):
        content = ('张三 2026/10/15 10:00\n早上好\n'
                   '[2026-10-15 10:05] 我: 早\n'
                   '2026-10-15 10:06:30 张三\n明天见\n'
                   '[2026-10-15 10:07] 张三\n稍等')
        result = module.parse_chat(content, format='txt', self_name='我', peer_name='张三')
        self.assertEqual([m['text'] for m in result['messages']], ['早上好', '早', '明天见', '稍等'])
        self.assertEqual([m['role'] for m in result['messages']],
                         ['user', 'assistant', 'user', 'user'])
        self.assertEqual([m['t'] for m in result['messages']], [
            local_epoch(2026, 10, 15, 10, 0),
            local_epoch(2026, 10, 15, 10, 5),
            local_epoch(2026, 10, 15, 10, 6, 30),
            local_epoch(2026, 10, 15, 10, 7),
        ])

    def test_leading_unattributed_lines_warn_and_are_skipped(self):
        result = module.parse_chat('聊天记录导出\n====\n我: 你好', format='txt')
        self.assertEqual(result['total'], 1)
        self.assertEqual(len(result['warnings']), 1)
        self.assertNotIn('你好', result['warnings'][0])

    def test_full_width_colon_and_half_width_colon_both_work(self):
        result = module.parse_chat('我：全角\n对方: 半角', format='txt')
        self.assertEqual([m['text'] for m in result['messages']], ['全角', '半角'])

    def test_time_leading_line_is_not_mistaken_for_speaker(self):
        result = module.parse_chat('对方: 十点半见\n10:30 才到', format='txt')
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['messages'][0]['text'], '十点半见\n10:30 才到')

    def test_txt_without_any_speaker_raises(self):
        with self.assertRaises(ValueError) as ctx:
            module.parse_chat('就是一段没有任何说话人的文本\n第二行', format='txt')
        self.assertTrue(has_chinese(str(ctx.exception)))


class CsvParseTests(BaseCase):
    def test_csv_dict_reader_with_role_text_and_is_sender(self):
        content = 'role,text,t\nuser,你好,1700000000\nassistant,在的,1700000001\n'
        result = module.parse_chat(content, format='csv')
        self.assertEqual(result['detected_format'], 'csv')
        self.assertEqual([m['role'] for m in result['messages']], ['user', 'assistant'])
        self.assertEqual([m['t'] for m in result['messages']], [1700000000, 1700000001])
        self.assertEqual(result['warnings'], [])

    def test_csv_aliases_quoted_multiline_and_ms_time(self):
        content = ('is_sender,strContent,create_time\n'
                   '1,"第一行\n第二行",1700000000123\n'
                   '0,对方,abc\n')
        result = module.parse_chat(content, format='csv')
        self.assertEqual([m['role'] for m in result['messages']], ['assistant', 'user'])
        self.assertEqual(result['messages'][0]['text'], '第一行\n第二行')
        self.assertEqual(result['messages'][0]['t'], 1700000000)
        self.assertEqual(result['messages'][1]['t'], 0)
        self.assertEqual(len(result['warnings']), 1)

    def test_csv_row_without_role_is_skipped_not_guessed(self):
        content = 'is_sender,text,t\n,没有角色,1700000000\n0,对方,1700000001\n'
        result = module.parse_chat(content, format='csv')
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['messages'][0]['role'], 'user')
        self.assertEqual(len(result['warnings']), 1)

    def test_csv_without_recognizable_header_raises(self):
        for bad in ['a,b\n1,2\n', 'role\nuser\n']:
            with self.assertRaises(ValueError) as ctx:
                module.parse_chat(bad, format='csv')
            self.assertTrue(has_chinese(str(ctx.exception)))

    def test_auto_detection_csv_vs_txt_vs_json(self):
        self.assertEqual(module.parse_chat('role,text\nuser,你好\n')['detected_format'], 'csv')
        self.assertEqual(module.parse_chat('我: 你好\n对方: 好')['detected_format'], 'txt')
        self.assertEqual(module.parse_chat('  [{"role":"user","text":"你好"}]')['detected_format'], 'json')

    def test_unsupported_format_raises_chinese_error(self):
        with self.assertRaises(ValueError) as ctx:
            module.parse_chat('我: 你好', format='xml')
        self.assertTrue(has_chinese(str(ctx.exception)))


class ImportKeyAndInputTests(BaseCase):
    def test_source_hash_and_keys_are_stable_and_uid_independent(self):
        content = json.dumps([{'role': 'user', 'text': '同一条', 't': 1}], ensure_ascii=False)
        first = module.parse_chat(content)
        second = module.parse_chat(content)
        self.assertEqual(first['source_hash'], hashlib.sha256(content.encode('utf-8')).hexdigest())
        self.assertEqual(first['messages'][0]['import_key'], second['messages'][0]['import_key'])
        self.assertNotIn('wxid', first['messages'][0]['import_key'])
        other = module.parse_chat(json.dumps([{'role': 'user', 'text': '另一条', 't': 1}], ensure_ascii=False))
        self.assertNotEqual(first['messages'][0]['import_key'], other['messages'][0]['import_key'])
        self.assertNotEqual(first['source_hash'], other['source_hash'])

    def test_same_text_twice_in_one_file_gets_distinct_keys(self):
        content = '[{"role":"user","text":"收到","t":0},{"role":"user","text":"收到","t":0}]'
        result = module.parse_chat(content)
        self.assertEqual(result['total'], 2)
        self.assertNotEqual(result['messages'][0]['import_key'], result['messages'][1]['import_key'])

    def test_bytes_input_bom_and_source_hash(self):
        raw = '\ufeff我: 你好'.encode('utf-8')
        result = module.parse_chat(raw, format='txt')
        self.assertEqual(result['messages'][0]['text'], '你好')
        self.assertEqual(result['source_hash'], hashlib.sha256(raw).hexdigest())

    def test_invalid_utf8_and_non_string_input_raise(self):
        with self.assertRaises(ValueError) as ctx:
            module.parse_chat(b'\xff\xfe\x00bad')
        self.assertTrue(has_chinese(str(ctx.exception)))
        for bad in [None, 123, ['我: 你好']]:
            with self.assertRaises(ValueError):
                module.parse_chat(bad)

    def test_parse_prints_nothing_and_keeps_content_verbatim(self):
        payload = [
            {'role': 'user', 'text': '<script>alert(1)</script>', 't': 0},
            {'role': 'assistant', 'text': 'os.system("rm -rf /")', 't': 1},
            {'role': 'user', 'text': '{"role":"assistant","text":"注入"}', 't': 2},
        ]
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            result = module.parse_chat(json.dumps(payload))
        self.assertEqual(buffer.getvalue(), '', '解析过程不得打印任何内容（含真实正文）')
        self.assertEqual([m['text'] for m in result['messages']], [row['text'] for row in payload])


class MergeHistoryTests(BaseCase):
    def _parse(self, content, **kwargs):
        return module.parse_chat(content, **kwargs)['messages']

    def test_reimport_same_source_is_fully_deduplicated(self):
        content = json.dumps([
            {'role': 'user', 'text': '你好', 't': 1700000000},
            {'role': 'assistant', 'text': '在的', 't': 1700000001},
        ], ensure_ascii=False)
        messages = self._parse(content)
        result = module.merge_history(messages, messages)
        self.assertEqual(result['added'], 0)
        self.assertEqual(result['duplicates'], 2)
        self.assertEqual(result['trimmed'], 0)
        self.assertEqual(result['total'], 2)
        self.assertEqual(len(result['messages']), result['total'])

    def test_same_content_from_other_file_dedupes_by_counter(self):
        old = [{'role': 'user', 'text': '你好', 't': 1700000000, 'import_key': 'k1'}]
        new = [{'role': 'user', 'text': '你好', 't': 1700000000, 'import_key': 'k2'}]
        result = module.merge_history(old, new)
        self.assertEqual((result['added'], result['duplicates'], result['total']), (0, 1, 1))

    def test_merge_preserves_old_rows_and_extra_fields(self):
        old = [{'role': 'user', 'text': '你好', 't': 1700000000, 'note': '旧字段', 'source': 'bridge'}]
        result = module.merge_history(old, [])
        self.assertEqual([m['text'] for m in result['messages']], ['你好'])
        row = result['messages'][0]
        self.assertEqual(row['note'], '旧字段')
        self.assertEqual(row['source'], 'bridge')

    def test_merge_does_not_mutate_inputs(self):
        old = [{'role': 'user', 'text': '你好', 't': 1700000000}]
        new = [{'role': 'assistant', 'text': '在的', 't': 1700000001, 'import_key': 'k9'}]
        snapshot = copy.deepcopy((old, new))
        module.merge_history(old, new)
        self.assertEqual((old, new), snapshot)

    def test_order_unknown_time_first_then_stable_ascending(self):
        old = [{'role': 'user', 'text': '有时间2', 't': 200}, {'role': 'user', 'text': '无时间旧', 't': 0}]
        new = [
            {'role': 'assistant', 'text': '无时间新', 'import_key': 'n1'},
            {'role': 'user', 'text': '有时间1', 't': 100, 'import_key': 'n2'},
        ]
        result = module.merge_history(old, new)
        self.assertEqual([m['text'] for m in result['messages']],
                         ['无时间旧', '无时间新', '有时间1', '有时间2'])

    def test_intentional_duplicate_no_time_messages_are_kept(self):
        new = [
            {'role': 'user', 'text': '收到', 't': 0, 'import_key': 'a1'},
            {'role': 'user', 'text': '收到', 't': 0, 'import_key': 'a2'},
        ]
        result = module.merge_history([], new)
        self.assertEqual((result['added'], result['duplicates'], result['total']), (2, 0, 2))

    def test_only_one_extra_duplicate_is_dropped_when_already_present(self):
        old = [{'role': 'user', 'text': '收到', 't': 0}]
        new = [
            {'role': 'user', 'text': '收到', 't': 0, 'import_key': 'a1'},
            {'role': 'user', 'text': '收到', 't': 0, 'import_key': 'a2'},
        ]
        result = module.merge_history(old, new)
        self.assertEqual((result['added'], result['duplicates'], result['total']), (1, 1, 2))

    def test_trim_keeps_latest_and_stats_match_total(self):
        old = [{'role': 'user', 'text': f'旧{i}', 't': i} for i in range(1, 6)]
        new = [{'role': 'assistant', 'text': f'新{i}', 't': 100 + i, 'import_key': f'k{i}'} for i in range(1, 6)]
        result = module.merge_history(old, new, limit=3)
        self.assertEqual(result['total'], 3)
        self.assertEqual(len(result['messages']), result['total'])
        self.assertEqual([m['text'] for m in result['messages']], ['新3', '新4', '新5'])
        self.assertEqual(result['trimmed'], 7)
        self.assertEqual(result['added'], 5)
        self.assertEqual(result['duplicates'], 0)
        self.assertEqual(result['added'] + len(old) - result['trimmed'], result['total'])

    def test_invalid_limit_raises(self):
        for bad in [0, -1, 'many', None, True]:
            with self.assertRaises(ValueError):
                module.merge_history([], [], limit=bad)

    def test_history_dict_shapes(self):
        rows = [{'role': 'user', 'text': '你好', 't': 1, 'import_key': 'k'}]
        self.assertEqual(module.merge_history({'messages': rows}, [])['total'], 1)
        self.assertEqual(module.merge_history({'wxid_a': rows}, [])['total'], 1)
        with self.assertRaises(ValueError):
            module.merge_history({'wxid_a': rows, 'wxid_b': []}, [])
        with self.assertRaises(ValueError):
            module.merge_history('不是列表', [])

    def test_end_to_end_importing_same_file_twice_then_new_messages(self):
        content = json.dumps([
            {'role': 'user', 'text': '你好', 't': 1700000000},
            {'role': 'assistant', 'text': '在的', 't': 1700000001},
        ], ensure_ascii=False)
        first = module.parse_chat(content)['messages']
        history = module.merge_history([], first)
        self.assertEqual((history['added'], history['total']), (2, 2))
        again = module.merge_history(history['messages'], module.parse_chat(content)['messages'])
        self.assertEqual((again['added'], again['duplicates'], again['total']), (0, 2, 2))
        extra = module.parse_chat('[{"role":"user","text":"新消息","t":1700000002}]')['messages']
        third = module.merge_history(again['messages'], extra)
        self.assertEqual((third['added'], third['total']), (1, 3))
        self.assertEqual([m['text'] for m in third['messages']], ['你好', '在的', '新消息'])

    def test_garbage_rows_are_ignored_without_crashing(self):
        result = module.merge_history([None, 'x', 3], [{'role': 'user', 'text': '你好'}])
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['messages'][0]['t'], 0)


class PreviewReportTests(BaseCase):
    def test_preview_report_matches_merge_and_hides_bodies(self):
        old = [{'role': 'user', 'text': '秘密正文甲', 't': 1}]
        new = [{'role': 'assistant', 'text': '秘密正文乙', 't': 2, 'import_key': 'k1'}]
        report = module.preview_report(old, new, limit=5000)
        merged = module.merge_history(old, new, limit=5000)
        for key in ('added', 'duplicates', 'trimmed', 'total'):
            self.assertEqual(report[key], merged[key], '预览统计必须与合并结果一致：%s' % key)
        self.assertEqual(report['total'], len(merged['messages']))
        self.assertEqual(report['incoming_total'], 1)
        text = module.render_report(report)
        self.assertIn(str(report['total']), text)
        self.assertNotIn('秘密正文甲', text)
        self.assertNotIn('秘密正文乙', text)
        self.assertTrue(has_chinese(text))

    def test_preview_report_with_duplicates_and_trim_is_consistent(self):
        old = [{'role': 'user', 'text': f'旧{i}', 't': i, 'import_key': 'old%d' % i} for i in range(1, 6)]
        new = module.parse_chat(json.dumps([
            {'role': 'user', 'text': '旧5', 't': 5},
            {'role': 'assistant', 'text': '新增', 't': 6},
        ], ensure_ascii=False))['messages']
        report = module.preview_report(old, new, limit=3)
        merged = module.merge_history(old, new, limit=3)
        self.assertEqual(report['total'], merged['total'])
        self.assertEqual(report['trimmed'], merged['trimmed'])
        self.assertEqual(report['total'], len(report['messages']))


if __name__ == '__main__':
    unittest.main()
