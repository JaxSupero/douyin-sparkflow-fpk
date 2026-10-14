#!/usr/bin/env python3
"""对上游源码副本应用 fnOS 原生部署微补丁（严格锚点校验，缺失即失败）。

补丁清单（全部为“环境变量开关 + 默认值不变”的最小侵入式改动）：

1. webui/ops.py
   - HOST_CRONTAB_PATH / CRON_LOG_PATH 支持环境变量覆盖；
   - 新增 host_spool_enabled()，将“容器内才读写 spool”的判定扩展为
     “容器内 或 显式指定了 spool 路径（fnOS 原生部署）”。
2. utils/config.py
   - DEFAULT_APP_SETTINGS.ops_log_file 支持 SPARKFLOW_OPS_LOG_FILE 覆盖。
3. webui/login_lock.py
   - 登录工作区锁文件目录支持 SPARKFLOW_STATE_DIR 覆盖（默认 /opt 不变）。
4. webui/app.py
   - public_app_settings() 的 allowed_keys 补上 douyin_network_mode /
     douyin_proxy_url。上游漏暴露这两个键，而 settings.html 的「系统」表单与
     POST /settings 都在用它们：保存其实已落盘，但页面永远回显默认值，
     用户看到的现象是「网络出口/Mihomo 代理地址改不了」；更糟的是再点一次
     保存会把默认值写回去，静默覆盖用户设置。
5. core/browser.py
   - launch/launch_persistent_context 显式加 channel="chromium"。
     Playwright 1.49+ 的 headless=True 默认走 chromium_headless_shell 二进制；
     FPK 为省 323 MiB 已删除该副本（完整 Chromium 自带 libvk_swiftshader，
     无头渲染不受影响），不加 channel 会直接报 "Executable doesn't exist"。
     只在 headless 时指定，有头登录桌面走完整 Chromium 本来就无需该参数。

用法：
  patch-src.py <src_dir>          # 就地修改 <src_dir>（构建时对副本执行）
  patch-src.py <src_dir> --check  # 只校验锚点是否匹配，不写入
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def replace_once(text: str, old: str, new: str, *, file: str, expect: int = 1) -> str:
    count = text.count(old)
    if count != expect:
        raise SystemExit(
            f"[patch] FAIL {file}: 期望匹配 {expect} 处，实际 {count} 处；"
            f"上游代码可能已变化，请更新 patch-src.py 的锚点。\n锚点片段:\n{old}"
        )
    return text.replace(old, new)


PATCHES: dict[str, list[tuple[str, str, int]]] = {
    "webui/ops.py": [
        (
            'HOST_CRONTAB_PATH = Path("/host-spool-cron/root")',
            'HOST_CRONTAB_PATH = Path(os.getenv("SPARKFLOW_HOST_CRONTAB_PATH") or "/host-spool-cron/root")',
            1,
        ),
        (
            'def running_in_container():\n    return Path("/.dockerenv").exists()\n',
            'def running_in_container():\n    return Path("/.dockerenv").exists()\n'
            '\n'
            '\n'
            'def host_spool_enabled():\n'
            '    """fnOS 原生部署（FPK）：显式配置了 spool 路径时启用宿主 spool 行为。"""\n'
            '    return running_in_container() or bool(os.getenv("SPARKFLOW_HOST_CRONTAB_PATH"))\n',
            1,
        ),
        (
            "running_in_container() and HOST_CRONTAB_PATH",
            "host_spool_enabled() and HOST_CRONTAB_PATH",
            4,
        ),
        (
            'CRON_LOG_PATH = "/app/logs/douyin-sparkflow.log"',
            'CRON_LOG_PATH = os.getenv("SPARKFLOW_CRON_LOG_PATH") or "/app/logs/douyin-sparkflow.log"',
            1,
        ),
    ],
    "utils/config.py": [
        (
            '"ops_log_file": "/app/logs/douyin-sparkflow.log",',
            '"ops_log_file": os.getenv("SPARKFLOW_OPS_LOG_FILE") or "/app/logs/douyin-sparkflow.log",',
            1,
        ),
    ],
    "webui/login_lock.py": [
        (
            '        Path("/opt/douyin-sparkflow/state"),',
            '        Path(os.getenv("SPARKFLOW_STATE_DIR") or "/opt/douyin-sparkflow/state"),',
            1,
        ),
    ],
    "webui/app.py": [
        (
            '        "login_desktop_public_port",\n'
            '    )\n'
            '    return {key: settings.get(key) for key in allowed_keys}',
            '        "login_desktop_public_port",\n'
            '        "douyin_network_mode",\n'
            '        "douyin_proxy_url",\n'
            '    )\n'
            '    return {key: settings.get(key) for key in allowed_keys}',
            1,
        ),
    ],
    "core/browser.py": [
        (
            'def _browser_launch_options(GUI=False, network_mode=None):\n'
            '    args = _browser_args()\n'
            '    proxy = _douyin_browser_proxy(network_mode=network_mode)\n'
            '    if proxy:\n'
            '        return {\n'
            '            "headless": _headless_for(GUI),\n'
            '            "args": args,\n'
            '            "proxy": {"server": proxy},\n'
            '        }\n'
            '    args.append("--no-proxy-server")\n'
            '    return {\n'
            '        "headless": _headless_for(GUI),\n'
            '        "args": args,\n'
            '    }\n',
            'def _browser_launch_options(GUI=False, network_mode=None):\n'
            '    args = _browser_args()\n'
            '    proxy = _douyin_browser_proxy(network_mode=network_mode)\n'
            '    # fnOS FPK 精简包删掉了 chromium_headless_shell（省约 323 MiB）。\n'
            '    # Playwright 1.49+ 的 headless=True 默认去启动那个二进制，删掉后会\n'
            '    # 报 "Executable doesn\'t exist"。channel="chromium" 让无头模式改用\n'
            '    # 完整 Chromium；它自带 libvk_swiftshader 做软件光栅化，无需 headless_shell。\n'
            '    # 有头（登录桌面）本来就用完整 Chromium，不受影响。\n'
            '    headless = _headless_for(GUI)\n'
            '    options = {"headless": headless, "args": args}\n'
            '    if headless:\n'
            '        options["channel"] = "chromium"\n'
            '    if proxy:\n'
            '        options["proxy"] = {"server": proxy}\n'
            '    else:\n'
            '        args.append("--no-proxy-server")\n'
            '    return options\n',
            1,
        ),
    ],
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("src_dir", type=Path)
    ap.add_argument("--check", action="store_true", help="仅校验，不写入")
    args = ap.parse_args()

    total = 0
    for rel, ops in PATCHES.items():
        path = args.src_dir / rel
        if not path.is_file():
            raise SystemExit(f"[patch] FAIL: 缺少文件 {path}")
        text = original = path.read_text(encoding="utf-8")
        for old, new, expect in ops:
            text = replace_once(text, old, new, file=rel, expect=expect)
            total += expect
        if text != original and not args.check:
            path.write_text(text, encoding="utf-8")
        print(f"[patch] OK {rel} ({len(ops)} 组替换)")
    mode = "检查" if args.check else "应用"
    print(f"[patch] {mode}完成，共 {total} 处替换。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
