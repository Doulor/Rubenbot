"""群定位并排展示的前端回归：真实 app.js 里渲染两份定位。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 Node 才能运行群定位前端测试")
def test_group_profile_side_by_side():
    script = Path(__file__).parent / "frontend" / "group_profile.cjs"
    subprocess.run(["node", "--test", str(script)], check=True)
