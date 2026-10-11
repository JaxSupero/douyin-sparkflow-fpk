#!/bin/bash
# 抖音 SparkFlow — 运行环境公共定义（由各启动脚本 source）
# 所有路径以 TRIM_* 环境变量为准；支持 ${TRIM_PKGVAR}/fpk.env 用户覆盖。

DOUYIN_DIST="${TRIM_APPDEST}/dist"
DOUYIN_RUNTIME="${TRIM_PKGVAR}/runtime"
DOUYIN_VENV="${TRIM_PKGVAR}/venv"
DOUYIN_PY="${DOUYIN_VENV}/bin/python"

# 用户运行参数覆盖（登录网络模式、代理、端口等）
if [ -f "${TRIM_PKGVAR}/fpk.env" ]; then
  # shellcheck disable=SC1090
  . "${TRIM_PKGVAR}/fpk.env"
fi
: "${LOGIN_DESKTOP_API_PORT:=18090}"
: "${LOGIN_DESKTOP_VNC_PORT:=5901}"
: "${LOGIN_DESKTOP_WEB_PORT:=6080}"
: "${LOGIN_DESKTOP_PROXY_MODE:=auto}"
: "${LOGIN_DESKTOP_PROXY:=http://127.0.0.1:7890}"
: "${WEB_PORT:=8787}"

# ---------- 运行环境 ----------
# 优先 venv，其次随包 X/VNC 工具、应用中心 Node.js（协议发送可用）、系统路径
export PATH="${DOUYIN_VENV}/bin:${DOUYIN_DIST}/display/usr/bin:/var/apps/nodejs_v24/target/bin:/var/apps/nodejs_v22/target/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
# 随包 X11/VNC 动态库优先（与宿主同为 Debian 12，补缺不替换）
export LD_LIBRARY_PATH="${DOUYIN_DIST}/display/usr/lib/x86_64-linux-gnu:${DOUYIN_DIST}/display/usr/lib:${DOUYIN_DIST}/display/lib/x86_64-linux-gnu:${DOUYIN_DIST}/display/lib"

# Playwright 浏览器与运行主页
export PLAYWRIGHT_BROWSERS_PATH="${DOUYIN_DIST}/chrome"
export HOME="${TRIM_PKGVAR}/home"
export XDG_CACHE_HOME="${HOME}/.cache"
export XDG_DATA_HOME="${HOME}/.local/share"
export PYTHONUNBUFFERED=1
export TZ="${TZ:-Asia/Shanghai}"
export NO_PROXY="localhost,127.0.0.1,::1"
export no_proxy="localhost,127.0.0.1,::1"

# ---------- 应用自身环境 ----------
export SPARKFLOW_BROWSER_PROFILE_ROOT="${TRIM_PKGVAR}/browser-profiles"
export SPARKFLOW_STATE_DIR="${TRIM_PKGVAR}/state"
# 任务调度 spool（web 面板写、调度器读；已通过源码补丁接入）
export SPARKFLOW_HOST_CRONTAB_PATH="${TRIM_PKGVAR}/cron/root"
export SPARKFLOW_CRON_LOG_PATH="${TRIM_PKGVAR}/runtime/logs/douyin-sparkflow.log"
export SPARKFLOW_OPS_LOG_FILE="${TRIM_PKGVAR}/runtime/logs/douyin-sparkflow.log"
# 登录桌面服务（本机回环）+ Web 同源代理
export SPARKFLOW_LOGIN_DESKTOP_API_URL="http://127.0.0.1:${LOGIN_DESKTOP_API_PORT}"
export SPARKFLOW_LOGIN_DESKTOP_NOVNC_URL="http://127.0.0.1:${LOGIN_DESKTOP_WEB_PORT}"
export SPARKFLOW_LOGIN_DESKTOP_NOVNC_WS_URL="ws://127.0.0.1:${LOGIN_DESKTOP_WEB_PORT}/websockify"
export SPARKFLOW_LOGIN_DESKTOP_PUBLIC_URL="/login-desktop/proxy/vnc.html?autoconnect=1&resize=scale&view_only=0&path=login-desktop/proxy/websockify"
export SPARKFLOW_LOGIN_DESKTOP_API_TOKEN_FILE="${TRIM_PKGVAR}/state/login-api-token"
export SPARKFLOW_SESSION_COOKIE_SECURE=0
# 登录桌面本体
export LOGIN_DESKTOP_MODE=novnc
export LOGIN_PROFILE_DIR="${TRIM_PKGVAR}/login-profile"
export LOGIN_DESKTOP_API_PORT LOGIN_DESKTOP_VNC_PORT LOGIN_DESKTOP_WEB_PORT
export LOGIN_DESKTOP_PROXY_MODE LOGIN_DESKTOP_PROXY
export LOGIN_DESKTOP_IDLE_TIMEOUT_SECONDS="${LOGIN_DESKTOP_IDLE_TIMEOUT_SECONDS:-1800}"
export LOGIN_DESKTOP_STOP_AFTER_EXPORT_SECONDS="${LOGIN_DESKTOP_STOP_AFTER_EXPORT_SECONDS:-60}"
export LOGIN_DESKTOP_STATUS_CACHE_SECONDS="${LOGIN_DESKTOP_STATUS_CACHE_SECONDS:-15}"

DOUYIN_LOGDIR="${TRIM_PKGVAR}/logs"
DOUYIN_RUNDIR="${TRIM_PKGVAR}/run"

# ---------- 工具函数 ----------
run_forever() {
  # run_forever <名字> <命令...>：后台守护循环，退出后 2 秒自动重启
  local name="$1"; shift
  local logf="${DOUYIN_LOGDIR}/login_${name}.log"
  (
    while true; do
      printf '[%s] starting %s\n' "$(date -Is)" "$name" >> "$logf"
      "$@" >> "$logf" 2>&1
      printf '[%s] %s exited rc=%s; restart in 2s\n' "$(date -Is)" "$name" "$?" >> "$logf"
      sleep 2
    done
  ) &
}
