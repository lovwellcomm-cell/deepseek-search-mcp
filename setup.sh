#!/usr/bin/env bash
# macOS / Linux 安装脚本：找到 Python 3.11+，然后运行 install.py
# 用法：bash setup.sh             安装
#       bash setup.sh --uninstall 卸载
set -e
cd "$(dirname "$0")"

for py in python3.14 python3.13 python3.12 python3.11 python3 python; do
  if command -v "$py" >/dev/null 2>&1 && "$py" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    exec "$py" install.py "$@"
  fi
done

echo "[X] 没有找到 Python 3.11 或更新版本。"
echo "    macOS 可以运行：brew install python@3.12，或到 https://www.python.org/downloads/ 下载安装。"
exit 1
