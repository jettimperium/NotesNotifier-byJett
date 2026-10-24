@echo off
title Prack Notes Bot
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Создаю виртуальное окружение...
    where py >nul 2>&1
    if errorlevel 1 (
        python -m venv .venv
    ) else (
        py -m venv .venv
    )
    if errorlevel 1 (
        echo Не удалось создать .venv. Проверьте, что Python установлен.
        pause
        exit /b 1
    )
)

call ".venv\Scripts\activate.bat"

python -c "import discord, dotenv; from zoneinfo import ZoneInfo; ZoneInfo('Europe/Moscow')" >nul 2>&1
if errorlevel 1 (
    echo Устанавливаю зависимости...
    python -m pip install --upgrade pip
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo Не удалось установить зависимости.
        pause
        exit /b 1
    )
)

if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo.
    echo Создан файл .env.
    echo Откройте его блокнотом и вставьте токен бота в DISCORD_TOKEN=...
    echo После этого запустите start.bat снова.
    pause
    exit /b 1
)

echo.
echo Запускаю бота. Не закрывайте это окно, пока бот работает.
echo Остановить бота: Ctrl+C. Второе окно не запускайте.
echo.
python bot.py
echo.
echo Бот остановлен.
pause
