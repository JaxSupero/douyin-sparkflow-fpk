#!/bin/bash
# 抖音 SparkFlow — 登录桌面栈（Xvfb + fluxbox + x11vnc + websockify/noVNC + 登录 API 服务）
# 由 cmd/main 通过 setsid 拉起；停止时整进程组一起收掉。
set -u

SELF_DIR="$(CDPATH= cd -- "$(dirname -- "$(readlink -f "$0")")" && pwd)"
# shellcheck source=common.sh
. "${SELF_DIR}/common.sh"

RUN="${DOUYIN_RUNDIR}"
LOG="${DOUYIN_LOGDIR}"
mkdir -p "$RUN" "$LOG"

# ---------- 选择空闲的 X 显示号（:90-:99） ----------
pick_display() {
  local n sock lock
  for n in 99 98 97 96 95 94 93 92 91 90; do
    sock="/tmp/.X11-unix/X${n}"
    lock="/tmp/.X${n}-lock"
    if [ -S "$sock" ]; then
      continue
    fi
    if [ -e "$lock" ]; then
      # 锁存在但无对应 Xvfb 进程 → 认定为陈旧锁，清理后可用
      if pgrep -f "Xvfb :${n}( |\$)" >/dev/null 2>&1; then
        continue
      fi
      rm -f "$lock" 2>/dev/null || continue
    fi
    printf ':%s' "$n"
    return 0
  done
  return 1
}

DISPLAY_CHOSEN="$(pick_display)" || {
  echo "[$(date -Is)] no free X display (:90-:99) available" >&2
  exit 1
}
export DISPLAY="$DISPLAY_CHOSEN"
echo "DISPLAY=${DISPLAY}" > "${RUN}/display.env"
mkdir -p /tmp/.X11-unix 2>/dev/null || true

echo "[$(date -Is)] login stack starting on display ${DISPLAY}"

# ---------- Xvfb ----------
# 宿主 fnOS 的 /usr/bin 没有 xkbcomp（xorg 写死的绝对路径），
# 通过 LD_PRELOAD 垫片把 /usr/bin/xkbcomp 重定向到随包 xkbcomp。
XVFB_CMD=(Xvfb "${DISPLAY}" -screen 0 1600x1000x24 -ac +extension RANDR)
if [ -f "${DOUYIN_DIST}/display/xkbcomp-shim.so" ]; then
  XVFB_CMD=(env "LD_PRELOAD=${DOUYIN_DIST}/display/xkbcomp-shim.so"
            "DOUYIN_XKBCOMP=${DOUYIN_DIST}/display/usr/bin/xkbcomp"
            "${XVFB_CMD[@]}")
fi
run_forever xvfb "${XVFB_CMD[@]}"

# 等待 X socket 就绪（最多 30s）
n="${DISPLAY#:}"
for _ in $(seq 1 60); do
  [ -S "/tmp/.X11-unix/X${n}" ] && break
  sleep 0.5
done
if [ ! -S "/tmp/.X11-unix/X${n}" ]; then
  echo "[$(date -Is)] WARN: X socket not ready; window manager/VNC may retry" >&2
fi

# ---------- 窗口管理器 / VNC / noVNC 桥 ----------
run_forever fluxbox fluxbox
run_forever x11vnc x11vnc -display "${DISPLAY}" -forever -shared \
  -rfbport "${LOGIN_DESKTOP_VNC_PORT}" -localhost -nopw -nap -wait 50 -defer 50

if [ -f "${DOUYIN_DIST}/deps/bin/websockify" ]; then
  run_forever novnc "$DOUYIN_PY" "${DOUYIN_DIST}/deps/bin/websockify" \
    --web="${DOUYIN_DIST}/novnc" "${LOGIN_DESKTOP_WEB_PORT}" "127.0.0.1:${LOGIN_DESKTOP_VNC_PORT}"
else
  run_forever novnc "$DOUYIN_PY" -m websockify \
    --web="${DOUYIN_DIST}/novnc" "${LOGIN_DESKTOP_WEB_PORT}" "127.0.0.1:${LOGIN_DESKTOP_VNC_PORT}"
fi

# ---------- 登录 API 服务（前台循环，退出后重启） ----------
# API token 文件：内部鉴权用（web 面板与登录服务的共享密钥）
if [ -n "${SPARKFLOW_LOGIN_DESKTOP_API_TOKEN_FILE:-}" ]; then
  mkdir -p "$(dirname "${SPARKFLOW_LOGIN_DESKTOP_API_TOKEN_FILE}")"
  if [ ! -s "${SPARKFLOW_LOGIN_DESKTOP_API_TOKEN_FILE}" ]; then
    umask 077
    head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' > "${SPARKFLOW_LOGIN_DESKTOP_API_TOKEN_FILE}"
  fi
fi

cd "${DOUYIN_RUNTIME}" || exit 1
API_LOG="${LOG}/login_api.log"
while true; do
  printf '[%s] starting login api server\n' "$(date -Is)" >> "$API_LOG"
  "$DOUYIN_PY" login_desktop_server.py >> "$API_LOG" 2>&1
  printf '[%s] login api server exited rc=%s; restart in 2s\n' "$(date -Is)" "$?" >> "$API_LOG"
  sleep 2
done
