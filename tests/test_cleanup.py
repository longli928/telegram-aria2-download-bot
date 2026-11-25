#!/usr/bin/env python3
"""清理功能测试：/cleanup 的孤儿列表、删除、全部清理，以及根因修复
（任务 id 持久化、加不进 aria2 时不删本地目录）。

跑法：/tmp/aria2bot-test-venv/bin/python tests/test_cleanup.py
（需要 telethon，只用来 import tasks/app 的 Button；不连 Telegram、不动真服务器）

覆盖：
  1. list_orphans：bot 在管的 gid 不算孤儿；活动/等待/已停的孤儿都列出来；
     status=removed 的不算。
  2. cleanup_orphan：从 aria2 里删掉（force），连下载目录一起删；
     目录在下载目录外面时只删 aria2 里的，不动本地。
  3. cleanup_all_orphans：全部清掉，返回已清/失败。
  4. next_job_id 持久化：新建的 MirrorManager 继续往上编号，不从 1 重来。
  5. 加进 aria2 失败（InfoHash 已注册）时：消息提示用 /cleanup，不给重试按钮，
     且不删本地目录（里面可能是另一个任务正在下的文件）。
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["API_ID"] = "12345"
os.environ["API_HASH"] = "fakehash"
os.environ["BOT_TOKEN"] = "123456:fake-token"
os.environ["ALLOWED_USER_IDS"] = "123456789"

import config
import tasks
from aria2rpc import Aria2Error

TMP = tempfile.mkdtemp(prefix="aria2bot-cleanup-")
config.DOWNLOAD_DIR = os.path.join(TMP, "downloads")
os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)
tasks._SEQ_FILE = os.path.join(TMP, "job_seq.json")

PASSED = []


def ok(name):
    PASSED.append(name)
    print(f"  PASS {name}")


class FakeMsg:
    def __init__(self):
        self.edits = []

    async def edit(self, text, buttons=None):
        self.edits.append({"text": text, "buttons": buttons})


class FakeAria2:
    """假 aria2：按测试给定的表返回。"""

    def __init__(self, active=None, waiting=None, stopped=None, status=None, options=None):
        self.active = active or []
        self.waiting = waiting or []
        self.stopped = stopped or []
        self.status = status or {}
        self.options = options or {}
        self.removed = []
        self.removed_results = []

    def tell_active(self, keys=None):
        return self.active

    def tell_waiting(self, keys=None):
        return self.waiting

    def tell_stopped(self, keys=None):
        return self.stopped

    def tell_status(self, gid, keys=None):
        if gid not in self.status:
            raise Aria2Error("aria2 报错：GID not found")
        return self.status[gid]

    def get_option(self, gid):
        return self.options.get(gid, {})

    def remove(self, gid, force=False):
        self.removed.append((gid, force))
        return gid

    def remove_download_result(self, gid):
        self.removed_results.append(gid)
        return gid

    def add_uri(self, uris, options=None):
        raise Aria2Error("aria2 报错：InfoHash 75241ac1babb5501c166dcd324eff9485e738df3 is already registered.")


def torrent_entry(gid, status, name, total, done, speed=0):
    return {
        "gid": gid,
        "status": status,
        "totalLength": str(total),
        "completedLength": str(done),
        "downloadSpeed": str(speed),
        "uploadSpeed": "0",
        "bittorrent": {"info": {"name": name}},
        "files": [{"path": f"/x/{name}/file.bin", "length": str(total), "completedLength": str(done)}],
    }


async def test_list_orphans():
    tracked_gid = "aaaa1111aaaa1111"
    fake = FakeAria2(
        active=[
            torrent_entry(tracked_gid, "active", "tracked.torrent", 1000, 500, 100),
            torrent_entry("cccc3333cccc3333", "active", "orphan-active", 34_222_061_914, 83_738_624, 86_526),
        ],
        waiting=[torrent_entry("dddd4444dddd4444", "waiting", "orphan-waiting", 2000, 0)],
        stopped=[
            torrent_entry("eeee5555eeee5555", "complete", "orphan-done", 3000, 3000),
            torrent_entry("ffff6666ffff6666", "removed", "orphan-removed", 3000, 3000),
            torrent_entry("7777777777777777", "error", "orphan-error", 4000, 100),
        ],
    )
    mgr = tasks.MirrorManager(fake)
    mgr.jobs[1] = {"gid": tracked_gid}
    orphans = await mgr.list_orphans()
    gids = [o["gid"] for o in orphans]
    assert gids == ["cccc3333cccc3333", "dddd4444dddd4444", "eeee5555eeee5555", "7777777777777777"], gids
    assert orphans[0]["name"] == "orphan-active" and orphans[0]["done"] == 83_738_624
    assert orphans[1]["status"] == "waiting"
    assert orphans[2]["status"] == "complete"
    assert orphans[3]["status"] == "error"
    ok("list_orphans：只列 bot 不管的，removed 不算")


async def test_cleanup_orphan():
    gid = "cccc3333cccc3333"
    job_dir = os.path.join(config.DOWNLOAD_DIR, "3")
    os.makedirs(os.path.join(job_dir, "orphan-active"), exist_ok=True)
    with open(os.path.join(job_dir, "orphan-active", "file.bin"), "w") as f:
        f.write("x" * 100)
    entry = torrent_entry(gid, "active", "orphan-active", 34_222_061_914, 83_738_624, 86_526)
    fake = FakeAria2(status={gid: entry}, options={gid: {"dir": job_dir}})
    mgr = tasks.MirrorManager(fake)
    info = await mgr.cleanup_orphan(gid)
    assert info["name"] == "orphan-active" and info["size"] == 34_222_061_914, info
    assert fake.removed == [(gid, True)], fake.removed
    assert not os.path.exists(job_dir), "下载目录应该一起删掉"
    ok("cleanup_orphan：aria2 里删掉 + 本地目录一起删")


async def test_cleanup_orphan_outside_dir():
    gid = "dddd4444dddd4444"
    outside = os.path.join(TMP, "outside")
    os.makedirs(outside, exist_ok=True)
    with open(os.path.join(outside, "keep.txt"), "w") as f:
        f.write("keep")
    entry = torrent_entry(gid, "waiting", "orphan-waiting", 2000, 0)
    fake = FakeAria2(status={gid: entry}, options={gid: {"dir": outside}})
    mgr = tasks.MirrorManager(fake)
    await mgr.cleanup_orphan(gid)
    assert fake.removed == [(gid, True)]
    assert os.path.exists(os.path.join(outside, "keep.txt")), "下载目录外面的文件不能动"
    ok("cleanup_orphan：目录在外面时只删 aria2 里的，不动本地")


async def test_cleanup_orphan_stopped():
    # 已停的下载（complete/error）不能用 forceRemove，会报 aria2 RPC HTTP 400，
    # 得用 removeDownloadResult。这是截图里三个 [METADATA] 孤儿清不掉的原因。
    for status, gid, name in (
        ("complete", "aaaa1111aaaa1111", "[METADATA]75241ac1"),
        ("error", "bbbb2222bbbb2222", "[METADATA]75241ac1"),
    ):
        job_dir = os.path.join(config.DOWNLOAD_DIR, "7")
        os.makedirs(job_dir, exist_ok=True)
        with open(os.path.join(job_dir, "meta.torrent"), "w") as f:
            f.write("x")
        entry = torrent_entry(gid, status, name, 100_000, 100_000 if status == "complete" else 0)
        fake = FakeAria2(status={gid: entry}, options={gid: {"dir": job_dir}})
        mgr = tasks.MirrorManager(fake)
        info = await mgr.cleanup_orphan(gid)
        assert info["name"] == name
        assert fake.removed == [], f"{status} 不能用 forceRemove，实际调用了 {fake.removed}"
        assert fake.removed_results == [gid], fake.removed_results
        assert not os.path.exists(job_dir), "本地目录也要一起删"
    ok("cleanup_orphan（已停）：用 removeDownloadResult，不报 HTTP 400")


async def test_load_trackers_merges_all_sources():
    import aria2rpc
    import urllib.request
    import io

    # 两个源各给一份，有 1 个重叠的 tracker；第 3 个源直接报错
    bodies = {
        "https://raw.githubusercontent.com/ngosang/trackerslist/master/trackers_best.txt":
            "udp://a.example:1337/announce\nudp://shared.example:1337/announce\n",
        "https://cdn.jsdelivr.net/gh/ngosang/trackerslist@master/trackers_best.txt":
            "udp://b.example:1337/announce\n",
        "https://raw.githubusercontent.com/adysec/tracker/main/trackers_best.txt":
            "udp://shared.example:1337/announce\nudp://c.example:1337/announce\nudp://d.example:1337/announce\n",
    }

    class FakeResp:
        def __init__(self, text):
            self._buf = io.BytesIO(text.encode())
        def read(self):
            return self._buf.read()
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if url not in bodies:
            raise RuntimeError("boom")
        return FakeResp(bodies[url])

    real = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen
    try:
        result = aria2rpc._load_trackers()
    finally:
        urllib.request.urlopen = real
    parts = [p for p in result.split(",") if p.strip()]
    assert parts == [
        "udp://a.example:1337/announce",
        "udp://shared.example:1337/announce",
        "udp://b.example:1337/announce",
        "udp://c.example:1337/announce",
        "udp://d.example:1337/announce",
    ], parts
    ok("tracker 加载：ngosang + adysec 两家合并去重，坏源跳过不影响")


async def test_apply_tuning():
    import aria2rpc

    class FakeClient:
        def __init__(self):
            self.global_opts = None
            self.per_gid = {}
        def change_global_option(self, opts):
            self.global_opts = dict(opts)
        def change_option(self, gid, opts):
            self.per_gid[gid] = dict(opts)
        def tell_active(self, keys=None):
            return [{"gid": "aaaa1111aaaa1111"}]
        def tell_waiting(self, keys=None):
            return []

    fake = FakeClient()
    n = aria2rpc.apply_tuning(fake)
    assert n == 1, n
    assert fake.global_opts == {"bt-max-peers": "100", "bt-request-peer-speed-limit": "1M"}, fake.global_opts
    assert fake.per_gid == {"aaaa1111aaaa1111": {"bt-max-peers": "100", "bt-request-peer-speed-limit": "1M"}}, fake.per_gid
    # 启动参数里也要有这两个，不然 aria2 重启就丢了
    import inspect
    src = inspect.getsource(aria2rpc.ensure_daemon)
    assert "--bt-max-peers=100" in src and "--bt-request-peer-speed-limit=1M" in src, src
    ok("apply_tuning：不重启 aria2，全局 + 进行中的下载都换成提速参数")


async def test_poll_edits_while_pct_stuck():
    # 31.9 GB 以 86 KB/s 下：1% 是 319 MB，要 60 多分钟才涨 1%，旧逻辑（只看百分比）
    # 会让界面一直 0% 不动。新逻辑：百分比不动时也每 5 秒刷一次。
    import tasks as tasks_mod
    from tasks import MirrorManager
    import asyncio as _aio
    total = 34222061914
    calls = {"n": 0}
    edits = []
    clock = {"t": 1000000.0}

    def fake_time():
        return clock["t"]

    class FakeAria:
        def tell_status(self, gid, keys=None):
            calls["n"] += 1
            done = 83738624 + calls["n"] * 100000  # 每轮涨 100 KB
            if calls["n"] >= 4:
                return {"status": "complete", "totalLength": str(total),
                        "completedLength": str(total), "downloadSpeed": "0",
                        "followedBy": [], "bittorrent": {"info": {"name": "x"}}}
            return {"status": "active", "totalLength": str(total),
                    "completedLength": str(done), "downloadSpeed": "86526",
                    "numSeeders": "3", "connections": "44",
                    "bittorrent": {"info": {"name": "x"}}}

    class FakeMsg:
        async def edit(self, text, buttons=None):
            edits.append(text)

    mgr = MirrorManager.__new__(MirrorManager)
    mgr.aria2 = FakeAria()
    job = {"job_id": 1, "gid": "aaaa1111aaaa1111", "cancelled": False,
           "msg": FakeMsg(), "url": "magnet:?xt=urn:btih:00", "torrent_b64": None,
           "name": "x", "done": 0, "total": 0, "speed": 0}
    real_time = tasks_mod.time.time
    real_sleep = _aio.sleep

    async def fast_sleep(delay):
        clock["t"] += 6  # 每轮推进 6 秒，触发 5 秒刷新
        return None

    tasks_mod.time.time = fake_time
    _aio.sleep = fast_sleep
    try:
        await mgr._poll_download(job)
    finally:
        tasks_mod.time.time = real_time
        _aio.sleep = real_sleep
    # 百分比一直是 0，但消息被改了多次，且已下 MB 在涨
    assert len(edits) >= 3, [e.split("\n")[1:3] for e in edits]
    assert all("0%" in e for e in edits), edits
    assert "79.9 MB" in edits[0] or "80.0 MB" in edits[0], edits[0]
    ok("下载进度：百分比不动时也每 5 秒刷一次，不会一直 0% 卡住")


async def test_progress_elapsed():
    from tasks import _progress_text, MirrorManager
    import time
    t0 = time.time() - 754  # 12 分 34 秒前开始
    text = _progress_text("⬇️ 下载中", 83738624, 34222061914, t0, seeders=3, connections=44)
    assert "⏱ 已耗时 00:12:34" in text, text
    # 两栏布局：大小和速度一行，已耗时和预计还剩一行；进度条 17 格
    assert "📦 79.9 MB / 31.9 GB      🚀 速度" in text, text
    assert "⏱ 已耗时 00:12:34      ⏳ 预计还剩" in text, text
    bar_line = text.split("\n")[1]
    assert bar_line.count("█") + bar_line.count("░") == 17, bar_line
    assert "🌱 做种 3　🔗 连接 44" in text, text
    # /status 也带已耗时：job 里要有 started
    mgr = MirrorManager.__new__(MirrorManager)
    mgr.jobs = {1: {"job_id": 1, "name": "x", "state": "downloading",
                    "done": 83738624, "total": 34222061914, "speed": 86526,
                    "started": time.time() - 754}}
    mgr.pending_dst = {}
    st = mgr.status_text()
    assert "⏱ 已耗时 00:12:34" in st, st
    ok("进度条和 /status 都带 ⏱ 已耗时")


async def test_refresh_trackers():
    import aria2rpc
    fresh = "udp://tracker.a:1337/announce,udp://tracker.b:6969/announce"

    class FakeClient:
        def __init__(self):
            self.global_opts = None
            self.per_gid = {}
        def change_global_option(self, opts):
            self.global_opts = dict(opts)
        def change_option(self, gid, opts):
            self.per_gid[gid] = dict(opts)
        def tell_active(self, keys=None):
            return [{"gid": "aaaa1111aaaa1111"}, {"gid": "bbbb2222bbbb2222"}]
        def tell_waiting(self, keys=None):
            return [{"gid": "cccc3333cccc3333"}]

    old = aria2rpc._load_trackers
    aria2rpc._load_trackers = lambda: fresh
    try:
        fake = FakeClient()
        n = aria2rpc.refresh_trackers(fake)
        assert n == 2, n
        assert fake.global_opts == {"bt-tracker": fresh}, fake.global_opts
        assert fake.per_gid == {
            "aaaa1111aaaa1111": {"bt-tracker": fresh},
            "bbbb2222bbbb2222": {"bt-tracker": fresh},
            "cccc3333cccc3333": {"bt-tracker": fresh},
        }, fake.per_gid
    finally:
        aria2rpc._load_trackers = old
    ok("refresh_trackers：不重启 aria2，全局 + 进行中的下载都换成新 tracker")


async def test_fmt_size():
    from tasks import fmt_size, _progress_text
    import time
    cases = [(0, "0 B"), (500, "500 B"), (1024, "1.0 KB"),
             (86526, "84.5 KB"), (56623104, "54.0 MB"),
             (6614958080, "6.2 GB"), (22102822591, "20.6 GB"),
             (34222061914, "31.9 GB"), (1099511627776, "1.0 TB")]
    for n, want in cases:
        assert fmt_size(n) == want, (n, fmt_size(n), want)
    text = _progress_text("⬇️ 下载中", 83738624, 34222061914, time.time() - 10,
                          seeders=3, connections=44)
    assert "79.9 MB / 31.9 GB" in text and "MB/s" in text, text
    assert "MB /" not in text.split("\n")[2].replace("79.9 MB", ""), text
    ok("fmt_size：自适应 B/KB/MB/GB/TB，进度条不再固定 MB")


async def test_cleanup_pattern():
    # 回调 pattern：all 必须放前面再加 $ 锚定，不然 "all" 会被 [0-9a-f]+
    # 先匹配到 "a" 当成 gid，去删一个不存在的下载，报“不在 aria2 里了”。
    import re
    pat = re.compile(rb"cleanup:(all|[0-9a-f]+)$")
    assert pat.match(b"cleanup:all").group(1) == b"all"
    assert pat.match(b"cleanup:84a1d16c56d7153b").group(1) == b"84a1d16c56d7153b"
    assert pat.match(b"cleanup:eae4a166fb212774").group(1) == b"eae4a166fb212774"
    # 旧的写法会把 all 认成 gid "a"：
    old = re.compile(rb"cleanup:([0-9a-f]+|all)")
    assert old.match(b"cleanup:all").group(1) == b"a"
    ok("cleanup 回调 pattern：all 不会被认成 gid")


async def test_cleanup_all():
    fake = FakeAria2(
        active=[torrent_entry("a1a1a1a1a1a1a1a1", "active", "o1", 100, 50)],
        waiting=[torrent_entry("b2b2b2b2b2b2b2b2", "waiting", "o2", 200, 0)],
        stopped=[],
        status={
            "a1a1a1a1a1a1a1a1": torrent_entry("a1a1a1a1a1a1a1a1", "active", "o1", 100, 50),
            "b2b2b2b2b2b2b2b2": torrent_entry("b2b2b2b2b2b2b2b2", "waiting", "o2", 200, 0),
        },
        options={
            "a1a1a1a1a1a1a1a1": {"dir": os.path.join(config.DOWNLOAD_DIR, "9")},
            "b2b2b2b2b2b2b2b2": {"dir": os.path.join(config.DOWNLOAD_DIR, "10")},
        },
    )
    mgr = tasks.MirrorManager(fake)
    removed, failed = await mgr.cleanup_all_orphans()
    assert [r["name"] for r in removed] == ["o1", "o2"], removed
    assert failed == [], failed
    ok("cleanup_all_orphans：全部清掉")


async def test_job_id_persists():
    fake = FakeAria2()
    mgr1 = tasks.MirrorManager(fake)
    assert mgr1.next_job_id() == 1
    assert mgr1.next_job_id() == 2
    mgr2 = tasks.MirrorManager(fake)
    assert mgr2.next_job_id() == 3, "重启后应该继续编号，不从 1 重来"
    ok("next_job_id 持久化：重启后不从 1 重来")


async def test_add_failure_keeps_local():
    # 另一个任务正在下的目录（job_id 复用时会撞上这个目录）。
    other_dir = os.path.join(config.DOWNLOAD_DIR, "1")
    os.makedirs(other_dir, exist_ok=True)
    sentinel = os.path.join(other_dir, "partial.bin")
    with open(sentinel, "w") as f:
        f.write("partial data")
    fake = FakeAria2()
    mgr = tasks.MirrorManager(fake)
    msg = FakeMsg()
    job = {
        "job_id": 1,
        "user_id": 123456789,
        "chat_id": 123456789,
        "msg": msg,
        "url": "magnet:?xt=urn:btih:75241AC1BABB5501C166DCD324EFF9485E738DF3",
        "torrent_b64": None,
        "gid": None,
        "state": "downloading",
        "name": "magnet:?xt=urn:btih:75241AC1",
        "done": 0,
        "total": 0,
        "speed": 0,
        "local_dir": other_dir,
        "local": None,
        "size": 0,
        "cancelled": False,
        "task": None,
        "tz": None,
        "tz_name": "UTC",
    }
    result = await mgr._run_job_impl(job)
    assert result is False
    assert os.path.exists(sentinel), "加不进 aria2 时不能删本地目录"
    assert msg.edits, "应该给用户回一条失败消息"
    text = msg.edits[-1]["text"]
    assert "/cleanup" in text, text
    assert msg.edits[-1]["buttons"] is None, "已注册失败不给重试按钮（重试也没用）"
    assert 1 in mgr.failed, "失败信息要留着（但本地文件不能删）"
    ok("加进 aria2 失败：提示 /cleanup、不给重试、不删本地目录")


async def main():
    print("清理功能测试")
    await test_list_orphans()
    await test_cleanup_orphan()
    await test_cleanup_orphan_outside_dir()
    await test_cleanup_orphan_stopped()
    await test_load_trackers_merges_all_sources()
    await test_apply_tuning()
    await test_poll_edits_while_pct_stuck()
    await test_progress_elapsed()
    await test_refresh_trackers()
    await test_fmt_size()
    await test_cleanup_pattern()
    await test_cleanup_all()
    await test_job_id_persists()
    await test_add_failure_keeps_local()
    print(f"\n{PASSED.__len__()} / 14 通过")


if __name__ == "__main__":
    asyncio.run(main())
