# Publica el Centro de Pruebas QA con un tunel de Cloudflare propio.
# No toca el tunel del sistema: usa una copia de cloudflared en %LOCALAPPDATA%\fetal-medical\tunel_qa
# y solo detiene los procesos de esa carpeta.
$ErrorActionPreference = "Stop"
$Raiz = (Get-Item $PSScriptRoot).Parent.FullName
$Py = Join-Path $Raiz ".venv\Scripts\python.exe"
$Dir = Join-Path $env:LOCALAPPDATA "fetal-medical\tunel_qa"
$Cf = Join-Path $Dir "cloudflared.exe"
$Log = Join-Path $Dir "tunel.log"
New-Item -ItemType Directory -Force -Path $Dir | Out-Null

function Responde([string]$Url) {
    try { return (Invoke-WebRequest $Url -UseBasicParsing -TimeoutSec 3).StatusCode -eq 200 } catch { return $false }
}

Write-Host "[1/3] Panel de pruebas en http://127.0.0.1:8090 ..." -ForegroundColor White
if (-not (Responde "http://127.0.0.1:8090/ingresar")) {
    Start-Process -FilePath $Py -ArgumentList "-m", "uvicorn", "runner_web.app:app", "--host", "127.0.0.1", "--port", "8090",
        "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1" -WorkingDirectory $Raiz `
        -RedirectStandardOutput (Join-Path $Raiz "reports\panel.log") -RedirectStandardError (Join-Path $Raiz "reports\panel_err.log") -WindowStyle Hidden
    for ($i = 0; $i -lt 30 -and -not (Responde "http://127.0.0.1:8090/ingresar"); $i++) { Start-Sleep -Seconds 1 }
}
if (-not (Responde "http://127.0.0.1:8090/ingresar")) { Write-Host "ERROR: el panel no arranco. Revise reports\panel_err.log" -ForegroundColor Red; exit 1 }
Write-Host "    OK panel en linea" -ForegroundColor Green

Write-Host "[2/3] cloudflared propio del panel ..." -ForegroundColor White
if (-not (Test-Path $Cf)) {
    $DelSistema = Join-Path (Get-Item $Raiz).Parent.Parent.FullName "infraestructura\tunel\cloudflared.exe"
    if (Test-Path $DelSistema) { Copy-Item $DelSistema $Cf }
    else { Invoke-WebRequest "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe" -OutFile $Cf }
}
Get-Process -Name cloudflared -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "$Dir*" } | Stop-Process -Force
if (Test-Path $Log) { Remove-Item $Log -Force }
Start-Process -FilePath $Cf -ArgumentList "tunnel", "--no-autoupdate", "--url", "http://127.0.0.1:8090" `
    -RedirectStandardError $Log -RedirectStandardOutput (Join-Path $Dir "tunel_out.log") -WindowStyle Hidden

Write-Host "[3/3] Pidiendo la direccion publica ..." -ForegroundColor White
$Url = ""
for ($i = 0; $i -lt 45 -and -not $Url; $i++) {
    Start-Sleep -Seconds 1
    if (Test-Path $Log) {
        $m = Select-String -Path $Log -Pattern "https://[a-z0-9-]+\.trycloudflare\.com" | Select-Object -First 1
        if ($m) { $Url = $m.Matches[0].Value }
    }
}
if (-not $Url) { Write-Host "ERROR: Cloudflare no entrego una direccion. Revise $Log" -ForegroundColor Red; exit 1 }
try { Set-Clipboard -Value $Url } catch {}
Write-Host ""
Write-Host "================================================================" -ForegroundColor Green
Write-Host "  CENTRO DE PRUEBAS QA EN LINEA (copiado al portapapeles):" -ForegroundColor Green
Write-Host "  $Url" -ForegroundColor Cyan
Write-Host "  Usuario y clave: RUNNER_USER y RUNNER_PASSWORD del archivo .env" -ForegroundColor Yellow
Write-Host "  El tunel sigue abierto aunque cierre esta ventana; para cerrarlo:" -ForegroundColor Yellow
Write-Host "  Administrador de tareas > cloudflared (carpeta tunel_qa)." -ForegroundColor Yellow
Write-Host "================================================================" -ForegroundColor Green
