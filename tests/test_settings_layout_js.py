"""设置页信息架构的测试辅助：用 Node 跑一遍前端归类逻辑。

前端 app.js 不打包、不引依赖，归类函数是纯函数；这里把 app.js 里
``pageOf`` 的源码抠出来放进 node 里对一遍，确保「后端规则」与「前端渲染」
说的是同一套页面。没有 node 时整组用例跳过（CI 只跑 Python 时不报红）。
"""

from __future__ import annotations

import json
import shutil
import subprocess
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


def test_model_fields_belong_only_to_models_page():
    """两张卡的所有字段（包括模型池逐角色策略）只归模型页。"""
    from rubenbot.config import Config
    from rubenbot.web.config_schema import config_layout, field_paths, structured_config

    layout = config_layout()
    paths = [p for p in field_paths(structured_config(Config())) if p.startswith("llm.")]
    paths += ["llm.models.fast", "llm.model_strategies.fast"]
    assert set(run_page_of(layout, paths)) == {"models"}
    assert [g["route"] for g in layout["groups"] if any(p["id"] == "models" for p in g["pages"])] == ["models"]


def test_frontend_prefix_behavior():
    """最长前缀 + 按段匹配：``chatty`` 不能被 ``chat`` 规则吃掉。"""
    from rubenbot.web.config_schema import config_layout

    layout = json.loads(json.dumps(config_layout()))
    pages = run_page_of(layout, ["reply.max_segments", "chat.bot_name", "chatty.foo", "persona"])
    assert pages == ["expression", "chat", "", ""]


def card_fields(page: str, prefix: str) -> dict:
    """返回指定页面的实际卡片字段定义，而不只检查归属规则。"""
    from rubenbot.web.config_schema import config_layout

    source = APP_JS.read_text(encoding="utf-8")
    # 无浏览器也能跑真实卡片清单：用字段定义代替 DOM 节点，其他专用编辑器不参与。
    start = source.index("function settingsCardSpecs(")
    cards = source[start : source.index("\n}", start) + 2]
    script = cards + _extract_function("pageOf") + """
const card = (_, spec) => spec, fieldGrid = defs => defs;
const h = () => null, groupsEditor = h, pluginEditor = h, modelsEditor = h,
      adaptersEditor = h, mcpServerEditor = h, mapEditor = h;
const LOG_LEVELS = [], MOOD_NAMES = [], MODEL_POOL_STRATEGIES = [], MODEL_POOL_STRATEGY_LABELS = {};
const [layout, page, prefix] = JSON.parse(process.argv[1]);
const fields = settingsCardSpecs({}, {}, []).filter(c => pageOf(layout, c.path) === page)
  .flatMap(c => c.node.body).filter(f => f?.path?.startsWith(prefix));
console.log(JSON.stringify(fields));
"""
    args = json.dumps([config_layout(), page, prefix])
    result = subprocess.run(["node", "-e", script, args], capture_output=True, text=True, check=True)
    return {field["path"]: field for field in json.loads(result.stdout)}


def test_memory_card_defines_privacy_switch():
    field = card_fields("memory", "privacy.")["privacy.enabled"]
    assert field["kind"] == "switch"
    assert "开启后启用保密封存与跨场合审查，默认关" in field["hint"]


def test_attention_card_defines_two_number_inputs():
    fields = card_fields("expression", "attention.")
    assert set(fields) == {"attention.talk_value", "attention.engaged_window_seconds"}
    assert all(field["kind"] == "number" for field in fields.values())
    assert "不是回复概率" in fields["attention.talk_value"]["hint"]


def test_frontend_script_is_syntactically_valid():
    """整份 app.js 能被 node 解析（没有构建链，语法错误只能靠这一步抓）。"""
    result = subprocess.run(
        ["node", "--check", str(APP_JS)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_settings_menu_is_rendered_before_config_request():
    """慢配置接口不能阻塞机器人/设置页的二级菜单和页头。"""
    source = APP_JS.read_text(encoding="utf-8")
    render_start = source.index("async function renderSettingsGroup")
    menu_render = source.index("settingsMenuSkeleton(", render_start)
    config_request = source.index('const data = await api.get("/api/config")', render_start)
    assert menu_render < config_request
