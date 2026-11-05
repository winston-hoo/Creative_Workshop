"""pytest 全局夹具与跨模块隔离。

## 为什么需要 `_isolate_server_root`

各个接口自检模块都在**导入时**把自己的临时根目录写进 `server_main.PATHS`
（`server_main.PATHS = services.Paths(root=TMP_ROOT)`）。pytest 在跑第一个测试
之前就会导入全部测试模块，于是最后被导入的那个模块的根目录成了全局赢家。

表现很隐蔽：单独跑任何一个模块都全绿，一起跑就冒出
「FileNotFoundError: …<临时目录>/workspaces/测试作品/00-ingest/chapters/v001-c0001.md」，
而且报错里的临时目录确实是这个模块自己的——因为文件被写进了另一个模块的根目录，
本目录只剩下空骨架。

修法不是调整导入顺序（文件改名就会破坏），而是每个测试开始前按**当前测试模块**
声明的 `TMP_ROOT` 重新绑定。没有 `TMP_ROOT` 的模块（自己用夹具管理根目录的）
保持原样，不受影响。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from server import main as server_main
from server import services


@pytest.fixture(autouse=True)
def _isolate_server_root(request):
    """把 `server_main.PATHS` 钉回当前测试模块自己的临时根目录。"""
    root = getattr(request.module, "TMP_ROOT", None)
    if root is not None:
        server_main.PATHS = services.Paths(root=Path(root))
    yield


@pytest.fixture
def tmp_dir(tmp_path: Path) -> Path:
    """供按脚本方式书写的测试使用（`test_draft.test_save_and_load` 直接收 Path）。"""
    return tmp_path
