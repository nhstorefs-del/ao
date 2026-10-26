# Bản vá chờ duyệt

Sau khi Owner duyệt riêng việc gắn móc, Claude kiểm lại file đích và chạy `git apply --check tools/ao-intake/integrity/patches/ao_status_integrity.patch` cùng lệnh tương tự cho `watchdog_integrity.patch`, rồi mới áp dụng. `ao_status` gọi `check --brief`, thêm cảnh báo vào AO STATUS; lỗi gọi bộ kiểm được báo ngắn và không làm sập AO STATUS. `watchdog` gọi `check` mỗi lượt và ghi cảnh báo vào `tools/ao-intake/integrity/watchdog.log`; bản vá không thêm đường gửi Telegram. Hai bản vá chưa được áp dụng thật.

Trước khi áp dụng, sao lưu hai file đích với hậu tố `.bak_1013d2b`. Hoàn tác bằng cách khôi phục đúng hai bản sao đó sau khi đối chiếu thay đổi mới phát sinh. Bài thử `test_integrity.py` chạy `git apply --check` trên bản sao tạm của hai file.
