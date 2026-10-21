import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from threading import Thread
from http.server import ThreadingHTTPServer
import unittest
from unittest.mock import patch
import urllib.request
import urllib.error

ROOT=Path(__file__).parents[1]
sys.path.insert(0,str(ROOT))
from _offline import load_bridge
bridge=load_bridge('panel_test_bridge')

class PanelHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        directory=Path(self.temp.name)
        self.patchers=[]
        values={'HOME_DIR':str(directory),'RUNTIME_DIR':str(directory),'_assistant_service':None,'_health_cache':{},'STATE_FILE':str(directory/'config.json'),'CONTACTS_FILE':str(directory/'contacts.json'),'HISTORY_FILE':str(directory/'history.json'),'LOG_FILE':str(directory/'bridge.log'),'EVENT_DB':str(directory/'events.db'),'_event_store':None,'logs':[],'contacts':{},'history':{},'state':{**copy.deepcopy(bridge.state),'auto_reply':False,'send_enabled':False}}
        for key,value in values.items():
            p=patch.object(bridge,key,value,create=True);p.start();self.patchers.append(p)
        self.addCleanup(lambda:[p.stop() for p in reversed(self.patchers)])
        self.server=ThreadingHTTPServer(('127.0.0.1',0),bridge.Panel)
        self.thread=Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.url=f'http://127.0.0.1:{self.server.server_port}'
        def forbidden(*args, **kwargs):
            raise AssertionError('Real model/native transport is forbidden in offline tests')
        original_run = bridge.subprocess.run
        original_popen = bridge.subprocess.Popen
        def isolated_command(command, **kwargs):
            # The CLI test may only reach this test's fresh ephemeral HTTP server.
            if command == [str(ROOT/'wechat-ctl'), 'off'] and kwargs.get('env', {}).get('WECHAT_API') == self.url:
                return original_run(command, **kwargs)
            return forbidden()
        def isolated_popen(command, **kwargs):
            if command == [str(ROOT/'wechat-ctl'), 'off'] and kwargs.get('env', {}).get('WECHAT_API') == self.url:
                return original_popen(command, **kwargs)
            return forbidden()
        for target, key, value in (
            (bridge, 'port_open', lambda port: False),
            (bridge, 'send_via_onebot', forbidden),
            (bridge, 'send_via_clipboard', forbidden),
            (bridge.subprocess, 'run', isolated_command),
            (bridge.subprocess, 'Popen', isolated_popen),
        ):
            p=patch.object(target,key,value);p.start();self.patchers.append(p)
        self.client=urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self,path,payload=None,headers=None):
        request=urllib.request.Request(self.url+path,data=None if payload is None else json.dumps(payload).encode(),headers={'Content-Type':'application/json',**(headers or {})})
        try:
            response=self.client.open(request,timeout=5)
        except urllib.error.HTTPError as response:
            body=response.read().decode()
            return response.code,json.loads(body) if body else {}
        with response:return response.status,json.load(response)

    def test_settings_are_validated_atomically_and_persisted(self):
        status,response=self.request('/api/settings',{'allowlist':[' 测试甲 ','测试甲'],'max_consec':0})
        self.assertEqual(status,200)
        self.assertTrue(response['ok'])
        self.assertEqual(bridge.state['allowlist'],['测试甲'])
        self.assertEqual(json.loads(Path(bridge.STATE_FILE).read_text())['max_consec'],0)
        before=copy.deepcopy(bridge.state)
        status,response=self.request('/api/settings',{'auto_reply':True,'max_consec':-1})
        self.assertEqual(status,400)
        self.assertEqual(bridge.state,before)

    def test_send_disabled_blocks_manual_dispatch(self):
        with patch.object(bridge,'dispatch_send') as send:
            status,response=self.request('/api/send',{'target':'filehelper','text':'this test must never send'})
        self.assertFalse(response['ok'])
        send.assert_not_called()

    def test_cross_origin_write_and_nonboolean_toggle_are_rejected(self):
        before=copy.deepcopy(bridge.state)
        status,response=self.request('/api/settings',{'auto_reply':True},headers={'Origin':'https://untrusted.invalid'})
        self.assertEqual(status,403)
        status,response=self.request('/api/toggle?key=send_mode')
        self.assertEqual(status,405)
        self.assertEqual(bridge.state,before)

    def test_received_log_preserves_full_body_when_auto_reply_is_off(self):
        text='浏览器验收测试消息\\n'+'完整正文'*100
        with patch.object(bridge,'ask_hermes') as model,patch.object(bridge,'dispatch_send') as send:
            bridge.handle_incoming({'message_type':'private','user_id':'wxid_fixture','sender':{'nickname':'测试甲'},'raw_message':text})
        model.assert_not_called();send.assert_not_called()
        status,response=self.request('/api/logs?kind=received')
        self.assertEqual(response['items'][0]['text'],text)
        self.assertEqual(response['items'][0]['uid'],'wxid_fixture')
        self.assertEqual(response['items'][0]['scope'],'private')

    def test_turning_auto_reply_off_while_model_runs_blocks_late_send(self):
        bridge.state.update(auto_reply=True,send_enabled=True,allowlist=['wxid_fixture'])
        def finish_model(*args):
            bridge.state['auto_reply']=False
            return '这条消息不得发送'
        with patch.object(bridge,'ask_hermes',side_effect=finish_model),patch.object(bridge,'dispatch_send') as send:
            bridge.handle_incoming({'message_type':'private','user_id':'wxid_fixture','sender':{'nickname':'测试甲'},'raw_message':'测试长消息，不属于收尾用语'})
        send.assert_not_called()

    def test_browser_same_site_get_cannot_enable_auto_reply(self):
        status,response=self.request('/api/toggle?key=auto_reply',headers={'Sec-Fetch-Site':'same-site'})
        self.assertIn(status,(403,405))
        self.assertFalse(bridge.state['auto_reply'])

    def test_database_failure_does_not_break_logging_or_health(self):
        import sqlite3
        with patch.object(bridge,'get_event_store',side_effect=sqlite3.OperationalError('database is locked')):
            bridge.log('info','test fallback log')
            status,response=self.request('/api/state')
        self.assertEqual(status,200)
        self.assertIn('telemetry_error',response)
        self.assertEqual(bridge.logs[-1]['msg'],'test fallback log')

    def test_cli_off_is_an_explicit_idempotent_setting(self):
        import subprocess
        import os
        cli=ROOT/'wechat-ctl'
        self.assertIn('WECHAT_API',cli.read_text(),'CLI has no safe isolated API override yet')
        for unused in range(2):
            result=subprocess.run([str(cli),'off'],env={**os.environ,'WECHAT_API':self.url},capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertFalse(bridge.state['auto_reply'])

    def test_contact_delta_save_preserves_newly_learned_contacts(self):
        bridge.contacts.update({'wxid_untouched':'新学习到的联系人','wxid_edit':'旧名字','wxid_delete':'待删除'})
        status,response=self.request('/api/contacts',{'changes':{'wxid_edit':'新名字'},'deleted':['wxid_delete']})
        self.assertEqual(status,200)
        self.assertEqual(response['state']['contacts']['wxid_edit'],'新名字')
        self.assertEqual(bridge.contacts['wxid_untouched'],'新学习到的联系人')
        self.assertNotIn('wxid_delete',bridge.contacts)
        self.assertEqual(json.loads((Path(self.temp.name)/'contacts.json').read_text()),bridge.contacts)

    def test_log_api_returns_persistent_received_events(self):
        bridge.log('info','[私] 测试甲 (wxid_fixture): 收到一条消息')
        status,response=self.request('/api/logs?kind=received&limit=10')
        self.assertEqual(status,200)
        self.assertEqual(response['total'],1)
        self.assertEqual(response['items'][0]['kind'],'received')

if __name__=='__main__':unittest.main()
