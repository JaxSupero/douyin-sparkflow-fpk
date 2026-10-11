#!/bin/bash
# 抖音 SparkFlow — Web 控制台守护进程（崩溃自动重启）
set -u

SELF_DIR="$(CDPATH= cd -- "$(dirname -- "$(readlink -f "$0")")" && pwd)"
# shellcheck source=common.sh
. "${SELF_DIR}/common.sh"

cd "${DOUYIN_RUNTIME}" || exit 1
LOG="${DOUYIN_LOGDIR}/web.log"
while true; do
  printf '[%s] starting web (main.py --web --port %s)\n' "$(date -Is)" "$WEB_PORT" >> "$LOG"
  "$DOUYIN_PY" main.py --web --host 0.0.0.0 --port "$WEB_PORT" >> "$LOG" 2>&1
  printf '[%s] web exited rc=%s; restart in 3s\n' "$(date -Is)" "$?" >> "$LOG"
  sleep 3
done
