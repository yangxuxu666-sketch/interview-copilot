#!/bin/bash
set -eu
cd -- "$(dirname -- "$0")"
if [ "$(uname -s)" != "Darwin" ]; then
  echo "这个启动包适用于苹果 Mac 电脑。"
  exit 1
fi
TASK_PYTHON=""
for candidate in "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13" "/opt/homebrew/bin/python3.13" "/usr/local/bin/python3.13"; do
  if [ -x "$candidate" ]; then TASK_PYTHON="$candidate"; break; fi
done
if [ -z "$TASK_PYTHON" ]; then
  candidate="$(command -v python3.13 || true)"
  if [ -n "$candidate" ] && [ "$candidate" != "/usr/bin/python3.13" ]; then TASK_PYTHON="$candidate"; fi
fi
if [ -z "$TASK_PYTHON" ]; then
  echo "首次启动需要先安装 Python 3.13。已打开 Python 官网下载页。"
  echo "选择 macOS installer，安装完成后再双击本文件。无需安装 Xcode 或 Homebrew。"
  /usr/bin/open "https://www.python.org/downloads/release/python-31315/"
  read -r -p "按回车关闭。" _task_reply
  exit 1
fi
if ! "$TASK_PYTHON" "bootstrap.py"; then
  echo "启动没有完成，请保留上面的错误信息。"
  read -r -p "按回车关闭。" _task_reply
  exit 1
fi
