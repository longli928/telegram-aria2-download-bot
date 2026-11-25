#!/bin/bash
# setup.sh：在没有 root/apt/docker 的最小 Linux 环境上装好 aria2 和 rclone（静态二进制）。
# 用法：在项目目录里跑 ./setup.sh
# 依赖：curl、python3（解压用 python3 -m zipfile，这机器没有 unzip）。
set -e
cd "$(dirname "$0")"

ARIA2_URL="https://github.com/abcfy2/aria2-static-build/releases/download/1.37.0/aria2-x86_64-linux-musl_static.zip"
RCLONE_URL="https://downloads.rclone.org/rclone-current-linux-amd64.zip"

mkdir -p bin downloads
# 临时目录不要放 /tmp：有些容器的 /tmp 是很小的 tmpfs（比如只有 100M），
# 解压 rclone（约 85M 二进制）会报 No space left on device；改用磁盘上的项目内目录。
TMPDIR="${TMPDIR:-$PWD/.setup-tmp}"
mkdir -p "$TMPDIR"
trap 'rm -rf "$TMPDIR"' EXIT

echo "== 检查 =="
command -v curl >/dev/null || { echo "找不到 curl，先装 curl"; exit 1; }
command -v python3 >/dev/null || { echo "找不到 python3"; exit 1; }

echo "== 下载 aria2 1.37.0（静态版） =="
if [ -x bin/aria2c ]; then
    echo "bin/aria2c 已存在，跳过下载"
else
    curl -L --fail -o "$TMPDIR/aria2.zip" "$ARIA2_URL"
    python3 -m zipfile -e "$TMPDIR/aria2.zip" "$TMPDIR/aria2"
    # zip 里只有一个文件：aria2c
    cp "$TMPDIR/aria2/aria2c" bin/aria2c
    chmod +x bin/aria2c
fi
./bin/aria2c --version | head -1

echo "== 下载 rclone（当前版） =="
if [ -x bin/rclone ]; then
    echo "bin/rclone 已存在，跳过下载"
else
    curl -L --fail -o "$TMPDIR/rclone.zip" "$RCLONE_URL"
    python3 -m zipfile -e "$TMPDIR/rclone.zip" "$TMPDIR/rclone"
    cp "$TMPDIR/rclone"/rclone-*-linux-amd64/rclone bin/rclone
    chmod +x bin/rclone
fi
./bin/rclone version | head -1

echo "== 装 Python 依赖 =="
# Ubuntu 24.04 的系统 Python 是 externally-managed，直接 pip 会报错；
# 优先建 venv（最干净），建不了再退到 --break-system-packages。
if python3 -m venv .venv 2>/dev/null; then
    .venv/bin/pip install -q -r requirements.txt
    echo "依赖装进 .venv，启动用：.venv/bin/python app.py"
elif python3 -m pip --version >/dev/null 2>&1; then
    python3 -m pip install --break-system-packages -r requirements.txt \
        || echo "pip 安装失败，手动装：pip install -r requirements.txt"
else
    echo "没有 venv/pip，手动装依赖：pip install -r requirements.txt"
fi

echo ""
echo "装好了。下一步："
echo "  1. cp .env.example .env，填 API_ID / API_HASH / BOT_TOKEN / ALLOWED_USER_IDS"
echo "  2. 启动：.venv/bin/python app.py（没建出 venv 就用 python3 app.py）"
echo "  3. 在 Telegram 里发 /storage 加网盘（或直接把已有的 rclone.conf 拷过来，再在 bot 里点“已在 rclone 配好（导入）”）"
