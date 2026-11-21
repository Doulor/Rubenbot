"""表情库磁贴的前端回归：描述/情绪上磁贴、普通图片隔离计数。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 Node 才能运行表情库前端测试")
def test_sticker_library_tiles():
    script = Path(__file__).parent / "frontend" / "stickers.cjs"
    subprocess.run(["node", "--test", str(script)], check=True)
