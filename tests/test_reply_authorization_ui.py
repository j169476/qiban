import shutil
import subprocess
from pathlib import Path

import pytest


def test_reply_authorization_browser_contract():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the management JavaScript contract test")
    script = Path(__file__).with_name("reply_authorization_ui.mjs")
    result = subprocess.run([node, "--test", str(script)], capture_output=True, text=True, timeout=20,
                            encoding="utf-8", errors="replace")
    assert result.returncode == 0, result.stdout + result.stderr
