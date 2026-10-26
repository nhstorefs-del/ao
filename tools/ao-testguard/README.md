# TESTGUARD

Python 3.13, chỉ thư viện chuẩn. Chạy từ gốc `${AO_ROOT}`.

```powershell
python tools\ao-testguard\testguard.py snapshot --root tools\ao-report --glob "**/test_*.py" --out tools\ao-testguard\report_snapshot.json
python tools\ao-testguard\testguard.py check --root tools\ao-report --snapshot tools\ao-testguard\report_snapshot.json --allow-new
```

`--glob` lặp được; mỗi mẫu phải khớp ít nhất một tệp khi snapshot.
Snapshot JSON UTF-8, ghi nguyên tử, chứa đường dẫn tương đối, SHA-256 byte gốc và dấu hiệu nền.
Check quét lại các mẫu đã lưu và mọi tệp cũ. Sửa/xóa tệp cũ luôn vi phạm.
Tệp mới chỉ được phép khi `--allow-new`, vẫn bị quét dấu hiệu làm yếu.
Dấu hiệu nền được trừ theo số lượng và nội dung AST/mẫu, không theo dòng; thêm bản sao vẫn bị bắt.
Python dùng AST; JS/TS che chú thích và chuỗi trước khi bắt mẫu.
Chi tiết vi phạm có `tệp:dòng`; thay đổi băm/xóa dùng dòng 1 đại diện toàn tệp.
Exit: 0 sạch/snapshot thành công; 3 vi phạm; 2 lỗi dùng, đọc, mã hóa, cú pháp hoặc snapshot.
Đầu ra thường tối đa 29 dòng; `--json` trả đầy đủ để máy đọc.

Gắn tiêu chí xong vào lệnh giao việc có sẵn (không sửa ao_run):
```text
--signal 'cmd:python "${AO_ROOT}\tools\ao-testguard\testguard.py" check --root "${AO_ROOT}\tools\ao-report" --snapshot "${AO_ROOT}\tools\ao-testguard\report_snapshot.json" --allow-new exit=0'
```
Đây là đoạn tham số PowerShell để nối vào lệnh giao việc; dấu nháy đơn giữ dấu nháy kép bên trong.
Điều phối viên tạo và giữ snapshot ngoài phạm vi worker được ghi, trước job.
Check là một tiêu chí bổ sung; vẫn phải chạy bộ kiểm thật và nghiệm thu độc lập.

Kiểm thử: `cd tools\ao-testguard; python -m unittest test_testguard`.
Nghiệm thu tích hợp: `python tools\ao-testguard\verify_acceptance.py` từ gốc repo.
Lệnh này chạy `main()` gốc của `run_independent.py`, chỉ đổi OUTPUT sang `ao-testguard/evidence`
để không ghi đè báo cáo ngoài phạm vi; tắt tạo bytecode khi import mã ngoài phạm vi.
Nó cũng gọi `sha256sum -c independent_tests.SHA256`, kiểm ao-report thật và bản sao tạm.

Giới hạn: không chứng minh bài kiểm tốt hay lỗi đã sửa; không phân tích luồng dữ liệu,
alias/import đổi tên, monkeypatch, runner/config, phụ thuộc hay mã ứng dụng làm giả kết quả.
JS/TS chỉ regex bảo thủ: bỏ qua nội dung template literal (kể cả biểu thức), regex literal chưa phân tích;
có thể bắt `.only`/`.skip` của API không phải test. So sánh hằng chỉ bắt vài mẫu rõ ràng.
Python chưa bắt alias, mọi biến thể assert/helper hoặc ngoại lệ có kiểu nuốt assert.
Không bảo vệ snapshot bị worker sửa hoặc glob chọn thiếu; cần bảo vệ quyền ghi và xem lại phạm vi.
Chỉ nhận tệp UTF-8; lỗi mã hóa/cú pháp trả 2, không báo sạch. Symlink ngoài root bị chặn.
