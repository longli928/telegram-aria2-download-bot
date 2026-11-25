# 部署手册

## 部署前需要的值

- `BOT_TOKEN`：新 bot 的 token（@BotFather 新建一个 bot 拿）
- `API_ID` / `API_HASH`：Telegram 应用（my.telegram.org）
- `ALLOWED_USER_IDS`：你自己的 Telegram user id（@userinfobot 查）

## 步骤

1. 把项目传到服务器，解压到一个目录（下面用 `~/mirror-bot` 举例）：

   ```
   python3 -m zipfile -e telegram-download-bot.zip ~/ && mv ~/telegram-download-bot ~/mirror-bot
   ```

2. 跑 setup（建 .venv、装依赖、下 aria2 和 rclone 到 `mirror-bot/bin/`）：

   ```
   cd ~/mirror-bot && ./setup.sh
   ```

3. 写 `mirror-bot/.env`（只用这四个值，别的不用改）：

   ```
   BOT_TOKEN=<你的 bot token>
   API_ID=<应用的 api_id>
   API_HASH=<应用的 api_hash>
   ALLOWED_USER_IDS=<你的 Telegram user id>
   ```

   注意：`API_ID` / `API_HASH` / `BOT_TOKEN` 不要写进任何会公开的文件。

4. 启动 bot：

   ```
   cd ~/mirror-bot && nohup .venv/bin/python app.py >> bot.log 2>&1 &
   ```

   没建出 venv 就用 `python3 app.py`。等 1 分钟，查日志：

   ```
   tail -30 ~/mirror-bot/bot.log
   ```

   看到 `Bot 已上线：@<bot 用户名>` 就是跑起来了。
   pidfile 会处理残留（旧进程已死可覆盖），重启不用手动删。

   想让 bot 开机自启，用 systemd 写个 service，或者用你容器的启动文件拉起它
   （注意别把 bot 的 app.py 直接当启动文件：那样控制台会变成 bot 日志，没法再输命令）。

5. 给 bot 发 `/start`，再发 `/storage` 加网盘（OAuth 流程：点登录链接登录，
   登录后把跳回来的 localhost 链接发回 bot），或把已有的 rclone.conf 拷过来
   再点“已在 rclone 配好（导入）”。

## 验证

- 发一个小文件链接给 bot：应看到下载进度，下完自动传到网盘，
  传完本地 `mirror-bot/downloads/` 里清空。
- `/status`、`/cancel` 能正常用。

## 排错

- bot 没上线：先看 `bot.log` 末尾；再看 `ps aux | grep "mirror-bot"`。
- `setup.sh` 说建不了 venv：它会自动退到 `pip --break-system-packages`，
  再不行按它打印的手动安装提示走。
