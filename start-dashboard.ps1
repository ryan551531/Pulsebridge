$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

if (-not (Test-Path -LiteralPath '.venv')) {
    python -m venv .venv
}

& '.\.venv\Scripts\python.exe' -m pip install -r requirements.txt

$env:DASHBOARD_HOST = '0.0.0.0'
$env:PULSEBRIDGE_ADMIN_IPS = '192.168.0.32,127.0.0.1,::1'
$server = Start-Process -FilePath '.\.venv\Scripts\python.exe' -ArgumentList '-m', 'waitress', '--listen=0.0.0.0:8088', 'app:app' -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru

$dashboardUrl = 'http://127.0.0.1:8088'
$ready = $false
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    try {
        $response = Invoke-WebRequest -Uri $dashboardUrl -UseBasicParsing -TimeoutSec 1
        if ($response.StatusCode -eq 200) {
            $ready = $true
            break
        }
    } catch {
        Start-Sleep -Milliseconds 500
    }
}

if (-not $ready) {
    Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
    throw 'The dashboard did not start successfully.'
}

Start-Process $dashboardUrl
Write-Host ''
Write-Host 'PulseBridge is running at http://127.0.0.1:8088' -ForegroundColor Green
Write-Host 'Local network access: http://192.168.0.32:8088' -ForegroundColor Green
Write-Host 'Press Enter to stop the website.'
Read-Host
Stop-Process -Id $server.Id -ErrorAction SilentlyContinue
