"""Only the targeted checks for this contact-UX change; never sends WeChat."""
import unittest
from unittest.mock import patch
import test_panel_http as fixture
bridge=fixture.bridge

class ContactUXChecks(unittest.TestCase):
    setUp=fixture.PanelHTTPTests.setUp
    request=fixture.PanelHTTPTests.request

    def test_deleted_setting_and_affixes(self):
        self.assertNotIn('down_times',bridge.state)
        self.assertNotIn('down_times',bridge._PERSIST)
        status,_=self.request('/api/settings',{'reply_prefix':'【AI】 ','reply_suffix':'（AI生成）'})
        self.assertEqual(status,200)
        bridge.state.update(auto_reply=True,send_enabled=True,auto_skip=False,allowlist=['wxid_fixture'])
        with patch.object(bridge,'ask_hermes',return_value='好的'),patch.object(bridge,'send_via_onebot',return_value='synthetic-receipt'),patch.object(bridge,'dispatch_send',wraps=bridge.dispatch_send) as send:
            bridge.handle_incoming({'message_type':'private','user_id':'wxid_fixture','sender':{'nickname':'测试甲'},'raw_message':'请确认安排'})
        self.assertEqual(send.call_args.args[2],'【AI】 好的（AI生成）')
        self.assertEqual(bridge.history['wxid_fixture'][-1]['text'],'【AI】 好的（AI生成）')
        self.assertEqual(bridge.format_auto_reply('SKIP'),'SKIP')
        with patch.object(bridge,'send_via_onebot') as manual:
            bridge.dispatch_send('filehelper','文件传输助手','手写正文',False,'onebot')
        self.assertEqual(manual.call_args.args[1],'手写正文')

    def test_contact_context_view_is_exact_and_effective(self):
        self.request('/api/assistant/profile',{'uid':'wxid_fixture','prompt':'甲专属提示','notes':'甲专属备注','context_messages':1})
        bridge.history.update({'wxid_fixture':[{'role':'user','text':'甲较早消息','t':1},{'role':'assistant','text':'甲实际携带内容','t':2}],
                               'other':[{'role':'user','text':'不应串入的乙内容','t':1}]})
        status,data=self.request('/api/assistant/context?uid=wxid_fixture')
        self.assertEqual(status,200)
        self.assertIn('甲专属提示',data['prompt'])
        self.assertIn(data['global_prompt'],data['prompt'])
        self.assertEqual(data['context_used'],1)
        self.assertEqual(data['stored_total'],2)
        self.assertIn('甲实际携带内容',data['context_text'])
        self.assertNotIn('不应串入',str(data))

if __name__=='__main__':unittest.main()
