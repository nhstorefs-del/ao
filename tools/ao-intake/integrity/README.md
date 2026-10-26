# Kiểm hash cấu hình AO (mã chờ triển khai)

`integrity_check.py check` so SHA-256 của từng tệp với bản ghi mới nhất trong `baseline.jsonl`. Mỗi bản ghi nối với bản trước bằng `prev_hash` và `this_hash`; đọc sổ luôn kiểm toàn bộ chuỗi. Mã thoát: `0` khớp, `9` lệch, `8` chưa có đường chuẩn hoặc sổ hỏng. `check --brief` in tối đa 3 dòng để đưa vào AO STATUS. Khi lệch, nhật ký `drift.jsonl` ghi một lần cho cùng một tập lệch trong 10 phút. Không ghi nhật ký nếu chưa có đường chuẩn hoặc sổ hỏng.

Mặc định bảo vệ `.claude/settings.json`, `.claude/settings.local.json` nếu có, toàn bộ `.claude/hooks/` kể cả thư mục con, `tools/ao-report/coverage/hook_*.py`, `coverage_lib.py`, `session_start_status.py`, `tools/ao-intake/approval_hook.py`, `approval_ledger.py`, `approval_gate.py`, `tools/ao-intake/integrity/*.py` và chính `protected.json`. Tệp mới hoặc bị xóa trong các mẫu này đều được báo. Bỏ qua `__pycache__` và tên chứa `.bak`. `protected.json` chứa `{"patterns": ["đường/dẫn/glob", ...]}`; mọi đường dẫn phải tương đối từ gốc repo. Việc sửa tập bảo vệ cũng cần được đưa vào đường chuẩn mới có duyệt.

**Chưa tạo đường chuẩn thật trong DOT2B.** Sau khi Owner duyệt riêng bước tạo đường chuẩn đầu tiên, chỉ Claude chạy `python tools\ao-intake\integrity\integrity_check.py baseline --bootstrap --note "tham chiếu lời duyệt Owner" --reason "đường chuẩn đầu tiên"`. `--bootstrap` chỉ chạy được khi sổ trống; ghi `approved_by=bootstrap-owner`. Lệnh này ghi chú căn cứ, nhưng máy không tự chứng thực tin nhắn Owner cho ngoại lệ bootstrap; Claude phải đối chiếu tin nhắn thật trước khi chạy.

Mọi cập nhật sau đó dùng `baseline --reason "..." --approval-id APR-... --request-id REQ-...`. Thẻ phải còn hạn, còn lượt, gắn đúng yêu cầu, có câu `APPROVAL BY OWNER` (hoặc lỗi chính tả `APROVAL BY OWNER`) và nêu đường chuẩn/kiểm hash. Lệnh gọi `approval_ledger.consume` với tên công cụ `integrity_baseline`, rồi mới nối bản ghi. Nếu mất điện đúng giữa hai lần ghi, có thể đã trừ lượt mà bản ghi mới chưa có; cần kiểm tra hai sổ trước khi thử lại. Không tự cấp thẻ từ chương trình này.

Ai sửa được `settings.json` cũng có thể sửa tệp đường chuẩn hoặc chính mã kiểm. Chuỗi băm và kiểm chéo trong watchdog giúp **phát hiện** nhiều trường hợp thay đổi; chúng không ngăn tuyệt đối người có quyền ghi toàn bộ thư mục. Muốn chống người đó phải dùng nơi lưu/kiểm độc lập ngoài phạm vi quyền ghi của họ.

Hai bản vá trong `patches/` chỉ là tài liệu triển khai chờ Owner duyệt riêng. DOT2B không gắn vào SessionStart, AO STATUS hay watchdog.
