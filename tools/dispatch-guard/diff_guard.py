#!/usr/bin/env python3
"""Compare two snapshots and flag changes outside allowed globs."""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


def _load(path: Path | str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _allowed(path: str, patterns: Iterable[str]) -> bool:
    normalized = path.replace("\\", "/")
    return any(fnmatch.fnmatch(normalized, pattern.replace("\\", "/")) for pattern in patterns)


def compare(before: dict, after: dict, allow: Iterable[str]) -> tuple[list[dict[str, str | bool]], int]:
    before_root = os.path.normcase(os.path.abspath(before.get("root", "")))
    after_root = os.path.normcase(os.path.abspath(after.get("root", "")))
    if before_root != after_root:
        raise ValueError(f"Hai snapshot không cùng root: {before.get('root')} != {after.get('root')}")

    before_files = before.get("files", {})
    after_files = after.get("files", {})
    changes: list[dict[str, str | bool]] = []
    all_paths = sorted(set(before_files) | set(after_files))
    patterns = list(allow)
    for path in all_paths:
        if path not in before_files:
            kind = "MỚI"
        elif path not in after_files:
            kind = "BỊ XOÁ"
        elif before_files[path] != after_files[path]:
            kind = "SỬA"
        else:
            continue
        in_scope = _allowed(path, patterns)
        changes.append({"type": kind, "path": path, "in_scope": in_scope})

    out_of_scope = sum(not bool(item["in_scope"]) for item in changes)
    return changes, out_of_scope


def _md_cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_report(changes: list[dict[str, str | bool]], out_of_scope: int) -> str:
    if out_of_scope:
        first = f"KẾT QUẢ: CẢNH BÁO — {out_of_scope} file ngoài phạm vi"
    else:
        first = "KẾT QUẢ: PASS"
    show_salvage = any("salvaged_to" in item for item in changes)
    lines = [
        first,
        "",
        "| Loại | Đường dẫn | Phạm vi |" + (" Bản cứu |" if show_salvage else ""),
        "|---|---|---|" + ("---|" if show_salvage else ""),
    ]
    if changes:
        for item in changes:
            scope = "TRONG PHẠM VI" if item["in_scope"] else "**NGOÀI PHẠM VI**"
            salvage = ""
            if show_salvage:
                salvage_path = item.get("salvaged_to")
                salvage = f" `{_md_cell(salvage_path)}` |" if salvage_path else " — |"
            lines.append(
                f"| {_md_cell(item['type'])} | `{_md_cell(item['path'])}` | {scope} |{salvage}"
            )
    else:
        lines.append("| — | Không có thay đổi | — |" + (" — |" if show_salvage else ""))
    return "\n".join(lines) + "\n"


def _salvage(changes: list[dict[str, str | bool]], root: Path, salvage_dir: Path) -> None:
    timestamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%dT%H%M%S%f%z")
    batch_dir = salvage_dir.expanduser().resolve() / timestamp
    resolved_root = root.resolve(strict=True)
    for item in changes:
        if item["in_scope"] or item["type"] not in {"MỚI", "SỬA"}:
            continue
        source = (resolved_root / str(item["path"])).resolve(strict=True)
        try:
            relative = source.relative_to(resolved_root)
        except ValueError as exc:
            raise ValueError(f"Đường dẫn ngoài root snapshot: {source}") from exc
        if not source.is_file():
            continue
        destination = batch_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        item["salvaged_to"] = str(destination)


def run_diff(
    before_path: Path | str,
    after_path: Path | str,
    allow: Iterable[str],
    report_path: Path | str,
    salvage_dir: Path | str | None = None,
) -> int:
    before = _load(before_path)
    after = _load(after_path)
    changes, out_of_scope = compare(before, after, allow)
    if salvage_dir is not None:
        _salvage(changes, Path(str(after["root"])), Path(salvage_dir))
    target = Path(report_path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_report(changes, out_of_scope), encoding="utf-8")
    return out_of_scope


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="So sánh snapshot và kiểm tra phạm vi thay đổi.")
    parser.add_argument("--before", required=True)
    parser.add_argument("--after", required=True)
    parser.add_argument("--allow", action="append", required=True, help="Glob được phép; có thể lặp")
    parser.add_argument("--report", required=True)
    parser.add_argument(
        "--salvage-dir",
        help="Copy file mới/sửa ngoài phạm vi vào thư mục bằng chứng theo timestamp",
    )
    return parser.parse_args()


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    args = parse_args()
    count = run_diff(args.before, args.after, args.allow, args.report, args.salvage_dir)
    return 1 if count else 0


if __name__ == "__main__":
    raise SystemExit(main())
