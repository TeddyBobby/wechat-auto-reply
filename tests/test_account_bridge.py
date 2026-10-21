"""Small A/B integration checks; isolated temp data, no native/network sending."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import test_panel_http as fixture
bridge=fixture.bridge

class AccountIntegrationChecks(unittest.TestCase):
    request=fixture.PanelHTTPTests.request
    def setUp(self):
        fixture.PanelHTTPTests.setUp(self)
        extra=patch.multiple(bridge,account_manager=None,SEEN=set(),consec_short={},model_runtime=copy.deepcopy(bridge.model_runtime))
        extra.start();self.addCleanup(extra.stop)
        self.identity={'id':'account_a','wechat_pid':111,'onebot_generation':'bot-session-1','folder':'account_a_ab12'}
        self.manager=bridge.initialize_accounts(root=Path(self.temp.name),probe=lambda:dict(self.identity),migration_owner='account_a')
    def test_http_uses_account_pin_and_new_account_is_empty(self):
        _,a=self.request('/api/state');token=a['account']['token'];header={'X-Wechat-Account':token}
        self.assertEqual(self.request('/api/allowlist',{'allowlist':['shared_friend']},header)[0],200)
        self.assertEqual(self.request('/api/assistant/profile',{'uid':'shared_friend','prompt':'A专属'},header)[0],200)
        self.identity.update(id='account_b',folder='account_b_ab34')
        _,b=self.request('/api/state')
        self.assertFalse(b['auto_reply']);self.assertFalse(b['send_enabled'])
        self.assertEqual(b['contacts'],{});self.assertEqual(b['history'],{});self.assertEqual(b['allowlist'],[])
        self.assertEqual(self.request('/api/settings',{'allowlist':['wrong_target']},header)[0],409)
        self.assertEqual(self.request('/api/history?uid=shared_friend',headers=header)[0],409)
        status,data=self.request('/api/assistant/settings',headers={'X-Wechat-Account':b['account']['token']})
        self.assertEqual(status,200);self.assertEqual(data['settings']['profiles'],{})
        bridge.initialize_accounts(root=Path(self.temp.name),probe=lambda:dict(self.identity),migration_owner='account_a')
        _,restarted=self.request('/api/state')
        self.assertEqual(restarted['account']['id'],'account_b')
        self.assertNotEqual(restarted['account']['token'],b['account']['token'])
        self.assertEqual(self.request('/api/assistant/settings',headers={'X-Wechat-Account':b['account']['token']})[0],409)
    def test_old_model_result_cannot_send_or_enter_new_account(self):
        scope=self.manager.current(fresh=True)
        with self.manager.bind(scope):
            bridge.state.update(auto_reply=True,send_enabled=True,auto_skip=False,allowlist=['shared_friend'])
        def reply_then_switch(*a,**k):
            self.identity.update(id='account_b',folder='account_b_ab34')
            return '这是旧账号的回复，不得发送'
        with patch.object(bridge,'ask_hermes',side_effect=reply_then_switch),patch.object(bridge,'dispatch_send') as send:
            bridge.handle_incoming({'self_id':'account_a','message_type':'private','user_id':'shared_friend','sender':{'nickname':'模拟联系人'},'raw_message':'仅隔离测试消息'})
        send.assert_not_called()
        current=self.manager.current(fresh=True)
        self.assertEqual(current.workspace.account_id,'account_b')
        self.assertEqual(current.workspace.history,{})
        self.assertEqual(current.workspace.contacts,{})

    def test_group_name_is_never_learned_from_member_nickname(self):
        scope=self.manager.current(fresh=True)
        def receive(member,**extra):
            bridge.handle_incoming({'self_id':'account_a','message_type':'group','group_id':'12345@chatroom',
                                    'user_id':'member_id','sender':{'nickname':member},'raw_message':member+': 消息',**extra})
        with self.manager.bind(scope):
            bridge.contacts['12345@chatroom']='真正的群名'
            receive('成员甲');receive('成员乙')
            self.assertEqual(bridge.contacts['12345@chatroom'],'真正的群名')
            receive('成员甲',group_name='更新后的群名')
            self.assertEqual(bridge.contacts['12345@chatroom'],'更新后的群名')
            del bridge.contacts['12345@chatroom']
            receive('成员乙')
            self.assertEqual(bridge.contacts['12345@chatroom'],'12345@chatroom')
            self.assertEqual(bridge.conversation_name({'message_type':'group','sender':{'nickname':'成员乙'}},'12345@chatroom'),'未获取群名')

    def test_proxy_history_and_contacts_save_as_json_in_the_bound_account(self):
        first=self.manager.current(fresh=True)
        with patch.object(bridge,'log') as log:
            with self.manager.bind(first):
                bridge.record_turn('shared_friend','user','中文消息\n第二行')
                bridge.learn_contact('shared_friend','联系人甲')
            self.assertFalse(any(call.args[0]=='error' for call in log.call_args_list),log.call_args_list)
        a_history=json.loads((first.workspace.home/'history.json').read_text())
        self.assertEqual(a_history['shared_friend'][0]['text'],'中文消息\n第二行')
        self.assertEqual(json.loads((first.workspace.home/'contacts.json').read_text())['shared_friend'],'联系人甲')
        self.identity.update(id='account_b',folder='account_b_ab34')
        second=self.manager.current(fresh=True)
        with self.manager.bind(second):
            bridge.record_turn('shared_friend','user','仅属于账号B')
        self.assertEqual(json.loads((first.workspace.home/'history.json').read_text()),a_history)
        self.assertEqual(json.loads((second.workspace.home/'history.json').read_text())['shared_friend'][0]['text'],'仅属于账号B')

if __name__=='__main__':unittest.main()
