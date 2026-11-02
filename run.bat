@echo off
rem ===== SETTINGS: paste your NEW bot token below =====
set BOT_TOKEN=8985130432:AAExS-GZmL3JQDbvFQIUcplPg4FlGe9T5mg
set ADMIN_IDS=7518460854
set TZ_NAME=Asia/Yekaterinburg
set OPEN_HOUR=10
set CLOSE_HOUR=22

rem ===== MINI APP: https address from cloudflared (leave empty to disable) =====
set WEBAPP_URL=
set WEB_PORT=8080
rem AUTO_TUNNEL=ngrok (or cloudflared): the bot starts the tunnel itself and sets WEBAPP_URL automatically
set AUTO_TUNNEL=cloudflared

rem ===== PROXY: http://login:password@host:port  (or socks5://...) =====
set PROXY_URL=http://V9BBvF:DbWUzp@168.81.43.22:8000
rem cloudflared reads the proxy from HTTPS_PROXY
set HTTPS_PROXY=%PROXY_URL%
set NO_PROXY=localhost,127.0.0.1

python -m pip install --quiet --disable-pip-version-check "aiogram>=3.7" aiohttp-socks tzdata
python bot.py
pause
