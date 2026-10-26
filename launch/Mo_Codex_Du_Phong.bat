@echo off
chcp 65001 >nul
setlocal EnableExtensions

rem ============================================================
rem  Mo_Codex_Du_Phong.bat  -  Mo Codex lam dieu phoi vien DU PHONG
rem  Dung khi Claude (dieu phoi vien chinh) khong dung duoc.
rem
rem  Cach dung:
rem    Mo_Codex_Du_Phong.bat           -> mo Codex voi TAI KHOAN CHINH
rem    Mo_Codex_Du_Phong.bat 2         -> mo Codex voi TAI KHOAN THU 2
rem    Mo_Codex_Du_Phong.bat --check   -> chi kiem tra + in phien ban roi thoat
rem    Mo_Codex_Du_Phong.bat 2 --check
rem
rem  File nay KHONG chua khoa/token. Khong tu dang bai, khong tu gui tin.
rem ============================================================

for %%I in ("%~dp0..") do set "REPO=%%~fI"
set "AO_ROOT=%REPO%"
set "PROMPT=Bạn là coordinator dự phòng, Claude không dùng được. Đọc AGENTS.md, làm các lệnh mở đầu, rồi báo cho tôi bằng tiếng Việt việc nào đang dở và việc nào đang chờ tôi duyệt. Chưa làm gì khác."

rem --- Doc tham so: "2" = tai khoan thu 2 ; "--check" = chi kiem tra ---
set "MODE=1"
set "CHECK="
if /I "%~1"=="2" set "MODE=2"
if /I "%~1"=="--check" set "CHECK=1"
if /I "%~2"=="--check" set "CHECK=1"

rem --- Phai vao dung thu muc goc repo truoc khi mo Codex ---
if not exist "%REPO%\" (
  echo [LOI] Khong tim thay thu muc "%REPO%".
  echo       Kiem tra lai o dia/duong dan roi thu lai.
  pause
  exit /b 1
)
cd /d "%REPO%"

rem --- Tim duong dan Codex theo tung tai khoan ---
set "CODEX_EXE="
if "%MODE%"=="2" goto :mode2

rem ===== TAI KHOAN CHINH =====
set "MODEL=gpt-6.1-sol"
set "ACCOUNT_LABEL=tai khoan chinh"
rem Tu tim ban Codex moi nhat trong %LOCALAPPDATA%\OpenAI\Codex\bin\*\codex.exe
rem (lay file codex.exe duoc sua gan nhat). Khong co thi bao loi ro, khong doan.
if defined AO_CODEX_EXE set "CODEX_EXE=%AO_CODEX_EXE%"
if not defined CODEX_EXE for /f "usebackq delims=" %%F in (`powershell -NoProfile -Command "Get-ChildItem -Path $env:LOCALAPPDATA\OpenAI\Codex\bin\*\codex.exe -File -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1 -ExpandProperty FullName"`) do set "CODEX_EXE=%%F"
if not defined CODEX_EXE for /f "delims=" %%F in ('where codex 2^>nul') do set "CODEX_EXE=%%F"
if not defined CODEX_EXE (
  echo [LOI] Khong tim thay Codex trong "%LOCALAPPDATA%\OpenAI\Codex\bin\*\codex.exe".
  echo       Hay cai/mo lai Codex roi thu lai. Khong doan bua.
  pause
  exit /b 1
)
goto :found

rem ===== TAI KHOAN THU 2 =====
:mode2
set "MODEL=gpt-6-sol"
set "ACCOUNT_LABEL=tai khoan thu 2"
if not defined AO_CODEX2_HOME (
  echo [LOI] Tai khoan 2 chua cau hinh AO_CODEX2_HOME.
  exit /b 1
)
set "CODEX_HOME=%AO_CODEX2_HOME%"
set "CODEX_EXE=%LOCALAPPDATA%\Programs\OpenAI\Codex\bin\codex.exe"
if defined AO_CODEX_EXE set "CODEX_EXE=%AO_CODEX_EXE%"
if not exist "%CODEX_EXE%" for /f "delims=" %%F in ('where codex 2^>nul') do set "CODEX_EXE=%%F"
if not exist "%CODEX_EXE%" (
  echo [LOI] Khong tim thay Codex cho tai khoan thu 2 tai:
  echo       "%CODEX_EXE%"
  echo       Hay cai/mo lai Codex roi thu lai.
  pause
  exit /b 1
)

:found
rem --- Che do --check: chi in duong dan + phien ban, khong mo phien ---
if defined CHECK goto :do_check

rem --- Mo Codex TUONG TAC ---
echo ============================================================
echo  MO CODEX DU PHONG  (%ACCOUNT_LABEL%)
echo  Thu muc lam viec: %REPO%
echo  Codex: %CODEX_EXE%
echo  Model: %MODEL%
echo ============================================================
echo.
rem Codex tu choi chay trong cua so quyen Administrator (loi thuc te da gap 03/10).
rem Neu dang chay quyen cao thi them --no-daemon theo goi y cua chinh Codex.
set "NODAEMON="
net session >nul 2>&1
if not errorlevel 1 set "NODAEMON=--no-daemon"
"%CODEX_EXE%" %NODAEMON% --sandbox danger-full-access -m %MODEL% "%PROMPT%"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo [LOI] Codex thoat voi ma loi %RC%. Xem thong bao phia tren.
  echo       Neu la loi het han muc: dong cua so nay roi chay lai voi tai khoan 2:
  echo       Mo_Codex_Du_Phong.bat 2
  pause
)
exit /b %RC%

:do_check
echo Duong dan Codex: %CODEX_EXE%
echo Tai khoan: %ACCOUNT_LABEL% - Model: %MODEL%
"%CODEX_EXE%" --version
if errorlevel 1 (
  echo [LOI] Codex chay duoc nhung khong tra ve phien ban.
  exit /b 1
)
echo (Che do --check: chi kiem tra, khong mo phien. Xong.)
exit /b 0
