import copy
import unittest
from unittest.mock import patch
import test_panel_http as fixture
bridge=fixture.bridge

class AssistantHTTPTests(unittest.TestCase):
    setUp=fixture.PanelHTTPTests.setUp
    request=fixture.PanelHTTPTests.request

    def test_contact_profile_is_persisted_without_enabling_auto_reply(self):
        before=copy.deepcopy(bridge.state)
        status,data=self.request('/api/assistant/settings')
        self.assertEqual(status,200)
        self.assertTrue(data['ok'])
        status,data=self.request('/api/assistant/settings',{'default_prompt':'全局测试提示','context_messages':8})
        self.assertEqual(status,200)
        status,data=self.request('/api/assistant/profile',{'uid':'wxid_fixture','prompt':'只回复甲专属','notes':'甲的备注','context_messages':4,'context_enabled':None})
        self.assertEqual(status,200)
        self.assertEqual(data['profile']['prompt'],'只回复甲专属')
        self.assertEqual(bridge.state,before)
        status,data=self.request('/api/assistant/settings')
        self.assertEqual(data['settings']['profiles']['wxid_fixture']['notes'],'甲的备注')
        status,data=self.request('/api/assistant/settings',{'default_prompt':'不得部分保存','context_messages':True})
        self.assertEqual(status,400)
        self.assertEqual(bridge.get_assistant_service().settings.snapshot()['default_prompt'],'全局测试提示')

    def test_history_pagination_never_mixes_contacts(self):
        bridge.history.update({'wxid_fixture':[{'role':'user','text':str(i),'t':i} for i in range(5)],'other':[{'role':'user','text':'别人的隐私','t':1}]})
        status,data=self.request('/api/history?uid=wxid_fixture&limit=2')
        self.assertEqual(status,200)
        self.assertEqual([x['text'] for x in data['items']],['3','4'])
        self.assertEqual(data['total'],5)
        self.assertTrue(data['has_more'])
        status,data=self.request('/api/history?uid=wxid_fixture&limit=2&before='+str(data['next_before']))
        self.assertEqual([x['text'] for x in data['items']],['1','2'])
        self.assertNotIn('别人的隐私',str(data))

    def test_import_requires_preview_then_deduplicates_current_history(self):
        import json
        content=json.dumps([{'role':'user','text':'【导入测试】你好','t':1},{'role':'assistant','text':'【导入测试】收到','t':2}],ensure_ascii=False)
        with patch.object(bridge,'dispatch_send') as send,patch.object(bridge,'ask_hermes') as model:
            status,data=self.request('/api/history/import_preview',{'uid':'wxid_fixture','content':content,'format':'json','self_name':''})
            self.assertEqual(status,200)
            self.assertEqual(data['preview']['added'],2)
            self.assertEqual(bridge.history,{})
            ticket=data['ticket']
            bridge.history['wxid_fixture']=[{'role':'user','text':'预览期间新收到的消息','t':3}]
            status,data=self.request('/api/history/import',{'ticket':ticket})
            self.assertEqual(status,200)
            self.assertEqual((data['added'],data['total']),(2,3))
            self.assertEqual(bridge.history['wxid_fixture'][-1]['text'],'预览期间新收到的消息')
            status,data=self.request('/api/history/import',{'ticket':ticket})
            self.assertEqual(status,400)
            status,data=self.request('/api/history/import_preview',{'uid':'wxid_fixture','content':content,'format':'json'})
            self.assertEqual(data['preview']['added'],0)
            self.assertEqual(data['preview']['duplicates'],2)
            send.assert_not_called();model.assert_not_called()

    def test_preview_uses_contact_prompt_and_history_but_never_sends_or_saves(self):
        self.request('/api/assistant/settings',{'default_prompt':'全局身份与回复习惯始终携带'})
        bridge.history.update({'wxid_fixture':[{'role':'user','text':'甲的专有上下文','t':1},{'role':'assistant','text':'仅甲的历史回复','t':2}],
                               'other':[{'role':'user','text':'别人的隐私上下文','t':3}]})
        self.request('/api/assistant/profile',{'uid':'wxid_fixture','prompt':'甲的专属提示词','notes':'称呼甲同学'})
        before=copy.deepcopy(bridge.history)
        with patch.object(bridge,'ask_hermes',return_value='【模型桩】仅用于隔离单测的草稿') as model,patch.object(bridge,'dispatch_send') as send:
            status,data=self.request('/api/assistant/preview',{'uid':'wxid_fixture','text':'现在聊点新的','image_ids':[]})
        self.assertEqual(status,200)
        self.assertEqual(data['context_used'],2)
        prompt=model.call_args.args[0]
        for text in ('全局身份与回复习惯始终携带','甲的专属提示词','称呼甲同学','甲的专有上下文','现在聊点新的'):self.assertIn(text,prompt)
        self.assertNotIn('别人的隐私上下文',prompt)
        self.assertEqual(bridge.history,before)
        send.assert_not_called()

    def test_model_cli_is_read_only_and_strips_runtime_metadata(self):
        from pathlib import Path
        from types import SimpleNamespace
        recorded={}
        def command(args,**kwargs):
            recorded['args']=args
            recorded['path']=Path(args[args.index('--query-file')+1])
            recorded['query']=recorded['path'].read_text()
            return SimpleNamespace(returncode=0,stdout=chr(10).join(['Warning: Unknown toolsets: none','session_id: test-session','测试草稿正文']),stderr='')
        with patch.object(bridge.subprocess,'run',side_effect=command):
            reply=bridge.ask_hermes('已构建的专属提示词','测试甲',False,'',None,True)
        self.assertEqual(reply,'测试草稿正文')
        self.assertEqual(recorded['query'],'已构建的专属提示词')
        for flag in ('-Q','--ignore-rules','--query-file','--max-turns'):self.assertIn(flag,recorded['args'])
        self.assertEqual(recorded['args'][recorded['args'].index('-t')+1],'none')
        self.assertFalse(recorded['path'].exists())

    def test_incoming_image_enters_model_as_attachment_and_never_leaks_xml(self):
        import struct,zlib
        from pathlib import Path
        def chunk(tag,body):return struct.pack('>I',len(body))+tag+body+struct.pack('>I',zlib.crc32(tag+body)&0xffffffff)
        png=b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',1,1,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(b'\x00\xff\x00\x00'))+chunk(b'IEND',b'')
        path=Path(self.temp.name)/'onebot/image/test.png';path.parent.mkdir(parents=True);path.write_bytes(png)
        packet={'message_type':'private','user_id':'wxid_fixture','sender':{'nickname':'测试甲'},'raw_message':'<msg><img aeskey="TEST_SECRET_ONLY"/></msg>',
                'message':[{'type':'image','data':{'url':path.as_uri()}}]}
        with patch.object(bridge,'ask_hermes') as model,patch.object(bridge,'dispatch_send') as send:
            bridge.handle_incoming(packet)
            model.assert_not_called();send.assert_not_called()
        self.assertIn('wxid_fixture',bridge.history)
        self.assertNotIn('TEST_SECRET_ONLY',str(bridge.history))
        bridge.state.update(auto_reply=True,send_enabled=True,allowlist=['wxid_fixture'])
        with patch.object(bridge,'ask_hermes',return_value='测试桩看图回复') as model,patch.object(bridge,'dispatch_send',return_value='isolated-test') as send:
            bridge.handle_incoming(packet)
        self.assertTrue(model.called)
        self.assertIn('uploads',model.call_args.args[4])
        self.assertNotIn('TEST_SECRET_ONLY',model.call_args.args[0])
        self.assertNotIn('file://',model.call_args.args[0])
        self.assertTrue(send.called)

    def test_incoming_shared_link_uses_real_extraction_boundary(self):
        packet={'message_type':'private','user_id':'wxid_fixture','sender':{'nickname':'测试甲'},
                'raw_message':'<msg><appmsg><type>5</type><title>测试文章</title><des>描述</des><url>https://example.com/story</url></appmsg></msg>',
                'message':[{'type':'share','data':{}}]}
        page={'kind':'link','title':'测试文章','summary':'隔离测试页面摘要','content':'隔离测试页面完整正文，不是线上抓取结果','status':'ready','url':'https://example.com/story'}
        with patch('assistant_media.fetch_public_page',return_value=page) as fetch,patch.object(bridge,'ask_hermes') as model:
            bridge.handle_incoming(packet)
            fetch.assert_not_called();model.assert_not_called()
        bridge.state.update(auto_reply=True,send_enabled=True,allowlist=['wxid_fixture'])
        with patch('assistant_media.fetch_public_page',return_value=page) as fetch,patch.object(bridge,'ask_hermes',return_value='隔离测试草稿') as model,patch.object(bridge,'dispatch_send',return_value='isolated-test'):
            bridge.handle_incoming(packet)
        self.assertTrue(fetch.called)
        self.assertIn(page['content'],model.call_args.args[0])
        self.assertNotIn('<appmsg>',model.call_args.args[0])

    def test_model_image_gets_valid_extension_without_exposing_original_path(self):
        import base64
        from pathlib import Path
        from types import SimpleNamespace
        # Explicit PNG fixture; this does not stand in for a model vision test.
        png=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jJAAAAABJRU5ErkJggg==')
        original=Path(self.temp.name)/'opaque.bin';original.write_bytes(png)
        seen={}
        def command(args,**kwargs):
            image=Path(args[args.index('--image')+1]);seen['image']=image
            self.assertEqual(image.suffix,'.png')
            self.assertEqual(image.read_bytes(),png)
            return SimpleNamespace(returncode=0,stdout='隔离测试图片草稿',stderr='')
        with patch.object(bridge.subprocess,'run',side_effect=command):
            bridge.ask_hermes('测试图文输入','测试甲',False,'',str(original),True)
        self.assertFalse(seen['image'].exists())
        self.assertTrue(original.exists())

    def test_stderr_or_warning_only_output_cannot_become_a_reply(self):
        from types import SimpleNamespace
        for stdout,stderr in (('', 'Warning: provider is unavailable'),('Warning: model is deprecated','')):
            with self.subTest(stdout=stdout,stderr=stderr),patch.object(bridge.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=stdout,stderr=stderr)):
                with self.assertRaises(RuntimeError):
                    bridge.ask_hermes('测试消息','测试甲',False,'',None,True)

    def test_import_body_limit_includes_worst_case_json_escaping(self):
        import json
        # Declared text fixtures, never user chat: both decoded files are below 5MiB.
        # Quotes double in the outer JSON, and control characters expand sixfold.
        for character in ('"',chr(0)):
            content=('对方:'+character*12000+'\n')*400
            self.assertLess(len(content.encode('utf-8')),5*1024*1024)
            wire=json.dumps({'uid':'wxid_fixture','content':content,'format':'txt'}).encode()
            self.assertGreater(len(wire),6*1024*1024)
            with self.subTest(character=repr(character)):
                status,data=self.request('/api/history/import_preview',{'uid':'wxid_fixture','content':content,'format':'txt'})
                self.assertEqual(status,200,data.get('error'))
                self.assertEqual(data['preview']['total'],400)
                self.assertEqual(bridge.history,{})

if __name__=='__main__':unittest.main()
