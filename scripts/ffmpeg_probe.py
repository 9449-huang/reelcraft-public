#!/usr/bin/env python3
"""ffmpeg_probe.py — 跨平台定位 ffmpeg 可执行文件（标准库零依赖）。

解析优先级：
  1. 环境变量 FFMPEG（显式指定）
  2. PATH 中的 ffmpeg（系统 / 包管理器安装）
  3. imageio_ffmpeg 自带的二进制（若已 pip 安装）
  4. 常见的 WorkBuddy 托管 Python 站点包路径（向后兼容旧硬编码）

找不到时抛 FileNotFoundError，并提示设置 FFMPEG 或安装 ffmpeg。
这样 scripts/ 下的 media_gen.py / postprocess.py / vo_build.py 不用把绝对路径写死，
换个机器 / 用户也能跑（原硬编码路径只指向某一台机器的某一 Python 版本）。
"""
from __future__ import annotations
import os
import shutil
from pathlib import Path


def find_ffmpeg() -> str:
    # 1. 显式环境变量
    env = os.environ.get("FFMPEG")
    if env and os.path.isfile(env):
        return env

    # 2. PATH（系统已装 ffmpeg / 包管理器装的可直接命中）
    on_path = shutil.which("ffmpeg")
    if on_path:
        return on_path

    # 3. imageio_ffmpeg 自带二进制（若已 pip 安装）
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.isfile(exe):
            return exe
    except Exception:
        pass

    # 4. 向后兼容：旧版硬编码的托管 Python 站点包位置
    candidates = [
        Path.home() / ".workbuddy" / "binaries" / "python" / "versions" / "3.13.12"
        / "Lib" / "site-packages" / "imageio_ffmpeg" / "binaries" / "ffmpeg-win-x86_64-v7.1.exe",
    ]
    for c in candidates:
        if c.is_file():
            return str(c)

    raise FileNotFoundError(
        "未找到 ffmpeg。请二选一：① 设置环境变量 FFMPEG 指向可执行文件；"
        "② 将 ffmpeg 加入 PATH（如 Windows 用 'choco install ffmpeg'，"
        "macOS 用 'brew install ffmpeg'，Linux 用包管理器安装 ffmpeg）。"
    )
