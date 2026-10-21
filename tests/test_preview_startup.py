"""Clone-style preview startup using copied source and disposable data only."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler
from unittest.mock import patch
from _offline import load_bridge

ROOT = Path(__file__).resolve().parents[1]
SOURCES = (
    'account_runtime.py', 'assistant_core.py', 'assistant_media.py',
    'chat_import.py', 'panel_backend.py', 'panel_server.py',
    'wechat_hermes_bridge.py', 'panel.html', 'start.sh', 'stop.sh', 'setup.sh',
)

# Guard all Python children, including the real preview process. The only
# subprocess they need is ps for start/stop's own recorded process identity.
GUARD = '''import json, os, sys
from pathlib import Path

if Path(sys.argv[0]).name == 'wechat_hermes_bridge.py':
    # If bootstrap stalls on a hosted runner, capture its exact Python stack
    # before start.sh's readiness timeout reclaims this disposable process.
    import faulthandler
    faulthandler.dump_traceback_later(8, repeat=False)
    Path(os.environ['PREVIEW_TEST_PYTHON_INFO']).write_text(json.dumps({
        'version': sys.version.split()[0],
        'executable': Path(sys.executable).name,
        'resolved_executable': Path(sys.executable).resolve().name,
        'mode': os.environ.get('WECHAT_MODE'),
        'panel_port': os.environ.get('WECHAT_PANEL_PORT'),
        'isolated_home': os.environ.get('WECHAT_HOME') == str(Path(sys.argv[0]).parent),
    }))

def guard(event, args):
    forbidden = False
    if event == 'socket.connect':
        # start.sh's readiness probe may only reach this test's own HTTP port.
        address = args[1]
        forbidden = not (isinstance(address, tuple) and
                         address[:2] == ('127.0.0.1', int(os.environ['WECHAT_PANEL_PORT'])))
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
    def test_loopback_server_listens_without_reverse_dns(self):
        bridge = load_bridge('preview_dns_test_bridge')
        with patch('socket.getfqdn', side_effect=AssertionError('Loopback server must not query reverse DNS')) as reverse_dns:
            with bridge.LocalHTTPServer(('127.0.0.1', 0), BaseHTTPRequestHandler) as server:
                self.assertEqual(server.server_name, 'localhost')
                self.assertEqual(server.server_port, server.server_address[1])
                self.assertGreater(server.server_port, 0)
                with socket.create_connection(server.server_address, timeout=1):
                    pass
            reverse_dns.assert_not_called()

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
                'PREVIEW_TEST_PYTHON_INFO': str(base / 'python-startup.json'),
            }
            client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            url = f'http://127.0.0.1:{port}'

            commands = []

            def command(script, timeout=30):
                try:
                    result = subprocess.run(
                        ['bash', str(checkout / script)], cwd=checkout, env=env,
                        capture_output=True, text=True, timeout=timeout)
                except subprocess.TimeoutExpired as error:
                    commands.append((script, 'timeout', str(error.stdout or ''), str(error.stderr or '')))
                    raise
                commands.append((script, result.returncode, result.stdout, result.stderr))
                return result

            def diagnostics():
                # Read only artifacts produced inside this synthetic fixture.
                parts = [f'parent_python={sys.version.split()[0]}, executable={Path(sys.executable).name}']
                for script, result, stdout, stderr in commands:
                    parts.append(f'{script} exit={result}\n{stdout[-3000:]}{stderr[-3000:]}')
                for path in (checkout / '.runtime/bridge-run.log',
                             checkout / '.runtime/bridge.pid.json',
                             base / 'guard-violations.txt', base / 'python-startup.json'):
                    if path.is_file() and not path.is_symlink():
                        parts.append(f'{path.name}:\n{path.read_text(errors="replace")[-5000:]}')
                value = '\n'.join(parts)
                for private_path, label in ((str(base), '<fixture>'), (str(ROOT), '<checkout>'),
                                            (str(Path.home()), '<home>'), (sys.executable, '<python>')):
                    value = value.replace(private_path, label)
                return value

            def wait_for_page(timeout=10):
                deadline = time.monotonic() + timeout
                last_problem = 'no attempt'
                while time.monotonic() < deadline:
                    try:
                        with client.open(url + '/', timeout=min(1, max(.05, deadline-time.monotonic()))) as response:
                            return response.status, response.headers['Content-Type'], response.read().decode('utf-8')
                    except (OSError, urllib.error.URLError) as error:
                        last_problem = f'{type(error).__name__}: {error}'
                        time.sleep(.1)
                self.fail(f'Preview HTTP not ready within {timeout}s: {last_problem}')

            def get_state():
                with client.open(url + '/api/state', timeout=3) as response:
                    return json.load(response)

            stopped = None
            failure = None
            try:
                setup = command('setup.sh')
                self.assertEqual(setup.returncode, 0, setup.stdout + setup.stderr)
                started = command('start.sh')
                self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
                status, content_type, body = wait_for_page()
                self.assertEqual(status, 200)
                self.assertIn('text/html', content_type)
                self.assertIn('微信控制台', body)
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
            except Exception as error:
                failure = error
            finally:
                # stop.sh validates target path and start time before signalling;
                # it can only use records in this disposable copy.
                try:
                    stopped = command('stop.sh', timeout=20)
                except Exception as error:
                    failure = failure or error
            if failure is not None:
                raise AssertionError('Preview startup integration failed.\n' + diagnostics()) from failure
            self.assertEqual(stopped.returncode, 0, diagnostics())
            with self.assertRaises(OSError):
                with socket.create_connection(('127.0.0.1', port), timeout=1):
                    pass
            self.assertFalse((base / 'guard-violations.txt').exists(),
                             'Preview attempted forbidden native/model/network access\n' + diagnostics())


if __name__ == '__main__':
    unittest.main()
