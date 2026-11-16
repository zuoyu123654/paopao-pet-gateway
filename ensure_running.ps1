# 泡泡陪伴网关 · 守护（幂等：缺谁补谁，都在就什么都不做）
# 用法：powershell -ExecutionPolicy Bypass -File .\ensure_running.ps1
# 建议：计划任务「登录时」+「每 5 分钟」各调一次；再配 watchdog.ps1 常驻

$ErrorActionPreference = 'SilentlyContinue'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$logDir = Join-Path $here 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

$cfgPath = Join-Path $here 'config.json'
if (-not (Test-Path $cfgPath)) { exit 0 }
$cfg = Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
$gwPort = if ($cfg.listen_port) { [int]$cfg.listen_port } else { 9888 }

function Test-Port([int]$port) {
    return (Test-NetConnection -ComputerName 127.0.0.1 -Port $port -InformationLevel Quiet -WarningAction SilentlyContinue)
}

function Test-RecentlyStarted([string]$marker, [int]$minutes) {
    if (-not (Test-Path -LiteralPath $marker)) { return $false }
    $age = (Get-Date) - (Get-Item -LiteralPath $marker).LastWriteTime
    return ($age.TotalMinutes -lt $minutes)
}

# 1) 本地 TTS（可选：config.mark = {gsv_root, gsv_python, gsv_config}）
$mark = $cfg.mark
if ($mark -and $mark.gsv_root -and $mark.gsv_python) {
    $ttsPort = if ($cfg.tts.api) { [int]([uri]$cfg.tts.api).Port } else { 9882 }
    $markerGsv = Join-Path $logDir '.gsv-started'
    if (-not (Test-Port $ttsPort) -and -not (Test-RecentlyStarted $markerGsv 3)) {
        Set-Content -LiteralPath $markerGsv -Value (Get-Date -Format o)
        $args = @((Join-Path $mark.gsv_root 'api_v2.py'), '-a', '127.0.0.1', '-p', "$ttsPort")
        if ($mark.gsv_config) { $args += @('-c', $mark.gsv_config) }
        Start-Process -FilePath $mark.gsv_python -ArgumentList $args `
            -WorkingDirectory $mark.gsv_root -WindowStyle Hidden `
            -RedirectStandardOutput (Join-Path $logDir 'gsv_stdout.log') `
            -RedirectStandardError (Join-Path $logDir 'gsv_stderr.log')
        Start-Sleep -Seconds 3
    }
}

# 2) 网关
$markerGw = Join-Path $logDir '.gateway-started'
if (-not (Test-Port $gwPort) -and -not (Test-RecentlyStarted $markerGw 2)) {
    Set-Content -LiteralPath $markerGw -Value (Get-Date -Format o)
    $py = Join-Path $here '.venv\Scripts\python.exe'
    if (-not (Test-Path $py)) { $py = 'python' }
    Start-Process -FilePath $py -ArgumentList @((Join-Path $here 'gateway.py')) `
        -WorkingDirectory $here -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $logDir 'stdout.log') `
        -RedirectStandardError (Join-Path $logDir 'stderr.log')
}
