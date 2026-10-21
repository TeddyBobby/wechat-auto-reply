import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
PATH = ROOT / 'account_runtime.py'


_LOADED = {}


def module():
    # 只加载一次：重复加载会造出不同的异常类对象，assertRaises 就抓不住了
    if 'mod' not in _LOADED:
        assert PATH.exists(), '账号隔离底层 account_runtime.py 未实现'
        spec = importlib.util.spec_from_file_location('account_runtime', PATH)
        value = importlib.util.module_from_spec(spec)
        sys.modules['account_runtime'] = value  # 兼容 py3.9 下 from __future__ annotations
        spec.loader.exec_module(value)
        _LOADED['mod'] = value
    return _LOADED['mod']


class FakeProbe:
    """假探测：进程内直接给定原生身份，不访问真实微信。"""

    def __init__(self, account_id='', wechat_pid=0, generation='', folder=''):
        self.data = {'id': account_id, 'wechat_pid': wechat_pid,
                     'onebot_generation': generation, 'folder': folder}

    def set(self, **fields):
        self.data.update(fields)
        return self

    def __call__(self):
        return dict(self.data)


def manager_for(root, probe, migration_owner=None, defaults=None):
    m = module()
    # cache_ttl=0：测试里不依赖墙上时钟，每次观察都真实重读 probe
    return m.AccountManager(root, probe, dict(defaults or {}),
                            migration_owner=migration_owner, cache_ttl=0)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


class AccountRuntimeTests(unittest.TestCase):
    def test_a_b_files_and_proxy_context_are_isolated(self):
        m = module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            probe = FakeProbe('wxid_a', 101, 'gen-1')
            manager = manager_for(root, probe)
            # 代理只建一次，靠 bind 切换作用域 —— 这是"不串记录"的关键
            contacts = m.MappingProxy(manager, 'contacts')
            history = m.MappingProxy(manager, 'history')
            state = m.MappingProxy(manager, 'state')
            logs = m.ListProxy(manager, 'logs')
            seen = m.SetProxy(manager, 'seen')

            a1 = manager.current()
            self.assertEqual(a1.workspace.account_id, 'wxid_a')
            with manager.bind(a1):
                contacts['wxid_friend'] = '朋友甲'
                history['wxid_friend'] = [{'role': 'user', 'text': '你好', 't': 1}]
                state['auto_reply'] = True
                state['send_enabled'] = True
                state['max_consec'] = 2
                logs.append({'msg': 'A 的日志'})
                seen.add('msg-a-1')
                a1.workspace.save_state()
                a1.workspace.save_contacts()
                a1.workspace.save_history()
            home_a = manager.account_home('wxid_a')
            self.assertTrue((home_a / 'config.json').exists())
            self.assertTrue((home_a / 'contacts.json').exists())
            self.assertTrue((home_a / 'history.json').exists())

            # 切到 B：同一批代理必须给出 B 自己的空数据
            probe.set(id='wxid_b', wechat_pid=202)
            b1 = manager.current(fresh=True)
            self.assertEqual(b1.workspace.account_id, 'wxid_b')
            self.assertNotEqual(b1.token, a1.token)
            with manager.bind(b1):
                self.assertEqual(dict(contacts), {})
                self.assertEqual(dict(history), {})
                self.assertEqual(list(state['allowlist']), [])
                self.assertFalse(state['auto_reply'])
                self.assertFalse(state['send_enabled'])
                self.assertEqual(list(logs), [])
                self.assertEqual(set(seen), set())
                self.assertEqual(state['max_consec'], 3)  # defaults，不是 A 的 2
            home_b = manager.account_home('wxid_b')
            self.assertNotEqual(home_a, home_b)
            self.assertFalse((home_b / 'contacts.json').exists())

            # 切回 A：数据还在，但发送开关已被关闭并落盘
            probe.set(id='wxid_a', wechat_pid=101)
            a2 = manager.current(fresh=True)
            with manager.bind(a2):
                self.assertEqual(contacts['wxid_friend'], '朋友甲')
                self.assertEqual(history['wxid_friend'][0]['text'], '你好')
                self.assertEqual(state['max_consec'], 2)
                self.assertFalse(state['auto_reply'])
                self.assertFalse(state['send_enabled'])
            persisted = json.loads((home_a / 'config.json').read_text(encoding='utf-8'))
            self.assertFalse(persisted['auto_reply'])
            self.assertFalse(persisted['send_enabled'])

            # PathProxy 动态跟随作用域
            log_path = m.PathProxy(manager, 'bridge.log')
            home = m.PathProxy(manager)
            with manager.bind(a1):
                self.assertEqual(str(log_path), str(home_a / 'bridge.log'))
                self.assertTrue(os_path_ok(log_path))
            with manager.bind(b1):
                self.assertEqual(str(log_path), str(home_b / 'bridge.log'))
                self.assertEqual(str(log_path + '.old'), str(home_b / 'bridge.log') + '.old')

    def test_a_b_a_old_token_rejected_and_sending_stays_off(self):
        m = module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            probe = FakeProbe('wxid_a', 101, 'gen-1')
            manager = manager_for(root, probe)
            a1 = manager.current()
            self.assertTrue(a1.ready)                     # 新 generation 绑到当前账号

            probe.set(id='wxid_b', wechat_pid=202)
            b = manager.current(fresh=True)
            self.assertNotEqual(b.token, a1.token)

            probe.set(id='wxid_a', wechat_pid=101)
            a2 = manager.current(fresh=True)
            # A → B → A：旧作用域令牌必须失效
            self.assertNotEqual(a2.token, a1.token)
            self.assertNotEqual(a2.token, b.token)
            with self.assertRaises(m.AccountChanged):
                manager.assert_current(a1, fresh=True)
            with self.assertRaises(ValueError):            # AccountChanged 是 ValueError 子类
                manager.assert_current(b, fresh=True)
            self.assertTrue(manager.assert_current(a2, fresh=True))   # 当前令牌可用

            # 进入 B、切回 A 都不自动恢复发送，且已落盘
            self.assertFalse(b.workspace.sending_enabled)
            self.assertFalse(a2.workspace.sending_enabled)
            for account_id in ('wxid_a', 'wxid_b'):
                path = manager.account_home(account_id) / 'config.json'
                if path.exists():
                    saved = json.loads(path.read_text(encoding='utf-8'))
                    self.assertFalse(saved['auto_reply'])
                    self.assertFalse(saved['send_enabled'])

            # 同一账号但 wechat_pid 变了也算身份变化 → 旧令牌失效
            probe.set(wechat_pid=303)
            a3 = manager.current(fresh=True)
            self.assertNotEqual(a3.token, a2.token)
            with self.assertRaises(m.AccountChanged):
                manager.assert_current(a2, fresh=True)

            # OneBot已归属即可配置发送，不要求先打开AI页初始化配置服务。
            manager.assert_current(a3, fresh=True, require_ready=True)
            manager.current().workspace.attach_assistant_service(FakeService('wxid_a'))
            manager.assert_current(a3, fresh=True, require_ready=True)
            manager.current().workspace.attach_assistant_service(FakeService('wxid_b'))
            with self.assertRaises(m.AccountChanged):
                manager.assert_current(a3, fresh=True, require_ready=True)

            # 同一个 generation 对应不同账号 → ready=False，提示重连
            probe.set(id='wxid_b', wechat_pid=404)
            changed = manager.current(fresh=True)
            self.assertFalse(changed.ready)
            self.assertIn('OneBot', changed.reason)

            # 账号空 → 拒绝
            probe.set(id='', wechat_pid=0)
            unknown = manager.current(fresh=True)
            self.assertFalse(unknown.ready)
            with self.assertRaises(m.AccountChanged):
                manager.assert_current(unknown, fresh=True)

    def test_legacy_migration_only_to_owner_then_empty_and_unknown_hidden(self):
        m = module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'root'
            root.mkdir()
            write_json(root / 'config.json', {'auto_reply': True, 'send_enabled': True,
                                              'allowlist': ['wxid_friend'], 'max_consec': 2})
            write_json(root / 'contacts.json', {'wxid_friend': '朋友甲'})
            write_json(root / 'history.json', {'wxid_friend': [{'role': 'user', 'text': '老消息', 't': 1}]})
            write_json(root / 'assistant_settings.json', {'default_prompt': '旧提示词'})
            (root / 'bridge.log').write_text('旧日志\n', encoding='utf-8')
            (root / 'uploads').mkdir()
            (root / 'uploads' / 'pic.jpg').write_bytes(b'img')
            db = sqlite3.connect(root / 'events.db')
            db.execute('CREATE TABLE t(x)')
            db.execute("INSERT INTO t VALUES('legacy-row')")
            db.commit()
            db.close()

            probe = FakeProbe('wxid_a', 101, 'gen-1', 'wxid_a_1a2b')
            manager = manager_for(root, probe, migration_owner='wxid_a')
            result = manager.bootstrap()
            self.assertTrue(result['migrated'])
            self.assertEqual(result['legacy_owner'], 'wxid_a')
            registry = json.loads(manager.registry_path.read_text(encoding='utf-8'))
            self.assertEqual(registry['legacy_owner'], 'wxid_a')

            # 首次迁移保留原开关，旧数据归 A
            a = manager.current(fresh=True)
            self.assertTrue(a.workspace.state['auto_reply'])
            self.assertTrue(a.workspace.state['send_enabled'])
            self.assertEqual(a.workspace.state['max_consec'], 2)
            self.assertEqual(a.workspace.state['allowlist'], ['wxid_friend'])
            self.assertEqual(a.workspace.contacts, {'wxid_friend': '朋友甲'})
            self.assertEqual(a.workspace.history['wxid_friend'][0]['text'], '老消息')
            self.assertEqual(a.workspace.assistant_settings['default_prompt'], '旧提示词')
            self.assertTrue((manager.account_home('wxid_a') / 'uploads' / 'pic.jpg').exists())
            copied_db = sqlite3.connect(manager.account_home('wxid_a') / 'events.db')
            self.assertEqual(copied_db.execute('SELECT x FROM t').fetchone()[0], 'legacy-row')
            copied_db.close()
            # 根目录原文件保留
            self.assertTrue((root / 'config.json').exists())
            self.assertTrue((root / 'uploads' / 'pic.jpg').exists())
            self.assertEqual((manager.account_home('wxid_a') / 'config.json').stat().st_mode & 0o777, 0o600)
            self.assertEqual(manager.account_home('wxid_a').stat().st_mode & 0o777, 0o700)
            # 重启不再迁移
            self.assertFalse(manager.bootstrap()['migrated'])

            # B 是新账号：空配置，不继承任何 Root 文件
            probe.set(id='wxid_b', wechat_pid=202)
            b = manager.current(fresh=True)
            self.assertEqual(b.workspace.contacts, {})
            self.assertEqual(b.workspace.history, {})
            self.assertEqual(b.workspace.state['allowlist'], [])
            self.assertFalse(b.workspace.state['auto_reply'])
            self.assertFalse(b.workspace.state['send_enabled'])
            self.assertFalse((manager.account_home('wxid_b') / 'config.json').exists())
            self.assertFalse((manager.account_home('wxid_b') / 'contacts.json').exists())

            # unknown：内存空 workspace，绝不露出 A 的资料
            probe.set(id='', wechat_pid=0)
            unknown = manager.current(fresh=True)
            self.assertEqual(unknown.workspace.account_id, '')
            self.assertTrue(unknown.workspace.ephemeral)
            self.assertEqual(unknown.workspace.contacts, {})
            self.assertEqual(unknown.workspace.history, {})
            self.assertEqual(unknown.workspace.state['allowlist'], [])
            self.assertFalse(unknown.ready)
            snapshot = manager.snapshot(unknown)
            self.assertEqual(snapshot['id'], '')
            self.assertFalse(snapshot['verified'])
            self.assertEqual(snapshot['status'], 'unknown')
            self.assertFalse((manager.accounts_dir / '_unknown').exists())
            contacts = m.MappingProxy(manager, 'contacts')
            with manager.bind(unknown):
                self.assertEqual(dict(contacts), {})

            # 账号不是迁移归属账号 → 拒绝启动迁移
            with tempfile.TemporaryDirectory() as other:
                bad_root = Path(other) / 'root'
                bad_root.mkdir()
                write_json(bad_root / 'config.json', {'auto_reply': True})
                mismatched = manager_for(bad_root, FakeProbe('wxid_b', 202, 'gen-9'),
                                         migration_owner='wxid_a')
                with self.assertRaises(m.BootstrapError):
                    mismatched.bootstrap()
                self.assertFalse((bad_root / 'accounts' / 'registry.json').exists())


class FakeService:
    def __init__(self, account_id):
        self.account_id = account_id

    def is_bound(self, account_id):
        return self.account_id == account_id


def os_path_ok(proxy):
    import os
    return os.fspath(proxy) == str(proxy)


if __name__ == '__main__':
    unittest.main()
