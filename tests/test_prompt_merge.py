"""Global identity must remain in both context preview and actual model input."""
import tempfile,unittest
from pathlib import Path
from test_assistant_core import module

class PromptMergeChecks(unittest.TestCase):
    def test_global_identity_and_contact_supplement_are_composed_once(self):
        m=module()
        with tempfile.TemporaryDirectory() as d:
            settings=m.AssistantSettings(Path(d)/'settings.json')
            settings.update({'default_prompt':'我的身份是测试前端，保持自然','context_enabled':False})
            settings.update_profile({'uid':'a','prompt':'对甲称呼老同学','notes':'仅甲的备注'})
            settings.update_profile({'uid':'b','prompt':'仅乙的专属内容'})
            cfg=settings.effective('a')
            self.assertIn('我的身份是测试前端，保持自然',cfg['prompt'])
            self.assertIn('对甲称呼老同学',cfg['prompt'])
            self.assertLess(cfg['prompt'].index('我的身份'),cfg['prompt'].index('对甲'))
            self.assertNotIn('仅乙的专属内容',cfg['prompt'])
            final=m.compose_prompt('现在的问题','甲',False,'',cfg)
            self.assertEqual(final.count('我的身份是测试前端，保持自然'),1)
            self.assertEqual(final.count('对甲称呼老同学'),1)
            self.assertFalse(cfg['context_enabled'])
            settings.update({'default_prompt':'更新后的全局身份'})
            reloaded=m.AssistantSettings(Path(d)/'settings.json')
            self.assertIn('更新后的全局身份',reloaded.effective('a')['prompt'])
            self.assertIn('对甲称呼老同学',reloaded.effective('a')['prompt'])
            self.assertEqual(reloaded.snapshot()['profiles']['a']['prompt'],'对甲称呼老同学')
            reloaded.update_profile({'uid':'a','prompt':''})
            self.assertEqual(reloaded.effective('a')['prompt'],'更新后的全局身份')

if __name__=='__main__':unittest.main()
