# Cảnh báo chạy thẳng thợ

Mã này chưa được đăng ký vào `.claude/settings.json`. Mục đăng ký đề xuất nằm ở `patches/settings_hook_entry.json`; chỉ thêm mục `PreToolUse` sau khi Owner duyệt riêng.

Khi đã đăng ký, hook đọc lệnh Bash/PowerShell qua Claude Code. Mặc định `warn`: ghi `data/directrun.jsonl` và hiện cảnh báo, không từ chối. Chạy `python report.py` để xem thống kê 24 giờ trước khi đề nghị Owner duyệt chặn cứng.

Điều khiển bằng tệp UTF-8 `MODE` trong thư mục này:

- Không có tệp hoặc nội dung `warn`: chỉ cảnh báo.
- Nội dung chính xác `off`: tắt kiểm tra.
- Nội dung chính xác `block`: từ chối. Chỉ chuyển sau khi Owner duyệt riêng.
- Nội dung khác: cảnh báo.

Để hạ khẩn cấp, đổi `MODE` thành `warn`, hoặc tạo tệp `ALLOW_ONCE`. Khi `ALLOW_ONCE` tồn tại, chế độ `block` hạ về `warn`; xóa tệp cờ sau khi xử lý để trở lại `block`. Tên cờ không có nghĩa hook tự tiêu thụ nó. Có thể tắt bằng biến môi trường `AO_DIRECTRUN_HOOK=0`.

Hook chỉ thấy lệnh chạy qua công cụ Claude Code, không thấy tiến trình chạy bên ngoài. Thợ cùng tài khoản Windows vẫn có thể chạy ngoài hook. Đây là lớp nhắc và chặn ở giao diện, không phải rào bảo mật hệ điều hành. Lỗi hook mở đường cho lệnh chạy và ghi loại lỗi vào `logs/directrun_hook.log`, không ghi nội dung lệnh vào log lỗi.
