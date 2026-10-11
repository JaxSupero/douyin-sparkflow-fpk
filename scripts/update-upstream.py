#!/usr/bin/env python3
"""Pin one upstream commit and bump the FPK patch version, idempotently."""
from __future__ import annotations

import argparse
import re
from pathlib import Path
from datetime import date


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("sha")
    parser.add_argument("date")
    parser.add_argument("pin", type=Path)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()

    if not re.fullmatch(r"[0-9a-f]{40}", args.sha):
        parser.error("sha must be a 40-character lowercase commit hash")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.date):
        parser.error("date must use YYYY-MM-DD format")
    try:
        date.fromisoformat(args.date)
    except ValueError:
        parser.error("date is not a valid calendar date")

    old = {}
    if args.pin.exists():
        for line in args.pin.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                old[key] = value

    if old.get("UPSTREAM_SHA") == args.sha:
        with args.output.open("a", encoding="utf-8") as output:
            output.write("changed=false\n")
        print("Upstream unchanged; no update or build dispatch needed.")
        return 0

    text = args.manifest.read_text(encoding="utf-8")
    match = re.search(r"(?m)^version\s*=\s*(\d+)\.(\d+)\.(\d+)\s*$", text)
    if not match:
        raise SystemExit("manifest must contain exactly one X.Y.Z version field")
    if len(re.findall(r"(?m)^version\s*=", text)) != 1:
        raise SystemExit("manifest must contain exactly one version field")

    major, minor, patch = map(int, match.groups())
    next_version = f"{major}.{minor}.{patch + 1}"
    updated_manifest = text[: match.start()] + f"version               = {next_version}" + text[match.end() :]

    args.pin.parent.mkdir(parents=True, exist_ok=True)
    pin_tmp = args.pin.with_suffix(args.pin.suffix + ".tmp")
    pin_tmp.write_text(
        f"UPSTREAM_SHA={args.sha}\nUPSTREAM_DATE={args.date}\n",
        encoding="utf-8",
    )
    manifest_tmp = args.manifest.with_suffix(args.manifest.suffix + ".tmp")
    manifest_tmp.write_text(updated_manifest, encoding="utf-8")
    pin_tmp.replace(args.pin)
    manifest_tmp.replace(args.manifest)

    with args.output.open("a", encoding="utf-8") as output:
        output.write("changed=true\n")
        output.write(f"new_version={next_version}\n")
    print(f"Updated upstream pin to {args.sha}; FPK version -> {next_version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
