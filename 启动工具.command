#!/bin/bash
# GhostMic 启动脚本
# 兼容旧启动入口：优先打开 Mac 应用，未构建时使用网页版。

cd "$(dirname "$0")" || exit 1

# 新版应用可直接启动；保留旧入口，避免用户需要更换习惯。
for app in "$PWD/GhostMic.app" "$HOME/Applications/GhostMic.app" "/Applications/GhostMic.app"; do
  if [ -d "$app" ] && [ "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$app/Contents/Info.plist" 2>/dev/null)" = "com.ghostmic.desktop" ]; then
    exec /usr/bin/open -a "$app"
  fi
done

# 按优先级找一个可用的 python（必须有 sounddevice / numpy）
CANDIDATES=(
  "$HOME/.workbuddy/binaries/python/envs/default/bin/python"
  "$(command -v python3)"
  "/opt/homebrew/bin/python3"
  "/usr/local/bin/python3"
  "/usr/bin/python3"
)

PY=""
for p in "${CANDIDATES[@]}"; do
  [ -n "$p" ] && [ -x "$p" ] || continue
  if "$p" -c "import sounddevice, numpy" >/dev/null 2>&1; then
    PY="$p"
    break
  fi
done

if [ -z "$PY" ]; then
  echo "找不到装了 sounddevice 和 numpy 的 Python。"
  echo ""
  echo "请先在终端里执行一次："
  echo "  python3 -m pip install --user sounddevice numpy"
  echo ""
  read -n 1 -s -r -p "按任意键退出..."
  exit 1
fi

echo "使用 Python：$PY"
echo "启动中…浏览器会自动打开 http://127.0.0.1:8787/"
echo "（这个窗口保持开着，关掉它就等于关闭工具）"
echo ""

exec "$PY" server.py
