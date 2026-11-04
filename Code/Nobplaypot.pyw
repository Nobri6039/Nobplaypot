"""Nobplaypot 图形入口。

启动器 launch.json 的 script 指向本文件，用 pythonw.exe 运行（无控制台）。
真正的代码在上一级的 Nobplaypot.py / np_*.py 里，这里只负责把目录加进
sys.path 再调用 main()。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import Nobplaypot  # noqa: E402

if __name__ == "__main__":
    Nobplaypot.main()
