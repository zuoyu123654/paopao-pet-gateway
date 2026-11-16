# 泡泡陪伴网关 · 启动（前台，能看见日志）
# 用法：powershell -ExecutionPolicy Bypass -File .\start.ps1

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here
New-Item -ItemType Directory -Force -Path "$here\logs" | Out-Null

$py = Join-Path $here ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "没找到 .venv，先跑 install.ps1" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path "$here\config.json")) {
    Copy-Item "$here\config.example.json" "$here\config.json"
    Write-Host "没有 config.json，已从模板生成一份——先改它再启动。" -ForegroundColor Yellow
    exit 1
}

Write-Host "网关启动中…… http://127.0.0.1:9888/doctor" -ForegroundColor Cyan
& $py "$here\gateway.py"
