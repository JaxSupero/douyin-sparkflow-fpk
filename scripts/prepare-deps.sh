#!/bin/bash
# 准备离线依赖（构建机执行；产物都进 app/dist/，随 FPK 打包，目标机安装无需联网）
#
# 产物：
#   app/dist/deps      — Python 3.12 依赖（manylinux2014_x86_64 轮子，uv 交叉安装）
#   app/dist/chrome    — Playwright Chromium（版本跟随上游 requirements.txt）
#   app/dist/display   — Xvfb / x11vnc / fluxbox 及其宿主缺失的依赖库（Debian 12 包解包）
#   app/dist/display/fonts — 中文字体 + Noto Color Emoji
#   app/dist/novnc     — noVNC 静态资源（websockify --web 根目录）
#
# 环境变量：
#   DEBIAN_SUITE              Debian 套件版本（默认 bookworm）
#   DEBIAN_MIRROR             Debian 主仓镜像（默认 deb.debian.org）
#   DEBIAN_SECURITY_MIRROR    Debian 安全仓镜像（默认 deb.debian.org/debian-security）
#   DISPLAY_PKGS / FONT_PKGS  覆盖默认 X 组件/字体包列表
#   FORCE_REBUILD_DEPS=1      强制重新生成全部依赖缓存
#   OFFLINE=1                 严格离线；缺少/过期的既有产物直接失败
set -euo pipefail

ROOT="${ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
SRC_DIR="${SRC_DIR:-$(dirname "$ROOT")/douyin-sparkflow/DouYinSparkFlow}"
PYPI="${1:-https://pypi.tuna.tsinghua.edu.cn/simple}"
PW_HOST="${2:-https://cdn.npmmirror.com/binaries/playwright}"

CACHE="$ROOT/.cache"
DIST="$ROOT/app/dist"
TOOLS="$CACHE/tools"
DEBS="$CACHE/debs"
mkdir -p "$CACHE" "$DIST" "$TOOLS" "$DEBS"

DEBIAN_SUITE="${DEBIAN_SUITE:-bookworm}"
DEBIAN_MIRROR="${DEBIAN_MIRROR:-https://deb.debian.org/debian}"
DEBIAN_SECURITY_MIRROR="${DEBIAN_SECURITY_MIRROR:-https://deb.debian.org/debian-security}"

# fnOS 宿主的 /usr/bin 没有 xkbcomp，但 xorg 编译期写死了这个绝对路径，
# 需要随包带上并由 xkbcomp-shim.so 重定向。缺它 Xvfb 会直接 Fatal server error。
DEFAULT_DISPLAY_PKGS="xvfb x11vnc fluxbox xkb-data x11-common libxfont2 libimlib2 libspectre1 libvncclient1 libvncserver1 x11-xkb-utils"
DEFAULT_FONT_PKGS="fonts-wqy-zenhei fonts-wqy-microhei fonts-noto-color-emoji"

if [ "${OFFLINE:-0}" = "1" ] && [ "${FORCE_REBUILD_DEPS:-0}" = "1" ]; then
  echo "OFFLINE=1 与 FORCE_REBUILD_DEPS=1 不能同时使用" >&2
  exit 1
fi
[ -f "$SRC_DIR/requirements.txt" ] || { echo "找不到上游源码：$SRC_DIR" >&2; exit 1; }

log() { printf '\n=== %s ===\n' "$*"; }

have() { [ -e "$1" ]; }

# ─────────────────────────────────────────────────────────────
log "1/7 准备 uv（通过 PyPI 获取二进制，不走 GitHub Releases）"
UV="$TOOLS/uv"
if [ ! -x "$UV" ]; then
  mkdir -p "$TOOLS/uvpkg"
  ( cd "$TOOLS/uvpkg" && rm -f ./*.whl && pip3 download uv --no-deps -d . -i "$PYPI" >/dev/null )
  ( cd "$TOOLS/uvpkg" && rm -rf extracted && unzip -q -o ./*.whl -d extracted )
  cp "$TOOLS/uvpkg"/extracted/uv-*.data/scripts/uv "$UV"
  chmod +x "$UV"
fi
"$UV" --version

# ─────────────────────────────────────────────────────────────
log "2/7 Python 依赖（python3.12 / manylinux2014_x86_64）"
# 指纹相同则复用（CI 缓存命中时省掉几分钟下载与解包）
DEPS_WANT="$({
  echo "python=3.12"
  echo "platform=manylinux2014_x86_64"
  grep -vE '^\s*(#|$)' "$SRC_DIR/requirements.txt" | sort
  echo "websockify==0.13.0"
} | sha256sum | awk '{print $1}')"
DEPS_HAVE=""
[ -f "$DIST/deps/.deps-version" ] && DEPS_HAVE="$(cat "$DIST/deps/.deps-version" 2>/dev/null || true)"
if [ "${OFFLINE:-0}" = "1" ] && { [ "$DEPS_WANT" != "$DEPS_HAVE" ] || [ ! -d "$DIST/deps/playwright" ]; }; then
  echo "OFFLINE=1 且 Python 依赖缓存缺失或已过期（want=$DEPS_WANT have=$DEPS_HAVE），中止" >&2
  exit 1
fi
if [ "${FORCE_REBUILD_DEPS:-0}" != "1" ] && [ "$DEPS_WANT" = "$DEPS_HAVE" ] && have "$DIST/deps/playwright"; then
  echo "复用已有 Python 依赖（指纹 $DEPS_HAVE）"
else
  rm -rf "$DIST/deps"
  mkdir -p "$DIST/deps"
  UV_DEFAULT_INDEX="$PYPI" "$UV" pip install --target "$DIST/deps" \
    --python-platform x86_64-unknown-linux-gnu --python-version 3.12 \
    --only-binary :all: \
    -r "$SRC_DIR/requirements.txt" websockify==0.13.0
  # console 脚本 shebang 统一为 /usr/bin/env python3（运行时由 venv PATH 解析）
  for f in "$DIST/deps/bin"/*; do
    [ -f "$f" ] || continue
    sed -i '1s|^#!.*|#!/usr/bin/env python3|' "$f"
    chmod 755 "$f"
  done
  # 依赖指纹（venv 重建判据）：由 requirements 内容决定，不含构建机路径
  printf '%s\n' "$DEPS_WANT" > "$DIST/deps/.deps-version"
fi

# 剥离 dist-info/sboms：Rust/R 扩展（如 pydantic-core）在此记录了 CI 构建机绝对路径
# （实测 /home/runner/work/pydantic/pydantic/pydantic-core）。它是供应链元数据、运行时不用，
# 但会被 build-fpk.sh 的构建机路径泄漏检查拦下，故在此统一清理。
find "$DIST/deps" -maxdepth 2 -type d -name sboms -exec rm -rf {} + 2>/dev/null || true

# .pyc 会把构建机绝对源码路径写进 co_filename，清掉（运行时会自行重建）
find "$DIST/deps" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true

echo "deps-version: $(cat "$DIST/deps/.deps-version")"

# ─────────────────────────────────────────────────────────────
log "3/7 Playwright Chromium（$PW_HOST）"
PW_VER="$(awk '/^playwright==/ {sub(/^playwright==/, ""); gsub(/[[:space:]]/, ""); print; exit}' "$SRC_DIR/requirements.txt")"
[ -n "$PW_VER" ] || { echo "requirements.txt 未固定 playwright 版本" >&2; exit 1; }
# 浏览器 revision 随 Playwright 版本变化，用版本号当缓存键
CHROME_STAMP="$DIST/chrome/.playwright-version"
CHROME_HAVE=""
[ -f "$CHROME_STAMP" ] && CHROME_HAVE="$(cat "$CHROME_STAMP" 2>/dev/null || true)"
# 指纹相同且 Chromium 主程序存在才复用（stamp 本身不能证明目录未损坏）
CHROME_BIN="$(find "$DIST/chrome" -maxdepth 3 -type f -path '*/chrome-linux/chrome' -print -quit 2>/dev/null || true)"
CHROME_CACHE_OK=0
[ -n "$CHROME_BIN" ] && [ -x "$CHROME_BIN" ] && CHROME_CACHE_OK=1
if [ "${OFFLINE:-0}" = "1" ] && { [ "$PW_VER" != "$CHROME_HAVE" ] || [ "$CHROME_CACHE_OK" != "1" ]; }; then
  echo "OFFLINE=1 且 Chromium 缓存缺失、损坏或版本不符（want=$PW_VER have=$CHROME_HAVE），中止" >&2
  exit 1
fi
if [ "${FORCE_REBUILD_DEPS:-0}" != "1" ] && [ "$PW_VER" = "$CHROME_HAVE" ] && [ "$CHROME_CACHE_OK" = "1" ]; then
  echo "复用已有 Chromium（playwright $PW_VER）"
  du -sh "$DIST/chrome"
else
  PW_VENV="$TOOLS/pwvenv"
  if [ ! -x "$PW_VENV/bin/python" ]; then
    python3 -m venv "$PW_VENV"
  fi
  if ! "$PW_VENV/bin/pip" show playwright 2>/dev/null | grep -q "Version: $PW_VER"; then
    "$PW_VENV/bin/pip" install -q -i "$PYPI" "playwright==$PW_VER"
  fi
  rm -rf "$DIST/chrome"
  mkdir -p "$DIST/chrome"
  PLAYWRIGHT_BROWSERS_PATH="$DIST/chrome" PLAYWRIGHT_DOWNLOAD_HOST="$PW_HOST" \
    "$PW_VENV/bin/playwright" install chromium
  rm -rf "$DIST/chrome/.links"
  CHROME_CACHE_OK=1
fi

# 方案 A：删掉 chromium_headless_shell（实测 323 MiB，占 chrome 目录 35%）。
# Playwright 1.49+ 的 headless=True 默认走 headless_shell 二进制，删掉后必须让
# launch 走 channel="chromium" 回退到完整 Chromium —— 这一改动由 patch-src.py
# 给 core/browser.py 的 _browser_launch_options 加上 channel 实现。
# 完整 Chromium 自带 libvk_swiftshader，无头渲染不依赖 headless_shell。
# 实测：删掉后 channel="chromium" + headless=True 能正常启动并访问目标站点。
# 保留开关：KEEP_HEADLESS_SHELL=1（排查浏览器问题时临时用）。
if [ "${KEEP_HEADLESS_SHELL:-0}" != "1" ]; then
  rm -rf "${DIST:?}"/chrome/chromium_headless_shell-*
  echo "已移除 chromium_headless_shell（无头任务改走完整 Chromium）"
else
  echo "KEEP_HEADLESS_SHELL=1：保留 headless_shell"
fi
printf '%s\n' "$PW_VER" > "$CHROME_STAMP"
du -sh "$DIST/chrome"

# X 显示栈：按指纹复用；缓存未命中时从固定 Debian bookworm 源解析并打包。
log "4/7 X 显示栈（Xvfb / x11vnc / fluxbox，Debian ${DEBIAN_SUITE} 固定包集）"
DISPLAY_PKGS="${DISPLAY_PKGS:-$DEFAULT_DISPLAY_PKGS}"
FONT_PKGS="${FONT_PKGS:-$DEFAULT_FONT_PKGS}"
DISPLAY_WANT="$({
  printf 'suite=%s\nmirror=%s\nsecurity=%s\n' "$DEBIAN_SUITE" "$DEBIAN_MIRROR" "$DEBIAN_SECURITY_MIRROR"
  printf '%s\n' $DISPLAY_PKGS | sort
} | sha256sum | awk '{print $1}')"
FONT_WANT="$({ printf 'suite=%s\n' "$DEBIAN_SUITE"; printf '%s\n' $FONT_PKGS | sort; } | sha256sum | awk '{print $1}')"
DISPLAY_STAMP="$DIST/display/.display-version"
FONT_STAMP="$DIST/display/fonts/.fonts-version"
DISPLAY_HAVE=""; FONT_HAVE=""
[ -f "$DISPLAY_STAMP" ] && DISPLAY_HAVE="$(cat "$DISPLAY_STAMP" 2>/dev/null || true)"
[ -f "$FONT_STAMP" ] && FONT_HAVE="$(cat "$FONT_STAMP" 2>/dev/null || true)"
DISPLAY_CACHE_OK=0
[ -x "$DIST/display/usr/bin/Xvfb" ] && [ -x "$DIST/display/usr/bin/xkbcomp" ] && DISPLAY_CACHE_OK=1
FONTS_CACHE_OK=0
[ -d "$DIST/display/fonts" ] && [ -n "$(find "$DIST/display/fonts" -maxdepth 1 -type f \( -name '*.ttc' -o -name '*.ttf' -o -name '*.otf' \) -print -quit 2>/dev/null)" ] && FONTS_CACHE_OK=1

APT_STATE="$CACHE/apt-state"
APT_LISTS="$APT_STATE/lists"
APT_SOURCES="$APT_STATE/sources.list"
APT_ARCHIVES="$CACHE/apt-archives"
APT_EMPTY_PARTS="$CACHE/apt-empty-parts"
APT_SANDBOX_USER="$(id -un)"
mkdir -p "$APT_LISTS/partial" "$APT_ARCHIVES/partial" "$APT_STATE/log" "$APT_EMPTY_PARTS"

# Debian 源的签名密钥：Debian 宿主自带，Ubuntu runner 没有 —— 缺了 apt 会以
# "repository is not signed" 退出 100。这里显式定位 keyring 并用 signed-by 绑定；
# 实在找不到才降级 trusted=yes（并留下告警，不静默跳过校验）。
DEBIAN_KEYRING=""
for c in /usr/share/keyrings/debian-archive-keyring.gpg \
         /etc/apt/trusted.gpg.d/debian-archive-keyring.gpg; do
  [ -s "$c" ] && { DEBIAN_KEYRING="$c"; break; }
done
if [ -z "$DEBIAN_KEYRING" ]; then
  SUDO=""
  [ "$(id -u)" != "0" ] && command -v sudo >/dev/null 2>&1 && SUDO="sudo"
  echo "未找到 Debian 签名密钥，尝试用宿主 apt 安装 debian-archive-keyring"
  $SUDO apt-get install -y debian-archive-keyring >/dev/null 2>&1 || true
  for c in /usr/share/keyrings/debian-archive-keyring.gpg \
           /etc/apt/trusted.gpg.d/debian-archive-keyring.gpg; do
    [ -s "$c" ] && { DEBIAN_KEYRING="$c"; break; }
  done
fi
if [ -n "$DEBIAN_KEYRING" ]; then
  SIGNED_BY="[signed-by=$DEBIAN_KEYRING]"
  echo "使用 Debian 签名密钥：$DEBIAN_KEYRING"
else
  SIGNED_BY="[trusted=yes]"
  echo "::warning:: 找不到 Debian 签名密钥，回退为 trusted=yes（不校验镜像签名）"
fi

printf 'deb %s %s %s main\ndeb %s %s %s-updates main\ndeb %s %s %s-security main\n' \
  "$SIGNED_BY" "$DEBIAN_MIRROR" "$DEBIAN_SUITE" \
  "$SIGNED_BY" "$DEBIAN_MIRROR" "$DEBIAN_SUITE" \
  "$SIGNED_BY" "$DEBIAN_SECURITY_MIRROR" "$DEBIAN_SUITE" > "$APT_SOURCES"
APT_SOURCES_WANT="$(sha256sum "$APT_SOURCES" | awk '{print $1}')"
APT_OPTS=(
  -o "Dir::State::status=/dev/null"
  -o "Dir::State::lists=$APT_LISTS"
  -o "Dir::State::extended_states=/dev/null"
  -o "Dir::Etc::main=/dev/null"
  -o "Dir::Etc::parts=$APT_EMPTY_PARTS"
  -o "Dir::Etc::sourcelist=$APT_SOURCES"
  -o "Dir::Etc::sourceparts=$APT_EMPTY_PARTS"
  -o "Dir::Cache::archives=$APT_ARCHIVES"
  -o "Dir::Cache::pkgcache=/dev/null"
  -o "Dir::Cache::srcpkgcache=/dev/null"
  -o "Dir::Etc::preferences=/dev/null"
  -o "Dir::Etc::preferencesparts=$APT_EMPTY_PARTS"
  -o "Dir::Log=$APT_STATE/log"
  -o "APT::Sandbox::User=$APT_SANDBOX_USER"
  -o APT::Get::AllowUnauthenticated=false
)
APT_URI_OPTS=(-o APT::Get::Download-Only=true -o APT::Get::Print-URIs=true)

apt_indexes_present() {
  local f
  shopt -s nullglob
  local files=("$APT_LISTS"/*InRelease "$APT_LISTS"/*Packages*)
  shopt -u nullglob
  for f in "${files[@]}"; do
    [ -s "$f" ] && return 0
  done
  return 1
}

resolve_debs() {
  local out="$1"; shift
  local log_file="$CACHE/apt-resolve.log"
  if ! apt-get "${APT_OPTS[@]}" install -y --no-install-recommends "${APT_URI_OPTS[@]}" "$@" > "$log_file" 2>&1; then
    cat "$log_file" >&2
    echo "::error:: 无法解析 Debian 包依赖（mirror=$DEBIAN_MIRROR suite=$DEBIAN_SUITE packages=$*）" >&2
    return 1
  fi
  grep -oE "'[^']+\\.deb'" "$log_file" | tr -d "'" | sort -u > "$out"
  if [ ! -s "$out" ]; then
    echo "::error:: apt 未输出 .deb 下载地址（suite=$DEBIAN_SUITE packages=$*）" >&2
    cat "$log_file" >&2
    return 1
  fi
}

apt_update_indexes() {
  local stamp="$APT_STATE/.sources-version"
  local have=""
  [ -f "$stamp" ] && have="$(cat "$stamp" 2>/dev/null || true)"
  if [ "$have" != "$APT_SOURCES_WANT" ] || ! apt_indexes_present; then
    apt-get "${APT_OPTS[@]}" update
    printf '%s\n' "$APT_SOURCES_WANT" > "$stamp"
  fi
}

if [ "${OFFLINE:-0}" = "1" ] && { [ "$DISPLAY_HAVE" != "$DISPLAY_WANT" ] || [ "$DISPLAY_CACHE_OK" != "1" ] || [ "$FONT_HAVE" != "$FONT_WANT" ] || [ "$FONTS_CACHE_OK" != "1" ] || [ ! -f "$DIST/display/xkbcomp-shim.so" ]; }; then
  echo "OFFLINE=1 且显示/字体缓存缺失、损坏或指纹不符，拒绝离线打包过期内容" >&2
  exit 1
fi
if [ "${FORCE_REBUILD_DEPS:-0}" = "1" ]; then
  DISPLAY_HAVE=""; FONT_HAVE=""
fi

download_debs() {
  local list="$1"
  local url fn
  while read -r url; do
    fn="$DEBS/$(basename "$url")"
    [ -f "$fn" ] || curl -fsSL --retry 3 --retry-delay 5 --max-time 300 -o "$fn" "$url"
  done < "$list"
}

extract_debs() {
  local list="$1" stage="$2"
  rm -rf "$stage"; mkdir -p "$stage"
  local url
  while read -r url; do
    dpkg-deb -x "$DEBS/$(basename "$url")" "$stage"
  done < "$list"
  # 裁剪文档类内容（仅 Debian 根布局；Ubuntu runner 的 /usr/local 会被忽略）
  # usr/share/zoneinfo 一并裁掉：Xvfb/x11vnc/fluxbox 不需要时区库，宿主自带；
  # 而且 tzdata 里含大量目录型软链（如 posix/America -> ../America），
  # 与打包阶段的「软链物化」互相冲突。
  rm -rf "$stage/usr/share/doc" "$stage/usr/share/man" "$stage/usr/share/lintian" \
         "$stage/usr/share/bug" "$stage/usr/share/info" "$stage/usr/share/gettext" \
         "$stage/usr/share/locale" "$stage/usr/share/xml" "$stage/usr/share/zoneinfo" \
         "$stage/usr/bin/Xephyr" \
         "$stage/usr/bin/Xvfb.lock" "$stage/usr/share/applications" \
         "$stage/usr/share/mime" "$stage/usr/share/sgml" 2>/dev/null || true
}

if [ "${FORCE_REBUILD_DEPS:-0}" != "1" ] && [ "$DISPLAY_HAVE" = "$DISPLAY_WANT" ] && [ "$DISPLAY_CACHE_OK" = "1" ]; then
  echo "复用匹配的 X 组件缓存（$DISPLAY_HAVE）"
else
  apt_update_indexes
  resolve_debs "$CACHE/display-debs.txt" $DISPLAY_PKGS
  echo "X 组件需下载 $(wc -l < "$CACHE/display-debs.txt") 个 Debian 包"
  download_debs "$CACHE/display-debs.txt"
  extract_debs "$CACHE/display-debs.txt" "$CACHE/display-stage"
  rm -rf "$DIST/display"
  mkdir -p "$DIST/display"
  if [ -d "$CACHE/display-stage/usr" ]; then
    cp -a "$CACHE/display-stage/usr" "$DIST/display/"
  fi
  [ -x "$DIST/display/usr/bin/Xvfb" ] || { echo "::error:: Xvfb 未就位" >&2; exit 1; }
  [ -x "$DIST/display/usr/bin/xkbcomp" ] || { echo "::error:: xkbcomp 未就位（Xvfb 必需）" >&2; exit 1; }
  mkdir -p "$DIST/display/fonts"

  # 方案 B：按 ldd 精确闭包裁剪共享库（实测 913M → 108M，省约 805 MiB）。
  #
  # 为什么必须裁剪而不能只靠 apt 包白名单：xvfb/x11vnc/fluxbox 的传递依赖会
  # 把整条 Mesa GPU 栈拖进来 —— dri/ 驱动 320M + libLLVM-15 224M（软链物化出
  # 两份实体）+ libicu72 60M + libz3/libgs/libx265 等，合计约 805 MiB，
  # 而 X/VNC 与 Chromium 的 ldd 闭包从未加载其中任何一个。
  # Chromium 自带 libvk_swiftshader 做软件光栅化，不需要 llvmpipe。
  #
  # 保留 non-.so 内容（X11 xkb 数据、fluxbox 配置、字体索引等），只裁 .so；
  # dri/ 子目录除非被闭包命中否则整目录删除。
  PRUNE_STAGE="$CACHE/display-prune"
  rm -rf "$PRUNE_STAGE"; mkdir -p "$PRUNE_STAGE"
  cp -a "$DIST/display/usr" "$PRUNE_STAGE/"
  # 闭包目标：X/VNC 栈 + 完整 Chromium（headless_shell 已被删除）
  CHROME_BIN="$(find "$DIST/chrome" -maxdepth 3 -type f -path '*/chrome-linux/chrome' -print -quit 2>/dev/null || true)"
  # Xdummy 是 fnOS/小米系固件自带的虚拟 X 服务器，仅部分机型存在；
  # 构建机（Debian/Ubuntu runner）没有它，而闭包里也不需要它，
  # 因此只在目标机确实带上了才纳入闭包与硬门槛，否则会误报缺失。
  PRUNE_BINS=(Xvfb x11vnc fluxbox xkbcomp)
  [ -x "$DIST/display/usr/bin/Xdummy" ] && PRUNE_BINS+=(Xdummy)
  [ -n "$CHROME_BIN" ] && PRUNE_BINS+=("$CHROME_BIN")
  echo "按 ldd 闭包裁剪显示栈（目标：${PRUNE_BINS[*]}）"
  python3 "$ROOT/scripts/prune-display.py" "$PRUNE_STAGE" \
    --bins "${PRUNE_BINS[@]}" \
    --report "$DIST/display/.closure-sonames" \
    || { echo "::error:: 显示栈闭包裁剪失败" >&2; exit 1; }
  [ -d "$DIST/display/fonts" ] || mkdir -p "$DIST/display/fonts"
  rm -rf "$DIST/display/usr"
  cp -a "$PRUNE_STAGE/usr" "$DIST/display/"
  rm -rf "$PRUNE_STAGE"
  [ -x "$DIST/display/usr/bin/Xvfb" ] || { echo "::error:: 裁剪后 Xvfb 丢失" >&2; exit 1; }

  # 硬门槛：裁剪后所有目标二进制必须零缺失库，否则视为残包直接失败。
  # 这是「闭包裁剪」唯一的正确性保证 —— 不能靠包体变小就认为成功。
  #
  # 注意 ldd 的退出码：目标不是动态链接、依赖解析失败等情况下 ldd 会返回非 0。
  # 脚本整体是 `set -euo pipefail`，若把 ldd 直接放在命令替换里，非 0 会让
  # 整段在赋值处静默中止（CI 上就是这样：前 4 个 ✓ 打完后直接退出，看不出原因）。
  # 因此先落盘再过滤，并对 ldd 自身的失败单独处理。
  LD_PATH="$DIST/display/usr/lib/x86_64-linux-gnu:$DIST/display/usr/lib"
  PRUNE_FAIL=0
  for bin in "${PRUNE_BINS[@]}"; do
    case "$bin" in
      /*) bp="$bin" ;;
      *)  bp="$DIST/display/usr/bin/$bin" ;;
    esac
    if [ ! -e "$bp" ]; then
      echo "::error:: 裁剪后目标缺失：$bp" >&2
      PRUNE_FAIL=1
      continue
    fi
    ldd_out="$CACHE/ldd-out.txt"
    if ! env LD_LIBRARY_PATH="$LD_PATH" ldd "$bp" > "$ldd_out" 2>/dev/null; then
      # ldd 本身失败（非常规 ELF / 解析器缺失等）。不静默放过，但与缺库区分开：
      echo "  ! $(basename "$bp"): ldd 返回非 0（$(tr -d '\n' < "$ldd_out" | head -c 80)）"
      PRUNE_FAIL=1
      continue
    fi
    missing="$(awk '/not found/ {print $1}' "$ldd_out" | tr '\n' ' ')"
    if [ -n "$missing" ]; then
      echo "::error:: $(basename "$bp") 缺库：$missing" >&2
      PRUNE_FAIL=1
    else
      echo "  ✓ $(basename "$bp") 依赖完整"
    fi
  done
  if [ "$PRUNE_FAIL" != "0" ]; then
    echo "::error:: 显示栈裁剪后存在缺失依赖，构建中止（拒绝打出装不起来的包）" >&2
    exit 1
  fi
  echo "显示栈裁剪后：$(du -sh "$DIST/display" | cut -f1)"
  printf '%s\n' "$DISPLAY_WANT" > "$DISPLAY_STAMP"
fi

# ─────────────────────────────────────────────────────────────
log "5/7 Xvfb xkbcomp 垫片（宿主 /usr/bin 无 xkbcomp）"
gcc -shared -fPIC -O2 -o "$DIST/display/xkbcomp-shim.so" "$ROOT/scripts/xkbcomp-shim.c" -ldl
echo "xkbcomp-shim.so: $(stat -c %s "$DIST/display/xkbcomp-shim.so") bytes"

# ─────────────────────────────────────────────────────────────
log "6/7 中文字体（文泉驿 + Noto Emoji）"
if [ "${FORCE_REBUILD_DEPS:-0}" != "1" ] && [ "$FONT_HAVE" = "$FONT_WANT" ] && [ "$FONTS_CACHE_OK" = "1" ]; then
  echo "复用匹配的字体缓存（$FONT_HAVE）"
else
  apt_update_indexes
  resolve_debs "$CACHE/font-debs.txt" $FONT_PKGS
  download_debs "$CACHE/font-debs.txt"
  extract_debs "$CACHE/font-debs.txt" "$CACHE/font-stage"
  mkdir -p "$DIST/display/fonts"
  find "$CACHE/font-stage/usr/share/fonts" -type f \( -name '*.ttc' -o -name '*.ttf' -o -name '*.otf' \) \
    -exec cp -a {} "$DIST/display/fonts/" \;
  [ -n "$(find "$DIST/display/fonts" -maxdepth 1 -type f \( -name '*.ttc' -o -name '*.ttf' -o -name '*.otf' \) -print -quit)" ] || { echo "::error:: 字体准备失败" >&2; exit 1; }
  printf '%s\n' "$FONT_WANT" > "$FONT_STAMP"
fi
ls -la "$DIST/display/fonts/"

# ─────────────────────────────────────────────────────────────
log "7/7 noVNC（noVNC 1.5.0 静态资源）"
NV_TGZ="$CACHE/novnc.tar.gz"
NV_STAMP="$DIST/novnc/.novnc-version"
NV_WANT="1.5.0"
NV_HAVE=""
[ -f "$NV_STAMP" ] && NV_HAVE="$(cat "$NV_STAMP" 2>/dev/null || true)"
if [ "${FORCE_REBUILD_DEPS:-0}" = "1" ]; then NV_HAVE=""; fi
if [ "${OFFLINE:-0}" = "1" ] && { [ "$NV_HAVE" != "$NV_WANT" ] || [ ! -f "$DIST/novnc/vnc.html" ]; }; then
  echo "OFFLINE=1 且 noVNC 缓存缺失或版本不符，中止" >&2
  exit 1
fi
if [ "${FORCE_REBUILD_DEPS:-0}" != "1" ] && [ "$NV_HAVE" = "$NV_WANT" ] && [ -f "$DIST/novnc/vnc.html" ]; then
  echo "noVNC 缓存有效（$NV_WANT）"
else
  if [ ! -s "$NV_TGZ" ]; then
    curl -fsSL --retry 3 --max-time 300 -o "$NV_TGZ" \
      "https://codeload.github.com/novnc/noVNC/tar.gz/refs/tags/v${NV_WANT}"
  fi
  NV_TOP="$(tar tzf "$NV_TGZ" | awk 'NR==1 { sub("/.*", ""); top=$0 } END { print top }')"
  [ -n "$NV_TOP" ] || { echo "noVNC 源码包损坏" >&2; exit 1; }
  rm -rf "$CACHE/$NV_TOP" "$DIST/novnc"
  tar xzf "$NV_TGZ" -C "$CACHE"
  mkdir -p "$DIST/novnc"
  cp -a "$CACHE/$NV_TOP/." "$DIST/novnc/"
  rm -rf "$DIST/novnc/tests" "$DIST/novnc/docs" "$DIST/novnc/.github" "$DIST/novnc/po"
  [ -f "$DIST/novnc/vnc.html" ] || { echo "noVNC 资源不完整" >&2; exit 1; }
  printf '%s\n' "$NV_WANT" > "$NV_STAMP"
fi

printf '\n全部依赖准备完成：\n'
du -sh "$DIST"/deps "$DIST"/chrome "$DIST"/display "$DIST"/novnc 2>/dev/null
