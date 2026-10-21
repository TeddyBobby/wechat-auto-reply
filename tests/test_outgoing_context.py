"""Focused outgoing history tests. Fake transport only, never send WeChat."""
import unittest
from unittest.mock import patch
from test_account_bridge import AccountIntegrationChecks, bridge

class OutgoingContextChecks(unittest.TestCase):
    setUp=AccountIntegrationChecks.setUp
    request=AccountIntegrationChecks.request

    def test_device_sync_is_our_side_deduped_and_never_autoreplied(self):
        scope=self.manager.current(fresh=True)
        with self.manager.bind(scope):
            bridge.state.update(auto_reply=True,send_enabled=True,reply_groups=True,allow_all=True)
        def event(mid='phone-1',**values):
            return {'self_id':'friend_x','user_id':'account_a','sender':{'user_id':'account_a','nickname':'本人'},'message_type':'private','raw_message':'手机手写消息','message_id':mid,**values}
        with patch.object(bridge,'ask_hermes') as model,patch.object(bridge,'dispatch_send') as send:
            bridge.handle_incoming(event());bridge.handle_incoming(event())
            bridge.handle_incoming(event('phone-group',self_id='account_a',group_id='123@chatroom',message_type='group'))
            bridge.handle_incoming(event('phone-group-retained',self_id='account_a',to_user_id='456@chatroom'))
            bridge.handle_incoming(event('foreign',user_id='account_b',sender={'user_id':'account_b'}))
        model.assert_not_called();send.assert_not_called()
        self.assertEqual(len(scope.workspace.history.get('friend_x',[])),1)
        row=scope.workspace.history['friend_x'][0]
        self.assertEqual((row['role'],row['source'],row['message_id']),('assistant','self_sync','phone-1'))
        self.assertEqual(scope.workspace.history['123@chatroom'][0]['role'],'assistant')
        self.assertEqual(scope.workspace.history['456@chatroom'][0]['role'],'assistant')
        self.assertNotIn('account_a',scope.workspace.history)

    def test_confirmed_manual_send_is_context_and_echo_does_not_duplicate(self):
        scope=self.manager.current(fresh=True)
        with self.manager.bind(scope),patch.object(bridge,'send_via_onebot',return_value='server-1'):
            bridge.dispatch_send('friend_x','对方','后台手写消息',mode='onebot')
        self.assertEqual(scope.workspace.history['friend_x'][0]['source'],'manual')
        bridge.handle_incoming({'self_id':'friend_x','user_id':'account_a','sender':{'user_id':'account_a'},'message_type':'private','message_id':'server-1','raw_message':'后台手写消息'})
        self.assertEqual(len(scope.workspace.history['friend_x']),1)
        bridge.handle_incoming({'self_id':'friend_x','user_id':'account_a','sender':{'user_id':'account_a'},'message_type':'private','message_id':'different-real-id','raw_message':'后台手写消息'})
        self.assertEqual(len(scope.workspace.history['friend_x']),2)
        with self.manager.bind(scope),patch.object(bridge,'send_via_onebot',side_effect=TimeoutError('送达未知')):
            with self.assertRaises(TimeoutError):bridge.dispatch_send('friend_x','对方','不能当成功入库',mode='onebot')
        self.assertEqual(len(scope.workspace.history['friend_x']),2)

    def test_manual_device_reply_during_generation_cancels_stale_ai(self):
        scope=self.manager.current(fresh=True)
        with self.manager.bind(scope):bridge.state.update(auto_reply=True,send_enabled=True,auto_skip=False,allowlist=['friend_x'])
        def model_result(*a,**k):
            bridge.handle_incoming({'self_id':'friend_x','user_id':'account_a','sender':{'user_id':'account_a'},'message_type':'private','message_id':'human-during','raw_message':'本人已经回复'})
            return '已经过期的模型回复'
        with patch.object(bridge,'ask_hermes',side_effect=model_result),patch.object(bridge,'send_via_onebot') as send:
            bridge.handle_incoming({'self_id':'account_a','user_id':'friend_x','sender':{'nickname':'对方'},'message_type':'private','message_id':'incoming-race','raw_message':'需要回复的问题'})
        send.assert_not_called()
        self.assertEqual([row['text'] for row in scope.workspace.history['friend_x']],['需要回复的问题','本人已经回复'])

    def test_ai_reply_is_recorded_once_and_unknown_group_target_is_not_guessed(self):
        scope=self.manager.current(fresh=True)
        with self.manager.bind(scope):bridge.state.update(auto_reply=True,send_enabled=True,auto_skip=False,allowlist=['friend_x'],reply_suffix='（AI）')
        with patch.object(bridge,'ask_hermes',return_value='模型回复'),patch.object(bridge,'send_via_onebot',return_value='server-ai'):
            bridge.handle_incoming({'self_id':'account_a','user_id':'friend_x','sender':{'nickname':'对方'},'message_type':'private','message_id':'incoming-1','raw_message':'需要回复的问题'})
        rows=scope.workspace.history['friend_x']
        self.assertEqual([x['role'] for x in rows],['user','assistant'])
        self.assertEqual(rows[-1]['text'],'模型回复（AI）');self.assertEqual(rows[-1]['model_text'],'模型回复')
        bridge.handle_incoming({'self_id':'account_a','user_id':'account_a','sender':{'user_id':'account_a'},'message_type':'private','message_id':'missing-group','raw_message':'目标已丢失的出向消息'})
        self.assertNotIn('account_a',scope.workspace.history)
        self.identity.update(id='account_b',folder='account_b_ab34')
        bridge.handle_incoming({'self_id':'friend_x','user_id':'account_a','sender':{'user_id':'account_a'},'message_type':'private','message_id':'late-A','raw_message':'旧账号不得串到B'})
        self.assertEqual(self.manager.current(fresh=True).workspace.history,{})

if __name__=='__main__':unittest.main()
