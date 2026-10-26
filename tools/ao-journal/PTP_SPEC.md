# PTP_SPEC — Mở rộng TỐI THIỂU `tools/ao-journal` để đạt Pull-the-Plug Test (bước 1–3)

Mục tiêu duy nhất: để một coordinator khác (không phải Claude) tiếp quản workflow A→B→C chỉ từ state AO (journal + queue + file bằng chứng), không đọc transcript/phiên Claude, và KHÔNG chạy lại task đã ACCEPTED.
Ràng buộc (Owner): KHÔNG redesign AO Core, KHÔNG framework mới, KHÔNG thay queue/journal — MỞ RỘNG `journal.py`, `queue.py`, `check_completion.py` hiện có. Chỉ thư viện chuẩn Python 3. Không đọc/ghi khoá. Không đụng `journal.jsonl`/`queue.json` THẬT khi test (dùng thư mục tạm, tham số `--journal`/`--queue` đã có). Không sửa CLAUDE.md. Giữ nguyên hash-chain (`seq`, `prev_hash`, `this_hash`) và lệnh/đầu ra cũ khi không xung đột (tương thích ngược càng nhiều càng tốt; chỗ BẮT BUỘC đổi hành vi được nêu dưới).

Trạng thái task (suy ra từ bản ghi MỚI NHẤT của task trong journal):
`dispatched` → (worker báo) `done` | `failed` → (coordinator chấp nhận qua cổng kiểm) `accepted`.
Ý nghĩa: `done` = worker TỰ BÁO xong (chưa đáng tin); `accepted` = đã qua cổng kiểm bằng máy, có bằng chứng, là DUY NHẤT trạng thái mở khoá task phụ thuộc.

## BƯỚC 1 — Chốt durable state + ACCEPTED
- `journal.py accept --task-id X --by <coordinator_id> [--note ...]`: chỉ cho phép khi bản ghi mới nhất của X là `done` (worker đã báo); ghi bản ghi `status:"accepted"` với `accepted_by`, `note`, `evidence`: danh sách `{signal, passed}` và `output_hashes` (sha256 từng file trong signal `file:`; `cmd:` thì ghi exit thực đo). Bước 1 CHƯA bắt buộc cổng kiểm đầy đủ (bước 2), nhưng `accept` phải gọi `check_completion` và từ chối nếu signal fail hoặc task chưa có signal.
- `accept` IDEMPOTENT: nếu X đã `accepted` → KHÔNG ghi thêm bản ghi, in `ALREADY_ACCEPTED task=X` (exit 0, journal không đổi). `accept` task chưa `done` (hoặc không tồn tại) → từ chối (exit ≠ 0), journal không đổi.
- `queue.py sync`: task phụ thuộc chỉ `waiting→ready` khi MỌI dependency có trạng thái `accepted` (KHÔNG còn mở bằng `done`). Thêm `accepted` vào VALID_STATUSES/đếm; ánh xạ trạng thái journal→queue gồm `accepted`. (Đây là chỗ đổi hành vi bắt buộc; ghi vào RATIONALE.md.)
- `queue.py state [--json]`: in/ghi bản chụp state NGUỒN-DUY-NHẤT-LÀ-JOURNAL+QUEUE, KHÔNG phải nguồn sự thật thứ hai (không lưu file riêng, chỉ in ra): mỗi task: id, title, depends_on, status, latest_receipt_seq, accepted (bool) + accepted_by + output_hashes, danh sách task `ready` có thể giao tiếp, danh sách `accepted`, và `journal_verify` (PASS/FAIL). Coordinator mới chỉ cần lệnh này + file bằng chứng.

## BƯỚC 2 — Đóng verification gate (`accepted` chỉ qua cổng)
- Cổng kiểm khi `accept`: (a) task có ≥ 1 completion signal; (b) TẤT CẢ signal PASS tại thời điểm accept; (c) `brief_hash` trong receipt `dispatched` khớp hash file brief HIỆN TẠI (brief bị sửa sau khi giao → từ chối, `BRIEF_CHANGED`); (d) mọi file `file:` còn tồn tại và sha256 khớp giá trị đã ghi ở bản ghi `verified` (nếu có) — nếu file đổi giữa verify và accept → từ chối (`OUTPUT_CHANGED`); (e) `journal verify` PASS (chuỗi hash nguyên vẹn) trước khi ghi accepted; (f) tuỳ chọn `--guard-report <file>`: nếu truyền, file phải chứa `GUARD_RESULT: PASS`, ngược lại từ chối.
- Thêm lệnh `journal.py verify-task --task-id X` (hoặc tái dùng `check_completion.py`): ghi bản ghi `status:"verified"` (không đổi trạng thái task, chỉ ghi kết quả + `output_hashes`); `accept` yêu cầu có `verified` mới nhất khớp trạng thái hiện tại (hoặc tự gọi verify-task rồi accept trong cùng lệnh).
- Mọi từ chối: exit code khác 0, thông báo mã lỗi rõ (`NO_SIGNALS`, `SIGNAL_FAILED`, `BRIEF_CHANGED`, `OUTPUT_CHANGED`, `CHAIN_BROKEN`, `GUARD_NOT_PASS`, `NOT_DONE`), journal KHÔNG thêm bản ghi `accepted`. Worker nói "done" mà signal fail → không bao giờ thành `accepted`, queue không mở task con.

## BƯỚC 3 — attempt_id + idempotency + checkpoint
- Mỗi receipt `dispatched` có `attempt_id` = `<task_id>#<n>` (n = 1,2,… theo số lần dispatch của task) và `idempotency_key` = sha256(`task_id` + `brief_hash`).
- `append` (dispatch): TỪ CHỐI nếu task đã `accepted` (`ALREADY_ACCEPTED`: không chạy lại), hoặc đang `dispatched` chưa có kết quả (`ATTEMPT_IN_FLIGHT`) — ngoại trừ `--retry --reason "..."` (cho phép attempt n+1 khi attempt trước `failed` hoặc `unknown`/quá hạn; ghi `retry_of`, `retry_reason`). Dispatch cùng `idempotency_key` khi attempt trước `failed` mà không có `--retry` → từ chối.
- `complete` (worker báo kết quả) BẮT BUỘC mang `--attempt-id`; chỉ chấp nhận nếu trùng attempt MỚI NHẤT còn `dispatched`; attempt cũ/không khớp → từ chối `STALE_RESULT`, journal không đổi. `accept` cũng chỉ tính attempt mới nhất.
- Checkpoint: `journal.py checkpoint --task-id X|--workflow W --by <coordinator_id> --next-action "..." [--blocked-reason ...] [--approval-ref ...] --context-ref <file> (lặp lại)`: ghi bản ghi `status:"checkpoint"` (KHÔNG đổi trạng thái task). `context_refs` là các file local cần đọc để tiếp tục (brief, quy tắc, kết quả) — phải tồn tại. `queue.py state` hiển thị checkpoint mới nhất mỗi task/workflow và `next_action`.
- `queue.py state` đánh dấu `stale_dispatch` cho attempt `dispatched` quá `--stale-hours` (mặc định 6) chưa có kết quả; KHÔNG tự đổi trạng thái (chỉ cờ cảnh báo, người/coordinator quyết định retry).

## Tài liệu & kiểm
- Bổ sung `RATIONALE.md` mục mới (đúng tinh thần Owner: nêu VÌ SAO chọn thứ tự này/thiết kế này, không chỉ làm gì).
- Mỗi bước có test tự động stdlib `unittest` trong `tools/ao-journal/tests/` chạy bằng thư mục tạm (không đụng file thật): `python -m unittest discover -s tools/ao-journal/tests`. Test phải gồm cả TRƯỜNG HỢP XẤU (liệt kê trong từng bước). Claude (AO) sẽ test độc lập bằng kịch bản riêng ở mức CLI — đừng chỉnh test cho khớp mã; mã phải đúng spec.
