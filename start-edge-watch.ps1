# 泡泡桌宠：「看片窗口」——一个开好扩展的 Edge 窗口（免手动加载）
#
# 为什么需要它：Edge 157（Chromium 136+）在**默认用户目录**上会忽略 --load-extension，
# 实测带参数重启后扩展压根没加载；但换成**独立配置目录**就能自动加载（实测 source=ext）。
# 所以这个是「不想点三下、也不想动主浏览器」时的替代：独立配置、扩展预装、不用登录也能播。
#
# 缺点：这个窗口没有你平时的登录态（B站 1080p 可能要求登录；字幕是网关用 bili CLI 拿的，不受影响）。

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$ext = Join-Path $here 'browser_ext'
$edge = @(
    'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
    'C:\Program Files\Microsoft\Edge\Application\msedge.exe'
) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1

if (-not $edge) {
    Write-Host '找不到 msedge.exe。' -ForegroundColor Red
    exit 1
}
if (-not (Test-Path -LiteralPath (Join-Path $ext 'manifest.json'))) {
    Write-Host "扩展目录不完整：$ext" -ForegroundColor Red
    exit 1
}

$profileDir = Join-Path $env:LOCALAPPDATA 'paopao-edge-watch'
New-Item -ItemType Directory -Force -Path $profileDir | Out-Null

Start-Process -FilePath $edge -ArgumentList @(
    "--user-data-dir=$profileDir",
    "--load-extension=$ext",
    '--no-first-run',
    '--no-default-browser-check',
    'https://www.bilibili.com'
)
Write-Host "已用独立配置目录启动：$profileDir" -ForegroundColor Green
Write-Host '这个窗口里看 B站，扩展会自动加载（不用点开发者模式）。'
Write-Host '验证：(Invoke-WebRequest http://127.0.0.1:9888/status -UseBasicParsing).Content | ConvertFrom-Json | % player'
