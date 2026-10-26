"""Kiểm độc lập (AO/Claude viết) cho Coordinator Adapter — dùng FAKE coordinator (không gọi AI thật).

Chạy: python tools/ao-journal/verifier/verify_adapter.py
Chỉ dùng thư mục tạm; không đụng journal/queue/turn_log thật.
"""
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent.parent
J, Q = str(HERE / "journal.py"), str(HERE / "queue.py")
AD = HERE / "adapters" / "ao_adapter.py"
CLAUDE_MD = Path(r"${AO_ROOT}\CLAUDE.md")
results = []


def run(args, **kw):
    p = subprocess.run([sys.executable] + args, capture_output=True, text=True, encoding="utf-8", **kw)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{str(detail)[:260]}]" if (detail and not cond) else ""))


def recs(path):
    p = Path(path)
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()] if p.exists() else []


class Env:
    def __init__(self, tmp, workers=None, caps=None):
        self.tmp = Path(tmp)
        self.jr, self.qu = str(self.tmp / "j.jsonl"), str(self.tmp / "q.json")
        self.tlog = str(self.tmp / "turn_log.jsonl")
        self.capdir = self.tmp / "caps"
        self.capdir.mkdir()
        self.workers_file = self.tmp / "workers.json"
        self.fake_out = self.tmp / "fake_out.txt"
        self.prompt_out = self.tmp / "prompt.txt"
        # worker stub: đọc 'OUT=<path>' trong brief, ghi file đó (exit 0)
        self.stub_ok = self.tmp / "stub_ok.py"
        self.stub_ok.write_text("import sys,re\nb=open(sys.argv[1],encoding='utf-8').read()\nm=re.search(r'OUT=(.+)',b)\nopen(m.group(1).strip(),'w',encoding='utf-8').write('ket qua')\n", encoding="utf-8")
        self.stub_noout = self.tmp / "stub_noout.py"
        self.stub_noout.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
        self.stub_fail = self.tmp / "stub_fail.py"
        self.stub_fail.write_text("import sys\nsys.exit(3)\n", encoding="utf-8")
        self.stub_slow = self.tmp / "stub_slow.py"
        self.stub_slow.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
        py = sys.executable
        self.workers = workers or {
            "w-ok": {"command": [py, str(self.stub_ok), "{brief}"], "timeout_s": 30},
            "w-noout": {"command": [py, str(self.stub_noout), "{brief}"], "timeout_s": 30},
            "w-fail": {"command": [py, str(self.stub_fail), "{brief}"], "timeout_s": 30},
            "w-127": {"command": [str(self.tmp / "khong_ton_tai_xyz.exe"), "{brief}"], "timeout_s": 30},
            "w-slow": {"command": [py, str(self.stub_slow), "{brief}"], "timeout_s": 2},
        }
        self.workers_file.write_text(json.dumps(self.workers), encoding="utf-8")
        full = {k: "unknown" for k in ("supports_tools", "supports_files", "supports_vision", "supports_long_session",
                                       "supports_structured_output", "supports_resume", "supports_subagents")}
        full.update({"supports_structured_output": True, "invocation": "fake", "max_context_chars": 20000, "notes": "test"})
        (self.capdir / "fake.json").write_text(json.dumps(caps or full), encoding="utf-8")

    def jc(self, *a): return run([J, "--journal", self.jr] + list(a))
    def qc(self, *a): return run([Q, "--queue", self.qu, "--journal", self.jr] + list(a))

    def state(self, wf="W"):
        rc, o = self.qc("state", "--json", "--workflow", wf)
        return json.loads(o)

    def task(self, tid, wf="W"):
        return next(t for t in self.state(wf)["tasks"] if t["id"] == tid)

    def add(self, tid, worker, deps=(), wf="W", out=None):
        brief = self.tmp / f"brief_{tid}.md"
        out = out or (self.tmp / f"out_{tid}.txt")
        brief.write_text(f"viec {tid}\nOUT={out}\n", encoding="utf-8")
        args = ["add", "--id", tid, "--title", tid, "--workflow", wf, "--worker", worker, "--brief", str(brief), "--signal", f"file:{out}"]
        for d in deps:
            args += ["--depends-on", d]
        return self.qc(*args)

    def turn(self, raw, coordinator="fake", wf="W", extra=()):
        self.fake_out.write_text(raw if isinstance(raw, str) else json.dumps(raw), encoding="utf-8")
        args = [str(AD), "turn", "--coordinator", coordinator, "--workflow", wf,
                "--journal", self.jr, "--queue", self.qu, "--workers", str(self.workers_file),
                "--capabilities-dir", str(self.capdir), "--turn-log", self.tlog, "--claude-md", str(CLAUDE_MD),
                "--fake-output-file", str(self.fake_out), "--prompt-out", str(self.prompt_out)] + list(extra)
        rc, o = run(args)
        m = re.search(r"\{.*\}\s*$", o, re.S)
        try:
            res = json.loads(m.group(0)) if m else {}
        except Exception:
            res = {}
        return rc, res, o

    def n(self): return len(recs(self.jr))


if not AD.exists():
    print("FAIL adapters/ao_adapter.py chua ton tai")
    sys.exit(1)

# ---------- 0. kiểm tĩnh mã nguồn Adapter
src = AD.read_text(encoding="utf-8")
check("STATIC: khong dung shell=True", "shell=True" not in src)
check("STATIC: khong dung eval/exec", not re.search(r"\b(eval|exec)\s*\(", src))
check("STATIC: khong ghi truc tiep journal.jsonl/queue.json (chi qua lenh Core)",
      not re.search(r"open\([^)]*(journal|queue)[^)]*['\"][wa]", src) and "write_text" not in "".join(l for l in src.splitlines() if "journal" in l.lower() and "queue" in l.lower()))
check("STATIC: Adapter <= 450 dong (tinh gon)", len(src.splitlines()) <= 450, f"{len(src.splitlines())} dong")

# ---------- 1. capability
with tempfile.TemporaryDirectory() as tmp:
    e = Env(tmp)
    e.add("A", "w-ok"); e.qc("sync")
    n = e.n()
    (e.capdir / "fake.json").unlink()
    rc, res, o = e.turn({"action": "dispatch", "task_id": "A"})
    check("thieu file capability -> CAPABILITY_MISSING, khong doi state", res.get("outcome") == "CAPABILITY_MISSING" and e.n() == n, o)
    (e.capdir / "fake.json").write_text(json.dumps({"supports_tools": True}), encoding="utf-8")
    rc, res, o = e.turn({"action": "dispatch", "task_id": "A"})
    check("thieu co trong capability -> CAPABILITY_MISSING", res.get("outcome") == "CAPABILITY_MISSING" and e.n() == n, o)
    bad = {k: "unknown" for k in ("supports_tools", "supports_files", "supports_vision", "supports_long_session", "supports_structured_output", "supports_resume", "supports_subagents")}
    bad["supports_tools"] = "co"
    bad.update({"invocation": "fake", "max_context_chars": 1000, "notes": ""})
    (e.capdir / "fake.json").write_text(json.dumps(bad), encoding="utf-8")
    rc, res, o = e.turn({"action": "dispatch", "task_id": "A"})
    check("gia tri co khong hop le (khong phai true/false/unknown) -> CAPABILITY_MISSING", res.get("outcome") == "CAPABILITY_MISSING" and e.n() == n, o)
    unk = dict(bad); unk["supports_tools"] = "unknown"; unk["supports_structured_output"] = "unknown"
    (e.capdir / "fake.json").write_text(json.dumps(unk), encoding="utf-8")
    rc, res, o = e.turn({"action": "dispatch", "task_id": "A"})
    check("structured_output != true -> chay duoc nhung degraded=true", res.get("outcome") == "APPLIED" and res.get("degraded") is True, o)

# ---------- 2. rejections: state KHÔNG đổi
with tempfile.TemporaryDirectory() as tmp:
    e = Env(tmp)
    e.add("A", "w-ok"); e.add("B", "w-ok", deps=["A"]); e.qc("sync")
    n = e.n()
    cases = [
        ("JSON hong", "{day khong phai json", "MALFORMED_OUTPUT"),
        ("khong co JSON", "toi nghi nen dispatch A", "MALFORMED_OUTPUT"),
        ("hai JSON object", '{"action":"dispatch","task_id":"A"} {"action":"dispatch","task_id":"B"}', "MALFORMED_OUTPUT"),
        ("JSON la mang", '[{"action":"dispatch","task_id":"A"}]', None),
        ("action ngoai allowed (accept khi A moi ready)", {"action": "accept", "task_id": "A"}, "ADAPTER_REJECTED"),
        ("dispatch B khi phu thuoc chua accepted", {"action": "dispatch", "task_id": "B"}, "ADAPTER_REJECTED"),
        ("verify khi chua done", {"action": "verify", "task_id": "A"}, "ADAPTER_REJECTED"),
        ("task khong ton tai", {"action": "dispatch", "task_id": "ZZZ"}, "ADAPTER_REJECTED"),
        ("action la gi do la", {"action": "delete_everything", "task_id": "A"}, "ADAPTER_REJECTED"),
        ("khoa cam: brief", {"action": "dispatch", "task_id": "A", "brief": "x.md"}, "ADAPTER_REJECTED"),
        ("khoa cam: worker", {"action": "dispatch", "task_id": "A", "worker": "w-fail"}, "ADAPTER_REJECTED"),
        ("khoa cam: signal", {"action": "dispatch", "task_id": "A", "signal": "file:de-dang"}, "ADAPTER_REJECTED"),
        ("khoa cam: signals", {"action": "dispatch", "task_id": "A", "signals": ["file:x"]}, "ADAPTER_REJECTED"),
        ("khoa cam: command", {"action": "dispatch", "task_id": "A", "command": "rm -rf /"}, "ADAPTER_REJECTED"),
        ("khoa la", {"action": "dispatch", "task_id": "A", "tuy_y": 1}, "ADAPTER_REJECTED"),
        ("done som", {"action": "done"}, "ADAPTER_REJECTED"),
        ("needs_owner thieu ly do", {"action": "needs_owner"}, "ADAPTER_REJECTED"),
        ("checkpoint thieu next_action", {"action": "checkpoint"}, "ADAPTER_REJECTED"),
    ]
    for name, raw, expect in cases:
        rc, res, o = e.turn(raw)
        oc = str(res.get("outcome", ""))
        ok_out = (expect is None and (oc.startswith("ADAPTER_REJECTED") or oc == "MALFORMED_OUTPUT")) or (expect and oc.startswith(expect))
        check(f"REJECT[{name}]: outcome dung + state khong doi", ok_out and e.n() == n, f"outcome={oc} journal={e.n()} (truoc={n})")
    check("task A van 'ready' va chua co ban ghi nao sau toan bo ca xau", e.task("A")["status"] == "ready" and e.n() == n)
    # code fence được chấp nhận
    rc, res, o = e.turn('Day la quyet dinh:\n```json\n{"action":"dispatch","task_id":"A","reason":"bat dau"}\n```')
    check("JSON boc ```json``` duoc chap nhan va APPLIED", res.get("outcome") == "APPLIED", o)

# ---------- 3. happy path A→B→C bằng fake coordinator
with tempfile.TemporaryDirectory() as tmp:
    e = Env(tmp)
    e.add("A", "w-ok"); e.add("B", "w-ok", deps=["A"]); e.add("C", "w-ok", deps=["B"]); e.qc("sync")
    (e.capdir / "fake-coord.json").write_text((e.capdir / "fake.json").read_text(encoding="utf-8"), encoding="utf-8")
    seq = [{"action": "dispatch", "task_id": "A"}, {"action": "verify", "task_id": "A"}, {"action": "accept", "task_id": "A"},
           {"action": "dispatch", "task_id": "B"}, {"action": "accept", "task_id": "B"},
           {"action": "dispatch", "task_id": "C"}, {"action": "accept", "task_id": "C"}, {"action": "done"}]
    outs = []
    for step in seq:
        e.qc("sync")
        rc, res, o = e.turn(step, coordinator="fake-coord")
        outs.append(res.get("outcome"))
    check("happy path: moi luot APPLIED", all(x == "APPLIED" for x in outs), str(outs))
    e.qc("sync")
    st = e.state()
    check("happy path: A,B,C deu accepted", all(t["status"] == "accepted" for t in st["tasks"]), str([(t["id"], t["status"]) for t in st["tasks"]]))
    rs = recs(e.jr)
    for tid in "ABC":
        check(f"happy path: {tid} chi dispatch dung 1 lan", len([r for r in rs if r.get("task_id") == tid and r.get("status") == "dispatched"]) == 1)
    check("dispatched_by/accepted_by mang ma coordinator 'fake-coord'",
          all(r.get("dispatched_by") == "fake-coord" for r in rs if r.get("status") == "dispatched")
          and all(r.get("accepted_by") == "fake-coord" for r in rs if r.get("status") == "accepted"), str([r for r in rs if r.get("status") in ("dispatched", "accepted")][:2]))
    check("happy path: co checkpoint WORKFLOW_DONE", any(r.get("status") == "checkpoint" and "WORKFLOW_DONE" in str(r.get("next_action")) for r in rs))
    rc, o = e.jc("verify")
    check("happy path: hash-chain PASS", rc == 0, o)
    check("turn_log co dong cho moi luot va khong rong", len(recs(e.tlog)) >= len(seq), str(len(recs(e.tlog))))
    # Core + Adapter: C chỉ chạy sau B accepted -> thứ tự trong journal
    order = [(r["task_id"], r["status"]) for r in rs if r.get("status") in ("dispatched", "accepted")]
    try:
        ok_order = order.index(("B", "dispatched")) > order.index(("A", "accepted")) and order.index(("C", "dispatched")) > order.index(("B", "accepted"))
    except ValueError:
        ok_order = False
    check("thu tu journal: dispatch(B) sau accept(A), dispatch(C) sau accept(B)", ok_order, str(order))

# ---------- 4. worker thất bại -> ghi cơ học 'failed'
for wname, expect in (("w-127", "failed"), ("w-fail", "failed"), ("w-slow", "failed")):
    with tempfile.TemporaryDirectory() as tmp:
        e = Env(tmp)
        e.add("A", wname); e.qc("sync")
        rc, res, o = e.turn({"action": "dispatch", "task_id": "A"})
        t = e.task("A")
        last = [r for r in recs(e.jr) if r.get("task_id") == "A"][-1]
        check(f"worker {wname}: complete failed ghi co hoc (khong 'done')", t["status"] == "failed" and last.get("status") == "failed", f"{t['status']} {o[:160]}")
        check(f"worker {wname}: outcome bao WORKER_FAILED", str(res.get("outcome", "")).startswith("WORKER_FAILED"), str(res))
        check(f"worker {wname}: allowed_actions == ['retry']", [x for x in t.get("allowed_actions", []) if x not in ("switch_worker", "add_signal", "propose_amendment")] == ["retry"], str(t.get("allowed_actions")))
        if wname == "w-127":
            check("worker 127: summary nhac ma khoi dong/127", re.search(r"127|START_FAILED|not found|No such", str(last.get("result_summary", "")), re.I) is not None, str(last.get("result_summary")))

with tempfile.TemporaryDirectory() as tmp:       # worker chưa đăng ký
    e = Env(tmp)
    e.add("A", "w-khong-dang-ky"); e.qc("sync")
    rc, res, o = e.turn({"action": "dispatch", "task_id": "A"})
    last = [r for r in recs(e.jr) if r.get("task_id") == "A"][-1]
    check("worker chua dang ky -> failed + WORKER_NOT_REGISTERED", last.get("status") == "failed" and "WORKER_NOT_REGISTERED" in str(last.get("result_summary")), str(last))

with tempfile.TemporaryDirectory() as tmp:       # worker exit 0 nhưng không tạo file -> Core chặn accept
    e = Env(tmp)
    e.add("A", "w-noout"); e.qc("sync")
    e.turn({"action": "dispatch", "task_id": "A"})
    check("worker exit0 (khong tao file) -> 'done' (tu bao)", e.task("A")["status"] == "done", e.task("A")["status"])
    n = e.n()
    rc, res, o = e.turn({"action": "accept", "task_id": "A"})
    check("accept bi CORE_REFUSED (SIGNAL_FAILED), khong accepted", str(res.get("outcome", "")).startswith("CORE_REFUSED") and e.task("A")["status"] != "accepted", str(res))

# ---------- 5. retry
with tempfile.TemporaryDirectory() as tmp:
    e = Env(tmp)
    e.add("A", "w-fail"); e.qc("sync")
    e.turn({"action": "dispatch", "task_id": "A"})
    n = e.n()
    rc, res, o = e.turn({"action": "retry", "task_id": "A"})
    check("retry thieu reason -> ADAPTER_REJECTED, state khong doi", str(res.get("outcome", "")).startswith("ADAPTER_REJECTED") and e.n() == n, str(res))
    rc, res, o = e.turn({"action": "retry", "task_id": "A", "reason": "thu lai lan 2"})
    d = [r for r in recs(e.jr) if r.get("status") == "dispatched" and r.get("task_id") == "A"][-1]
    check("retry hop le -> attempt A#2 + retry_of, worker van la w-fail tu spec", d.get("attempt_id") == "A#2" and d.get("retry_of") == "A#1" and d.get("worker") == "w-fail", str(d))

# ---------- 6. Core vẫn là authority: spec sửa tay
with tempfile.TemporaryDirectory() as tmp:
    e = Env(tmp)
    e.add("A", "w-ok"); e.qc("sync")
    q = json.loads(Path(e.qu).read_text(encoding="utf-8")); q[0]["worker"] = "evil"
    Path(e.qu).write_text(json.dumps(q), encoding="utf-8")
    n = e.n()
    rc, res, o = e.turn({"action": "dispatch", "task_id": "A"})
    check("spec bi sua tay -> CORE_REFUSED:SPEC_CHANGED va khong dispatch", str(res.get("outcome", "")).startswith("CORE_REFUSED") and "SPEC_CHANGED" in json.dumps(res) and e.n() == n, str(res))

# ---------- 7. needs_owner / checkpoint / context_ref
with tempfile.TemporaryDirectory() as tmp:
    e = Env(tmp)
    e.add("A", "w-ok"); e.qc("sync")
    ctx = e.tmp / "ctx.md"; ctx.write_text("boi canh", encoding="utf-8")
    rc, res, o = e.turn({"action": "needs_owner", "blocked_reason": "can Owner quyet dinh phuong an X", "context_refs": [str(ctx)]})
    st = e.state()
    check("needs_owner -> APPLIED + hien trong state.needs_owner", res.get("outcome") == "APPLIED" and st.get("needs_owner"), str(res) + str(st.get("needs_owner")))
    n = e.n()
    rc, res, o = e.turn({"action": "checkpoint", "next_action": "tiep tuc", "context_refs": [str(e.tmp / "khong_co.md")]})
    check("checkpoint context_ref khong ton tai -> CORE_REFUSED, state khong doi", str(res.get("outcome", "")).startswith("CORE_REFUSED") and e.n() == n, str(res))

# ---------- 8. prompt xác định + không phụ thuộc phiên
with tempfile.TemporaryDirectory() as tmp:
    e = Env(tmp)
    e.add("A", "w-ok"); e.qc("sync")
    hashes = []
    for i in range(2):
        e.turn({"action": "needs_owner", "blocked_reason": "x"}, extra=())      # sẽ APPLIED lần 1; để kiểm tra prompt ta dùng turn bị reject (không đổi state)
    # dùng lượt không đổi state: JSON hỏng
    for env_noise in ({}, {"AO_NOISE": "khac"}):
        e.fake_out.write_text("{hong", encoding="utf-8")
        args = [str(AD), "turn", "--coordinator", "fake", "--workflow", "W", "--journal", e.jr, "--queue", e.qu,
                "--workers", str(e.workers_file), "--capabilities-dir", str(e.capdir), "--turn-log", e.tlog,
                "--claude-md", str(CLAUDE_MD), "--fake-output-file", str(e.fake_out), "--prompt-out", str(e.prompt_out)]
        import os
        envn = dict(os.environ); envn.update(env_noise)
        subprocess.run([sys.executable] + args, capture_output=True, text=True, encoding="utf-8", env=envn)
        hashes.append(hashlib.sha256(e.prompt_out.read_bytes()).hexdigest() if e.prompt_out.exists() else "none")
    check("prompt xac dinh: cung state + cung CLAUDE.md -> cung prompt (khong phu thuoc moi truong/phien)", hashes[0] == hashes[1] and hashes[0] != "none", str(hashes))
    pr = e.prompt_out.read_text(encoding="utf-8")
    check("prompt chua state (allowed_actions) + chinh sach 'So viec ben'", "allowed_actions" in pr and "durable workflow" in pr, pr[:200])
    check("prompt khong chua duong dan phien/transcript cua Claude", not re.search(r"\.claude\\projects|transcript|\.jsonl\b.*claude", pr, re.I), pr[:200])

# ---------- 9. hai Adapter chạy đồng thời cùng dispatch
bad = []
for rnd in range(6):
    with tempfile.TemporaryDirectory() as tmp:
        e = Env(tmp)
        e.add("A", "w-ok"); e.qc("sync")
        procs = []
        for i in range(3):
            (e.capdir / f"c{i}.json").write_text((e.capdir / "fake.json").read_text(encoding="utf-8"), encoding="utf-8")
            fo = e.tmp / f"fake_{i}.txt"; fo.write_text('{"action":"dispatch","task_id":"A"}', encoding="utf-8")
            procs.append(subprocess.Popen([sys.executable, str(AD), "turn", "--coordinator", f"c{i}", "--workflow", "W", "--journal", e.jr, "--queue", e.qu,
                                           "--workers", str(e.workers_file), "--capabilities-dir", str(e.capdir), "--turn-log", str(e.tmp / f"tl{i}.jsonl"),
                                           "--claude-md", str(CLAUDE_MD), "--fake-output-file", str(fo), "--prompt-out", str(e.tmp / f"p{i}.txt")],
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8"))
        for p in procs:
            p.communicate()
        disp = [r for r in recs(e.jr) if r.get("status") == "dispatched"]
        rc, o = e.jc("verify")
        if len(disp) != 1 or rc != 0:
            bad.append((rnd, len(disp), rc))
check("6 vong x 3 Adapter dong thoi cung dispatch -> luon dung 1 dispatched + hash-chain PASS", not bad, str(bad[:3]))


# ---------- 10. PHÁT LẠI câu trả lời THẬT của DeepSeek (lượt thật 2026-10-01) + null-handling
REAL = HERE / "tests" / "fixtures" / "real_deepseek_answer_20261001.txt"
with tempfile.TemporaryDirectory() as tmp:
    e = Env(tmp)
    e.add("A", "w-ok"); e.add("B", "w-ok", deps=["A"]); e.qc("sync")
    e.qc("dispatch", "--id", "A", "--by", "setup")
    (e.tmp / "out_A.txt").write_text("ket qua", encoding="utf-8")
    e.jc("complete", "--task-id", "A", "--attempt-id", "A#1", "--status", "done", "--summary", "setup")
    e.qc("sync")
    n = e.n()
    if REAL.exists():
        rc, res, o = e.turn(REAL.read_text(encoding="utf-8"))
        check("REPLAY cau tra loi THAT cua DeepSeek -> APPLIED verify A", res.get("outcome") == "APPLIED" and res.get("action") == "verify", str(res))
        check("REPLAY: co ban ghi verified cua A trong journal", any(r.get("status") == "verified" and r.get("task_id") == "A" for r in recs(e.jr)[n:]), str(recs(e.jr)[n:][:2]))
    else:
        check("fixture cau tra loi that ton tai", False, str(REAL))
    # null cho trường tuỳ chọn
    base = {"action": "verify", "task_id": "A"}
    for fld in ("reason", "next_action", "blocked_reason", "context_refs"):
        d = dict(base); d[fld] = None
        rc, res, o = e.turn(d)
        check(f"null cho '{fld}' duoc coi la vang mat (khong INVALID_FIELD)", "INVALID_FIELD" not in str(res.get("outcome")) and res.get("outcome") in ("APPLIED",) or str(res.get("outcome", "")).startswith("CORE_REFUSED"), str(res))
    # khoá cấm với giá trị null vẫn bị từ chối
    for fld in ("brief", "worker", "signal", "signals", "command"):
        d = dict(base); d[fld] = None
        n2 = e.n()
        rc, res, o = e.turn(d)
        check(f"khoa cam '{fld}': null van bi FORBIDDEN_FIELD", "FORBIDDEN_FIELD" in str(res.get("outcome")) and e.n() == n2, str(res))
    # retry / needs_owner với null phải bị từ chối đúng mã (không INVALID_FIELD)
    n2 = e.n()
    rc, res, o = e.turn({"action": "needs_owner", "blocked_reason": None})
    check("needs_owner voi blocked_reason=null bi tu choi (khong INVALID_FIELD)", str(res.get("outcome", "")).startswith("ADAPTER_REJECTED") and "INVALID_FIELD" not in str(res.get("outcome")) and e.n() == n2, str(res))
    # kiểu sai vẫn INVALID_FIELD
    rc, res, o = e.turn({"action": "verify", "task_id": 7})
    check("task_id la so -> INVALID_FIELD", "INVALID_FIELD" in str(res.get("outcome")) or str(res.get("outcome", "")).startswith("ADAPTER_REJECTED"), str(res))
    rc, res, o = e.turn({"action": "verify", "task_id": "A", "context_refs": "chuoi"})
    check("context_refs la chuoi -> INVALID_FIELD", "INVALID_FIELD" in str(res.get("outcome")), str(res))

failed = [n for n, ok in results if not ok]
print(f"\nTONG: {len(results) - len(failed)}/{len(results)} PASS")
sys.exit(1 if failed else 0)
