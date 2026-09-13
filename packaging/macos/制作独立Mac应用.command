#!/bin/bash
set -eu
cd -- "$(dirname -- "$0")"
TASK_PYTHON=""
for candidate in "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13" "/opt/homebrew/bin/python3.13" "/usr/local/bin/python3.13"; do
  if [ -x "$candidate" ]; then TASK_PYTHON="$candidate"; break; fi
done
if [ -z "$TASK_PYTHON" ]; then TASK_PYTHON="$(command -v python3.13 || true)"; fi
if [ -z "$TASK_PYTHON" ]; then
  echo "请先安装 Python 3.13，再制作独立应用。"
  /usr/bin/open "https://www.python.org/downloads/release/python-31315/"
  read -r -p "按回车关闭。" _task_reply
  exit 1
fi
if ! "$TASK_PYTHON" "bootstrap.py" --build; then
  echo "制作没有完成，请保留上面的错误信息。"
  read -r -p "按回车关闭。" _task_reply
  exit 1
fi
echo "制作完成。mac-dist 内的压缩包可以发给使用相同芯片类型的朋友。"
read -r -p "按回车关闭。" _task_reply
