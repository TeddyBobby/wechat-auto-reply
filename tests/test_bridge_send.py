import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from _offline import load_bridge
bridge=load_bridge('dispatch_test_bridge')

class DirectDispatchTests(unittest.TestCase):
    def test_timeout_never_falls_back_to_clipboard(self):
        with patch.object(bridge,'send_via_onebot',side_effect=TimeoutError('unknown delivery')), patch.object(bridge,'send_via_clipboard') as ui:
            with self.assertRaises(TimeoutError):
                bridge.dispatch_send('wxid_test','测试联系人','测试消息',mode='auto')
            ui.assert_not_called()

    def test_explicit_direct_mode_cannot_silently_send_through_ui(self):
        with patch.object(bridge,'contacts',{}), patch.object(bridge,'send_via_clipboard') as ui:
            with self.assertRaisesRegex(RuntimeError,'目标|wxid'):
                bridge.dispatch_send('','找不到的昵称','测试消息',mode='onebot')
            ui.assert_not_called()

if __name__=='__main__':unittest.main()
