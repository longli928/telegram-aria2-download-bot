#!/usr/bin/env python3
"""rclone 封装：配置文件读写、remote 校验、目录操作、上传（move）。

- 所有 rclone 子进程都用 stdin=DEVNULL，免得哪个命令等交互输入卡住。
- 含 token 的命令和输出一律不记日志；报错记日志前先用 config._redact 抹掉。
- 上传用 `rclone move`：传完本地文件自动删，传失败本地文件留着，等重试。
- rclone 二进制：优先用 RCLONE_BIN（默认 ./bin/rclone），不存在就用 PATH 里的 rclone。
"""
import configparser
import json
import os
import re
import shutil
import subprocess

import config


def rclone_bin():
    """rclone 可执行文件：./bin/rclone 存在就用它，否则用 PATH 里的。"""
    if os.path.isfile(config.RCLONE_BIN) and os.access(config.RCLONE_BIN, os.X_OK):
        return config.RCLONE_BIN
    found = shutil.which("rclone")
    return found or config.RCLONE_BIN


def rclone_config_path():
    return config.RCLONE_CONFIG


def conf_sections():
    """读 rclone 配置文件，只返回 {remote: {key: value}}；值里可能有密钥，绝不能记日志。"""
    sections, cur = {}, None
    try:
        with open(rclone_config_path(), encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith(("#", ";")):
                    continue
                if line.startswith("[") and line.endswith("]"):
                    cur = line[1:-1].strip()
                    sections[cur] = {}
                elif "=" in line and cur:
                    k, v = line.split("=", 1)
                    sections[cur][k.strip()] = v.strip()
    except Exception:
        pass
    return sections


def remote_type(remote):
    return conf_sections().get(remote, {}).get("type", "")


def list_remotes():
    try:
        r = subprocess.run(
            [rclone_bin(), "listremotes"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            return []
        return [ln.strip().rstrip(":") for ln in r.stdout.splitlines() if ln.strip()]
    except Exception:
        return []


def run_config_create(name, type_, params):
    """调 rclone config create 写一个 remote。params 里可能有密钥：命令和输出一律不记日志。
    只给非 OAuth 后端用；OAuth 后端走 write_remote_direct，
    因为 config create 会重新跑一遍 OAuth、在那等 code 卡住。"""
    cmd = [rclone_bin(), "config", "create", name, type_]
    for k, v in params.items():
        if v is None or v == "":
            continue
        cmd += [k, str(v)]
    try:
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        # 注意：TimeoutExpired 的 str 里会带上完整命令（含 token），绝不能原样记日志。
        raise RuntimeError("rclone 写入配置超时（60 秒），这个后端可能在等交互输入")
    if r.returncode != 0:
        raise RuntimeError("rclone 写入配置失败，请检查填的内容")
    try:
        os.chmod(rclone_config_path(), 0o600)
    except Exception:
        pass


def write_remote_direct(name, type_, params, path=None):
    """直接把 remote 写进一个 rclone 配置文件（默认真实的 rclone.conf，不用 rclone config create）。
    原因见 run_config_create：OAuth 后端用 config create 会重新跑 OAuth 卡住。
    用 configparser 读改写，原子替换，权限保持 600。"""
    path = path or rclone_config_path()
    cp = configparser.RawConfigParser()
    cp.optionxform = str  # 保持键名大小写
    if os.path.exists(path):
        cp.read(path, encoding="utf-8")
    if cp.has_section(name):
        cp.remove_section(name)
    cp.add_section(name)
    cp.set(name, "type", type_)
    for k, v in params.items():
        if v:
            cp.set(name, k, v)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        cp.write(f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def list_remote_dirs(remote, path, config_args=None):
    """列出网盘上某个目录下的子目录（只要目录，按名字排好）。返回 [名字]，失败抛错。"""
    args = [rclone_bin(), "lsjson"] + list(config_args or [])
    target = f"{remote}:{path}" if path else f"{remote}:"
    r = subprocess.run(args + [target], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"列目录失败：{(r.stderr or '').strip()[-200:]}")
    try:
        items = json.loads(r.stdout or "[]")
    except Exception:
        items = []
    names = sorted(
        [d.get("Name", "") for d in items if isinstance(d, dict) and d.get("IsDir") and d.get("Name")],
        key=str.lower,
    )
    return names


def rclone_mkdir(remote, path, config_args=None):
    """在网盘上建一个文件夹（连父目录一起建）。失败抛错。"""
    args = [rclone_bin(), "mkdir"] + list(config_args or [])
    target = f"{remote}:{path}"
    r = subprocess.run(args + [target], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"建文件夹失败：{(r.stderr or '').strip()[-200:]}")


def verify_remote(remote):
    """新建的 remote 能不能列目录，能才算配好了。"""
    r = subprocess.run(
        [rclone_bin(), "lsd", f"{remote}:"],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60,
    )
    return r.returncode == 0


def delete_remote(remote):
    try:
        subprocess.run(
            [rclone_bin(), "config", "delete", remote],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30,
        )
    except Exception:
        pass


def _parse_rclone_bytes(s):
    m = re.search(r"Transferred:\s*([\d.]+)\s*([KMGT]?i?B)\s*/\s*([\d.]+)", s)
    if not m:
        return None
    units = {"B": 1, "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3,
             "KB": 1000, "MB": 1000 ** 2, "GB": 1000 ** 3}
    done = float(m.group(1)) * units.get(m.group(2), 1)
    return int(done)


def move_to_storage(local_path, display_name, storage, on_progress=None):
    """用 `rclone move` 把本地文件/目录搬到指定存储。

    - local_path 是文件：搬到 <remote>:<folder>/<display_name>。
    - local_path 是目录：整目录搬过去，远端目录名用 display_name（种子名），
      不要用本地目录名——任务目录名是任务编号（1、2…），重启后还会重用，
      传上去就成了 <文件夹>/1 这种看不懂的名字。
    - .aria2 控制文件不上传（--exclude）。"""
    size = 0
    if os.path.isfile(local_path):
        size = os.path.getsize(local_path)
    else:
        for root, _dirs, files in os.walk(local_path):
            for f in files:
                try:
                    size += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    folder = (storage.get("folder") or "").strip("/")
    if os.path.isdir(local_path):
        remote_path = f"{folder}/{display_name}" if folder else display_name
    else:
        remote_path = f"{folder}/{display_name}" if folder else display_name
    dest = f"{storage['remote']}:{remote_path}"
    cmd = [
        rclone_bin(), "move", local_path, dest,
        "--retries", "3",
        "--low-level-retries", "10",
        "--exclude", "*.aria2",
        "--stats", "2s",
        "--use-json-log",
        "--log-level", "INFO",
    ]
    if (storage.get("kind") or "") == "onedrive":
        cmd += ["--onedrive-chunk-size", "10M"]
    config.log(f"rclone 上传：{local_path} -> {dest}")
    proc = subprocess.Popen(
        cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    last_err = ""
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        last_err = line
        done = None
        try:
            j = json.loads(line)
            stats = j.get("stats") or {}
            if "bytes" in stats:
                done = int(stats["bytes"])
            elif j.get("msg"):
                done = _parse_rclone_bytes(j["msg"])
        except Exception:
            done = _parse_rclone_bytes(line)
        if done is not None and on_progress:
            on_progress(min(done, size), size)
    rc = proc.wait()
    if rc != 0:
        raise RuntimeError(f"rclone 上传失败，退出码 {rc}：{last_err[-300:]}")
    if on_progress:
        on_progress(size, size)
    config.log(f"rclone 上传完成：{dest}")
    return remote_path


def get_remote_file_info(remote, remote_path):
    """上传成功后用 rclone lsjson 查落盘文件信息：名字、大小、修改时间。
    目标是目录时 lsjson 返回的是内容列表，不能取第一条当文件——
    改用 rclone size 取整个目录的总大小，名字用目录名。"""
    target = f"{remote}:{remote_path.lstrip('/')}"
    r = subprocess.run(
        [rclone_bin(), "lsjson", target],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0:
        raise RuntimeError(f"rclone lsjson 失败：{r.stderr.strip()[-200:]}")
    d = json.loads(r.stdout)
    if isinstance(d, list):
        # 目录：取总大小；空目录大小为 0。
        total = 0
        try:
            r2 = subprocess.run(
                [rclone_bin(), "size", "--json", target],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60,
            )
            if r2.returncode == 0:
                total = int((json.loads(r2.stdout or "{}") or {}).get("bytes") or 0)
        except Exception:
            pass
        return {
            "name": os.path.basename(remote_path.rstrip("/")),
            "size": total,
            "webUrl": "",
            "created": "",
            "modified": "",
            "path": "/" + remote_path.lstrip("/"),
        }
    return {
        "name": d.get("Name", ""),
        "size": d.get("Size", 0),
        "webUrl": "",
        "created": "",
        "modified": d.get("ModTime", ""),
        "path": "/" + remote_path.lstrip("/"),
    }
