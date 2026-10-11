#!/bin/bash
# 构建 douyin-sparkflow 飞牛 FPK 安装包
#
# 前置：app/dist/** 已就绪（scripts/prepare-deps.sh）
# 产物：dist/fnos-douyin_sparkflow_v<版本>.fpk (+ .sha256)
#
# 构建流程：
#   1. 获取上游源码（本地 SRC_DIR 或从 git 检出 UPSTREAM_REPO@UPSTREAM_REF）
#   2. 应用 fnOS 原生部署补丁（锚点严格校验，不匹配即失败）
#   3. 权限规范化（可执行位、软链归一化、构建机路径清理）
#   4. 打包（优先 fnpack；不可用时回退 tar+gzip 复刻官方双层结构）
#   5. 结构/checksum/JSON/补丁校验
#
# 环境变量：
#   SRC_DIR           本地上游源码目录（存在则优先使用）
#   UPSTREAM_REPO     上游 git 仓库（默认 halfwaystudent/douyin-sparkflow）
#   UPSTREAM_REF      上游分支或 commit（默认 main）
#   UPSTREAM_SHA      钉死的 commit；提供时忽略 UPSTREAM_REF，保证可复现
#   ALLOW_FALLBACK=1  允许 fnpack 缺失时回退 tar+gzip（CI 默认开启）
#   SKIP_DEPS_CHECK=1 跳过 dist/ 完整性检查（仅用于本地增量调试）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APPNAME="douyin_sparkflow"
OUT="$ROOT/dist"
UPSTREAM_REPO="${UPSTREAM_REPO:-https://github.com/halfwaystudent/douyin-sparkflow.git}"
UPSTREAM_REF="${UPSTREAM_REF:-main}"
UPSTREAM_SHA="${UPSTREAM_SHA:-}"
SRC_DIR="${SRC_DIR:-$(dirname "$ROOT")/douyin-sparkflow/DouYinSparkFlow}"

cd "$ROOT"
log() { printf '\n=== %s ===\n' "$*"; }

# 归一化 tar 成员名清单：同一条目的三种写法都要能匹配同一成员
#   tar 目录 → "cmd/"            （带尾斜杠）
#   兜底构建 → "./ui/config"      （带 ./ 前缀）
#   fnpack   → "ui/config"        （干净）
normalize_list() { sed -e 's:^\./::' -e 's:/*$::' "$1" | sort -u; }

# ─────────────────────────────────────────────
clone_upstream() {
  # GitHub 偶发 TLS 中断 / 速率限制，重试 3 次再放弃。
  local dest="$1"; shift
  local attempt=1
  while [ "$attempt" -le 3 ]; do
    if git clone --depth 1 "$@" "$dest"; then
      return 0
    fi
    echo "git clone 失败（第 $attempt/3 次），5 秒后重试" >&2
    rm -rf "$dest"
    sleep 5
    attempt=$((attempt + 1))
  done
  echo "::error:: 无法从上游拉取源码（重试 3 次均失败）" >&2
  return 1
}

log "0/6 获取上游源码"
BUILD_SRC=""
if [ -d "$SRC_DIR" ] && [ -f "$SRC_DIR/main.py" ]; then
  BUILD_SRC="$SRC_DIR"
  echo "使用本地上游源码：$BUILD_SRC"
else
  CHECKOUT="$ROOT/.cache/upstream"
  rm -rf "$CHECKOUT"
  if [ -n "$UPSTREAM_SHA" ]; then
    # 钉 commit：先抓目标分支再 checkout 目标 SHA，保证单次浅克隆也能拿到
    echo "从 $UPSTREAM_REPO 检出 $UPSTREAM_REF 后切换到 $UPSTREAM_SHA"
    clone_upstream "$CHECKOUT" --branch "$UPSTREAM_REF" "$UPSTREAM_REPO"
    git -C "$CHECKOUT" fetch --depth 1 origin "$UPSTREAM_SHA" 2>/dev/null || true
    git -C "$CHECKOUT" checkout -q "$UPSTREAM_SHA"
  else
    echo "从 $UPSTREAM_REPO 检出 $UPSTREAM_REF"
    clone_upstream "$CHECKOUT" --branch "$UPSTREAM_REF" "$UPSTREAM_REPO"
  fi
  BUILD_SRC="$CHECKOUT/DouYinSparkFlow"
  [ -f "$BUILD_SRC/main.py" ] || { echo "上游源码结构异常：缺少 DouYinSparkFlow/main.py" >&2; exit 1; }
  echo "上游 HEAD: $(git -C "$CHECKOUT" rev-parse HEAD)"
fi

# ─────────────────────────────────────────────
log "1/6 环境检查"
if [ "${SKIP_DEPS_CHECK:-0}" != "1" ]; then
  for d in deps chrome display novnc; do
    [ -e "app/dist/$d" ] || { echo "缺少 app/dist/$d：请先运行 scripts/prepare-deps.sh" >&2; exit 1; }
  done
  [ -f app/dist/display/xkbcomp-shim.so ] || { echo "缺少 xkbcomp-shim.so" >&2; exit 1; }
  [ -x app/dist/display/usr/bin/xkbcomp ] || { echo "缺少 xkbcomp（Xvfb 必需）" >&2; exit 1; }
fi

log "2/6 同步源码到 app/src"
rm -rf app/src
mkdir -p app/src
(cd "$BUILD_SRC" && tar cf - \
  --exclude='./tests' \
  --exclude='./docs/images' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='./logs' \
  --exclude='./state' \
  --exclude='./config.json' \
  --exclude='./usersData.json' \
  --exclude='./webui_settings.json' \
  --exclude='./.im_sdk_cache' \
  .) | (cd app/src && tar xf -)
# 附带许可证（上游仓库根目录 LICENSE，随应用分发）
cp -f LICENSE app/src/LICENSE 2>/dev/null \
  || cp -f "$(dirname "$BUILD_SRC")/LICENSE" app/src/LICENSE 2>/dev/null || true

log "3/6 应用 fnOS 原生部署补丁"
python3 scripts/patch-src.py app/src

log "4/6 权限规范化 + 软链接归一化"
# 构建机路径泄漏防护：Chromium 目录等产物可能残留 /home/runner/... 之类绝对路径，
# 一旦进入安装包，目标机上行为会与本地验证结果不一致。
find app -type l | while read -r link; do
  target="$(readlink -f "$link" 2>/dev/null || true)"
  if [ -n "$target" ] && [ -e "$target" ]; then
    cp -Lp --remove-destination "$target" "$link"
  else
    echo "WARN: 移除悬空软链: $link"
    rm -f "$link"
  fi
done
[ -z "$(find app -type l)" ] || { echo "仍存在软链接，构建中止"; exit 1; }

chmod 755 cmd/*
chmod 755 app/scripts/*.sh app/scripts/*.py 2>/dev/null || true
chmod 755 app/dist/display/xkbcomp-shim.so
find app/dist/display/usr/bin -type f -exec chmod 755 {} \; 2>/dev/null || true
find app/dist/deps/bin -type f -exec chmod 755 {} \; 2>/dev/null || true
find app/dist/chrome -type f \( -name chrome -o -name "chrome_crashpad_handler" -o -name "xdg-settings" \) -exec chmod 755 {} \; 2>/dev/null || true
find app -type d -exec chmod 755 {} \; 2>/dev/null || true
find app -type f -exec chmod a+r {} \;
find app -type f -perm /u+x -exec chmod a+x {} \; 2>/dev/null || true

# 禁用脚本里的自引用（venv bin 目录），否则 fnOS 解压后可能覆盖出坏链接
find app/dist/deps/bin -type l -delete 2>/dev/null || true

log "5/6 打包"
VER="$(grep '^version' manifest | awk -F= '{print $2}' | tr -d ' ')"
mkdir -p "$OUT"
FPK="$ROOT/${APPNAME}.fpk"
FINAL="$OUT/fnos-${APPNAME}_v${VER}.fpk"
rm -f "$FPK" "$FINAL"

FNPACK_BIN="$(command -v fnpack || true)"
if [ -n "$FNPACK_BIN" ]; then
  echo "使用 fnpack：$FNPACK_BIN"
  (cd "$ROOT" && "$FNPACK_BIN" build)
  [ -f "$FPK" ] || { echo "fnpack 未生成 $FPK" >&2; exit 1; }
elif [ "${ALLOW_FALLBACK:-0}" = "1" ]; then
  # 兜底：复刻官方双层 tar.gz 结构（manifest.checksum = app.tgz 的 MD5）。
  # 官方 fnpack 的输出格式（实测 1.0.4 / 1.2.4 一致）：
  #   外层 tar.gz 内含 manifest / manifest.checksum / cmd / config / wizard /
  #   ICON*.PNG / app.tgz；内层 app.tgz 里的路径**不带 app/ 前缀**（是 ui/…、src/…、dist/…）。
  # 用 -C app . 而不是 tar app，是为了让内层路径与 fnpack 逐字节一致 ——
  # fnOS 安装时会把 app.tgz 解压进 target 目录，带前缀会变成 target/app/ui/… 而找不到 ui/config。
  echo "fnpack 不可用，使用 tar+gzip 兜底构建"
  tar -C app --owner=root --group=root --mtime='@0' -cf - . | gzip -n > app.tgz
  sum="$(md5sum app.tgz | awk '{print $1}')"
  # manifest 里可能根本没有 checksum 行（fnpack 成功构建后会把该行删掉），
  # 此时纯 sed 替换是静默空操作 → 写回失败。这里显式处理“有则替换、无则追加”。
  if grep -q '^checksum[[:space:]]*=' manifest; then
    sed -i "s/^checksum[[:space:]]*=.*/checksum              = ${sum}/" manifest
  else
    printf 'checksum              = %s\n' "$sum" >> manifest
  fi
  # 成员集合与官方 fnpack 输出一致（fnpack 不生成 manifest.checksum，实测 1.0.4/1.2.4 均如此）
  tar --owner=root --group=root --mtime='@0' -cf - \
    manifest cmd config wizard ICON.PNG ICON_256.PNG LICENSE app.tgz \
    | gzip -n > "$FPK"
  rm -f app.tgz
  [ -f "$FPK" ] || { echo "兜底构建未生成 $FPK" >&2; exit 1; }
else
  echo "缺少 fnpack（可用 ALLOW_FALLBACK=1 启用 tar+gzip 兜底构建）" >&2
  exit 1
fi

mv -f "$FPK" "$FINAL"
[ -f "$FINAL" ] || { echo "未生成 $FINAL" >&2; exit 1; }

log "5/6 校验"
FAIL=0
TMP_VERIFY="$(mktemp -d)"
OUTER_LIST="$TMP_VERIFY/outer.txt"
INNER_LIST="$TMP_VERIFY/inner.txt"
# 注意：不能用 `tar tzf | grep -q`（grep 命中即退出会令 tar 收到 SIGPIPE，
# 在 set -o pipefail 下会误报失败）。统一先落盘清单再检查。
tar tzf "$FINAL" > "$OUTER_LIST"
normalize_list "$OUTER_LIST" > "$OUTER_LIST.norm"

# 5.1 必备成员
for m in manifest app.tgz cmd config wizard ICON.PNG ICON_256.PNG LICENSE; do
  grep -qx "$m" "$OUTER_LIST.norm" || { echo "缺少成员: $m"; FAIL=1; }
done
# 5.2 checksum = app.tgz 的 MD5
# 注意不要写成 `tar | grep '^checksum' | awk`：grep 命中即退出会让 tar 收到 SIGPIPE，
# 在 set -o pipefail 下整条管道判失败（就是 build 脚本静默中止的真凶）。
SUM_IN_MANIFEST="$(tar xzf "$FINAL" -O manifest | awk -F= '/^checksum[[:space:]]*=/ {gsub(/[[:space:]]/,"",$2); print $2}')"
SUM_ACTUAL="$(tar xzf "$FINAL" -O app.tgz | md5sum | awk '{print $1}')"
echo "checksum(manifest)=$SUM_IN_MANIFEST"
echo "checksum(actual) =$SUM_ACTUAL"
[ "$SUM_IN_MANIFEST" = "$SUM_ACTUAL" ] || { echo "checksum 不匹配"; FAIL=1; }

# 5.3 app.tgz 提取 + 关键文件
tar xzf "$FINAL" -C "$TMP_VERIFY" app.tgz || { echo "提取 app.tgz 失败"; FAIL=1; }
if [ -f "$TMP_VERIFY/app.tgz" ]; then
  tar tzf "$TMP_VERIFY/app.tgz" > "$TMP_VERIFY/inner.raw"
  normalize_list "$TMP_VERIFY/inner.raw" > "$INNER_LIST"
  for p in "src/main.py" "src/login_desktop_server.py" "scripts/bootstrap.py" \
           "scripts/login_stack.sh" "scripts/web.sh" "scripts/scheduler.sh" \
           "ui/config" "dist/display/usr/bin/Xvfb" "dist/display/xkbcomp-shim.so" \
           "dist/novnc/vnc.html" "dist/deps/.deps-version"; do
    grep -qx "$p" "$INNER_LIST" || { echo "app.tgz 缺少: $p"; FAIL=1; }
  done
  grep -q "^dist/deps/playwright" "$INNER_LIST" || { echo "app.tgz 缺少 Python 依赖"; FAIL=1; }
  grep -q "^dist/chrome/chromium-" "$INNER_LIST" || { echo "app.tgz 缺少 Chromium"; FAIL=1; }
  grep -q "^dist/display/fonts/" "$INNER_LIST" || { echo "app.tgz 缺少字体"; FAIL=1; }
  # Playwright .links 记录构建机 driver 绝对路径，必须已清除
  if grep -q "^dist/chrome/\.links/" "$INNER_LIST"; then
    echo "app.tgz 残留 Playwright .links（构建机路径），构建中止"; FAIL=1
  fi
  # 构建机路径泄漏（CI 为 /home/runner/...；本地工作目录为 /vol*/douyin-sparkflow-fpk）
  # 用流式 grep 扫描，不落盘 —— app.tgz 近 1.2GB，整体解包不划算。
  LEAK="$(gzip -dc "$TMP_VERIFY/app.tgz" 2>/dev/null \
    | grep -aoE '/home/runner/[A-Za-z0-9._/-]+|/vol[0-9]+/[A-Za-z0-9._/-]*douyin-sparkflow-fpk[A-Za-z0-9._/-]*' \
    | sort -u | head -5 || true)"
  if [ -n "$LEAK" ]; then
    echo "::error:: app.tgz 内混入构建机绝对路径，构建中止："
    printf '  %s\n' $LEAK
    FAIL=1
  fi
fi

# 5.4 JSON 合法性（外层 config/wizard + 内层 ui/config）
# 用 JSON_RESULT 标记行判定，避免 python 非零退出在 set -e 下静默中止脚本。
tar xzf "$FINAL" -C "$TMP_VERIFY" config wizard
# 注意：兜底构建的内层成员是 "./ui/config"，按精确名 "ui/config" 提取会报
# "Not found in archive"。用 --wildcards 按后缀匹配，两种格式都能取到。
extract_inner() {
  tar xzf "$TMP_VERIFY/app.tgz" -C "$TMP_VERIFY" --wildcards --no-anchored "$1" 2>/dev/null || true
}
[ -f "$TMP_VERIFY/app.tgz" ] && extract_inner "ui/config"
JSON_OUT="$(python3 - "$TMP_VERIFY" <<'EOF'
import json, sys
from pathlib import Path
root = Path(sys.argv[1])
result = "ok"
for rel in ("config/privilege", "config/resource", "wizard/install", "wizard/uninstall", "ui/config"):
    p = root / rel
    if not p.exists():
        print("json MISSING:", rel)
        result = "fail"
        continue
    try:
        json.loads(p.read_text(encoding="utf-8"))
        print("json OK:", rel)
    except (json.JSONDecodeError, OSError) as exc:
        print("json INVALID:", rel, exc)
        result = "fail"
print("JSON_RESULT=" + result)
EOF
)"
printf '%s\n' "$JSON_OUT"
[ "$(printf '%s' "$JSON_OUT" | grep -c 'JSON_RESULT=ok')" = "1" ] || FAIL=1

# 5.5 cmd 执行位（顶层 cmd/）与补丁生效检查（内层 src/）
tar tzvf "$FINAL" cmd/main > "$TMP_VERIFY/cmdmain.mode"
grep -qE "^-rwx" "$TMP_VERIFY/cmdmain.mode" || { echo "cmd/main 缺少执行位"; FAIL=1; }
if [ -f "$TMP_VERIFY/app.tgz" ]; then
  extract_inner "src/webui/ops.py"
  extract_inner "src/webui/login_lock.py"
  [ -f "$TMP_VERIFY/src/webui/ops.py" ] || { echo "无法提取 src/webui/ops.py"; FAIL=1; }
  [ -f "$TMP_VERIFY/src/webui/login_lock.py" ] || { echo "无法提取 src/webui/login_lock.py"; FAIL=1; }
  grep -q "host_spool_enabled" "$TMP_VERIFY/src/webui/ops.py" || { echo "补丁未生效: ops.py"; FAIL=1; }
  grep -q "SPARKFLOW_HOST_CRONTAB_PATH" "$TMP_VERIFY/src/webui/ops.py" || { echo "补丁未生效: spool 环境变量"; FAIL=1; }
  grep -q "SPARKFLOW_STATE_DIR" "$TMP_VERIFY/src/webui/login_lock.py" || { echo "补丁未生效: 状态目录"; FAIL=1; }
fi
rm -rf "$TMP_VERIFY"

[ "$FAIL" = "0" ] || { echo "校验失败！" >&2; exit 1; }

log "6/6 完成"
sha256sum "$FINAL" | tee "$FINAL.sha256"
ls -lh "$FINAL"
echo
echo "使用说明：将 $FINAL 上传到飞牛应用中心手动安装即可。"
