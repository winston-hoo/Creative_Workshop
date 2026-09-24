#!/usr/bin/env python
"""本地工作台启动入口。

    python run_server.py                 # 默认 127.0.0.1:8765
    python run_server.py --port 9000

**只绑本机地址。** 作品原文不出本机，这条不是可选项，所以非本机地址会被直接拒绝。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="run_server", description="小说创作工坊 · 本地工作台")
    parser.add_argument("--host", default="127.0.0.1", help="只允许本机地址")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--reload", action="store_true", help="改代码自动重启（开发用）")
    args = parser.parse_args(argv)

    if args.host not in LOCAL_HOSTS:
        print(f"拒绝启动：只允许绑定本机地址（收到 {args.host}）。")
        print("作品原文不出本机是设计约束，不是可调参数。")
        return 2

    try:
        import uvicorn
    except ImportError:
        print("缺少 uvicorn。请先安装：")
        print("  pip install fastapi 'uvicorn[standard]'")
        return 2

    from server.main import app

    print(f"工作台启动：http://{args.host}:{args.port}")
    print(f"作品工作区：{ROOT / 'workspaces'}")
    print("按 Ctrl+C 停止。")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
