"""WebUI（第二阶段，可选依赖）。

这个包本身不导入 FastAPI，主程序只有在启用 WebUI 时才会按需导入
:mod:`rubenbot.web.app`，未安装 ``rubenbot[webui]`` 时核心功能不受影响。
"""
