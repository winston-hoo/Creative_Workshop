#!/usr/bin/env python
"""一站式启动入口（打包成 exe 后的入口）。

双击 exe 即用：起本地服务 → 自动开浏览器 → 关掉窗口即停。

    python launcher.py                 # 源码方式跑，和双击 exe 等价
    python launcher.py --no-browser    # 不开浏览器（排查用）
    python launcher.py --port 9000

数据放在 `%LOCALAPPDATA%\\创作工坊`，首次启动自动铺一份配置模板，
**已存在的文件一律不覆盖**（用户改过的配置比模板重要）。
服务只绑 127.0.0.1，原文与密钥都不出本机。
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import webbrowser
from pathlib import Path

APP_NAME = "创作工坊"
DEFAULT_PORT = 8765
HOST = "127.0.0.1"  # 不是可调参数：作品原文不出本机
SEEDS = ("providers.yaml", "primitives.yaml", "work.yaml")


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def data_root() -> Path:
    """打包后放用户数据目录；源码运行时就是项目目录。"""
    if not is_frozen():
        return Path(__file__).resolve().parent
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / APP_NAME


def seed(target_root: Path) -> list[str]:
    """首次启动铺配置模板，只补缺的，不覆盖已有文件。"""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    source_dir = base / "defaults" if is_frozen() else base
    written: list[str] = []
    for name in SEEDS:
        target = target_root / name
        source = source_dir / name
        if target.exists() or not source.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        written.append(name)
    return written


def pick_port(preferred: int) -> int:
    """8765 被占了就换一个空闲端口，而不是让用户看到启动失败。"""
    for port in (preferred, 0):
        with socket.socket() as probe:
            try:
                probe.bind((HOST, port))
            except OSError:
                continue
            return int(probe.getsockname()[1])
    raise SystemExit("拒绝启动：找不到可用端口。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=APP_NAME, description="小说创作工坊 · 本地工作台")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true", help="不开浏览器")
    args = parser.parse_args(argv)

    root = data_root()
    root.mkdir(parents=True, exist_ok=True)
    created = seed(root)
    os.environ["WORKSHOP_ROOT"] = str(root)  # server.main 靠它决定数据根

    import uvicorn  # 放在这里，报错信息才不会被 PyInstaller 的启动噪音盖住

    from server.main import app

    port = pick_port(args.port)
    url = f"http://{HOST}:{port}"

    print(f"{APP_NAME} 已启动：{url}")
    print(f"作品与配置目录：{root}")
    if created:
        print("首次启动，已铺好配置模板：" + "、".join(created))
    print("关掉这个窗口即停止。")

    if not args.no_browser:
        threading.Timer(0.8, webbrowser.open, args=[url]).start()

    uvicorn.run(app, host=HOST, port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
