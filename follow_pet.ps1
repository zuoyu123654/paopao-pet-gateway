# 让网关跟桌宠同生共死：桌宠在 → 网关在；桌宠关 → 网关也关；桌宠再开 → 网关自己起来。
# 用法：powershell -ExecutionPolicy Bypass -File .\follow_pet.ps1
#      桌宠进程名不一样时：-ProcessName "你的进程名"
#
# 登录自启（免管理员）：启动文件夹放一个 vbs，隐藏窗口拉起本脚本：
#   %APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\泡泡伴生.vbs
#   CreateObject("Wscript.Shell").Run "powershell -ExecutionPolicy Bypass -WindowStyle Hidden -File ""<网关目录>\follow_pet.ps1""", 0, False
#
# 它只管网关（9888）；TTS（9882）由网关自己的 tts.guard 负责拉起。
#
# 2026-11-18 加：主动识屏「当日熔断」自愈。
#   桌宠自己的限流器有熔断：连续 3 次识屏请求失败（比如上游 402 余额不足）就
#   paused_until_date=当天，**一整天不再主动看屏**，恢复要等到第二天。实测
#   14:57 余额空了 3 分钟，她一下午就再没主动看过屏（网关的保底 look 还在跑，
#   所以看着像"频率变低"）。这里每 60 秒看一眼状态文件：熔断中 + 上游探针
#   通了 → 清熔断 + 重启桌宠（她只在启动时读这个状态，内存里还记着"今天不看"）。
#   熔断没解除、或者上游还挂着，就什么都不做。

param(
    [string]$ProcessName = "dsh-pet-standalone-webm-chat",
    [int]$IntervalSeconds = 3,
    [string]$PetExe = ""
)

$ErrorActionPreference = "SilentlyContinue"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$gateway = Join-Path $here "gateway.py"
$py = Join-Path $here ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $py)) {
    # 没有 .venv 时优先用 DSH 自带的 Python：PATH 上的 python 常常是 WindowsApps 的空壳，
    # 用它拉网关会一启动就报 ModuleNotFoundError，网关再也起不来（2026-11-18 补）。
    $dshPy = Join-Path $env:USERPROFILE ".dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
    if (Test-Path -LiteralPath $dshPy) { $py = $dshPy } else { $py = "python" }
}
$logDir = Join-Path $here "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$outLog = Join-Path $logDir "stdout.log"
$errLog = Join-Path $logDir "stderr.log"
$escapeHere = [regex]::Escape($here)

# 网关端口 + 桌宠状态文件（熔断自愈用）
$gwPort = 9888
$cfgPath = Join-Path $here "config.json"
if (Test-Path -LiteralPath $cfgPath) {
    try {
        $cfg = Get-Content -LiteralPath $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($cfg.listen_port) { $gwPort = [int]$cfg.listen_port }
    } catch { }
}
$petState = Join-Path $env:APPDATA "dsh-pet-standalone-webm-chat\proactive_screen_state.json"
$guardLog = Join-Path $logDir "pet_guard.log"
$lastGuard = [datetime]::MinValue
$lastRestart = [datetime]::MinValue

function Get-GatewayProcs {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine -match $escapeHere -and $_.CommandLine -like "*gateway.py*" }
}

function Start-Gateway {
    Start-Process -FilePath $py -ArgumentList $gateway -WorkingDirectory $here `
        -WindowStyle Hidden -RedirectStandardOutput $outLog -RedirectStandardError $errLog
}

function Write-GuardLog([string]$msg) {
    Add-Content -LiteralPath $guardLog -Value "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $msg" -Encoding UTF8
}

# 上游探针：真发一条 1 token 的请求。余额没了会 402，探针就失败，不动手。
function Test-Upstream {
    $body = '{"model":"deepseek-v4-flash","messages":[{"role":"user","content":"ping"}],"max_tokens":1,"stream":false}'
    try {
        $r = Invoke-RestMethod -Uri "http://127.0.0.1:$gwPort/v1/chat/completions" -Method Post `
            -Body $body -ContentType "application/json; charset=utf-8" -TimeoutSec 25
    } catch {
        return $false
    }
    if ($null -eq $r) { return $false }
    if ($r.error) { return $false }
    return $true
}

function Clear-ProactiveBreaker {
    if (-not (Test-Path -LiteralPath $petState)) { return }
    $pet = @(Get-Process -Name $ProcessName -ErrorAction SilentlyContinue)
    $exe = $PetExe
    if (-not $exe -and $pet.Count -gt 0) { $exe = $pet[0].Path }
    if (-not $exe -or -not (Test-Path -LiteralPath $exe)) {
        Write-GuardLog "熔断中，但找不到桌宠 exe，跳过"
        return
    }
    if (((Get-Date) - $script:lastRestart).TotalMinutes -lt 10) {
        Write-GuardLog "熔断中，10 分钟内刚重启过，先等"
        return
    }
    try {
        $st = Get-Content -LiteralPath $petState -Raw -Encoding UTF8 | ConvertFrom-Json
        $st.consecutive_failures = 0
        $st.paused_until_date = ""
        $tmp = $petState + ".guard-tmp"
        ($st | ConvertTo-Json -Depth 8) | Set-Content -LiteralPath $tmp -Encoding UTF8
        Move-Item -LiteralPath $tmp -Destination $petState -Force
    } catch {
        Write-GuardLog "写状态文件失败：$($_.Exception.Message)"
        return
    }
    Write-GuardLog "熔断已清（上游通了），重启桌宠让她重新读状态"
    foreach ($p in $pet) { Stop-Process -Id $p.Id -Force }
    Start-Sleep -Seconds 2
    Start-Process -FilePath $exe -WorkingDirectory (Split-Path -Parent $exe)
    $script:lastRestart = Get-Date
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

    # 主动识屏熔断自愈（60 秒一次，只有真熔断 + 上游通才动）
    if (((Get-Date) - $lastGuard).TotalSeconds -ge 60) {
        $lastGuard = Get-Date
        try {
            if (Test-Path -LiteralPath $petState) {
                $st = Get-Content -LiteralPath $petState -Raw -Encoding UTF8 | ConvertFrom-Json
                $today = (Get-Date).ToString('yyyy-MM-dd')
                if ($st.paused_until_date -eq $today) {
                    if (Test-Upstream) {
                        Clear-ProactiveBreaker
                    } else {
                        Write-GuardLog "熔断中（连续失败 $($st.consecutive_failures)），上游探针没通，先不动"
                    }
                }
            }
        } catch {
            Write-GuardLog "熔断自愈检查出错：$($_.Exception.Message)"
        }
    }

    Start-Sleep -Seconds $IntervalSeconds
}
