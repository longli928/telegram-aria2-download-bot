#!/usr/bin/env python3
"""
Telegram 下载 bot：发链接 -> aria2 下载 -> rclone 传网盘 -> 删本地。

- 用 Telethon (MTProto, api_id/api_hash + bot_token)，绕过 Bot API 20MB 限制。
- /mirror <链接>：直链、magnet、.torrent 链接都行；直接发链接也行；发 .torrent 文件也行。
  bot 自己拉起 aria2c（./bin/aria2c，RPC 只监听 127.0.0.1:6800），下完用 rclone move
  传到默认存储，传完删本地文件。
- /storage：添加网盘、选默认存储、改保存位置、删网盘（逻辑见 storage.py，从老 bot 搬过来）。
  没设默认又多个网盘时，下载完成会问你传到哪个。
- /status：看进行中的任务；/cancel：取消任务。
- /timezone：设置上传时间的显示时区。
- 白名单：只有 ALLOWED_USER_IDS 里的用户能用；检查直接用 event.sender_id。
- 同时在 SERVER_PORT 上起一个极简 HTTP 服务做保活。

环境变量（在 .env 里填，千万别写进公开仓库）：
  API_ID, API_HASH, BOT_TOKEN, ALLOWED_USER_IDS,
  ARIA2_SECRET（不填自动生成，存 aria2-secret.txt）,
  ARIA2_BIN, RCLONE_BIN, RCLONE_CONFIG, DOWNLOAD_DIR,
  MAX_CONCURRENT_TASKS, TIMEZONE, SERVER_PORT
"""
import asyncio
import base64
import glob
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from telethon import Button, TelegramClient, events
from telethon import utils as tg_utils
from telethon.tl import functions as tg_functions
from telethon.tl import types as tg_types

import aria2rpc
import auth
import config
import rclone as rclone_mod
import storage as storage_mod
import tasks as tasks_mod
import tzutil

_HELP_TEXT = (
    "使用帮助\n"
    "\n"
    "📥 /mirror <链接>：下载一个链接，下完自动传到网盘，传完删本地。\n"
    "  直链、magnet、.torrent 链接都行；直接发链接给我也行；发 .torrent 文件也行。\n"
    "  一条消息里多个链接会各建一个任务。\n"
    "📊 /status：看进行中的任务。\n"
    "❌ /cancel：取消一个任务（会删本地没下完的文件）。\n"
    "🧹 /cleanup：清理 aria2 里 bot 不管的孤儿下载（比如重启后丢了的任务），删了就能重新发磁力。\n"
    "📦 /storage：添加网盘、选默认存储、改保存位置、删网盘。\n"
    "  添加：OAuth 的网盘点登录链接登录，登录后把跳回来的 localhost 链接发我；其它的按提示填配置。\n"
    "  默认存储：设了就直接往它传；没设又有多个网盘时，下载完成会问你传哪个。\n"
    "🕐 /timezone：设置上传时间的显示时区，发 UTC 序号（比如 -8、+8）或时区名（比如 Europe/London）。\n"
    "上传失败会有重试按钮；添加存储中途不想加了，发“取消”。"
)


# ---------- 单实例保护：绝不能两个 bot 进程同时跑 ----------
# 两个实例共用同一个 bot_session.session 会互相锁 SQLite，
# 一个崩（database is locked），另一个拿不到 sender 实体还可能乱回白名单提示。
# 启动时先看 bot.pid：里面 pid 还活着就直接退出；pid 已死就是残留文件，直接覆盖。
_PID_FILE = os.path.join(config.BASE_DIR, "bot.pid")


def _single_instance_guard():
    if os.path.exists(_PID_FILE):
        try:
            old = int(open(_PID_FILE, encoding="utf-8").read().strip())
        except Exception:
            old = 0
        if old:
            try:
                os.kill(old, 0)
                print(f"已有 bot 实例在运行（PID {old}），本次不启动，避免双开抢 session。")
                raise SystemExit(2)
            except ProcessLookupError:
                pass  # 旧进程已死，pidfile 是残留的，可以覆盖
            except PermissionError:
                print(f"bot.pid 里的 PID {old} 无法确认，本次不启动。")
                raise SystemExit(2)
    try:
        with open(_PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except Exception as e:
        print(f"写 bot.pid 失败：{e}")
        raise SystemExit(2)


def _clear_pid_file():
    try:
        if os.path.exists(_PID_FILE) and open(_PID_FILE, encoding="utf-8").read().strip() == str(os.getpid()):
            os.remove(_PID_FILE)
    except Exception:
        pass


# ---------- 占位 HTTP 服务（部分托管平台要求应用监听端口） ----------
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"ok - telegram bot running\n")

    def log_message(self, *a):
        pass


def serve_http():
    try:
        HTTPServer(("0.0.0.0", config.SERVER_PORT), Handler).serve_forever()
    except Exception as e:
        config.log(f"HTTP 服务启动失败（不影响 bot）: {e}")


def _is_torrent_document(event):
    """这条消息是不是发了一个 .torrent 文件。"""
    if not event.file:
        return False
    name = (event.file.name or "").lower()
    return name.endswith(".torrent")


async def _tracker_refresh_loop():
    """每天刷一次 tracker 列表、推一次提速参数，给正在跑的 aria2（不重启）。
    同步的 RPC 放线程里跑，别堵事件循环。"""
    while True:
        try:
            await asyncio.get_event_loop().run_in_executor(None, aria2rpc.refresh_trackers)
            await asyncio.get_event_loop().run_in_executor(None, aria2rpc.apply_tuning)
        except Exception as e:
            config.log(f"tracker 定时刷新失败：{e}")
        await asyncio.sleep(86400)


async def main():
    _single_instance_guard()
    if not config.API_ID or not config.API_HASH or not config.BOT_TOKEN:
        config.log("❌ 缺少 API_ID / API_HASH / BOT_TOKEN，bot 无法启动")
        config.log("设置环境变量或上传 .env 后重启")
        # 别直接退出，让 HTTP 服务活着，方便在面板里看到状态
        while True:
            time.sleep(3600)
    if not config.ALLOWED:
        config.log("⚠️ ALLOWED_USER_IDS 为空：白名单模式下谁都不能用 bot，请在 .env 里填 ALLOWED_USER_IDS")
    tzutil.load_user_timezones()
    storage_mod.load_providers()
    storage_mod.load_storages()
    config.log(f"已加载 {len(storage_mod.storage_list())} 个存储，默认：{storage_mod.default_storage_name() or '未设置'}")
    storage_mod.cleanup_stale_picker_configs()
    if not shutil.which("rclone") and not os.path.isfile(config.RCLONE_BIN):
        config.log("⚠️ 没找到 rclone：上传会失败，请先跑 ./setup.sh 装 rclone")
    if not (os.path.isfile(config.ARIA2_BIN) and os.access(config.ARIA2_BIN, os.X_OK)):
        config.log(f"⚠️ 没找到 aria2c（{config.ARIA2_BIN}）：下载会失败，请先跑 ./setup.sh 装 aria2")

    os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)
    threading.Thread(target=serve_http, daemon=True).start()
    config.log(f"HTTP 保活服务已起，端口 {config.SERVER_PORT}")

    # aria2：bot 自己拉起（RPC 不通才拉），拉不起来下载任务会直接报错。
    aria2_client = None
    try:
        aria2_client = aria2rpc.ensure_daemon()
    except Exception as e:
        config.log(f"⚠️ aria2 启动失败：{e}，/mirror 会报错，先跑 ./setup.sh 装好 aria2 再重启 bot")

    manager = tasks_mod.MirrorManager(aria2_client) if aria2_client else None
    storage_flow = storage_mod.StorageFlow()

    client = TelegramClient("bot_session", config.API_ID, config.API_HASH)
    await client.start(bot_token=config.BOT_TOKEN)
    me = await client.get_me()
    config.log(f"Bot 已上线：@{me.username} (id={me.id})，已配存储 {len(storage_mod.storage_list())} 个，默认：{storage_mod.default_storage_name() or '未设置'}")
    # 启动时顺手把命令菜单同步成代码里的。
    try:
        await client(tg_functions.bots.SetBotCommandsRequest(
            scope=tg_types.BotCommandScopeDefault(),
            lang_code="",
            commands=[
                tg_types.BotCommand("start", "开始使用"),
                tg_types.BotCommand("help", "使用帮助"),
                tg_types.BotCommand("mirror", "新建任务"),
                tg_types.BotCommand("status", "进行中的任务"),
                tg_types.BotCommand("cancel", "取消任务"),
                tg_types.BotCommand("cleanup", "清理孤儿下载"),
                tg_types.BotCommand("storage", "存储管理"),
                tg_types.BotCommand("timezone", "查看或设置时区"),
            ],
        ))
        config.log("命令菜单已同步")
    except Exception as e:
        config.log(f"同步命令菜单失败（不影响 bot）：{e}")

    # 等着输 UTC 序号的用户（点了 /timezone 还没发数字的），只放内存里。
    _tz_pending = set()

    async def _need_aria2(event):
        if manager is None:
            await event.reply("aria2 还没起来（没装好或者启动失败），先在服务器上跑 ./setup.sh，再重启 bot。")
            return None
        return manager

    async def _orphan_list_message(mgr):
        """孤儿列表的消息文本和按钮（/cleanup 和清理回调共用）。"""
        orphans = await mgr.list_orphans()
        if not orphans:
            return "✅ 没有孤儿下载，aria2 里的都是 bot 在管的任务。", None
        lines = ["🧹 aria2 里 bot 不管的孤儿下载："]
        buttons = []
        state_text = {
            "active": "⬇️ 下载中",
            "waiting": "⏳ 等待",
            "paused": "⏸ 已暂停",
            "complete": "✅ 已完成",
            "error": "❌ 出错",
        }
        for o in orphans:
            pct = o["done"] * 100 // o["total"] if o["total"] else 0
            state = state_text.get(o["status"], o["status"])
            lines.append(
                f"• {o['name'][:40]} — {state} {pct}%"
                f"　📦 {tasks_mod.fmt_size(o['done'])} / {tasks_mod.fmt_size(o['total'])}"
            )
            buttons.append([Button.inline(f"🗑 删除 {o['name'][:18]}", data=f"cleanup:{o['gid']}".encode())])
        lines.append("\n删掉就能重新发这个磁力。")
        if len(orphans) > 1:
            buttons.append([Button.inline("🗑 全部清理", data=b"cleanup:all")])
        return "\n".join(lines), buttons

    @client.on(events.NewMessage)
    async def handler(event):
        # 直接用 sender_id，不用 get_sender()：后者要查 session 数据库解析实体，
        # 数据库一旦被锁就会失败/返回空，导致把你自己的消息也当成陌生人拒绝。
        uid = event.sender_id or 0
        if not event.is_private:
            return  # 只在私聊里用
        if uid not in config.ALLOWED:
            # 记下来是谁被拒绝了，不然下次再刷屏又查不到原因。
            config.log(f"白名单拒绝：sender_id={uid}（0 表示拿不到发送者，常是频道转发的消息）")
            await event.reply("这个 bot 是私用的，只有白名单用户能用。")
            return
        if event.text and event.text.startswith("/"):
            if event.text.startswith("/start"):
                await event.reply(
                    "发链接给我，我用 aria2 下载，下完自动传到网盘，传完删本地。\n"
                    "/mirror <链接> 也可以；/storage 管网盘，/status 看任务。",
                    buttons=[
                        [Button.inline("📥 发个链接试试", data=b"help")],
                        [Button.inline("📦 存储管理", data=b"stor:menu")],
                        [Button.inline("❓ 使用帮助", data=b"help")],
                    ],
                )
            elif event.text.startswith("/help"):
                await event.reply(_HELP_TEXT, buttons=[
                    [Button.inline("📦 存储管理", data=b"stor:menu")],
                ])
            elif event.text.startswith("/mirror"):
                mgr = await _need_aria2(event)
                if mgr is None:
                    return
                text = event.text[len("/mirror"):].strip()
                urls = tasks_mod.extract_urls(text)
                if not urls:
                    await event.reply("用法：/mirror <链接>\n直链、magnet、.torrent 链接都行，也可以直接把链接发给我。")
                    return
                tz, tz_name = tzutil.get_user_tz(uid)
                for url in urls:
                    msg = await event.reply(f"📥 新建任务：{url[:100]}\n正在加进 aria2…")
                    await mgr.start_mirror(event, msg, url, uid, tz, tz_name)
                if len(urls) > 1:
                    await event.reply(f"📥 已建 {len(urls)} 个任务，用 /status 看进度。")
            elif event.text.startswith("/status"):
                mgr = await _need_aria2(event)
                if mgr is None:
                    return
                ids = mgr.active_job_ids()
                buttons = None
                if ids:
                    buttons = [[Button.inline(f"❌ 取消 #{i}", data=f"mirror:cancel:{i}".encode())] for i in ids]
                await event.reply(mgr.status_text(), buttons=buttons)
            elif event.text.startswith("/cancel"):
                mgr = await _need_aria2(event)
                if mgr is None:
                    return
                parts = event.text.split()
                if len(parts) == 2 and parts[1].lstrip("#").isdigit():
                    job_id = int(parts[1].lstrip("#"))
                    ok = await mgr.cancel(job_id, event)
                    if not ok:
                        await event.reply(f"没有 #{job_id} 这个任务，可能已经完了或者不在了。")
                    return
                ids = mgr.active_job_ids()
                if not ids:
                    await event.reply("现在没有进行中的任务。")
                    return
                buttons = [[Button.inline(f"❌ 取消 #{i}", data=f"mirror:cancel:{i}".encode())] for i in ids]
                await event.reply("取消哪个任务？", buttons=buttons)
            elif event.text.startswith("/cleanup"):
                mgr = await _need_aria2(event)
                if mgr is None:
                    return
                text, buttons = await _orphan_list_message(mgr)
                if buttons:
                    await event.reply(text, buttons=buttons)
                else:
                    await event.reply(text)
            elif event.text.startswith("/timezone"):
                # /timezone：提示输入 UTC 序号；/timezone -8：直接设置。
                parts = event.text.split()
                if len(parts) == 1:
                    _, cur = tzutil.get_user_tz(uid)
                    _tz_pending.add(uid)
                    await event.reply(
                        f"你当前的时区是：{cur}\n"
                        "发 UTC 序号改时区，比如 -8、+8（范围 -12 到 +12）；也可以发时区名，比如 Europe/London。"
                    )
                else:
                    parsed = tzutil.parse_tz_name(parts[1])
                    if not parsed:
                        await event.reply(
                            f"看不懂：{parts[1]}\n发一个 UTC 序号就行，比如 -8、+8，范围 -12 到 +12。"
                        )
                        return
                    _, disp = parsed
                    tzutil.set_user_tz(uid, disp)
                    _tz_pending.discard(uid)
                    await event.reply(f"时区已设为 {disp}，以后上传时间就按这个时区显示。")
            elif event.text.startswith("/storage"):
                storage_flow.cancel_flow(uid)
                await event.reply(storage_mod.menu_text(), buttons=storage_mod.menu_buttons())
            return
        # 刚点了 /timezone 的用户直接发了个数字：当成 UTC 序号设置时区。
        if event.text and uid in _tz_pending:
            text = event.text.strip()
            if re.fullmatch(r"[+-]?\d{1,2}", text):
                parsed = tzutil.parse_tz_name(text)
                if parsed:
                    _, disp = parsed
                    tzutil.set_user_tz(uid, disp)
                    _tz_pending.discard(uid)
                    await event.reply(f"时区已设为 {disp}，以后上传时间就按这个时区显示。")
                    return
                await event.reply("UTC 序号范围是 -12 到 +12，再发一次，比如 -8。")
                return
            _tz_pending.discard(uid)
        # 正在添加存储的流程中：用户发的文字就是要填的那一项。
        if storage_flow.has_flow(uid):
            st = storage_flow.get(uid)
            text = (event.text or "").strip()
            if st and st.get("step") == "oauth_wait" and text not in ("取消", "/cancel"):
                # 授权等着收 localhost 链接：交给后台的 authorize 任务。
                q = st.get("oauth_queue")
                if q is not None:
                    q.put_nowait(text)
                else:
                    await event.reply("授权还没准备好，稍等一下再发这个链接。")
                return
            await storage_flow.handle_text(event, uid)
            return
        # 发了一个 .torrent 文件：下这个种子。
        if _is_torrent_document(event):
            mgr = await _need_aria2(event)
            if mgr is None:
                return
            tz, tz_name = tzutil.get_user_tz(uid)
            name = event.file.name or "file.torrent"
            msg = await event.reply(f"收到种子文件 {name}，正在加进 aria2…")
            try:
                data = await event.download_media(file=bytes)
                torrent_b64 = base64.b64encode(data).decode("ascii")
            except Exception as e:
                await msg.edit(f"❌ 种子文件下载失败：{e}")
                return
            await mgr.start_torrent_file(event, msg, torrent_b64, name, uid, tz, tz_name)
            return
        # 直接发的链接 / magnet：当成下载任务。
        urls = tasks_mod.extract_urls(event.text or "")
        if urls:
            mgr = await _need_aria2(event)
            if mgr is None:
                return
            tz, tz_name = tzutil.get_user_tz(uid)
            for url in urls:
                msg = await event.reply(f"📥 新建任务：{url[:100]}\n正在加进 aria2…")
                await mgr.start_mirror(event, msg, url, uid, tz, tz_name)
            if len(urls) > 1:
                await event.reply(f"📥 已建 {len(urls)} 个任务，用 /status 看进度。")
            return
        await event.reply("发个下载链接给我（直链、magnet、.torrent 链接都行），或者发 .torrent 文件。\n不会用就发 /help 看看。")

    @client.on(events.CallbackQuery(pattern=rb"help"))
    async def help_button_handler(event):
        if await auth.cb_uid(event) is None:
            return
        await event.edit(_HELP_TEXT, buttons=[[Button.inline("📦 存储管理", data=b"stor:menu")]])

    @client.on(events.CallbackQuery(pattern=rb"retry:(\d+)"))
    async def retry_handler(event):
        uid = await auth.cb_uid(event, "重试按钮")
        if uid is None:
            return
        if manager is None:
            await event.answer("aria2 还没起来，重试不了。", alert=True)
            return
        job_id = int(event.pattern_match.group(1))
        if job_id not in manager.failed:
            await event.answer("这个任务已经不在了，请重新发一次链接。", alert=True)
            return
        await event.answer("正在重试…")
        msg = await event.get_message()
        await manager.retry(job_id, event, msg)

    @client.on(events.CallbackQuery(pattern=rb"mirror:cancel:(\d+)"))
    async def mirror_cancel_handler(event):
        uid = await auth.cb_uid(event, "取消任务按钮")
        if uid is None:
            return
        if manager is None:
            await event.answer("aria2 还没起来。", alert=True)
            return
        job_id = int(event.pattern_match.group(1))
        ok = await manager.cancel(job_id, event)
        if not ok:
            await event.answer("这个任务已经不在了。", alert=True)

    # ---------- 清理孤儿下载：cleanup: 回调 ----------
    # 注意：all 必须放前面再加 $ 锚定。原来写的是 ([0-9a-f]+|all)，没锚定，
    # "all" 里的 a 是十六进制字符，[0-9a-f]+ 会先匹配到 "a" 当成 gid，
    # 结果去删一个不存在的 gid，报“这个下载已经不在 aria2 里了”。
    @client.on(events.CallbackQuery(pattern=rb"cleanup:(all|[0-9a-f]+)$"))
    async def cleanup_handler(event):
        uid = await auth.cb_uid(event, "清理孤儿按钮")
        if uid is None:
            return
        if manager is None:
            await event.answer("aria2 还没起来。", alert=True)
            return
        target = event.pattern_match.group(1).decode()
        if target == "all":
            removed, failed = await manager.cleanup_all_orphans()
            text = f"🧹 已清理 {len(removed)} 个孤儿下载"
            if failed:
                text += f"，{len(failed)} 个失败"
            text += "，现在可以重新发磁力了。"
            await event.edit(text)
            await event.answer(f"已清理 {len(removed)} 个")
            return
        try:
            info = await manager.cleanup_orphan(target)
        except Exception as e:
            # 已经清掉的再点一次：不是失败，别吓用户。
            if "已经不在 aria2 里了" in str(e):
                await event.answer("这个已经清掉了。")
            else:
                await event.answer(f"清理失败：{e}", alert=True)
            return
        text, buttons = await _orphan_list_message(manager)
        msg = f"🧹 已清理：{info['name']}\n\n{text}"
        if buttons:
            await event.edit(msg, buttons=buttons)
        else:
            await event.edit(msg)
        await event.answer("已清理")

    # ---------- 下载完选存哪个网盘：dst: 回调 ----------
    @client.on(events.CallbackQuery(pattern=rb"dst:(cancel:)?(\d+)(?::(.+))?"))
    async def dst_handler(event):
        uid = await auth.cb_uid(event)
        if uid is None:
            return
        if manager is None:
            await event.answer("aria2 还没起来。", alert=True)
            return
        cancel = event.pattern_match.group(1)
        job_id = int(event.pattern_match.group(2))
        job = manager.pending_dst.get(job_id)
        if not job:
            await event.answer("这个任务已经不在了，请重新发一次链接。", alert=True)
            return
        if cancel:
            manager.pending_dst.pop(job_id, None)
            manager._cleanup_local(job)
            await event.edit("已取消，本地文件已删。")
            return
        remote = (event.pattern_match.group(3) or b"").decode()
        store = storage_mod.get_storage(remote)
        if not store:
            await event.answer("这个存储已经不在了，请重新发一次链接。", alert=True)
            manager.pending_dst.pop(job_id, None)
            manager._cleanup_local(job)
            return
        await event.answer("正在上传…")
        await manager.continue_with_storage(job_id, store, event)

    # 存储管理的全部回调（stor: 开头）由 storage 模块注册。
    storage_flow.register_callbacks(client)

    # tracker 定时刷新：aria2 只在启动时读一次 bt-tracker，几个月不重启列表就冻住了。
    # 每天拉一次最新的推给正在跑的 aria2（全局 + 进行中的下载），不用重启 aria2。
    if aria2_client:
        asyncio.create_task(_tracker_refresh_loop())

    await client.run_until_disconnected()
    _clear_pid_file()
    aria2rpc.stop_daemon()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
