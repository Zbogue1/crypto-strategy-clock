@echo off
title Crypto Strategy Clock - Running...
cd /d "C:\Users\Zachg\Claude\Projects\Investment Strategy Clock"

if "%ANTHROPIC_API_KEY%"=="" (
    echo.
    echo   ==========================================
    echo    API Key Setup  ^(one time only^)
    echo   ==========================================
    echo.
    echo   Get your key at: console.anthropic.com
    echo   API Keys section -- starts with sk-ant-
    echo.
    set /p ANTHROPIC_API_KEY=  Paste your API key and press Enter:
    setx ANTHROPIC_API_KEY "%ANTHROPIC_API_KEY%" >nul
    echo.
    echo   Key saved permanently. Won't ask again.
    echo.
)

echo.
echo   ==========================================
echo    Crypto Strategy Clock - Scanning Market
echo   ==========================================
echo.
python crypto_oracle_v3.py --once > agent_run.log 2>&1
type agent_run.log
echo.
echo   ==========================================
echo   Log saved to agent_run.log
echo   ==========================================
echo.
pause
