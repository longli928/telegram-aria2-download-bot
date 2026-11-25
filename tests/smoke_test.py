#!/usr/bin/env python3
"""冒烟测试：不连 Telegram、不动真服务器，验证新 bot 的核心链路。

跑法：/tmp/aria2bot-venv/bin/python tests/smoke_test.py
需要本机有 ./bin/aria2c 和 ./bin/rclone 吗？——不需要，测试用 /tmp/setup-test 里的
真实静态二进制（setup.sh 下下来的那两个），以及一个 rclone alias 远端当“网盘”。

覆盖：
  1. 全部模块能 import，配置能解析（假 token，不连网）
  2. 链接识别：直链 / magnet / .torrent 链接 / 去重
  3. 日志脱敏、rclone 配置解析、存储注册表（默认/唯一/多个/没有）
  4. providers.json：71 个后端，OAuth 判断、手写字段
  5. aria2 RPC 客户端：对假 RPC 服务器发带 token 的请求、处理报错
  6. aria2 守护进程：真实 aria2c 拉起 + ping + 下一个真实小文件
  7. 全链路：MirrorManager._run_job_impl —— aria2 下 -> rclone move 传 alias 远端
     -> 查落盘信息 -> 本地删干净（FakeMsg 模拟 Telegram 消息）
"""
import asyncio
import base64
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

# ---- 1. 先设假环境变量，再 import 模块 ----
TMP = tempfile.mkdtemp(prefix="aria2bot-smoke-")
os.environ["API_ID"] = "12345"
os.environ["API_HASH"] = "fakehash"
os.environ["BOT_TOKEN"] = "123456:fake-token"
os.environ["ALLOWED_USER_IDS"] = "111,222"
os.environ["DOWNLOAD_DIR"] = os.path.join(TMP, "downloads")
os.environ["RCLONE_BIN"] = "/tmp/setup-test/bin/rclone"
os.environ["ARIA2_BIN"] = "/tmp/setup-test/bin/aria2c"
os.environ["RCLONE_CONFIG"] = os.path.join(TMP, "rclone.conf")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import aria2rpc
import rclone as rclone_mod
import storage as storage_mod
import tasks as tasks_mod
import tzutil
import app  # noqa: F401  (只验证能 import，不跑 main)

PASSED = []
FAILED = []


def check(name, cond, extra=""):
    if cond:
        PASSED.append(name)
        print(f"  PASS {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL {name} {extra}")


async def main():
    print(f"临时目录：{TMP}")
    print("== 1. import 与配置 ==")
    check("config.API_ID 解析", config.API_ID == 12345)
    check("config.ALLOWED 白名单", config.ALLOWED == {111, 222})
    check("config.DOWNLOAD_DIR", config.DOWNLOAD_DIR == os.path.join(TMP, "downloads"))
    check("config.RCLONE_BIN", config.RCLONE_BIN == "/tmp/setup-test/bin/rclone")
    check("tzutil 解析 UTC-8", tzutil.parse_tz_name("-8")[1] == "UTC-8")
    check("tzutil 解析 IANA", tzutil.parse_tz_name("Europe/London")[1] == "Europe/London")
    check("tzutil 拒绝 99", tzutil.parse_tz_name("99") is None)

    print("== 2. 链接识别 ==")
    urls = tasks_mod.extract_urls("下这个 https://example.com/file.zip 和 magnet:?xt=urn:btih:ABC123 还有 https://x.com/a.torrent")
    check("抠出 3 个链接", len(urls) == 3, str(urls))
    check("magnet 识别", tasks_mod.is_magnet("magnet:?xt=urn:btih:ABC"))
    check("直链不是 magnet", not tasks_mod.is_magnet("https://example.com/x"))
    check("去重", len(tasks_mod.extract_urls("https://a.com/1 https://a.com/1")) == 1)
    check("没链接返回空", tasks_mod.extract_urls("你好") == [])

    print("== 3. 脱敏与 rclone 配置解析 ==")
    long_token = "x" * 100
    check("长 token 被抹掉", "[REDACTED]" in config._redact(f"token={long_token}"))
    check("短文本不动", config._redact("hello") == "hello")
    with open(os.environ["RCLONE_CONFIG"], "w", encoding="utf-8") as f:
        f.write("[dst]\ntype = alias\nremote = " + os.path.join(TMP, "dst") + "\n\n[od]\ntype = onedrive\ntoken = " + "y" * 90 + "\n")
    os.chmod(os.environ["RCLONE_CONFIG"], 0o600)
    sections = rclone_mod.conf_sections()
    check("读出两个 remote", set(sections) == {"dst", "od"}, str(sections.keys()))
    check("remote 类型", rclone_mod.remote_type("od") == "onedrive")
    check("rclone 二进制找到", os.path.isfile(rclone_mod.rclone_bin()))

    print("== 4. 存储注册表 ==")
    storage_mod._STORAGES_FILE = os.path.join(TMP, "storages.json")
    storage_mod.load_storages()
    check("全新部署注册表是空的", storage_mod.storage_list() == [])
    storage_mod._storages = {"default": None, "storages": {
        "dst": {"remote": "dst", "folder": "mirror", "kind": "alias", "label": "Alias"},
    }}
    storage_mod.save_storages()
    check("storages.json chmod 600", (os.stat(os.path.join(TMP, "storages.json")).st_mode & 0o777) == 0o600)
    s, need = storage_mod.resolve_storage()
    check("唯一存储不用问", s["remote"] == "dst" and need is False)
    storage_mod._storages["storages"]["od"] = {"remote": "od", "folder": "", "kind": "onedrive", "label": "OneDrive"}
    s, need = storage_mod.resolve_storage()
    check("多个没默认要问", s is None and need is True)
    storage_mod._storages["default"] = "dst"
    s, need = storage_mod.resolve_storage()
    check("有默认用默认", s["remote"] == "dst" and need is False)
    check("显示名优先 remote", storage_mod.storage_display({"remote": "od", "label": "OneDrive"}) == "od（OneDrive）")

    print("== 5. providers.json ==")
    storage_mod.load_providers()
    check("后端数量 >= 70", len(storage_mod._PROVIDERS) >= 70, str(len(storage_mod._PROVIDERS)))
    check("onedrive 是 OAuth", storage_mod.is_oauth("onedrive") is True)
    check("s3 不是 OAuth", storage_mod.is_oauth("s3") is False)
    check("webdav 有手写字段", len(storage_mod.manual_fields("webdav")) == 3)
    check("未知后端字段为空", storage_mod.manual_fields("nope") == [])

    print("== 6. aria2 RPC 客户端（假服务器） ==")
    seen = {}

    class FakeRPC(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.update(body)
            method = body["method"]
            if method == "aria2.getVersion":
                result = {"version": "1.37.0", "enabledFeatures": []}
            elif method == "aria2.addUri":
                result = "2089b05ecca3d829"
            else:
                result = {}
            payload = json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), FakeRPC)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    fake_client = aria2rpc.Aria2Client(secret="testsecret", url=f"http://127.0.0.1:{srv.server_address[1]}/jsonrpc")
    v = fake_client._call("aria2.getVersion")
    check("假 RPC 拿到版本号", v["version"] == "1.37.0")
    check("请求带了 token", seen["params"][0] == "token:testsecret", str(seen.get("params")))
    gid = fake_client.add_uri(["https://example.com/x.zip"], {"dir": "/tmp"})
    check("addUri 返回 gid", gid == "2089b05ecca3d829")
    srv.shutdown()

    print("== 7. 真实 aria2 守护进程 + 全链路 ==")
    aria2rpc._SECRET_FILE = os.path.join(TMP, "aria2-secret.txt")
    config.DOWNLOAD_DIR = os.path.join(TMP, "downloads")
    real_client = aria2rpc.ensure_daemon()
    check("ensure_daemon 后 RPC 通", real_client.ping())
    check("secret 文件 chmod 600", (os.stat(os.path.join(TMP, "aria2-secret.txt")).st_mode & 0o777) == 0o600)
    again = aria2rpc.ensure_daemon()
    check("已在跑时 ensure_daemon 直接复用", again.ping())

    # 全链路：下一个真实小文件 -> rclone move 到 alias 远端 -> 本地删干净
    os.makedirs(os.path.join(TMP, "dst"), exist_ok=True)
    mgr = tasks_mod.MirrorManager(real_client)

    class FakeMsg:
        def __init__(self):
            self.texts = []

        async def edit(self, text=None, buttons=None):
            self.texts.append(text or "")
            return self

    event = SimpleNamespace(chat_id=999)
    msg = FakeMsg()
    tz, tz_name = tzutil.get_user_tz(111)
    job_id = await mgr.start_mirror(event, msg, "https://example.com/", 111, tz, tz_name)
    task = mgr.jobs[job_id]["task"]
    result = await asyncio.wait_for(task, timeout=120)
    check("全链路返回 True", result is True, str(result))
    check("状态消息最后是上传成功", any("已上传到" in t for t in msg.texts), msg.texts[-1][:120] if msg.texts else "")
    dst_files = []
    for root, _dirs, files in os.walk(os.path.join(TMP, "dst")):
        dst_files += files
    check("文件已到 alias 远端", len(dst_files) >= 1, str(dst_files))
    check("本地任务目录已删干净", not os.path.exists(os.path.join(TMP, "downloads", str(job_id))))
    check("失败表里没有这个任务", job_id not in mgr.failed)

    aria2rpc.stop_daemon()
    print()
    print(f"通过 {len(PASSED)}，失败 {len(FAILED)}")
    if FAILED:
        print("失败项：", FAILED)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
