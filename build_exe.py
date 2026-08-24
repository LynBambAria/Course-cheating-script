"""
自动化打包脚本：将刷课脚本打包为完全独立的 Windows .exe 应用程序
包含完整 Python 运行时、OpenCV、PyAutoGUI、Pillow、NumPy 及内置模板资源
在没有安装 Python 的新机器上双击即可直接运行
"""

import os
import sys
import subprocess
import shutil
import time
from pathlib import Path

# 设置控制台输出为 UTF-8
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
DIST_DIR = BASE_DIR / "dist"
BUILD_DIR = BASE_DIR / "build"
EXE_NAME = "刷课脚本"


def check_and_install_dependencies():
    """检查依赖，仅在缺少时自动安装"""
    print("=" * 60)
    print("🔍 检查项目依赖...")
    print("=" * 60)
    
    missing = []
    try:
        import PyInstaller
    except ImportError:
        missing.append("pyinstaller")

    try:
        import pyautogui
    except ImportError:
        missing.append("pyautogui")

    try:
        import PIL
    except ImportError:
        missing.append("pillow")

    try:
        import numpy
    except ImportError:
        missing.append("numpy")

    try:
        import cv2
    except ImportError:
        missing.append("opencv-python-headless")

    if not missing:
        print("✅ 所有依赖均已就绪，无需重复安装。\n")
        return

    print(f"📦 正在安装缺失依赖: {', '.join(missing)}...")
    python_exe = sys.executable
    cmd = [python_exe, "-m", "pip", "install"] + missing
    try:
        subprocess.check_call(cmd)
        print("✅ 依赖安装完成！\n")
    except Exception as e:
        print(f"⚠️ 安装依赖遇到问题: {e}")


def close_running_instances():
    """清理正在运行的旧版进程，防止文件占用导致写入拒绝"""
    try:
        subprocess.run(
            ["taskkill", "/F", "/IM", f"{EXE_NAME}.exe"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        time.sleep(1)
    except Exception:
        pass


def build_executable():
    """执行 PyInstaller 打包"""
    print("=" * 60)
    print("🚀 开始使用 PyInstaller 进行独立可执行程序打包...")
    print("=" * 60)

    close_running_instances()

    # 确保 templates 目录存在
    if not TEMPLATES_DIR.exists():
        TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)

    template_arg = f"{TEMPLATES_DIR};templates"

    pyinstaller_cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--windowed",
        "--name", EXE_NAME,
        "--add-data", template_arg,
        "--hidden-import", "cv2",
        "--hidden-import", "numpy",
        "--hidden-import", "PIL",
        "--hidden-import", "PIL.ImageGrab",
        "--hidden-import", "pyautogui",
        "--hidden-import", "tkinter",
        "--hidden-import", "tkinter.ttk",
        "--hidden-import", "tkinter.scrolledtext",
        "--hidden-import", "tkinter.messagebox",
        "--collect-all", "cv2",
        "--collect-all", "pyautogui",
        "--collect-all", "PIL",
        str(BASE_DIR / "auto_course.py")
    ]

    print("执行命令:", " ".join(pyinstaller_cmd))
    result = subprocess.run(pyinstaller_cmd, cwd=str(BASE_DIR))
    if result.returncode != 0:
        print("❌ 打包失败，请检查上方错误信息。")
        return False

    print("\n" + "=" * 60)
    print("🎉 独立 EXE 程序打包成功！")
    print("=" * 60)

    target_exe = DIST_DIR / f"{EXE_NAME}.exe"
    dist_templates = DIST_DIR / "templates"

    # 同步 templates 文件夹到 dist 目录
    if TEMPLATES_DIR.exists():
        dist_templates.mkdir(parents=True, exist_ok=True)
        for item in TEMPLATES_DIR.glob("*.png"):
            shutil.copy2(item, dist_templates / item.name)
            print(f"已同步模板文件: templates/{item.name} -> dist/templates/")

    # 复制说明文件
    readme_file = BASE_DIR / "README.md"
    if readme_file.exists():
        shutil.copy2(readme_file, DIST_DIR / "README.md")
        print("已复制 README.md 到 dist 目录")

    # 复制捕获模板脚本
    capture_bat = BASE_DIR / "capture_templates.bat"
    if capture_bat.exists():
        shutil.copy2(capture_bat, DIST_DIR / "capture_templates.bat")

    # 统计产物大小
    if target_exe.exists():
        size_mb = target_exe.stat().st_size / (1024 * 1024)
        print(f"\n📁 独立可执行程序产物路径: {target_exe}")
        print(f"📦 文件大小: {size_mb:.2f} MB (包含全部 Python 运行时与依赖库)")
        print(f"💡 该 exe 文件可直接发送给任意 Windows 电脑，无需安装 Python 即可运行！")

    return True


if __name__ == "__main__":
    check_and_install_dependencies()
    success = build_executable()
    if not success:
        sys.exit(1)
