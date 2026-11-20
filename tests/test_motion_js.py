"""随全量 pytest 一起验证前端弹簧的物理响应和帧调度。"""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 Node 才能运行弹簧测试")
def test_spring_motion():
    script = Path(__file__).parent / "frontend" / "motion.cjs"
    subprocess.run(["node", "--test", str(script)], check=True)
