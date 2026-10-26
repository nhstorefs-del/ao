# ao_run — giao việc có giám sát

`python ao_run.py wait --id VD1 --timeout 3600 --poll 5` chờ worker xong, lỗi, dừng hoặc mất giám sát.
Chạy nền từ thư mục trung tính: `Start-Job -ScriptBlock { Set-Location '${AO_ROOT}\tools\ao-memory'; python ao_run.py wait --id VD1 }`.
Lệnh in một dòng JSON để harness đọc và đánh thức coordinator khi kết thúc.
Mã thoát: 0 hoàn tất cần duyệt, 10 lỗi, 11 dừng, 12 mất giám sát, 13 hết giờ, 2 id không tồn tại.

Chạy Python từ thư mục trung tính, ví dụ `${AO_ROOT}\tools\ao-memory` (không chạy từ `ao-journal`).

```powershell
python ao_run.py start --lane A --id VD1 --title "Việc mẫu" --workflow demo --worker deepseek-nvidia --brief "D:\work\brief có dấu.md" --signal "file:D:\work\kết quả.txt" --fallback-worker deepseek-paid --max-retries 2
python ao_run.py status --id VD1
python ao_run.py resume --id VD1
python ao_run.py stop --id VD1
python ao_run.py doctor
```

Viết brief và xác định tiêu chí hoàn thành trước khi `start`. `file:` cần đường dẫn tuyệt đối; `cmd:<lệnh> exit=N` cũng được hỗ trợ. `start` đăng ký, đọc lại, dispatch rồi tạo supervisor tách rời. `jobs\<id>\events.jsonl` là dòng thời gian; `status.json` cho trạng thái mới nhất; `worker.log` là đầu ra worker đã lọc khóa. `DONE_NEEDS_REVIEW` nghĩa là AO cần kiểm độc lập rồi tự quyết định `accept`. `GAVE_UP` cần Owner quyết định.

Worker thoát mã 0 và mọi `signals` đạt thì vào `DONE_NEEDS_REVIEW`, kể cả log có mẫu lỗi hoặc thiếu `done_markers`.
Worker thoát mã khác 0 hoặc còn tiêu chí chưa đạt thì `FAILED`; lý do ghi mã thoát và tiêu chí còn thiếu.
`error_patterns` chỉ thêm gợi ý từ một dòng log vào lý do `FAILED`, không tự gây retry hay dừng worker.
Worker đang chạy chỉ bị dừng tự động khi log và file kết quả không đổi quá `stall_seconds`.

Giới hạn: `resume` chỉ chạy khi supervisor cũ đã mất; tiến trình worker mồ côi từ lần máy tắt không bị giết vì supervisor mới không sinh ra nó. Sau khi đã ghi task vào sổ, lỗi hệ thống giữa các bước có thể cần AO xử lý theo trạng thái sổ; công cụ báo lỗi thay vì tự sửa spec hoặc tự accept. `cmd:` chạy qua shell theo contract của sổ việc, nên chỉ dùng lệnh do AO tin cậy viết trước.

`start` bắt buộc `--lane A|B|C|D|E` (không phân biệt hoa thường). Làn độc lập với `--project`; job cũ hiện làn `?`. Cờ bảo trì làn A chỉ cảnh báo, không chặn start.
