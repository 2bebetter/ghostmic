#!/usr/bin/env python3
"""Prepare an isolated Python environment and build GhostMic from source."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENVIRONMENT = ROOT / ".venv"
PYTHON = ENVIRONMENT / "bin" / "python3"


def run(*command):
    subprocess.run(command, cwd=ROOT, check=True)


def main():
    if sys.platform != "darwin":
        raise SystemExit("GhostMic 目前只支持在 macOS 上安装。")
    if sys.version_info < (3, 9):
        raise SystemExit("请安装 Python 3.9 或更新版本，然后重试。")
    tools = subprocess.run(
        ["/usr/bin/xcrun", "--find", "swiftc"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if tools.returncode:
        raise SystemExit("请先安装 Xcode 命令行工具：xcode-select --install")
    if ENVIRONMENT.exists() and not PYTHON.is_file():
        raise SystemExit("项目中已有 .venv，但它不是可用的 Python 环境。请检查该目录后重试。")
    if not ENVIRONMENT.exists():
        print("正在准备项目专用的 Python 环境…", flush=True)
        run(sys.executable, "-m", "venv", str(ENVIRONMENT))
    print("正在安装播放组件…", flush=True)
    run(str(PYTHON), "-m", "pip", "install", "--disable-pip-version-check", "-r", str(ROOT / "requirements.txt"))
    print("正在构建 GhostMic…", flush=True)
    run(str(PYTHON), str(ROOT / "scripts" / "build_macos_app.py"))
    print("\n安装完成。双击项目目录中的 GhostMic.app 即可打开。", flush=True)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        raise SystemExit(f"安装未完成（退出代码 {error.returncode}），请检查上面的错误后重试。") from error
