#!/usr/bin/env python
"""把工作台打包成单个 exe。

    python build_exe.py

产物 `dist/创作工坊.exe`：目标机器不需要装 Python，双击即用。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# CI 里用 ASCII 名：runner 上 bash 把非 ASCII 文件名交给 gh 时会丢，
# GitHub 收到空文件名后存成 default.exe（2026-09-24 实测）。本地构建不受影响。
NAME = os.environ.get("WORKSHOP_EXE_NAME") or "创作工坊"
SEEDS = ("providers.yaml", "primitives.yaml", "work.yaml")

# 输出被重定向（CI、写日志文件）时 stdout 不再是控制台，Python 会退回 ANSI 代码页
# （英文 Windows 上是 cp1252），中文 print 会直接抛 UnicodeEncodeError 把构建打挂。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def build_args() -> list[str]:
    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile",
        "--name", NAME,
        "--paths", str(ROOT / "src"),
        # 前端静态文件：server.main 按 <模块目录>/static 找它
        "--add-data", f"{ROOT / 'server' / 'static'}{os.pathsep}server/static",
        # 上传接口靠它，FastAPI 里是延迟导入，静态分析抓不到
        "--hidden-import", "multipart",
    ]
    # 配置模板：首次启动由 launcher.py 铺到用户数据目录
    for name in SEEDS:
        args += ["--add-data", f"{ROOT / name}{os.pathsep}defaults"]
    return args + [str(ROOT / "launcher.py")]


def main() -> int:
    print("打包中……（首次约 1~2 分钟）")
    result = subprocess.run(build_args(), cwd=ROOT)
    if result.returncode != 0:
        return result.returncode

    exe = ROOT / "dist" / f"{NAME}.exe"
    if not exe.exists():
        print(f"打包命令返回成功，但没找到产物：{exe}")
        return 1
    print(f"完成：{exe}（{exe.stat().st_size / 1048576:.1f} MB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
