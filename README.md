[English](README_EN.md)

![icon](icon.png)

# Telegram 下载 Bot（aria2 + rclone）

发链接给 bot → aria2 下载 → rclone 传到网盘 → 传完删本地文件。

## 功能

- `/mirror <链接>`：下载直链、magnet、`.torrent` 链接；直接发链接也行；发 `.torrent` 文件也行。一条消息多个链接会各建一个任务。
- 下载进度实时更新（百分比、速度、ETA、已耗时）；进度每 5 秒刷新。
- `/status`：查看进行中的任务；`/cancel`：取消任务（同步清理本地没下完的文件）。
- `/cleanup`：清理 aria2 里的孤儿下载（bot 重启后丢了记录、aria2 还在下的任务）。
- `/storage`：添加网盘、选默认存储、改保存位置、删除网盘。OAuth 网盘（OneDrive / Google Drive / Dropbox 等）点登录链接登录，登录后把跳回来的 localhost 链接发回 bot；其它网盘按提示填配置。也可以把已经配好的 rclone.conf 拷过来，点“已在 rclone 配好（导入）”。
- 默认存储：设了就直接往它传；没设又有多个网盘时，下载完成会问你传哪个。
- 上传失败本地文件留着，消息带重试按钮；下载失败也能重试。
- `/timezone`：设置上传时间的显示时区。
- 白名单：只有 `ALLOWED_USER_IDS` 里的用户能用。
- 磁力链接：正确跟进 aria2 的元数据任务（followedBy）；下完自动停做种（`--seed-time=0`）并触发上传。
- tracker 自动更新：每天从 ngosang/trackerslist 和 adysec/tracker 两家拉最新的 best 列表，合并去重后推给正在跑的 aria2，不用重启。

## 部署

```bash
./setup.sh            # 下 aria2、rclone 静态二进制到 ./bin/，建 .venv 装依赖
cp .env.example .env  # 填 API_ID / API_HASH / BOT_TOKEN / ALLOWED_USER_IDS
.venv/bin/python app.py
```

bot 自己会拉起 aria2c（RPC 只监听 127.0.0.1:6800）；aria2 的 secret 不填会自动生成，存进 `aria2-secret.txt`（chmod 600）。详见 [DEPLOY.md](DEPLOY.md)。

## 文件说明

| 文件 | 作用 |
|---|---|
| `app.py` | 入口：Telethon 启动、白名单、命令、消息与回调处理 |
| `tasks.py` | 下载任务：aria2 轮询进度、上传、重试、取消、清理本地 |
| `aria2rpc.py` | aria2 JSON-RPC 客户端 + 守护进程管理（自动拉起 aria2c） |
| `rclone.py` | rclone 封装：配置读写、remote 校验、目录操作、`rclone move` 上传 |
| `storage.py` | `/storage`：存储注册表、OAuth 授权、文件夹选择器、添加/改/删 |
| `auth.py` | 白名单检查（直接用 `sender_id`） |
| `config.py` | 环境变量配置 |
| `tzutil.py` | 用户时区 |
| `providers.json` | rclone 71 种后端注册表（`rclone config providers` 生成） |
| `setup.sh` | 下静态二进制、建 venv 装依赖 |
| `tests/` | 测试（见下） |

## 测试

```bash
.venv/bin/python tests/test_cleanup.py   # 单元测试
python tests/smoke_test.py              # 冒烟测试
```

冒烟测试不连 Telegram、不动真服务器：用假 RPC 服务器测 aria2 客户端，
用真实 aria2c + 真实 rclone（alias 远端当网盘）跑通整条链路
（下载 → move 上传 → 查落盘信息 → 本地删干净）。
