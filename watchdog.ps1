# 泡泡陪伴网关 · 常驻守护
# 每 5 分钟跑一次 ensure_running.ps1：缺谁补谁（TTS / 网关），都在就什么都不做。
# 用法：powershell -ExecutionPolicy Bypass -File .\watchdog.ps1
#
# 登录自启（免管理员）：在启动文件夹放一个 vbs，隐藏窗口拉起本脚本：
#   %APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\泡泡语音与网关.vbs
#   CreateObject("Wscript.Shell").Run "powershell -ExecutionPolicy Bypass -WindowStyle Hidden -File ""<网关目录>\watchdog.ps1""", 0, False
#
# 只想「跟着桌宠起停」的话，用 follow_pet.ps1，别用这个。

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$ensure = Join-Path $here 'ensure_running.ps1'
$logDir = Join-Path $here 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir 'watchdog.log'

if (-not (Test-Path -LiteralPath $ensure)) {
    Add-Content -LiteralPath $log -Value "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] 找不到 ensure_running.ps1，watchdog 退出"
    exit 1
}

Add-Content -LiteralPath $log -Value "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] watchdog start (pid $PID)"

while ($true) {
    try {
        & $ensure
    } catch {
        Add-Content -LiteralPath $log -Value "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] ensure failed: $($_.Exception.Message)"
    }
    Start-Sleep -Seconds 300
}
