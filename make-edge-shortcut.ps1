# 泡泡桌宠：给 Edge 的桌面快捷方式加上「播放进度扩展」
#
# 背景：桌面那个 Microsoft Edge.lnk 如果是「公共桌面」的（属主 Administrators），
# 改它会失败。所以这里在**用户桌面**放一个同名快捷方式——资源管理器会优先用用户桌面的，
# 于是你点桌面上那个图标，Edge 就带上了 --load-extension。
#
# 幂等：已经带过参数就不再重复加；原始参数备份到 logs\edge-shortcuts-backup.json。
# 还原：删掉用户桌面的 Microsoft Edge.lnk（公共桌面那个一直没动）。

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$ext = Join-Path $here 'browser_ext'
$logDir = Join-Path $here 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir 'edge-shortcuts-backup.json'

if (-not (Test-Path -LiteralPath (Join-Path $ext 'manifest.json'))) {
    Write-Host "扩展目录不完整：$ext" -ForegroundColor Red
    exit 1
}

$edge = @(
    'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
    'C:\Program Files\Microsoft\Edge\Application\msedge.exe'
) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1

if (-not $edge) {
    Write-Host '找不到 msedge.exe（没装 Edge？）。Chrome 用户请改用 chrome://extensions 手动加载。' -ForegroundColor Yellow
    exit 1
}

$flag = "--load-extension=$ext"
$userLnk = Join-Path $env:USERPROFILE 'Desktop\Microsoft Edge.lnk'
$shell = New-Object -ComObject WScript.Shell

$backup = @()
if (Test-Path -LiteralPath $userLnk) {
    $old = $shell.CreateShortcut($userLnk)
    $backup += [pscustomobject]@{ link = $userLnk; args = [string]$old.Arguments }
    if ([string]$old.Arguments -match '--load-extension') {
        Write-Host "桌面快捷方式已经带扩展参数了，不重复加：$userLnk" -ForegroundColor Green
        Write-Host "  参数：$($old.Arguments)"
        exit 0
    }
}

$lnk = $shell.CreateShortcut($userLnk)
$lnk.TargetPath = $edge
$base = "--profile-directory=Default --source-shortcut-location=desktop"
if ($backup.Count -gt 0 -and $backup[0].args) {
    # 保留原来除 load-extension 之外的参数
    $base = ($backup[0].args -replace '--load-extension=\S+', '').Trim()
    if (-not $base) { $base = "" }
}
$lnk.Arguments = (($base + ' ' + $flag).Trim())
$lnk.WorkingDirectory = Split-Path -Parent $edge
$lnk.IconLocation = "$edge,0"
$lnk.Description = 'Microsoft Edge（带泡泡的播放进度扩展）'
$lnk.Save()

if ($backup.Count -gt 0) {
    $all = @()
    if (Test-Path -LiteralPath $log) {
        try { $all = @(Get-Content -LiteralPath $log -Raw -Encoding UTF8 | ConvertFrom-Json) } catch { $all = @() }
    }
    ($all + $backup) | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath $log -Encoding UTF8
}

$check = $shell.CreateShortcut($userLnk)
Write-Host "已写好桌面快捷方式：$userLnk" -ForegroundColor Green
Write-Host "  目标：$($check.TargetPath)"
Write-Host "  参数：$($check.Arguments)"
Write-Host ''
Write-Host '下次从桌面图标启动 Edge 就自动带上扩展。' -ForegroundColor Cyan
Write-Host '想现在就用（不重启 Edge）：edge://extensions → 开发人员模式 → 加载解压缩的扩展 → 选：'
Write-Host "  $ext"
