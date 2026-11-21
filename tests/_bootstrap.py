"""测试引导：隔离模式（python3 -I）下把仓库根加入 sys.path。

普通模式（python3 -m unittest）下 cwd 已在 sys.path，本引导为空操作。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
