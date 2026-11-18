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

# 1) 本地 TTS（可选）
#    两种写法都认：
#      a) config.tts.guard.launch = {python, script, args[], workdir}  ← 推荐（网关自愈也用这一份）
#      b) config.mark = {gsv_root, gsv_python, gsv_config}            ← 旧写法，留着兼容
$ttsPort = if ($cfg.tts.api) { [int]([uri]$cfg.tts.api).Port } else { 9882 }
$markerGsv = Join-Path $logDir '.gsv-started'
$ttsExe = ''
$ttsArgs = @()
$ttsWorkDir = $here

$gl = $cfg.tts.guard.launch
$mark = $cfg.mark
if ($gl -and $gl.python -and $gl.script) {
    $ttsExe = [string]$gl.python
    $extraArgs = @()
    if ($gl.args) { $extraArgs = @($gl.args) }
    $ttsArgs = @([string]$gl.script) + $extraArgs
    if ($gl.workdir) { $ttsWorkDir = [string]$gl.workdir }
    # 没写 -a/-p 的话补上，否则 api_v2 会去占 9880
    if ($ttsArgs -notcontains '-a') { $ttsArgs += @('-a', '127.0.0.1') }
    if ($ttsArgs -notcontains '-p') { $ttsArgs += @('-p', "$ttsPort") }
}
elseif ($mark -and $mark.gsv_root -and $mark.gsv_python) {
    $ttsExe = [string]$mark.gsv_python
    $ttsArgs = @((Join-Path $mark.gsv_root 'api_v2.py'), '-a', '127.0.0.1', '-p', "$ttsPort")
    if ($mark.gsv_config) { $ttsArgs += @('-c', $mark.gsv_config) }
    $ttsWorkDir = [string]$mark.gsv_root
}

if ($ttsExe -and -not (Test-Port $ttsPort) -and -not (Test-RecentlyStarted $markerGsv 3)) {
    Set-Content -LiteralPath $markerGsv -Value (Get-Date -Format o)
    Start-Process -FilePath $ttsExe -ArgumentList $ttsArgs `
        -WorkingDirectory $ttsWorkDir -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $logDir 'gsv_stdout.log') `
        -RedirectStandardError (Join-Path $logDir 'gsv_stderr.log')
    Start-Sleep -Seconds 3
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
