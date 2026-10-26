"""Kiểm độc lập (AO/Claude viết) cho PTP bước 3 — attempt_id + idempotency + checkpoint, mức dòng lệnh.

Chạy: python tools/ao-journal/verifier/verify_step3.py
Chỉ dùng thư mục tạm; không đụng journal/queue thật.
"""
import hashlib
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
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail[:170]}]" if (detail and not cond) else ""))


def recs(path):
    p = Path(path)
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()] if p.exists() else []


with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    jr, qu = str(tmp / "j.jsonl"), str(tmp / "q.json")
    jc = lambda *a: run([J, "--journal", jr] + list(a))
    qc = lambda *a: run([Q, "--queue", qu, "--journal", jr] + list(a))
    brief = tmp / "brief.md"
    brief.write_text("lam A", encoding="utf-8")
    outA = tmp / "outA.txt"
    outA.write_text("A xong", encoding="utf-8")

    # --- attempt_id + idempotency_key
    rc, o = jc("append", "--task-id", "A", "--worker", "w1", "--brief", str(brief), "--signal", f"file:{outA}")
    d = [r for r in recs(jr) if r.get("status") == "dispatched"][-1]
    check("dispatch co attempt_id = A#1", d.get("attempt_id") == "A#1", str(d.get("attempt_id")))
    exp_key = hashlib.sha256(("A" + d["brief_hash"]).encode("utf-8")).hexdigest()
    check("idempotency_key = sha256(task_id + brief_hash)", d.get("idempotency_key") == exp_key, str(d.get("idempotency_key")))

    # --- dispatch lại khi attempt đang bay
    n = len(recs(jr))
    rc, o = jc("append", "--task-id", "A", "--worker", "w2", "--brief", str(brief), "--signal", f"file:{outA}")
    check("dispatch lan 2 khi attempt dang bay bi tu choi (ATTEMPT_IN_FLIGHT)", rc != 0 and "ATTEMPT_IN_FLIGHT" in o and len(recs(jr)) == n, o)

    # --- complete bắt buộc attempt-id và phải khớp
    rc, o = jc("complete", "--task-id", "A", "--status", "done", "--summary", "thieu attempt")
    check("complete thieu --attempt-id bi tu choi", rc != 0 and len(recs(jr)) == n, o)
    rc, o = jc("complete", "--task-id", "A", "--attempt-id", "A#9", "--status", "done", "--summary", "sai attempt")
    check("complete attempt-id sai bi tu choi (STALE_RESULT)", rc != 0 and "STALE_RESULT" in o and len(recs(jr)) == n, o)
    rc, o = jc("complete", "--task-id", "A", "--attempt-id", "A#1", "--status", "done", "--summary", "xong that")
    check("complete dung attempt-id OK", rc == 0, o)
    rc, o = jc("accept", "--task-id", "A", "--by", "coordinatorX")
    check("accept OK", rc == 0, o)

    # --- không chạy lại task đã ACCEPTED
    n = len(recs(jr))
    rc, o = jc("append", "--task-id", "A", "--worker", "w1", "--brief", str(brief), "--signal", f"file:{outA}")
    check("dispatch task da ACCEPTED bi tu choi (ALREADY_ACCEPTED)", rc != 0 and "ALREADY_ACCEPTED" in o and len(recs(jr)) == n, o)
    rc, o = jc("append", "--task-id", "A", "--worker", "w1", "--brief", str(brief), "--signal", f"file:{outA}", "--retry", "--reason", "thu lai")
    check("--retry cung KHONG vuot duoc ALREADY_ACCEPTED", rc != 0 and len(recs(jr)) == n, o)
    # kết quả muộn của attempt cũ sau khi accepted
    rc, o = jc("complete", "--task-id", "A", "--attempt-id", "A#1", "--status", "failed", "--summary", "ket qua muon")
    check("ket qua muon sau khi ACCEPTED khong ghi de trang thai", len(recs(jr)) == n, o)

    # --- failed -> cần --retry --reason
    briefB = tmp / "briefB.md"
    briefB.write_text("lam B", encoding="utf-8")
    outB = tmp / "outB.txt"
    jc("append", "--task-id", "B", "--worker", "w1", "--brief", str(briefB), "--signal", f"file:{outB}")
    jc("complete", "--task-id", "B", "--attempt-id", "B#1", "--status", "failed", "--summary", "hong")
    n = len(recs(jr))
    rc, o = jc("append", "--task-id", "B", "--worker", "w1", "--brief", str(briefB), "--signal", f"file:{outB}")
    check("dispatch lai task failed khong co --retry bi tu choi", rc != 0 and len(recs(jr)) == n, o)
    rc, o = jc("append", "--task-id", "B", "--worker", "w2", "--brief", str(briefB), "--signal", f"file:{outB}", "--retry")
    check("--retry thieu --reason bi tu choi", rc != 0 and len(recs(jr)) == n, o)
    rc, o = jc("append", "--task-id", "B", "--worker", "w2", "--brief", str(briefB), "--signal", f"file:{outB}", "--retry", "--reason", "worker khac")
    d2 = [r for r in recs(jr) if r.get("status") == "dispatched" and r.get("task_id") == "B"][-1]
    check("retry hop le -> attempt B#2 + retry_of + retry_reason", rc == 0 and d2.get("attempt_id") == "B#2" and d2.get("retry_of") and d2.get("retry_reason"), str(d2))
    # kết quả muộn của attempt 1 sau khi đã có attempt 2
    n = len(recs(jr))
    rc, o = jc("complete", "--task-id", "B", "--attempt-id", "B#1", "--status", "done", "--summary", "ket qua muon cua attempt cu")
    check("ket qua attempt cu (B#1) bi tu choi khi dang o B#2", rc != 0 and len(recs(jr)) == n, o)
    outB.write_text("B xong", encoding="utf-8")
    rc, o = jc("complete", "--task-id", "B", "--attempt-id", "B#2", "--status", "done", "--summary", "B#2 xong")
    rc, o = jc("accept", "--task-id", "B", "--by", "coordinatorY")
    check("B#2 accept OK", rc == 0, o)

    # --- checkpoint
    ctx = tmp / "context_note.md"
    ctx.write_text("boi canh", encoding="utf-8")
    n = len(recs(jr))
    rc, o = jc("checkpoint", "--task-id", "C", "--by", "claude-code", "--next-action", "giao B cho worker w3", "--context-ref", str(ctx))
    cp = recs(jr)[-1]
    check("checkpoint ghi duoc status=checkpoint", rc == 0 and cp.get("status") == "checkpoint" and cp.get("next_action") == "giao B cho worker w3", o)
    check("checkpoint co by + context_refs", cp.get("by") == "claude-code" and cp.get("context_refs"), str(cp))
    n2 = len(recs(jr))
    rc, o = jc("checkpoint", "--task-id", "C", "--by", "x", "--next-action", "y", "--context-ref", str(tmp / "khong_co.md"))
    check("checkpoint voi context-ref khong ton tai bi tu choi", rc != 0 and len(recs(jr)) == n2, o)
    # workflow checkpoint không cần task
    rc, o = jc("checkpoint", "--workflow", "W1", "--by", "deepseek", "--next-action", "chay tiep C", "--blocked-reason", "cho Owner duyet", "--approval-ref", "log#12", "--context-ref", str(ctx))
    check("checkpoint theo --workflow (co blocked_reason + approval_ref)", rc == 0, o)
    # checkpoint không đổi trạng thái task
    qc("add", "--id", "A", "--title", "Task A")
    qc("add", "--id", "B", "--title", "Task B", "--depends-on", "A")
    qc("sync")
    rc, o = qc("state", "--json")
    st = json.loads(o)
    tasks = st["tasks"] if isinstance(st, dict) and "tasks" in st else st
    status = {t["id"]: t.get("status") for t in tasks}
    check("state: A accepted, B accepted (checkpoint khong doi trang thai)", status.get("A") == "accepted" and status.get("B") == "accepted", str(status))
    txt = json.dumps(st, ensure_ascii=False)
    check("state hien thi checkpoint/next_action", "chay tiep C" in txt or "giao B cho worker w3" in txt, txt[:200])
    check("state hien thi attempt_id", "A#1" in txt and "B#2" in txt, txt[:200])

    # --- stale_dispatch
    jc("append", "--task-id", "S", "--worker", "w", "--brief", str(brief), "--signal", f"file:{outA}")
    rc, o = qc("state", "--json", "--stale-hours", "0")
    check("state --stale-hours 0 danh dau stale_dispatch", "stale_dispatch" in o, o[:200])
    rc, o = jc("verify")
    check("hash-chain van PASS sau toan bo thao tac", rc == 0, o)


# --- ĐỒNG THỜI (race): nhiều coordinator tranh nhau cùng lúc. Lỗi ngẫu nhiên nên chạy NHIỀU VÒNG.
ROUNDS = 15
bad_dispatch, bad_accept, bad_chain = [], [], []
for rnd in range(ROUNDS):
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        jr = str(tmp / "j.jsonl")
        brief = tmp / "b.md"; brief.write_text("x", encoding="utf-8")
        out = tmp / "o.txt"; out.write_text("y", encoding="utf-8")
        procs = [subprocess.Popen([sys.executable, J, "--journal", jr, "append", "--task-id", "R", "--worker", f"w{i}",
                                   "--brief", str(brief), "--signal", f"file:{out}"],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8") for i in range(8)]
        for p in procs:
            p.communicate()
        ok_count = sum(1 for p in procs if p.returncode == 0)
        rs = recs(jr)
        disp = [r for r in rs if r.get("status") == "dispatched"]
        if ok_count != 1 or len(disp) != 1 or [r.get("seq") for r in rs] != list(range(1, len(rs) + 1)):
            bad_dispatch.append((rnd, ok_count, len(disp)))
        run([J, "--journal", jr, "complete", "--task-id", "R", "--attempt-id", "R#1", "--status", "done", "--summary", "xong"])
        procs = [subprocess.Popen([sys.executable, J, "--journal", jr, "accept", "--task-id", "R", "--by", f"c{i}"],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8") for i in range(6)]
        for p in procs:
            p.communicate()
        acc = [r for r in recs(jr) if r.get("status") == "accepted"]
        if len(acc) != 1:
            bad_accept.append((rnd, len(acc)))
        rc, o = run([J, "--journal", jr, "verify"])
        if rc != 0:
            bad_chain.append(rnd)
check(f"RACE: {ROUNDS} vong x 8 dispatch dong thoi -> luon dung 1 thanh cong + 1 ban ghi + seq lien tuc", not bad_dispatch, str(bad_dispatch[:4]))
check(f"RACE: {ROUNDS} vong x 6 accept dong thoi -> luon dung 1 ban ghi accepted", not bad_accept, str(bad_accept[:4]))
check(f"RACE: hash-chain luon PASS sau {ROUNDS} vong", not bad_chain, str(bad_chain[:4]))

failed = [n for n, ok in results if not ok]
print(f"\nTONG: {len(results) - len(failed)}/{len(results)} PASS")
sys.exit(1 if failed else 0)
