# Compatibility restriction — DELEGATE-FIRST
`guard_run.py` no longer executes arbitrary commands. It exits 2 before parsing legacy command arguments, creating output files, snapshots, or spawning subprocesses. There is no role, reason, or override flag.
Use `tools/ao-memory/ao_run.py` configured worker dispatch, which records a durable decision and checks its execution lease. For independent artifact verification, use standalone `snapshot.py` and `diff_guard.py`; their existing behavior is unchanged.
The examples below are historical and no longer executable via this wrapper.

# Dispatch guard

Chụp cây thư mục bằng `python snapshot.py <thư_mục> --out snapshot.json [--ignore <glob>]`, rồi kiểm tra bằng `python diff_guard.py --before before.json --after after.json --allow "src/**" --report report.md`.

Lệnh dùng hằng ngày: `python guard_run.py --dir <thư_mục_theo_dõi> --allow "src/**" --workdir <thư_mục_chạy> --log run.log --report report.md -- <lệnh>`. Công cụ giữ exit code của lệnh, ghi toàn bộ output vào log và in một dòng `GUARD_RESULT` để biết có file ngoài phạm vi hay không.

Tuỳ chọn `--salvage-dir <thư_mục>` copy mọi file **mới hoặc bị sửa** ngoài phạm vi vào `<thư_mục>/<timestamp>/<đường_dẫn_tương_đối>` trước khi ghi báo cáo. Bản gốc không bị di chuyển hay xoá. File đã bị xoá vẫn được báo cáo nhưng không thể copy từ snapshot sau. Không truyền tuỳ chọn này thì guard giữ nguyên hành vi cũ và không tạo bản cứu. Có thể dùng trực tiếp: `python diff_guard.py ... --salvage-dir salvage`, hoặc thêm cùng tuỳ chọn vào `guard_run.py` trước dấu `--`.

Ví dụ: `python guard_run.py --dir "D:\work\my-project" --allow "tools/my-tool/**" --workdir "${AO_ROOT}" --log run.log --report report.md -- codex --sandbox danger-full-access exec --skip-git-repo-check "..."`.
