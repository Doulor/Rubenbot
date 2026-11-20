"""用 Node 在无浏览器依赖的 DOM 宿主上测试插件双页签。"""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 Node 才能运行插件前端测试")
def test_plugin_tabs():
    script = Path(__file__).parent / "frontend" / "plugins.cjs"
    subprocess.run(["node", "--test", str(script)], check=True)
