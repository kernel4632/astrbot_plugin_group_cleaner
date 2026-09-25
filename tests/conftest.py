"""把插件父目录加入 sys.path，使测试能 import 插件包。

本插件的 cleaner/state 模块只依赖标准库，此处不需要桩模块。
"""

import sys
from pathlib import Path

PARENT = Path(__file__).resolve().parents[1].parent
if str(PARENT) not in sys.path:
    sys.path.insert(0, str(PARENT))
