"""随全量 pytest 验证前端切页、缓存刷新与编辑保护。"""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 Node 才能运行前端路由测试")
def test_route_cache():
    script = Path(__file__).parent / "frontend" / "route_cache.cjs"
    subprocess.run(["node", "--test", str(script)], check=True)
