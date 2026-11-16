# 泡泡桌宠：带调试端口的 Edge（B站视频模式「精确进度」用）
#
# 为什么要单独一个窗口：Chromium 136+（Edge 157 也是）**不允许在默认用户目录上开
# --remote-debugging-port**，会直接忽略这个参数。所以这里用一个独立的配置目录，
# 只给「看片时让泡泡知道放到哪」用；不登录 B站也能播（字幕是 bili CLI 拿的，
# 跟浏览器登不登录没关系）。
#
# 用法：双击本脚本，或者把它发到桌面做快捷方式。
# 网关那边不用改配置（默认 player_watch.cdp_port = 9222）。

$ErrorActionPreference = 'Stop'

$edge = 'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'
if (-not (Test-Path -LiteralPath $edge)) {
    $edge = 'C:\Program Files\Microsoft\Edge\Application\msedge.exe'
}
if (-not (Test-Path -LiteralPath $edge)) {
    Write-Host '找不到 msedge.exe，改成你自己的 Edge 路径。'
    exit 1
}

$profileDir = Join-Path $env:LOCALAPPDATA 'paopao-edge-watch'
New-Item -ItemType Directory -Force -Path $profileDir | Out-Null

Start-Process -FilePath $edge -ArgumentList @(
    '--remote-debugging-port=9222',
    "--user-data-dir=$profileDir",
    '--no-first-run',
    '--no-default-browser-check',
    'https://www.bilibili.com'
)

Start-Sleep -Seconds 4
try {
    $ver = Invoke-WebRequest -Uri 'http://127.0.0.1:9222/json/version' -UseBasicParsing -TimeoutSec 3
    Write-Host '调试端口已开：' $ver.StatusCode
    Write-Host '这个窗口里看 B站，泡泡就能读到「现在放到第几秒」。'
} catch {
    Write-Host '端口没起来：Edge 可能把这个参数忽略了（版本太新/策略限制）。'
}
