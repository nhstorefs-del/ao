# Báo cáo thời gian và token AO

Chạy từ gốc repo:

```powershell
python -B tools\ao-memory\cost_report.py
python -B tools\ao-memory\cost_report.py --since 2026-10-05
python -B tools\ao-memory\cost_report.py --json
python -B tools\ao-memory\cost_report.py --md tools\ao-memory\COST_REPORT.md
python -B -m unittest tools\ao-memory\tests\test_cost_report.py -v
```

`--since` lọc theo ngày bắt đầu job ở UTC+07, tính cả ngày đó. `--json` in dữ liệu máy đọc được. `--md` ghi bảng Markdown vào đường dẫn chỉ định. Lệnh chỉ đọc `jobs/`; riêng `--md` ghi file được chỉ định. Hãy chọn đường dẫn file đã được phép ghi. Dùng `-B` để Python không tạo `__pycache__`.

## Cách tính

- `job.json` và `status.json`: ID, worker, trạng thái, số lần thử.
- `events.jsonl`: cộng thời gian từ từng `START` đến sự kiện kết thúc tương ứng. Ngày là ngày của `START` đầu tiên. Retry được tính là nhiều lượt thử và có cờ riêng. Thời gian chờ giữa các lượt không cộng vào thời gian chạy.
- `worker.log`: lấy các lượt `START` trong `events.jsonl`, tìm header `=== ID#n worker ===` tương ứng trong log, rồi chỉ lấy số `tokens used` **cuối cùng** của mỗi đoạn lượt thử. Các số trước đó có thể do worker chép lại đầu ra lệnh; chân trang giống nhau in hai lần cũng chỉ tính một lần. Nếu log cũ không có header, một lượt chỉ lấy số cuối; nhiều lượt chỉ cộng khi số chân trang đúng bằng số `START`, nếu không thì lấy số cuối để tránh cộng nhầm. `input_tokens`/`output_tokens` cũng chỉ lấy giá trị cuối mỗi lượt khi có. Không suy token từ ký tự.
- Job đang chạy: chỉ đọc `job.json`, `status.json`; không mở `events.jsonl` hay `worker.log`. Thời gian và token của job này để trống cho tới khi kết thúc.
- Cờ `lỗi/bỏ` nếu trạng thái hoặc lịch sử sự kiện có lỗi/bỏ; `retry` nếu có lượt thử lại; `>10 phút` nếu tổng thời gian đo được lớn hơn 600 giây.

Các mẫu log đã khảo sát: `codex-main`, `codex-2` có `tokens used` tổng. Mẫu `deepseek-nvidia`, `deepseek-paid` dùng Qwen CLI nhưng không có số token tường minh trong log job; báo cáo ghi **không có số đo**. Việc worker nào có số đo cụ thể phụ thuộc chính log job đó, không dựa vào tên worker.

Đây là báo cáo **mức dùng token**, chưa phải tiền: không có đơn giá hay số tiền thanh toán trong các file job. Tổng theo worker/ngày chỉ cộng số token đã đo; cần nhìn thêm số job chưa đo. Thời gian của job kết thúc bằng `ORPHANED` hoặc `STOPPED` là khoảng thời gian theo dấu mốc hệ thống, có thể dài hơn thời gian worker thực sự xử lý.

Giới hạn: nếu một lượt `START` tự gọi thêm nhiều phiên Codex nhưng không có mốc lượt riêng trong `events.jsonl`, báo cáo chỉ giữ chân trang cuối theo quy tắc trên; đây có thể là mức thấp hơn tổng sử dụng thật của các phiên đó.
