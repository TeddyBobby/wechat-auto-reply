"""Private test imports use disposable data; never open the installed bridge home."""
import atexit
import importlib.util
import os
from pathlib import Path
import tempfile
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

def load_bridge(name):
    home = tempfile.TemporaryDirectory(prefix="wechat-offline-test-")
    atexit.register(home.cleanup)
    spec = importlib.util.spec_from_file_location(name, ROOT / "wechat_hermes_bridge.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(os.environ, {"WECHAT_HOME": home.name}):
        spec.loader.exec_module(module)
    return module
