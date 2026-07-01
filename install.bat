@echo off
setlocal enabledelayedexpansion
REM Arize Coding Harness Tracing — Windows installer router
REM
REM Usage:
REM   install.bat <harness> [--with-skills] [--branch NAME]
REM   install.bat uninstall [harness]
REM   install.bat update

REM --- Constants ---
set "REPO_URL=https://github.com/Arize-ai/coding-harness-tracing.git"
if not defined ARIZE_INSTALL_BRANCH set "ARIZE_INSTALL_BRANCH=main"
set "INSTALL_BRANCH=%ARIZE_INSTALL_BRANCH%"
set "TARBALL_URL=https://github.com/Arize-ai/coding-harness-tracing/archive/refs/heads/%INSTALL_BRANCH%.tar.gz"
set "INSTALL_DIR=%USERPROFILE%\.arize\harness"
set "VENV_DIR=%INSTALL_DIR%\venv"
set "VENV_PYTHON=%VENV_DIR%\Scripts\python.exe"
set "VENV_PIP=%VENV_DIR%\Scripts\pip.exe"

REM --- Parse arguments ---
set "COMMAND="
set "UNINSTALL_HARNESS="
set "WITH_SKILLS="
:parse_args
if "%~1"=="" goto :done_args
if /i "%~1"=="-h"        goto :usage
if /i "%~1"=="--help"    goto :usage
if /i "%~1"=="help"      goto :usage
if /i "%~1"=="--with-skills" ( set "WITH_SKILLS=--with-skills" & shift & goto :parse_args )
if /i "%~1"=="--branch" ( set "INSTALL_BRANCH=%~2" & set "TARBALL_URL=https://github.com/Arize-ai/coding-harness-tracing/archive/refs/heads/%~2.tar.gz" & shift & shift & goto :parse_args )
for %%C in (claude codex copilot cursor gemini kiro antigravity opencode) do if /i "%~1"=="%%C" ( set "COMMAND=%%C" & shift & goto :parse_args )
if /i "%~1"=="update" ( set "COMMAND=update" & shift & goto :parse_args )
if /i "%~1"=="uninstall" (
    set "COMMAND=uninstall" & shift
    for %%C in (claude codex copilot cursor gemini kiro antigravity opencode) do if /i "%~1"=="%%C" ( set "UNINSTALL_HARNESS=%%C" & shift )
    goto :parse_args
)
echo [arize] Unknown argument: %~1 >&2
goto :usage
:done_args
if "%COMMAND%"=="" ( echo [arize] No command specified >&2 & goto :usage )

REM --- Harness name -> directory mapping ---
REM claude->tracing\claude_code  codex->tracing\codex  copilot->tracing\copilot  cursor->tracing\cursor  gemini->tracing\gemini  kiro->tracing\kiro  antigravity->tracing\antigravity  opencode->tracing\opencode

REM --- Dispatch ---
if "%COMMAND%"=="update"    goto :cmd_update
if "%COMMAND%"=="uninstall" goto :cmd_uninstall

REM --- Install a harness ---
call :find_python
if "%FOUND_PYTHON%"=="" ( echo [arize] Error: Python 3.9+ is required >&2 & exit /b 1 )
echo [arize] Found Python: %FOUND_PYTHON%
call :bootstrap_repo
if %ERRORLEVEL% neq 0 exit /b 1
call :setup_venv
if %ERRORLEVEL% neq 0 exit /b 1
call :resolve_dir "%COMMAND%"
set "_PY=%INSTALL_DIR%\%HARNESS_DIR%\install.py"
if not exist "%_PY%" ( echo [arize] install.py not found at %_PY% >&2 & exit /b 1 )
echo [arize] Running %COMMAND% install...
"%VENV_PYTHON%" "%_PY%" install %WITH_SKILLS%
exit /b %ERRORLEVEL%

REM --- cmd_update ---
:cmd_update
if not exist "%INSTALL_DIR%" ( echo [arize] Not installed at %INSTALL_DIR% >&2 & exit /b 1 )
call :find_python
if "%FOUND_PYTHON%"=="" ( echo [arize] Error: Python 3.9+ is required >&2 & exit /b 1 )
set "_UPDATE_NEED_VENV=0"
if exist "%INSTALL_DIR%\.git" (
    echo [arize] Pulling latest changes...
    git -C "%INSTALL_DIR%" pull --ff-only >nul 2>&1
    if !ERRORLEVEL! neq 0 (
        echo [arize] Pull failed — re-cloning
        rmdir /s /q "%INSTALL_DIR%" 2>nul
        call :bootstrap_repo
        if !ERRORLEVEL! neq 0 exit /b 1
        set "_UPDATE_NEED_VENV=1"
    )
) else (
    rmdir /s /q "%INSTALL_DIR%" 2>nul
    call :bootstrap_repo
    if !ERRORLEVEL! neq 0 exit /b 1
    set "_UPDATE_NEED_VENV=1"
)
REM Re-create venv if it was wiped along with INSTALL_DIR
if "!_UPDATE_NEED_VENV!"=="1" (
    call :setup_venv
    if !ERRORLEVEL! neq 0 exit /b 1
) else if exist "%VENV_PIP%" (
    echo [arize] Reinstalling package...
    "%VENV_PIP%" install --quiet "%INSTALL_DIR%" >nul 2>&1
)
if exist "%VENV_PYTHON%" (
    for /f "usebackq delims=" %%H in (`"%VENV_PYTHON%" -c "from core.setup import list_installed_harnesses; [print(h) for h in list_installed_harnesses()]" 2^>nul`) do (
        call :resolve_dir "%%H"
        if exist "%INSTALL_DIR%\!HARNESS_DIR!\install.py" ( echo [arize] Reinstalling %%H... & "%VENV_PYTHON%" "%INSTALL_DIR%\!HARNESS_DIR!\install.py" install )
    )
)
echo [arize] Update complete!
exit /b 0

REM --- cmd_uninstall ---
:cmd_uninstall
if not "%UNINSTALL_HARNESS%"=="" (
    if not exist "%VENV_PYTHON%" ( echo [arize] Venv not found >&2 & exit /b 1 )
    call :resolve_dir "%UNINSTALL_HARNESS%"
    set "_PY=%INSTALL_DIR%\!HARNESS_DIR!\install.py"
    if not exist "!_PY!" ( echo [arize] install.py not found >&2 & exit /b 1 )
    echo [arize] Uninstalling %UNINSTALL_HARNESS%...
    "%VENV_PYTHON%" "!_PY!" uninstall
    exit /b !ERRORLEVEL!
)
REM Full wipe
echo [arize] Uninstalling coding-harness-tracing
if exist "%VENV_PYTHON%" (
    for /f "usebackq delims=" %%H in (`"%VENV_PYTHON%" -c "from core.setup import list_installed_harnesses; [print(h) for h in list_installed_harnesses()]" 2^>nul`) do (
        call :resolve_dir "%%H"
        if exist "%INSTALL_DIR%\!HARNESS_DIR!\install.py" ( "%VENV_PYTHON%" "%INSTALL_DIR%\!HARNESS_DIR!\install.py" uninstall )
    )
    "%VENV_PYTHON%" -c "from core.setup.wipe import wipe_shared_runtime; wipe_shared_runtime()" 2>nul
)
if exist "%INSTALL_DIR%" ( rmdir /s /q "%INSTALL_DIR%" 2>nul & echo [arize] Removed %INSTALL_DIR% )
echo [arize] Uninstall complete.
exit /b 0

REM ===================================================================
REM  Helpers
REM ===================================================================

REM --- find_python: locate Python >= 3.9 (try py -3, python3, python, then known paths) ---
:find_python
set "FOUND_PYTHON="
REM Try py -3 first (Windows Python Launcher — ensures Python 3)
where py >nul 2>&1 && ( py -3 -c "import sys; assert sys.version_info >= (3, 9)" >nul 2>&1 && ( set "FOUND_PYTHON=py -3" & goto :eof ) )
REM Then python3 and python on PATH
for %%P in (python3 python) do (
    where %%P >nul 2>&1 && ( %%P -c "import sys; assert sys.version_info >= (3, 9)" >nul 2>&1 && ( set "FOUND_PYTHON=%%P" & goto :eof ) )
)
for %%V in (313 312 311 310 39) do (
    for %%D in ("%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" "C:\Python%%V\python.exe") do (
        if exist %%~D ( %%~D -c "import sys; assert sys.version_info >= (3, 9)" >nul 2>&1 && ( set "FOUND_PYTHON=%%~D" & goto :eof ) )
    )
)
goto :eof

REM --- bootstrap_repo: clone or tarball into INSTALL_DIR ---
:bootstrap_repo
if exist "%INSTALL_DIR%\.git" (
    echo [arize] Repository at %INSTALL_DIR%, syncing...
    git -C "%INSTALL_DIR%" fetch --depth 1 origin "%INSTALL_BRANCH%" >nul 2>&1 && git -C "%INSTALL_DIR%" checkout -B "%INSTALL_BRANCH%" FETCH_HEAD >nul 2>&1 && goto :eof
    git -C "%INSTALL_DIR%" pull --ff-only >nul 2>&1 && goto :eof
    echo [arize] git update failed — re-cloning
    rmdir /s /q "%INSTALL_DIR%" 2>nul
)
if exist "%INSTALL_DIR%" if not exist "%INSTALL_DIR%\.git" ( rmdir /s /q "%INSTALL_DIR%" 2>nul )
where git >nul 2>&1 && (
    echo [arize] Cloning coding-harness-tracing...
    git clone --depth 1 --branch "%INSTALL_BRANCH%" "%REPO_URL%" "%INSTALL_DIR%" >nul 2>&1 && goto :eof
    echo [arize] git clone failed — falling back to tarball
)
call :download_tarball
goto :eof

REM --- download_tarball ---
:download_tarball
echo [arize] Downloading tarball...
set "TMPZIP=%TEMP%\arize-install-%RANDOM%.tar.gz"
powershell -NoProfile -Command "Invoke-WebRequest -Uri '%TARBALL_URL%' -OutFile '%TMPZIP%'" >nul 2>&1
if !ERRORLEVEL! neq 0 ( curl -sSfL "%TARBALL_URL%" -o "%TMPZIP%" 2>nul || ( echo [arize] Download failed >&2 & exit /b 1 ) )
if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"
tar xzf "%TMPZIP%" --strip-components=1 -C "%INSTALL_DIR%" >nul 2>&1
if !ERRORLEVEL! neq 0 (
    set "TMPDIR=%TEMP%\arize-extract-%RANDOM%"
    mkdir "!TMPDIR!" 2>nul
    powershell -NoProfile -Command "& { $gz=[IO.File]::OpenRead('%TMPZIP%'); $d=New-Object IO.Compression.GZipStream($gz,[IO.Compression.CompressionMode]::Decompress); $f=[IO.File]::Create('!TMPDIR!\a.tar'); $d.CopyTo($f); $f.Close(); $d.Close(); $gz.Close() }" >nul 2>&1
    tar xf "!TMPDIR!\a.tar" --strip-components=1 -C "%INSTALL_DIR%" >nul 2>&1
    if !ERRORLEVEL! neq 0 ( rmdir /s /q "!TMPDIR!" 2>nul & del "%TMPZIP%" 2>nul & echo [arize] Extraction failed >&2 & exit /b 1 )
    rmdir /s /q "!TMPDIR!" 2>nul
)
del "%TMPZIP%" 2>nul
echo [arize] Extracted to %INSTALL_DIR%
goto :eof

REM --- setup_venv ---
:setup_venv
if exist "%VENV_PYTHON%" ( "%VENV_PYTHON%" -c "import core" >nul 2>&1 && ( echo [arize] Venv ready & goto :eof ) )
echo [arize] Creating venv...
%FOUND_PYTHON% -m venv "%VENV_DIR%" >nul 2>&1
if !ERRORLEVEL! neq 0 ( echo [arize] Failed to create venv >&2 & exit /b 1 )
if not exist "%VENV_PIP%" ( echo [arize] pip not found in venv >&2 & exit /b 1 )
echo [arize] Installing coding-harness-tracing...
"%VENV_PIP%" install --quiet "%INSTALL_DIR%" >nul 2>&1
if !ERRORLEVEL! neq 0 ( echo [arize] pip install failed >&2 & exit /b 1 )
echo [arize] Venv ready at %VENV_DIR%
goto :eof

REM --- resolve_dir: map command/harness name to directory ---
:resolve_dir
set "HARNESS_DIR="
if /i "%~1"=="claude"      set "HARNESS_DIR=tracing\claude_code"
if /i "%~1"=="claude-code" set "HARNESS_DIR=tracing\claude_code"
if /i "%~1"=="codex"       set "HARNESS_DIR=tracing\codex"
if /i "%~1"=="copilot"     set "HARNESS_DIR=tracing\copilot"
if /i "%~1"=="cursor"      set "HARNESS_DIR=tracing\cursor"
if /i "%~1"=="gemini"      set "HARNESS_DIR=tracing\gemini"
if /i "%~1"=="kiro"        set "HARNESS_DIR=tracing\kiro"
if /i "%~1"=="antigravity" set "HARNESS_DIR=tracing\antigravity"
if /i "%~1"=="opencode"    set "HARNESS_DIR=tracing\opencode"
if "%HARNESS_DIR%"=="" ( echo [arize] Unknown harness: %~1 >&2 & exit /b 1 )
goto :eof

REM --- Usage ---
:usage
echo.
echo   Arize Coding Harness Tracing Installer
echo.
echo   Usage: install.bat ^<command^> [flags]
echo.
echo   Commands:
echo     claude              Install tracing for Claude Code / Agent SDK
echo     codex               Install tracing for OpenAI Codex CLI
echo     copilot             Install tracing for GitHub Copilot
echo     cursor              Install tracing for Cursor IDE
echo     gemini              Install tracing for Gemini CLI
echo     kiro                Install tracing for Kiro CLI
echo     antigravity         Install tracing for Google Antigravity CLI/IDE
echo     opencode            Install tracing for opencode
echo     update              Update to latest and reinstall all harnesses
echo     uninstall [harness] Remove one harness or full wipe
echo.
echo   Flags:
echo     --with-skills   Symlink harness skills into .agents\skills\
echo     --branch NAME   Install from a specific git branch (default: main)
echo.
echo   Examples:
echo     install.bat claude
echo     install.bat codex --with-skills
echo     install.bat cursor --branch dev
echo     install.bat uninstall claude
echo     install.bat uninstall
echo     install.bat update
echo.
exit /b 1
