"""Bounded Haiku worker for AO."""
import json
import os
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(r"${AO_ROOT}")
KEY_FILE = ROOT / ".worker-keys" / "haiku.env"
CONFIG_DIR = Path(os.environ.get("AO_HAIKU_CONFIG_DIR", str(Path.home() / ".claude-haiku")))
USAGE_FILE = ROOT / "tools/ao-memory/data/haiku_usage.jsonl"
ALLOWED = ("Read", "Glob", "Grep", "Write", "Edit")
DISALLOWED = ("Bash", "PowerShell", "WebFetch", "WebSearch")
TRAN_MOI_LUOT = 90_000
TRAN_TONG_VIEC = 400_000
SYSTEM_RESERVE = 5_000


def load_credentials(path=KEY_FILE):
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError as exc:
        raise RuntimeError("Thiếu tệp haiku.env") from exc
    except OSError as exc:
        raise RuntimeError("Không đọc được tệp haiku.env") from exc
    values = {}
    for line in lines:
        line = line.strip()
        if line.startswith("export "):
            line = line[7:]
        name, sep, value = line.partition("=")
        if sep and name.strip() in {"ANTHROPIC_API_KEY", "ANTHROPIC_MODEL"}:
            values[name.strip()] = value.strip().strip('"\'')
    key = values.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise RuntimeError("haiku.env thiếu ANTHROPIC_API_KEY")
    return key, values.get("ANTHROPIC_MODEL") or "claude-haiku-5-5"


def child_env(key):
    env = {name: value for name, value in os.environ.items()
           if not name.upper().startswith(("CLAUDE", "ANTHROPIC", "HAIKU_TRAN"))}
    env.update({"ANTHROPIC_API_KEY": key, "CLAUDE_CONFIG_DIR": str(CONFIG_DIR),
                "AO_ONOPEN_HOOK": "0", "AO_COVERAGE": "0", "AO_SESSIONSTART": "0"})
    return env


def command(model):
    return ["claude", "-p", "--model", model, "--permission-mode", "acceptEdits",
            "--allowedTools", *ALLOWED, "--disallowedTools", *DISALLOWED,
            "--max-turns", "25", "--autocompact", "100k",
            "--output-format", "stream-json", "--verbose"]


def redact(value, key):
    return value.replace(key, "[REDACTED]") if key else value


def count_brief_tokens(brief, key, model):
    payload = json.dumps({"model": model, "messages": [
        {"role": "user", "content": brief}]}).encode("utf-8")
    req = Request("https://api.anthropic.com/v1/messages/count_tokens",
                  data=payload, method="POST",
                  headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                           "content-type": "application/json"})
    try:
        with urlopen(req, timeout=20) as response:
            count = json.load(response)["input_tokens"]
        if type(count) is not int or count < 0:
            raise ValueError("invalid count")
        return count + SYSTEM_RESERVE
    except (OSError, ValueError, KeyError, TypeError):
        return (len(brief) + 1) // 2 + SYSTEM_RESERVE


def usage_numbers(usage):
    if not isinstance(usage, dict) or "input_tokens" not in usage or "output_tokens" not in usage:
        raise ValueError("missing usage")
    values = [usage.get(name, 0) for name in
              ("input_tokens", "cache_creation_input_tokens",
               "cache_read_input_tokens", "output_tokens")]
    if any(type(value) is not int or value < 0 for value in values):
        raise ValueError("invalid usage")
    return sum(values[:3]), values[3]


def kill_tree(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    else:
        process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


def write_usage(job_id, turns, total_in, total_out, largest, result):
    USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
    row = {"time": datetime.now(timezone.utc).isoformat(), "job_id": job_id,
           "turns": turns, "total_input": total_in, "total_output": total_out,
           "largest_turn_input": largest, "result": result,
           "estimated_usd": round((total_in * 0.10 + total_out * 0.50) / 1_000_000, 8)}
    with USAGE_FILE.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False) + "\n")


def run_stream(brief, key, model):
    process = subprocess.Popen(command(model), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, cwd=ROOT, env=child_env(key),
                               text=True, encoding="utf-8", errors="replace", bufsize=1)
    errors = []
    thread = threading.Thread(target=lambda: errors.extend(process.stderr), daemon=True)
    thread.start()
    process.stdin.write(brief)
    process.stdin.close()
    messages = {}
    result_text = None
    invalid = breached = False
    for line in process.stdout:
        try:
            event = json.loads(line)
        except ValueError:
            invalid = True
            kill_tree(process)
            break
        if event.get("type") == "assistant":
            message = event.get("message") or {}
            mid = message.get("id")
            try:
                incoming, outgoing = usage_numbers(message.get("usage"))
            except ValueError:
                invalid = True
                kill_tree(process)
                break
            if not mid:
                invalid = True
                kill_tree(process)
                break
            old = messages.get(mid, (0, 0))
            messages[mid] = (max(old[0], incoming), max(old[1], outgoing))
            total_in = sum(pair[0] for pair in messages.values())
            total_out = sum(pair[1] for pair in messages.values())
            if incoming > TRAN_MOI_LUOT or total_in + total_out > TRAN_TONG_VIEC:
                breached = True
                kill_tree(process)
                break
        elif event.get("type") == "result":
            result_text = event.get("result")
    if process.poll() is None:
        process.wait()
    thread.join(timeout=5)
    total_in = sum(pair[0] for pair in messages.values())
    total_out = sum(pair[1] for pair in messages.values())
    largest = max((pair[0] for pair in messages.values()), default=0)
    if breached:
        print(f"CHAM_TRAN_TOKEN: lượt={len(messages)} vào={largest} (trần 90000)", file=sys.stderr)
        return 4, len(messages), total_in, total_out, largest
    if invalid or not messages or result_text is None:
        print("LOI: Claude không báo usage đầy đủ; đã đóng việc.", file=sys.stderr)
        return 2, len(messages), total_in, total_out, largest
    sys.stdout.write(redact(str(result_text), key))
    if errors:
        sys.stderr.write(redact("".join(errors), key))
    return process.returncode, len(messages), total_in, total_out, largest


def main():
    job_id = os.environ.get("AO_JOB_ID") or os.environ.get("AO_TASK_ID")
    turns = total_in = total_out = largest = 0
    result = "LOI"
    try:
        key, model = load_credentials()
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        brief = sys.stdin.buffer.read().decode("utf-8")
        estimate = count_brief_tokens(brief, key, model)
        if estimate >= TRAN_MOI_LUOT:
            print(f"Brief ~{estimate} token vượt trần 90.000 token/lượt của thợ Haiku "
                  "(bậc giá đổi ở 100.000). Chia nhỏ việc hoặc giao thợ khác.", file=sys.stderr)
            return 3
        code, turns, total_in, total_out, largest = run_stream(brief, key, model)
        result = "CHAM_TRAN" if code == 4 else ("OK" if code == 0 else "LOI")
        return code
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"Không chạy được Haiku ({type(exc).__name__})", file=sys.stderr)
        return 2
    finally:
        try:
            write_usage(job_id, turns, total_in, total_out, largest, result)
        except OSError:
            print("LOI: không ghi được sổ chi tiêu Haiku.", file=sys.stderr)
            return 2


if __name__ == "__main__":
    raise SystemExit(main())
