"""从源码目录直接启动：`.venv/bin/python main.py`。真正的入口在 djtool/app.py。"""
import sys

from djtool.app import main

if __name__ == '__main__':
    sys.exit(main())
