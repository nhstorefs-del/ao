# DELEGATE-FIRST execution control — v1

This is enforcement inside the trusted AO control layer, not a prompt setting or a sandbox for arbitrary host tools. Owner gives outcome; supervisor retains independent acceptance responsibility.

Durable per-attempt state in `<journal>.delegation.sqlite3`: RECEIVED → DELEGATION_DECISION_REQUIRED → DELEGATED → EXECUTING → FINISHED. Supervisor-only fixed handlers take SELF_EXECUTION_AUTHORIZED instead of DELEGATED. BEGIN IMMEDIATE, synchronous FULL, transition validation and a chained SHA-256 event history persist decision before execution; hash chain detects accidental edits but is not authentication against a same-user attacker who rewrites code/state.

Default dispatch records delegation to the declared worker. Brief path/content SHA, worker, attempt and journal receipt are bound. Existing journal attempts without gate state cannot be auto-granted. Receipts must be latest nonterminal attempts. Direct APIs are checked as well as CLI: journal.append, ao_run.start/launch_supervisor/resume, ao_supervisor.run and final Popen, adapter.worker_result. Retry/fallback gets a fresh attempt/decision. Two concurrent claimants cannot both execute. Process owner and creation identity are checked. The main runner captures one immutable brief snapshot and uses it for command substitution and stdin.

Supervisor self-execution is not arbitrary commands with a reason label. `delegation_gate.py supervisor-action` has only fixed operations: coordination/inspect_state; independent_verification/hash_artifact (16 MiB maximum); trivial_unblocking/check_path; recovery/inspect_state. Unknown operation/reason refused. No argv, shell, callback, expression, or code-writing handler exists. Authorization is committed before handler; failed/crashed handlers do not become accepted tasks.

Deliberate compatibility restrictions:
- Arbitrary `cmd:` completion signals no longer execute in check_completion or the supervisor. They fail closed; file existence/hash signals remain supported. Tests requiring command execution must be delegated as worker work and produce a file artifact for independent verification.
- Legacy live Qwen coordinator backend (`--yolo`) is refused before key read/process start. Fake local adapter remains supported. Do not restore it without a bounded transport and gate qualification.
- Legacy jobs lacking gate state cannot launch/resume. Existing live jobs/state are not rewritten; already-running processes are not retroactively controlled.
- `ao_run resume --id ID --recover-delegation --recovery-reason TEXT` requires recorded execution owner AND child process proven dead. Live/unknown child, access denied/query failure, or unknown process identity fails closed. Popen→child-binding crash leaves PENDING/UNKNOWN and cannot be blindly retried. Investigate durable artifacts/effects first; create a separately authorized task if no safe resume exists.
- Recovery does not promise exactly-once external side effects. There is no global prevention of a same-user actor directly running shell, editing trusted Python/SQLite, using an unrelated launcher, or falsely registering itself as a worker.

Operational ledger must still record expected outcome/criteria/artifacts/verification. FINISHED or worker done is not accepted; journal verify/accept and supervisor independent verification remain required.

Test runner (synthetic local subprocesses/temp ledgers only):
`python tools/ao-memory/tests/run_delegate_first.py --suite gate --output <evidence.json>`
Repeat for `dispatcher` and `journal`. Runner eagerly loads the stdlib ThreadPoolExecutor to avoid existing AO `queue.py` module shadowing in test discovery. It does not enable providers, schedules, services or callbacks.

## Bound child authorization
The launcher remains the durable execution lease owner. `child_receipt_check` authorizes only its separately spawned, durably BOUND child, with exact child PID/creation, actual parent PID, live parent creation/exit status, unchanged worker/brief binding and latest nonterminal journal receipt. The child cannot use launcher `require`/`claim`/`finish` to acquire or finalize that lease. Unknown identities, inaccessible or dead parents, pending attachment, stale receipts and changed bindings fail closed.
A maintenance child may wait at most three seconds for the launcher to commit child attachment; it performs no process control or provider effects while waiting. A readiness refusal is not permission to retry an external action. This is a trusted AO code/state boundary, not a same-user operating-system sandbox.

## Hub idle observation
Real recovery tests falsified `native ThreadCount == 1` as a reusable idle criterion: the reviewed hub showed native counts 4, 5 and 3 across 90 seconds and closed on timeout. Native count is now advisory evidence only. Qualified health adds versioned `python-live-thread-count-1`: count other live Python threads, excluding the exact main thread and current health handler. Other Python threads must be zero, with strict integer typing; missing/unknown/negative/boolean counts fail closed. Every observation also binds exact loaded source/code fingerprints, PID/creation, sole loopback listener and unchanged durable file/hash/lock facts. It is a trusted Python point-in-time observation, not an OS sandbox or a mathematical drain guarantee. No provider calls are used to qualify this metric.
