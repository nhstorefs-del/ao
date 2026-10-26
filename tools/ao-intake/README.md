# AO Intake — luồng yêu cầu và bản mã duyệt DOT2A

## DOT2A — mã thẻ duyệt (chưa bật hook và chưa vá script thật)

`approval_hook.py` là hook `UserPromptSubmit` chưa đăng ký. Nó bỏ nội dung `<pasted_content>` trước khi tìm lời Owner tự gõ `APPROVAL BY OWNER` hoặc `APROVAL BY OWNER` cùng mã `REQ-<hex>`. Tin khác được ghi vào `data/owner_messages.jsonl`; hook không in vào chat. `AO_APPROVAL_HOOK=0` tắt hook. Lỗi hook chỉ ghi loại lỗi vào `logs/approval_hook.log` và không chặn prompt.

`approval_ledger.py` ghi sổ JSONL nối băm, mặc định 30 phút và tối đa 5 lần dùng. Sổ bị sửa ở một dòng sẽ khiến đọc hoặc tiêu thụ thẻ thất bại. `approval_gate.require(tool_name, summary)` chỉ kiểm thẻ khi có biến `AO_INTAKE_REQUEST`; thiếu thẻ hoặc sổ không đọc được thì từ chối với mã 7 khi gọi CLI `approval_gate.py`. Dispatcher đặt biến đó vào tiến trình Claude nền; `ao_run.py` và `ao_supervisor.py` hiện kế thừa môi trường khi khởi chạy worker.

Yêu cầu nhạy cảm giữ `WAITING_OWNER` đến khi có thẻ đúng mã; chu kỳ dispatcher kế tiếp chuyển CAS về `RECORDED` và ghi event. Trang việc hiển thị câu lệnh mẫu để Owner chép vào chat và giờ duyệt/hết hạn. Hiện chưa có nút duyệt. Cổng đăng/gửi thực tế chưa được gắn vào bốn script; bản vá mẫu và hướng dẫn ở `patches/README.md`. Cần Owner duyệt **riêng** việc đăng ký hook và việc vá bốn script trước khi bật bảo vệ thực tế.

Giới hạn: worker cùng tài khoản Windows có thể gọi script trực tiếp khi không có `AO_INTAKE_REQUEST`; cấu hình quyền Claude không thay thế cô lập hệ điều hành. Chuỗi băm phát hiện sửa dòng còn tồn tại nhưng không tự phát hiện việc xóa nguyên phần đuôi nếu không có mốc băm tin cậy độc lập.

Python 3.13, thư viện chuẩn. Có trang việc trong hub; không có lịch, bus hay hành động Telegram trong intake. Chạy các lệnh dưới từ `${AO_ROOT}`.

```powershell
python tools/ao-intake/store.py submit --text "Đối chiếu tài liệu" --key "moi-lan-bam-mot-uuid" --kind task
python tools/ao-intake/store.py list --state RECORDED
python tools/ao-intake/store.py show --id REQ-...
python tools/ao-intake/dispatcher.py once
python tools/ao-intake/dispatcher.py reply --conversation CONV-... --text "Thông tin bổ sung" --key "uuid-cua-tin-nay"
python tools/ao-intake/recover.py scan
python tools/ao-intake/recover.py scan --apply
```

`submit` trả JSON sau commit SQLite WAL + synchronous FULL. Cùng key trả bản cũ (`deduplicated=true`); key khác luôn tạo mới. `similar_to` chỉ cảnh báo nội dung trùng chính xác trong 24 giờ. Nguồn mặc định `cli`, có `--source`. `--db PATH` đứng trước tên lệnh để dùng DB riêng.

Trạng thái: RECORDED → CLAIMED → RUNNING → DONE / FAILED / NEEDS_OWNER_ANSWER / WAITING_OWNER / WAITING_QUOTA. Cổng nhạy cảm chặn ngay từ CLAIMED → WAITING_OWNER. Chuyển trạng thái bằng CAS, nhật ký cùng giao dịch; trigger cấm lùi trạng thái và sửa/xóa events. `DONE` là hoàn tất lượt intake có báo cáo, **không** thay thế nghiệm thu độc lập AO.

Mỗi hội thoại chỉ một lượt hoạt động. Tin trả lời được commit thành request mới và message Owner ngay cả khi Claude đang chạy; request cũ không mở lại. `once` kế tiếp xử lý hàng đợi theo thứ tự. Lượt sau dùng session ID đã lưu + `--resume`. `reply --key` chống gửi trùng; bỏ key sinh UUID mới. Không có session thì từ chối nối giả bằng phiên mới. Trần 40 lượt/hội thoại, task 12 lượt công cụ, ask 4; timeout 20 phút mỗi lượt.

Claude dùng thư mục cấu hình riêng (biến `AO_INTAKE_CLAUDE_CONFIG_DIR`, mặc định `~/.claude`), stdin DEVNULL, permission-mode default, không bỏ qua quyền; tắt ba hook qua AO_ONOPEN_HOOK/AO_COVERAGE/AO_SESSIONSTART=0, loại biến CLAUDE/ANTHROPIC cũ. `--tools` thu hẹp bộ công cụ: ask chỉ Read/Glob/Grep, task Read/Glob/Grep+Bash. `--allowedTools` có Read/Glob/Grep; Bash của task chỉ ao_run start/status và ao_status đúng mẫu brief; disallowedTools đủ tám mẫu yêu cầu. Không thêm Write/Edit/Bash cho ask. **Vì vậy ask không chạy được ao_status**. Dispatcher lưu câu trả lời cuối thành report.md; Claude không cần quyền ghi trực tiếp. Lượt thật trước DISPFIX chứng minh việc lưu báo cáo trong evidence/real_ask.json.

Máy sinh prompt.md và worker_brief.md riêng mỗi request; signal/report của thợ cũng phải ở thư mục đó. Claude tự chọn thợ/làn theo bản chất, dispatcher không phân làn bằng từ khóa. Job đầu dùng id tất định `<request_id>-job1`, job sau tăng số. Báo cáo cuối bắt đầu `CAN_OWNER_TRA_LOI:` nếu cần trả lời; `CHO_OWNER_DUYET:` nếu cần duyệt; `CHO_NGHIEM_THU:` nếu đã giao thợ chưa kiểm xong. Máy đọc marker dòng đầu. Không dùng sự tồn tại của báo cáo thợ như chứng cứ nghiệm thu.

Cổng nhạy cảm dùng từ khóa chuẩn hóa dấu tiếng Việt: đăng/hẹn bài, gửi tin, xóa/di chuyển, tải/cài phần mềm, chi tiền, sửa CLAUDE.md/AGENTS.md. Yêu cầu nhạy cảm vào WAITING_OWNER cho tới khi hook đã đăng ký ghi được thẻ đúng mã; cụm APPROVAL BY OWNER nằm ngay trong yêu cầu giao diện không tự duyệt. Đây **chỉ là lưới phụ**: có thể chặn nhầm hoặc lọt diễn đạt khác. Quyền hẹp do Claude Code thi hành là lớp chính cho Claude nền; cổng trong bốn script còn chờ vá.

Khôi phục đối chiếu PID **và thời điểm tạo**. Khóa hội thoại giữ PID dispatcher đến khi ghi xong kết quả, tránh khôi phục chen vào khoảng Claude đã thoát nhưng kết quả chưa commit. PID không đủ bằng chứng thì không tự coi là chết. `scan` chỉ đọc/đề xuất, không tăng attempts; dùng `--apply` cho cả khôi phục Claude chưa có job. `--apply`: chỉ resume job ORPHANED/STALLED khi supervisor và worker đều chết. Job RUNNING/CLAIMED/RETRYING mất tiến trình được đề xuất resume nhưng `apply_eligible=false`, không chạy tự động; cần xử lý trạng thái AO trước. DONE/DONE_NEEDS_REVIEW/live/unknown/failed không đụng. Bất kỳ job nào đã tồn tại (kể cả status hỏng/mất) cũng chặn giao lại Claude. Chưa có job mới được khôi phục Claude với ảnh chụp máy và --resume nếu có session, tối đa 2 lần; giữ RUNNING, không lùi về RECORDED. DONE/FAILED/WAITING_OWNER không chạy lại. Khóa sót sau commit cuối được thu hồi khi chủ khóa đã chết, chỉ để nhận tin mới.

Giới hạn còn lại:

- Quyền Bash dùng mẫu CLI, **không** xác minh máy từng tham số worker/workflow/brief/signal của ao_run. Phạm vi ghi thợ hiện là brief; không có sandbox tài khoản Windows riêng hay allowlist worker máy cưỡng chế. Không có guard toàn cây sau từng job nền. Y5.2 trong kế hoạch rộng chưa được chứng minh đầy đủ bởi bước này; hash kiểm nguồn khi chạy regression không thay thế guard thợ. Chỉ dùng việc nháp không tác dụng phụ ra ngoài.
- Các lệnh ao_run vẫn có quyền sẵn có của hệ AO. Không tuyên bố khóa tuyệt đối thợ, phê duyệt bằng thẻ, callback hoặc bảo đảm tự tiếp tục. Marker và báo cáo vẫn cần người điều phối kiểm thật.
- WAITING_QUOTA giữ nguyên yêu cầu, chưa có retry quota. WAITING_OWNER chỉ quay về RECORDED khi chu kỳ kế tiếp thấy thẻ hợp lệ đúng mã; hook phải được đăng ký riêng mới tạo thẻ từ chat. Resume job phải có người chủ động dùng --apply; chưa có lịch quét. Nếu job đã tồn tại thì người điều phối đọc kết quả/nghiệm thu; chưa tự đồng bộ DONE từ job.
- Timeout/ngoại lệ được FAILED; không tự chạy lại FAILED. Không lưu stderr thô (tránh dữ liệu nhạy cảm); last_error ghi loại lỗi. Khi Claude chưa trả JSON session ID thì không có session để resume. DB có migration cộng thêm total_cost_usd cho schema intake cũ; giữ nguyên dữ liệu và nhật ký. Không hỗ trợ schema không tương thích khác.
- Nội dung nguyên văn được lưu cục bộ và đưa vào Claude theo yêu cầu; không nhập khóa/token hay dữ liệu khách. Máy này chạy cùng người dùng nên Read không phải sandbox dữ liệu.

Kiểm thử: `cd tools/ao-intake; python -m unittest test_intake`. `python -B tools/ao-intake/verify.py` chạy các suite và manifest; dùng nguyên `run_independent.main()` nhưng đổi OUTPUT trong bộ nhớ sang evidence/ để giữ phạm vi ghi. `smoke_real.py` là phép thử thủ công duy nhất, có chặn lần chạy lại; không nằm trong unittest. Không chạy nó lần nữa để kiểm CLI.

## DISPFIX — 13/10/2026

- Read/Glob/Grep cho cả task và ask. Brief nêu gốc làm việc `${AO_ROOT}`, dùng Glob tìm tên (ví dụ `**/LANES/README*`, `projects/**`), Grep tìm nội dung; không dùng Bash dò tệp, không lặp lệnh bị từ chối. Không tìm thấy sau vài bước thì hỏi bằng `CAN_OWNER_TRA_LOI:`.
- Trần mỗi lượt gọi Claude: task 12 bước / 0,40 USD; ask 4 bước / 0,15 USD qua `--max-budget-usd` (đã xác nhận bằng `claude --help`). Giữ quyền Bash hẹp của task và toàn bộ disallowedTools; ask không có Bash/giao thợ.
- Mỗi lượt lưu `requests/<request_id>/run_result_<n>.json`, kể cả ngoại lệ runner (chỉ tên loại lỗi). Đây là bản chẩn đoán chọn trường: is_error, subtype, num_turns, total_cost_usd, stop_reason/terminal_reason, 500 ký tự đầu result; tối đa 50 permission_denials, mỗi mục chỉ tool_name + tối đa 80 ký tự command. Có tổng số lệnh bị chặn. Lược trường khác và che các mẫu khóa/token phổ biến trước khi cắt; che theo mẫu không bảo đảm phát hiện mọi bí mật tùy ý.
- requests.total_cost_usd cộng chi phí các lượt theo request (bao gồm phục hồi); migration thêm cột mặc định 0, không xóa dữ liệu. Không khôi phục chi phí lịch sử chưa được lưu. Request trả lời mới có tổng riêng.
- FAILED/WAITING_QUOTA lưu lý do tiếng Việt ở last_error và events.note; trang việc hiện hai trường này sẵn có. error_max_turns nêu N/M và gợi ý đường dẫn; hết ngân sách nêu trần; có lệnh bị chặn thì nêu số lượng. DONE giữ hành vi báo cáo như cũ và vẫn cần nghiệm thu độc lập.
- FAILED không tự chạy lại: Owner giao lại bằng một yêu cầu mới với mã khóa (idempotency_key) mới. Chưa thêm nút giao lại.
- Giới hạn: chưa kiểm Claude nền thật/độ cưỡng chế trần trên tài khoản thật; không khởi động lại hub, không đổi lịch Windows, không thao tác DB thật. Chi phí do CLI báo, trần áp dụng mỗi lượt gọi, không phải cả hội thoại.
