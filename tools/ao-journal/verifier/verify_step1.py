"""Kiểm độc lập (do AO/Claude viết, KHÔNG phải test của worker) cho PTP bước 1, ở mức dòng lệnh.

Chạy: python tools/ao-journal/verifier/verify_step1.py
Mọi thứ nằm trong thư mục tạm; không đụng journal.jsonl / queue.json thật.
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
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if (detail and not cond) else ""))


def lines(path):
    p = Path(path)
    return len([l for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]) if p.exists() else 0


with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    jr, qu = str(tmp / "j.jsonl"), str(tmp / "q.json")
    jc = lambda *a: run([J, "--journal", jr] + list(a))
    qc = lambda *a: run([Q, "--queue", qu, "--journal", jr] + list(a))

    briefA, briefB = tmp / "briefA.md", tmp / "briefB.md"
    briefA.write_text("lam A", encoding="utf-8")
    briefB.write_text("lam B", encoding="utf-8")
    outA, outB = tmp / "outA.txt", tmp / "outB.txt"

    qc("add", "--id", "A", "--title", "Task A")
    qc("add", "--id", "B", "--title", "Task B", "--depends-on", "A")
    rc, out = jc("append", "--task-id", "A", "--worker", "w1", "--brief", str(briefA), "--signal", f"file:{outA}")
    check("A dispatch ghi duoc", rc == 0, out)
    n0 = lines(jr)

    # worker báo done nhưng KHÔNG có file bằng chứng
    rc, out = jc("complete", "--task-id", "A", "--attempt-id", "A#1", "--status", "done", "--summary", "worker noi xong")
    check("complete done ghi duoc (worker tu bao)", rc == 0, out)
    n1 = lines(jr)
    rc, out = jc("accept", "--task-id", "A", "--by", "tester")
    check("accept bi TU CHOI khi file bang chung thieu", rc != 0, out)
    check("journal khong them dong khi accept bi tu choi", lines(jr) == n1)
    qc("sync")
    rc, out = qc("state", "--json")
    st = json.loads(out)
    tasks = st["tasks"] if isinstance(st, dict) and "tasks" in st else st
    getst = lambda tid: next(t for t in tasks if t.get("id") == tid).get("status")
    check("B van waiting khi A moi 'done' (chua accepted)", getst("B") == "waiting", str(getst("B")))

    # có file -> accept OK
    outA.write_text("ket qua A", encoding="utf-8")
    rc, out = jc("accept", "--task-id", "A", "--by", "tester")
    check("accept thanh cong khi file co mat", rc == 0, out)
    n2 = lines(jr)
    rc, out = jc("accept", "--task-id", "A", "--by", "tester2")
    check("accept lan 2 = ALREADY_ACCEPTED, exit 0", rc == 0 and "ALREADY_ACCEPTED" in out, out)
    check("accept lan 2 khong ghi them dong", lines(jr) == n2)
    qc("sync")
    rc, out = qc("state", "--json")
    st = json.loads(out)
    tasks = st["tasks"] if isinstance(st, dict) and "tasks" in st else st
    check("A accepted trong state", getst("A") == "accepted", str(getst("A")))
    check("B ready sau khi A accepted", getst("B") == "ready", str(getst("B")))
    check("state co journal_verify PASS", "PASS" in json.dumps(st, ensure_ascii=False).upper(), "")
    rc, out = jc("verify")
    check("hash-chain verify PASS", rc == 0, out)
    j_text = Path(jr).read_text(encoding="utf-8")
    check("co ban ghi accepted voi output_hashes", '"accepted"' in j_text and "output_hashes" in j_text)

    # chưa dispatch / không tồn tại
    rc, out = jc("accept", "--task-id", "ZZZ", "--by", "tester")
    check("accept task khong ton tai bi tu choi", rc != 0, out)

    # task chỉ dispatched (chưa done)
    outB.write_text("x", encoding="utf-8")
    jc("append", "--task-id", "B", "--worker", "w1", "--brief", str(briefB), "--signal", f"file:{outB}")
    rc, out = jc("accept", "--task-id", "B", "--by", "tester")
    check("accept task moi 'dispatched' (chua done) bi tu choi", rc != 0, out)

    # task failed
    jc("complete", "--task-id", "B", "--attempt-id", "B#1", "--status", "failed", "--summary", "hong")
    rc, out = jc("accept", "--task-id", "B", "--by", "tester")
    check("accept task 'failed' bi tu choi", rc != 0, out)

    # task không có signal
    jc("append", "--task-id", "C", "--worker", "w1", "--brief", str(briefA))
    jc("complete", "--task-id", "C", "--attempt-id", "C#1", "--status", "done", "--summary", "xong")
    nC = lines(jr)
    rc, out = jc("accept", "--task-id", "C", "--by", "tester")
    check("accept task khong co signal bi tu choi", rc != 0 and lines(jr) == nC, out)

    # signal dạng cmd: exit khác kỳ vọng
    jc("append", "--task-id", "D", "--worker", "w1", "--brief", str(briefA), "--signal", 'cmd:python -c "import sys; sys.exit(3)" exit=0')
    jc("complete", "--task-id", "D", "--attempt-id", "D#1", "--status", "done", "--summary", "xong")
    nD = lines(jr)
    rc, out = jc("accept", "--task-id", "D", "--by", "tester")
    check("accept bi tu choi khi signal cmd that bai", rc != 0 and lines(jr) == nD, out)

    # phá hash-chain
    ls = Path(jr).read_text(encoding="utf-8").splitlines()
    ls[0] = ls[0].replace('"w1"', '"hacker"', 1)
    Path(jr).write_text("\n".join(ls) + "\n", encoding="utf-8")
    rc, out = jc("verify")
    check("verify phat hien sua noi dung (chain broken)", rc != 0, out)

failed = [n for n, ok in results if not ok]
print(f"\nTONG: {len(results) - len(failed)}/{len(results)} PASS")
sys.exit(1 if failed else 0)
