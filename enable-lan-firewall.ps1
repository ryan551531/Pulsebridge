$ErrorActionPreference = 'Stop'

netsh advfirewall firewall delete rule name="PulseBridge LAN 8088" | Out-Null
netsh advfirewall firewall add rule name="PulseBridge LAN 8088" dir=in action=allow protocol=TCP localport=8088 profile=private,domain remoteip=localsubnet

Write-Host ''
Write-Host 'PulseBridge is allowed from the local subnet on TCP port 8088.' -ForegroundColor Green
Write-Host 'Open http://192.168.0.32:8088 from another local-network device.'
Read-Host 'Press Enter to close'
