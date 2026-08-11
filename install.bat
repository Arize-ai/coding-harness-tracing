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
REM When set, install from local wheels in this directory instead of fetching the
REM repo: no network and no remote code execution. Mirrors install.sh --wheel-dir.
if not defined ARIZE_WHEEL_DIR set "ARIZE_WHEEL_DIR="
set "WHEEL_DIR=%ARIZE_WHEEL_DIR%"

REM --- Parse arguments ---
set "COMMAND="
set "UNINSTALL_HARNESS="
set "WITH_SKILLS="
set "STATUS_ARGS="
:parse_args
if "%~1"=="" goto :done_args
if /i "%~1"=="-h"        goto :usage
if /i "%~1"=="--help"    goto :usage
if /i "%~1"=="help"      goto :usage
if /i "%~1"=="--with-skills" ( set "WITH_SKILLS=--with-skills" & shift & goto :parse_args )
if /i "%~1"=="--non-interactive" ( set "ARIZE_NONINTERACTIVE=1" & shift & goto :parse_args )
if /i "%~1"=="-y" ( set "ARIZE_NONINTERACTIVE=1" & shift & goto :parse_args )
if /i "%~1"=="--json" ( set "STATUS_ARGS=--json" & shift & goto :parse_args )
if /i "%~1"=="--wheel-dir" (
    set "WHEEL_DIR=%~2"
    if not exist "%~2\" ( echo [arize] --wheel-dir needs a directory; got '%~2' >&2 & exit /b 1 )
    if not exist "%~2\coding_harness_tracing-*.whl" ( echo [arize] No coding_harness_tracing-*.whl in %~2 >&2 & exit /b 1 )
    shift & shift & goto :parse_args
)
if /i "%~1"=="--branch" ( set "INSTALL_BRANCH=%~2" & set "TARBALL_URL=https://github.com/Arize-ai/coding-harness-tracing/archive/refs/heads/%~2.tar.gz" & shift & shift & goto :parse_args )
for %%C in (claude codex copilot cursor gemini kiro opencode omp devin) do if /i "%~1"=="%%C" ( set "COMMAND=%%C" & shift & goto :parse_args )
if /i "%~1"=="update" ( set "COMMAND=update" & shift & goto :parse_args )
if /i "%~1"=="status" ( set "COMMAND=status" & shift & goto :parse_args )
if /i "%~1"=="uninstall" (
    set "COMMAND=uninstall" & shift
    for %%C in (claude codex copilot cursor gemini kiro opencode omp devin) do if /i "%~1"=="%%C" ( set "UNINSTALL_HARNESS=%%C" & shift )
    goto :parse_args
)
echo [arize] Unknown argument: %~1 >&2
goto :usage
:done_args
if "%COMMAND%"=="" ( echo [arize] No command specified >&2 & goto :usage )

REM --- Harness name -> directory mapping ---
REM claude->tracing\claude_code  codex->tracing\codex  copilot->tracing\copilot  cursor->tracing\cursor  gemini->tracing\gemini  kiro->tracing\kiro  opencode->tracing\opencode  omp->tracing\omp  devin->tracing\devin

REM --- Dispatch ---
if "%COMMAND%"=="status"    goto :cmd_status
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
REM Migrate legacy config.yaml -> config.json (no-op if nothing to migrate)
if exist "%VENV_PYTHON%" "%VENV_PYTHON%" -m core.config migrate
echo [arize] Running %COMMAND% install...
call :run_harness_py "%COMMAND%" install "%WITH_SKILLS%"
exit /b %ERRORLEVEL%

REM --- cmd_status ---
:cmd_status
if not exist "%VENV_PYTHON%" ( echo [arize] Venv not found - nothing installed >&2 & exit /b 1 )
"%VENV_PYTHON%" -m core.setup.status %STATUS_ARGS%
exit /b %ERRORLEVEL%

REM --- cmd_update ---
:cmd_update
if not exist "%INSTALL_DIR%" ( echo [arize] Not installed at %INSTALL_DIR% >&2 & exit /b 1 )
call :find_python
if "%FOUND_PYTHON%"=="" ( echo [arize] Error: Python 3.9+ is required >&2 & exit /b 1 )
REM Re-registering runs each harness's installer, which prompts for the project
REM name. With no console to answer on that dies with an EOFError, so fall back
REM to stored values there — and only there, so an interactive update keeps
REM every prompt it has today. cmd has no -t test; ask Python instead.
%FOUND_PYTHON% -c "import sys; sys.exit(0 if sys.stdin.isatty() else 1)" >nul 2>&1 || set "ARIZE_NONINTERACTIVE=1"
set "_UPDATE_NEED_VENV=0"
if not defined WHEEL_DIR if not exist "%INSTALL_DIR%\.git" if not exist "%INSTALL_DIR%\pyproject.toml" (
    echo [arize] This looks like an offline install with no source tree to update. >&2
    echo [arize] Re-run the installer that created it, or pass --wheel-dir with a newer wheel. >&2
    exit /b 1
)
if defined WHEEL_DIR (
    echo [arize] Updating from local wheels in %WHEEL_DIR%...
) else if exist "%INSTALL_DIR%\.git" (
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
    call :pip_install_harness "-U"
    REM Stop here on failure: migrating config and re-registering hooks against
    REM the package that is still installed would report success for an update
    REM that did not happen.
    if !ERRORLEVEL! neq 0 exit /b 1
)
REM Migrate legacy config.yaml -> config.json (no-op if nothing to migrate)
if exist "%VENV_PYTHON%" "%VENV_PYTHON%" -m core.config migrate
if exist "%VENV_PYTHON%" (
    for /f "usebackq delims=" %%H in (`"%VENV_PYTHON%" -c "from core.setup import list_installed_harnesses; [print(h) for h in list_installed_harnesses()]" 2^>nul`) do (
        echo [arize] Reinstalling %%H...
        call :run_harness_py "%%H" install
        if !ERRORLEVEL! neq 0 echo [arize] %%H re-registration failed ^(continuing^) >&2
    )
)
echo [arize] Update complete!
exit /b 0

REM --- cmd_uninstall ---
:cmd_uninstall
if not "%UNINSTALL_HARNESS%"=="" (
    if not exist "%VENV_PYTHON%" ( echo [arize] Venv not found >&2 & exit /b 1 )
    echo [arize] Uninstalling %UNINSTALL_HARNESS%...
    call :run_harness_py "%UNINSTALL_HARNESS%" uninstall
    exit /b !ERRORLEVEL!
)
REM Full wipe
echo [arize] Uninstalling coding-harness-tracing
if exist "%VENV_PYTHON%" (
    for /f "usebackq delims=" %%H in (`"%VENV_PYTHON%" -c "from core.setup import list_installed_harnesses; [print(h) for h in list_installed_harnesses()]" 2^>nul`) do (
        call :run_harness_py "%%H" uninstall
    )
    "%VENV_PYTHON%" -c "from core.setup.wipe import wipe_shared_runtime; wipe_shared_runtime()" 2>nul
)
REM Everything after this point must live on one line. A full uninstall deletes
REM the directory this script is running from, and cmd reads a batch file from
REM disk line by line — so once it is gone there is no next line to read. The
REM uninstall then succeeded while reporting "The system cannot find the path
REM specified" and a non-zero exit. Parsed as one line, cmd never looks back.
if exist "%INSTALL_DIR%" ( rmdir /s /q "%INSTALL_DIR%" 2>nul & echo [arize] Removed %INSTALL_DIR% & echo [arize] Uninstall complete. & exit /b 0 )
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
if defined WHEEL_DIR (
    if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"
    REM `status`, `update` and `uninstall` are documented as running from
    REM %INSTALL_DIR%\install.bat, which repo mode gets via the extract. Skip the
    REM copy when we are already running from there, or it would truncate itself.
    if /i not "%~f0"=="%INSTALL_DIR%\install.bat" copy /y "%~f0" "%INSTALL_DIR%\install.bat" >nul
    goto :eof
)
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
call :pip_install_harness ""
if !ERRORLEVEL! neq 0 exit /b 1
echo [arize] Venv ready at %VENV_DIR%
goto :eof

REM --- pip_install_harness: install the package into the venv ---
REM Extra args are passed through to pip (-U for update).
REM
REM Shared so the two callers cannot drift: setup_venv checked ERRORLEVEL and
REM cmd_update did not, so a failed offline reinstall let update carry on to
REM migrate config and re-register every harness against the OLD package, then
REM print "Update complete!" and exit 0. install.sh has always checked this.
REM
REM Wheel mode deliberately does not send stderr to nul — pip's "No matching
REM distribution found" is the only thing that explains the failure, and matching
REM install.sh means a Windows user sees it too.
:pip_install_harness
if defined WHEEL_DIR (
    "%VENV_PIP%" install --quiet %~1 --no-index --find-links "%WHEEL_DIR%" coding-harness-tracing
    if !ERRORLEVEL! neq 0 ( echo [arize] Failed to install coding-harness-tracing from %WHEEL_DIR% >&2 & exit /b 1 )
) else (
    "%VENV_PIP%" install --quiet %~1 "%INSTALL_DIR%" >nul 2>&1
    if !ERRORLEVEL! neq 0 ( echo [arize] Failed to install coding-harness-tracing package >&2 & exit /b 1 )
)
exit /b 0

REM --- run_harness_py: invoke a harness installer ---
REM %1 harness key, %2 verb (install/uninstall), %3 optional flags.
REM Repo mode runs install.py from the source tree; wheel mode has no source tree
REM and runs the same code as a module. Both import core.* from site-packages.
:run_harness_py
call :resolve_dir "%~1"
if !ERRORLEVEL! neq 0 exit /b 1
set "_PY=%INSTALL_DIR%\!HARNESS_DIR!\install.py"
set "_MOD=!HARNESS_DIR:\=.!.install"
if exist "!_PY!" (
    "%VENV_PYTHON%" "!_PY!" %~2 %~3
) else (
    "%VENV_PYTHON%" -m "!_MOD!" %~2 %~3
)
exit /b !ERRORLEVEL!

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
if /i "%~1"=="opencode"    set "HARNESS_DIR=tracing\opencode"
if /i "%~1"=="omp"         set "HARNESS_DIR=tracing\omp"
if /i "%~1"=="devin"       set "HARNESS_DIR=tracing\devin"
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
echo     opencode            Install tracing for opencode
echo     omp                 Install tracing for Oh My Pi (omp)
echo     devin               Install tracing for Devin CLI
echo     status              Report configured harnesses and hook wiring
echo     update              Update to latest and reinstall all harnesses
echo     uninstall [harness] Remove one harness or full wipe
echo.
echo   Flags:
echo     --with-skills   Symlink harness skills into .agents\skills\
echo     --wheel-dir DIR Install from local wheels in DIR instead of downloading
echo                     the repo. No network and no remote code execution; also
echo                     settable as ARIZE_WHEEL_DIR.
echo     --branch NAME   Install from a specific git branch (default: main)
echo     --json          With status: emit machine-readable JSON
echo     --non-interactive, -y  Ask nothing; read values from the environment
echo                     or the file named by ARIZE_ENV_FILE. Missing required
echo                     values are an error.
echo.
echo   Non-interactive install reads the environment, plus a dotenv file named
echo   with ARIZE_ENV_FILE — no automatic .env search: ARIZE_API_KEY and ARIZE_SPACE_ID for Arize AX,
echo   PHOENIX_ENDPOINT and PHOENIX_API_KEY for Phoenix, plus optional
echo   ARIZE_BACKEND, ARIZE_PROJECT_NAME, ARIZE_USER_ID, ARIZE_OTLP_ENDPOINT,
echo   ARIZE_LOG_PROMPTS, ARIZE_LOG_TOOL_DETAILS, ARIZE_LOG_TOOL_CONTENT — all
echo   off unless set to true.
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
