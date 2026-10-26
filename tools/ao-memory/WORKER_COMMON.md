# WORKER_COMMON — quy tắc chung cho MỌI worker (Codex/Qwen/DeepSeek/NVIDIA)

Brief của từng việc chỉ cần ghi: **Việc / Mục tiêu / Đầu vào / Đầu ra / Tiêu chí xong** và dòng
`Đọc trước và tuân thủ: ${AO_ROOT}\tools\ao-memory\WORKER_COMMON.md`.
Không chép lại các điều dưới đây vào brief. Nếu brief ghi khác (hẹp hơn) cho riêng việc đó thì brief thắng.

## Quy tắc cứng
1. Làm thẳng bằng công cụ của bạn. KHÔNG dùng `coordination/bus/mailbox`.
2. Windows. Python 3.13 chuẩn (thư viện chuẩn, trừ khi brief cho thêm); `ffmpeg/ffprobe` có sẵn.
3. Chạy `python` từ thư mục trung tính (VD thư mục tạm), KHÔNG từ `tools\ao-journal`.
4. Chỉ GHI trong thư mục/ file brief nêu ở "Đầu ra". Mọi nơi khác chỉ ĐỌC. Không động vào `CLAUDE.md`, `AGENTS.md`, `workers.json`, sổ việc/jobs của AO, file khoá.
5. Không commit/push, không xoá/di chuyển dữ liệu có sẵn, không cài phần mềm, không tốn tiền, không đăng/gửi tin ra ngoài — trừ khi brief ghi rõ kèm trích dẫn duyệt của Owner.
6. Không in/ghi khoá, token, `.worker-keys`, dữ liệu khách ra màn hình, log, brief hay báo cáo.
7. Thêm phụ thuộc mới: không (trừ khi brief cho phép).

## Báo cáo kết quả (ngắn)
- Ghi báo cáo ra file nêu ở brief (mặc định `result_<ID>.md` cạnh brief), ≤ ~40 dòng: đã làm gì · file tạo/sửa (đường dẫn đầy đủ) · lệnh đã chạy và kết quả thật · điều chưa làm được/rủi ro.
- Không dán lại nội dung file lớn vào báo cáo; chỉ ghi đường dẫn + số dòng/đoạn liên quan.
- Tự kiểm bằng cách chạy thật (test/lệnh) trước khi báo xong; nói thẳng nếu có test fail hoặc bước bị bỏ qua.

## Khung brief tối thiểu (điều phối viên dùng)
```
# Việc: <tên>
Đọc trước và tuân thủ: ${AO_ROOT}\tools\ao-memory\WORKER_COMMON.md
Mục tiêu: <1–3 câu>
Đầu vào: <đường dẫn file/thư mục cần đọc>
Đầu ra: <đường dẫn được GHI> ; báo cáo: <đường dẫn result_*.md>
Tiêu chí xong: <kiểm được bằng máy: file tồn tại, lệnh X exit 0, test Y pass>
Ngoại lệ so với WORKER_COMMON (nếu có): <...>
```
Cách giao (điều phối viên): ghi brief bằng công cụ Write ra file, rồi `ao_run.py start ... --brief <đường dẫn>`. KHÔNG dán nội dung brief vào lệnh shell (heredoc).
