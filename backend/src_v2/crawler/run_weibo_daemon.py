#!/usr/bin/env python3
"""
微博爬虫守护进程 - 确保稳定长时间运行
用法: python run_weibo_daemon.py
"""
import subprocess
import sys
from pathlib import Path

# 切换到脚本所在目录
script_dir = Path(__file__).parent
output_dir = Path("../../data/harmful_weibo")
output_dir.mkdir(parents=True, exist_ok=True)

# 构建命令
cmd = [
    sys.executable,
    str(script_dir / "weibo_playwright.py"),
    "--keywords-file", str(script_dir / "keywords_harmful_v2.txt"),
    "--output", str(output_dir),
    "--num", "50",
    "--delay", "2.0",
    "--headless",
]

print("Starting Weibo crawler daemon...")
print(f"Command: {' '.join(cmd)}")
print(f"Output: {output_dir}")
print("=" * 50)

# 使用 subprocess.Popen 启动真正的后台进程
# creationflags 确保在 Windows 上关闭窗口后进程继续运行
process = subprocess.Popen(
    cmd,
    stdout=open(output_dir / "weibo_stdout.log", "w", encoding="utf-8"),
    stderr=open(output_dir / "weibo_stderr.log", "w", encoding="utf-8"),
    cwd=str(script_dir),
)

print(f"Process started with PID: {process.pid}")
print(f"Logs: {output_dir / 'weibo_stdout.log'}")
print("You can close this window now. The crawler will continue running.")

# 等待进程结束
process.wait()
print("Crawler finished.")
