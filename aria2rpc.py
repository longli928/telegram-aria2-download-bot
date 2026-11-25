#!/usr/bin/env python3
"""aria2：JSON-RPC 客户端 + 守护进程管理。

- bot 自己起 aria2c：二进制在 ./bin/aria2c（setup.sh 下的静态版），
  RPC 只监听 127.0.0.1:6800，带 secret；bot 启动时先测 RPC 通不通，
  不通就拉起 aria2c 再等它就绪。
- secret：.env 里的 ARIA2_SECRET 优先；没填就随机生成，存进 aria2-secret.txt（chmod 600），
  下次启动接着用。
- 直链、magnet、.torrent 链接都走同一个 addUri；.torrent 文件走 addTorrent（base64）。
- 这里只做同步 RPC 调用（urllib），调用方在线程里跑，不堵事件循环。
"""
import json
import os
import secrets
import subprocess
import time
import urllib.error
import urllib.request

import config

_SECRET_FILE = os.path.join(config.BASE_DIR, "aria2-secret.txt")
_ARIA2_LOG = os.path.join(config.BASE_DIR, "aria2.log")
_daemon_proc = None


def get_secret():
    """拿 RPC secret：.env 优先；没有就生成一个存文件（chmod 600），下次接着用。"""
    if config.ARIA2_SECRET:
        return config.ARIA2_SECRET
    try:
        with open(_SECRET_FILE, encoding="utf-8") as f:
            s = f.read().strip()
        if s:
            return s
    except Exception:
        pass
    s = secrets.token_urlsafe(24)
    try:
        with open(_SECRET_FILE, "w", encoding="utf-8") as f:
            f.write(s + "\n")
        os.chmod(_SECRET_FILE, 0o600)
    except Exception as e:
        config.log(f"保存 aria2 secret 失败：{e}")
    return s


def _rpc_url():
    return f"http://{config.ARIA2_RPC_HOST}:{config.ARIA2_RPC_PORT}/jsonrpc"


class Aria2Error(RuntimeError):
    pass


class Aria2Client:
    """aria2 JSON-RPC 客户端（同步，调用方放线程里跑）。"""

    def __init__(self, secret=None, url=None):
        self.secret = secret if secret is not None else get_secret()
        self.url = url or _rpc_url()

    def _call(self, method, params=None):
        payload = {
            "jsonrpc": "2.0",
            "id": f"aria2-{int(time.time() * 1000)}",
            "method": method,
            "params": [f"token:{self.secret}"] + list(params or []),
        }
        req = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise Aria2Error(f"aria2 RPC HTTP {e.code}")
        except Exception as e:
            raise Aria2Error(f"aria2 RPC 连不上：{e}")
        if "error" in body:
            err = body["error"]
            raise Aria2Error(f"aria2 报错：{err.get('message', err)}")
        return body.get("result")

    def ping(self):
        """RPC 通不通：能拿到版本号就算通。"""
        try:
            self._call("aria2.getVersion")
            return True
        except Exception:
            return False

    def add_uri(self, uris, options=None):
        """加一个下载任务：直链 / magnet / .torrent 链接都走这里。返回 gid。"""
        return self._call("aria2.addUri", [list(uris), dict(options or {})])

    def add_torrent(self, torrent_b64, uris=None, options=None):
        """加一个 .torrent 文件（base64 编码的内容）。返回 gid。"""
        return self._call("aria2.addTorrent", [torrent_b64, list(uris or []), dict(options or {})])

    def tell_status(self, gid, keys=None):
        return self._call("aria2.tellStatus", [gid, list(keys or [])])

    def tell_active(self, keys=None):
        return self._call("aria2.tellActive", [list(keys or [])])

    def tell_waiting(self, keys=None):
        return self._call("aria2.tellWaiting", [0, 100, list(keys or [])])

    def tell_stopped(self, keys=None):
        return self._call("aria2.tellStopped", [0, 100, list(keys or [])])

    def get_option(self, gid):
        """取一个下载的选项（含 dir，即下载目录）。"""
        return self._call("aria2.getOption", [gid])

    def remove(self, gid, force=False):
        return self._call("aria2.forceRemove" if force else "aria2.remove", [gid])

    def remove_download_result(self, gid):
        """从已停列表里清掉一个下载（complete/error/removed 只能用这个，remove 删不掉）。"""
        return self._call("aria2.removeDownloadResult", [gid])

    def change_global_option(self, options):
        """改 aria2 全局选项（影响之后新加的下载），如 bt-tracker。"""
        return self._call("aria2.changeGlobalOption", [dict(options)])

    def change_option(self, gid, options):
        """改某个下载的选项（正在跑的也能改），如 bt-tracker。"""
        return self._call("aria2.changeOption", [gid, dict(options)])

    def get_global_stat(self):
        return self._call("aria2.getGlobalStat")

    def purge_download_result(self):
        return self._call("aria2.purgeDownloadResult")


def _ca_certificate_arg():
    """系统上 /etc/ssl/certs/ca-certificates.crt 通常是有的，aria2 用它验 HTTPS；
    没有就不传这个参数，让 aria2 用默认的。"""
    for cand in ("/etc/ssl/certs/ca-certificates.crt", "/etc/ssl/cert.pem"):
        if os.path.exists(cand):
            return ["--ca-certificate=" + cand]
    return []


# 默认公开 tracker：ngosang/trackerslist 和 adysec/tracker 两家的 trackers_best，
# 每天自动测速更新。磁力链接经常一个 tracker 都不带，只靠 DHT 找人；
# DHT 在受限容器里可能半天都连不上节点，给所有 BT 任务默认带上这组 tracker。
# bot 启动 aria2 时先去拉最新的 best 列表，拉不到就用下面这份 2026-11-25 的快照兜底。
_TRACKERS_URLS = (
    "https://raw.githubusercontent.com/ngosang/trackerslist/master/trackers_best.txt",
    "https://cdn.jsdelivr.net/gh/ngosang/trackerslist@master/trackers_best.txt",
    "https://raw.githubusercontent.com/adysec/tracker/main/trackers_best.txt",
    "https://cdn.jsdelivr.net/gh/adysec/tracker@main/trackers_best.txt",
)
_DEFAULT_TRACKERS = (
    "udp://tracker.opentrackr.org:1337/announce,"
    "udp://open.stealth.si:80/announce,"
    "udp://tracker.torrent.eu.org:451/announce,"
    "udp://open.demonii.com:1337/announce,"
    "udp://open.tracker.cl:1337/announce,"
    "udp://tracker.qu.ax:6969/announce,"
    "udp://tracker-udp.gbitt.info:80/announce,"
    "udp://tracker.corpscorp.online:80/announce,"
    "udp://tracker.bittor.pw:1337/announce,"
    "udp://tracker.dler.org:6969/announce,"
    "http://tracker.dler.com:6969/announce,"
    "udp://tracker2.dler.org:80/announce,"
    "udp://retracker01-msk-virt.corbina.net:80/announce,"
    "udp://explodie.org:6969/announce,"
    "udp://tracker.tryhackx.org:6969/announce,"
    "udp://tracker.ducks.party:1984/announce,"
    "udp://tracker.nyaa.vc:6969/announce,"
    "udp://tracker.t-rb.org:6969/announce,"
    "udp://tracker.peerfect.org:6969/announce,"
    "udp://tracker.opentrackr.com:6969/announce"
)


def _load_trackers():
    """从所有 tracker 源拉最新的 best 列表，合并去重：ngosang 和 adysec 两家，
    各走 GitHub raw 和 jsDelivr 镜像。哪个都拉不到就用内置快照。"""
    merged = []
    seen = set()
    for url in _TRACKERS_URLS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "mirror-bot"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                lines = [
                    ln.strip()
                    for ln in resp.read().decode("utf-8", "ignore").splitlines()
                ]
            good = [ln for ln in lines if ln.startswith(("udp://", "http://", "https://", "wss://"))]
            new = [ln for ln in good if ln not in seen]
            for ln in new:
                seen.add(ln)
            merged.extend(new)
            config.log(f"tracker 列表已更新：{url}，{len(good)} 个（新增 {len(new)} 个）")
        except Exception as e:
            config.log(f"拉 tracker 列表失败（{url}）：{e}")
    if len(merged) >= 5:
        return ",".join(merged)
    config.log("tracker 列表用内置快照")
    return _DEFAULT_TRACKERS


# BT 提速：多连 peer，速度低时一直找更多 peer。迅雷快靠的是 CDN 和人多，
# aria2 没有这些，只能尽量多连 peer。0 做种者的种子是硬伤，调参只能改善，
# 不能保证到迅雷的 10 MB/s。
_BT_TUNING = {
    "bt-max-peers": "100",
    "bt-request-peer-speed-limit": "1M",
}


def apply_tuning(client=None):
    """把 BT 提速参数推给正在跑的 aria2（不重启）：全局 + 进行中的下载。返回推给几个下载。"""
    client = client or Aria2Client()
    try:
        client.change_global_option(dict(_BT_TUNING))
    except Exception as e:
        config.log(f"更新 aria2 全局提速参数失败：{e}")
        return 0
    updated = 0
    try:
        active = client.tell_active(["gid"]) or []
        waiting = client.tell_waiting(["gid"]) or []
    except Exception:
        active, waiting = [], []
    for st in list(active) + list(waiting):
        gid = st.get("gid")
        if not gid:
            continue
        try:
            client.change_option(gid, dict(_BT_TUNING))
            updated += 1
        except Exception:
            pass
    config.log(f"aria2 提速参数已应用：bt-max-peers=100，bt-request-peer-speed-limit=1M（全局 + {updated} 个进行中的下载）")
    return updated


def refresh_trackers(client=None):
    """拉最新的 tracker 列表，推给正在跑的 aria2（不重启）：全局 + 进行中的下载都换成新的。
    只改附加 tracker（bt-tracker），种子自带的 tracker 不动。返回 tracker 个数，失败返回 0。
    aria2 只在启动时读一次 bt-tracker，几个月不重启列表就冻住了，所以要定期推。"""
    client = client or Aria2Client()
    trackers = _load_trackers()
    try:
        client.change_global_option({"bt-tracker": trackers})
    except Exception as e:
        config.log(f"更新 aria2 全局 tracker 失败：{e}")
        return 0
    updated = 0
    try:
        active = client.tell_active(["gid"]) or []
        waiting = client.tell_waiting(["gid"]) or []
    except Exception:
        active, waiting = [], []
    for st in list(active) + list(waiting):
        gid = st.get("gid")
        if not gid:
            continue
        try:
            client.change_option(gid, {"bt-tracker": trackers})
            updated += 1
        except Exception:
            pass
    n = len([t for t in trackers.split(",") if t.strip()])
    config.log(f"tracker 列表已刷新：{n} 个（全局 + {updated} 个进行中的下载）")
    return n


def ensure_daemon(client=None):
    """保证 aria2c 在跑：RPC 通就直接用；不通就拉起 ./bin/aria2c，最多等 15 秒。"""
    global _daemon_proc
    client = client or Aria2Client()
    if client.ping():
        return client
    binary = config.ARIA2_BIN
    if not (os.path.isfile(binary) and os.access(binary, os.X_OK)):
        raise Aria2Error(f"找不到 aria2c：{binary}，先跑 ./setup.sh 下静态二进制")
    os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)
    args = [
        binary,
        "--enable-rpc",
        "--rpc-listen-port", str(config.ARIA2_RPC_PORT),
        "--rpc-listen-all=false",
        f"--rpc-secret={client.secret}",
        "--dir", config.DOWNLOAD_DIR,
        "--max-concurrent-downloads=5",
        "--split=16",
        "--max-connection-per-server=16",
        "--min-split-size=1M",
        "--continue=true",
        "--auto-file-renaming=false",
        "--allow-overwrite=true",
        "--file-allocation=none",
        "--disk-cache=64M",
        # 下完就停，不做种。
        "--seed-time=0",
        # BT 提速：多连 peer，速度低时一直找更多 peer。
        "--bt-max-peers=100",
        "--bt-request-peer-speed-limit=1M",
        "--check-certificate=true",
        "--max-overall-download-limit=0",
        "--daemon=false",
        "--enable-dht=true",
        f"--bt-tracker={_load_trackers()}",
    ] + _ca_certificate_arg()
    logf = open(_ARIA2_LOG, "ab")
    try:
        _daemon_proc = subprocess.Popen(
            args, stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT,
        )
    except Exception as e:
        logf.close()
        raise Aria2Error(f"启动 aria2c 失败：{e}")
    config.log(f"aria2c 已拉起（PID {_daemon_proc.pid}），等 RPC 就绪…")
    deadline = time.time() + 15
    while time.time() < deadline:
        if _daemon_proc.poll() is not None:
            raise Aria2Error(f"aria2c 刚启动就退出了（退出码 {_daemon_proc.returncode}），看 {_ARIA2_LOG}")
        if client.ping():
            config.log("aria2 RPC 已就绪")
            return client
        time.sleep(0.5)
    raise Aria2Error("aria2c 启动了但 RPC 一直没响应，看 aria2.log")


def stop_daemon():
    global _daemon_proc
    if _daemon_proc is not None and _daemon_proc.poll() is None:
        try:
            _daemon_proc.terminate()
            _daemon_proc.wait(timeout=5)
        except Exception:
            try:
                _daemon_proc.kill()
            except Exception:
                pass
    _daemon_proc = None
