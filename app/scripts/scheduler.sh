#!/bin/bash
# 抖音 SparkFlow — 任务调度器（读取数据目录中的 spool 文件，按分钟触发发送任务）
set -u

SELF_DIR="$(CDPATH= cd -- "$(dirname -- "$(readlink -f "$0")")" && pwd)"
# shellcheck source=common.sh
. "${SELF_DIR}/common.sh"

cd "${DOUYIN_RUNTIME}" || exit 1
SPOOL="${TRIM_PKGVAR}/cron/root"
mkdir -p "$(dirname "$SPOOL")"
LOG="${DOUYIN_LOGDIR}/scheduler.log"
while true; do
  printf '[%s] starting scheduler (spool=%s)\n' "$(date -Is)" "$SPOOL" >> "$LOG"
  "$DOUYIN_PY" scripts/cron_runner.py "$SPOOL" >> "$LOG" 2>&1
  printf '[%s] scheduler exited rc=%s; restart in 3s\n' "$(date -Is)" "$?" >> "$LOG"
  sleep 3
done
