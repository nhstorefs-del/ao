# Lý do triển khai cho AO

Mục 1 — chọn làm journal JSONL append-only kèm SHA-256 hash-chain ở mức từng receipt — Vì AO hiện chỉ có một owner nhưng giao việc qua nhiều worker, nên bằng chứng quan trọng nhất là biết brief nào đã được giao và lịch sử có bị sửa sau khi worker trả kết quả hay không; nối hash cho từng dòng đạt mục tiêu đó mà không cần thêm cơ sở dữ liệu hay dịch vụ vận hành.

Mục 2 — chọn queue JSON chạy bằng lệnh `sync`/`list`, không làm vòng lặp nền — Vì Claude trong AO chỉ có thể hành động trong một lượt hội thoại hoặc tác vụ hẹn giờ, không phải tiến trình luôn chạy; một daemon sẽ tạo thêm trạng thái khó quan sát nhưng vẫn không thể tự giao việc khi không có lượt gọi, còn queue foreground cho Claude đọc đúng lúc mở phiên hoặc trước khi dispatch.

Mục 3 — chọn lưu completion signals ngay trong receipt dispatched và kiểm bằng một lệnh riêng — Vì tiêu chí “xong” phải được cố định cùng đúng hash của brief trước khi Codex/Qwen bắt đầu; nếu để worker khai lại sau khi làm, tiêu chí có thể bị nới theo kết quả. AO vẫn cần Claude xem chất lượng đầu ra, nên lớp này chỉ tự đo file/exit code trước, không tự tuyên bố chất lượng nội dung.

Mục 4 — chọn chỉ ghi `difficulty`, `cost_tokens`, `quality`, chưa làm thuật toán chọn worker — Sổ thật bắt đầu từ 0 lượt (dữ liệu thử nghiệm đã lưu riêng ở `_selftest_archive/`), chưa đủ ~50 lượt để so sánh Codex/Qwen theo loại việc mà không bị nhiễu bởi vài mẫu đầu; đây chỉ là bước ghi dữ liệu để sau này mới tính, không phải cơ chế chọn worker.

Mục 5 — chọn salvage opt-in, copy theo timestamp và giữ nguyên đường dẫn tương đối, không move/xoá — Vì AO có thể cần Claude dọn hoặc ghi đè file ngoài scope sau khi guard cảnh báo; bản copy giữ được chính xác nội dung worker đã tạo để hậu kiểm, còn mặc định tắt bảo toàn toàn bộ cách dispatch hiện tại và tránh tự sinh kho bằng chứng khi owner không yêu cầu.

## PTP bước 1

`done` là lời báo của worker, còn `accepted` là quyết định của coordinator sau khi đo lại completion signals và lưu bằng chứng cùng hash đầu ra. Tách hai trạng thái để một lời báo xong hoặc một file đã biến mất không thể tự mở công việc tiếp theo.

Vì vậy `queue sync` chỉ mở task con khi mọi task cha đã `accepted`. Điều kiện này đặt cổng kiểm ngay tại điểm chuyển giao giữa các task; một task cha mới `done` vẫn cần được kiểm và chấp nhận. `accept` lặp lại không thêm receipt để lịch sử không bị phình và coordinator tiếp quản có thể gọi lại an toàn.

`queue state` chỉ tính bản chiếu từ journal và queue tại lúc gọi, không lưu file trạng thái thứ ba. Coordinator mới có một lệnh đọc được task sẵn sàng, receipt mới nhất, bằng chứng chấp nhận và tình trạng hash-chain mà không cần hòa giải nhiều bản ghi sự thật.

## PTP bước 2

`verify-task` đo lại các signal đã cố định lúc dispatch và ghi receipt `verified` gồm kết quả từng signal, hash file và hash brief tại lúc đo. Receipt này chỉ là bằng chứng; `queue sync` và trạng thái task vẫn bỏ qua nó. Signal thất bại vẫn được ghi với `passed:false` để thấy rõ lần kiểm thất bại; `accept` không dùng receipt đó để chấp nhận.

`accept` đòi task đã `done` vì lời worker chỉ xác định thời điểm cần kiểm, không chứng minh kết quả. Cổng `NO_SIGNALS` tránh chấp nhận khi không có tiêu chí đo; `SIGNAL_FAILED` đòi mọi tiêu chí thực sự đạt tại lúc chấp nhận. Hash brief hiện tại phải bằng hash đã chốt khi dispatch để tiêu chí không bị thay sau lúc giao. Hash file hiện tại phải bằng hash của receipt `verified` để file bằng chứng không thể bị tráo hoặc xoá giữa hai lệnh. Nếu chưa có receipt `verified` sau `done`, `accept` tự tạo nó sau các phép kiểm ban đầu.

Kiểm hash-chain trước khi ghi `accepted` ngăn chấp nhận dựa trên lịch sử đã bị sửa. Guard report chỉ là cổng bổ sung khi được chỉ định; thiếu dòng PASS độc lập thì từ chối. Receipt `accepted` trỏ tới `verified_seq` và liệt kê các cổng đã qua để người tiếp quản kiểm tra được quyết định. Từ chối không ghi `rejected`, giữ journal không đổi trong các ca lỗi thông thường và tránh biến một lần kiểm thất bại thành trạng thái công việc.

Guard dùng regex khớp toàn dòng để nhận cả `GUARD_RESULT: PASS` và `PASS exit_code=<n>` do guard thật in ra, đồng thời từ chối tiền tố lạ hoặc WARN.
Lấy dòng chứa `GUARD_RESULT:` cuối cùng để PASS cũ không che được WARN hoặc kết quả không hợp lệ xuất hiện sau đó.

## PTP bước 3

`attempt_id` tăng theo số dispatch của từng task và buộc worker gửi lại khi `complete`. Kết quả đến muộn của attempt cũ vì thế không thể ghi đè attempt mới hoặc trạng thái đã `accepted`. Receipt cũ không có trường này vẫn được suy ra từ thứ tự dispatch trong journal để tiếp tục đọc và xử lý lịch sử đã có.

`idempotency_key` gắn task với hash brief đã giao. Cùng với chặn `ALREADY_ACCEPTED`, nó giúp coordinator tiếp quản nhận ra công việc đã hoàn tất và không chạy lại. Retry phải có lý do ghi vào receipt; attempt đang bay chỉ được retry khi người gọi nêu rõ ngưỡng stale, vì thời gian trôi qua đơn thuần không chứng minh worker đã dừng.

## Khoá ghi

Kiểm tra 15 vòng với nhiều coordinator đã bắt được hai đến ba receipt `accepted` cho cùng task, `attempt_id` bị trùng, lệnh báo thành công nhưng thiếu dòng và hash-chain bị đứt. Các lệnh trước đây đọc journal và kiểm tra điều kiện trước khi ghi mà không loại trừ tiến trình khác. Khoá tệp liên tiến trình bao trọn chuỗi đọc, kiểm tra và ghi, kể cả lúc `accept` tự gọi `verify-task`. Lệnh đọc `verify` và `queue state` cũng giữ khoá journal để không đọc một dòng đang ghi dở. `queue add` và `sync` giữ khoá queue cho thao tác đọc, sửa, ghi; thao tác cần cả hai luôn lấy journal trước queue.

Khoá tệp dùng được với các lệnh foreground hiện có và chỉ cần thư viện chuẩn, không tạo daemon hay cơ sở dữ liệu để vận hành. Lệnh chờ tối đa khoảng 10 giây rồi báo `LOCK_TIMEOUT` mà không thay đổi journal. File khoá quá 60 giây được coi là stale để phục hồi sau khi tiến trình chết. Ngưỡng thời gian có rủi ro: tiến trình còn sống nhưng giữ khoá hơn 60 giây cũng có thể bị giành khoá; khi đó hai thao tác có thể lại chồng nhau. Vì vậy các thao tác trong khoá cần hoàn thành nhanh, và việc xoá khoá chỉ thực hiện nếu file vẫn thuộc đúng tiến trình đang nhả.

Checkpoint là receipt trong journal có hash-chain, không phải file trạng thái thứ hai. `queue state` chỉ chiếu checkpoint mới nhất để người tiếp quản biết hành động tiếp theo và các file ngữ cảnh cần đọc. Cờ `stale_dispatch` chỉ cảnh báo; tự đổi trạng thái hoặc tự retry có thể tạo hai worker cùng chạy khi worker đầu vẫn còn hoạt động.

## C1/C2

## AMEND

Core chỉ cho coordinator tự thêm tiêu chí đo và chuyển sang worker đã nằm trong danh sách fallback lúc lập kế hoạch. Thêm tiêu chí chỉ làm chuẩn “xong” chặt hơn; bỏ, nới hoặc sửa tiêu chí, brief, worker và danh sách fallback phải qua đề xuất và duyệt của Owner. Mỗi lần đổi tạo phiên bản spec và giữ lịch sử, để lượt đã giao tiếp tục được chấm theo tín hiệu và hash của phiên bản lúc giao. Approval-ref là một bước giảm tốc: Core kiểm tra dòng tham chiếu trong sổ duyệt, nhưng không xác thực được danh tính người gọi CLI hay tác giả dòng đó. Adapter và hệ thống xác thực người dùng nằm ngoài Core giai đoạn này.

Spec `worker`, đường dẫn brief tuyệt đối và danh sách signals được chốt lúc `queue add` bằng hash của JSON chuẩn tắc. Không có lệnh sửa spec; `queue dispatch` kiểm lại hash trước khi giao việc. Như vậy coordinator không thể hạ chuẩn “xong” bằng cách đổi worker, brief hay signals giữa lúc lập kế hoạch và lúc dispatch. Task cũ thiếu spec vẫn đọc được nhưng Core từ chối dispatch với `NO_SPEC`.

`queue dispatch` lấy đúng dữ liệu từ spec và gọi `journal.append` trong cùng khoá journal rồi queue, nên luật attempt, retry và hash-chain vẫn nằm ở Core hiện có. `journal.append` chỉ nhận thêm tham số tùy chọn `dispatched_by` vì receipt phải ghi người thực hiện dispatch ngay trước khi tính hash; sửa receipt sau khi ghi sẽ phá hash-chain.

`allowed_actions` và `workflow_allowed` được tính trong `queue state` từ bản chiếu journal và queue. Coordinator chỉ cần chọn hành động Core cho phép, không phải tự mang logic trạng thái sang Adapter. `done` ở cấp workflow chỉ xuất hiện khi workflow có ít nhất một task và tất cả đã accepted; checkpoint `NEEDS_OWNER:` mới nhất quyết định danh sách `needs_owner`.
