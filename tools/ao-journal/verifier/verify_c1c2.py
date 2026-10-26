"""Kiểm độc lập (AO/Claude viết) cho C1 + C2 — spec bất biến, queue dispatch, allowed_actions do Core sinh, needs_owner.

Chạy: python tools/ao-journal/verifier/verify_c1c2.py
Chỉ dùng thư mục tạm; không đụng journal/queue thật.
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent.parent
J = str(HERE / "journal.py")
Q = str(HERE / "queue.py")
results = []


def run(args):
    p = subprocess.run([sys.executable] + args, capture_output=True, text=True, encoding="utf-8")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{str(detail)[:200]}]" if (detail and not cond) else ""))


def recs(path):
    p = Path(path)
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()] if p.exists() else []


AMEND_ACTIONS = {"switch_worker", "add_signal", "propose_amendment"}  # them boi AMEND (Owner duyet 2026-10-01); khong thuoc C1/C2 goc


def core_actions(t):
    return [x for x in (t.get("allowed_actions") or []) if x not in AMEND_ACTIONS]


def mk(tmp):
    jr, qu = str(tmp / "j.jsonl"), str(tmp / "q.json")
    jc = lambda *a: run([J, "--journal", jr] + list(a))
    qc = lambda *a: run([Q, "--queue", qu, "--journal", jr] + list(a))

    def state(wf=None):
        args = ["state", "--json"] + (["--workflow", wf] if wf else [])
        rc, o = qc(*args)
        return json.loads(o)

    def task(st, tid):
        return next(t for t in st["tasks"] if t["id"] == tid)

    return jr, qu, jc, qc, state, task


with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    jr, qu, jc, qc, state, task = mk(tmp)
    briefA = tmp / "briefA.md"; briefA.write_text("lam A", encoding="utf-8")
    briefB = tmp / "briefB.md"; briefB.write_text("lam B", encoding="utf-8")
    outA = tmp / "outA.txt"; outB = tmp / "outB.txt"

    # --- C1: add có spec + không spec
    rc, o = qc("add", "--id", "A", "--title", "A", "--workflow", "W", "--worker", "w1", "--brief", str(briefA), "--signal", f"file:{outA}")
    check("add voi spec OK", rc == 0, o)
    rc, o = qc("add", "--id", "B", "--title", "B", "--workflow", "W", "--depends-on", "A", "--worker", "w2", "--brief", str(briefB), "--signal", f"file:{outB}")
    check("add B (co phu thuoc) voi spec OK", rc == 0, o)
    rc, o = qc("add", "--id", "L", "--title", "legacy khong spec", "--workflow", "W")
    check("add khong spec van hop le (tuong thich cu)", rc == 0, o)
    rc, o = qc("add", "--id", "A", "--title", "trung")
    check("add trung id bi tu choi", rc != 0, o)
    rc, o = qc("add", "--id", "Z", "--title", "brief khong ton tai", "--worker", "w", "--brief", str(tmp / "khong_co.md"), "--signal", "file:x")
    check("add voi brief khong ton tai bi tu choi", rc != 0, o)
    qc("sync")
    st = state("W")
    a = task(st, "A")
    check("state co spec + spec_hash", a.get("worker") == "w1" and a.get("brief") and a.get("signals") and a.get("spec_hash"), str(a))
    check("A ready -> allowed_actions == ['dispatch']", core_actions(a) == ["dispatch"], str(a.get("allowed_actions")))
    check("B waiting -> allowed_actions == []", core_actions(task(st, "B")) == [], str(task(st, "B")))
    L = task(st, "L")
    check("task ready khong spec -> [] + blocked NO_SPEC", L.get("allowed_actions") == [] and "NO_SPEC" in json.dumps(L), str(L))

    # --- không có đường CLI sửa spec / override
    for sub in ("edit", "update", "set", "amend"):
        rc, o = qc(sub, "--id", "A", "--worker", "hack")
        check(f"khong co lenh '{sub}' de sua spec", rc != 0, o)
    rc, o = qc("dispatch", "--id", "A", "--worker", "hacker", "--by", "t")
    check("dispatch khong nhan --worker (override bi tu choi)", rc != 0, o)
    rc, o = qc("dispatch", "--id", "A", "--signal", "file:de_dang", "--by", "t")
    check("dispatch khong nhan --signal (override bi tu choi)", rc != 0, o)
    rc, o = qc("dispatch", "--id", "A", "--brief", str(briefB), "--by", "t")
    check("dispatch khong nhan --brief (override bi tu choi)", rc != 0, o)

    # --- dispatch lấy đúng spec
    n0 = len(recs(jr))
    rc, o = qc("dispatch", "--id", "A", "--by", "coordX")
    check("dispatch A OK", rc == 0, o)
    d = [r for r in recs(jr) if r.get("status") == "dispatched" and r.get("task_id") == "A"][-1]
    check("receipt dung worker/signals/brief tu spec", d.get("worker") == "w1" and d.get("completion_signals") == [f"file:{outA}"] and Path(d.get("brief_path")).resolve() == briefA.resolve(), str(d))
    check("receipt co attempt_id A#1 + dispatched_by", d.get("attempt_id") == "A#1" and d.get("dispatched_by") == "coordX", str(d))
    n1 = len(recs(jr))
    rc, o = qc("dispatch", "--id", "A", "--by", "coordX")
    check("dispatch lan 2 (dang bay) bi tu choi", rc != 0 and len(recs(jr)) == n1, o)
    rc, o = qc("dispatch", "--id", "B", "--by", "coordX")
    check("dispatch B khi A chua accepted bi tu choi (khong thuoc allowed)", rc != 0 and len(recs(jr)) == n1, o)
    st = state("W")
    check("A dispatched -> allowed_actions == []", task(st, "A").get("allowed_actions") == [], str(task(st, "A").get("allowed_actions")))

    # --- done -> verify/accept ; accept ; B ready
    outA.write_text("ket qua A", encoding="utf-8")
    jc("complete", "--task-id", "A", "--attempt-id", "A#1", "--status", "done", "--summary", "xong")
    st = state("W")
    check("A done -> allowed_actions == ['verify','accept']", sorted(task(st, "A").get("allowed_actions", [])) == ["accept", "verify"], str(task(st, "A").get("allowed_actions")))
    check("workflow_allowed chua co 'done' khi chua het accepted", "done" not in st["workflow_allowed"].get("W", []), str(st["workflow_allowed"]))
    rc, o = jc("accept", "--task-id", "A", "--by", "coordX")
    qc("sync")
    st = state("W")
    check("A accepted -> allowed_actions == []", task(st, "A").get("allowed_actions") == [], str(task(st, "A").get("allowed_actions")))
    check("B ready -> ['dispatch'] sau khi A accepted", core_actions(task(st, "B")) == ["dispatch"], str(task(st, "B")))
    n = len(recs(jr))
    rc, o = qc("dispatch", "--id", "A", "--by", "coordX")
    rc2, o2 = qc("dispatch", "--id", "A", "--retry", "--reason", "thu", "--by", "coordX")
    check("dispatch/--retry task da ACCEPTED bi tu choi", rc != 0 and rc2 != 0 and len(recs(jr)) == n, o + o2)

    # --- failed -> retry
    rc, o = qc("dispatch", "--id", "B", "--by", "coordY")
    jc("complete", "--task-id", "B", "--attempt-id", "B#1", "--status", "failed", "--summary", "hong")
    st = state("W")
    check("B failed -> allowed_actions == ['retry']", core_actions(task(st, "B")) == ["retry"], str(task(st, "B").get("allowed_actions")))
    n = len(recs(jr))
    rc, o = qc("dispatch", "--id", "B", "--by", "coordY")
    check("dispatch task failed khong co --retry bi tu choi", rc != 0 and len(recs(jr)) == n, o)
    rc, o = qc("dispatch", "--id", "B", "--retry", "--by", "coordY")
    check("--retry thieu --reason bi tu choi", rc != 0 and len(recs(jr)) == n, o)
    rc, o = qc("dispatch", "--id", "B", "--retry", "--reason", "thu lai lan 2", "--by", "coordY")
    d2 = [r for r in recs(jr) if r.get("status") == "dispatched" and r.get("task_id") == "B"][-1]
    check("retry hop le -> B#2 + retry_of, worker/signals van tu spec", rc == 0 and d2.get("attempt_id") == "B#2" and d2.get("retry_of") == "B#1" and d2.get("completion_signals") == [f"file:{outB}"] and d2.get("worker") == "w2", str(d2))

    # --- workflow_allowed + done
    outB.write_text("ket qua B", encoding="utf-8")
    jc("complete", "--task-id", "B", "--attempt-id", "B#2", "--status", "done", "--summary", "xong")
    jc("accept", "--task-id", "B", "--by", "coordY")
    qc("sync")
    st = state("W")
    # L chưa accepted => 'done' KHÔNG được có
    check("co task chua accepted (L) -> khong cho 'done'", "done" not in st["workflow_allowed"].get("W", []), str(st["workflow_allowed"]))

    # --- needs_owner
    rc, o = jc("checkpoint", "--workflow", "W", "--by", "coordY", "--next-action", "cho Owner", "--blocked-reason", "NEEDS_OWNER: can quyet dinh X", "--context-ref", str(briefA))
    st = state("W")
    check("needs_owner hien khi checkpoint moi nhat co NEEDS_OWNER", any("W" in json.dumps(x) for x in st.get("needs_owner", [])) and st.get("needs_owner"), str(st.get("needs_owner")))
    jc("checkpoint", "--workflow", "W", "--by", "coordY", "--next-action", "da duoc go roi", "--context-ref", str(briefA))
    st = state("W")
    check("needs_owner bien mat khi checkpoint moi nhat khong con block", not st.get("needs_owner"), str(st.get("needs_owner")))
    jc("checkpoint", "--workflow", "W", "--by", "coordY", "--next-action", "x", "--blocked-reason", "ly do khac, khong phai NEEDS_OWNER", "--context-ref", str(briefA))
    st = state("W")
    check("blocked_reason khong bat dau NEEDS_OWNER: khong vao needs_owner", not st.get("needs_owner"), str(st.get("needs_owner")))

    # --- lọc workflow
    qc("add", "--id", "X1", "--title", "khac workflow", "--workflow", "OTHER", "--worker", "w", "--brief", str(briefA), "--signal", f"file:{outA}")
    st = state("OTHER")
    check("--workflow OTHER chi thay task cua OTHER", [t["id"] for t in st["tasks"]] == ["X1"], str([t["id"] for t in st["tasks"]]))

# --- done chỉ khi mọi task của workflow đã accepted và có >=1 task
with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    jr, qu, jc, qc, state, task = mk(tmp)
    b = tmp / "b.md"; b.write_text("x", encoding="utf-8")
    o1 = tmp / "o1.txt"; o1.write_text("y", encoding="utf-8")
    qc("add", "--id", "P", "--title", "P", "--workflow", "D", "--worker", "w", "--brief", str(b), "--signal", f"file:{o1}")
    qc("sync")
    check("workflow chua accepted -> khong co 'done'", "done" not in state("D")["workflow_allowed"].get("D", []))
    qc("dispatch", "--id", "P", "--by", "c")
    jc("complete", "--task-id", "P", "--attempt-id", "P#1", "--status", "done", "--summary", "ok")
    jc("accept", "--task-id", "P", "--by", "c")
    st = state("D")
    check("moi task cua workflow accepted -> workflow_allowed co 'done'", "done" in st["workflow_allowed"].get("D", []), str(st["workflow_allowed"]))

# --- SPEC_CHANGED: sửa tay queue.json
with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    jr, qu, jc, qc, state, task = mk(tmp)
    b = tmp / "b.md"; b.write_text("x", encoding="utf-8")
    o = tmp / "o.txt"
    qc("add", "--id", "S", "--title", "S", "--workflow", "W", "--worker", "w", "--brief", str(b), "--signal", f"file:{o}")
    qc("sync")
    q = json.loads(Path(qu).read_text(encoding="utf-8"))
    q[0]["signals"] = ["file:" + str(b)]          # hạ chuẩn "xong" bằng cách sửa tay
    Path(qu).write_text(json.dumps(q), encoding="utf-8")
    n = len(recs(jr))
    rc, o2 = qc("dispatch", "--id", "S", "--by", "c")
    check("spec bi sua tay -> SPEC_CHANGED va khong dispatch", rc != 0 and "SPEC_CHANGED" in o2 and len(recs(jr)) == n, o2)
    q[0]["worker"] = "evil"
    Path(qu).write_text(json.dumps(q), encoding="utf-8")
    rc, o2 = qc("dispatch", "--id", "S", "--by", "c")
    check("doi worker bang tay cung bi SPEC_CHANGED", rc != 0 and len(recs(jr)) == n, o2)

# --- đồng thời: 8 dispatch cùng task, lặp nhiều vòng
bad = []
for rnd in range(12):
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        jr, qu, jc, qc, state, task = mk(tmp)
        b = tmp / "b.md"; b.write_text("x", encoding="utf-8")
        o = tmp / "o.txt"
        qc("add", "--id", "R", "--title", "R", "--workflow", "W", "--worker", "w", "--brief", str(b), "--signal", f"file:{o}")
        qc("sync")
        procs = [subprocess.Popen([sys.executable, Q, "--queue", qu, "--journal", jr, "dispatch", "--id", "R", "--by", f"c{i}"],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8") for i in range(8)]
        for p in procs:
            p.communicate()
        ok = sum(1 for p in procs if p.returncode == 0)
        rs = recs(jr)
        disp = [r for r in rs if r.get("status") == "dispatched"]
        rcv, ov = jc("verify")
        if ok != 1 or len(disp) != 1 or rcv != 0:
            bad.append((rnd, ok, len(disp), rcv))
check("12 vong x 8 queue dispatch dong thoi -> luon dung 1 thanh cong + hash-chain PASS", not bad, str(bad[:4]))

failed = [n for n, ok in results if not ok]
print(f"\nTONG: {len(results) - len(failed)}/{len(results)} PASS")
sys.exit(1 if failed else 0)
