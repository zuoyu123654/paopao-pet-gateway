# 泡泡桌宠：清理 Edge「--load-extension」旧参数（旧 unpacked 扩展已退役）
#
# 2026-11-17 起，播放进度改用 Tampermonkey 用户脚本（browser_ext\paopao-player.user.js），
# browser_ext\ 里已经没有 manifest.json。Edge 再用 --load-extension 指过去，启动时会弹
# 「扩展已失效 / 无法加载」。
#
# 2026-11-18：原来只清桌面那份，**任务栏固定项是另一份 lnk**，漏了 → Edge 还在弹。
# 现在桌面 / 公共桌面 / 任务栏 / 开始菜单（用户 + 全体）一起清。
#
# 幂等：没有这个参数就什么都不做；改前把原参数追加备份到 logs\edge-shortcuts-backup.json。

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$logDir = Join-Path $here 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir 'edge-shortcuts-backup.json'

$targets = @(
    [pscustomobject]@{ name = '桌面';            path = (Join-Path $env:USERPROFILE 'Desktop\Microsoft Edge.lnk') },
    [pscustomobject]@{ name = '公共桌面';        path = (Join-Path $env:PUBLIC 'Desktop\Microsoft Edge.lnk') },
    [pscustomobject]@{ name = '任务栏';          path = (Join-Path $env:APPDATA 'Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar\Microsoft Edge.lnk') },
    [pscustomobject]@{ name = '开始菜单(用户)';  path = (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Microsoft Edge.lnk') },
    [pscustomobject]@{ name = '开始菜单(全体)';  path = (Join-Path $env:ProgramData 'Microsoft\Windows\Start Menu\Programs\Microsoft Edge.lnk') }
)

$shell = New-Object -ComObject WScript.Shell
$changed = 0

foreach ($t in $targets) {
    if (-not (Test-Path -LiteralPath $t.path)) { continue }
    try {
        $lnk = $shell.CreateShortcut($t.path)
        $argsText = [string]$lnk.Arguments
        if ($argsText -notmatch '--load-extension') { continue }

        # 备份原参数（追加）
        $entry = [pscustomobject]@{ link = $t.path; args = $argsText; at = (Get-Date -Format o) }
        $all = @()
        if (Test-Path -LiteralPath $log) {
            try { $all = @(Get-Content -LiteralPath $log -Raw -Encoding UTF8 | ConvertFrom-Json) } catch { }
        }
        @(@($all) + $entry) | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath $log -Encoding UTF8

        # 只删指向 browser_ext 的那段（别的 --load-extension 不碰）
        $newArgs = ($argsText -replace '--load-extension=(?:"[^"]*browser_ext[^"]*"|\S*browser_ext\S*)', '') `
            -replace '\s{2,}', ' '
        $newArgs = $newArgs.Trim()

        $lnk.Arguments = $newArgs
        if (-not $lnk.Description) { $lnk.Description = 'Microsoft Edge' }
        $lnk.Save()

        $check = $shell.CreateShortcut($t.path)
        if ([string]$check.Arguments -match '--load-extension.*browser_ext') {
            Write-Host "[$($t.name)] 保存后还带着旧参数，可能被系统缓存：请右键任务栏图标取消固定再重新固定。" -ForegroundColor Yellow
        } else {
            Write-Host "[$($t.name)] 已清理" -ForegroundColor Green
        }
        Write-Host "  旧：$argsText"
        Write-Host "  新：$newArgs"
        $changed++
    } catch {
        Write-Host "[$($t.name)] 改不了（$($_.Exception.Message)）——权限或只读，跳过。" -ForegroundColor Yellow
    }
}

if ($changed -eq 0) {
    Write-Host 'Edge 的桌面 / 任务栏 / 开始菜单快捷方式都没有旧扩展参数，已经干净。' -ForegroundColor Green
} else {
    Write-Host ''
    Write-Host '已清完。Edge 如果还弹一次，是它缓存了启动参数：' -ForegroundColor Yellow
    Write-Host '  1) 完全退出 Edge（任务管理器里确认没有 msedge.exe）；'
    Write-Host '  2) 从快捷方式重新打开；'
    Write-Host '  3) 还不行就右键任务栏图标「取消固定」→ 重新固定。'
    Write-Host ''
    Write-Host '旧扩展如果还装在 Edge 里，请到 edge://extensions 移除「泡泡桌宠 · 播放进度上报」。'
    Write-Host '现在用 Tampermonkey + browser_ext\paopao-player.user.js（网关 /ext/ 直接提供）。'
}
