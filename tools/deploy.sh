#!/bin/sh
# 入口不依赖当前工作目录；优先使用本仓库安装了 dev 依赖的虚拟环境。
set -eu
root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root"
python=.venv/bin/python
[ -x "$python" ] || python=python3
exec "$python" tools/deploy.py "$@"
