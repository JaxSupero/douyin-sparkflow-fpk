#!/usr/bin/env python3
"""抖音 SparkFlow — fnOS 原生部署引导脚本（幂等）。

职责：
1. 使用应用中心 python312 运行时，在 ${TRIM_PKGVAR}/venv 里物化一个轻量 venv
   （bin/python 符号链接 + pyvenv.cfg）并把随包依赖（dist/deps）复制进 site-packages；
   依赖版本指纹变化或 venv 缺失时自动重建。
2. 修复运行目录中来自旧 Docker 版部署的历史路径配置
   （config.json / webui_settings.json），只迁移“已知旧默认值”，不动用户自定义。

用法：bootstrap.py   （要求已导出 TRIM_APPDEST / TRIM_PKGVAR）
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

PY312_CANDIDATES = (
    "/var/apps/python312/target/bin/python3",
    "/var/apps/python312/target/bin/python3.12",
)


def log(msg: str) -> None:
    print(f"[bootstrap] {msg}", flush=True)


def err(msg: str) -> None:
    print(f"[bootstrap][ERROR] {msg}", file=sys.stderr, flush=True)


def find_python() -> str | None:
    override = os.environ.get("DOUYIN_PYTHON_BIN", "").strip()
    if override and os.access(override, os.X_OK):
        return override
    for cand in PY312_CANDIDATES:
        if os.access(cand, os.X_OK):
            return cand
    return None


def python_xy(py: str) -> str:
    out = subprocess.check_output(
        [py, "-c", "import sys;print(f'{sys.version_info[0]}.{sys.version_info[1]}')"],
        text=True,
    )
    return out.strip()


def build_venv(py: str, venv: Path, deps: Path) -> None:
    ver = python_xy(py)
    log(f"rebuilding venv for python{ver}")
    if venv.exists():
        shutil.rmtree(venv)
    bindir = venv / "bin"
    site = venv / "lib" / f"python{ver}" / "site-packages"
    bindir.mkdir(parents=True, exist_ok=True)
    site.mkdir(parents=True, exist_ok=True)
    for name in ("python", "python3"):
        link = bindir / name
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(py)
    (venv / "pyvenv.cfg").write_text(
        "home = {}\ninclude-system-site-packages = false\nversion = {}\n".format(
            Path(py).parent, ver
        ),
        encoding="utf-8",
    )
    if not deps.exists():
        raise RuntimeError(f"bundled deps missing: {deps}")
    for item in sorted(deps.iterdir()):
        if item.name in (".deps-version", "bin"):
            continue
        target = site / item.name
        if item.is_dir():
            shutil.copytree(item, target, symlinks=True)
        else:
            shutil.copy2(item, target)
    # console 脚本（websockify 等）：shebang 在构建期已改写为 /usr/bin/env python3
    dep_bin = deps / "bin"
    if dep_bin.is_dir():
        for item in sorted(dep_bin.iterdir()):
            if item.is_file():
                target = bindir / item.name
                shutil.copy2(item, target)
                os.chmod(target, 0o755)


def ensure_venv(appdest: Path, pkgvar: Path) -> None:
    py = find_python()
    if py is None:
        raise RuntimeError(
            "未找到应用中心 Python 3.12 运行时（/var/apps/python312）。"
            "请先在应用中心安装「Python 3.12」应用。"
        )
    venv = pkgvar / "venv"
    deps = appdest / "dist" / "deps"
    want = "unknown"
    marker_src = deps / ".deps-version"
    if marker_src.exists():
        want = marker_src.read_text(encoding="utf-8").strip()
    marker = venv / ".deps-version"
    have = marker.read_text(encoding="utf-8").strip() if marker.exists() else ""
    if (venv / "bin" / "python").exists() and have == want:
        return
    build_venv(py, venv, deps)
    marker.write_text(want + "\n", encoding="utf-8")
    log("venv ready")


def patch_json(path: Path, mutate, *, create: dict | None = None) -> None:
    data: dict
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log(f"WARN: cannot parse {path}: {exc}; leaving it untouched")
            return
        if not isinstance(data, dict):
            return
    elif create is not None:
        data = create
    else:
        return
    if mutate(data):
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        log(f"updated {path.name}")


def fix_config_json(runtime: Path, pkgvar: Path) -> None:
    target_root = str(pkgvar / "browser-profiles")

    def mutate(cfg: dict) -> bool:
        changed = False
        pbp = cfg.get("persistentBrowserProfiles")
        if not isinstance(pbp, dict):
            pbp = {}
            cfg["persistentBrowserProfiles"] = pbp
            changed = True
        root = str(pbp.get("root") or "").strip()
        if (
            not root
            or root.startswith("/opt/douyin-sparkflow")
            or root.startswith("/app/")
            or root == "/opt/douyin-sparkflow/state/browser-profiles"
        ):
            pbp["root"] = target_root
            changed = True
        return changed

    cfg_path = runtime / "config.json"
    create = None
    if not cfg_path.exists():
        example = runtime / "config.example.json"
        if example.exists():
            try:
                create = json.loads(example.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                create = None
    patch_json(cfg_path, mutate, create=create)


def fix_app_settings(runtime: Path, pkgvar: Path) -> None:
    want_ops_log = str(pkgvar / "runtime" / "logs" / "douyin-sparkflow.log")
    want_api = "http://127.0.0.1:" + os.environ.get("LOGIN_DESKTOP_API_PORT", "18090")

    def mutate(st: dict) -> bool:
        changed = False
        ops = str(st.get("ops_log_file") or "")
        if ops.startswith("/app/") or ops == "/var/log/douyin-sparkflow.log":
            st["ops_log_file"] = want_ops_log
            changed = True
        compose = str(st.get("compose_root") or "")
        if compose.startswith("/opt/douyin-sparkflow"):
            st["compose_root"] = ""
            changed = True
        api = str(st.get("login_desktop_api_url") or "")
        if "login-desktop:" in api or api.startswith("/"):
            st["login_desktop_api_url"] = want_api
            changed = True
        proxy = str(st.get("douyin_proxy_url") or "")
        if proxy in ("", "http://proxy:7890"):
            st["douyin_proxy_url"] = "http://127.0.0.1:7890"
            changed = True
        return changed

    patch_json(runtime / "webui_settings.json", mutate, create={})


def ensure_dirs(pkgvar: Path) -> None:
    for rel in (
        "runtime/logs",
        "state",
        "browser-profiles",
        "login-profile",
        "cron",
        "run",
        "logs",
        "home/.local/share/fonts",
        "home/.cache",
        "home/.config",
    ):
        (pkgvar / rel).mkdir(parents=True, exist_ok=True)


def check_writable(pkgvar: Path) -> None:
    probe = pkgvar / ".write-probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise RuntimeError(
            f"应用数据目录不可写（{pkgvar}）：{exc}。"
            f"若从旧 Docker 版应用升级，请以 root 在 NAS 终端执行："
            f" chown -R douyin_sparkflow:douyin_sparkflow '{pkgvar}'"
        )


def copy_fonts(appdest: Path, pkgvar: Path) -> None:
    src = appdest / "dist" / "display" / "fonts"
    dst = pkgvar / "home" / ".local" / "share" / "fonts"
    if not src.is_dir() or not dst.is_dir():
        return
    if any(dst.iterdir()):
        return
    n = 0
    for item in src.iterdir():
        if item.is_file():
            shutil.copy2(item, dst / item.name)
            n += 1
    if n:
        log(f"fonts installed ({n})")


def main() -> int:
    appdest = Path(os.environ.get("TRIM_APPDEST", ""))
    pkgvar = Path(os.environ.get("TRIM_PKGVAR", ""))
    if not appdest.is_dir() or not pkgvar.is_dir():
        err(f"TRIM_APPDEST/TRIM_PKGVAR invalid: {appdest} / {pkgvar}")
        return 1
    runtime = pkgvar / "runtime"

    try:
        check_writable(pkgvar)
        ensure_dirs(pkgvar)
        ensure_venv(appdest, pkgvar)
        fix_config_json(runtime, pkgvar)
        fix_app_settings(runtime, pkgvar)
        copy_fonts(appdest, pkgvar)
    except Exception as exc:  # noqa: BLE001 - 引导失败必须回传明确原因
        err(str(exc))
        return 1
    log("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
