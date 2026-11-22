#!/bin/zsh
set -eu
cd "$(dirname "$0")/.."
export PYINSTALLER_CONFIG_DIR="$PWD/work/pyinstaller-cache"
.venv/bin/python scripts/build_icon.py
.venv/bin/python -m PyInstaller --noconfirm --workpath work/packaging DJTOOL.spec
