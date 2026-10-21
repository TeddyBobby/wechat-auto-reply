"""All history is not the same as a large recent-message/character cap."""
import unittest,tempfile
import subprocess
from pathlib import Path
from unittest.mock import patch
from test_assistant_core import module
import test_panel_http as fixture

class AllContextCoreChecks(unittest.TestCase):
    def test_all_does_not_apply_message_or_character_truncation(self):
        m=module();rows=[{'role':'user','text':'第%d条 '%i+'字'*300,'t':i} for i in range(150)]
        with tempfile.TemporaryDirectory() as d:
            settings=m.AssistantSettings(Path(d)/'settings.json')
            settings.update({'context_messages':-1,'context_char_limit':1000})
            text,used=m.make_context(rows,'甲',settings.effective('a'))
            self.assertEqual(used,150);self.assertGreater(len(text),1000)
            self.assertIn('第0条',text);self.assertIn('第149条',text);self.assertNotIn('[截断]',text)
            settings.update_profile({'uid':'a','context_messages':2})
            recent,count=m.make_context(rows,'甲',settings.effective('a'))
            self.assertEqual(count,2);self.assertNotIn('第0条',recent)
            settings.update_profile({'uid':'a','context_messages':-1})
            restored=m.AssistantSettings(Path(d)/'settings.json')
            self.assertEqual(restored.effective('a')['context_messages'],-1)
            self.assertEqual(m.make_context(rows,'甲',{**restored.effective('a'),'context_enabled':False}),('',0))

class AllContextHTTPChecks(unittest.TestCase):
    setUp=fixture.PanelHTTPTests.setUp
    request=fixture.PanelHTTPTests.request
    def test_model_window_overflow_is_clear_and_never_retried_with_less_history(self):
        bridge=fixture.bridge
        failure=subprocess.CompletedProcess([],1,'', 'context_length_exceeded: maximum context length')
        with patch.object(bridge,'run_model_process',return_value=failure) as run:
            with self.assertRaisesRegex(RuntimeError,'上下文窗口'):
                bridge._ask_hermes('完整历史','测试甲',False,prepared=True)
        self.assertEqual(run.call_count,1)

    def test_actual_context_and_model_input_both_receive_complete_history(self):
        bridge=fixture.bridge
        rows=[{'role':'user','text':'记录%d '%i+'文'*300,'t':i} for i in range(150)]
        bridge.history.update({'wxid_fixture':rows,'other':[{'role':'user','text':'另一个人的隐私','t':0}]})
        status,_=self.request('/api/assistant/settings',{'context_messages':-1,'context_char_limit':1000})
        self.assertEqual(status,200)
        status,data=self.request('/api/assistant/context?uid=wxid_fixture')
        self.assertEqual(status,200);self.assertEqual(data['context_used'],150)
        self.assertEqual(data['context_mode'],'all');self.assertFalse(data['context_char_limit_applied'])
        self.assertNotIn('另一个人的隐私',data['context_text'])
        with patch.object(bridge,'ask_hermes',return_value='仅测试草稿') as model,patch.object(bridge,'dispatch_send') as send:
            status,_=self.request('/api/assistant/preview',{'uid':'wxid_fixture','text':'新的消息','image_ids':[]})
        self.assertEqual(status,200);prompt=model.call_args.args[0]
        self.assertIn('记录0 ',prompt);self.assertIn('记录149 ',prompt)
        self.assertNotIn('另一个人的隐私',prompt);send.assert_not_called()
