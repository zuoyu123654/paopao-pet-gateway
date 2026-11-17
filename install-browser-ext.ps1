# 泡泡桌宠：浏览器播放进度上报（2026-11-17 起改用 Tampermonkey）
#
# 旧的 unpacked 扩展（browser_ext/manifest.json）已经退役：Edge 更新/开发者模式策略会把它清掉。
# 现在用商店扩展 Tampermonkey + 我们的用户脚本，脚本由网关直接提供，不会被“开发者模式扩展”清。
#
# 用法：
#   .\install-browser-ext.ps1                  # 打开 Tampermonkey 商店页 + 用户脚本安装页
#   .\install-browser-ext.ps1 -DedicatedProfile # 开一个带调试端口的独立「看片窗口」（CDP 兜底）

param(
    [switch]$RestartEdge,          # 兼容旧参数：现在不再改快捷方式，忽略
    [switch]$DedicatedProfile      # 独立配置目录 + 调试端口的看片窗口（不依赖扩展）
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$userScript = Join-Path $here 'browser_ext\paopao-player.user.js'
$edge = @(
    'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
    'C:\Program Files\Microsoft\Edge\Application\msedge.exe'
) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1

if ($DedicatedProfile) {
    & (Join-Path $here 'start-edge-watch.ps1')
    exit 0
}

if (-not (Test-Path -LiteralPath $userScript)) {
    Write-Host "用户脚本不完整：$userScript" -ForegroundColor Red
    exit 1
}

if (-not $edge) {
    Write-Host '找不到 msedge.exe。手动打开 Edge 也一样：' -ForegroundColor Yellow
} else {
    # 1) Tampermonkey 商店页（已装过就跳过）
    Start-Process -FilePath $edge -ArgumentList 'https://microsoftedge.microsoft.com/addons/search/tampermonkey'
    Start-Sleep -Seconds 1
    # 2) 用户脚本安装页（Tampermonkey 会接管 .user.js）
    Start-Process -FilePath $edge -ArgumentList 'http://127.0.0.1:9888/ext/paopao-player.user.js'
}

Write-Host ''
Write-Host '接下来三步：' -ForegroundColor Cyan
Write-Host '  1) 如果还没装，先在商店装 Tampermonkey（篡改猴）'
Write-Host '  2) 回到刚才打开的 .user.js 页面，点「安装」'
Write-Host '  3) edge://extensions → Tampermonkey「详细信息」→ 打开「允许用户脚本 / Allow user scripts」'
Write-Host ''
Write-Host '旧扩展（泡泡桌宠 · 播放进度上报）如果还装在 Edge 里，请到 edge://extensions 移除。' -ForegroundColor Yellow
if ($RestartEdge) {
    Write-Host '（-RestartEdge 已不再使用：不再改 Edge 快捷方式，也不再加载 unpacked 扩展。）' -ForegroundColor DarkGray
}
Write-Host '装完播个 B站视频，验证：' -ForegroundColor Cyan
Write-Host '  (Invoke-WebRequest http://127.0.0.1:9888/status -UseBasicParsing).Content | ConvertFrom-Json | % player'
