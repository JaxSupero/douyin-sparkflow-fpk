#!/bin/bash
# 端到端复刻 CI 的 X 组件解析（隔离 apt），确认整份包列表都能解出 .deb URL。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

CACHE="$ROOT/.cache"
APT_STATE="$CACHE/apt-state-verify"
APT_LISTS="$APT_STATE/lists"
APT_SOURCES="$APT_STATE/sources.list"
APT_ARCHIVES="$CACHE/apt-archives-verify"
EMPTY="$CACHE/apt-empty-verify"
mkdir -p "$APT_LISTS/partial" "$APT_ARCHIVES/partial" "$EMPTY" "$APT_STATE/log"

DEBIAN_SUITE=bookworm
DEBIAN_MIRROR=https://mirrors.ustc.edu.cn/debian
DEBIAN_SECURITY_MIRROR=https://mirrors.ustc.edu.cn/debian-security
DEBIAN_KEYRING=/usr/share/keyrings/debian-archive-keyring.gpg

PKGS="xvfb x11vnc fluxbox xkb-data x11-common libxfont2 libimlib2 libspectre1 libvncclient1 libvncserver1 x11-xkb-utils"
FONTS="fonts-wqy-zenhei fonts-wqy-microhei fonts-noto-color-emoji"

printf 'deb [signed-by=%s] %s %s main\ndeb [signed-by=%s] %s %s-updates main\ndeb [signed-by=%s] %s %s-security main\n' \
  "$DEBIAN_KEYRING" "$DEBIAN_MIRROR" "$DEBIAN_SUITE" \
  "$DEBIAN_KEYRING" "$DEBIAN_MIRROR" "$DEBIAN_SUITE" \
  "$DEBIAN_KEYRING" "$DEBIAN_SECURITY_MIRROR" "$DEBIAN_SUITE" > "$APT_SOURCES"

OPTS=(
  -o "Dir::State::status=/dev/null"
  -o "Dir::State::lists=$APT_LISTS"
  -o "Dir::State::extended_states=/dev/null"
  -o "Dir::Etc::main=/dev/null"
  -o "Dir::Etc::parts=$EMPTY"
  -o "Dir::Etc::sourcelist=$APT_SOURCES"
  -o "Dir::Etc::sourceparts=$EMPTY"
  -o "Dir::Cache::archives=$APT_ARCHIVES"
  -o "Dir::Cache::pkgcache=/dev/null"
  -o "Dir::Cache::srcpkgcache=/dev/null"
  -o "Dir::Etc::preferences=/dev/null"
  -o "Dir::Etc::preferencesparts=$EMPTY"
  -o "Dir::Log=$APT_STATE/log"
  -o "APT::Sandbox::User=$(id -un)"
  -o APT::Get::AllowUnauthenticated=false
)
URI=(-o APT::Get::Download-Only=true -o APT::Get::Print-URIs=true)

echo "--- apt update（隔离） ---"
apt-get "${OPTS[@]}" update > "$APT_STATE/update.log" 2>&1 && echo "update OK" || { tail -5 "$APT_STATE/update.log"; exit 1; }

echo "--- 解析 X 组件 ---"
apt-get "${OPTS[@]}" install -y --no-install-recommends "${URI[@]}" $PKGS > "$APT_STATE/x.log" 2>&1 || { tail -8 "$APT_STATE/x.log"; exit 1; }
grep -oE "'[^']+\.deb'" "$APT_STATE/x.log" | tr -d "'" | sort -u > "$APT_STATE/x.txt"
echo "X 组件依赖包数：$(wc -l < "$APT_STATE/x.txt")"

echo "--- 解析字体 ---"
apt-get "${OPTS[@]}" install -y --no-install-recommends "${URI[@]}" $FONTS > "$APT_STATE/f.log" 2>&1 || { tail -8 "$APT_STATE/f.log"; exit 1; }
grep -oE "'[^']+\.deb'" "$APT_STATE/f.log" | tr -d "'" | sort -u > "$APT_STATE/f.txt"
echo "字体依赖包数：$(wc -l < "$APT_STATE/f.txt")"

echo
echo "=== 关键校验 ==="
XDEB=$(grep -c 'xkb-utils' "$APT_STATE/x.txt" || true)
echo "含 xkb-utils 的包：$XDEB"
grep 'xkb-utils' "$APT_STATE/x.txt" | sed 's/^/  /'
echo "含 xvfb 的包：$(grep -c 'xvfb' "$APT_STATE/x.txt" || true)"
grep 'xvfb' "$APT_STATE/x.txt" | sed 's/^/  /'
echo "含 wqy 的包：$(grep -cE 'wqy' "$APT_STATE/f.txt" || true)"
grep -E 'wqy' "$APT_STATE/f.txt" | sed 's/^/  /'
