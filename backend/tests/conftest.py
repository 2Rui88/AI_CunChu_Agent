"""
测试公共配置：
- 把 backend 目录加入 sys.path（支持从任意目录以 pytest 运行）
- Windows 下桩掉 fcntl（Unix-only，faiss_service 仅在锁操作时真正使用；
  测试中锁相关函数全部被 mock，导入期只需模块存在）
"""
import sys
import types
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

if sys.platform == "win32" and "fcntl" not in sys.modules:
    try:
        import fcntl  # noqa: F401
    except ModuleNotFoundError:
        stub = types.ModuleType("fcntl")
        stub.LOCK_EX = 2
        stub.LOCK_UN = 8
        stub.LOCK_NB = 4
        sys.modules["fcntl"] = stub
