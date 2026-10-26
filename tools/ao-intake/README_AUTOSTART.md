# Chu kỳ intake và tự bật hub — Owner tự tạo lịch

Chỉ chuẩn bị mã/wrapper. Chưa tạo hoặc sửa tác vụ lịch Windows. Chạy dưới tài khoản Windows hiện tại khi đã đăng nhập; không cần quyền cao nhất.

- `run_dispatch_cycle.py`: một lượt rồi thoát, khóa độc quyền → kiểm hàng chờ → `recover.scan(apply=True)` → `Dispatcher.once()`. Hàng chờ RECORDED rỗng và không có RUNNING/CLAIMED thì ghi `idle`, không gọi dispatcher/Claude. Lỗi từng bước chỉ ghi tên loại lỗi và tiếp tục; mã thoát 0. Phải xem log để biết có lỗi.
- `cycle.lock`: PID, thời điểm UTC và danh tính tiến trình của lần chiếm gần nhất. Khóa byte của hệ điều hành mới là quyền sở hữu; tự nhả khi tiến trình chết. Tệp giữ lại sau chu kỳ; không xóa/đổi tên tệp này khi chu kỳ đang chạy. Metadata cũ không nghĩa là đang chạy, và không được dùng để cưỡng ép lấy khóa còn sống.
- `hub_autostart.cmd`: curl tối đa 2 giây tới 8798; có phản hồi HTTP thì thoát. Nếu không phản hồi, gọi Python 3.13 tuyệt đối để mở hub ẩn cửa sổ, không mở trình duyệt, không dừng/khởi động lại tiến trình nào.
- `test_cycle.py`: kiểm thử offline bằng mock/DB riêng, gồm khóa tiến trình chết và xoay log. Không gọi Claude/AO thật.
- `PAUSE`: có tệp này thì chu kỳ thoát 0 ngay, không tạo DB/khóa/log. Không dừng chu kỳ đã chạy, không tắt hub.
- `logs\cycle.log`: mốc bắt đầu/kết thúc, số kết quả và loại lỗi; không lưu nguyên văn yêu cầu hay stderr. Xoay trước khi vượt 1 MiB, giữ 3 bản `.1`–`.3`. Lượt bị khóa còn sống thoát im lặng.

## Lệnh Owner tự chạy để tạo hai tác vụ

Dán nguyên khối sau vào PowerShell **nếu Owner muốn bật lịch**. Đây chỉ là hướng dẫn; người làm brief không chạy khối này. Lệnh không có `-Force`: nếu tên đã tồn tại, kiểm tra tác vụ cũ trước, không tự ghi đè.

```powershell
$aoRoot = '${AO_ROOT}'
$aoPython = (Get-Command python).Source
$aoPrincipal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$aoSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
$aoCycle = New-ScheduledTaskAction -Execute $aoPython -Argument '-X utf8 -B "${AO_ROOT}\tools\ao-intake\run_dispatch_cycle.py"' -WorkingDirectory $aoRoot
$aoEvery2 = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 2)
Register-ScheduledTask -TaskName 'AO-Intake-Cycle' -Action $aoCycle -Trigger $aoEvery2 -Settings $aoSettings -Principal $aoPrincipal
$aoHub = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\cmd.exe" -Argument '/d /c ""${AO_ROOT}\tools\ao-intake\hub_autostart.cmd""' -WorkingDirectory $aoRoot
$aoLogin = New-ScheduledTaskTrigger -AtLogOn -User ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)
$aoEvery10 = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 10)
Register-ScheduledTask -TaskName 'AO-Hub-Autostart' -Action $aoHub -Trigger @($aoLogin, $aoEvery10) -Settings $aoSettings -Principal $aoPrincipal
```

Chu kỳ chạy mỗi 2 phút; hub chạy khi đăng nhập và mỗi 10 phút. Không đặt giới hạn cưỡng ép dừng chu kỳ vì dispatcher có thể đang chờ Claude; lần lịch kế tiếp bị IgnoreNew/khóa bỏ qua. Tác vụ dùng chế độ chỉ chạy khi đăng nhập. Hai trigger lặp không đặt thời hạn kết thúc.

## Tắt nhanh và xem log

```powershell
schtasks /Change /TN AO-Intake-Cycle /Disable
schtasks /Change /TN AO-Hub-Autostart /Disable
New-Item -ItemType File -Path '${AO_ROOT}\tools\ao-intake\PAUSE' -Force
Get-Content -LiteralPath '${AO_ROOT}\tools\ao-intake\logs\cycle.log' -Tail 40
```

Disable chỉ ngăn lần lịch tiếp theo; PAUSE cũng chỉ ngăn chu kỳ mới. Muốn bật lại, Owner tự bỏ tệp PAUSE và dùng `/Enable` cho đúng tên tác vụ. Không dừng hub hay tiến trình đang chạy bằng wrapper.

Kiểm thử hàng rỗng với DB riêng (không trỏ DB thật):

```powershell
& (Get-Command python).Source -X utf8 -B '${AO_ROOT}\tools\ao-intake\run_dispatch_cycle.py' --db '${AO_ROOT}\tools\ao-intake\evidence\intake4_empty\intake.db' --base '${AO_ROOT}\tools\ao-intake\evidence\intake4_empty'
```

Giới hạn: chỉ dùng nháp/đối chiếu không tác dụng phụ ra ngoài như README gốc. Khôi phục giữ nguyên luật bước 2; không tự nghiệm thu job, không retry FAILED/WAITING_QUOTA, không bảo đảm callback. Lịch có hàng thật sẽ có thể dùng hạn mức của tài khoản Claude dùng cho intake. Wrapper chỉ kiểm phản hồi HTTP, không xác minh đó là hub đúng phiên bản; lỗi/treo hub không được wrapper cưỡng ép sửa. Không có kiểm thử bật hub mới hay chạy lịch thật trong brief này.
