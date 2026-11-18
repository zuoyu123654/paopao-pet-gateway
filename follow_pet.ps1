# 让网关跟桌宠同生共死：桌宠在 → 网关在；桌宠关 → 网关也关；桌宠再开 → 网关自己起来。
# 用法：powershell -ExecutionPolicy Bypass -File .\follow_pet.ps1
#      桌宠进程名不一样时：-ProcessName "你的进程名"
#
# 登录自启（免管理员）：启动文件夹放 vbs，隐藏窗口拉起本脚本：
#   %APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\泡泡伴生.vbs
#   CreateObject("Wscript.Shell").Run "powershell -ExecutionPolicy Bypass -WindowStyle Hidden -File ""<网关目录>\follow_pet.ps1""", 0, False
#
# 它只管网关（9888）；TTS（9882）由网关自己的 tts.guard 负责拉起。

param(
    [string]$ProcessName = "dsh-pet-standalone-webm-chat",
    [int]$IntervalSeconds = 3
)

$ErrorActionPreference = "SilentlyContinue"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$gateway = Join-Path $here "gateway.py"
$py = Join-Path $here ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $py)) { $py = "python" }
$logDir = Join-Path $here "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$outLog = Join-Path $logDir "stdout.log"
$errLog = Join-Path $logDir "stderr.log"
$escapeHere = [regex]::Escape($here)

function Get-GatewayProcs {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine -match $escapeHere -and $_.CommandLine -like "*gateway.py*" }
}

function Start-Gateway {
    Start-Process -FilePath $py -ArgumentList $gateway -WorkingDirectory $here `
        -WindowStyle Hidden -RedirectStandardOutput $outLog -RedirectStandardError $errLog
}

while ($true) {
    $pet = Get-Process -Name $ProcessName -ErrorAction SilentlyContinue
    $gw = @(Get-GatewayProcs)

    if ($pet -and $gw.Count -eq 0) {
        Start-Gateway
        Start-Sleep -Seconds 2
    }
    elseif (-not $pet -and $gw.Count -gt 0) {
        foreach ($p in $gw) { Stop-Process -Id $p.ProcessId -Force }
    }

    Start-Sleep -Seconds $IntervalSeconds
}
