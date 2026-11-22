"""模型池编辑器前端回归：真实 app.js 里增删模型、调顺序、改策略。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 Node 才能运行模型池前端测试")
def test_model_pool_editor():
    script = Path(__file__).parent / "frontend" / "model_pool.cjs"
    subprocess.run(["node", "--test", str(script)], check=True, capture_output=True, text=True)
