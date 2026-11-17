# 泡泡桌宠：「看片窗口」——独立配置目录 + 调试端口（不依赖扩展）
#
# 2026-11-17 起，播放进度上报主用 Tampermonkey 用户脚本；这个窗口是 CDP 兜底：
#   * Chromium 136+ 在默认配置目录上会忽略 --remote-debugging-port，所以用独立配置目录；
#   * 扩展被 Edge 清掉也没关系，PlayerWatch 会从调试端口读 URL/currentTime。
# 缺点：这个窗口没有你平时的登录态（B站 1080p 可能要求登录；字幕走 bili CLI，不受影响）。

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$edge = @(
    'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
    'C:\Program Files\Microsoft\Edge\Application\msedge.exe'
) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1

if (-not $edge) {
    Write-Host '找不到 msedge.exe。' -ForegroundColor Red
    exit 1
}

$profileDir = Join-Path $env:LOCALAPPDATA 'paopao-edge-watch'
New-Item -ItemType Directory -Force -Path $profileDir | Out-Null

Start-Process -FilePath $edge -ArgumentList @(
    "--user-data-dir=$profileDir",
    '--remote-debugging-port=9222',
    '--no-first-run',
    '--no-default-browser-check',
    'https://www.bilibili.com'
)
Write-Host "已用独立配置目录启动：$profileDir" -ForegroundColor Green
Write-Host '调试端口 9222 已带；这个窗口里看 B站，扩展没了也能读 URL/进度。'
Write-Host '验证：(Invoke-WebRequest http://127.0.0.1:9888/status -UseBasicParsing).Content | ConvertFrom-Json | % player'
Write-Host '（如果这个配置目录的 Edge 已经开着，先关掉再重跑，新参数才会生效。）' -ForegroundColor DarkGray
