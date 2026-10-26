"""Kiểm độc lập (AO/Claude viết) cho AMEND Core: fallback_workers, add-signal (chỉ thêm), propose/approve/reject amendment, spec version.

Chạy: python tools/ao-journal/verifier/verify_amend.py
Chỉ dùng thư mục tạm; không đụng journal/queue thật.
"""
import json
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
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
    print(("PASS " if cond else "FAIL ") + name + (f"  [{str(detail)[:300]}]" if (detail and not cond) else ""))


def recs(path):
    p = Path(path)
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()] if p.exists() else []


def mk(tmp):
    jr, qu = str(tmp / "j.jsonl"), str(tmp / "q.json")
    jc = lambda *a: run([J, "--journal", jr] + list(a))
    qc = lambda *a: run([Q, "--queue", qu, "--journal", jr] + list(a))

    def state(wf=None):
        rc, o = qc("state", "--json", *(["--workflow", wf] if wf else []))
        return json.loads(o)

    def task(st, tid):
        return next(t for t in st["tasks"] if t["id"] == tid)

    return jr, qu, jc, qc, state, task


def jload(o):
    try:
        return json.loads(o)
    except Exception:
        i, k = o.find("{"), o.rfind("}")
        if i >= 0 and k > i:
            try:
                return json.loads(o[i:k + 1])
            except Exception:
                pass
        m = re.search(r"amendment_id\D{0,4}(\d+)", o)
        return {"amendment_id": int(m.group(1))} if m else {}


def refuse(name, rc_o, code=None, before=None):
    rc, o = rc_o
    ok = rc != 0 and (code is None or code in o)
    if before is not None:
        ok = ok and before()
    check(name, ok, f"rc={rc} {o}")


with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    jr, qu, jc, qc, state, task = mk(tmp)
    brief = tmp / "brief.md"; brief.write_text("lam", encoding="utf-8")
    brief2 = tmp / "brief2.md"; brief2.write_text("lam kieu khac", encoding="utf-8")
    out = tmp / "out.txt"; out2 = tmp / "out2.txt"
    approvals = tmp / "approvals.md"
    approvals.write_text(
        "- dong 1: Owner noi ok thoi thuong\n"
        "- dong 2: APPROVAL BY OWNER amendment 1 cho task A\n"
        "- dong 3: APPROVAL BY OWNER duyet viec khac\n"
        "- dong 4: approval by owner AMENDMENT 2 hop le\n"
        "- dong 5: amendment 3 nhung khong co cum mat\n",
        encoding="utf-8",
    )
    sig = f"file:{out}"

    rc, o = qc("add", "--id", "A", "--title", "A", "--workflow", "W", "--worker", "w1", "--brief", str(brief), "--signal", sig,
               "--fallback-worker", "w2", "--fallback-worker", "w3")
    check("add voi --fallback-worker (lap lai) OK", rc == 0, o)
    rc, o = qc("add", "--id", "N", "--title", "khong fallback", "--workflow", "W", "--worker", "w1", "--brief", str(brief), "--signal", sig)
    check("add khong fallback OK", rc == 0, o)
    rc, o = qc("add", "--id", "L", "--title", "legacy", "--workflow", "W")
    check("add task cu khong spec van OK", rc == 0, o)
    qc("sync")
    st = state("W")
    a = task(st, "A")
    check("state: spec_version==1, fallback_workers dung", a.get("spec_version") == 1 and a.get("fallback_workers") == ["w2", "w3"], str(a))
    check("state: allowed_actions A gom dispatch+switch_worker+add_signal+propose_amendment",
          {"dispatch", "switch_worker", "add_signal", "propose_amendment"} <= set(a.get("allowed_actions", [])), str(a.get("allowed_actions")))
    check("state: allowed_actions A KHONG co approve/reject",
          not any("approve" in x or "reject" in x for x in a.get("allowed_actions", [])), str(a.get("allowed_actions")))
    n = task(st, "N")
    check("task khong fallback: khong co switch_worker, van co add_signal", "switch_worker" not in n.get("allowed_actions", []) and "add_signal" in n.get("allowed_actions", []), str(n.get("allowed_actions")))
    Lt = task(st, "L")
    check("task cu khong spec: khong co switch/add_signal/propose", not ({"switch_worker", "add_signal", "propose_amendment"} & set(Lt.get("allowed_actions", []))), str(Lt))
    check("state co pending_amendments rong", state("W").get("pending_amendments") in ([], {}, None) or state("W").get("pending_amendments") == [], str(state("W").get("pending_amendments")))

    # ---- (1) fallback
    j0 = len(recs(jr))
    refuse("switch_worker ngoai danh sach -> WORKER_NOT_ALLOWED", qc("dispatch", "--id", "A", "--worker", "hacker", "--reason", "x", "--by", "c"), "WORKER_NOT_ALLOWED", lambda: len(recs(jr)) == j0)
    refuse("switch_worker thieu reason -> REASON_REQUIRED", qc("dispatch", "--id", "A", "--worker", "w2", "--by", "c"), "REASON_REQUIRED", lambda: len(recs(jr)) == j0)
    refuse("switch_worker khi khong co fallback -> NO_FALLBACK", qc("dispatch", "--id", "N", "--worker", "w2", "--reason", "x", "--by", "c"), "NO_FALLBACK", lambda: len(recs(jr)) == j0)
    rc, o = qc("dispatch", "--id", "A", "--worker", "w2", "--reason", "w1 het han muc", "--by", "c")
    d = jload(o)
    check("dispatch voi fallback w2 hop le", rc == 0 and d.get("worker") == "w2" and d.get("worker_switched_from") == "w1" and "het han muc" in str(d.get("worker_switch_reason")), o)
    check("spec khong doi sau khi doi worker (v1, worker goc w1)", task(state("W"), "A").get("worker") == "w1" and task(state("W"), "A").get("spec_version") == 1)
    jc("complete", "--task-id", "A", "--attempt-id", d.get("attempt_id", "A#1"), "--status", "failed", "--summary", "w2 hong")
    refuse("retry ma khong reason bi tu choi (luat Buoc 3 van giu)", qc("dispatch", "--id", "A", "--retry", "--worker", "w3", "--by", "c"), None)
    rc, o = qc("dispatch", "--id", "A", "--retry", "--worker", "w3", "--reason", "thu w3", "--by", "c")
    d2 = jload(o)
    check("retry + doi sang w3 trong fallback", rc == 0 and d2.get("worker") == "w3" and d2.get("retry_of") == d.get("attempt_id"), o)
    jc("complete", "--task-id", "A", "--attempt-id", d2.get("attempt_id", "A#2"), "--status", "failed", "--summary", "hong tiep")

    # ---- (2) add-signal chi them
    refuse("add-signal dinh dang sai bi tu choi", qc("add-signal", "--id", "A", "--signal", "bua bai", "--reason", "r", "--by", "c"), None)
    refuse("add-signal trung -> DUPLICATE_SIGNAL", qc("add-signal", "--id", "A", "--signal", sig, "--reason", "r", "--by", "c"), "DUPLICATE_SIGNAL")
    refuse("add-signal task khong spec -> NO_SPEC", qc("add-signal", "--id", "L", "--signal", sig, "--reason", "r", "--by", "c"), "NO_SPEC")
    refuse("add-signal thieu reason bi tu choi", qc("add-signal", "--id", "A", "--signal", f"file:{out2}", "--by", "c"), None)
    j1 = len(recs(jr))
    rc, o = qc("add-signal", "--id", "A", "--signal", f"file:{out2}", "--reason", "can them file kq", "--by", "c")
    check("add-signal hop le OK", rc == 0, o)
    a = task(state("W"), "A")
    check("sau add-signal: v2, tieu chi cu + moi (khong mat cai cu)", a.get("spec_version") == 2 and a.get("signals") == [sig, f"file:{out2}"], str(a))
    sc = [r for r in recs(jr)[j1:] if r.get("status") == "spec_changed"]
    check("journal co spec_changed (1->2, by, reason)", len(sc) == 1 and sc[0].get("from_version") == 1 and sc[0].get("to_version") == 2 and sc[0].get("by") == "c" and sc[0].get("reason"), str(sc))
    hist = a.get("spec_history") or []
    check("spec_history giu ca v1 (tieu chi cu) va v2", len(hist) >= 2 and any(h.get("version") == 1 for h in hist), str(hist))
    for sub in ("remove-signal", "del-signal", "set-signal", "replace-signal", "edit"):
        refuse(f"khong co lenh '{sub}' de bot/sua tieu chi", qc(sub, "--id", "A", "--signal", sig), None)
    rc, o = jc("verify")
    check("journal verify PASS sau spec_changed", rc == 0, o)
    # trang thai task khong bi bản ghi spec_changed lam hong: A van failed -> retry duoc, lan nay dung tieu chi v2
    check("A van 'failed' (spec_changed khong lam doi trang thai)", task(state("W"), "A").get("status") == "failed", str(task(state("W"), "A").get("status")))
    rc, o = qc("dispatch", "--id", "A", "--retry", "--reason", "thu lai voi tieu chi moi", "--by", "c")
    d3 = jload(o)
    check("luot moi dung tieu chi v2 (2 signal)", rc == 0 and d3.get("completion_signals") == [sig, f"file:{out2}"], o)
    check("luot cu van giu tieu chi v1 (1 signal) trong journal", any(r.get("attempt_id") == d.get("attempt_id") and r.get("completion_signals") == [sig] for r in recs(jr) if r.get("status") == "dispatched"))
    refuse("dang bay: add-signal bi tu choi", qc("add-signal", "--id", "A", "--signal", f"file:{tmp / 'z'}", "--reason", "r", "--by", "c"), None)
    refuse("dang bay: propose-amendment bi tu choi", qc("propose-amendment", "--id", "A", "--by", "c", "--reason", "r", "--evidence", "e", "--worker", "w9"), None)
    refuse("dang bay: doi worker bi tu choi", qc("dispatch", "--id", "A", "--worker", "w2", "--reason", "r", "--by", "c"), None)
    # hoan tat A de test accepted
    open(out, "w").write("x"); open(out2, "w").write("y")
    jc("complete", "--task-id", "A", "--attempt-id", d3["attempt_id"], "--status", "done", "--summary", "xong")
    refuse("done cho accept: add-signal bi tu choi", qc("add-signal", "--id", "A", "--signal", f"file:{tmp / 'z'}", "--reason", "r", "--by", "c"), None)
    rc, o = jc("accept", "--task-id", "A", "--by", "c")
    check("accept A theo tieu chi cua CHINH luot do (v2, 2 file)", rc == 0, o)
    refuse("accepted: add-signal bi tu choi", qc("add-signal", "--id", "A", "--signal", f"file:{tmp / 'z'}", "--reason", "r", "--by", "c"), None)
    refuse("accepted: propose-amendment bi tu choi", qc("propose-amendment", "--id", "A", "--by", "c", "--reason", "r", "--evidence", "e", "--worker", "w9"), None)

    # ---- (3) propose / approve / reject  (task N, chua giao)
    j2 = len(recs(jr)); q2 = Path(qu).read_text(encoding="utf-8")
    refuse("propose thieu evidence bi tu choi", qc("propose-amendment", "--id", "N", "--by", "c", "--reason", "r", "--worker", "w9"), None)
    refuse("propose khong co gi de doi bi tu choi", qc("propose-amendment", "--id", "N", "--by", "c", "--reason", "r", "--evidence", "e"), None)
    refuse("propose brief khong ton tai bi tu choi", qc("propose-amendment", "--id", "N", "--by", "c", "--reason", "r", "--evidence", "e", "--brief", str(tmp / "ko.md")), None)
    refuse("propose tieu chi dinh dang sai bi tu choi", qc("propose-amendment", "--id", "N", "--by", "c", "--reason", "r", "--evidence", "e", "--signal", "bua"), None)
    refuse("propose tieu chi toan van ban (khong may kiem) -> NO_MACHINE_SIGNAL", qc("propose-amendment", "--id", "N", "--by", "c", "--reason", "r", "--evidence", "e", "--signal", "note:cam thay on"), None)
    check("cac propose xau khong ghi gi vao journal/queue", len(recs(jr)) == j2 and Path(qu).read_text(encoding="utf-8") == q2)
    rc, o = qc("propose-amendment", "--id", "N", "--by", "coordX", "--reason", "brief sai", "--evidence", "log cho thay brief nham", "--brief", str(brief2), "--signal", f"file:{out2}")
    p1 = jload(o)
    aid1 = p1.get("amendment_id")
    check("propose hop le tra amendment_id", rc == 0 and aid1 is not None, o)
    n = task(state("W"), "N")
    check("propose KHONG doi spec (van v1, brief goc, tieu chi goc)", n.get("spec_version") == 1 and n.get("brief") == str(brief) and n.get("signals") == [sig], str(n))
    st = state("W")
    pa = st.get("pending_amendments") or []
    check("state hien pending_amendments co de xuat", any(x.get("task_id") == "N" and x.get("amendment_id") == aid1 for x in (pa if isinstance(pa, list) else sum(pa.values(), []))), str(pa))
    check("N van dispatch duoc theo spec CU khi chua duyet (allowed 'dispatch')", "dispatch" in n.get("allowed_actions", []), str(n.get("allowed_actions")))
    refuse("approve thieu approval-ref bi tu choi", qc("approve-amendment", "--amendment-id", str(aid1)), None)
    refuse("approve voi dong khong co cum APPROVAL BY OWNER -> APPROVAL_REF_INVALID", qc("approve-amendment", "--amendment-id", str(aid1), "--approvals-log", str(approvals), "--approval-ref", f"{approvals}:1"), "APPROVAL_REF_INVALID")
    refuse("approve dong co cum nhung khong co 'amendment N' -> APPROVAL_REF_INVALID", qc("approve-amendment", "--amendment-id", str(aid1), "--approvals-log", str(approvals), "--approval-ref", f"{approvals}:3"), "APPROVAL_REF_INVALID")
    refuse("approve dong amendment N nhung khong co cum mat -> APPROVAL_REF_INVALID", qc("approve-amendment", "--amendment-id", str(aid1), "--approvals-log", str(approvals), "--approval-ref", f"{approvals}:5"), "APPROVAL_REF_INVALID")
    refuse("approve dong cua amendment KHAC (2) cho amendment 1 -> APPROVAL_REF_INVALID", qc("approve-amendment", "--amendment-id", str(aid1), "--approvals-log", str(approvals), "--approval-ref", f"{approvals}:4"), "APPROVAL_REF_INVALID")
    refuse("approve file/dong khong ton tai -> APPROVAL_REF_INVALID", qc("approve-amendment", "--amendment-id", str(aid1), "--approvals-log", str(approvals), "--approval-ref", f"{approvals}:99"), "APPROVAL_REF_INVALID")
    refuse("approve amendment khong ton tai", qc("approve-amendment", "--amendment-id", "9999", "--approvals-log", str(approvals), "--approval-ref", f"{approvals}:2"), None)
    check("moi lan approve sai deu khong doi spec", task(state("W"), "N").get("spec_version") == 1)
    rc, o = qc("approve-amendment", "--amendment-id", str(aid1), "--approvals-log", str(approvals), "--approval-ref", f"{approvals}:2")
    check("approve dung dong duyet OK", rc == 0, o)
    n = task(state("W"), "N")
    check("sau approve: spec v2 voi brief+tieu chi moi", n.get("spec_version") == 2 and n.get("brief") == str(brief2) and n.get("signals") == [f"file:{out2}"], str(n))
    qj = json.loads(Path(qu).read_text(encoding="utf-8"))
    qt = next(t for t in (qj["tasks"] if isinstance(qj, dict) else qj) if t["id"] == "N")
    check("spec_history (queue.json day du) giu v1 goc + v2 moi", any(h.get("version") == 1 and h.get("brief") == str(brief) for h in qt.get("spec_history", [])) and any(h.get("version") == 2 and h.get("brief") == str(brief2) for h in qt.get("spec_history", [])), str(qt.get("spec_history")))
    kinds = [r.get("status") for r in recs(jr)]
    check("journal co amendment_proposed + amendment_approved + spec_changed", all(k in kinds for k in ("amendment_proposed", "amendment_approved", "spec_changed")), str(kinds))
    refuse("approve lai -> ALREADY_APPLIED", qc("approve-amendment", "--amendment-id", str(aid1), "--approvals-log", str(approvals), "--approval-ref", f"{approvals}:2"), "ALREADY_APPLIED")
    check("pending_amendments het de xuat da ap", not [x for x in (lambda pa: pa if isinstance(pa, list) else sum(pa.values(), []))(state("W").get("pending_amendments") or []) if x.get("amendment_id") == aid1])
    rc, o = jc("verify")
    check("journal verify PASS sau amendment", rc == 0, o)
    rc, o = qc("dispatch", "--id", "N", "--by", "c")
    dn = jload(o)
    check("dispatch N sau amendment dung spec v2", rc == 0 and dn.get("completion_signals") == [f"file:{out2}"], o)

    # STALE: de xuat tren v bat ky, sau do add-signal lam spec doi
    qc("add", "--id", "S", "--title", "stale", "--workflow", "W2", "--worker", "w1", "--brief", str(brief), "--signal", sig)
    qc("sync")
    rc, o = qc("propose-amendment", "--id", "S", "--by", "c", "--reason", "r", "--evidence", "e", "--worker", "w9")
    aid_s = jload(o).get("amendment_id")
    qc("add-signal", "--id", "S", "--signal", f"file:{out2}", "--reason", "r", "--by", "c")
    with open(approvals, "a", encoding="utf-8") as f:
        f.write(f"- dong 6: APPROVAL BY OWNER amendment {aid_s} dung id\n")
    refuse("approve de xuat cu sau khi spec da doi -> STALE_AMENDMENT", qc("approve-amendment", "--amendment-id", str(aid_s), "--approvals-log", str(approvals), "--approval-ref", f"{approvals}:6"), "STALE_AMENDMENT")
    # reject
    rc, o = qc("propose-amendment", "--id", "S", "--by", "c", "--reason", "r2", "--evidence", "e", "--worker", "w9")
    aid_r = jload(o).get("amendment_id")
    check("amendment_id tang dan, khong trung", aid_r is not None and aid_r != aid_s and aid_s != aid1, f"{aid1} {aid_s} {aid_r}")
    rc, o = qc("reject-amendment", "--amendment-id", str(aid_r), "--reason", "khong dong y")
    check("reject OK", rc == 0, o)
    check("reject khong doi spec", task(state("W2"), "S").get("worker") == "w1")
    refuse("approve de xuat da bi reject -> ALREADY_REJECTED", qc("approve-amendment", "--amendment-id", str(aid_r), "--approvals-log", str(approvals), "--approval-ref", f"{approvals}:4"), "ALREADY_REJECTED")
    # J3 luat cung: tieu chi toan chu (neu he thong cho phep dinh dang khong-may) khong duoc xoa het tieu chi may
    refuse("J3: de xuat bo het tieu chi may -> NO_MACHINE_SIGNAL", qc("propose-amendment", "--id", "S", "--by", "c", "--reason", "r", "--evidence", "e", "--signal", "note:x"), None)

    # ---- tuong thich: T1/T2 kieu cu & journal chain
    rc, o = jc("verify")
    check("journal verify PASS cuoi cung", rc == 0, o)

# ---- tranh chap: nhieu add-signal / propose dong thoi
with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    jr, qu, jc, qc, state, task = mk(tmp)
    brief = tmp / "b.md"; brief.write_text("x", encoding="utf-8")
    qc("add", "--id", "R", "--title", "race", "--workflow", "RW", "--worker", "w1", "--brief", str(brief), "--signal", f"file:{tmp / 'base'}")
    qc("sync")
    ok_all = True
    for rnd in range(5):
        base = task(state("RW"), "R").get("spec_version")
        with ThreadPoolExecutor(8) as ex:
            res = list(ex.map(lambda i: qc("add-signal", "--id", "R", "--signal", f"file:{tmp / f'f{rnd}_{i}'}", "--reason", "race", "--by", f"c{i}"), range(8)))
        succ = sum(1 for rc, _ in res if rc == 0)
        now = task(state("RW"), "R")
        # khong thuoc tinh nao ep chi 1 thanh cong (khac tieu chi => deu hop le), nhung version phai = base + succ, signals khong mat
        if now.get("spec_version") != base + succ or len(now.get("signals", [])) != 1 + (rnd * 8) + succ:
            ok_all = False
            print("   race lech:", rnd, base, succ, now.get("spec_version"), len(now.get("signals", [])))
            break
    check("race add-signal x8 x5 vong: version = so lan thanh cong, khong mat tieu chi", ok_all)
    versions = [h.get("version") for h in (task(state("RW"), "R").get("spec_history") or [])]
    check("spec_history version tang lien tuc, khong trung", versions == sorted(set(versions)) and len(versions) >= 1, str(versions))
    rc, o = jc("verify")
    check("journal verify PASS sau race", rc == 0, o)
    ok_p = True
    for rnd in range(3):
        with ThreadPoolExecutor(8) as ex:
            res = list(ex.map(lambda i: qc("propose-amendment", "--id", "R", "--by", f"c{i}", "--reason", "race", "--evidence", "e", "--worker", f"wx{rnd}_{i}"), range(8)))
        ids = [jload(o).get("amendment_id") for rc, o in res if rc == 0]
        if len(ids) != 8 or len(set(ids)) != 8:
            ok_p = False
            print("   amendment_id lech:", ids)
            break
    check("race propose x8 x3 vong: amendment_id khong trung", ok_p)
    rc, o = jc("verify")
    check("journal verify PASS sau race propose", rc == 0, o)

total, passed = len(results), sum(1 for _, ok in results if ok)
print(f"\nTONG: {passed}/{total} PASS")
sys.exit(0 if passed == total else 1)
