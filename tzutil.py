#!/usr/bin/env python3
"""时区：每个用户自己设，存在 timezones.json 里（Telegram 不提供用户时区）。

/timezone：不带参数看当前时区并等输入；/timezone -8 直接设置。
接受 UTC 序号（-12 到 +12，固定偏移，不跟夏令时）或 IANA 名（如 Europe/London）。
"""
import json
import os
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import config

_TZ_FILE = os.path.join(config.BASE_DIR, "timezones.json")
_user_timezones = {}


def load_user_timezones():
    global _user_timezones
    try:
        with open(_TZ_FILE, encoding="utf-8") as f:
            _user_timezones = json.load(f) or {}
    except Exception:
        _user_timezones = {}


def save_user_timezones():
    try:
        with open(_TZ_FILE, "w", encoding="utf-8") as f:
            json.dump(_user_timezones, f, ensure_ascii=False, indent=2)
        os.chmod(_TZ_FILE, 0o600)
    except Exception as e:
        config.log(f"保存用户时区失败：{e}")


def default_tz():
    try:
        return ZoneInfo(config.TIMEZONE), config.TIMEZONE
    except Exception:
        return timezone.utc, "UTC"


def parse_tz_name(name):
    """解析用户输入的时区，返回 (tz, 显示名)，解析不了返回 None。"""
    if not name:
        return None
    s = name.strip()
    m = re.fullmatch(r"(?:UTC)?([+-]?\d{1,2})", s, re.IGNORECASE)
    if m:
        try:
            hours = int(m.group(1))
        except Exception:
            return None
        if -12 <= hours <= 12:
            if hours == 0:
                return timezone.utc, "UTC"
            sign = "+" if hours > 0 else "-"
            return timezone(timedelta(hours=hours)), f"UTC{sign}{abs(hours)}"
        return None
    try:
        return ZoneInfo(s), s
    except Exception:
        return None


def get_user_tz(uid):
    """取某个用户的时区：设过就用他的，没设过用 TIMEZONE 默认值。"""
    name = _user_timezones.get(str(uid))
    if name:
        parsed = parse_tz_name(name)
        if parsed:
            return parsed
    return default_tz()


def set_user_tz(uid, display_name):
    _user_timezones[str(uid)] = display_name
    save_user_timezones()


def fmt_time(s, tz=None):
    """把 rclone 的 UTC 时间（如 2026-11-24T18:52:12Z）换算成指定时区的本地时间显示。"""
    if not s:
        return s
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00").replace("z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        tz = tz or default_tz()[0]
        return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return s
