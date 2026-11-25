# Deployment Guide

## What you need before deploying

- `BOT_TOKEN`: your bot's token (create a bot with @BotFather)
- `API_ID` / `API_HASH`: your Telegram app (my.telegram.org)
- `ALLOWED_USER_IDS`: your own Telegram user id (check with @userinfobot)

## Steps

1. Upload the project to your server and extract it into a directory (using `~/mirror-bot` below as an example):

   ```
   python3 -m zipfile -e telegram-download-bot.zip ~/ && mv ~/telegram-download-bot ~/mirror-bot
   ```

2. Run setup (creates .venv, installs deps, downloads aria2 and rclone into `mirror-bot/bin/`):

   ```
   cd ~/mirror-bot && ./setup.sh
   ```

3. Write `mirror-bot/.env` (only these four values, nothing else to change):

   ```
   BOT_TOKEN=<your bot token>
   API_ID=<your app's api_id>
   API_HASH=<your app's api_hash>
   ALLOWED_USER_IDS=<your Telegram user id>
   ```

   Note: never put `API_ID` / `API_HASH` / `BOT_TOKEN` in any file that will be made public.

4. Start the bot:

   ```
   cd ~/mirror-bot && nohup .venv/bin/python app.py >> bot.log 2>&1 &
   ```

   If no venv was created, use `python3 app.py` instead. Wait 1 minute, then check the log:

   ```
   tail -30 ~/mirror-bot/bot.log
   ```

   When you see `Bot 已上线：@<bot username>`, it's running.
   The pidfile handles leftovers (a dead old process can be overwritten), so no manual cleanup is needed on restart.

   To auto-start the bot on boot, write a systemd service, or use your container's startup file to launch it
   (don't use the bot's app.py itself as the startup file: the console would become the bot log and you couldn't type commands anymore).

5. Send `/start` to the bot, then `/storage` to add a cloud drive (OAuth flow: click the login link, log in,
   and send the localhost redirect URL back to the bot), or copy an existing rclone.conf over
   and tap "Already configured in rclone (import)".

## Verify

- Send the bot a small file link: you should see download progress; when it finishes it uploads to your cloud drive automatically,
  and the local `mirror-bot/downloads/` is emptied afterwards.
- `/status` and `/cancel` should work.

## Troubleshooting

- Bot not coming online: check the end of `bot.log` first, then `ps aux | grep "mirror-bot"`.
- `setup.sh` says it can't create a venv: it falls back to `pip --break-system-packages` automatically;
  if that still fails, follow the manual install hints it prints.
