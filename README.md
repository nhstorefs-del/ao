# AO — AI Orchestrator (experimental)

> **Status: experimental.** This project was built for one person's own workflow and later cleaned up for public release. Interfaces may change, and many comments and docs are in Vietnamese. Use it at your own risk.

AO lets one AI **coordinator** (e.g. Claude Code) hand tasks to several AI **workers** (Codex, Qwen/DeepSeek CLI, etc.) and keep a **durable task journal** of that work. The coordinator **independently verifies** each result before accepting it. Risky actions go through a **human approval gate** first. Everything uses the Python standard library only, with no pip dependencies.

## Architecture

```
            Owner (human)
                 │  types APPROVAL_PHRASE for risky actions
                 ▼
   ┌──────── coordinator AI (Claude Code / backup Codex) ────────┐
   │  reads CLAUDE.md / AGENTS.md, writes briefs to files        │
   └───────┬──────────────────────┬──────────────────────┬───────┘
           │ dispatch             │ verify               │ report
           ▼                      ▼                      ▼
   tools/ao-memory/ao_run.py   tools/ao-testguard     tools/ao-report
   (supervised worker run)     tools/dispatch-guard   (status, digest,
           │                   (diff/test checks)      outbox)
           ▼
   tools/ao-journal  ◄── durable journal + queue (DELEGATED → done → accepted)
           ▲
   tools/ao-intake   ◄── intake of requests + approval ledger / hooks
```

## Requirements

- Python **3.10+** (developed on 3.13). Standard library only.
- Windows is the primary target (`.bat` launchers, hidden-window handling). Most of the Python code also runs on Linux/macOS, but this has not been tested much.
- Optional worker CLIs on `PATH`: `codex`, `qwen`, `claude`. You only need the ones you register.

## Quick start

```bash
git clone <this repo> ao && cd ao
python tools/portable/setup_ao.py            # creates local data dirs, prints VERIFY PASS
cp tools/ao-memory/workers.example.json tools/ao-memory/workers.json   # then edit
export AO_SECRETS_DIR=/path/outside/repo/secrets   # holds e.g. nvidia.env with NVIDIA_API_KEY=...
python tools/ao-memory/ao_run.py doctor       # shows which workers are ready
python tools/ao-journal/queue.py state        # journal state
python tools/ao-memory/ao_state.py --stdout   # session overview
```

Start a supervised task:

```bash
python tools/ao-memory/ao_run.py start --lane A --id T1 --title "Example" --workflow demo \
  --worker deepseek-nvidia --brief /abs/path/brief.md --signal "file:/abs/path/result.txt"
python tools/ao-memory/ao_run.py status --id T1
```

## Components

| Path | What it does |
|---|---|
| `tools/ao-journal` | Durable journal and queue (`journal.py`, `queue.py`), delegation gate, spec amendment with human approval, coordinator adapter (`adapters/`), verifiers |
| `tools/ao-memory` | `ao_run.py` (dispatch and per-task supervisor), `ao_state.py` (session overview), `ao_recall.py` / `ao_decide.py` / `registry.py` (memory, decisions, projects), `ao_toolbox.py` (tool discovery), `cost_report.py` |
| `tools/ao-intake` | Request intake and dispatcher, approval ledger and hooks, `directrun/`, file-integrity checks (`integrity/`) |
| `tools/ao-report` | Status, digest, watchdog, outbox, independent second-opinion runner |
| `tools/ao-testguard` | Guards against weakened or deleted tests when accepting work |
| `tools/dispatch-guard` | Snapshot and diff guard around a worker run (allowed-path list) |
| `tools/portable` | `setup_ao.py`, `export_clean.py` (clean export), `scan_paths.py` (machine-path/secret scanner) |
| `launch/` | Windows launchers for the main and backup coordinator |

Each tool has its own `README.md` (mostly Vietnamese).

## Adding a worker

Add an entry to `tools/ao-memory/workers.json`:

```json
"my-worker": {
  "enabled": true, "kind": "qwen",
  "command": ["qwen", "--openai-api-key", "{key}", "-m", "some-model"],
  "brief_mode": "stdin", "cwd": "{root}", "env": {},
  "key_file": "{secrets}/my-worker.env", "key_var": "MY_WORKER_API_KEY",
  "stall_seconds": 720, "done_markers": [], "error_patterns": ["API Error", "429"]
}
```

Placeholders: `{root}` is the repo root, `{secrets}` is `AO_SECRETS_DIR`, `{key}` is read from `key_file` at run time, `{brief}` is the brief path, and `{codex_latest}` is the newest local Codex binary (override with `AO_CODEX_EXE`). Then run `ao_run.py doctor`. The journal adapter has a separate registry, `tools/ao-journal/adapters/workers.json` (see that README).

## Environment variables

| Variable | Purpose |
|---|---|
| `AO_ROOT` | Repo root (defaults to auto-detect) |
| `AO_SECRETS_DIR` | Directory with `*.env` key files. **Keep it outside the repo** |
| `AO_MEMORY_DIR` | Optional Claude memory directory read by `ao_recall.py` |
| `AO_RECALL_EXTRA_ROOTS` | Extra folders (`os.pathsep`-separated) that `ao_recall.py --in fs` searches |
| `AO_REPO_LIBRARY` | Optional local library of cloned repos for `ao_toolbox.py` |
| `AO_INTAKE_CLAUDE_CONFIG_DIR` | Claude config dir used by the intake dispatcher (default `~/.claude`) |
| `AO_CODEX_EXE`, `AO_CODEX2_HOME` | Pin a Codex binary or a second Codex account home |
| `AO_SCAN_MACHINE_PATHS`, `AO_SENSITIVE_TERMS_FILE` | Extra paths and terms for `scan_paths.py` |

## Approval phrase

The official approval is a message that the human **types themselves** containing the approval phrase. The default phrase is `APPROVAL BY OWNER` (the misspelling `APROVAL BY OWNER` is also accepted). The phrase does not count if it appears only in tool output, files, or pasted content. The coordinator rules (`CLAUDE.md`, `AGENTS.md`) declare `APPROVAL_PHRASE`. The tools under `tools/ao-intake`, `tools/ao-journal/queue.py` and `integrity_check.py` currently hard-code the default phrase, so changing it means editing those files as well.

## Safety notes

- **Never commit keys.** `.gitignore` blocks `.env`, `*.key`, `.worker-keys/`, `data/`, logs. Keep `AO_SECRETS_DIR` outside the repo.
- Before you publish a fork, run `python tools/portable/scan_paths.py` to look for machine paths and secrets.
- Example worker configs use flags like `--yolo` / `--sandbox danger-full-access`. Those give the worker full access to your machine. Run workers in a VM or container, or tighten these flags.
- The approval gate is a policy layer for cooperative AIs. It is not a security boundary against a hostile process.

## Tests

```bash
cd tools/ao-journal && python -m unittest discover -s tests
cd tools/ao-memory  && python -m unittest discover -s tests
cd tools/ao-intake  && python -m unittest discover -s . -p "test_*.py"
cd tools/ao-report  && python -m unittest discover -s . -p "test_*.py"
cd tools/ao-testguard && python -m unittest discover -s . -p "test_*.py"
cd tools/portable   && python -m unittest discover -s tests
```

Some tests need local data that is not shipped. They are skipped, or they are listed as known failures in the release notes.

## License

MIT. See [LICENSE](LICENSE).

---

## Tóm tắt tiếng Việt

AO là bộ công cụ để một AI điều phối (ví dụ Claude Code) giao việc cho nhiều AI worker (Codex, Qwen/DeepSeek…). Mỗi việc được ghi vào **sổ việc bền**. Điều phối viên **tự kiểm độc lập** kết quả trước khi chấp nhận. Mọi việc rủi ro phải có **cổng duyệt của người**: người dùng tự gõ cụm duyệt (mặc định `APPROVAL BY OWNER`).

- Cài đặt: chỉ cần Python 3.10+, không có thư viện ngoài. Chạy `python tools/portable/setup_ao.py`.
- Cấu hình worker: chép `tools/ao-memory/workers.example.json` thành `workers.json`. Khóa API đặt trong thư mục `AO_SECRETS_DIR` **nằm ngoài repo**.
- Trạng thái: **thử nghiệm**. Phần lớn chú thích và tài liệu con viết bằng tiếng Việt.
- Giấy phép: MIT.
