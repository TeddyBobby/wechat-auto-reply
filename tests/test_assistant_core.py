import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT=Path(__file__).parents[1]
PATH=ROOT/'assistant_core.py'

def module():
    assert PATH.exists(),'AI assistant settings are not implemented'
    spec=importlib.util.spec_from_file_location('assistant_core',PATH)
    value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value)
    return value

class AssistantSettingsTests(unittest.TestCase):
    def test_profile_inherits_defaults_without_changing_other_contacts(self):
        m=module()
        with tempfile.TemporaryDirectory() as directory:
            settings=m.AssistantSettings(Path(directory)/'assistant_settings.json')
            settings.update({'default_prompt':'全局简短','context_messages':12})
            settings.update_profile({'uid':'wxid_a','prompt':'对甲详细一些','notes':'甲喜欢直接回答','context_messages':6,'context_enabled':None})
            self.assertIn('全局简短',settings.effective('wxid_a')['prompt'])
            self.assertIn('对甲详细一些',settings.effective('wxid_a')['prompt'])
            self.assertEqual(settings.effective('wxid_a')['context_messages'],6)
            self.assertEqual(settings.effective('wxid_b')['prompt'],'全局简短')
            self.assertEqual(settings.effective('wxid_b')['context_messages'],12)
            settings=m.AssistantSettings(Path(directory)/'assistant_settings.json')
            self.assertEqual(settings.effective('wxid_a')['notes'],'甲喜欢直接回答')
            with self.assertRaises(ValueError):settings.update({'default_prompt':'不得半写','context_messages':True})
            self.assertEqual(settings.effective('wxid_b')['prompt'],'全局简短')
            settings.update_profile({'uid':'wxid_a','prompt':'','context_messages':0,'context_enabled':None,'notes':''})
            self.assertEqual(settings.effective('wxid_a')['prompt'],'全局简短')
            self.assertEqual(settings.effective('wxid_a')['context_messages'],0)

    def test_context_is_bounded_and_includes_only_supplied_contact_rows(self):
        m=module()
        config={'context_enabled':True,'context_messages':2,'context_char_limit':1000}
        rows=[{'role':'user','text':'旧话','t':0},{'role':'assistant','text':'之前回复','t':0},{'role':'user','text':'最新消息','t':0}]
        text,count=m.make_context(rows,'测试甲',config)
        self.assertEqual(count,2)
        self.assertNotIn('旧话',text)
        self.assertIn('最新消息',text)
        text,count=m.make_context(rows,'测试甲',{**config,'context_enabled':False})
        self.assertEqual((text,count),('',0))
        text,count=m.make_context(rows,'测试甲',{**config,'context_messages':0})
        self.assertEqual((text,count),('',0))

    def test_prompt_contains_profile_notes_and_untrusted_content_boundary(self):
        m=module()
        prompt=m.compose_prompt('现在的问题','测试甲',False,'历史仅甲',{'prompt':'少用感叹号','notes':'称呼甲同学'})
        for expected in ('少用感叹号','称呼甲同学','历史仅甲','现在的问题','不可信'):
            self.assertIn(expected,prompt)
        self.assertIn('SKIP',prompt)

    def test_missing_runtime_config_is_fail_closed(self):
        import os
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'WECHAT_HOME':directory}):
            spec=importlib.util.spec_from_file_location('bridge_empty_config_test',ROOT/'wechat_hermes_bridge.py')
            bridge=importlib.util.module_from_spec(spec);spec.loader.exec_module(bridge)
            self.assertFalse(bridge.state['auto_reply'])
            self.assertFalse(bridge.state['send_enabled'])
            self.assertEqual(bridge.state['allowlist'],[])

if __name__=='__main__':unittest.main()
