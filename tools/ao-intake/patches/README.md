# Bản vá mẫu DOT2A — chưa áp dụng

Các `.patch` chỉ là bản nháp để Claude rà soát và áp dụng sau khi Owner duyệt riêng việc sửa bốn script. `settings_hook_entry.json` là mục `hooks.UserPromptSubmit` để ghép vào `.claude/settings.json` sau khi Owner duyệt riêng việc đăng ký hook. Không thay toàn bộ tệp settings bằng JSON mẫu.

Kiểm tra trên bản sao tạm của bốn script bằng `git apply --check` và `git apply` trong thư mục tạm có cùng cấu trúc `tools/...`. Cần kiểm lại trên phiên bản script mới nhất trước khi áp dụng. Không áp dụng patch vào repo khi chưa có duyệt.

Sau khi áp dụng từng patch, chạy `--help` và kiểm tra chỉ đọc của script đó, rồi mô phỏng `AO_INTAKE_REQUEST=REQ-...` không có thẻ (phải thoát 7), có thẻ đúng (chỉ mô phỏng, không gửi thật). Đặc biệt không dùng `--i-have-owner-approval` làm bằng chứng thay thế thẻ. Nếu cần hoàn tác, dùng bản sao gốc đã tạo trước khi vá hoặc `git apply -R` trên đúng phiên bản vừa vá.

Hook đọc tin Owner từ stdin; `AO_APPROVAL_HOOK=0` tắt ghi. Sổ duyệt mặc định ở `tools/ao-intake/data/approvals.jsonl`; nhật ký tin Owner ở `tools/ao-intake/data/owner_messages.jsonl`. Chỉ dùng các tệp thử trong thư mục tạm khi kiểm tra.
