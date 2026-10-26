"""Kiểm độc lập (AO/Claude viết) cho PTP bước 2 — verification gate, mức dòng lệnh.

Chạy: python tools/ao-journal/verifier/verify_step2.py
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
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail[:160]}]" if (detail and not cond) else ""))


def lines(path):
    p = Path(path)
    return len([l for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]) if p.exists() else 0


def recs(path):
    return [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]


def fresh(tmp, name):
    """Tạo task name đã dispatched + done (worker tự báo) với brief+output hợp lệ."""
    brief = tmp / f"brief_{name}.md"
    out = tmp / f"out_{name}.txt"
    brief.write_text("brief " + name, encoding="utf-8")
    out.write_text("ket qua " + name, encoding="utf-8")
    jc("append", "--task-id", name, "--worker", "w", "--brief", str(brief), "--signal", f"file:{out}")
    jc("complete", "--task-id", name, "--attempt-id", name + "#1", "--status", "done", "--summary", "xong")
    return brief, out


with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    jr, qu = str(tmp / "j.jsonl"), str(tmp / "q.json")
    jc = lambda *a: run([J, "--journal", jr] + list(a))
    qc = lambda *a: run([Q, "--queue", qu, "--journal", jr] + list(a))

    def no_accepted_added(task, before):
        return not any(r.get("task_id") == task and r.get("status") == "accepted" for r in recs(jr)[before:])

    # --- happy path có verify-task + guard PASS
    brief, out = fresh(tmp, "H")
    guard = tmp / "guard.txt"
    guard.write_text("GUARD_RESULT: PASS exit_code=0\n", encoding="utf-8")  # dong that guard_run.py in ra
    rc, o = jc("verify-task", "--task-id", "H")
    check("verify-task chay duoc", rc == 0, o)
    vrec = [r for r in recs(jr) if r.get("status") == "verified" and r.get("task_id") == "H"]
    check("co ban ghi verified", len(vrec) == 1)
    rc, o = jc("accept", "--task-id", "H", "--by", "t", "--guard-report", str(guard))
    check("accept happy path (verify + guard PASS) OK", rc == 0, o)
    acc = [r for r in recs(jr) if r.get("status") == "accepted" and r.get("task_id") == "H"]
    check("accepted co verified_seq + gate", acc and "verified_seq" in acc[-1] and "gate" in acc[-1], str(acc[-1:] ))

    # --- BRIEF_CHANGED
    brief, out = fresh(tmp, "BC")
    brief.write_text("brief da bi sua sau khi giao", encoding="utf-8")
    n = lines(jr)
    rc, o = jc("accept", "--task-id", "BC", "--by", "t")
    check("BRIEF_CHANGED bi tu choi", rc != 0 and "BRIEF_CHANGED" in o and no_accepted_added("BC", n), o)

    # --- brief bị xoá
    brief, out = fresh(tmp, "BD")
    brief.unlink()
    n = lines(jr)
    rc, o = jc("accept", "--task-id", "BD", "--by", "t")
    check("brief bi xoa -> tu choi (khong accept)", rc != 0 and no_accepted_added("BD", n), o)

    # --- OUTPUT_CHANGED giữa verify và accept
    brief, out = fresh(tmp, "OC")
    rc, o = jc("verify-task", "--task-id", "OC")
    out.write_text("file da bi doi sau khi verify", encoding="utf-8")
    n = lines(jr)
    rc, o = jc("accept", "--task-id", "OC", "--by", "t")
    check("OUTPUT_CHANGED (sua file giua verify va accept) bi tu choi", rc != 0 and "OUTPUT_CHANGED" in o and no_accepted_added("OC", n), o)

    # --- file bị xoá sau verify
    brief, out = fresh(tmp, "OD")
    jc("verify-task", "--task-id", "OD")
    out.unlink()
    n = lines(jr)
    rc, o = jc("accept", "--task-id", "OD", "--by", "t")
    check("file bang chung bi xoa sau verify -> tu choi", rc != 0 and no_accepted_added("OD", n), o)

    # --- GUARD_NOT_PASS (WARN) và guard report không tồn tại
    brief, out = fresh(tmp, "GW")
    warn = tmp / "guard_warn.txt"
    warn.write_text("GUARD_RESULT: WARN N_OUT_OF_SCOPE=2 exit_code=0\n", encoding="utf-8")
    n = lines(jr)
    rc, o = jc("accept", "--task-id", "GW", "--by", "t", "--guard-report", str(warn))
    check("GUARD_NOT_PASS khi report WARN", rc != 0 and "GUARD_NOT_PASS" in o and no_accepted_added("GW", n), o)
    rc, o = jc("accept", "--task-id", "GW", "--by", "t", "--guard-report", str(tmp / "khong_co.txt"))
    check("GUARD_NOT_PASS khi report khong ton tai", rc != 0 and no_accepted_added("GW", n), o)
    rc, o = jc("accept", "--task-id", "GW", "--by", "t")
    check("cung task accept OK khi khong yeu cau guard (file con nguyen)", rc == 0, o)

    # --- guard: tiền tố lạ, PASS rồi WARN (lấy kết quả cuối), PASS exit_code=1 vẫn hợp lệ
    brief, out = fresh(tmp, "GP")
    pre = tmp / "guard_prefix.txt"
    pre.write_text("... GUARD_RESULT: PASS exit_code=0\n", encoding="utf-8")
    n = lines(jr)
    rc, o = jc("accept", "--task-id", "GP", "--by", "t", "--guard-report", str(pre))
    check("guard co tien to la bi tu choi", rc != 0 and no_accepted_added("GP", n), o)
    pw = tmp / "guard_pass_then_warn.txt"
    pw.write_text("GUARD_RESULT: PASS exit_code=0\nGUARD_RESULT: WARN N_OUT_OF_SCOPE=1 exit_code=0\n", encoding="utf-8")
    rc, o = jc("accept", "--task-id", "GP", "--by", "t", "--guard-report", str(pw))
    check("guard PASS roi WARN (ket qua cuoi WARN) bi tu choi", rc != 0 and no_accepted_added("GP", n), o)
    p1 = tmp / "guard_pass_exit1.txt"
    p1.write_text("GUARD_RESULT: PASS exit_code=1\n", encoding="utf-8")
    rc, o = jc("accept", "--task-id", "GP", "--by", "t", "--guard-report", str(p1))
    check("guard PASS exit_code=1 duoc chap nhan (signals quyet dinh)", rc == 0, o)

    # --- NO_SIGNALS, SIGNAL_FAILED, NOT_DONE
    jc("append", "--task-id", "NS", "--worker", "w", "--brief", str(tmp / "brief_H.md"))
    jc("complete", "--task-id", "NS", "--attempt-id", "NS#1", "--status", "done", "--summary", "xong")
    n = lines(jr)
    rc, o = jc("accept", "--task-id", "NS", "--by", "t")
    check("NO_SIGNALS bi tu choi", rc != 0 and "NO_SIGNALS" in o and no_accepted_added("NS", n), o)

    brief, out = fresh(tmp, "SF")
    out.unlink()
    n = lines(jr)
    rc, o = jc("accept", "--task-id", "SF", "--by", "t")
    check("SIGNAL_FAILED bi tu choi", rc != 0 and "SIGNAL_FAILED" in o and no_accepted_added("SF", n), o)

    jc("append", "--task-id", "ND", "--worker", "w", "--brief", str(tmp / "brief_H.md"), "--signal", f"file:{tmp / 'brief_H.md'}")
    n = lines(jr)
    rc, o = jc("accept", "--task-id", "ND", "--by", "t")
    check("NOT_DONE bi tu choi", rc != 0 and "NOT_DONE" in o and no_accepted_added("ND", n), o)

    # --- CHAIN_BROKEN
    brief, out = fresh(tmp, "CB")
    ls = Path(jr).read_text(encoding="utf-8").splitlines()
    ls[0] = ls[0].replace('"worker":"w"', '"worker":"x"', 1)
    Path(jr).write_text("\n".join(ls) + "\n", encoding="utf-8")
    n = lines(jr)
    rc, o = jc("accept", "--task-id", "CB", "--by", "t")
    check("CHAIN_BROKEN: accept bi chan khi journal bi sua", rc != 0 and no_accepted_added("CB", n), o)

failed = [n for n, ok in results if not ok]
print(f"\nTONG: {len(results) - len(failed)}/{len(results)} PASS")
sys.exit(1 if failed else 0)
