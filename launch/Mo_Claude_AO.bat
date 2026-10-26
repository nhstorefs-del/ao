@echo off
setlocal
for %%I in ("%~dp0..") do set "AO_ROOT=%%~fI"
cd /d "%AO_ROOT%"
where claude >nul 2>&1
if errorlevel 1 (
 echo [LOI] Khong co claude CLI trong PATH. Day la thanh phan tuy chon.
 exit /b 1
)
call claude %*
exit /b %ERRORLEVEL%
