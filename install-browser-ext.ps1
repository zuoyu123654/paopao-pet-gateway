# 泡泡桌宠：装浏览器扩展（方案 A）
#
# 两种用法：
#   .\install-browser-ext.ps1                  # 手动：帮你打开扩展页和文件夹，点三下
#   .\install-browser-ext.ps1 -RestartEdge     # 全自动：改桌面快捷方式 → 关掉 Edge → 带参数启动
#
# -RestartEdge 会关掉当前的 Edge 窗口（标签页不一定自动恢复），确认没开着要留的东西再用。

param(
    [switch]$RestartEdge
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$ext = Join-Path $here 'browser_ext'
$edge = @(
    'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
    'C:\Program Files\Microsoft\Edge\Application\msedge.exe'
) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1

if (-not (Test-Path -LiteralPath (Join-Path $ext 'manifest.json'))) {
    Write-Host "扩展目录不完整：$ext" -ForegroundColor Red
    exit 1
}

# 1) 先把桌面快捷方式弄好（幂等）
& (Join-Path $here 'make-edge-shortcut.ps1')

if (-not $RestartEdge) {
    Start-Process -FilePath explorer.exe -ArgumentList $ext
    if ($edge) { Start-Process -FilePath $edge -ArgumentList 'edge://extensions' }
    Write-Host ''
    Write-Host '接下来三步：' -ForegroundColor Cyan
    Write-Host '  1) 扩展页右上角打开「开发人员模式」'
    Write-Host '  2) 点「加载解压缩的扩展」'
    Write-Host "  3) 选这个文件夹：$ext"
    Write-Host ''
    Write-Host '或者下次从桌面图标启动 Edge，扩展会自动带上（不用点）。'
    exit 0
}

# 2) 全自动：关掉 Edge → 用桌面快捷方式启动（带 --load-extension）
Write-Host '关闭 Edge…' -ForegroundColor Cyan
$procs = Get-Process msedge -ErrorAction SilentlyContinue
if ($procs) {
    $main = $procs | Where-Object { $_.MainWindowHandle -ne 0 }
    foreach ($p in $main) { $p.CloseMainWindow() | Out-Null }
    Start-Sleep -Seconds 3
    Get-Process msedge -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
}

$userLnk = Join-Path $env:USERPROFILE 'Desktop\Microsoft Edge.lnk'
if (Test-Path -LiteralPath $userLnk) {
    Start-Process -FilePath $userLnk
} elseif ($edge) {
    Start-Process -FilePath $edge -ArgumentList @("--load-extension=$ext", 'https://www.bilibili.com')
}
Write-Host '已启动。打开任意 B站视频播几秒，然后验证：' -ForegroundColor Cyan
Write-Host '  (Invoke-WebRequest http://127.0.0.1:9888/status -UseBasicParsing).Content | ConvertFrom-Json | % player'
