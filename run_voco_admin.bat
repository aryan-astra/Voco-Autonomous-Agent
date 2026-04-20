@echo off
setlocal EnableExtensions DisableDelayedExpansion

set "VOCO_PRINT_CONFIG=0"
for %%A in (%*) do (
    if /i "%%~A"=="--print-config" set "VOCO_PRINT_CONFIG=1"
)

if "%VOCO_PRINT_CONFIG%"=="0" (
    net session >nul 2>&1
    if errorlevel 1 (
        echo [VOCO][INFO] Requesting administrator privileges...
        powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -ArgumentList '%*' -Verb RunAs"
        if errorlevel 1 (
            echo [VOCO][ERROR] Unable to relaunch with administrator privileges.
            exit /b 1
        )
        exit /b 0
    )
)

cd /d "%~dp0" || (
    echo [VOCO][ERROR] Failed to switch to script directory: %~dp0
    exit /b 1
)

set "VOCO_POLICY_DRIVE=O:"
if not exist "%VOCO_POLICY_DRIVE%\" (
    echo [VOCO][WARN] O: drive not found. Falling back to %~d0 for runtime caches.
    set "VOCO_POLICY_DRIVE=%~d0"
)

set "VOCO_RUNTIME_ROOT=%VOCO_POLICY_DRIVE%\voco-runtime"
set "OLLAMA_MODELS=%VOCO_RUNTIME_ROOT%\ollama\models"
set "TEMP=%VOCO_RUNTIME_ROOT%\temp"
set "TMP=%TEMP%"
set "PIP_CACHE_DIR=%VOCO_RUNTIME_ROOT%\pip\cache"
set "HF_HOME=%VOCO_RUNTIME_ROOT%\huggingface"
set "HF_HUB_CACHE=%HF_HOME%\hub"
set "TRANSFORMERS_CACHE=%HF_HOME%\transformers"
set "OPENWAKEWORD_MODEL_DIR=%VOCO_RUNTIME_ROOT%\openwakeword\models"

call :print_runtime_policy
if "%VOCO_PRINT_CONFIG%"=="1" (
    echo [VOCO][INFO] --print-config requested. Exiting without startup actions.
    exit /b 0
)

call :ensure_runtime_directories
if errorlevel 1 exit /b 1

where ollama >nul 2>&1
if errorlevel 1 (
    echo [VOCO][ERROR] Ollama CLI was not found in PATH.
    echo [VOCO][ERROR] Install Ollama or ensure its install path is available before startup.
    exit /b 1
)

call :ensure_ollama_ready
if errorlevel 1 exit /b 1

call :ensure_qwen3_model
if errorlevel 1 exit /b 1

call :launch_voco_ui
set "VOCO_EXIT_CODE=%ERRORLEVEL%"
if not "%VOCO_EXIT_CODE%"=="0" (
    echo [VOCO][ERROR] VOCO UI exited with code %VOCO_EXIT_CODE%.
)
exit /b %VOCO_EXIT_CODE%

:print_runtime_policy
echo [VOCO] Runtime storage policy active:
echo [VOCO]   OLLAMA_MODELS=%OLLAMA_MODELS%
echo [VOCO]   TEMP=%TEMP%
echo [VOCO]   TMP=%TMP%
echo [VOCO]   PIP_CACHE_DIR=%PIP_CACHE_DIR%
echo [VOCO]   HF_HOME=%HF_HOME%
echo [VOCO]   HF_HUB_CACHE=%HF_HUB_CACHE%
echo [VOCO]   TRANSFORMERS_CACHE=%TRANSFORMERS_CACHE%
echo [VOCO]   OPENWAKEWORD_MODEL_DIR=%OPENWAKEWORD_MODEL_DIR%
exit /b 0

:ensure_runtime_directories
echo [VOCO][STEP] Ensuring runtime directories exist...
for %%D in ("%VOCO_RUNTIME_ROOT%" "%OLLAMA_MODELS%" "%TEMP%" "%PIP_CACHE_DIR%" "%HF_HOME%" "%HF_HUB_CACHE%" "%TRANSFORMERS_CACHE%" "%OPENWAKEWORD_MODEL_DIR%") do (
    if not exist "%%~fD" (
        mkdir "%%~fD" >nul 2>&1
        if errorlevel 1 (
            echo [VOCO][ERROR] Failed to create runtime directory: %%~fD
            exit /b 1
        )
    )
)
echo [VOCO][INFO] Runtime directories are ready.
exit /b 0

:ensure_ollama_ready
echo [VOCO][STEP] Checking Ollama availability...
ollama list >nul 2>&1
if not errorlevel 1 (
    echo [VOCO][INFO] Ollama is already responding.
    exit /b 0
)

echo [VOCO][WARN] Ollama is not responding. Attempting to start it...
set "VOCO_OLLAMA_SERVICE="
for %%S in (Ollama ollama) do (
    sc query "%%~S" >nul 2>&1
    if not errorlevel 1 set "VOCO_OLLAMA_SERVICE=%%~S"
)

if defined VOCO_OLLAMA_SERVICE (
    echo [VOCO][INFO] Starting Windows service "%VOCO_OLLAMA_SERVICE%"...
    sc start "%VOCO_OLLAMA_SERVICE%" >nul 2>&1
) else (
    echo [VOCO][INFO] Ollama service not found. Starting "ollama serve" in background...
    start "VOCO Ollama Serve" /min cmd /c "ollama serve"
)

set "VOCO_OLLAMA_WAIT=0"
:wait_for_ollama
ollama list >nul 2>&1
if not errorlevel 1 (
    echo [VOCO][INFO] Ollama is ready.
    exit /b 0
)

set /a VOCO_OLLAMA_WAIT+=1
if %VOCO_OLLAMA_WAIT% GEQ 15 (
    echo [VOCO][ERROR] Ollama did not become ready in time.
    echo [VOCO][ERROR] Start it manually with: ollama serve
    exit /b 1
)

timeout /t 2 /nobreak >nul
goto :wait_for_ollama

:ensure_qwen3_model
echo [VOCO][STEP] Ensuring Ollama model "qwen3:4b" exists...
ollama show qwen3:4b >nul 2>&1
if not errorlevel 1 (
    echo [VOCO][INFO] Model "qwen3:4b" already exists.
    exit /b 0
)

echo [VOCO][INFO] Pulling model "qwen3:4b"...
ollama pull qwen3:4b
if errorlevel 1 (
    echo [VOCO][ERROR] Failed to pull model "qwen3:4b".
    exit /b 1
)

echo [VOCO][INFO] Model "qwen3:4b" is ready.
exit /b 0

:launch_voco_ui
echo [VOCO][STEP] Launching VOCO UI...
if exist ".venv\Scripts\activate.bat" (
    echo [VOCO][INFO] Activating .venv environment...
    call ".venv\Scripts\activate.bat"
    if errorlevel 1 (
        echo [VOCO][ERROR] Failed to activate .venv.
        exit /b 1
    )
) else (
    echo [VOCO][WARN] .venv not found. Continuing with system Python.
)

python voco_ui.py
exit /b %ERRORLEVEL%
