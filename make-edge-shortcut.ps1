# 泡泡桌宠：清理旧的 Edge「--load-extension」参数（旧扩展已退役）
#
# 2026-11-17 起，播放进度改用 Tampermonkey 用户脚本（browser_ext\paopao-player.user.js），
# 不再用 unpacked 扩展。这个脚本只做一件事：把用户桌面 Microsoft Edge.lnk 里
# 旧的 --load-extension=...\browser_ext 参数删掉，避免 Edge 每次启动还去加载它。
#
# 幂等：没有这个参数就什么都不做；原始参数备份到 logs\edge-shortcuts-backup.json。

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$logDir = Join-Path $here 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir 'edge-shortcuts-backup.json'

$userLnk = Join-Path $env:USERPROFILE 'Desktop\Microsoft Edge.lnk'
if (-not (Test-Path -LiteralPath $userLnk)) {
    Write-Host "桌面没有 Microsoft Edge.lnk，不需要清理。" -ForegroundColor Green
    exit 0
}

$shell = New-Object -ComObject WScript.Shell
$old = $shell.CreateShortcut($userLnk)
$argsText = [string]$old.Arguments
if ($argsText -notmatch '--load-extension') {
    Write-Host "桌面 Edge 快捷方式没有 --load-extension 参数，已经干净。" -ForegroundColor Green
    exit 0
}

$backup = @([pscustomobject]@{ link = $userLnk; args = $argsText })
if (Test-Path -LiteralPath $log) {
    try {
        $all = @(Get-Content -LiteralPath $log -Raw -Encoding UTF8 | ConvertFrom-Json)
        $backup = @($all) + $backup
    } catch { }
}
$backup | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath $log -Encoding UTF8

$newArgs = ($argsText -replace '--load-extension=(?:"[^"]*browser_ext[^"]*"|\S*browser_ext\S*)', '').Trim()
$lnk = $shell.CreateShortcut($userLnk)
$lnk.Arguments = $newArgs
$lnk.Description = 'Microsoft Edge'
$lnk.Save()

Write-Host "已从桌面 Edge 快捷方式移除旧扩展参数：" -ForegroundColor Green
Write-Host "  旧：$argsText"
Write-Host "  新：$newArgs"
Write-Host ''
Write-Host '旧扩展如果还装在 Edge 里，请到 edge://extensions 移除「泡泡桌宠 · 播放进度上报」。' -ForegroundColor Yellow
Write-Host '现在用 Tampermonkey + browser_ext\paopao-player.user.js（网关 /ext/ 直接提供）。'
