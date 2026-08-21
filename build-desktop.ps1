$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

& '.\.venv\Scripts\python.exe' -m pip install pyinstaller waitress pywebview
& '.\.venv\Scripts\python.exe' -m PyInstaller `
  --noconfirm `
  --clean `
  --onefile `
  --windowed `
  --name 'PulseBridge' `
  --add-data 'templates;templates' `
  --add-data 'static;static' `
  --hidden-import 'erpnext_sync' `
  --collect-all 'webview' `
  desktop_launcher.py

Write-Host "Desktop application created at $PSScriptRoot\dist\PulseBridge.exe"
