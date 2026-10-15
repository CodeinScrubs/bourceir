@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if not defined BRS_NODE for /d %%D in ("data\runtime\farabi\node-v*-win-*") do if exist "%%D\node.exe" set "BRS_NODE=%%~fD\node.exe"
if not defined BRS_PLAYWRIGHT_MODULE if exist "data\runtime\farabi\node_modules\playwright\index.mjs" (
    set "BRS_PLAYWRIGHT_MODULE=%CD%\data\runtime\farabi\node_modules\playwright\index.mjs"
    set "PLAYWRIGHT_BROWSERS_PATH=%CD%\data\runtime\farabi\browsers"
)
if not defined BRS_NODE if exist "%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe" set "BRS_NODE=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe"
if not defined BRS_NODE set "BRS_NODE=node"
if not defined BRS_PLAYWRIGHT_MODULE if exist "%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\node_modules\playwright\index.mjs" set "BRS_PLAYWRIGHT_MODULE=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\node_modules\playwright\index.mjs"
echo ابزار فقط گزارش می‌خواند. خودتان در مرورگر وارد حساب شوید و سپس Enter بزنید.
echo First time on Windows: run SETUP_FARABI.bat. Guide: docs/farabi-guide.md
"%BRS_NODE%" scripts\farabi_capture.mjs
set "capture_exit=%ERRORLEVEL%"
if not "%capture_exit%"=="0" echo برداشت کامل نشد. خروجی ناقص را وارد نکنید؛ راهنمای فارابی را بخوانید.
pause
exit /b %capture_exit%
