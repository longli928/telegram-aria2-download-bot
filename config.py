#!/usr/bin/env python3
"""配置：全部从 .env / 环境变量读，代码里不写任何密钥。

环境变量：
  API_ID, API_HASH, BOT_TOKEN     Telegram bot 凭据
  ALLOWED_USER_IDS                白名单，逗号分隔的 Telegram user id；为空谁都不能用
  ARIA2_SECRET                    aria2 RPC 密钥；不填就自动生成，存进 aria2-secret.txt（chmod 600）
  ARIA2_BIN                       aria2c 二进制路径，默认 ./bin/aria2c
  RCLONE_BIN                      rclone 二进制路径，默认 ./bin/rclone（找不到就用系统 PATH 里的 rclone）
  RCLONE_CONFIG                   rclone 配置文件路径，默认 ~/.config/rclone/rclone.conf
  DOWNLOAD_DIR                    下载目录，默认 ./downloads
  MAX_CONCURRENT_TASKS            同时跑的下载任务数上限，默认 3；0 = 不限制
  TIMEZONE                        没设过时区的用户的默认时区，默认 Asia/Shanghai
  SERVER_PORT                     HTTP 保活端口，默认 8080
"""
import os
import re
import time

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

API_ID = int(os.environ.get("API_ID", "0") or 0)
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

ALLOWED = {
    int(x.strip())
    for x in os.environ.get("ALLOWED_USER_IDS", "").split(",")
    if x.strip().isdigit()
}

ARIA2_SECRET = os.environ.get("ARIA2_SECRET", "") or ""
ARIA2_BIN = os.environ.get("ARIA2_BIN", "") or os.path.join(BASE_DIR, "bin", "aria2c")
RCLONE_BIN = os.environ.get("RCLONE_BIN", "") or os.path.join(BASE_DIR, "bin", "rclone")
RCLONE_CONFIG = os.environ.get("RCLONE_CONFIG") or os.path.expanduser("~/.config/rclone/rclone.conf")
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "") or os.path.join(BASE_DIR, "downloads")
MAX_CONCURRENT_TASKS = int(os.environ.get("MAX_CONCURRENT_TASKS", "3") or 0)
TIMEZONE = os.environ.get("TIMEZONE", "Asia/Shanghai")
SERVER_PORT = int(os.environ.get("SERVER_PORT", os.environ.get("PORT", "8080")) or 8080)

ARIA2_RPC_HOST = "127.0.0.1"
ARIA2_RPC_PORT = int(os.environ.get("ARIA2_RPC_PORT", "6800") or 6800)


def log(*a):
    print(f"[{time.strftime('%H:%M:%S')}]", *a, flush=True)


def _redact(s):
    """把日志/报错里的 token 之类长串抹掉，避免密钥进日志。"""
    if not s:
        return ""
    return re.sub(r"[A-Za-z0-9_\-!*/+=.]{80,}", "[REDACTED]", str(s))
