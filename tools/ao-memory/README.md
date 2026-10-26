# AO Memory — Trung tâm công việc tầng 1a

Hook SessionStart ở `.claude/settings.json` tự nạp `ao_state.py --brief` từ phiên Claude Code mới.
Để gỡ hook, xoá khoá `hooks` trong `.claude/settings.json` hoặc đổi tên file đó.
Mở phiên Claude Code mới sau khi gỡ; phiên đang chạy không tự đổi ngữ cảnh.

Đầu phiên, chạy từ thư mục trung tính (ví dụ thư mục home):

```powershell
python '${AO_ROOT}\tools\ao-memory\ao_state.py' --stdout
python '${AO_ROOT}\tools\ao-memory\ao_recall.py' 'từ khóa cũ'
python '${AO_ROOT}\tools\ao-memory\ao_decide.py' list --last 10
```

Khi Owner nhắc việc cũ, dùng `ao_recall.py`; `--in ledger,registry,memory,decisions,transcripts,fs`, `--last N`, `--since YYYY-MM-DD` giới hạn kết quả. `--rebuild` được chấp nhận; tìm kiếm hiện đọc theo luồng, chưa lưu chỉ mục.

Ghi quyết định: `ao_decide.py add --quote "nguyên văn" --scope "phạm vi" --project example --official no`. Việc chờ Owner: `ao_decide.py wait add --project example --what "..."`; đóng bằng `wait done --id N`. Registry: `registry.py list`, `show ID`, `set ID --next "..."`.

Nguồn sổ việc, bộ nhớ, phiên và taskboard chỉ đọc. Trạng thái task phụ thuộc schema sổ việc hiện có; nếu cần thêm timestamp riêng cho từng task, đề xuất bổ sung vào `queue.py state --json` ở đợt khác. Không dùng công cụ này để duyệt thay Owner.

## Giám sát việc chạy

Từ thư mục trung tính, gọi `python '${AO_ROOT}\tools\ao-memory\ao_run.py'` kèm lệnh dưới đây. `list` và `status --id ID` hiển thị `MẤT_GIÁM_SÁT` khi `status.json` còn `RUNNING` nhưng PID giám sát đã chết. Dòng này cũng cho biết giám sát/worker còn sống và log đổi lần cuối bao lâu; đây là trạng thái đọc, chưa sửa job. Mục **CẢNH BÁO** trong `STATE.md` nhắc việc tương ứng; giờ nêu trong cảnh báo là mốc log cuối, không phải giờ chết chính xác.

`reap --id ID` (hoặc `reap --all`) ghi sự kiện `ORPHANED` và đổi trạng thái job, không chạy worker hay giao lại việc. `resume --id ID` tiếp tục luồng cũ: khởi động giám sát và xử lý retry qua sổ việc. Với status cũ chưa có thời gian tạo tiến trình, việc PID bị tái sử dụng chưa thể loại trừ; job mới lưu thời gian tạo để kiểm tra cùng PID.

## Công cụ có sẵn

Đầu phiên, chạy từ thư mục trung tính (ví dụ thư mục home):

```powershell
python '${AO_ROOT}\tools\ao-memory\ao_state.py' --stdout
python '${AO_ROOT}\tools\ao-memory\ao_toolbox.py' find 'lấy dữ liệu web'
```

Đọc mục **(8) CÔNG CỤ CÓ SẴN** trong STATE trước khi kết luận thiếu công cụ. Khi cần, chạy `ao_toolbox.py build` để quét lại nguồn; `ao_toolbox.py list --loai repo` để xem danh sách (`repo`, `skill`, `tools`, `worker`); `ao_toolbox.py find "<việc>"` để tra tối đa 6 gợi ý. `markdown` in riêng mục 8. Bảng máy đọc ở `data/toolbox.json`; chú thích tay ở `data/toolbox_notes.json`.

Khi thêm công cụ mới, `build` sẽ tự thấy đường dẫn và mô tả nhưng cần cập nhật chú thích tay để có “dùng khi nào”, từ khóa và cảnh báo. Dùng `list` tìm dòng “chưa có mô tả” hoặc repo “chưa có hồ sơ” để tự nhắc rà soát. Có thể đề xuất Owner thêm một dòng chạy `ao_state.py --stdout` vào `CLAUDE.md` sau khi duyệt riêng; đợt này không sửa file đó.

Supervisor chỉ đánh dấu DONE_NEEDS_REVIEW khi worker thoát mã 0, mọi `signals` đạt và có dấu xong nếu cấu hình yêu cầu.
Khi mã thoát khác 0 hoặc thiếu tiêu chí sau khi worker kết thúc, supervisor ghi FAILED với mã thoát và tiêu chí thiếu.
`error_patterns` chỉ thêm dòng log ngắn làm gợi ý trong lý do FAILED; chuỗi xuất hiện trong log không tự làm hỏng kết quả.
Worker còn chạy chỉ bị dừng theo quy tắc treo hoặc yêu cầu dừng.

Dùng `"command": ["{codex_latest}", ...]` để tự chọn Codex có phiên bản cao nhất trong `%LOCALAPPDATA%`.
Công cụ so cả bản trong thư mục mã băm và bản cài cũ; ứng viên lỗi hoặc quá 2 giây bị bỏ qua.
Đặt `AO_CODEX_EXE` thành đường dẫn executable để ghim bản cụ thể; bản ghim lỗi sẽ báo lỗi, không tự đổi bản.
`ao_run.py doctor` in đường dẫn, phiên bản và cảnh báo alpha; sự kiện START lưu executable và phiên bản thực dùng.

Windows: AO ẩn console của worker và lệnh phụ để tránh cửa sổ đen bật liên tục; giám sát vẫn chạy tách rời.
CẢNH BÁO: đừng đóng cửa sổ console của worker vì sẽ làm chết worker/giám sát giữa chừng.
Xem tiến trình bằng `python ao_run.py status` và `STATE.md`; stdout/stderr vẫn nằm trong log của job.
