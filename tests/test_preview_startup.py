"""Clone-style preview startup using copied source and disposable data only."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
SOURCES = (
    'account_runtime.py', 'assistant_core.py', 'assistant_media.py',
    'chat_import.py', 'panel_backend.py', 'panel_server.py',
    'wechat_hermes_bridge.py', 'panel.html', 'start.sh', 'stop.sh', 'setup.sh',
)

# Guard all Python children, including the real preview process. The only
# subprocess they need is ps for start/stop's own recorded process identity.
GUARD = '''import os, sys
from pathlib import Path

def guard(event, args):
    forbidden = event == 'socket.connect'
    if event == 'subprocess.Popen':
        forbidden = Path(str(args[0])).name != 'ps'
    if event == 'import' and str(args[0]).split('.')[0] == 'frida':
        forbidden = True
    if forbidden:
        with open(os.environ['PREVIEW_TEST_GUARD_LOG'], 'a') as stream:
            stream.write(event + '\\n')
        raise RuntimeError('Native/model/network calls are forbidden in preview tests')

sys.addaudithook(guard)
'''


class PreviewStartupTests(unittest.TestCase):
    def test_fresh_default_start_is_read_only_and_repeated_start_is_safe(self):
        with tempfile.TemporaryDirectory(prefix='wechat-preview-test-') as directory:
            base = Path(directory).resolve()
            checkout = base / 'checkout with spaces'
            checkout.mkdir()
            for name in SOURCES:
                shutil.copy2(ROOT / name, checkout / name)
            guard_dir = base / 'guard'
            guard_dir.mkdir()
            (guard_dir / 'sitecustomize.py').write_text(GUARD, encoding='utf-8')
            home, temporary = base / 'user-home', base / 'tmp'
            home.mkdir()
            temporary.mkdir()
            with socket.socket() as candidate:
                candidate.bind(('127.0.0.1', 0))
                port = candidate.getsockname()[1]
            # No personal environment, provider keys, native assets, or runtime
            # config is passed into the copied application.
            env = {
                'PATH': os.defpath, 'HOME': str(home), 'TMPDIR': str(temporary),
                'PYTHONPATH': str(guard_dir), 'PYTHONDONTWRITEBYTECODE': '1',
                'PYTHONUTF8': '1', 'TZ': 'UTC', 'LC_ALL': 'C',
                'WECHAT_PYTHON': sys.executable,
                'WECHAT_HOME': str(checkout), 'WECHAT_PANEL_PORT': str(port),
                'WECHAT_APP': str(base / 'not-installed-WeChat.app'),
                'PREVIEW_TEST_GUARD_LOG': str(base / 'guard-violations.txt'),
            }
            client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            url = f'http://127.0.0.1:{port}'

            def command(script, timeout=15):
                return subprocess.run(
                    ['bash', str(checkout / script)], cwd=checkout, env=env,
                    capture_output=True, text=True, timeout=timeout)

            def get_state():
                with client.open(url + '/api/state', timeout=3) as response:
                    return json.load(response)

            stopped = None
            try:
                setup = command('setup.sh')
                self.assertEqual(setup.returncode, 0, setup.stdout + setup.stderr)
                started = command('start.sh')
                self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
                with client.open(url + '/', timeout=3) as response:
                    self.assertEqual(response.status, 200)
                    self.assertIn('text/html', response.headers['Content-Type'])
                    self.assertIn('微信控制台', response.read().decode('utf-8'))
                state = get_state()
                self.assertTrue(state['preview_mode'])
                self.assertEqual(state['account']['id'], '')
                self.assertFalse(state['account']['verified'])
                self.assertEqual(state['contacts'], {})
                self.assertEqual(state['history'], {})
                self.assertEqual(state['allowlist'], [])
                self.assertFalse(state['auto_reply'])
                self.assertFalse(state['send_enabled'])
                services = {item['id']: item for item in state['services']}
                for name in ('wechat', 'onebot', 'hermes'):
                    self.assertFalse(services[name]['on'])
                    self.assertIsNone(services[name]['port'])
                    self.assertIsNone(services[name]['pid'])
                self.assertFalse((checkout / '.runtime/onebot-token').exists())
                for route in ('/api/settings', '/api/assistant/preview', '/onebot'):
                    request = urllib.request.Request(
                        url + route, data=b'{"auto_reply":true}',
                        headers={'Content-Type': 'application/json'})
                    with self.assertRaises(urllib.error.HTTPError) as rejected:
                        client.open(request, timeout=3)
                    error = rejected.exception
                    self.assertEqual(error.code, 403)
                    self.assertFalse(json.loads(error.read())['ok'])
                    error.close()
                self.assertFalse(get_state()['auto_reply'])
                record_path = checkout / '.runtime/bridge.pid.json'
                record = record_path.read_bytes()
                repeated = command('start.sh')
                self.assertNotEqual(repeated.returncode, 0)
                self.assertIn('被占用', repeated.stdout + repeated.stderr)
                self.assertEqual(record_path.read_bytes(), record)
                self.assertEqual(get_state()['pid'], state['pid'])
            finally:
                # stop.sh validates target path and start time before signalling;
                # it can only use records in this disposable copy.
                stopped = command('stop.sh', timeout=20)
            self.assertEqual(stopped.returncode, 0, stopped.stdout + stopped.stderr)
            with self.assertRaises(OSError):
                with socket.create_connection(('127.0.0.1', port), timeout=1):
                    pass
            self.assertFalse((base / 'guard-violations.txt').exists(),
                             'Preview attempted native/model/network access')


if __name__ == '__main__':
    unittest.main()
