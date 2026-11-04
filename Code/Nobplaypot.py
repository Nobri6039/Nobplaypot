"""程序入口

本文件由 tools/split_modules.py 从原 Nobplaypot.py 自动拆分而来。
手改请直接改本文件; 重新运行拆分脚本会覆盖。
"""
import os as _nbp_os
import sys as _nbp_sys
_nbp_sys.path.insert(0, _nbp_os.path.dirname(_nbp_os.path.abspath(__file__)))
del _nbp_os, _nbp_sys


from np_core import *  # noqa: F401,F403

from np_net import *  # noqa: F401,F403

from np_widgets import *  # noqa: F401,F403

from np_web import *  # noqa: F401,F403

from np_appshare import *  # noqa: F401,F403

from np_dialogs import *  # noqa: F401,F403

from np_views import *  # noqa: F401,F403

from np_app import *  # noqa: F401,F403

# 兼容: import * 不会带出下划线开头的名字, 这里补上,
# 旧脚本/测试里 Nobplaypot._xxx 的写法仍然可用。
import importlib as _nbp_importlib
for _nbp_name in ("np_core", "np_net", "np_widgets", "np_web", "np_appshare", "np_dialogs", "np_views", "np_app"):
    globals().update({_k: _v for _k, _v in
                        vars(_nbp_importlib.import_module(_nbp_name)).items()
                        if _k.startswith('_') and not _k.startswith('__')})
del _nbp_name, _nbp_importlib

def main():
    setup_logging()
    install_excepthooks()
    # 上次崩溃/被强杀时可能留下 ffmpeg 采集孤儿进程, 启动时先清一遍
    threading.Thread(target=cleanup_orphan_ffmpeg, daemon=True,
                     name="ffmpeg-sweep").start()
    log.info("进入主循环")
    try:
        root = create_root()
        VideoPlayerApp(root)
        root.mainloop()
    except Exception:
        log.exception("主程序异常退出")
        raise
    finally:
        log.info("主循环结束")
        try:
            cleanup_orphan_ffmpeg()
        except Exception:
            pass


if __name__ == "__main__":
    main()
