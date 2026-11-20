"""设置页信息架构的测试辅助：用 Node 跑一遍前端归类逻辑。

前端 app.js 不打包、不引依赖，归类函数是纯函数；这里把 app.js 里
``pageOf`` 的源码抠出来放进 node 里对一遍，确保「后端规则」与「前端渲染」
说的是同一套页面。没有 node 时整组用例跳过（CI 只跑 Python 时不报红）。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parent.parent / "rubenbot" / "web" / "static" / "app.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 才能跑前端归类检查")


def _extract_function(name: str) -> str:
    """从 app.js 里按大括号配对抠出一个顶层函数源码。"""
    source = APP_JS.read_text(encoding="utf-8")
    start = source.index(f"function {name}(")
    depth = 0
    for index in range(source.index("{", start), len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"没有找到函数 {name} 的结尾")


def run_page_of(layout: dict, paths: list[str]) -> list[str]:
    """在 node 里调用 app.js 的 pageOf，返回每个路径的页面 id。"""
    script = "\n".join(
        [
            _extract_function("pageOf"),
            f"const layout = {json.dumps(layout)};",
            f"const paths = {json.dumps(paths)};",
            "console.log(JSON.stringify(paths.map((path) => pageOf(layout, path))));",
        ]
    )
    result = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def test_frontend_uses_backend_rules():
    """前端的 pageOf 必须与后端 resolve_page 给出相同结果。"""
    from rubenbot.config import Config
    from rubenbot.web.config_schema import (
        config_layout,
        field_paths,
        resolve_page,
        structured_config,
    )

    layout = json.loads(json.dumps(config_layout()))
    paths = field_paths(structured_config(Config()))
    pages = run_page_of(layout, paths)
    assert pages == [resolve_page(path) for path in paths]
    assert all(page for page in pages), "每个字段都应归到一个二级页面"


def test_frontend_prefix_behavior():
    """最长前缀 + 按段匹配：``chatty`` 不能被 ``chat`` 规则吃掉。"""
    from rubenbot.web.config_schema import config_layout

    layout = json.loads(json.dumps(config_layout()))
    pages = run_page_of(layout, ["chat.split_max_parts", "chat.bot_name", "chatty.foo", "persona"])
    assert pages == ["expression", "chat", "", ""]


def test_frontend_script_is_syntactically_valid():
    """整份 app.js 能被 node 解析（没有构建链，语法错误只能靠这一步抓）。"""
    result = subprocess.run(
        ["node", "--check", str(APP_JS)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
