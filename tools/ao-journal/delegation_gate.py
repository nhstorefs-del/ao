"""Durable DELEGATE-FIRST gate. Trusted AO code/SQLite boundary, not a host sandbox."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

VERSION = 1
SELF_OPERATIONS = {"coordination": {"inspect_state"}, "independent_verification": {"hash_artifact"},
                   "trivial_unblocking": {"check_path"}, "recovery": {"inspect_state"}}
SUPERVISORS = {"self", "supervisor", "coordinator", "ao-run", "codex-fallback"}
TRANSITIONS = {None: {"RECEIVED"}, "RECEIVED": {"DELEGATION_DECISION_REQUIRED"},
 "DELEGATION_DECISION_REQUIRED": {"DELEGATED", "SELF_EXECUTION_AUTHORIZED"},
 "DELEGATED": {"EXECUTING"}, "SELF_EXECUTION_AUTHORIZED": {"EXECUTING"},
 "EXECUTING": {"EXECUTING", "FINISHED", "RECOVERY_REQUIRED"}, "RECOVERY_REQUIRED": {"DELEGATED"}, "FINISHED": set()}

class GateRefused(ValueError): pass

def canonical(value): return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
def digest(value): return hashlib.sha256(canonical(value).encode()).hexdigest()
def database(journal): return Path(str(Path(journal).resolve()) + ".delegation.sqlite3")
def binding(worker, brief):
    if not isinstance(worker,str) or not worker.strip() or worker.strip().casefold() in SUPERVISORS:
        raise GateRefused("DELEGATION_WORKER_REQUIRED")
    p=Path(brief).resolve(strict=True)
    return {"worker":worker, "brief":str(p), "brief_sha256":hashlib.sha256(p.read_bytes()).hexdigest()}

def connect(journal, create=False):
    path=database(journal)
    if not create and not path.is_file(): raise GateRefused("DELEGATION_STATE_MISSING")
    if create: path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(str(path), timeout=15, isolation_level=None)
    db.execute("PRAGMA synchronous=FULL")
    if create:
        db.execute("CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, attempt TEXT NOT NULL, payload TEXT NOT NULL, previous TEXT NOT NULL, hash TEXT NOT NULL)")
    db.execute("BEGIN IMMEDIATE")
    return db

def history(db,attempt):
    rows=db.execute("SELECT seq,attempt,payload,previous,hash FROM events ORDER BY seq").fetchall()
    previous="GENESIS"; selected=[]
    for seq,ident,text,prev,h in rows:
        try: item=json.loads(text)
        except Exception as e: raise GateRefused("DELEGATION_STATE_CORRUPT") from e
        if prev != previous or digest([seq,ident,item,prev]) != h: raise GateRefused("DELEGATION_STATE_TAMPERED")
        previous=h
        if ident==attempt: selected.append(item)
    state=None
    for row in selected:
        if row.get("state") not in TRANSITIONS.get(state,set()): raise GateRefused("DELEGATION_TRANSITION_INVALID")
        state=row["state"]
    return selected

def process_identity(pid):
    import sys
    memory=Path(__file__).resolve().parent.parent/'ao-memory'
    if str(memory) not in sys.path: sys.path.insert(0,str(memory))
    from common import process_start_time
    return process_start_time(pid)

def process_alive(pid,created):
    import sys
    memory=Path(__file__).resolve().parent.parent/'ao-memory'
    if str(memory) not in sys.path: sys.path.insert(0,str(memory))
    from common import pid_alive
    return created is not None and pid_alive(pid,created)

def owner_dead(pid,created):
    if created is None: return False
    if os.name=='nt':
        import ctypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.restype=ctypes.c_void_p
        handle=kernel.OpenProcess(0x1000,False,int(pid))
        if not handle: return ctypes.get_last_error()==87
        kernel.CloseHandle.argtypes=[ctypes.c_void_p]
        kernel.CloseHandle(handle)
        current=process_identity(pid)
        return current is not None and current!=created
    try: os.kill(pid,0)
    except ProcessLookupError: return True
    except PermissionError: return False
    current=process_identity(pid)
    return current is not None and current!=created

def write(db,attempt,state,**fields):
    if set(fields) & {'state','pid','version','ts','process_created'}: raise GateRefused('DELEGATION_RESERVED_FIELD')
    rows=history(db,attempt); old=rows[-1]["state"] if rows else None
    if state not in TRANSITIONS.get(old,set()): raise GateRefused(f"DELEGATION_TRANSITION_INVALID:{old}->{state}")
    row={"state":state,"ts":time.time(),"pid":os.getpid(),"process_created":process_identity(os.getpid()),"version":VERSION,**fields}
    last=db.execute("SELECT seq,hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
    seq=last[0]+1 if last else 1; prev=last[1] if last else "GENESIS"
    db.execute("INSERT INTO events VALUES (?,?,?,?,?)",(seq,attempt,canonical(row),prev,digest([seq,attempt,row,prev])))
    return row

def transaction(journal, fn,create=False):
    db=connect(journal,create)
    try:
        value=fn(db); db.execute("COMMIT"); return value
    except Exception:
        db.execute("ROLLBACK"); raise
    finally: db.close()

def prepare(journal,attempt,worker,brief,by="supervisor"):
    bound=binding(worker,brief)
    def action(db):
        rows=history(db,attempt)
        if rows:
            if rows[-1]["state"] != "DELEGATED" or rows[-1].get("binding")!=bound: raise GateRefused("DELEGATION_BINDING_OR_STATE_MISMATCH")
            return rows[-1]
        ledger=Path(journal)
        if ledger.is_file():
            for line in ledger.read_text(encoding='utf-8').splitlines():
                if line.strip() and json.loads(line).get('attempt_id')==attempt: raise GateRefused('DELEGATION_LEGACY_OR_LOST_STATE')
        write(db,attempt,"RECEIVED",binding=bound)
        write(db,attempt,"DELEGATION_DECISION_REQUIRED",binding=bound)
        return write(db,attempt,"DELEGATED",binding=bound,decision="delegate",by=by,reason="worker_suitable_default")
    return transaction(journal,action,True)

def require(journal,attempt,worker,brief,states=("DELEGATED",)):
    bound=binding(worker,brief)
    def action(db):
        rows=history(db,attempt)
        if not rows or rows[-1]["state"] not in states or rows[-1].get("binding") != bound: raise GateRefused("DELEGATION_BINDING_OR_STATE_MISMATCH")
        if rows[-1]["state"] == "EXECUTING" and (rows[-1].get("pid") != os.getpid() or rows[-1].get("role") != "worker" or rows[-1].get("process_created") is None or rows[-1].get("process_created") != process_identity(os.getpid())):
            raise GateRefused("DELEGATION_EXECUTION_OWNER_MISMATCH")
        return rows[-1]
    return transaction(journal,action)

def claim(journal,attempt,worker,brief):
    bound=binding(worker,brief)
    def action(db):
        rows=history(db,attempt)
        if not rows or rows[-1]["state"]!="DELEGATED" or rows[-1].get("binding")!=bound: raise GateRefused("DELEGATION_EXECUTION_DENIED")
        return write(db,attempt,"EXECUTING",binding=bound,role="worker",child_state="PENDING")
    return transaction(journal,action)

def attach_child(journal,attempt,worker,brief,child_pid):
    bound=binding(worker,brief)
    def action(db):
        rows=history(db,attempt)
        old=rows[-1] if rows else {}
        if old.get('state')!='EXECUTING' or old.get('binding')!=bound or old.get('pid')!=os.getpid() or old.get('process_created')!=process_identity(os.getpid()) or old.get('role')!='worker' or old.get('child_state')!='PENDING': raise GateRefused('DELEGATION_CHILD_BINDING_DENIED')
        created=process_identity(child_pid)
        return write(db,attempt,'EXECUTING',binding=bound,role='worker',child_state='BOUND' if created is not None else 'UNKNOWN',child_pid=child_pid,child_created=created)
    return transaction(journal,action)

def finish(journal,attempt,worker,brief):
    bound=binding(worker,brief)
    def action(db):
        rows=history(db,attempt)
        if not rows or rows[-1]["state"]!="EXECUTING" or rows[-1].get("binding")!=bound or rows[-1].get("pid")!=os.getpid() or rows[-1].get("process_created") is None or rows[-1].get("process_created")!=process_identity(os.getpid()) or rows[-1].get("role")!="worker": raise GateRefused("DELEGATION_FINISH_DENIED")
        return write(db,attempt,"FINISHED",binding=bound,role="worker")
    return transaction(journal,action)

def receipt_check(journal,attempt,worker,brief,states=("DELEGATED",)):
    row=require(journal,attempt,worker,brief,states)
    return _validate_receipt(journal,attempt,worker,row)

def child_receipt_check(journal,attempt,worker,brief):
    """Authorize only the durably bound live child; never transfer launcher ownership."""
    bound=binding(worker,brief)
    def action(db):
        rows=history(db,attempt); row=rows[-1] if rows else {}
        if row.get('state')!='EXECUTING' or row.get('binding')!=bound or row.get('role')!='worker' or row.get('child_state')!='BOUND':
            raise GateRefused('DELEGATION_CHILD_EXECUTION_DENIED')
        current=os.getpid(); created=process_identity(current)
        if row.get('child_pid')!=current or created is None or row.get('child_created')!=created or row.get('pid')==current:
            raise GateRefused('DELEGATION_CHILD_IDENTITY_MISMATCH')
        parent=os.getppid()
        if row.get('pid')!=parent or row.get('process_created') is None or process_identity(parent)!=row['process_created'] or not process_alive(parent,row['process_created']):
            raise GateRefused('DELEGATION_CHILD_PARENT_NOT_LIVE_BOUND_OWNER')
        return row
    row=transaction(journal,action)
    return _validate_receipt(journal,attempt,worker,row)

def _validate_receipt(journal,attempt,worker,row):
    import journal as ledger
    p=Path(journal).resolve()
    if not ledger.verify(p,quiet=True): raise GateRefused("DELEGATION_JOURNAL_CHAIN_BROKEN")
    records=ledger._read_records(p)
    found=next((r for r in reversed(records) if r.get("attempt_id")==attempt and r.get("status")=="dispatched"),None)
    if found:
        latest=next((r for r in reversed(records) if r.get('task_id')==found['task_id'] and r.get('status') in {'dispatched','done','failed','accepted'}),None)
        if not latest or latest.get('status')!='dispatched' or latest.get('attempt_id')!=attempt: raise GateRefused('DELEGATION_ATTEMPT_TERMINAL_OR_STALE')
    if not found or found.get("worker")!=worker or found.get("brief_hash")!=row["binding"]["brief_sha256"] or found.get("delegation_journal")!=str(p) or found.get("delegation_binding")!=row["binding"]:
        raise GateRefused("DELEGATION_RECEIPT_MISSING_OR_CHANGED")
    return row

def snapshot(journal,attempt): return transaction(journal,lambda db: history(db,attempt))

def recover(journal,attempt,worker,brief,reason):
    if not reason or not reason.strip(): raise GateRefused("RECOVERY_PROOF_REQUIRED")
    bound=binding(worker,brief)
    def action(db):
        rows=history(db,attempt)
        if not rows or rows[-1]["state"]!="EXECUTING" or rows[-1].get("binding")!=bound: raise GateRefused("RECOVERY_STATE_INVALID")
        # A reused PID with different creation identity is not the execution owner.
        owner=rows[-1]
        if not owner_dead(owner['pid'],owner.get('process_created')): raise GateRefused("RECOVERY_OWNER_NOT_PROVEN_DEAD")
        if owner.get('child_state')!='BOUND' or not owner_dead(owner.get('child_pid'),owner.get('child_created')): raise GateRefused('RECOVERY_CHILD_NOT_PROVEN_DEAD')
        write(db,attempt,"RECOVERY_REQUIRED",binding=bound,reason=reason)
        return write(db,attempt,"DELEGATED",binding=bound,reason=reason,decision="explicit_dead_owner_recovery")
    return transaction(journal,action)

def supervisor_action(journal,ident,reason,operation,target=None):
    # No supplied callback, Python expression, shell, argv or arbitrary command.
    if reason not in SELF_OPERATIONS or operation not in SELF_OPERATIONS[reason]: raise GateRefused("SELF_EXECUTION_OPERATION_DENIED")
    if operation in {"hash_artifact","check_path"}:
        if target is None: raise GateRefused("SELF_EXECUTION_TARGET_REQUIRED")
        path=Path(target).resolve()
        if path.name.casefold()==".env" or ".worker-keys" in {part.casefold() for part in path.parts}: raise GateRefused("SELF_EXECUTION_SECRET_PATH_DENIED")
        if operation=="hash_artifact" and (not path.is_file() or path.stat().st_size>16*1024*1024): raise GateRefused("SELF_EXECUTION_BOUND_EXCEEDED")
    bound={"reason":reason,"operation":operation,"target":str(path) if target is not None and operation in {"hash_artifact","check_path"} else None}
    def authorize(db):
        if history(db,ident): raise GateRefused("SELF_EXECUTION_ID_REUSED")
        write(db,ident,"RECEIVED",binding=bound)
        write(db,ident,"DELEGATION_DECISION_REQUIRED",binding=bound)
        write(db,ident,"SELF_EXECUTION_AUTHORIZED",binding=bound,reason=reason,operation=operation)
        return write(db,ident,"EXECUTING",binding=bound,role="supervisor")
    transaction(journal,authorize,True)  # Durable authorization before any handler.
    if operation=="inspect_state":
        result={"gate_version":VERSION,"event_count":len(snapshot(journal,ident))}
    elif operation=="check_path": result={"exists":path.exists(),"is_file":path.is_file()}
    else:
        with path.open('rb') as source:
            content=source.read(16*1024*1024+1)
        if len(content)>16*1024*1024: raise GateRefused('SELF_EXECUTION_BOUND_EXCEEDED')
        result={"sha256":hashlib.sha256(content).hexdigest()}
    def complete(db):
        rows=history(db,ident)
        if not rows or rows[-1].get('pid')!=os.getpid() or rows[-1].get('process_created')!=process_identity(os.getpid()) or rows[-1].get('role')!='supervisor': raise GateRefused('SELF_EXECUTION_OWNER_MISMATCH')
        write(db,ident,"FINISHED",binding=bound,role="supervisor",result=result)
    transaction(journal,complete)
    return result

def main():
    p=argparse.ArgumentParser(); p.add_argument("--journal",required=True)
    s=p.add_subparsers(dest="action",required=True)
    a=s.add_parser("inspect"); a.add_argument("--attempt",required=True)
    a=s.add_parser("supervisor-action")
    for k in ("id","reason","operation"): a.add_argument("--"+k,required=True)
    a.add_argument("--target")
    a=p.parse_args()
    try:
        result=snapshot(a.journal,a.attempt) if a.action=="inspect" else supervisor_action(a.journal,a.id,a.reason,a.operation,a.target)
        print(canonical(result)); return 0
    except (ValueError,OSError,sqlite3.Error) as e: print("ERROR:",e); return 2
if __name__=="__main__": raise SystemExit(main())
