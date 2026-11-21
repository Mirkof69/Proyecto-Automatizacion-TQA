@echo off
rem Centro de Pruebas QA en internet: arranca el panel (127.0.0.1:8090) si no esta y abre un tunel de Cloudflare
rem propio, distinto del tunel del sistema (PUBLICAR_WEB.bat). Usuario y clave: RUNNER_USER / RUNNER_PASSWORD de .env
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\publicar_panel.ps1"
pause
