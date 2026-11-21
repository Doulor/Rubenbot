# 参与开发

## 环境

Python 3.12。安装开发依赖并跑全量测试：

```bash
pip install -e ".[dev]"
pytest
```

代码检查用 ruff（配置在 `pyproject.toml` 的 `[tool.ruff]`）：

```bash
ruff check .
```

## 改动约定

- 注释与文档用中文，标识符用英文。
- 先读 `SPEC.md`，记忆层相关改动必须严格按规格实现；规格没写清的地方按「轻量、简单、
  不过度设计」原则自行拿主意。
- 代码原创（MIT）；只借鉴思路，不复制 GPL 项目的代码。
- 核心依赖保持轻量（httpx、websockets、pyyaml、mcp），测试用 pytest。
- 新增行为要带测试，提交前跑通全量 pytest 与 `ruff check .`。

## 版本与发布

- 版本号只在 `pyproject.toml` 的 `project.version` 维护，遵循 [语义化版本](https://semver.org/lang/zh-CN/)
  （`MAJOR.MINOR.PATCH`：不兼容改动 / 向下兼容的新功能 / 向下兼容的修复）。
- 发布时在对应提交上打 `vX.Y.Z` 标签（如 `v0.1.0`），与 `pyproject.toml` 里的版本号一致。
- 同一次发布同步更新 `CHANGELOG.md`：把 `Unreleased` 小节的内容整理到新的 `[X.Y.Z] - YYYY-MM-DD` 小节。
- 插件 manifest 的 `min_core_version` 使用同一套版本号，发布前确认与实际能力匹配。
