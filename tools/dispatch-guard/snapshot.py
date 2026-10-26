#!/usr/bin/env python3
"""Create a lightweight, portable snapshot of a directory tree."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


MAX_HASH_SIZE = 50 * 1024 * 1024
DEFAULT_IGNORED_DIRS = {
    "node_modules",
    ".git",
    "dist",
    "dist-electron",
    "__pycache__",
    ".venv",
}


def _matches(path: str, patterns: Iterable[str]) -> bool:
    normalized = path.replace("\\", "/")
    return any(fnmatch.fnmatch(normalized, pattern.replace("\\", "/")) for pattern in patterns)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def take_snapshot(root: Path | str, ignore: Iterable[str] = ()) -> dict:
    root_path = Path(root).expanduser().resolve(strict=True)
    if not root_path.is_dir():
        raise NotADirectoryError(f"Không phải thư mục: {root_path}")

    ignore_patterns = list(ignore)
    files: dict[str, dict[str, int | float | str]] = {}
    for current, dirnames, filenames in os.walk(root_path):
        current_path = Path(current)
        kept_dirs = []
        for dirname in dirnames:
            candidate = current_path / dirname
            relative = candidate.relative_to(root_path).as_posix()
            if dirname in DEFAULT_IGNORED_DIRS or _matches(relative, ignore_patterns) or _matches(relative + "/", ignore_patterns):
                continue
            kept_dirs.append(dirname)
        dirnames[:] = kept_dirs

        for filename in filenames:
            path = current_path / filename
            relative = path.relative_to(root_path).as_posix()
            if _matches(relative, ignore_patterns):
                continue
            try:
                stat = path.stat()
                file_hash = _hash_file(path) if stat.st_size <= MAX_HASH_SIZE else "SKIPPED_LARGE"
            except (FileNotFoundError, PermissionError, OSError) as exc:
                raise RuntimeError(f"Không thể đọc file khi chụp snapshot: {path}: {exc}") from exc
            files[relative] = {
                "size": stat.st_size,
                "mtime": stat.st_mtime,
                "hash": file_hash,
            }

    return {
        "root": str(root_path),
        "taken_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "files": dict(sorted(files.items())),
    }


def write_snapshot(snapshot: dict, output: Path | str) -> None:
    output_path = Path(output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Chụp snapshot cây thư mục.")
    parser.add_argument("root", help="Thư mục gốc cần chụp")
    parser.add_argument("--out", required=True, help="File JSON đầu ra")
    parser.add_argument("--ignore", action="append", default=[], help="Glob cần bỏ qua; có thể lặp")
    return parser.parse_args()


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    args = parse_args()
    write_snapshot(take_snapshot(args.root, args.ignore), args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
