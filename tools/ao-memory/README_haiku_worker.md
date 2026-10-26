# Worker `haiku` cho AO

`workers.json` gọi `haiku_worker.py` bằng Python 3.13 và đưa brief qua stdin UTF-8. Bộ bọc đọc khóa và model từ `.worker-keys/haiku.env` khi chạy; mặc định model là `claude-haiku-5-5`. Không đặt khóa trong `workers.json`, tham số dòng lệnh hoặc tài liệu.

Claude chạy tại gốc `${AO_ROOT}`, dùng cấu hình riêng `~/.claude-haiku` (đổi bằng `AO_HAIKU_CONFIG_DIR`), với `--permission-mode acceptEdits`. Chỉ cho phép `Read Glob Grep Write Edit`; cấm `Bash PowerShell WebFetch WebSearch`. Các biến hook `AO_ONOPEN_HOOK`, `AO_COVERAGE`, `AO_SESSIONSTART` đều bằng `0`. Bộ bọc che nguyên chuỗi khóa nếu xuất hiện trong stdout/stderr.

Trần cứng trong mã: trước khi chạy, đếm brief bằng API `count_tokens` miễn phí rồi cộng dự phòng hệ thống 5.000 token; nếu API đếm lỗi thì lấy `ceil(len(brief)/2)` cộng 5.000. Từ 90.000 token trở lên thì từ chối trước khi gọi Claude, mã thoát 3. Khi chạy, bộ bọc đọc `stream-json` và cộng input, cache creation, cache read theo từng message; nếu một lượt vượt 90.000 token vào hoặc tổng việc vượt 400.000 token vào + ra thì dừng cây tiến trình, mã thoát 4. Không có usage hợp lệ thì đóng việc, mã thoát 2. Một dòng chi tiêu cho mỗi lần chạy nằm ở `data/haiku_usage.jsonl`; đây là ước tính theo bậc giá dưới.

Đã kiểm bằng `claude --help`: CLI hỗ trợ `--output-format stream-json --verbose`, `--max-turns 25` và `--autocompact 100k`. Mức nén nhỏ nhất CLI công bố là 100k; trần 90k được kiểm qua usage. Usage đến sau một lượt gọi API nên cơ chế này dừng ngay khi nhận báo cáo lượt đó; nó không thể hủy lượt đã được nhà cung cấp xử lý.

Nếu CLI yêu cầu đăng nhập lần đầu hoặc không nhận `ANTHROPIC_API_KEY` trong chế độ `-p`, kiểm tra phiên bản CLI và cơ chế xác thực API chính thức của Claude Code. Có thể dùng `settings.json` trong thư mục cấu hình riêng hoặc `--settings` nếu tài liệu phiên bản đang cài hỗ trợ `apiKeyHelper`; không đặt khóa dạng văn bản thuần vào settings, không dùng `bypassPermissions`. Chỉ chạy lại sau khi xác nhận đường xác thực và quyền truy cập.

Kiểm thử cục bộ: từ `tools\ao-memory\tests`, chạy `python -m unittest test_haiku_worker`. Bài thử mock `subprocess`, không gọi API. Chạy thật qua `ao_run` phát sinh chi phí API và phải có phê duyệt Owner riêng theo luật repo.

Giới hạn: danh sách công cụ của CLI chỉ giới hạn lời gọi công cụ trực tiếp; model vẫn có thể đọc nội dung tệp mà brief cho phép. `acceptEdits` cho phép ghi tệp nên brief phải chỉ rõ đường dẫn được ghi. Tệp được ghi trước khi chạm trần giữ nguyên.
