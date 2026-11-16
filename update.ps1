# 泡泡陪伴网关 · 更新（拉最新代码 + 补依赖，不碰你的配置和数据）
# 用法：powershell -ExecutionPolicy Bypass -File .\update.ps1

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

Write-Host "== 1/3 拉最新代码" -ForegroundColor Cyan
if (-not (Test-Path "$here\.git")) {
    Write-Host "这不是 git 仓库（可能是解压的包）。请手动下载新版本覆盖，config.json 不会被覆盖。" -ForegroundColor Yellow
    exit 1
}
$before = git rev-parse HEAD
git pull --ff-only
$after = git rev-parse HEAD
if ($before -eq $after) {
    Write-Host "  已经是最新（$($after.Substring(0,7))）"
} else {
    Write-Host "  $($before.Substring(0,7)) → $($after.Substring(0,7))"
}

Write-Host "== 2/3 补依赖" -ForegroundColor Cyan
$py = Join-Path $here ".venv\Scripts\python.exe"
if (Test-Path $py) {
    & $py -m pip install -r "$here\requirements.txt"
} else {
    Write-Host "  没找到 .venv，跳过（先跑 install.ps1）" -ForegroundColor Yellow
}

Write-Host "== 3/3 完成" -ForegroundColor Green
Write-Host "  重启网关才会生效："
Write-Host "    前台：.\start.ps1"
Write-Host "    或结束旧进程后跑 .\ensure_running.ps1"
Write-Host "  config.json / logs / audio / ledger.jsonl 都没被动过。"
