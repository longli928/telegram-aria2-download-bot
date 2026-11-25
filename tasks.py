#!/usr/bin/env python3
"""下载任务（新写）：/mirror <链接> / 直接发链接 / 发 .torrent 文件。

流程：aria2 下 → 下完按默认存储 rclone move 传上去 → 传完删本地。
- 直链、magnet、.torrent 链接都走 aria2 addUri；.torrent 文件走 addTorrent。
- 下载进度每 2 秒改一次状态消息（只在百分比变了时改，免得刷屏被限流）。
- 传完问不到默认存储又多个网盘时，先不传，等用户点按钮选（本地文件留着）；
  选完再传；取消就删本地。
- 上传失败本地文件留着，消息带重试按钮；下载失败也能重试（重新加回 aria2）。
- 任务 id 全局自增，不拿消息 id，免得不同聊天的消息 id 撞号。
"""
import asyncio
import json
import os
import re
import shutil
import time

from telethon import Button

import config
import rclone as rclone_mod
import storage as storage_mod
import tzutil
from aria2rpc import Aria2Client, Aria2Error

# 任务 id 持久化：只放内存里的话 bot 一重启就从 1 开始，新的任务会复用
# 旧任务的下载目录（downloads/<id>/），失败清理时就把别人正在下的文件删了。
_SEQ_FILE = os.path.join(config.BASE_DIR, "job_seq.json")


def _load_seq():
    try:
        with open(_SEQ_FILE) as f:
            return max(int(json.load(f).get("next", 1)), 1)
    except Exception:
        return 1


def _save_seq(n):
    try:
        with open(_SEQ_FILE, "w") as f:
            json.dump({"next": n}, f)
    except Exception:
        pass

# 一条消息里能认出来的下载链接：http(s) 直链、magnet、.torrent 链接。
_URL_RE = re.compile(r"https?://[^\s<>\"]+")
_MAGNET_RE = re.compile(r"magnet:\?[^\s<>\"]+")


def extract_urls(text):
    """从一段文字里抠出所有下载链接（去重、保序）。返回 [url]。"""
    text = text or ""
    found = []
    for m in _MAGNET_RE.finditer(text):
        u = m.group(0).rstrip(").,")
        if u not in found:
            found.append(u)
    for m in _URL_RE.finditer(text):
        u = m.group(0).rstrip(").,")
        if u not in found:
            found.append(u)
    return found


def is_magnet(url):
    return url.startswith("magnet:?")


def _sanitize_name(name):
    name = (name or "").strip().replace("/", "_").replace("\x00", "")
    return name or "download"


def fmt_size(nbytes):
    """把字节数格式化成自适应单位：B / KB / MB / GB / TB。"""
    size = float(nbytes or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _fmt_eta(seconds):
    """剩余秒数 -> hh:mm:ss。"""
    seconds = max(int(seconds), 0)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _progress_text(label, done, total, t0, seeders=None, connections=None):
    pct = done * 100 // total if total else 0
    dt = max(time.time() - t0, 0.1)
    speed = done / dt  # 字节/秒
    eta = (total - done) / speed if speed > 0 else 0
    bar_len = 17
    filled = pct * bar_len // 100
    bar = "█" * filled + "░" * (bar_len - filled)
    text = (
        f"{label}\n{bar} {pct}%\n"
        f"📦 {fmt_size(done)} / {fmt_size(total)}      🚀 速度 {fmt_size(speed)}/s\n"
        f"⏱ 已耗时 {_fmt_eta(dt)}      ⏳ 预计还剩 {_fmt_eta(eta)}"
    )
    if seeders is not None:
        text += f"\n🌱 做种 {seeders}　🔗 连接 {connections or 0}"
    return text


class MirrorManager:
    def __init__(self, aria2_client):
        self.aria2 = aria2_client
        self.jobs = {}          # job_id -> job dict（进行中的）
        self.failed = {}        # job_id -> 重试用的信息
        self.pending_dst = {}   # job_id -> 等用户选存储的 job
        sem = config.MAX_CONCURRENT_TASKS
        self._sem = asyncio.Semaphore(sem) if sem and sem > 0 else None
        self._next_job_id = _load_seq()

    def next_job_id(self):
        job_id = self._next_job_id
        self._next_job_id += 1
        _save_seq(self._next_job_id)
        return job_id

    # ---------- 任务入口 ----------
    async def start_mirror(self, event, msg, url, uid, tz, tz_name):
        """新建一个下载任务（后台跑，不堵消息处理）。返回 job_id。"""
        job_id = self.next_job_id()
        job = {
            "job_id": job_id,
            "user_id": uid,
            "chat_id": event.chat_id,
            "msg": msg,
            "url": url,
            "torrent_b64": None,
            "gid": None,
            "state": "queued",
            "name": url[:60],
            "done": 0,
            "total": 0,
            "speed": 0,
            "local_dir": os.path.join(config.DOWNLOAD_DIR, str(job_id)),
            "local": None,
            "storage": None,
            "tz": tz,
            "tz_name": tz_name,
            "cancelled": False,
            "task": None,
            "started": time.time(),
        }
        self.jobs[job_id] = job
        config.log(f"新建下载任务 {job_id}：{url[:120]}（用户 {uid}）")
        job["task"] = asyncio.create_task(self._run_job(job))
        return job_id

    async def start_torrent_file(self, event, msg, torrent_b64, filename, uid, tz, tz_name):
        """用户发了一个 .torrent 文件：先走 addTorrent，剩下流程跟链接一样。"""
        job_id = self.next_job_id()
        job = {
            "job_id": job_id,
            "user_id": uid,
            "chat_id": event.chat_id,
            "msg": msg,
            "url": filename,
            "torrent_b64": torrent_b64,
            "gid": None,
            "state": "queued",
            "name": filename,
            "done": 0,
            "total": 0,
            "speed": 0,
            "local_dir": os.path.join(config.DOWNLOAD_DIR, str(job_id)),
            "local": None,
            "storage": None,
            "tz": tz,
            "tz_name": tz_name,
            "cancelled": False,
            "task": None,
            "started": time.time(),
        }
        self.jobs[job_id] = job
        config.log(f"新建种子任务 {job_id}：{filename}（用户 {uid}）")
        job["task"] = asyncio.create_task(self._run_job(job))
        return job_id

    # ---------- 任务主流程 ----------
    async def _run_job(self, job):
        if self._sem is None:
            return await self._run_job_impl(job)
        async with self._sem:
            return await self._run_job_impl(job)

    async def _run_job_impl(self, job):
        msg = job["msg"]
        job_id = job["job_id"]
        loop = asyncio.get_running_loop()
        try:
            os.makedirs(job["local_dir"], exist_ok=True)
            # 1. 加进 aria2
            job["state"] = "downloading"
            if job["torrent_b64"]:
                gid = await loop.run_in_executor(
                    None, lambda: self.aria2.add_torrent(job["torrent_b64"], [], {"dir": job["local_dir"]}))
            else:
                gid = await loop.run_in_executor(
                    None, lambda: self.aria2.add_uri([job["url"]], {"dir": job["local_dir"]}))
            job["gid"] = gid
            config.log(f"任务 {job_id} 已加进 aria2：gid={gid}")
            # 2. 轮询进度，直到下完/出错
            await self._poll_download(job)
            if job["cancelled"]:
                return False
            # 下完了就让 aria2 停掉它（可能还在做种），免得一直占着文件和上传带宽。
            try:
                await loop.run_in_executor(None, lambda: self.aria2.remove(job["gid"]))
            except Exception:
                pass
            # 3. 下完了：定本地路径和名字
            local, name, size = self._local_result(job)
            job["local"] = local
            job["name"] = name
            job["size"] = size
            config.log(f"任务 {job_id} 下载完成：{name}（{fmt_size(size)}）")
            # 4. 决定存到哪个网盘
            store, need_choice = storage_mod.resolve_storage()
            if need_choice:
                self.pending_dst[job_id] = job
                job["state"] = "waiting_storage"
                buttons = [[Button.inline(f"📦 {storage_mod.storage_display(s)}", data=f"dst:{job_id}:{s['remote']}".encode())] for s in storage_mod.storage_list()]
                buttons.append([Button.inline("❌ 取消", data=f"dst:cancel:{job_id}".encode())])
                try:
                    await msg.edit(f"✅ 下载完了：{name}\n📦 大小：{fmt_size(size)}\n传到哪个网盘？", buttons=buttons)
                except Exception:
                    pass
                config.log(f"任务 {job_id} 等用户选存储")
                return "waiting"
            if store is None:
                try:
                    await msg.edit("❌ 还没配好存储，先用 /storage 添加一个网盘。")
                except Exception:
                    pass
                self._cleanup_local(job)
                return False
            job["storage"] = store
            return await self._upload(job, store)
        except asyncio.CancelledError:
            self._cleanup_local(job)
            raise
        except Exception as e:
            config.log(f"任务 {job_id} 下载失败：{e}")
            self.failed[job_id] = {
                "user_id": job["user_id"],
                "chat_id": job["chat_id"],
                "url": job["url"],
                "torrent_b64": job["torrent_b64"],
                "name": job["name"],
                "kind": "download",
            }
            try:
                if "already registered" in str(e):
                    await msg.edit(
                        f"❌ 下载失败：{job['name']}\n{str(e)[:300]}\n\n这个种子已经在 aria2 里了（可能是重启后丢了的孤儿任务），先用 /cleanup 清掉它，再重新发一次磁力。"
                    )
                else:
                    await msg.edit(
                        f"❌ 下载失败：{job['name']}\n{str(e)[:300]}\n\n点下面按钮可以重试。",
                        buttons=[[Button.inline("🔁 重试", data=f"retry:{job_id}".encode())]],
                    )
            except Exception:
                pass
            # 只有这个任务真的加进了 aria2 才清本地目录；加都加不进去（比如
            # InfoHash 已注册）时，目录里可能是另一个任务正在下的文件，不能删。
            if job.get("gid"):
                self._cleanup_local(job)
            return False
        finally:
            self.jobs.pop(job_id, None)

    async def _poll_download(self, job):
        """每 2 秒问一次 aria2 进度，百分比变了就改消息，百分比不动时也每 5 秒刷一次；
        下完/出错/取消就返回。"""
        msg = job["msg"]
        loop = asyncio.get_running_loop()
        t0 = time.time()
        last_pct = -1
        last_edit = 0
        _meta_waits = 0
        while True:
            if job["cancelled"]:
                raise asyncio.CancelledError()
            try:
                st = await loop.run_in_executor(None, lambda: self.aria2.tell_status(job["gid"]))
            except Exception as e:
                raise Aria2Error(f"问 aria2 进度失败：{e}")
            status = st.get("status", "")
            total = int(st.get("totalLength") or 0)
            done = int(st.get("completedLength") or 0)
            speed = int(st.get("downloadSpeed") or 0)
            job["done"], job["total"], job["speed"] = done, total, speed
            name = self._status_name(st) or job["name"]
            job["name"] = name
            # 种子才有做种数/连接数，直链没有——只给种子进度条加这一行。
            is_torrent = is_magnet(job.get("url") or "") or bool(job.get("torrent_b64"))
            seeders = int(st.get("numSeeders") or 0) if is_torrent else None
            conns = int(st.get("connections") or 0) if is_torrent else None
            if status == "complete":
                # 磁力分两段：aria2 先下一个元数据任务（名字是 [METADATA]<hash>），
                # 下完后另起一个真正的下载任务，原任务的 followedBy 指过去。
                # 只看原任务会把“元数据下完了”当成“片子下完了”，传走一堆刚建出来的
                # 0 字节空文件，还把正在下载的目录删掉。
                followed = st.get("followedBy") or []
                if followed:
                    job["gid"] = followed[0]
                    _meta_waits = 0
                    continue
                # 元数据已完成但还没跟出正片任务：等几轮再确认，别急着认完成。
                if name.startswith("[METADATA]") or name.startswith("[MEMORY][METADATA]"):
                    _meta_waits += 1
                    if _meta_waits < 3:
                        await asyncio.sleep(2)
                        continue
                return
            if status == "error":
                raise Aria2Error(f"aria2 下载出错：{st.get('errorMessage') or st.get('errorCode') or '未知错误'}")
            if status == "removed":
                raise asyncio.CancelledError()
            # 下完了但 aria2 还在做种（状态还是 active）：下载已经完成，做种是另一回事——
            # 直接取消做种，再报下载完成，不等它做完（做种多久没个准，之前卡过很久）。
            # 元数据任务除外——它下完也要跟 followedBy 走，不能直接取消。
            if (status == "active" and total > 0 and done >= total
                    and not name.startswith("[METADATA]")
                    and not name.startswith("[MEMORY][METADATA]")):
                try:
                    await loop.run_in_executor(None, lambda: self.aria2.remove(job["gid"]))
                except Exception:
                    pass
                return
            pct = done * 100 // total if total else 0
            # 百分比变了就改；百分比不动时也每 5 秒刷一次，不然大文件慢速下 1% 要等
            # 很久（31.9 GB 的 1% 是 319 MB），界面一直 0% 不动，看着像卡死了。
            if pct != last_pct or time.time() - last_edit >= 5:
                last_pct = pct
                try:
                    await msg.edit(
                        _progress_text(f"⬇️ 下载中：{name}", done, total, t0, seeders, conns),
                        buttons=[[Button.inline("❌ 取消", data=f"mirror:cancel:{job['job_id']}".encode())]],
                    )
                    last_edit = time.time()
                except Exception:
                    pass
            await asyncio.sleep(2)

    @staticmethod
    def _status_name(st):
        """从 aria2 状态里拿显示名：种子用种子名，直链用第一个文件的名字。"""
        try:
            bt = st.get("bittorrent") or {}
            info = bt.get("info") or {}
            if info.get("name"):
                return info["name"]
            files = st.get("files") or []
            if files:
                path = files[0].get("path") or ""
                if path:
                    return os.path.basename(path)
        except Exception:
            pass
        return ""

    def _local_result(self, job):
        """下完后定本地路径：任务目录里只有一个东西就传它；多个就整个目录一起传。
        .aria2 控制文件是 aria2 的记账文件，不是下载内容，不算在内。"""
        d = job["local_dir"]
        entries = []
        try:
            entries = [e for e in os.listdir(d) if not e.startswith(".") and not e.endswith(".aria2")]
        except Exception:
            pass
        if not entries:
            raise RuntimeError("aria2 说下完了，但本地目录里没东西")
        if len(entries) == 1:
            local = os.path.join(d, entries[0])
            name = entries[0]
        else:
            # 多文件（种子）：整个目录一起传，远端目录名用任务名。
            local = d
            name = _sanitize_name(job["name"])
        size = 0
        if os.path.isfile(local):
            size = os.path.getsize(local)
        else:
            for root, _dirs, files in os.walk(local):
                for f in files:
                    try:
                        size += os.path.getsize(os.path.join(root, f))
                    except OSError:
                        pass
        if size == 0 and (is_magnet(job.get("url") or "") or job.get("torrent_b64")):
            raise RuntimeError("下载完成但文件大小为 0，没有下到数据")
        return local, name, size

    # ---------- 上传 ----------
    async def _upload(self, job, store):
        """rclone move 传上去，全程用消息实时更新进度。成功删本地、报落盘信息；
        失败本地留着，消息带重试按钮。"""
        msg = job["msg"]
        job_id = job["job_id"]
        name = job["name"]
        size = job.get("size") or 0
        local = job["local"]
        job["state"] = "uploading"
        uploaded_ok = False
        try:
            t0 = time.time()
            up = {"done": 0}
            stop = {"done": False}

            async def _watch():
                last_pct = -1
                last_edit = 0
                while not stop["done"]:
                    await asyncio.sleep(2)
                    done = up["done"]
                    pct = done * 100 // size if size else 0
                    # 百分比变了就改；不动时也每 5 秒刷一次，不然大文件上传 1% 要等很久。
                    if pct == last_pct and done < size and time.time() - last_edit < 5:
                        continue
                    last_pct = pct
                    try:
                        await msg.edit(_progress_text(f"⬆️ 上传到 {storage_mod.storage_display(store)}：{name}", done, size, t0))
                        last_edit = time.time()
                    except Exception:
                        pass
                    if done >= size:
                        break

            watcher = asyncio.create_task(_watch())
            try:
                loop = asyncio.get_running_loop()
                remote = await loop.run_in_executor(
                    None,
                    lambda: rclone_mod.move_to_storage(
                        local, name, store,
                        on_progress=lambda d, t: up.update({"done": d}),
                    ),
                )
            finally:
                stop["done"] = True
                await watcher
            dt = time.time() - t0
            config.log(f"任务 {job_id} 上传完成：{name}，用时 {dt:.0f}s")
            # 成功：尽量返回落盘文件信息
            try:
                info = await loop.run_in_executor(None, lambda: rclone_mod.get_remote_file_info(store["remote"], remote))
                text = self._format_file_info(info, job, store)
            except Exception as e:
                config.log(f"查落盘文件信息失败：{e}")
                text = f"✅ 已上传到 {storage_mod.storage_display(store)}\n📂 位置：/{remote}"
            await msg.edit(text)
            config.log(f"任务 {job_id} 完成：{name} -> {store['remote']}:{remote}")
            self.failed.pop(job_id, None)
            uploaded_ok = True
            return True
        except Exception as e:
            config.log(f"任务 {job_id} 上传失败：{e}")
            self.failed[job_id] = {
                "user_id": job["user_id"],
                "chat_id": job["chat_id"],
                "url": job["url"],
                "torrent_b64": job["torrent_b64"],
                "name": name,
                "size": size,
                "local": local,
                "local_dir": job["local_dir"],
                "storage": store,
                "kind": "upload",
            }
            try:
                await msg.edit(
                    f"❌ 上传失败：{name}\n{str(e)[:300]}\n\n本地文件还留着，点下面按钮可以重试。",
                    buttons=[[Button.inline("🔁 重试上传", data=f"retry:{job_id}".encode())]],
                )
            except Exception:
                pass
            return False
        finally:
            # 传成功：move 已经把文件搬走了，任务目录只剩空目录/空子目录，全清掉；
            # 传失败：local 还在，不能删（等重试），只清已经空了的目录。
            if uploaded_ok:
                self._cleanup_local(job)
            else:
                self._cleanup_local(job, only_if_empty=True)

    def _format_file_info(self, info, job, store):
        lines = [
            f"✅ 已上传到 {storage_mod.storage_display(store)}",
            f"📄 文件名：{info['name']}",
            f"📦 大小：{fmt_size(info['size'])}",
            f"📂 位置：{info['path']}",
        ]
        if info.get("modified"):
            text = f"🕐 上传时间：{tzutil.fmt_time(info['modified'], job['tz'])}"
            if job.get("tz_name"):
                text += f"（{job['tz_name']}）"
            lines.append(text)
        if info.get("webUrl"):
            lines.append(f"🔗 打开：{info['webUrl']}")
        return "\n".join(lines)

    # ---------- 选存储后继续上传 ----------
    async def continue_with_storage(self, job_id, store, event):
        """用户在“传到哪个网盘”里选了一个：继续传（重新拿并发名额）。"""
        job = self.pending_dst.pop(job_id, None)
        if not job:
            return None
        job["storage"] = store
        msg = await event.get_message()

        async def _do():
            return await self._upload(job, store)

        job["msg"] = msg
        if self._sem is None:
            return await _do()
        async with self._sem:
            return await _do()

    # ---------- 重试 ----------
    async def retry(self, job_id, event, msg):
        """重试按钮：上传失败且本地还在就只重传；下载失败或本地没了就从头下。"""
        info = self.failed.get(job_id)
        if not info:
            return None
        uid = info["user_id"]
        tz, tz_name = tzutil.get_user_tz(uid)
        if info.get("kind") == "upload" and info.get("local") and os.path.exists(info["local"]):
            job = {
                "job_id": job_id,
                "user_id": uid,
                "chat_id": info["chat_id"],
                "msg": msg,
                "url": info["url"],
                "torrent_b64": info.get("torrent_b64"),
                "gid": None,
                "state": "uploading",
                "name": info["name"],
                "done": 0,
                "total": info.get("size") or 0,
                "speed": 0,
                "local_dir": info.get("local_dir") or "",
                "local": info["local"],
                "size": info.get("size") or 0,
                "storage": info["storage"],
                "tz": tz,
                "tz_name": tz_name,
                "cancelled": False,
                "task": None,
            }
            self.failed.pop(job_id, None)
            config.log(f"重试上传任务 {job_id}：{info['name']}")
            return await self._upload(job, info["storage"])
        # 从头下：重新建任务
        self.failed.pop(job_id, None)
        if info.get("torrent_b64"):
            new_id = await self.start_torrent_file(event, msg, info["torrent_b64"], info["name"], uid, tz, tz_name)
        else:
            new_id = await self.start_mirror(event, msg, info["url"], uid, tz, tz_name)
        config.log(f"重试下载任务 {job_id}，新任务 {new_id}")
        return new_id

    # ---------- 取消 / 状态 ----------
    async def cancel(self, job_id, event):
        """取消一个任务：aria2 里删掉，后台协程停掉，本地文件删掉。"""
        job = self.jobs.get(job_id) or self.pending_dst.get(job_id)
        if not job:
            return False
        job["cancelled"] = True
        loop = asyncio.get_running_loop()
        if job.get("gid"):
            try:
                await loop.run_in_executor(None, lambda: self.aria2.remove(job["gid"], force=True))
            except Exception:
                pass
        task = job.get("task")
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        self.pending_dst.pop(job_id, None)
        self.jobs.pop(job_id, None)
        self._cleanup_local(job)
        try:
            await event.edit("已取消，本地文件已删。")
        except Exception:
            pass
        config.log(f"任务 {job_id} 已取消")
        return True

    def status_text(self):
        """进行中的任务列表（/status 用）。"""
        items = list(self.jobs.values()) + list(self.pending_dst.values())
        if not items:
            return "现在没有进行中的下载任务。"
        lines = ["📥 进行中的任务："]
        for job in items:
            state = {
                "queued": "⏳ 排队",
                "downloading": "⬇️ 下载中",
                "uploading": "⬆️ 上传中",
                "waiting_storage": "📦 等选网盘",
            }.get(job["state"], job["state"])
            total = job.get("total") or 0
            done = job.get("done") or 0
            pct = done * 100 // total if total else 0
            speed = job.get("speed") or 0  # 字节/秒
            size_part = f"　📦 {fmt_size(done)} / {fmt_size(total)}" if total else ""
            elapsed = f"　⏱ 已耗时 {_fmt_eta(time.time() - job['started'])}" if job.get("started") else ""
            lines.append(
                f"• #{job['job_id']} {job['name'][:40]} — {state} {pct}%{size_part}"
                + (f"　🚀 {fmt_size(speed)}/s" if job["state"] == "downloading" and speed else "")
                + elapsed
            )
        return "\n".join(lines)

    def active_job_ids(self):
        return [j["job_id"] for j in list(self.jobs.values()) + list(self.pending_dst.values())]

    # ---------- 孤儿清理（/cleanup 用） ----------
    _ORPHAN_KEYS = ["gid", "status", "totalLength", "completedLength",
                    "downloadSpeed", "uploadSpeed", "bittorrent", "files"]

    async def list_orphans(self):
        """aria2 里 bot 不管的下载：重启丢了的任务、加不进去的重复种子。"""
        loop = asyncio.get_running_loop()
        known = {j.get("gid") for j in list(self.jobs.values()) + list(self.pending_dst.values()) if j.get("gid")}
        orphans = []
        for fn in (self.aria2.tell_active, self.aria2.tell_waiting, self.aria2.tell_stopped):
            try:
                entries = await loop.run_in_executor(None, lambda f=fn: f(self._ORPHAN_KEYS))
            except Exception:
                continue
            for st in entries or []:
                gid = st.get("gid")
                if not gid or gid in known or st.get("status") == "removed":
                    continue
                known.add(gid)
                orphans.append({
                    "gid": gid,
                    "name": self._status_name(st) or gid,
                    "status": st.get("status", ""),
                    "total": int(st.get("totalLength") or 0),
                    "done": int(st.get("completedLength") or 0),
                    "speed": int(st.get("downloadSpeed") or 0),
                })
        return orphans

    async def cleanup_orphan(self, gid):
        """从 aria2 里删掉一个孤儿下载，连本地目录一起删。返回删掉的信息。"""
        loop = asyncio.get_running_loop()
        try:
            st = await loop.run_in_executor(None, lambda: self.aria2.tell_status(gid))
        except Exception as e:
            raise RuntimeError(f"这个下载已经不在 aria2 里了（{e}）")
        name = self._status_name(st) or gid
        total = int(st.get("totalLength") or 0)
        # 先拿下载目录（删掉之后就问不到了），只删下载目录里的东西。
        local_dir = ""
        try:
            opts = await loop.run_in_executor(None, lambda: self.aria2.get_option(gid))
            local_dir = (opts or {}).get("dir", "")
        except Exception:
            pass
        if not local_dir:
            files = st.get("files") or []
            if files and files[0].get("path"):
                local_dir = os.path.dirname(files[0]["path"])
        # 正在跑的用 forceRemove；已经停了的（complete/error/removed）只能用
        # removeDownloadResult，forceRemove 会报 aria2 RPC HTTP 400。
        status = st.get("status", "")
        try:
            if status in ("active", "waiting", "paused"):
                await loop.run_in_executor(None, lambda: self.aria2.remove(gid, force=True))
            else:
                await loop.run_in_executor(None, lambda: self.aria2.remove_download_result(gid))
        except Exception as e:
            raise RuntimeError(f"从 aria2 里删不掉：{e}")
        if local_dir:
            base = os.path.abspath(config.DOWNLOAD_DIR)
            target = os.path.abspath(local_dir)
            if target.startswith(base + os.sep):
                shutil.rmtree(target, ignore_errors=True)
        config.log(f"清理孤儿下载：{name}（gid={gid}）")
        return {"gid": gid, "name": name, "size": total}

    async def cleanup_all_orphans(self):
        """清掉全部孤儿下载，返回（已清掉的，失败的）。"""
        removed, failed = [], []
        for o in await self.list_orphans():
            try:
                removed.append(await self.cleanup_orphan(o["gid"]))
            except Exception as e:
                failed.append({"gid": o["gid"], "name": o["name"], "error": str(e)})
        return removed, failed

    # ---------- 清理 ----------
    @staticmethod
    def _cleanup_local(job, only_if_empty=False):
        """删本地：传完/取消/下载失败都清；上传失败不删（等重试）。
        only_if_empty=True 时只在目录空了才删目录本身。"""
        local = job.get("local")
        d = job.get("local_dir")
        try:
            if local and os.path.exists(local):
                if only_if_empty:
                    pass  # move 成功本地已经没了；失败的 local 不能删
                else:
                    if os.path.isdir(local):
                        shutil.rmtree(local, ignore_errors=True)
                    else:
                        os.remove(local)
            if d and os.path.isdir(d):
                if only_if_empty:
                    if not os.listdir(d):
                        os.rmdir(d)
                else:
                    shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass
