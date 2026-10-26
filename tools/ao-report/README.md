# AO tự báo và tự kiểm lỗi

`ao_supervisor.py` ghi `JOBS.log` ngay trước khi worker chạy và sau khi thoát. Lỗi được phân loại bởi `error_classifier.py`; chỉ retry, rework hoặc chuyển worker khi còn lượt và `recovery_policy.json` cho phép. Mọi quyết định nằm trong `recovery.jsonl`. `DONE_NEEDS_REVIEW` vẫn cần kiểm độc lập trước nghiệm thu.

Việc cần Owner quyết được ghi vào `needs_human.md` và `outbox/human.jsonl` trước khi gửi Telegram. Làn này không chịu trần 6 tin/giờ, 20 tin/ngày; gửi lỗi sẽ thử lại đến khi thành công. Mỗi việc mới có `escalation_id` riêng. Tin thường được ghi ở `sent.jsonl`; `AO_AUTOREPORT=0` tắt tính năng báo tự động.

`watchdog.py` quét báo bù, gửi tin chờ nghiệm thu và digest. `digest.py` gửi 08:00 và 17:30 kể cả khi không có lỗi. **Không có digest đúng giờ là dấu hiệu cần kiểm tra.** `check_the_checker.py` chạy bằng lịch riêng, đọc nhịp tim và gọi trực tiếp `telegram_send.py`; `ao_status.py` cũng cảnh báo khi nhịp tim watchdog quá 45 phút. Khi cả máy tắt, không có chương trình nào gửi được; lúc bật lại digest nêu khoảng trống nhịp tim.

Lệnh xem: `python tools\ao-memory\ao_run.py report-pending`; `python tools\ao-report\ao_status.py`; `python tools\ao-report\metrics.py`. Lệnh ghi cách xử lý Owner đã duyệt: `python tools\ao-report\learn.py resolve --signature ID --action retry --owner-quote "Owner duyệt thử lại" --auto yes`. Lệnh này chỉ học nhóm hành động an toàn và không nới allowlist hay giới hạn 3 lần chạy. Người chạy lệnh phải xác minh câu duyệt đến từ Owner; CLI không thể tự xác thực người nói.

Attempt bắt đầu trước `2026-10-11T00:00+07:00` được coi là legacy, không gửi bù. `INSTALL_WATCHDOG.md` có hai lệnh tạo lịch riêng; lịch chưa được cài. Với Telegram CLI, nếu API đã nhận tin nhưng máy chết trước khi ghi `delivered`, lần chạy sau có thể gửi trùng; tin mang `escalation_id` để đối chiếu. Hiện chưa có giao dịch idempotent từ API.

## Chính sách Owner chốt ngày 11/10/2026

`MAX_TOTAL_RUNS = 3` trong `recovery_policy.py` là tổng số lần chạy worker của một việc, gồm lần đầu. Retry, fallback, làm lại QC và chạy lại vì thiếu kết quả đều dùng chung bộ đếm `state/run_counter.json`. Sổ tăng và ghi bền trước khi worker được tạo; cùng ID luôn dùng chung, ID khác có cùng nội dung brief và đường dẫn tín hiệu trong 24 giờ cũng dùng chung. Brief chỉ được giảm giới hạn bằng `--max-retries`. Hết 3/3 sẽ chuyển sang mục cần người.

`policy.lock` ghim băm mã chính sách và allowlist. Sửa tay chính sách mà không cập nhật khóa sẽ từ chối khởi động. Lệnh `counter_admin.py reset KEY --owner-quote "..."` và `counter_admin.py set-policy --owner-quote "..."` ghi sổ kiểm toán; người chạy phải đối chiếu câu trích với chat thật của Owner. `AO_AUTOREPORT=0` tắt cả báo cáo và bộ đếm mới, giữ luồng ao_run cũ.

Chi phí tự khắc phục chỉ được ước tính trong digest, không có trần chi phí cứng, trần 20 lượt/ngày hay trần một lần chuyển worker. Allowlist và phân loại dữ liệu vẫn bắt buộc; việc đăng, xóa, gửi ra ngoài hay tốn tiền không tự thử lại.

Mục mới từ `learn.py resolve` luôn ở `PENDING_CONFIRMATION`. Trong 7 ngày đầu chỉ `retry` hoặc `ignore_benign` được thử tối đa 3 lần áp dụng; `fallback` và `rework` đợi `learn.py confirm ID --owner-quote "..."`. Digest hiện nguyên văn câu trích; `learn.py reject ID --owner-quote "..."` vô hiệu mục ngay.

Bộ ca độc lập: `python tools\ao-report\run_independent.py` ghi `independent_results.json`. Adapter đo bằng các hàm sản xuất trong môi trường tạm và đánh dấu phép đo chưa hỗ trợ là FAIL. Chỉ xem là nghiệm thu khi toàn bộ ca và bất biến đều được đo và đạt.
