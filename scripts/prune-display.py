#!/usr/bin/env python3
"""按精确 .so 闭包裁剪已解包的 Debian 树。

为什么需要这一步（实测教训）：
  只用「apt 包白名单」不够 —— libgl1 这个 Debian 包 Depends 会拉进
  libgl1-mesa-dri / libllvm15 / libicu72 / libz3-4 等 169 个包、约 840MB。
  但 ldd 闭包显示 Xvfb/x11vnc/fluxbox/chrome 只需要 libGL.so.1 这一层，
  从不加载 dri 驱动或 libLLVM。

正确策略：
  1. apt 解析 → 解包（保留完整依赖树，保证 ldd 解析不受缺库影响）
  2. 用闭包清单裁剪：只保留闭包命中的 .so + 所有非 .so 文件（X11 数据/字体/
     fluxbox 配置等）；闭包外的 .so 直接删除
  3. 保留 dri/ 仅当闭包命中（Chromium 自带 SwiftShader，通常命中 0）
"""
from __future__ import annotations
import argparse
import os
import subprocess
import sys
from pathlib import Path


def ldd_paths(binary: Path, ld_path: str) -> tuple[list[str], list[str]]:
    env = dict(os.environ)
    if ld_path:
        env["LD_LIBRARY_PATH"] = ld_path
    else:
        env.pop("LD_LIBRARY_PATH", None)
    try:
        res = subprocess.run(["ldd", str(binary)], capture_output=True,
                             text=True, env=env, timeout=90)
    except Exception:
        return [], []
    ok, bad = [], []
    for line in res.stdout.splitlines():
        line = line.strip()
        if "=>" not in line:
            continue
        parts = line.split("=>")
        soname = parts[0].strip()
        target = parts[1].strip().split("(")[0].strip()
        if target == "not found":
            bad.append(soname)
        elif target.startswith("/"):
            ok.append(target)
    return ok, bad


def is_elf(path: Path) -> bool:
    """只有动态链接 ELF 才值得跑 ldd。

    Debian 的 xserver-xdummy 提供的 Xdummy 是 shell 脚本包装器，
    对它跑 ldd 恒返回非 0；不先判 ELF 会把「不是 ELF」误报成「闭包失败」。
    """
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"\x7fELF"
    except OSError:
        return False


def compute_keep(stage: Path, bins: list[Path]) -> tuple[set[str], list[str], set[str]]:
    """返回 (保留的 .so 绝对路径集合, not found 列表, 解析到的 soname 集合)"""
    keep: set[str] = set()
    missing: list[str] = []
    sonames: set[str] = set()
    queue = list(bins)
    visited: set[str] = set()
    while queue:
        f = queue.pop()
        rf = str(f.resolve())
        if rf in visited:
            continue
        visited.add(rf)
        if not is_elf(f):
            print(f"[skip] {f.name}: 非 ELF，不做 ldd 依赖分析", file=sys.stderr)
            continue
        ok, bad = ldd_paths(f, f"{stage}/usr/lib/x86_64-linux-gnu:{stage}/usr/lib")
        missing.extend(bad)
        for p in ok:
            sonames.add(Path(p).name)
            rp = os.path.realpath(p)
            keep.add(rp)
            if rp not in visited:
                queue.append(Path(rp))
    return keep, missing, sonames


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", help="已解包的 Debian 根目录")
    ap.add_argument("--bins", nargs="+", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report", default=None)
    args = ap.parse_args()

    stage = Path(args.stage).resolve()
    bins = []
    for b in args.bins:
        p = Path(b)
        if not p.exists():
            p = stage / "usr/bin" / b
        if not p.exists():
            print(f"[error] 缺 {b}", file=sys.stderr)
            return 1
        bins.append(p.resolve())

    keep, missing, sonames = compute_keep(stage, bins)
    print(f"闭包: 保留 {len(keep)} 个 .so, not found {len(missing)}, soname {len(sonames)}")
    if missing:
        print("  not found:", ", ".join(sorted(set(missing))))

    libdir = stage / "usr/lib/x86_64-linux-gnu"
    removed_files = 0
    removed_bytes = 0
    kept_bytes = 0
    removed_dirs: list[str] = []

    if libdir.is_dir():
        for p in sorted(libdir.rglob("*"), reverse=True):
            if p.is_symlink():
                # 软链：保留指向保留文件的；否则删
                tgt = os.path.realpath(p)
                if tgt not in keep and tgt.startswith(str(stage)):
                    if not args.dry_run:
                        p.unlink()
                    removed_files += 1
                continue
            if not p.is_file():
                continue
            if ".so" not in p.name:
                continue                       # 非 .so（gconv/、dri/ 内 .so 另算）
            if str(p.resolve()) in keep:
                kept_bytes += p.stat().st_size
                continue
            removed_bytes += p.stat().st_size
            removed_files += 1
            if not args.dry_run:
                p.unlink()
        # 清理空目录（如 dri/）
        for d in sorted(libdir.rglob("*"), reverse=True):
            if d.is_dir() and not any(d.iterdir()):
                removed_dirs.append(str(d.relative_to(stage)))
                if not args.dry_run:
                    d.rmdir()

    # dri/ 逐文件判定（Chromium 用 SwiftShader，通常全部删除）
    dri = libdir / "dri"
    dri_removed = dri_bytes = 0
    if dri.is_dir():
        for p in dri.rglob("*"):
            if p.is_file() and str(p.resolve()) not in keep:
                dri_bytes += p.stat().st_size
                dri_removed += 1
                if not args.dry_run:
                    p.unlink()
        for d in sorted(dri.rglob("*"), reverse=True):
            if d.is_dir() and not any(d.iterdir()):
                if not args.dry_run:
                    d.rmdir()

    print(f"\n裁剪结果{'（dry-run）' if args.dry_run else ''}:")
    print(f"  删除 .so 文件: {removed_files} 个 / {removed_bytes/1024/1024:.1f} MiB")
    print(f"  保留 .so      : {kept_bytes/1024/1024:.1f} MiB")
    print(f"  dri/ 内删除   : {dri_removed} 个 / {dri_bytes/1024/1024:.1f} MiB")
    if removed_dirs:
        print(f"  清理空目录    : {len(removed_dirs)} 个")
    print(f"  stage 现体积  : {sum(f.stat().st_size for f in stage.rglob('*') if f.is_file())/1024/1024:.1f} MiB")

    if args.report:
        Path(args.report).write_text(
            "\n".join(sorted({Path(p).name for p in keep})) + "\n")
        print(f"  闭包清单写入 {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())