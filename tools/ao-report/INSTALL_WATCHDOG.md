# Cài hai lịch độc lập

Chạy từ PowerShell trong tài khoản Windows có cấu hình Telegram hiện có. Chỉ thực hiện sau khi Owner duyệt cài lịch:

```powershell
schtasks /Create /TN "AO-Watchdog" /SC MINUTE /MO 30 /ST 07:00 /ET 22:00 /TR 'python "${AO_ROOT}\tools\ao-report\watchdog.py"' /F
schtasks /Create /TN "AO-CheckChecker" /SC MINUTE /MO 60 /ST 07:00 /ET 22:00 /TR 'python "${AO_ROOT}\tools\ao-report\check_the_checker.py"' /F
```

Xem: `schtasks /Query /TN "AO-Watchdog" /V /FO LIST` và tương tự `AO-CheckChecker`.

Tắt: `schtasks /Change /TN "AO-Watchdog" /Disable` và tương tự `AO-CheckChecker`.

Xóa: `schtasks /Delete /TN "AO-Watchdog" /F` và tương tự `AO-CheckChecker`.

Chưa chạy bất kỳ lệnh tạo lịch nào.
