"""双击运行入口: .pyw 由 pythonw.exe 执行, 不会弹出控制台黑窗口。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from Nobplaypot import main

if __name__ == "__main__":
    main()
