"""Báo cáo số đo thời gian và token từ các job AO; chỉ đọc jobs/."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

JOBS = Path(__file__).resolve().parent / "jobs"
LOCAL_TZ = timezone(timedelta(hours=7))
END_TYPES = {"DONE_NEEDS_REVIEW", "DONE", "FAILED", "GAVE_UP", "ORPHANED", "STOPPED", "CANCELLED", "SUPERVISOR_ERROR"}
BAD_TYPES = {"FAILED", "GAVE_UP", "ORPHANED", "STOPPED", "CANCELLED", "SUPERVISOR_ERROR"}
COUNT = r"(\d[\d,]*)"
TOTAL_RE = re.compile(r"^\s*tokens used\s*:?\s*(?:\n\s*)?" + COUNT + r"\s*$", re.I | re.M)
PAIR_RE = re.compile(r"\b(input_tokens|output_tokens|prompt_tokens|completion_tokens|total_tokens)\b\s*[=:]\s*" + COUNT, re.I)
ATTEMPT_RE = re.compile(r"^===\s+(.+?#\d+)\s+[^\r\n=]+\s+===$", re.M)


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, UnicodeError, ValueError):
        return {}


def parse_time(value):
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def read_events(path: Path):
    result = []
    try:
        with path.open(encoding="utf-8-sig", errors="replace") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                    when = parse_time(event.get("ts"))
                    if when and isinstance(event.get("type"), str):
                        result.append((when, event["type"].upper(), str(event.get("detail", ""))))
                except (ValueError, AttributeError):
                    continue
    except OSError:
        pass
    return result


def read_tokens(path: Path, events=()):
    """Lấy chân trang cuối của mỗi lượt START có trong log, tối đa một số/lượt."""
    try:
        log = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, None, None
    start_count = sum(kind == "START" for _, kind, _ in events)
    starts = [match.group(1) for _, kind, detail in events if kind == "START"
              if (match := re.search(r"\b([^\s#]+#\d+)\b", detail))]
    headers = list(ATTEMPT_RE.finditer(log))
    if headers:
        # Chỉ tin header khớp START của chính job; văn bản chép lại về job khác bị bỏ qua.
        sections = [log[header.end():headers[index + 1].start() if index + 1 < len(headers) else len(log)]
                    for index, header in enumerate(headers) if header.group(1) in starts]
    elif start_count <= 1:
        sections = [log]
    else:
        # Log cũ thiếu header: chỉ cộng khi số chân trang khớp đúng số lượt START.
        matches = list(TOTAL_RE.finditer(log))
        sections = [match.group(0) for match in matches] if len(matches) == start_count else [log]
    totals = []
    inputs = []
    outputs = []
    pair_totals = []
    for section in sections:
        matches = TOTAL_RE.findall(section)
        if matches:
            totals.append(int(matches[-1].replace(",", "")))
        pairs = defaultdict(list)
        for key, number in PAIR_RE.findall(section):
            pairs[key.lower()].append(int(number.replace(",", "")))
        for keys, target in ((('input_tokens', 'prompt_tokens'), inputs),
                             (('output_tokens', 'completion_tokens'), outputs),
                             (('total_tokens',), pair_totals)):
            values = next((pairs[key] for key in keys if pairs[key]), [])
            if values:
                target.append(values[-1])
    token_in = sum(inputs) if inputs else None
    token_out = sum(outputs) if outputs else None
    token_total = sum(totals) if totals else (sum(pair_totals) if pair_totals else None)
    if token_total is None and token_in is not None and token_out is not None:
        token_total = token_in + token_out
    return token_in, token_out, token_total


def duration_seconds(events):
    """Cộng các lượt có START và điểm kết thúc; không tính thời gian chờ retry."""
    started = None
    total = 0.0
    complete = 0
    for when, kind, _ in events:
        if kind == "START":
            started = when
        elif kind in END_TYPES and started is not None:
            total += max(0.0, (when - started).total_seconds())
            complete += 1
            started = None
    return round(total, 1) if complete else None


def collect(jobs: Path = JOBS, since: date | None = None):
    rows = []
    if not jobs.exists():
        return rows
    for folder in sorted(jobs.iterdir()):
        if not folder.is_dir():
            continue
        job = read_json(folder / "job.json")
        status = read_json(folder / "status.json")
        if not job and not status:
            continue
        state = str(status.get("state") or "UNKNOWN")
        # Job đang chạy: không mở events.jsonl hoặc worker.log.
        active = state.upper() in {"RUNNING", "STARTING", "RETRYING", "DISPATCHED"}
        events = [] if active else read_events(folder / "events.jsonl")
        first = next((when for when, kind, _ in events if kind == "START"), None)
        if first is None:
            try:
                first = datetime.fromtimestamp((folder / "job.json").stat().st_mtime, timezone.utc)
            except OSError:
                first = None
        day = first.astimezone(LOCAL_TZ).date().isoformat() if first else None
        if since and (day is None or day < since.isoformat()):
            continue
        worker = str(status.get("worker") or job.get("worker") or "UNKNOWN")
        attempts = sum(kind == "START" for _, kind, _ in events)
        if not attempts:
            attempt_id = str(status.get("attempt_id") or job.get("attempt_id") or "")
            match = re.search(r"#(\d+)$", attempt_id)
            attempts = int(match.group(1)) if match else max(1, int(status.get("retries") or job.get("retries") or 0) + 1)
        retries = max(int(status.get("retries") or 0), int(job.get("retries") or 0), attempts - 1)
        token_in, token_out, token_total = (None, None, None) if active else read_tokens(folder / "worker.log", events)
        seconds = None if active else duration_seconds(events)
        flags = []
        if state.upper() in BAD_TYPES or any(kind in BAD_TYPES for _, kind, _ in events):
            flags.append("lỗi/bỏ")
        if retries:
            flags.append("retry")
        if seconds is not None and seconds > 600:
            flags.append(">10 phút")
        rows.append({"id": str(job.get("id") or status.get("id") or folder.name), "day": day,
                     "worker": worker, "seconds": seconds, "input_tokens": token_in,
                     "output_tokens": token_out, "total_tokens": token_total,
                     "state": state, "attempts": attempts, "flags": flags})
    return rows


def summary(rows, key):
    groups = defaultdict(list)
    for row in rows:
        groups[row.get(key) or "UNKNOWN"].append(row)
    return [{"name": name, "jobs": len(items), "measured_jobs": sum(x["total_tokens"] is not None for x in items),
             "unmeasured_jobs": sum(x["total_tokens"] is None for x in items),
             "seconds_known": round(sum(x["seconds"] or 0 for x in items), 1),
             "total_tokens_known": sum(x["total_tokens"] or 0 for x in items)}
            for name, items in sorted(groups.items())]


def display_number(value):
    return "không có số đo" if value is None else f"{value:,}"


def make_markdown(rows):
    lines = ["# Báo cáo thời gian và token AO", "", "Số liệu từ `jobs/*`; ngày theo UTC+07. Token chỉ lấy từ bộ đếm tường minh trong log. Job đang chạy không đọc log, chưa tính thời gian.", "",
             f"**Tổng:** {len(rows)} job; {sum(x['total_tokens'] is not None for x in rows)} có token thật; {sum(x['total_tokens'] is None for x in rows)} không có số đo token.", "",
             "## Theo job", "", "| Ngày | ID | Worker | Giây | Token vào | Token ra | Token tổng | Trạng thái | Lượt thử | Cờ |",
             "|---|---|---|---:|---:|---:|---:|---|---:|---|"]
    for x in rows:
        fields = [x["day"] or "?", x["id"], x["worker"], display_number(x["seconds"]),
                  display_number(x["input_tokens"]), display_number(x["output_tokens"]), display_number(x["total_tokens"]),
                  x["state"], str(x["attempts"]), ", ".join(x["flags"]) or "—"]
        lines.append("| " + " | ".join(str(v).replace("|", "\\|") for v in fields) + " |")
    for title, key in (("Theo worker", "worker"), ("Theo ngày", "day")):
        lines += ["", "## " + title, "", "| Nhóm | Job | Có token | Không có số đo | Giây đã đo | Token tổng đã đo |",
                  "|---|---:|---:|---:|---:|---:|"]
        for x in summary(rows, key):
            lines.append(f"| {x['name']} | {x['jobs']} | {x['measured_jobs']} | {x['unmeasured_jobs']} | {display_number(x['seconds_known'])} | {display_number(x['total_tokens_known'])} |")
    lines += ["", "## Giới hạn", "", "- Codex CLI thường chỉ ghi `tokens used` (tổng), không tách token vào/ra.",
              "- Mỗi lượt START chỉ lấy chân trang cuối trong đoạn log của lượt đó; phiên Codex phụ bên trong cùng lượt có thể chưa được cộng đủ.",
              "- Tổng theo nhóm chỉ cộng job có số đo; không đại diện cho toàn bộ chi phí.",
              "- Thời gian là tổng các đoạn START → kết thúc trong events.jsonl; đoạn thiếu mốc không được đo.",
              "- Cờ retry/lỗi cho biết có nguy cơ lãng phí, không xác nhận chi phí tiền thật.", ""]
    return "\n".join(lines)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", type=date.fromisoformat, metavar="YYYY-MM-DD")
    parser.add_argument("--json", action="store_true", help="In JSON ra màn hình")
    parser.add_argument("--md", type=Path, metavar="FILE", help="Lưu bảng Markdown ra file")
    args = parser.parse_args()
    rows = collect(since=args.since)
    if args.md:
        args.md.write_text(make_markdown(rows), encoding="utf-8")
    if args.json:
        print(json.dumps({"jobs": rows, "by_worker": summary(rows, "worker"), "by_day": summary(rows, "day")}, ensure_ascii=False, indent=2))
    elif not args.md:
        print(make_markdown(rows))
    else:
        print(f"Đã ghi {args.md}: {len(rows)} job, {sum(x['total_tokens'] is not None for x in rows)} có số đo token.")


if __name__ == "__main__":
    main()
