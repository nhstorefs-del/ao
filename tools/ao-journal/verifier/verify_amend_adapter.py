"""Kiểm độc lập (AO/Claude viết) cho phần Adapter của AMEND — dùng FAKE coordinator.

Chạy: python tools/ao-journal/verifier/verify_amend_adapter.py
Tái dùng lớp Env của verify_adapter.py (cắt phần trước kiểm tĩnh). Chỉ thư mục tạm.
"""
import json
import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
_src = (HERE / "verify_adapter.py").read_text(encoding="utf-8")
_src = _src.split("if not AD.exists():")[0]
exec(compile(_src, "verify_adapter_head", "exec"))  # định nghĩa run, check, recs, Env, AD, results...

src_ad = AD.read_text(encoding="utf-8")
check("STATIC: khong co action approve/reject trong ma Adapter", not re.search(r"['\"](approve_amendment|reject_amendment)['\"]", src_ad))
check("STATIC: Adapter khong goi lenh approve-amendment/reject-amendment cua Core", "approve-amendment" not in src_ad and "reject-amendment" not in src_ad)
check("STATIC: khong shell=True", "shell=True" not in src_ad)
check("STATIC: Adapter <= 500 dong", len(src_ad.splitlines()) <= 500, f"{len(src_ad.splitlines())} dong")


def fb_add(e, tid, worker, fallbacks, wf="W", sig_extra=()):
    brief = e.tmp / f"brief_{tid}.md"
    out = e.tmp / f"out_{tid}.txt"
    brief.write_text(f"viec {tid}\nOUT={out}\n", encoding="utf-8")
    args = ["add", "--id", tid, "--title", tid, "--workflow", wf, "--worker", worker, "--brief", str(brief), "--signal", f"file:{out}"]
    for f in fallbacks:
        args += ["--fallback-worker", f]
    return e.qc(*args), out


with tempfile.TemporaryDirectory() as tmp:
    e = Env(tmp)
    (rc, o), outA = fb_add(e, "A", "w-fail", ["w-ok", "w-noout"])
    e.qc("sync")

    # ---- dispatch bằng worker gốc (w-fail) -> failed
    rc, res, o = e.turn({"action": "dispatch", "task_id": "A"})
    check("dispatch worker goc (w-fail) -> WORKER_FAILED (exit 3), task failed", str(res.get("outcome")).startswith("WORKER_FAILED") and e.task("A").get("status") == "failed", o)

    # ---- switch_worker: bị Core từ chối khi ngoài danh sách (Core là authority); state không đổi
    n = e.n()
    rc, res, o = e.turn({"action": "switch_worker", "task_id": "A", "fallback_worker": "w-evil", "reason": "thu"})
    check("switch_worker ngoai danh sach -> CORE_REFUSED:WORKER_NOT_ALLOWED", str(res.get("outcome")).startswith("CORE_REFUSED") and "WORKER_NOT_ALLOWED" in str(res.get("outcome")), o)
    check("... state khong doi (khong co ban ghi moi)", e.n() == n)
    # thiếu reason
    rc, res, o = e.turn({"action": "switch_worker", "task_id": "A", "fallback_worker": "w-ok"})
    check("switch_worker thieu reason -> bi tu choi, state khong doi", res.get("outcome") not in ("APPLIED", None) and e.n() == n, o)
    # khoá lạ / khoá cấm
    for bad in ({"worker": "w-ok"}, {"brief": "x.md"}, {"signals": ["file:x"]}, {"command": "rm -rf"}, {"new_signal": "file:x"}, {"proposed_worker": "w"}):
        rc, res, o = e.turn({"action": "switch_worker", "task_id": "A", "fallback_worker": "w-ok", "reason": "r", **bad})
        check(f"switch_worker + khoa '{list(bad)[0]}' -> ADAPTER_REJECTED:FORBIDDEN_FIELD", "FORBIDDEN_FIELD" in str(res.get("outcome")) and e.n() == n, o)
    # null của khoá cấm cũng bị từ chối
    rc, res, o = e.turn({"action": "switch_worker", "task_id": "A", "fallback_worker": "w-ok", "reason": "r", "worker": None})
    check("khoa cam gia tri null van bi FORBIDDEN_FIELD", "FORBIDDEN_FIELD" in str(res.get("outcome")) and e.n() == n, o)
    # đúng: chuyển sang w-ok (retry do Core suy ra)
    rc, res, o = e.turn({"action": "switch_worker", "task_id": "A", "fallback_worker": "w-ok", "reason": "w-fail hong, thu w-ok"})
    check("switch_worker hop le tren task failed -> APPLIED", res.get("outcome") == "APPLIED", o)
    dis = [r for r in recs(e.jr) if r.get("status") == "dispatched" and r.get("task_id") == "A"]
    check("receipt ghi worker=w-ok + worker_switched_from=w-fail + reason", dis and dis[-1].get("worker") == "w-ok" and dis[-1].get("worker_switched_from") == "w-fail" and dis[-1].get("worker_switch_reason"), str(dis[-1:] ))
    check("worker w-ok da chay that (file ket qua ton tai) -> task done", outA.exists() and e.task("A").get("status") == "done", str(e.task("A").get("status")))
    check("spec van ghi worker goc w-fail, v1", e.task("A").get("worker") == "w-fail" and e.task("A").get("spec_version") == 1)

# ---- add_signal
with tempfile.TemporaryDirectory() as tmp:
    e = Env(tmp)
    (rc, o), outB = fb_add(e, "B", "w-ok", [])
    e.qc("sync")
    extra = e.tmp / "extra.txt"
    n = e.n()
    # khoá thiếu / sai
    rc, res, o = e.turn({"action": "add_signal", "task_id": "B", "reason": "r"})
    check("add_signal thieu new_signal bi tu choi, state khong doi", res.get("outcome") not in ("APPLIED", None) and e.n() == n, o)
    for bad in ({"signals": ["file:x"]}, {"signal": "file:x"}, {"brief": "b"}, {"fallback_worker": "w"}, {"proposed_signals": ["file:x"]}):
        rc, res, o = e.turn({"action": "add_signal", "task_id": "B", "new_signal": f"file:{extra}", "reason": "r", **bad})
        check(f"add_signal + khoa '{list(bad)[0]}' -> FORBIDDEN_FIELD", "FORBIDDEN_FIELD" in str(res.get("outcome")) and e.n() == n, o)
    rc, res, o = e.turn({"action": "add_signal", "task_id": "B", "new_signal": f"file:{outB}", "reason": "trung"})
    check("add_signal trung tieu chi -> CORE_REFUSED:DUPLICATE_SIGNAL", "DUPLICATE_SIGNAL" in str(res.get("outcome")), o)
    rc, res, o = e.turn({"action": "add_signal", "task_id": "B", "new_signal": f"file:{extra}", "reason": "can them file phu"})
    check("add_signal hop le -> APPLIED, spec v2 co 2 tieu chi", res.get("outcome") == "APPLIED" and e.task("B").get("spec_version") == 2 and len(e.task("B").get("signals", [])) == 2, o)
    # worker w-ok chỉ ghi file OUT => tiêu chí mới (extra.txt) không có => accept phải bị gate chặn
    rc, res, o = e.turn({"action": "dispatch", "task_id": "B"})
    check("dispatch sau add_signal APPLIED", res.get("outcome") == "APPLIED", o)
    st = e.task("B").get("status")
    # sau dispatch: worker ghi OUT, hoàn tất done (adapter chỉ ghi exit). Gate accept phải chặn vì thiếu extra.txt
    rc, res, o = e.turn({"action": "accept", "task_id": "B"})
    check("accept bi Core chan vi thieu tieu chi them (SIGNAL_FAILED), khong accepted", str(res.get("outcome")).startswith("CORE_REFUSED") and not e.task("B").get("accepted"), o)
    extra.write_text("ok", encoding="utf-8")
    rc, res, o = e.turn({"action": "accept", "task_id": "B"})
    check("tao file tieu chi them -> accept qua", res.get("outcome") == "APPLIED" and e.task("B").get("accepted"), o)
    rc, res, o = e.turn({"action": "add_signal", "task_id": "B", "new_signal": f"file:{e.tmp / 'q'}", "reason": "r"})
    check("add_signal tren task accepted -> bi tu choi (khong thuoc allowed)", "ADAPTER_REJECTED" in str(res.get("outcome")), o)

# ---- propose_amendment
with tempfile.TemporaryDirectory() as tmp:
    e = Env(tmp)
    (rc, o), outC = fb_add(e, "C", "w-ok", [])
    e.qc("sync")
    n = e.n()
    nb = e.tmp / "better_brief.md"; nb.write_text("brief tot hon\n", encoding="utf-8")
    base = {"action": "propose_amendment", "task_id": "C", "reason": "brief sai", "evidence": "log cho thay nham"}
    rc, res, o = e.turn({**base, "proposed_brief": str(nb), "proposed_signals": [f"file:{outC}"], "proposed_worker": "w-ok", "proposed_fallback_workers": ["w-noout"]})
    check("propose_amendment hop le -> APPLIED", res.get("outcome") == "APPLIED", o)
    pe = [r for r in recs(e.jr) if r.get("status") == "amendment_proposed"]
    check("journal co amendment_proposed (proposed_by = coordinator)", len(pe) == 1 and pe[0].get("task_id") == "C" and pe[0].get("proposed_by") == "fake", str(pe))
    t = e.task("C")
    check("propose KHONG doi spec (v1, brief goc)", t.get("spec_version") == 1 and t.get("brief") != str(nb), str(t))
    check("state.pending_amendments co de xuat", bool(e.state().get("pending_amendments")))
    n2 = e.n()
    for act in ("approve_amendment", "approve", "reject_amendment", "apply_amendment", "set_spec"):
        rc, res, o = e.turn({"action": act, "task_id": "C", "amendment_id": 1})
        check(f"action '{act}' khong ton tai -> ADAPTER_REJECTED, state khong doi", "ADAPTER_REJECTED" in str(res.get("outcome")) and e.n() == n2, o)
    for bad in ({"brief": "x"}, {"worker": "w"}, {"signals": ["file:x"]}, {"new_signal": "file:x"}, {"fallback_worker": "w"}, {"approval_ref": "x:1"}):
        rc, res, o = e.turn({**base, "proposed_worker": "w-ok", **bad})
        check(f"propose_amendment + khoa '{list(bad)[0]}' -> FORBIDDEN_FIELD", "FORBIDDEN_FIELD" in str(res.get("outcome")) and e.n() == n2, o)
    rc, res, o = e.turn({"action": "propose_amendment", "task_id": "C", "reason": "r", "evidence": "e"})
    check("propose_amendment khong de xuat gi -> bi tu choi", res.get("outcome") not in ("APPLIED", None) and e.n() == n2, o)
    rc, res, o = e.turn({**base, "proposed_signals": ["note:chi la chu"]})
    check("propose tieu chi khong kiem duoc bang may -> bi tu choi (Core)", res.get("outcome") not in ("APPLIED", None) and e.n() == n2, o)
    rc, res, o = e.turn({"action": "propose_amendment", "task_id": "C", "reason": "r", "evidence": "e", "proposed_worker": None, "proposed_brief": str(nb)})
    check("null cua khoa tuy chon duoc coi la vang mat", res.get("outcome") == "APPLIED", o)
    # task đã accepted: không propose được
    rc, res, o = e.turn({"action": "dispatch", "task_id": "C"}); rc, res, o = e.turn({"action": "accept", "task_id": "C"})
    n3 = e.n()
    rc, res, o = e.turn({**base, "proposed_worker": "w-ok"})
    check("task da accepted: propose_amendment bi tu choi, state khong doi", res.get("outcome") not in ("APPLIED", None) and e.n() == n3, o)
    rc, o2 = e.jc("verify")
    check("journal verify PASS sau toan bo kich ban", rc == 0, o2)

# ---- prompt phải nói rõ 3 điều luật
with tempfile.TemporaryDirectory() as tmp:
    e = Env(tmp)
    (rc, o), _ = fb_add(e, "P", "w-ok", ["w-noout"]); e.qc("sync")
    e.turn({"action": "checkpoint", "task_id": None, "next_action": "x"})
    p = e.prompt_out.read_text(encoding="utf-8") if e.prompt_out.exists() else ""
    for kw in ("switch_worker", "add_signal", "propose_amendment", "fallback_workers"):
        check(f"prompt gui coordinator nhac '{kw}'", kw in p)
    check("prompt khong cho coordinator quyen duyet (khong co 'approve_amendment' nhu action hop le)", "approve_amendment" not in p or "khong" in p.lower() or "not" in p.lower())

total, passed = len(results), sum(1 for _, ok in results if ok)
print(f"\nTONG: {passed}/{total} PASS")
sys.exit(0 if passed == total else 1)
