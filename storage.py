#!/usr/bin/env python3
"""存储管理（/storage）：添加网盘、选默认存储、改保存位置、删网盘。

- 凭据存在 rclone 自己的配置文件里（RCLONE_CONFIG，默认 ~/.config/rclone/rclone.conf）；
  注册表和默认存储存在 storages.json 里（chmod 600）。
- 设了默认存储，下载任务完成就直接往它传；没设默认又只有一个就用那个；
  没设默认且有多个就要问；一个都没有先让用户加。
- OAuth 的网盘（OneDrive / Google Drive / Dropbox 等 20 种）不用填 client_id/secret：
  bot 跑 `rclone authorize`（用 rclone 自带的应用），把登录链接发给用户，用户登录后把
  跳回来的 localhost 链接发回 bot，bot 回放给本机上的 authorize 服务换 token。
  贴回来的链接先对 state：对不上就是贴了上一次的旧链接；链接有问题就直接提示哪不对，
  然后结束这次添加、同时杀掉 authorize 进程，不一直等着。重新开始一个添加流程会先把
  上一次没走完的 authorize 进程杀掉，免得旧进程占着端口、新链接对不上。
  注意：OAuth 后端的 remote 不能走 `rclone config create`（它会重新跑一遍 OAuth、在那
  等 code 卡住），所以直接写 rclone.conf；OneDrive 还要先拿 drive_id/drive_type
  （问 Graph 的 /me/drive），不然 lsd 会报缺这两项。
  非 OAuth 的按提示填配置，还能再补 key=value 参数。
  所有 rclone 子进程都用 stdin=DEVNULL；报错记日志前先抹掉 token。
  选文件夹时直接调 rclone 列目录（lsjson），按钮进目录/选定/新建文件夹，也能手动输入；
  OAuth/手动的在选文件夹期间用临时配置（/tmp/rclone-picker-<uid>.conf，流程结束就删），
  不污染真实配置。存储列表优先显示用户起的名字，没有再显示网盘名；添加界面的后端按字母 A-Z 排。
  存储管理里还有“修改保存位置”：选一个存储，同样用文件夹选择器改它在 storages.json 里的
  folder（从现在的文件夹开始列），改完直接保存，不用重新添加。
"""
import asyncio
import glob
import json
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from telethon import Button, events

import auth
import config
import rclone as rclone_mod

_STORAGES_FILE = os.path.join(config.BASE_DIR, "storages.json")
_storages = {"default": None, "storages": {}}


def load_storages():
    """读 storages.json；第一次运行（还没有这个文件）就建个空的——
    全新部署是个干干净净的 bot，没有预置的存储，文件夹在 /storage 添加存储时用选文件夹器定。"""
    global _storages
    try:
        with open(_STORAGES_FILE, encoding="utf-8") as f:
            data = json.load(f) or {}
        if isinstance(data.get("storages"), dict):
            _storages = {"default": data.get("default"), "storages": data["storages"]}
            return
    except Exception:
        pass
    _storages = {"default": None, "storages": {}}
    save_storages()


def save_storages():
    try:
        with open(_STORAGES_FILE, "w", encoding="utf-8") as f:
            json.dump(_storages, f, ensure_ascii=False, indent=2)
        os.chmod(_STORAGES_FILE, 0o600)
    except Exception as e:
        config.log(f"保存存储设置失败：{e}")


def storage_list():
    return [_storages["storages"][k] for k in sorted(_storages["storages"])]


def get_storage(remote):
    return _storages["storages"].get(remote)


def default_storage_name():
    return _storages.get("default")


def storage_display(s):
    """菜单里显示的名字：优先显示用户起的名字（remote 名），没有再显示网盘名；
    名字后面括号里跟网盘名，方便区分。"""
    remote = (s.get("remote") or "").strip()
    label = (s.get("label") or "").strip()
    if remote:
        if label and label != remote:
            return f"{remote}（{label}）"
        return remote
    return label


def resolve_storage():
    """下载任务完成该往哪存：返回 (storage, 需要询问)。
    有默认就用默认；没默认但只有一个就用那个；没默认且有多个就要问；一个都没有返回 (None, False)。"""
    items = storage_list()
    default = _storages.get("default")
    if default and default in _storages["storages"]:
        return _storages["storages"][default], False
    if len(items) == 1:
        return items[0], False
    if len(items) > 1:
        return None, True
    return None, False


def menu_text():
    lines = ["📦 存储管理", ""]
    items = storage_list()
    if not items:
        lines.append("还没添加网盘，点下面按钮添加一个。")
    else:
        default = _storages.get("default")
        for s in items:
            mark = " ⭐ 默认" if s["remote"] == default else ""
            folder = f"/{s['folder']}" if s.get("folder") else "/（根目录）"
            lines.append(f"• {storage_display(s)} → {folder}{mark}")
        lines.append("")
        if not default and len(items) > 1:
            lines.append("没选默认存储，下载任务完成时会问你存到哪个。")
        elif not default:
            lines.append("没选默认存储，只有一个网盘，直接存它。")
    return "\n".join(lines)


def menu_buttons():
    return [
        [Button.inline("➕ 添加存储", data=b"stor:add")],
        [Button.inline("⭐ 选择默认存储", data=b"stor:default")],
        [Button.inline("📁 修改保存位置", data=b"stor:editfolder")],
        [Button.inline("🗑 删除存储", data=b"stor:dellist")],
    ]


# ---------- rclone 后端注册表 ----------
# 从 providers.json 加载（由 `rclone config providers` 生成，跟着装的 rclone 版本走；
# 升级 rclone 后重新跑一遍生成）。没有 providers.json 就只用内置的几种常见网盘兜底。
_PROVIDERS_FILE = os.path.join(config.BASE_DIR, "providers.json")
_PROVIDERS = []          # [{"name", "prefix", "desc", "options"}]
_PROVIDER_MAP = {}       # prefix -> provider
_PROVIDER_PAGES = []     # 分页后的 provider 列表，每页 10 个

_DISPLAY_OVERRIDES = {
    "onedrive": "Microsoft OneDrive",
    "drive": "Google Drive",
    "dropbox": "Dropbox",
    "s3": "Amazon S3",
    "gphotos": "Google Photos",
    "gcs": "Google Cloud Storage",
    "box": "Box",
    "pcloud": "pCloud",
    "webdav": "WebDAV",
    "ftp": "FTP",
    "sftp": "SFTP",
    "smb": "SMB",
    "mega": "MEGA",
    "b2": "Backblaze B2",
    "yandex": "Yandex Disk",
    "mailru": "Mail.ru Cloud",
    "jottacloud": "Jottacloud",
    "hidrive": "HiDrive",
    "koofr": "Koofr",
    "pikpak": "PikPak",
    "protondrive": "Proton Drive",
    "oos": "Oracle Object Storage",
    "internetarchive": "Internet Archive",
    "imagekit": "ImageKit",
    "filefabric": "File Fabric",
    "opendrive": "OpenDrive",
    "huaweidrive": "Huawei Drive",
    "kdrive": "kDrive",
    "sugarsync": "SugarSync",
    "seafile": "Seafile",
}


def load_providers():
    global _PROVIDERS, _PROVIDER_MAP, _PROVIDER_PAGES
    try:
        with open(_PROVIDERS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        _PROVIDERS = []
        for p in raw:
            _PROVIDERS.append({
                "name": p.get("Name", ""),
                "prefix": p.get("Prefix", "") or p.get("Name", ""),
                "desc": p.get("Description", ""),
                "options": p.get("Options") or [],
            })
        _PROVIDER_MAP = {x["prefix"]: x for x in _PROVIDERS}
        config.log(f"已加载 {len(_PROVIDERS)} 个 rclone 后端")
    except Exception as e:
        config.log(f"加载 providers.json 失败（{e}），只用内置的几种常见网盘")
        _PROVIDERS = []
        _PROVIDER_MAP = {}
    if not _PROVIDERS:
        for prefix, label in (("onedrive", "OneDrive"), ("drive", "Google Drive"), ("dropbox", "Dropbox"), ("s3", "S3 兼容"), ("webdav", "WebDAV")):
            _PROVIDERS.append({"name": prefix, "prefix": prefix, "desc": label, "options": []})
            _PROVIDER_MAP[prefix] = _PROVIDERS[-1]
    _PROVIDERS.sort(key=lambda x: x["prefix"].lower())
    _PROVIDER_PAGES = [_PROVIDERS[i:i + 10] for i in range(0, len(_PROVIDERS), 10)]


def provider_label(prefix):
    if prefix in _DISPLAY_OVERRIDES:
        return _DISPLAY_OVERRIDES[prefix]
    p = _PROVIDER_MAP.get(prefix)
    if p and p.get("name"):
        return p["name"].replace("_", " ").title()
    return prefix


def is_oauth(prefix):
    """这个后端走 OAuth 吗？——看它有没有 token 这一项（有就是 OAuth 的）。"""
    p = _PROVIDER_MAP.get(prefix)
    if not p:
        return False
    return any((o.get("Name") == "token") for o in p.get("options", []))


# 手写引导字段（常见非 OAuth 后端）：(rclone 参数名, 提示语, 是不是密钥, 是不是可跳过)
_MANUAL_FIELDS = {
    "s3": [
        ("provider", "服务商是哪个？比如 AWS、Minio、Cloudflare R2；不知道就发“跳过”", False, True),
        ("endpoint", "S3 的 endpoint 地址，比如 https://xxx.r2.cloudflarestorage.com；用 AWS 就发“跳过”", False, True),
        ("region", "区域，比如 us-east-1；不知道就发“跳过”", False, True),
        ("access_key_id", "发一下 Access Key ID", True, False),
        ("secret_access_key", "发一下 Secret Access Key", True, False),
    ],
    "webdav": [
        ("url", "发一下 WebDAV 地址，比如 https://example.com/dav", False, False),
        ("user", "发一下用户名", False, False),
        ("pass", "发一下密码", True, False),
    ],
    "ftp": [
        ("host", "发一下 FTP 服务器地址，比如 ftp.example.com", False, False),
        ("user", "发一下用户名（匿名就发“跳过”）", False, True),
        ("pass", "发一下密码（匿名就发“跳过”）", True, True),
    ],
    "sftp": [
        ("host", "发一下 SFTP 服务器地址，比如 sftp.example.com", False, False),
        ("user", "发一下用户名", False, False),
        ("pass", "发一下密码（用密钥文件登录就发“跳过”）", True, True),
    ],
    "smb": [
        ("host", "发一下 SMB 服务器地址，比如 192.168.1.10", False, False),
        ("user", "发一下用户名", False, False),
        ("pass", "发一下密码", True, False),
    ],
    "mega": [
        ("user", "发一下 MEGA 账号（邮箱）", False, False),
        ("pass", "发一下 MEGA 密码", True, False),
    ],
    "b2": [
        ("account", "发一下 B2 的 Key ID", True, False),
        ("key", "发一下 B2 的 Application Key", True, False),
    ],
    "seafile": [
        ("url", "发一下 Seafile 服务器地址，比如 https://seafile.example.com", False, False),
        ("user", "发一下账号（邮箱）", False, False),
        ("pass", "发一下密码", True, False),
    ],
    "protondrive": [
        ("username", "发一下 Proton 账号（邮箱）", False, False),
        ("password", "发一下 Proton 密码", True, False),
    ],
    "alias": [
        ("remote", "指向哪个已有的 remote？比如 mydrive:folder", False, False),
    ],
    "crypt": [
        ("remote", "加密后指向哪个 remote？比如 mydrive:crypt", False, False),
        ("password", "发一下加密密码（丢了就解不开，自己记好）", True, False),
    ],
}


def manual_fields(prefix):
    """这个后端要填的字段：先用手写的，没有就按 providers.json 里 Required 的来。"""
    if prefix in _MANUAL_FIELDS:
        return _MANUAL_FIELDS[prefix]
    p = _PROVIDER_MAP.get(prefix)
    if not p:
        return []
    fields = []
    seen = set()
    for o in p.get("options", []):
        name = o.get("Name", "")
        if not name or name in seen:
            continue
        if name in ("token", "client_id", "client_secret"):
            continue
        if not o.get("Required") or o.get("Advanced"):
            continue
        seen.add(name)
        help_text = (o.get("Help") or "").split("\n")[0].strip()
        if len(help_text) > 80:
            help_text = help_text[:80] + "…"
        prompt = f"发一下 {name}" + (f"（{help_text}）" if help_text else "")
        secret = bool(o.get("IsPassword") or o.get("Sensitive"))
        fields.append((name, prompt, secret, False))
    return fields


def extra_hint(prefix):
    """补充参数步骤里给的提示：这个后端还能填哪些参数。"""
    p = _PROVIDER_MAP.get(prefix)
    if not p:
        return ""
    names = []
    for o in p.get("options", []):
        name = o.get("Name", "")
        if not name or name in ("token", "client_id", "client_secret"):
            continue
        if o.get("Advanced"):
            continue
        if name not in names:
            names.append(name)
    return "、".join(names[:12])


def build_create_params(type_, data, token=None):
    """rclone config create 用的参数。OAuth 的只传 authorize 拿到的 token；
    手动的传填好的键值对，空的去掉。"""
    if token:
        return {"token": token}
    return {k: v for k, v in (data or {}).items() if v}


def parse_oauth_redirect(text):
    """从用户发来的 localhost 链接里解析 code 和 state。返回 (code, state)，没有就是 (None, None)。"""
    text = (text or "").strip()
    if "localhost" not in text and "127.0.0.1" not in text:
        return None, None
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(text).query)
    code = (qs.get("code") or [None])[0]
    state = (qs.get("state") or [None])[0]
    return code, state


def fetch_onedrive_drive(access_token):
    """用 access_token 问 Graph 拿默认 OneDrive 的 drive_id 和 drive_type。
    OneDrive 光有 token 不够，lsd 会报缺 drive_id/drive_type，所以建 remote 前要先拿。
    返回 (drive_id, drive_type)，拿不到就抛错。"""
    req = urllib.request.Request(
        "https://graph.microsoft.com/v1.0/me/drive",
        headers={"Authorization": "Bearer " + access_token},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            d = json.load(resp)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"问 OneDrive 要 drive 信息失败（HTTP {e.code}），token 可能有问题")
    except Exception as e:
        raise RuntimeError(f"问 OneDrive 要 drive 信息失败：{e}")
    drive_id = d.get("id")
    drive_type = d.get("driveType") or "personal"
    if not drive_id:
        raise RuntimeError("问 OneDrive 要 drive 信息失败：没拿到 drive id")
    return drive_id, drive_type


_OAUTH_TIMEOUT = 600            # 从开始授权算起，10 分钟内没完成就作废
_OAUTH_EXCHANGE_TIMEOUT = 120   # 拿到 code 后等 token 的最长时间（秒）


def cleanup_stale_picker_configs():
    """清掉上次没走完的选文件夹流程留下的临时配置（里面有 token）。"""
    for _f in glob.glob(os.path.join(tempfile.gettempdir(), "rclone-picker-*.conf")):
        try:
            os.remove(_f)
        except Exception:
            pass


class StorageFlow:
    """一个用户的添加/改位置流程状态机。uid -> state dict。"""

    def __init__(self):
        self._state = {}

    # ---------- 流程状态 ----------
    def has_flow(self, uid):
        return uid in self._state

    def get(self, uid):
        return self._state.get(uid)

    def cancel_flow(self, uid):
        """作废一次添加流程：正在跑的 authorize 进程也杀掉，正在等的任务会被唤醒后悄悄退出。"""
        self._cleanup_picker_config(uid)
        st = self._state.pop(uid, None)
        if not st:
            return
        proc = st.get("proc")
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
        q = st.get("oauth_queue")
        if q is not None:
            q.put_nowait(None)
        st["cancelled"] = True

    def _picker_config_path(self, uid):
        return os.path.join(tempfile.gettempdir(), f"rclone-picker-{uid}.conf")

    def _picker_rclone_args(self, st, uid):
        """选文件夹时调 rclone 用的 --config 参数：导入的直接用真实配置；
        OAuth/手动的用临时配置（不污染真实配置，流程结束就删）。"""
        if st.get("import"):
            return []
        params = {}
        if is_oauth(st["type"]):
            params = {"token": st.get("token") or ""}
            if st.get("drive_id"):
                params["drive_id"] = st["drive_id"]
            if st.get("drive_type"):
                params["drive_type"] = st["drive_type"]
        else:
            params = dict(st.get("data") or {})
        path = self._picker_config_path(uid)
        rclone_mod.write_remote_direct(st["remote"], st["type"], params, path=path)
        return ["--config", path]

    def _cleanup_picker_config(self, uid):
        try:
            os.remove(self._picker_config_path(uid))
        except Exception:
            pass

    # ---------- 添加流程的界面 ----------
    @staticmethod
    def add_text(page):
        total = len(_PROVIDERS)
        pages = len(_PROVIDER_PAGES)
        return f"加哪种网盘？一共 {total} 种（第 {page + 1}/{pages} 页），选一个。\nOAuth 的网盘会给你登录链接，登录后把 localhost 链接发我。"

    @staticmethod
    def add_buttons(page):
        buttons = []
        page_items = _PROVIDER_PAGES[page]
        for i in range(0, len(page_items), 2):
            row = [Button.inline(provider_label(p["prefix"]), data=f"stor:type:{p['prefix']}".encode()) for p in page_items[i:i + 2]]
            buttons.append(row)
        nav = []
        if page > 0:
            nav.append(Button.inline("⬅️ 上一页", data=f"stor:page:{page - 1}".encode()))
        if page < len(_PROVIDER_PAGES) - 1:
            nav.append(Button.inline("下一页 ➡️", data=f"stor:page:{page + 1}".encode()))
        if nav:
            buttons.append(nav)
        buttons.append([Button.inline("📥 已在 rclone 配好（导入）", data=b"stor:type:import")])
        buttons.append([Button.inline("❌ 取消", data=b"stor:cancel")])
        return buttons

    def _confirm_text(self, st):
        kind_label = provider_label(st["type"])
        folder = st.get("folder", "")
        return (f"确认一下：\n类型：{kind_label}\n名字：{st['remote']}\n文件夹：/{folder or '（根目录）'}\n\n没问题就点确认添加。")

    @staticmethod
    def _confirm_buttons():
        return [[Button.inline("✅ 确认添加", data=b"stor:confirm"), Button.inline("❌ 取消", data=b"stor:cancel")]]

    # ---------- 文件夹选择器 ----------
    async def show_folder_picker(self, event, uid, edit=False):
        """显示文件夹选择器：列出当前目录的子目录，按钮进目录/选定/新建/手动输入。"""
        st = self._state.get(uid)
        if not st:
            return
        path = st.get("browse_path", "")
        try:
            loop = asyncio.get_running_loop()
            dirs = await loop.run_in_executor(
                None, lambda: rclone_mod.list_remote_dirs(st["remote"], path, self._picker_rclone_args(st, uid)))
        except Exception as e:
            # 列不出目录就回落到手动输入。
            st["step"] = "folder"
            text = f"列不出网盘目录（{config._redact(str(e))[:150]}），手动发文件夹名；想存根目录就发“根目录”。"
            if edit:
                await event.edit(text)
            else:
                await event.reply(text)
            return
        st["browse_dirs"] = dirs
        disp_path = "/" + path if path else "/（根目录）"
        if st.get("mode") == "edit":
            disp = storage_display(_storages["storages"].get(st["remote"], st))
            text = f"把 {disp} 的保存位置改成哪个文件夹？当前：{disp_path}\n点一个目录进去，或者直接选“就存在这里”。"
        else:
            text = f"存在哪个文件夹？当前：{disp_path}\n点一个目录进去，或者直接选“就存在这里”。"
        buttons = []
        for i, name in enumerate(dirs[:25]):
            label = name if len(name) <= 30 else name[:29] + "…"
            buttons.append([Button.inline(f"📁 {label}", data=f"stor:cd:{i}".encode())])
        if len(dirs) > 25:
            buttons.append([Button.inline(f"…还有 {len(dirs) - 25} 个，手动输入进更深的目录", data=b"stor:manual")])
        row = [Button.inline("✅ 就存在这里", data=b"stor:sel")]
        if path:
            row.append(Button.inline("⬆️ 上一级", data=b"stor:up"))
        buttons.append(row)
        buttons.append([Button.inline("➕ 新建文件夹", data=b"stor:mkdir"), Button.inline("✏️ 手动输入", data=b"stor:manual")])
        buttons.append([Button.inline("❌ 取消", data=b"stor:cancel")])
        if edit:
            await event.edit(text, buttons=buttons)
        else:
            await event.reply(text, buttons=buttons)

    async def enter_folder_step(self, event, uid, edit=False, start_path=None):
        """进入选文件夹这一步：列目录，给用户选。改位置时从现在的文件夹开始，添加时从根目录开始。"""
        st = self._state.get(uid)
        if not st:
            return
        st["step"] = "folder"
        st["browse_path"] = start_path or ""
        st["browse_dirs"] = []
        await self.show_folder_picker(event, uid, edit=edit)

    async def finish_edit_folder(self, event, uid, folder, edit=False):
        """改位置收尾：把文件夹存进 storages.json，告诉用户改好了。"""
        st = self._state.get(uid)
        if not st:
            return
        remote = st["remote"]
        _storages["storages"][remote]["folder"] = folder
        save_storages()
        self._state.pop(uid, None)
        self._cleanup_picker_config(uid)
        disp = storage_display(_storages["storages"][remote])
        folder_disp = "/" + folder if folder else "/（根目录）"
        text = f"✅ 已把 {disp} 的保存位置改成 {folder_disp}。"
        buttons = [[Button.inline("🔙 返回存储管理", data=b"stor:menu")]]
        if edit:
            await event.edit(text, buttons=buttons)
        else:
            await event.reply(text, buttons=buttons)

    # ---------- OAuth ----------
    async def run_oauth_flow(self, event, uid, st):
        """跑 rclone authorize：拿登录链接发给用户，等用户把 localhost 链接发回来，换 token。
        换到 token 后把步骤切到 folder 并问文件夹；中途取消/超时就悄悄收尾。"""
        prefix = st["type"]
        t0 = asyncio.get_running_loop().time()
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                rclone_mod.rclone_bin(), "authorize", "--auth-no-open-browser", prefix,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        except Exception as e:
            await event.reply(f"启动授权失败：{e}")
            self._state.pop(uid, None)
            return
        st["proc"] = proc
        st["oauth_queue"] = asyncio.Queue()
        try:
            # 1. 等 rclone 吐出本地授权链接（/auth?state=...）
            auth_url = None
            try:
                while asyncio.get_running_loop().time() - t0 < 60:
                    line = await asyncio.wait_for(proc.stdout.readline(), timeout=60)
                    if not line:
                        break
                    m = re.search(r"http://127\.0\.0\.1:53682/auth\?state=\S+", line.decode(errors="replace"))
                    if m:
                        auth_url = m.group(0).rstrip("\"'<>")
                        break
            except asyncio.TimeoutError:
                pass
            if not auth_url:
                if st.get("cancelled"):
                    return
                await event.reply("没拿到登录链接，授权启动失败了，从 /storage 重新试一次吧。")
                self._state.pop(uid, None)
                return
            # 记下这次授权的 state：用户贴回来的链接 state 得跟它一样才是这次的，
            # 不一样就是贴了上一次（旧）的链接，直接告诉用户，不用等回放超时才发现。
            m_state = re.search(r"state=([A-Za-z0-9_-]+)", auth_url)
            if m_state:
                st["oauth_state"] = m_state.group(1)
            # 2. 跟着本地链接的 307 跳转，拿到真正的登录页地址（用 rclone 自带的应用）
            def _fetch_redirect(url):
                req = urllib.request.Request(url, method="GET")

                class NoRedir(urllib.request.HTTPRedirectHandler):
                    def redirect_request(self, req, fp, code, msg, headers, newurl):
                        return None

                opener = urllib.request.build_opener(NoRedir)
                try:
                    opener.open(req, timeout=15)
                    return None
                except urllib.error.HTTPError as e:
                    return e.headers.get("Location")
                except Exception:
                    return None

            loop = asyncio.get_running_loop()
            provider_url = await loop.run_in_executor(None, lambda: _fetch_redirect(auth_url))
            if not provider_url:
                if st.get("cancelled"):
                    return
                await event.reply("没拿到登录页地址，授权启动失败了，从 /storage 重新试一次吧。")
                self._state.pop(uid, None)
                return
            st["step"] = "oauth_wait"
            await event.reply(
                f"点这个链接登录{provider_label(prefix)}：\n{provider_url}\n\n登录后浏览器会跳到一个打不开的 localhost 页面，把地址栏的链接完整复制发我。10 分钟内有效，不加了就发“取消”。")
            # 3. 等用户把 localhost 链接发回来（带 code 的），回放给本机上的 authorize 服务
            got_code = False
            while asyncio.get_running_loop().time() - t0 < _OAUTH_TIMEOUT:
                try:
                    pasted = await asyncio.wait_for(
                        st["oauth_queue"].get(),
                        timeout=max(1, _OAUTH_TIMEOUT - (asyncio.get_running_loop().time() - t0)))
                except asyncio.TimeoutError:
                    break
                if pasted is None:  # 取消了
                    return
                code, pasted_state = parse_oauth_redirect(pasted)
                if not code:
                    await event.reply("这个链接里没有 code——登录完浏览器地址栏那个 localhost 链接才是。这次添加作废了，从 /storage 重新来吧。")
                    self.cancel_flow(uid)
                    return
                expected = st.get("oauth_state")
                if not pasted_state:
                    await event.reply("这个链接里没有 state，可能是复制得不完整。这次添加作废了，从 /storage 重新来吧。")
                    self.cancel_flow(uid)
                    return
                if expected and pasted_state != expected:
                    config.log(f"OAuth state 对不上：这次要 {expected[:8]}…，收到 {pasted_state[:8]}…（贴了旧链接）")
                    await event.reply("这个链接不是这次授权的（state 对不上），是之前那次的链接。这次添加作废了，从 /storage 重新来一遍，点最新那条消息里的登录链接，别用浏览器里旧的那个标签页。")
                    self.cancel_flow(uid)
                    return
                parsed = urllib.parse.urlparse(pasted.strip())
                cb_url = f"http://127.0.0.1:53682{parsed.path}" + (f"?{parsed.query}" if parsed.query else "")

                def _replay(url):
                    try:
                        with urllib.request.urlopen(url, timeout=10) as resp:
                            return resp.status
                    except urllib.error.HTTPError as e:
                        return e.code
                    except Exception as e:
                        return str(e)

                await loop.run_in_executor(None, lambda: _replay(cb_url))
                got = False
                try:
                    for _ in range(10):
                        line = await asyncio.wait_for(proc.stdout.readline(), timeout=1)
                        if not line:
                            break
                        if "Got code" in line.decode(errors="replace"):
                            got = True
                            break
                except asyncio.TimeoutError:
                    pass
                if not got:
                    # state 对上了但回放没拿到 code：code 过期或已经用过了。
                    config.log("OAuth 回放后没拿到 Got code：state 对上了，但 code 可能过期或已用过")
                    await event.reply("这个链接的 code 好像过期或已经用过了。这次添加作废了，从 /storage 重新来一遍。")
                    self.cancel_flow(uid)
                    return
                got_code = True
                break
            if not got_code:
                if st.get("cancelled"):
                    return
                await event.reply("授权超时了（10 分钟），这次作废，从 /storage 重新来吧。")
                self._state.pop(uid, None)
                return
            # 4. 等 rclone 拿 code 换 token（换完进程自己退出）
            out_lines = []
            token = None
            try:
                while True:
                    line = await asyncio.wait_for(proc.stdout.readline(), timeout=_OAUTH_EXCHANGE_TIMEOUT)
                    if not line:
                        break
                    text = line.decode(errors="replace")
                    out_lines.append(text)
                    m = re.search(r"\{[^{}]*\"access_token\"[^{}]*\}", text)
                    if m:
                        token = m.group(0)
                        break
            except asyncio.TimeoutError:
                pass
            if proc.returncode is None:
                try:
                    proc.kill()
                except Exception:
                    pass
            if not token:
                if st.get("cancelled"):
                    return
                tail = "".join(out_lines[-6:]).strip()
                await event.reply(f"换 token 失败了：\n{tail or 'rclone 没给回 token'}\n可以从 /storage 重新试一次。")
                self._state.pop(uid, None)
                return
            st["token"] = token
            await event.reply("✅ 登录好了，token 已拿到。")
            if st["type"] == "onedrive":
                # OneDrive 还得先拿 drive_id / drive_type，不然列目录和 lsd 都会报缺这两项。
                try:
                    tok = json.loads(token)
                except Exception:
                    tok = {}
                try:
                    drive_id, drive_type = await loop.run_in_executor(None, lambda: fetch_onedrive_drive(tok.get("access_token", "")))
                except Exception as e:
                    await event.reply(f"拿 OneDrive 的 drive 信息失败了：{str(e)[:200]}\n可以从 /storage 重新试一次。")
                    self._state.pop(uid, None)
                    return
                st["drive_id"] = drive_id
                st["drive_type"] = drive_type
            await self.enter_folder_step(event, uid)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            config.log(f"OAuth 授权流程出错：{e}")
            if not st.get("cancelled"):
                try:
                    await event.reply(f"授权流程出错了：{e}\n可以从 /storage 重新试一次。")
                except Exception:
                    pass
            self._state.pop(uid, None)
        finally:
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                except Exception:
                    pass
            st.pop("proc", None)
            st.pop("oauth_queue", None)

    # ---------- 添加流程的文字输入 ----------
    async def _ask_field(self, event, uid):
        """问添加流程的下一项（按类型要填的字段顺序）。"""
        st = self._state.get(uid)
        if not st:
            return
        fields = manual_fields(st["type"])
        if st["idx"] < len(fields):
            key, prompt, secret, optional = fields[st["idx"]]
            await event.reply(prompt + ("（发完我会删掉这条消息）" if secret else ""))

    async def handle_text(self, event, uid):
        """添加存储流程中用户发来的文字：名字 / 某一项凭据 / 补充参数 / 文件夹。"""
        st = self._state.get(uid)
        if not st:
            return
        text = (event.text or "").strip()
        if text in ("取消", "/cancel"):
            self.cancel_flow(uid)
            await event.reply("已取消添加存储。")
            return
        if st["step"] == "name":
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", text):
                await event.reply("名字只能用字母、数字、_ 和 -，1 到 32 个字符，再发一次。")
                return
            if text in rclone_mod.list_remotes() or text in _storages["storages"]:
                await event.reply(f"已经有个叫 {text} 的存储了，换个名字。")
                return
            st["remote"] = text
            if is_oauth(st["type"]):
                st["step"] = "oauth"
                await event.reply(f"好的，名字就叫 {text}。\n正在启动授权…")
                asyncio.create_task(self.run_oauth_flow(event, uid, st))
            else:
                st["step"] = "field"
                st["idx"] = 0
                fields = manual_fields(st["type"])
                if fields:
                    await self._ask_field(event, uid)
                else:
                    st["step"] = "extra"
                    hint = extra_hint(st["type"])
                    await event.reply("发配置参数，一行一个 key=value。" + (f"\n常用参数：{hint}。" if hint else "") + "\n没有就发“跳过”。")
            return
        if st["step"] == "field":
            fields = manual_fields(st["type"])
            key, prompt, secret, optional = fields[st["idx"]]
            if secret:
                # 密钥消息读完就删掉，不留在聊天记录里；删不掉就提醒用户自己删。
                try:
                    await event.delete()
                except Exception:
                    pass
            if optional and text in ("跳过", "skip", ""):
                st["data"][key] = ""
            else:
                if not text:
                    await event.reply("这一项不能为空，再发一次。")
                    return
                st["data"][key] = text
            st["idx"] += 1
            if st["idx"] < len(fields):
                await self._ask_field(event, uid)
            else:
                st["step"] = "extra"
                await event.reply("还要补充参数吗？一行一个 key=value，没有就发“跳过”。")
            return
        if st["step"] == "extra":
            if text not in ("跳过", "完成", "skip", "done", ""):
                bad = []
                sensitive = False
                for line in text.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    if "=" not in line:
                        bad.append(line)
                        continue
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip()
                    if not k:
                        bad.append(line)
                        continue
                    st["data"][k] = v
                    kl = k.lower()
                    if any(s in kl for s in ("secret", "pass", "token", "key", "pwd")):
                        sensitive = True
                if bad:
                    await event.reply(f"这几行没看懂（要 key=value 的格式）：{', '.join(bad)}，重新发，或者发“跳过”。")
                    return
                if sensitive:
                    # 里面有密钥，读完删掉这条消息。
                    try:
                        await event.delete()
                    except Exception:
                        pass
            st["step"] = "folder"
            await self.enter_folder_step(event, uid)
            return
        if st["step"] == "mkdir":
            name = text.strip().strip("/")
            if not name or name in (".", "..") or "/" in name or not re.fullmatch(r"[A-Za-z0-9_.\- ]+", name):
                await event.reply("名字不太对，只能用字母、数字、_、-、. 和空格（不能带 /），再发一次。")
                return
            cur = st.get("browse_path", "")
            new_path = f"{cur}/{name}" if cur else name
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    None, lambda: rclone_mod.rclone_mkdir(st["remote"], new_path, self._picker_rclone_args(st, uid)))
            except Exception as e:
                await event.reply(f"建文件夹失败：{config._redact(str(e))[:200]}\n再发一个名字试试，或者发“取消”。")
                return
            st["folder"] = new_path
            if st.get("mode") == "edit":
                await self.finish_edit_folder(event, uid, new_path)
                return
            st["step"] = "confirm"
            self._cleanup_picker_config(uid)
            await event.reply(self._confirm_text(st), buttons=self._confirm_buttons())
            return
        if st["step"] == "folder":
            folder = "" if text in ("根目录", "跳过", "") else text.strip("/")
            if folder and not re.fullmatch(r"[A-Za-z0-9_.\-/]+", folder):
                await event.reply("文件夹名不太对，只能用字母、数字、_、-、. 和 /，再发一次。")
                return
            if st.get("mode") == "edit":
                await self.finish_edit_folder(event, uid, folder)
                return
            st["folder"] = folder
            st["step"] = "confirm"
            self._cleanup_picker_config(uid)
            await event.reply(self._confirm_text(st), buttons=self._confirm_buttons())
            return

    # ---------- 回调注册 ----------
    def register_callbacks(self, client):
        flow = self

        @client.on(events.CallbackQuery(pattern=rb"stor:menu"))
        async def storage_menu_handler(event):
            if await auth.cb_uid(event) is None:
                return
            await event.edit(menu_text(), buttons=menu_buttons())

        @client.on(events.CallbackQuery(pattern=rb"stor:add"))
        async def storage_add_handler(event):
            if await auth.cb_uid(event) is None:
                return
            await event.edit(flow.add_text(0), buttons=flow.add_buttons(0))

        @client.on(events.CallbackQuery(pattern=rb"stor:page:(\d+)"))
        async def storage_page_handler(event):
            if await auth.cb_uid(event) is None:
                return
            page = int(event.pattern_match.group(1))
            page = max(0, min(page, len(_PROVIDER_PAGES) - 1))
            await event.edit(flow.add_text(page), buttons=flow.add_buttons(page))

        @client.on(events.CallbackQuery(pattern=rb"stor:type:(.+)"))
        async def storage_type_handler(event):
            uid = await auth.cb_uid(event)
            if uid is None:
                return
            type_ = event.pattern_match.group(1).decode()
            if type_ == "import":
                loop = asyncio.get_running_loop()
                remotes = await loop.run_in_executor(None, rclone_mod.list_remotes)
                candidates = [r for r in remotes if r not in _storages["storages"]]
                if not candidates:
                    await event.answer("rclone 配置里没有还没加进来的网盘。", alert=True)
                    return
                buttons = [[Button.inline(r, data=f"stor:import:{r}".encode())] for r in candidates]
                buttons.append([Button.inline("❌ 取消", data=b"stor:cancel")])
                await event.edit("rclone 配置里有下面这些还没加进来的网盘，选一个导入：", buttons=buttons)
                return
            if type_ not in _PROVIDER_MAP:
                await event.answer("不认识这个类型。", alert=True)
                return
            # 先把上一次没走完的流程作废掉（正在跑的 authorize 进程也杀掉），不然旧进程还占着
            # 53682 端口，新流程的授权链接会对不上，用户贴回来的链接就一直报“对不上”。
            flow.cancel_flow(uid)
            flow._state[uid] = {"step": "name", "type": type_, "remote": "", "idx": 0, "data": {}, "folder": "", "import": False}
            if is_oauth(type_):
                note = "接下来给你登录链接，登录后把跳回来的 localhost 链接发我。"
            else:
                note = "接下来按提示发配置。"
            await event.edit(f"添加{provider_label(type_)}。先给它起个名字，只能用字母、数字、_ 和 -，比如 mydrive：\n{note}")

        @client.on(events.CallbackQuery(pattern=rb"stor:import:(.+)"))
        async def storage_import_handler(event):
            uid = await auth.cb_uid(event)
            if uid is None:
                return
            remote = event.pattern_match.group(1).decode()
            if remote in _storages["storages"]:
                await event.answer("这个已经加过了。", alert=True)
                return
            kind = rclone_mod.remote_type(remote) or "other"
            flow.cancel_flow(uid)
            flow._state[uid] = {"step": "folder", "type": kind, "remote": remote, "idx": 0, "data": {}, "folder": "", "import": True}
            await event.edit(f"导入 {remote}（{kind}）。正在列目录…")
            await flow.enter_folder_step(event, uid, edit=True)

        @client.on(events.CallbackQuery(pattern=rb"stor:confirm"))
        async def storage_confirm_handler(event):
            uid = await auth.cb_uid(event)
            if uid is None:
                return
            st = flow._state.pop(uid, None)
            if not st or st.get("step") != "confirm":
                await event.answer("这个添加流程已经不在了，请重新从 /storage 开始。", alert=True)
                return
            flow._cleanup_picker_config(uid)
            await event.answer("正在添加…")
            try:
                loop = asyncio.get_running_loop()
                if not st.get("import"):
                    if is_oauth(st["type"]):
                        # OAuth 后端：config create 会重新跑 OAuth 卡住，直接写 rclone.conf。
                        # OneDrive 还得带上 drive_id / drive_type，不然 lsd 会报缺这两项
                        #（登录后就已经拿到了，这里只是兜底再拿一次）。
                        params = {"token": st.get("token") or ""}
                        if st["type"] == "onedrive":
                            if not st.get("drive_id"):
                                try:
                                    tok = json.loads(st.get("token") or "{}")
                                except Exception:
                                    tok = {}
                                drive_id, drive_type = await loop.run_in_executor(None, lambda: fetch_onedrive_drive(tok.get("access_token", "")))
                                st["drive_id"], st["drive_type"] = drive_id, drive_type
                            params["drive_id"] = st["drive_id"]
                            params["drive_type"] = st["drive_type"]
                        await event.edit("正在写入 rclone 配置…")
                        await loop.run_in_executor(None, lambda: rclone_mod.write_remote_direct(st["remote"], st["type"], params))
                    else:
                        params = build_create_params(st["type"], st["data"], token=st.get("token"))
                        await event.edit("正在写入 rclone 配置…")
                        await loop.run_in_executor(None, lambda: rclone_mod.run_config_create(st["remote"], st["type"], params))
                await event.edit("正在验证这个网盘能不能用…")
                ok = await loop.run_in_executor(None, lambda: rclone_mod.verify_remote(st["remote"]))
                if not ok:
                    # 有些网盘第一次要现发现 drive 之类，隔 5 秒再试一次。
                    await asyncio.sleep(5)
                    ok = await loop.run_in_executor(None, lambda: rclone_mod.verify_remote(st["remote"]))
                if not ok:
                    if not st.get("import"):
                        rclone_mod.delete_remote(st["remote"])
                    raise RuntimeError("验证失败：rclone 列不出这个网盘的目录，请检查填的凭据对不对")
                label = provider_label(st["type"])
                _storages["storages"][st["remote"]] = {
                    "remote": st["remote"],
                    "folder": st.get("folder", ""),
                    "kind": st["type"],
                    "label": label,
                }
                save_storages()
                disp = storage_display(_storages["storages"][st["remote"]])
                folder = f"/{st.get('folder')}" if st.get("folder") else "/（根目录）"
                extra = ""
                if not _storages.get("default") and len(_storages["storages"]) > 1:
                    extra = "\n还没选默认存储，下载任务完成时会问你存到哪个；也可以现在点下面设为默认。"
                buttons = None
                if not _storages.get("default"):
                    buttons = [[Button.inline("⭐ 设为默认存储", data=f"stor:setdef:{st['remote']}".encode())]]
                await event.edit(f"✅ 已添加存储：{disp}\n文件夹：{folder}{extra}", buttons=buttons)
                config.log(f"已添加存储 {st['remote']}（{st['type']}）")
            except Exception as e:
                # 报错里可能带上完整命令（含 token），记日志前先抹掉。
                config.log(f"添加存储失败：{config._redact(str(e))[:300]}")
                await event.edit(f"❌ 添加失败：{str(e)[:300]}\n可以从 /storage 重新试一次。")

        @client.on(events.CallbackQuery(pattern=rb"stor:cancel"))
        async def storage_cancel_handler(event):
            uid = await auth.cb_uid(event)
            if uid is None:
                return
            flow.cancel_flow(uid)
            await event.edit("已取消添加存储。", buttons=[[Button.inline("🔙 返回存储管理", data=b"stor:menu")]])

        async def _picker_guard(event):
            """选文件夹的按钮都先过这一关：流程不在了就别往下走。返回 st 或 None。"""
            uid = await auth.cb_uid(event)
            if uid is None:
                return None
            st = flow._state.get(uid)
            if not st or st.get("step") not in ("folder", "mkdir"):
                await event.answer("这个选择已经不在了，请重新从 /storage 开始。", alert=True)
                return None
            return st

        @client.on(events.CallbackQuery(pattern=rb"stor:cd:(\d+)"))
        async def storage_cd_handler(event):
            st = await _picker_guard(event)
            if not st:
                return
            if st.get("step") != "folder":
                await event.answer("先完成新建文件夹那一步。", alert=True)
                return
            idx = int(event.pattern_match.group(1))
            dirs = st.get("browse_dirs") or []
            if idx < 0 or idx >= len(dirs):
                await event.answer("这个目录已经不在了。", alert=True)
                return
            cur = st.get("browse_path", "")
            st["browse_path"] = f"{cur}/{dirs[idx]}" if cur else dirs[idx]
            await flow.show_folder_picker(event, event.sender_id or 0, edit=True)

        @client.on(events.CallbackQuery(pattern=rb"stor:up"))
        async def storage_up_handler(event):
            st = await _picker_guard(event)
            if not st:
                return
            if st.get("step") != "folder":
                await event.answer("先完成新建文件夹那一步。", alert=True)
                return
            cur = st.get("browse_path", "")
            st["browse_path"] = cur.rsplit("/", 1)[0] if "/" in cur else ""
            await flow.show_folder_picker(event, event.sender_id or 0, edit=True)

        @client.on(events.CallbackQuery(pattern=rb"stor:sel"))
        async def storage_sel_handler(event):
            st = await _picker_guard(event)
            if not st:
                return
            if st.get("step") != "folder":
                await event.answer("先完成新建文件夹那一步。", alert=True)
                return
            if st.get("mode") == "edit":
                # 改位置：直接把选好的文件夹存进 storages.json，不用再确认。
                await flow.finish_edit_folder(event, event.sender_id or 0, st.get("browse_path", ""), edit=True)
                return
            st["folder"] = st.get("browse_path", "")
            st["step"] = "confirm"
            flow._cleanup_picker_config(event.sender_id or 0)
            await event.edit(flow._confirm_text(st), buttons=flow._confirm_buttons())

        @client.on(events.CallbackQuery(pattern=rb"stor:mkdir"))
        async def storage_mkdir_handler(event):
            st = await _picker_guard(event)
            if not st:
                return
            if st.get("step") != "folder":
                await event.answer("已经在新建了，直接发名字就行。", alert=True)
                return
            st["step"] = "mkdir"
            cur = st.get("browse_path", "")
            disp = "/" + cur if cur else "/（根目录）"
            await event.edit(f"新文件夹叫什么名字？只会建在 {disp} 下面。发一个名字就行。", buttons=[[Button.inline("❌ 取消", data=b"stor:cancel")]])

        @client.on(events.CallbackQuery(pattern=rb"stor:manual"))
        async def storage_manual_handler(event):
            st = await _picker_guard(event)
            if not st:
                return
            st["step"] = "folder"
            if st.get("mode") == "edit":
                await event.edit("发一下新的保存位置：文件夹名；想存根目录就发“根目录”。")
            else:
                await event.edit("发文件夹名；想存根目录就发“根目录”。")

        @client.on(events.CallbackQuery(pattern=rb"stor:editfolder"))
        async def storage_editfolder_handler(event):
            if await auth.cb_uid(event) is None:
                return
            items = storage_list()
            if not items:
                await event.answer("还没有存储，先添加一个。", alert=True)
                return
            buttons = []
            for s in items:
                folder = f"/{s.get('folder')}" if s.get("folder") else "/（根目录）"
                buttons.append([Button.inline(f"📁 {storage_display(s)} → {folder}", data=f"stor:edit:{s['remote']}".encode())])
            buttons.append([Button.inline("🔙 返回存储管理", data=b"stor:menu")])
            await event.edit("改哪个存储的保存位置？", buttons=buttons)

        @client.on(events.CallbackQuery(pattern=rb"stor:edit:(.+)"))
        async def storage_edit_handler(event):
            uid = await auth.cb_uid(event)
            if uid is None:
                return
            remote = event.pattern_match.group(1).decode()
            s = _storages["storages"].get(remote)
            if not s:
                await event.answer("这个存储已经不在了。", alert=True)
                return
            # 先把正在进行的添加/改位置流程作废掉，再开改位置的流程。
            flow.cancel_flow(uid)
            flow._state[uid] = {
                "step": "folder", "mode": "edit", "type": s.get("kind", ""), "remote": remote,
                "idx": 0, "data": {}, "folder": s.get("folder", ""), "import": True,
            }
            await event.edit("正在列目录…")
            await flow.enter_folder_step(event, uid, edit=True, start_path=s.get("folder", ""))

        @client.on(events.CallbackQuery(pattern=rb"stor:default"))
        async def storage_default_handler(event):
            if await auth.cb_uid(event) is None:
                return
            buttons = [[Button.inline(f"⭐ {storage_display(s)}", data=f"stor:setdef:{s['remote']}".encode())] for s in storage_list()]
            if _storages.get("default"):
                buttons.append([Button.inline("清除默认存储", data=b"stor:cleardef")])
            buttons.append([Button.inline("🔙 返回", data=b"stor:menu")])
            await event.edit("选一个作为默认存储，下载任务完成就直接往它传：", buttons=buttons)

        @client.on(events.CallbackQuery(pattern=rb"stor:setdef:(.+)"))
        async def storage_setdef_handler(event):
            if await auth.cb_uid(event) is None:
                return
            remote = event.pattern_match.group(1).decode()
            s = _storages["storages"].get(remote)
            if not s:
                await event.answer("这个存储已经不在了。", alert=True)
                return
            _storages["default"] = remote
            save_storages()
            config.log(f"默认存储改为 {remote}")
            await event.edit(f"✅ 默认存储已设为：{storage_display(s)}\n以后下载任务完成默认往它传。", buttons=[[Button.inline("🔙 返回存储管理", data=b"stor:menu")]])

        @client.on(events.CallbackQuery(pattern=rb"stor:cleardef"))
        async def storage_cleardef_handler(event):
            if await auth.cb_uid(event) is None:
                return
            _storages["default"] = None
            save_storages()
            await event.edit("已清除默认存储。多个网盘时，下载任务完成会问你存到哪个。", buttons=[[Button.inline("🔙 返回存储管理", data=b"stor:menu")]])

        @client.on(events.CallbackQuery(pattern=rb"stor:dellist"))
        async def storage_dellist_handler(event):
            if await auth.cb_uid(event) is None:
                return
            items = storage_list()
            if not items:
                await event.answer("还没有存储，先添加一个。", alert=True)
                return
            buttons = []
            for s in items:
                buttons.append([Button.inline(f"🗑 删除 {storage_display(s)}", data=f"stor:del:{s['remote']}".encode())])
            buttons.append([Button.inline("🔙 返回存储管理", data=b"stor:menu")])
            await event.edit("删除哪个存储？", buttons=buttons)

        @client.on(events.CallbackQuery(pattern=rb"stor:del:(.+)"))
        async def storage_del_handler(event):
            if await auth.cb_uid(event) is None:
                return
            remote = event.pattern_match.group(1).decode()
            s = _storages["storages"].get(remote)
            if not s:
                await event.answer("这个存储已经不在了。", alert=True)
                return
            await event.edit(
                f"确定删除 {storage_display(s)} 吗？\nrclone 配置里的这个网盘也会删掉，网盘上的文件不动；是默认存储的话默认也会清除。",
                buttons=[[Button.inline("🗑 确认删除", data=f"stor:delconfirm:{remote}".encode()), Button.inline("取消", data=b"stor:menu")]],
            )

        @client.on(events.CallbackQuery(pattern=rb"stor:delconfirm:(.+)"))
        async def storage_delconfirm_handler(event):
            if await auth.cb_uid(event) is None:
                return
            remote = event.pattern_match.group(1).decode()
            s = _storages["storages"].pop(remote, None)
            if not s:
                await event.answer("这个存储已经不在了。", alert=True)
                return
            if _storages.get("default") == remote:
                _storages["default"] = None
            save_storages()
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: rclone_mod.delete_remote(remote))
            config.log(f"已删除存储 {remote}")
            await event.edit(f"已删除存储 {storage_display(s)}。", buttons=[[Button.inline("🔙 返回存储管理", data=b"stor:menu")]])
