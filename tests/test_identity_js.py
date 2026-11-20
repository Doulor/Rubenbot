"""身份署名、名单和画像的实际前端渲染回归测试。"""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 Node 才能运行前端测试")
def test_identity_views():
    subprocess.run(["node", "--test", str(Path(__file__).parent / "frontend" / "identity.cjs")], check=True)
